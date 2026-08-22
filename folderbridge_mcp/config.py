from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_NAME = ".folderbridge.json"
CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
MAX_WORKSPACES = 8
TASK_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
INLINE_RUNNERS = {
    "bash": {"-c"},
    "cmd": {"/c", "/k"},
    "cmd.exe": {"/c", "/k"},
    "node": {"-e", "--eval", "-p", "--print"},
    "perl": {"-e"},
    "powershell": {"-command", "-c", "-encodedcommand", "-enc"},
    "powershell.exe": {"-command", "-c", "-encodedcommand", "-enc"},
    "pwsh": {"-command", "-c", "-encodedcommand", "-enc"},
    "python": {"-c"},
    "python.exe": {"-c"},
    "python3": {"-c"},
    "ruby": {"-e"},
    "sh": {"-c"},
    "wscript": set(),
    "cscript": set(),
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    tasks: dict[str, Task]
    sha256: str


def canonical_workspace(raw: str | os.PathLike[str]) -> Path:
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ConfigError(f"Workspace is not a directory: {root}")
    anchor = Path(root.anchor).resolve(strict=True)
    try:
        home = Path.home().resolve(strict=True)
    except (OSError, RuntimeError):
        home = None
    if root == anchor or (home is not None and root == home):
        raise ConfigError("Workspace is too broad; choose a project directory, not a drive root or home directory")
    return root


def canonical_workspaces(raw_values: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...]) -> tuple[Path, ...]:
    if not raw_values:
        raise ConfigError("Choose at least one workspace directory")
    if len(raw_values) > MAX_WORKSPACES:
        raise ConfigError(f"At most {MAX_WORKSPACES} workspace directories are allowed")
    roots = tuple(canonical_workspace(raw) for raw in raw_values)
    for index, root in enumerate(roots):
        for other in roots[:index]:
            if root == other:
                raise ConfigError(f"Duplicate workspace directory: {root}")
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ConfigError(f"Workspace directories cannot contain one another: {other} and {root}")
    return roots


def workspace_id(workspace: Path) -> str:
    return hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]


def config_path(workspace: Path) -> Path:
    return workspace / CONFIG_NAME


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_task(name: str, raw: Any) -> Task:
    if not TASK_NAME_RE.fullmatch(name):
        raise ConfigError(f"Invalid task name: {name!r}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Task {name!r} must be an object")
    unknown = sorted(set(raw).difference({"argv", "timeout_seconds"}))
    if unknown:
        raise ConfigError(f"Task {name!r} has unknown fields: {', '.join(unknown)}")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ConfigError(f"Task {name!r}.argv must be a non-empty string array")
    if len(argv) > 32 or any(len(item) > 1000 or "\x00" in item for item in argv):
        raise ConfigError(f"Task {name!r}.argv is too large")
    executable = Path(argv[0]).name.lower()
    blocked_flags = INLINE_RUNNERS.get(executable)
    if blocked_flags is not None:
        lowered = {item.lower() for item in argv[1:]}
        if not blocked_flags or lowered.intersection(blocked_flags):
            raise ConfigError(
                f"Task {name!r} uses an inline shell/interpreter entry point; use a checked-in script instead"
            )
    timeout = raw.get("timeout_seconds", 120)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ConfigError(f"Task {name!r}.timeout_seconds must be between 1 and 600")
    return Task(name=name, argv=tuple(argv), timeout_seconds=timeout)


def load_config(workspace: Path, *, required: bool = False) -> ProjectConfig:
    path = config_path(workspace)
    if not path.exists():
        if required:
            raise ConfigError(f"Missing {CONFIG_NAME}; run init first")
        return ProjectConfig(path=path, tasks={}, sha256="")
    if path.is_symlink() or _is_reparse_point(path):
        raise ConfigError(f"Refusing a linked config file: {path}")
    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw_bytes) > MAX_CONFIG_BYTES:
            raise ConfigError(f"{CONFIG_NAME} exceeds {MAX_CONFIG_BYTES} bytes")
        raw = json.loads(raw_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read {CONFIG_NAME}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION or isinstance(raw.get("version"), bool):
        raise ConfigError(f"{CONFIG_NAME} must use version {CONFIG_VERSION}")
    unknown = sorted(set(raw).difference({"version", "tasks"}))
    if unknown:
        raise ConfigError(f"{CONFIG_NAME} has unknown fields: {', '.join(unknown)}")
    raw_tasks = raw.get("tasks", {})
    if not isinstance(raw_tasks, dict) or len(raw_tasks) > 20:
        raise ConfigError("tasks must be an object with at most 20 entries")
    tasks = {name: _validate_task(name, task) for name, task in raw_tasks.items() if isinstance(name, str)}
    if len(tasks) != len(raw_tasks):
        raise ConfigError("Every task name must be a string")
    return ProjectConfig(path=path, tasks=tasks, sha256=_hash_bytes(raw_bytes))


def default_config(workspace: Path) -> dict[str, Any]:
    tasks: dict[str, Any] = {}
    if any((workspace / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")):
        tasks["test"] = {"argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120}
    package_json = workspace / "package.json"
    if package_json.is_file() and not package_json.is_symlink():
        try:
            with package_json.open("rb") as handle:
                package_bytes = handle.read(MAX_CONFIG_BYTES + 1)
            package = json.loads(package_bytes) if len(package_bytes) <= MAX_CONFIG_BYTES else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if isinstance(scripts, dict):
            for script_name in ("test", "lint", "typecheck"):
                if isinstance(scripts.get(script_name), str):
                    tasks[f"npm-{script_name}"] = {
                        "argv": ["npm", "run", script_name],
                        "timeout_seconds": 180,
                    }
    return {"version": CONFIG_VERSION, "tasks": tasks}


def write_default_config(workspace: Path, *, force: bool = False) -> ProjectConfig:
    path = config_path(workspace)
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists; use --force to replace it")
    payload = _canonical_json(default_config(workspace))
    _atomic_write(path, payload)
    return load_config(workspace, required=True)


def trust_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"])
    else:
        base = Path.home() / ".local" / "state"
    return base / "folderbridge-mcp" / "trust"


def trust_path(workspace: Path) -> Path:
    key = hashlib.sha256(os.fsencode(str(workspace))).hexdigest()
    return trust_dir() / f"{key}.json"


def approve_config(workspace: Path, config: ProjectConfig) -> Path:
    if not config.sha256:
        raise ConfigError(f"Cannot approve a missing {CONFIG_NAME}")
    record = {
        "version": 1,
        "workspace": str(workspace),
        "config_sha256": config.sha256,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    path = trust_path(workspace)
    _atomic_write(path, _canonical_json(record), mode=0o600)
    return path


def config_is_trusted(workspace: Path, config: ProjectConfig) -> bool:
    if not config.sha256:
        return False
    path = trust_path(workspace)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            return False
        record = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(record, dict)
        and record.get("version") == 1
        and not isinstance(record.get("version"), bool)
        and record.get("workspace") == str(workspace)
        and record.get("config_sha256") == config.sha256
    )


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
