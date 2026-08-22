from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from . import __version__
from .config import ConfigError, canonical_workspace, config_is_trusted, load_config


LAUNCHER_SETTINGS_VERSION = 1
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
    workspace: str = ""
    access_mode: str = "read_only"
    profile: str = "folderbridge"
    tunnel_id: str = ""
    tunnel_client_path: str = ""
    allow_tasks: bool = False
    configured_fingerprint: str = ""

    def validate(self, *, require_tunnel_id: bool = False) -> Path:
        if self.version != LAUNCHER_SETTINGS_VERSION or isinstance(self.version, bool):
            raise LauncherError("启动器配置版本无效")
        try:
            workspace = canonical_workspace(self.workspace)
        except (ConfigError, OSError) as exc:
            raise LauncherError(str(exc)) from exc
        if any(ord(character) < 32 for character in str(workspace)):
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
        if self.allow_tasks:
            config = load_config(workspace, required=True)
            if not config_is_trusted(workspace, config):
                raise LauncherError("测试任务尚未在本机审核批准；请先用命令行 init/approve")
        return workspace

    def fingerprint(self) -> str:
        workspace = canonical_workspace(self.workspace)
        payload = {
            "version": __version__,
            "workspace": str(workspace),
            "access_mode": self.access_mode,
            "profile": self.profile,
            "tunnel_id": self.tunnel_id,
            "allow_tasks": self.allow_tasks,
            "mcp_command": mcp_command(workspace, self.access_mode, self.allow_tasks),
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
        if not all(
            isinstance(value, str)
            for value in (
                settings.workspace,
                settings.access_mode,
                settings.profile,
                settings.tunnel_id,
                settings.tunnel_client_path,
                settings.configured_fingerprint,
            )
        ) or not isinstance(settings.allow_tasks, bool):
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
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / "folderbridge-mcp" / "launcher.json"


def checkout_launcher_path() -> Path:
    return Path(__file__).resolve().parents[1] / "folderbridge_launcher.py"


def console_python() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.name.lower() in {"pythonw.exe", "pythonw"}:
        candidate = executable.with_name("python.exe" if executable.suffix.lower() == ".exe" else "python")
        if candidate.is_file():
            return candidate
    return executable


def mcp_argv(workspace: Path, access_mode: str, allow_tasks: bool) -> list[str]:
    if getattr(sys, "frozen", False):
        argv = [str(Path(sys.executable).resolve()), "serve", "--workspace", str(workspace)]
    else:
        argv = [str(console_python()), str(checkout_launcher_path()), "serve", "--workspace", str(workspace)]
    if access_mode == "read_only":
        argv.append("--read-only")
    if allow_tasks:
        argv.append("--allow-tasks")
    return argv


def mcp_command(workspace: Path, access_mode: str, allow_tasks: bool) -> str:
    argv = mcp_argv(workspace, access_mode, allow_tasks)
    # tunnel-client parses --mcp-command with POSIX-style escaping even on
    # Windows. Backslashes would therefore be consumed (C:\Users -> C:Users).
    # Windows accepts forward slashes for these absolute executable and
    # workspace paths, while list2cmdline still quotes arguments with spaces.
    if os.name == "nt":
        argv = [argument.replace("\\", "/") for argument in argv]
    return subprocess.list2cmdline(argv) if os.name == "nt" else _posix_join(argv)


def render_client_config(
    workspace: Path,
    access_mode: str,
    allow_tasks: bool,
    output_format: str,
) -> str:
    """Render a portable stdio client configuration without starting the server."""

    argv = mcp_argv(workspace, access_mode, allow_tasks)
    command, args = argv[0], argv[1:]
    if output_format == "tunnel":
        return mcp_command(workspace, access_mode, allow_tasks)
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


def build_init_argv(executable: Path, settings: LauncherSettings, workspace: Path) -> list[str]:
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
        mcp_command(workspace, settings.access_mode, settings.allow_tasks),
    ]


def build_doctor_argv(executable: Path, profile: str) -> list[str]:
    return [str(executable), "doctor", "--profile", profile, "--explain"]


def build_run_argv(executable: Path, profile: str) -> list[str]:
    return [str(executable), "run", "--profile", profile]


def control_plane_environment(api_key: str) -> dict[str, str]:
    env = dict(os.environ)
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
    for secret in secrets:
        if secret:
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
    creation_flags = _creation_flags()
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            creationflags=creation_flags,
            start_new_session=sys.platform != "win32",
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
        _terminate_process_tree(process)
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
                    creationflags=_creation_flags(),
                    start_new_session=sys.platform != "win32",
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
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                _terminate_process_tree(process)
                try:
                    process.wait(timeout=5)
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


def _creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    process.kill()


def _posix_join(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
