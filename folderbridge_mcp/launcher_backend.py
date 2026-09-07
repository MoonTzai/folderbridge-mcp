from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from . import __version__
from .capabilities import CAPABILITY_NAMES, normalize_capability_names
from .config import ConfigError, MAX_WORKSPACES, canonical_workspaces, config_is_trusted, load_config
from .flight_recorder import FlightRecorder
from .process_control import (
    ProcessGenerationQuiescence,
    owned_process_group_kwargs,
    prove_owned_process_generation_quiescent,
    terminate_owned_process_tree,
)
from .tunnel_health import (
    TunnelAdminHealthMonitor,
    TunnelAdminSnapshot,
    TunnelHealthSnapshot,
    evaluate_tunnel_health,
)
from .user_paths import user_config_root


LAUNCHER_SETTINGS_VERSION = 4
MAX_SETTINGS_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT = 256 * 1024
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
TUNNEL_ID_RE = re.compile(r"^tunnel_[A-Za-z0-9]{16,128}$")
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+"),
)


class LauncherError(RuntimeError):
    pass


@dataclass
class LauncherSettings:
    version: int = LAUNCHER_SETTINGS_VERSION
    workspaces: list[str] = field(default_factory=list)
    access_mode: str = "read_only"
    profile: str = "folderbridge"
    tunnel_id: str = ""
    tunnel_client_path: str = ""
    allow_tasks: bool = False
    capabilities: list[str] = field(default_factory=list)
    language: str = "zh"
    configured_fingerprint: str = ""

    def validate(self, *, require_tunnel_id: bool = False) -> tuple[Path, ...]:
        if self.version != LAUNCHER_SETTINGS_VERSION or isinstance(self.version, bool):
            raise LauncherError("启动器配置版本无效")
        try:
            workspaces = canonical_workspaces(self.workspaces)
        except (ConfigError, OSError) as exc:
            raise LauncherError(str(exc)) from exc
        if any(ord(character) < 32 for workspace in workspaces for character in str(workspace)):
            raise LauncherError("工作区路径不能包含控制字符")
        if self.access_mode not in {"read_only", "read_write"}:
            raise LauncherError("请选择只读或读写模式")
        if not PROFILE_RE.fullmatch(self.profile):
            raise LauncherError("Profile 只能包含字母、数字、下划线和短横线")
        if require_tunnel_id and not TUNNEL_ID_RE.fullmatch(self.tunnel_id):
            raise LauncherError("首次配置需要有效的 tunnel_... ID")
        if self.tunnel_id and not TUNNEL_ID_RE.fullmatch(self.tunnel_id):
            raise LauncherError("Tunnel ID 格式无效")
        if not isinstance(self.allow_tasks, bool):
            raise LauncherError("任务开关配置无效")
        if self.language not in {"zh", "en"}:
            raise LauncherError("界面语言配置无效")
        try:
            normalize_capability_names(self.capabilities)
        except (TypeError, ValueError) as exc:
            raise LauncherError(f"全局能力配置无效：{exc}") from exc
        if self.allow_tasks:
            try:
                for workspace in workspaces:
                    load_config(workspace, required=False)
            except ConfigError as exc:
                raise LauncherError(str(exc)) from exc
        return workspaces

    def fingerprint(self) -> str:
        try:
            workspaces = canonical_workspaces(self.workspaces)
        except (ConfigError, OSError) as exc:
            raise LauncherError(str(exc)) from exc
        payload = {
            "version": __version__,
            "workspaces": [str(workspace) for workspace in workspaces],
            "access_mode": self.access_mode,
            "profile": self.profile,
            "tunnel_id": self.tunnel_id,
            "allow_tasks": self.allow_tasks,
            "capabilities": list(normalize_capability_names(self.capabilities)),
            "mcp_command": mcp_command(workspaces, self.access_mode, self.allow_tasks, self.capabilities),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class LauncherSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or launcher_settings_path()

    def load(self) -> LauncherSettings:
        try:
            if self.path.is_symlink() or _is_reparse_point(self.path):
                return LauncherSettings()
            with self.path.open("rb") as handle:
                data = handle.read(MAX_SETTINGS_BYTES + 1)
            if len(data) > MAX_SETTINGS_BYTES:
                return LauncherSettings()
            raw = json.loads(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return LauncherSettings()
        if not isinstance(raw, dict):
            return LauncherSettings()
        if raw.get("version") == 1:
            legacy_allowed = {
                "version",
                "workspace",
                "access_mode",
                "profile",
                "tunnel_id",
                "tunnel_client_path",
                "allow_tasks",
                "configured_fingerprint",
            }
            if set(raw).difference(legacy_allowed) or not isinstance(raw.get("workspace", ""), str):
                return LauncherSettings()
            legacy_workspace = raw.get("workspace", "").strip()
            raw = {
                "version": LAUNCHER_SETTINGS_VERSION,
                "workspaces": [legacy_workspace] if legacy_workspace else [],
                "access_mode": raw.get("access_mode", "read_only"),
                "profile": raw.get("profile", "folderbridge"),
                "tunnel_id": raw.get("tunnel_id", ""),
                "tunnel_client_path": raw.get("tunnel_client_path", ""),
                "allow_tasks": raw.get("allow_tasks", False),
                "capabilities": [],
                "language": "zh",
                "configured_fingerprint": raw.get("configured_fingerprint", ""),
            }
        elif raw.get("version") == 2:
            version_two_allowed = {
                "version",
                "workspaces",
                "access_mode",
                "profile",
                "tunnel_id",
                "tunnel_client_path",
                "allow_tasks",
                "configured_fingerprint",
            }
            if set(raw).difference(version_two_allowed):
                return LauncherSettings()
            raw = {
                **raw,
                "version": LAUNCHER_SETTINGS_VERSION,
                "capabilities": [],
                "language": "zh",
            }
        elif raw.get("version") == 3:
            version_three_allowed = {
                "version",
                "workspaces",
                "access_mode",
                "profile",
                "tunnel_id",
                "tunnel_client_path",
                "allow_tasks",
                "capabilities",
                "configured_fingerprint",
            }
            if set(raw).difference(version_three_allowed):
                return LauncherSettings()
            raw = {
                **raw,
                "version": LAUNCHER_SETTINGS_VERSION,
                "language": "zh",
            }
        # 0.3.x exposed ComfyUI as a global capability. In 0.4 it is the
        # first hot-loaded extension, so preserve the rest of the v3 launcher
        # settings while dropping only that retired capability value.
        if raw.get("version") == LAUNCHER_SETTINGS_VERSION and isinstance(raw.get("capabilities"), list):
            raw = {**raw, "capabilities": [item for item in raw["capabilities"] if item != "comfyui"]}
        allowed = set(LauncherSettings.__dataclass_fields__)
        if set(raw).difference(allowed):
            return LauncherSettings()
        try:
            settings = LauncherSettings(**raw)
        except TypeError:
            return LauncherSettings()
        if settings.version != LAUNCHER_SETTINGS_VERSION or isinstance(settings.version, bool):
            return LauncherSettings()
        # Deliberately reject any unexpected type instead of coercing values.
        if (
            not isinstance(settings.workspaces, list)
            or len(settings.workspaces) > MAX_WORKSPACES
            or not all(isinstance(value, str) for value in settings.workspaces)
            or not all(
                isinstance(value, str)
                for value in (
                    settings.access_mode,
                    settings.profile,
                    settings.tunnel_id,
                    settings.tunnel_client_path,
                    settings.language,
                    settings.configured_fingerprint,
                )
            )
            or not isinstance(settings.allow_tasks, bool)
            or settings.language not in {"zh", "en"}
            or not isinstance(settings.capabilities, list)
            or not all(isinstance(value, str) for value in settings.capabilities)
        ):
            return LauncherSettings()
        try:
            normalize_capability_names(settings.capabilities)
        except ValueError:
            return LauncherSettings()
        return settings

    def save(self, settings: LauncherSettings) -> None:
        payload = json.dumps(asdict(settings), ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass


def launcher_settings_path() -> Path:
    return user_config_root() / "launcher.json"


def checkout_launcher_path() -> Path:
    return Path(__file__).resolve().parents[1] / "folderbridge_launcher.py"


def console_python() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() in {"pythonw.exe", "pythonw"}:
        candidate = executable.with_name("python.exe" if executable.suffix.lower() == ".exe" else "python")
        if candidate.is_file():
            return candidate
    return executable


def _workspace_tuple(workspaces: tuple[Path, ...] | list[Path] | Path) -> tuple[Path, ...]:
    roots = (workspaces,) if isinstance(workspaces, Path) else tuple(workspaces)
    if not roots:
        raise LauncherError("请至少添加一个本地工作区")
    return roots


def mcp_argv(
    workspaces: tuple[Path, ...] | list[Path] | Path,
    access_mode: str,
    allow_tasks: bool,
    capabilities: Iterable[str] = (),
) -> list[str]:
    roots = _workspace_tuple(workspaces)
    normalized_capabilities = normalize_capability_names(capabilities)
    if getattr(sys, "frozen", False):
        argv = [str(Path(sys.executable).resolve()), "serve"]
    else:
        argv = [str(console_python()), str(checkout_launcher_path()), "serve"]
    for workspace in roots:
        argv.extend(("--workspace", str(workspace)))
    if access_mode == "read_only":
        argv.append("--read-only")
    for capability in normalized_capabilities:
        argv.extend(("--capability", capability))
    if allow_tasks:
        argv.append("--allow-tasks")
    return argv


def mcp_command(
    workspaces: tuple[Path, ...] | list[Path] | Path,
    access_mode: str,
    allow_tasks: bool,
    capabilities: Iterable[str] = (),
) -> str:
    argv = mcp_argv(workspaces, access_mode, allow_tasks, capabilities)
    # tunnel-client parses --mcp-command with POSIX-style escaping even on
    # Windows. Backslashes would therefore be consumed (C:\Users -> C:Users).
    # Windows accepts forward slashes for these absolute executable and
    # workspace paths, while list2cmdline still quotes arguments with spaces.
    if os.name == "nt":
        argv = [argument.replace("\\", "/") for argument in argv]
    return subprocess.list2cmdline(argv) if os.name == "nt" else _posix_join(argv)


def render_client_config(
    workspaces: tuple[Path, ...] | list[Path] | Path,
    access_mode: str,
    allow_tasks: bool,
    output_format: str,
    capabilities: Iterable[str] = (),
) -> str:
    """Render a portable stdio client configuration without starting the server."""

    argv = mcp_argv(workspaces, access_mode, allow_tasks, capabilities)
    command, args = argv[0], argv[1:]
    if output_format == "tunnel":
        return mcp_command(workspaces, access_mode, allow_tasks, capabilities)
    if output_format == "json":
        return json.dumps(
            {"mcpServers": {"folderbridge": {"command": command, "args": args}}},
            ensure_ascii=False,
            indent=2,
        )
    if output_format == "toml":
        encoded_args = ", ".join(json.dumps(item, ensure_ascii=False) for item in args)
        return (
            "[mcp_servers.folderbridge]\n"
            f"command = {json.dumps(command, ensure_ascii=False)}\n"
            f"args = [{encoded_args}]"
        )
    raise LauncherError("客户端配置格式必须是 tunnel、json 或 toml")


def find_tunnel_client(explicit: str = "") -> Path | None:
    if explicit.strip():
        candidate = Path(explicit.strip().strip('"')).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.is_file() and resolved.name.lower() in {"tunnel-client", "tunnel-client.exe"}:
            return resolved
        return None
    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).resolve().with_name("tunnel-client.exe" if os.name == "nt" else "tunnel-client")
        if sibling.is_file():
            return sibling
    found = shutil.which("tunnel-client")
    return Path(found).resolve(strict=True) if found else None


def build_init_argv(executable: Path, settings: LauncherSettings, workspaces: tuple[Path, ...]) -> list[str]:
    return [
        str(executable),
        "init",
        "--sample",
        "sample_mcp_stdio_local",
        "--profile",
        settings.profile,
        "--tunnel-id",
        settings.tunnel_id,
        "--mcp-command",
        mcp_command(workspaces, settings.access_mode, settings.allow_tasks, settings.capabilities),
        # The launcher owns this profile after the user explicitly applies the
        # form. Re-applying settings must update it instead of failing merely
        # because the same profile name already exists.
        "--force",
    ]


def build_doctor_argv(executable: Path, profile: str) -> list[str]:
    return [str(executable), "doctor", "--profile", profile, "--explain"]


def build_run_argv(executable: Path, profile: str) -> list[str]:
    return [str(executable), "run", "--profile", profile]


def build_structured_admin_run_argv(
    executable: Path,
    profile: str,
    health_url_file: Path,
) -> list[str]:
    """Build the v0.0.14+ normal-run admin argv without changing the legacy path."""

    return [
        *build_run_argv(executable, profile),
        "--health.listen-addr",
        "127.0.0.1:0",
        "--health.url-file",
        str(health_url_file),
    ]


def control_plane_environment(api_key: str) -> dict[str, str]:
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        # tunnel-client starts this same one-file executable as an independent
        # stdio MCP server. PyInstaller 6.9+ otherwise treats it as a worker of
        # the GUI instance, and 6.22.1+ rejects tunnel-client as the parent.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    memory_key = api_key.strip()
    if len(memory_key) > 4096 or "\x00" in memory_key:
        raise LauncherError("Runtime API Key 格式无效")
    if memory_key:
        env["CONTROL_PLANE_API_KEY"] = memory_key
    elif "CONTROL_PLANE_API_KEY" in env:
        inherited_key = env["CONTROL_PLANE_API_KEY"].strip()
        if len(inherited_key) > 4096 or "\x00" in inherited_key:
            raise LauncherError("Runtime API Key 格式无效")
        if inherited_key:
            env["CONTROL_PLANE_API_KEY"] = inherited_key
        else:
            env.pop("CONTROL_PLANE_API_KEY", None)
    if not env.get("CONTROL_PLANE_API_KEY"):
        raise LauncherError("请输入 Runtime API Key，或预先设置 CONTROL_PLANE_API_KEY")
    return env


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = text
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<已隐藏>")
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: match.group(1) + "<已隐藏>", redacted)
        else:
            redacted = pattern.sub("<已隐藏>", redacted)
    return redacted


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool
    truncated: bool


@dataclass(frozen=True)
class TunnelAdminCapability:
    supported: bool
    version: tuple[int, int, int] | None
    version_text: str
    capabilities: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class TunnelRunPlan:
    argv: list[str]
    health_url_file: Path | None
    admin_capability: TunnelAdminCapability


class _BoundedCommandReader(threading.Thread):
    def __init__(self, stream: BinaryIO) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def run(self) -> None:
        half = MAX_COMMAND_OUTPUT // 2
        while True:
            try:
                chunk = self.stream.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self.total += len(chunk)
            head_room = half - len(self.head)
            if head_room > 0:
                self.head.extend(chunk[:head_room])
                chunk = chunk[head_room:]
            if chunk:
                self.tail.extend(chunk)
                if len(self.tail) > half:
                    del self.tail[: len(self.tail) - half]

    def result(self) -> tuple[bytes, bool]:
        if self.total <= MAX_COMMAND_OUTPUT:
            return bytes(self.head + self.tail), False
        marker = b"\n... command output omitted ...\n"
        return bytes(self.head) + marker + bytes(self.tail), True


def run_short_command(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: int = 30,
) -> CommandResult:
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise LauncherError(f"无法启动 {Path(argv[0]).name}: {exc}") from exc
    assert process.stdout is not None
    reader = _BoundedCommandReader(process.stdout)
    reader.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_owned_process_tree(process, hide_window=True)
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
    reader.join(timeout=5)
    process.stdout.close()
    reader.join(timeout=1)
    data, truncated = reader.result()
    return CommandResult(
        exit_code=exit_code,
        output=data.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=truncated,
    )


_TUNNEL_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_STRUCTURED_ADMIN_FLAGS = ("--health.listen-addr", "--health.url-file")


def probe_tunnel_admin_capability(
    executable: Path,
    *,
    env: dict[str, str],
) -> TunnelAdminCapability:
    """Fail closed unless both v0.0.14+ and the exact normal-run flags are declared."""

    probe_env = dict(env)
    probe_env.pop("CONTROL_PLANE_API_KEY", None)
    version_result = run_short_command(
        [str(executable), "--version"],
        env=probe_env,
        timeout_seconds=8,
    )
    version_text = version_result.output.strip()
    if version_result.timed_out or version_result.exit_code != 0 or version_result.truncated:
        return TunnelAdminCapability(False, None, version_text, (), "version-probe-failed")
    match = _TUNNEL_VERSION_RE.search(version_text)
    if match is None:
        return TunnelAdminCapability(False, None, version_text, (), "version-unrecognized")
    version = tuple(int(value) for value in match.groups())
    if version < (0, 0, 14):
        return TunnelAdminCapability(False, version, version_text, (), "version-too-old")

    help_result = run_short_command(
        [str(executable), "run", "--help"],
        env=probe_env,
        timeout_seconds=8,
    )
    if help_result.timed_out or help_result.exit_code != 0 or help_result.truncated:
        return TunnelAdminCapability(False, version, version_text, (), "run-help-probe-failed")
    missing = tuple(flag for flag in _STRUCTURED_ADMIN_FLAGS if flag not in help_result.output)
    if missing:
        return TunnelAdminCapability(
            False,
            version,
            version_text,
            (),
            "missing-capability:" + ",".join(missing),
        )
    return TunnelAdminCapability(
        True,
        version,
        version_text,
        tuple(flag.removeprefix("--") for flag in _STRUCTURED_ADMIN_FLAGS),
        "supported",
    )


def _allocate_tunnel_health_url_file(config_root: Path | None = None) -> Path:
    root = user_config_root() if config_root is None else Path(config_root).expanduser().resolve(strict=False)
    health_root = root / "tunnel-health"
    if health_root.is_symlink() or _is_reparse_point(health_root):
        raise ValueError("tunnel health directory must not be a symlink or reparse point")
    health_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(health_root, 0o700)
    except OSError:
        pass
    if not health_root.is_dir() or health_root.is_symlink() or _is_reparse_point(health_root):
        raise ValueError("tunnel health directory is not a private regular directory")
    for _ in range(8):
        candidate = health_root / f"generation-{secrets.token_hex(16)}.url"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise OSError("unable to allocate a unique tunnel health URL path")


def prepare_tunnel_run(
    executable: Path,
    profile: str,
    *,
    env: dict[str, str],
    config_root: Path | None = None,
) -> TunnelRunPlan:
    capability = probe_tunnel_admin_capability(executable, env=env)
    if not capability.supported:
        return TunnelRunPlan(build_run_argv(executable, profile), None, capability)
    try:
        health_url_file = _allocate_tunnel_health_url_file(config_root)
    except (OSError, ValueError) as exc:
        unavailable = TunnelAdminCapability(
            False,
            capability.version,
            capability.version_text,
            (),
            f"health-url-path-unavailable:{type(exc).__name__}",
        )
        return TunnelRunPlan(build_run_argv(executable, profile), None, unavailable)
    return TunnelRunPlan(
        build_structured_admin_run_argv(executable, profile, health_url_file),
        health_url_file,
        capability,
    )


TUNNEL_RECOVERY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
TUNNEL_RECOVERY_STABLE_SECONDS = 60.0


class TunnelSupervisor:
    """Own the tunnel-client lifecycle without coupling transient Tunnel warnings to it.

    The official tunnel-client owns the process-affine stdio MCP child.  This
    supervisor therefore reacts only to a *real tunnel-client process exit*.
    Poll/TLS/HTTP warning text is deliberately outside this state machine.
    """

    def __init__(
        self,
        output_callback: Callable[[str], None],
        *,
        flight_recorder: FlightRecorder | None = None,
        quiescence_probe: Callable[[object], ProcessGenerationQuiescence] | None = None,
    ) -> None:
        self._output_callback = output_callback
        self._flight_recorder = flight_recorder
        # Production stays fail-closed/manual-reconnect until both old-generation
        # quiescence and new-generation readiness can be verified end-to-end.
        # The unverified recovery state machine is deliberately private and is
        # enabled only by tests that exercise future ownership/readiness seams.
        self._automatic_recovery = False
        self._quiescence_probe = quiescence_probe or prove_owned_process_generation_quiescent
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._desired_running = False
        self._launch_argv: tuple[str, ...] | None = None
        self._launch_env: dict[str, str] | None = None
        self._recovery_attempts = 0
        self._next_recovery_at: float | None = None
        self._last_observed_exit_pid: int | None = None
        self._process_started_at: float | None = None
        self._generation_counter = 0
        self._generation_id: int | None = None
        self._admin_health_snapshot: TunnelAdminSnapshot | None = None
        self._admin_monitor: TunnelAdminHealthMonitor | None = None
        self._end_to_end_health_state = "unknown"

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._process

    def running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    def desired_running(self) -> bool:
        with self._lock:
            return self._desired_running

    def has_cached_launch_spec(self) -> bool:
        with self._lock:
            return self._launch_argv is not None and self._launch_env is not None

    def current_generation_id(self) -> int | None:
        with self._lock:
            return self._generation_id

    def exit_code(self) -> int | None:
        process = self.process
        return process.poll() if process is not None else None

    def reset_health_evidence(self) -> None:
        with self._lock:
            self._admin_health_snapshot = None
            self._end_to_end_health_state = "unknown"

    def update_admin_health(self, snapshot: TunnelAdminSnapshot) -> None:
        if not isinstance(snapshot, TunnelAdminSnapshot):
            raise TypeError("snapshot must be a TunnelAdminSnapshot")
        with self._lock:
            self._admin_health_snapshot = snapshot

    def update_admin_health_for_generation(
        self,
        generation_id: int,
        snapshot: TunnelAdminSnapshot,
    ) -> bool:
        if not isinstance(snapshot, TunnelAdminSnapshot):
            raise TypeError("snapshot must be a TunnelAdminSnapshot")
        with self._lock:
            process = self._process
            if (
                generation_id != self._generation_id
                or not self._desired_running
                or process is None
                or process.poll() is not None
            ):
                return False
            self._admin_health_snapshot = snapshot
            return True

    def start_admin_monitor(self, health_url_file: Path) -> bool:
        self.stop_admin_monitor()
        with self._lock:
            generation_id = self._generation_id
            process = self._process
            if (
                generation_id is None
                or not self._desired_running
                or process is None
                or process.poll() is not None
            ):
                return False
        monitor = TunnelAdminHealthMonitor(
            health_url_file,
            publish_snapshot=lambda snapshot: self.update_admin_health_for_generation(
                generation_id,
                snapshot,
            ),
        )
        with self._lock:
            process = self._process
            if (
                generation_id != self._generation_id
                or not self._desired_running
                or process is None
                or process.poll() is not None
            ):
                return False
            self._admin_monitor = monitor
        monitor.start()
        self._record(
            "tunnel.admin_monitor_started",
            generation_id=generation_id,
            health_url_file=str(health_url_file),
        )
        return True

    def stop_admin_monitor(self) -> None:
        with self._lock:
            monitor = self._admin_monitor
            self._admin_monitor = None
        if monitor is not None:
            monitor.stop()

    def update_end_to_end_health(self, state: str) -> None:
        if state not in {"healthy", "degraded", "unknown"}:
            raise ValueError("end-to-end health state must be healthy, degraded, or unknown")
        with self._lock:
            self._end_to_end_health_state = state

    def health_snapshot(self) -> TunnelHealthSnapshot:
        with self._lock:
            process = self._process
            admin = self._admin_health_snapshot
            end_to_end = self._end_to_end_health_state
        process_alive = process is not None and process.poll() is None
        return evaluate_tunnel_health(
            process_alive=process_alive,
            admin=admin,
            end_to_end_state=end_to_end if process_alive else "unknown",
        )

    def start(self, argv: list[str], *, env: dict[str, str]) -> int:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise LauncherError("连接已经在运行")
            self._desired_running = True
            self._launch_argv = tuple(argv)
            self._launch_env = dict(env)
            self._recovery_attempts = 0
            self._next_recovery_at = None
            self._last_observed_exit_pid = None
            self._admin_health_snapshot = None
            self._end_to_end_health_state = "unknown"
            try:
                return self._spawn_locked(recovery=False)
            except OSError as exc:
                self._forget_launch_locked()
                raise LauncherError(f"无法启动 tunnel-client: {exc}") from exc

    def stop(self) -> int | None:
        # Cancel desired-running first so a concurrent GUI reconcile can never
        # restart the process after an explicit operator stop/shutdown.
        with self._lock:
            self._desired_running = False
            self._next_recovery_at = None
            self._recovery_attempts = 0
            self._last_observed_exit_pid = None
            monitor = self._admin_monitor
            self._admin_monitor = None
            self._forget_launch_locked()
            process = self._process
        if monitor is not None:
            monitor.stop()
        if process is None:
            return None
        if process.poll() is None:
            # Kill the FolderBridge-owned Tunnel tree while the parent PID is
            # still alive. On Windows this lets taskkill /T reliably include
            # the MCP subprocesses spawned by tunnel-client instead of leaving
            # an orphan after terminating only the parent first.
            terminate_owned_process_tree(process, hide_window=True)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        self._record("tunnel.process_stop", pid=process.pid, exit_code=process.returncode)
        return process.returncode

    def reconcile(self, *, now: float | None = None) -> dict[str, object] | None:
        """Advance bounded recovery after a real tunnel-client process exit.

        This is intentionally driven by process state, never by Tunnel log text.
        Production defaults to manual reconnect because safe automatic recovery
        requires two proofs: the previous generation is quiescent *and* the new
        generation is ready. The injectable recovery path is retained for tests
        and future verified ownership/readiness implementations. FolderBridge
        itself does not replay a request, but an in-flight request from the
        previous generation can still have an ambiguous remote completion or
        redelivery outcome.
        """

        clock = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self._desired_running:
                return None
            process = self._process
            if process is None:
                return None

            exit_code = process.poll()
            if exit_code is None:
                if (
                    self._recovery_attempts > 0
                    and self._process_started_at is not None
                    and clock - self._process_started_at >= TUNNEL_RECOVERY_STABLE_SECONDS
                ):
                    self._recovery_attempts = 0
                    self._next_recovery_at = None
                    self._last_observed_exit_pid = None
                    event = {"state": "stable", "pid": process.pid, "generation_id": self._generation_id}
                    self._record("tunnel.recovery_stable", pid=process.pid, generation_id=self._generation_id)
                    return event
                return None

            previous_pid = process.pid
            previous_generation_id = self._generation_id
            if self._last_observed_exit_pid != previous_pid:
                self._record(
                    "tunnel.parent_exit_observed",
                    severity="warning",
                    pid=previous_pid,
                    generation_id=previous_generation_id,
                    exit_code=exit_code,
                    ambiguous_inflight=True,
                )
                if not self._automatic_recovery:
                    event = {
                        "state": "blocked",
                        "previous_pid": previous_pid,
                        "previous_generation_id": previous_generation_id,
                        "exit_code": exit_code,
                        "block_reason": "automatic-recovery-disabled",
                        "quiescence_proof": "not-attempted",
                        "ambiguous_inflight": True,
                    }
                    self._record(
                        "tunnel.recovery_blocked",
                        severity="error",
                        pid=previous_pid,
                        generation_id=previous_generation_id,
                        exit_code=exit_code,
                        block_reason="automatic-recovery-disabled",
                        ambiguous_inflight=True,
                    )
                    self._forget_launch_locked()
                    return event
                self._record(
                    "tunnel.previous_generation_fence_begin",
                    pid=previous_pid,
                    generation_id=previous_generation_id,
                )
                try:
                    quiescence = self._quiescence_probe(process)
                except Exception as exc:
                    quiescence = ProcessGenerationQuiescence(False, f"probe-error-{type(exc).__name__}")
                if not quiescence.quiescent:
                    event = {
                        "state": "blocked",
                        "previous_pid": previous_pid,
                        "previous_generation_id": previous_generation_id,
                        "exit_code": exit_code,
                        "quiescence_proof": quiescence.proof,
                        "ambiguous_inflight": True,
                    }
                    self._record(
                        "tunnel.previous_generation_fence_failed",
                        severity="error",
                        pid=previous_pid,
                        generation_id=previous_generation_id,
                        exit_code=exit_code,
                        quiescence_proof=quiescence.proof,
                        ambiguous_inflight=True,
                    )
                    self._record(
                        "tunnel.recovery_blocked",
                        severity="error",
                        pid=previous_pid,
                        generation_id=previous_generation_id,
                        quiescence_proof=quiescence.proof,
                    )
                    self._forget_launch_locked()
                    return event
                self._record(
                    "tunnel.previous_generation_quiescent",
                    pid=previous_pid,
                    generation_id=previous_generation_id,
                    quiescence_proof=quiescence.proof,
                )
                if self._recovery_attempts >= len(TUNNEL_RECOVERY_DELAYS):
                    event = self._exhaust_locked(previous_pid=previous_pid, exit_code=exit_code)
                    return event
                attempt = self._recovery_attempts + 1
                delay = TUNNEL_RECOVERY_DELAYS[self._recovery_attempts]
                self._last_observed_exit_pid = previous_pid
                self._next_recovery_at = clock + delay
                event = {
                    "state": "scheduled",
                    "previous_pid": previous_pid,
                    "previous_generation_id": previous_generation_id,
                    "exit_code": exit_code,
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "quiescence_proof": quiescence.proof,
                    "ambiguous_inflight": True,
                }
                self._record(
                    "tunnel.recovery_scheduled",
                    severity="warning",
                    pid=previous_pid,
                    generation_id=previous_generation_id,
                    exit_code=exit_code,
                    attempt=attempt,
                    delay_seconds=delay,
                    quiescence_proof=quiescence.proof,
                    ambiguous_inflight=True,
                )
                return event

            if self._next_recovery_at is None or clock < self._next_recovery_at:
                return None

            argv = self._launch_argv
            env = self._launch_env
            if argv is None or env is None:
                return self._exhaust_locked(previous_pid=previous_pid, exit_code=exit_code)

            attempt = self._recovery_attempts + 1
            try:
                new_pid = self._spawn_locked(recovery=True)
                # reconcile() owns the recovery clock.  Keep deterministic
                # recovery/stability semantics even when callers inject a
                # monotonic timestamp for tests or scheduling.
                self._process_started_at = clock
            except OSError as exc:
                self._recovery_attempts = attempt
                if attempt >= len(TUNNEL_RECOVERY_DELAYS):
                    event = self._exhaust_locked(
                        previous_pid=previous_pid,
                        exit_code=exit_code,
                        exception_type=type(exc).__name__,
                    )
                    return event
                delay = TUNNEL_RECOVERY_DELAYS[attempt]
                self._next_recovery_at = clock + delay
                event = {
                    "state": "restart_failed",
                    "previous_pid": previous_pid,
                    "exit_code": exit_code,
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "exception_type": type(exc).__name__,
                }
                self._record(
                    "tunnel.recovery_attempt_failed",
                    severity="warning",
                    pid=previous_pid,
                    exit_code=exit_code,
                    attempt=attempt,
                    delay_seconds=delay,
                    exception_type=type(exc).__name__,
                )
                return event

            self._recovery_attempts = attempt
            event = {
                "state": "restarted",
                "pid": new_pid,
                "generation_id": self._generation_id,
                "previous_pid": previous_pid,
                "previous_generation_id": previous_generation_id,
                "exit_code": exit_code,
                "attempt": attempt,
                "ambiguous_inflight": True,
            }
            self._record(
                "tunnel.recovery_succeeded",
                pid=new_pid,
                generation_id=self._generation_id,
                previous_pid=previous_pid,
                previous_generation_id=previous_generation_id,
                exit_code=exit_code,
                attempt=attempt,
                ambiguous_inflight=True,
            )
            return event

    def _spawn_locked(self, *, recovery: bool) -> int:
        if self._launch_argv is None or self._launch_env is None:
            raise OSError("Tunnel launch specification is unavailable")
        process = subprocess.Popen(
            list(self._launch_argv),
            env=dict(self._launch_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
        self._process = process
        # Health authority is generation-bound.  Never let a previous
        # process's admin/E2E evidence make a replacement generation green.
        self._admin_health_snapshot = None
        self._end_to_end_health_state = "unknown"
        self._generation_counter += 1
        self._generation_id = self._generation_counter
        generation_id = self._generation_id
        self._process_started_at = time.monotonic()
        self._next_recovery_at = None
        self._last_observed_exit_pid = None
        self._reader = threading.Thread(target=self._read_output, args=(process, generation_id), daemon=True)
        self._reader.start()
        self._record("tunnel.process_start", pid=process.pid, generation_id=generation_id, recovery=recovery)
        self._record("tunnel.generation_started", pid=process.pid, generation_id=generation_id, recovery=recovery)
        return process.pid

    def _forget_launch_locked(self) -> None:
        if self._launch_env is not None:
            self._launch_env.clear()
        self._launch_env = None
        self._launch_argv = None
        self._desired_running = False
        self._next_recovery_at = None
        self._last_observed_exit_pid = None
        self._process_started_at = None
        self._admin_health_snapshot = None
        self._end_to_end_health_state = "unknown"

    def _exhaust_locked(
        self,
        *,
        previous_pid: int,
        exit_code: int | None,
        exception_type: str | None = None,
    ) -> dict[str, object]:
        attempts = self._recovery_attempts
        self._next_recovery_at = None
        self._last_observed_exit_pid = previous_pid
        event: dict[str, object] = {
            "state": "exhausted",
            "previous_pid": previous_pid,
            "exit_code": exit_code,
            "attempt": attempts,
        }
        if exception_type is not None:
            event["exception_type"] = exception_type
        self._record(
            "tunnel.recovery_exhausted",
            severity="error",
            pid=previous_pid,
            exit_code=exit_code,
            attempt=attempts,
            exception_type=exception_type,
        )
        self._forget_launch_locked()
        return event

    def _record(self, event: str, *, severity: str = "info", text: str | None = None, **fields: object) -> None:
        if self._flight_recorder is None:
            return
        try:
            self._flight_recorder.record(event, severity=severity, text=text, **fields)
        except Exception:
            pass

    def _read_output(self, process: subprocess.Popen[bytes], generation_id: int | None = None) -> None:
        if process.stdout is None:
            return
        pending = bytearray()
        while True:
            try:
                chunk = process.stdout.read(4096)
            except (OSError, ValueError) as exc:
                self._record("tunnel.output_read_error", severity="error", text=str(exc), exception_type=type(exc).__name__, pid=process.pid, generation_id=generation_id)
                return
            if not chunk:
                if pending:
                    self._output_callback(bytes(pending).decode("utf-8", errors="replace"))
                self._record("tunnel.output_eof", pid=process.pid, generation_id=generation_id, exit_code=process.poll())
                return

            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(pending[: newline + 1])
                del pending[: newline + 1]
                self._output_callback(line.decode("utf-8", errors="replace"))


def _posix_join(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
