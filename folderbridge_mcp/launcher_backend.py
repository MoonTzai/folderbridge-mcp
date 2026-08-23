from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from . import __version__
from .capabilities import CAPABILITY_NAMES, normalize_capability_names
from .config import ConfigError, MAX_WORKSPACES, canonical_workspaces, config_is_trusted, load_config
from .process_control import owned_process_group_kwargs, terminate_owned_process_tree
from .user_paths import user_config_root


LAUNCHER_SETTINGS_VERSION = 3
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
                    settings.configured_fingerprint,
                )
            )
            or not isinstance(settings.allow_tasks, bool)
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


class TunnelSupervisor:
    def __init__(self, output_callback: Callable[[str], None]) -> None:
        self._output_callback = output_callback
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._process

    def running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    def exit_code(self) -> int | None:
        process = self.process
        return process.poll() if process is not None else None

    def start(self, argv: list[str], *, env: dict[str, str]) -> int:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise LauncherError("连接已经在运行")
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
                raise LauncherError(f"无法启动 tunnel-client: {exc}") from exc
            self._process = process
            self._reader = threading.Thread(target=self._read_output, args=(process,), daemon=True)
            self._reader.start()
            return process.pid

    def stop(self) -> int | None:
        process = self.process
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
        return process.returncode

    def _read_output(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        while True:
            try:
                chunk = process.stdout.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self._output_callback(chunk.decode("utf-8", errors="replace"))


def _posix_join(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
