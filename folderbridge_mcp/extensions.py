from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .config import workspace_id
from .process_control import owned_process_group_kwargs, terminate_owned_process_tree
from .security import ToolError, Workspace, clean_environment
from .user_paths import INTERNAL_CONFIG_ROOT_ENV, user_config_root


EXTENSION_SCHEMA_VERSION = 1
TRUST_STORE_VERSION = 1
MANIFEST_NAME = "folderbridge-extension.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_EXTENSION_FILES = 256
MAX_EXTENSION_BYTES = 64 * 1024 * 1024
MAX_WORKER_REQUEST_BYTES = 8 * 1024 * 1024
MAX_WORKER_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_WORKER_LOG_BYTES = 256 * 1024
MAX_ACTIONS = 64
MAX_ADAPTER_PATTERNS = 64
MAX_FOREGROUND_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_JOB_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_RUNNING_EXTENSION_JOBS = 16
MAX_RETAINED_FINISHED_JOBS = 128
MAX_INHERITED_ENV_VALUE_BYTES = 64 * 1024
MAX_WORKSPACE_ARTIFACTS = 64
EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXECUTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}(?:\.exe|\.cmd|\.bat|\.com)?$", re.IGNORECASE)
LOOPBACK_PERMISSION_RE = re.compile(r"^network\.loopback:(?:127\.0\.0\.1|localhost):([1-9][0-9]{0,4})$")
PROCESS_PERMISSION_RE = re.compile(r"^process\.execute:([A-Za-z0-9][A-Za-z0-9._+-]{0,127}(?:\.exe|\.cmd|\.bat|\.com)?)$", re.IGNORECASE)
ENVIRONMENT_PERMISSION_RE = re.compile(r"^environment\.inherit:([A-Z][A-Z0-9_]{0,127})$")
RESERVED_EXTENSION_ENV_NAMES = {
    "CONTROL_PLANE_API_KEY",
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYINSTALLER_RESET_ENVIRONMENT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
}
EXACT_PERMISSIONS = {
    "workspace.read",
    "workspace.write",
    "workspace.adapter",
    "extension.state",
    "git.commit-selected-files",
    "git.push-current-branch",
    "github.web-auth",
    "network.outbound:https",
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


@dataclass(frozen=True)
class ExtensionAction:
    name: str
    read_only: bool
    requires_workspace: bool
    authorization: str
    input_schema: dict[str, Any]
    run_mode: str
    timeout_seconds: int | None


@dataclass(frozen=True)
class ExtensionManifest:
    extension_id: str
    name: str
    version: str
    description: str
    entrypoint: str
    permissions: tuple[str, ...]
    actions: dict[str, ExtensionAction]
    execution_timeout_seconds: int
    workspace_adapter: dict[str, Any]


@dataclass(frozen=True)
class ExtensionRecord:
    path: Path
    manifest: ExtensionManifest
    sha256: str
    bundled: bool


class ExtensionTrustStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or extension_trust_path()
        # Keep each read-modify-write transaction atomic inside this process.
        # RLock allows set_enabled() to reuse status() without leaking locking
        # responsibilities to GUI or MCP callers.
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if self.path.is_symlink() or _is_reparse_point(self.path):
                return {}
            with self.path.open("rb") as handle:
                data = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(data) > MAX_MANIFEST_BYTES:
                return {}
            parsed = json.loads(data, parse_constant=_reject_json_constant)
        except (OSError, UnicodeDecodeError, ValueError):
            return {}
        version = parsed.get("version") if isinstance(parsed, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version != TRUST_STORE_VERSION:
            return {}
        extensions = parsed.get("extensions")
        if not isinstance(extensions, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        for extension_id, value in extensions.items():
            if not isinstance(extension_id, str) or not isinstance(value, dict):
                continue
            sha256 = value.get("sha256")
            permissions = value.get("permissions")
            enabled = value.get("enabled")
            if (
                EXTENSION_ID_RE.fullmatch(extension_id)
                and isinstance(sha256, str)
                and len(sha256) == 64
                and isinstance(permissions, list)
                and all(isinstance(item, str) for item in permissions)
                and isinstance(enabled, bool)
            ):
                cleaned[extension_id] = {
                    "sha256": sha256,
                    "permissions": permissions,
                    "enabled": enabled,
                }
        return cleaned

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps(
            {"version": TRUST_STORE_VERSION, "extensions": records},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
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

    def status(self, record: ExtensionRecord) -> dict[str, bool]:
        with self._lock:
            saved = self._load().get(record.manifest.extension_id)
            trusted = bool(
                saved
                and saved.get("sha256") == record.sha256
                and saved.get("permissions") == list(record.manifest.permissions)
            )
            return {
                "trusted": trusted,
                "enabled": bool(trusted and saved and saved.get("enabled")),
                "approval_stale": bool(saved and not trusted),
            }

    def approve(self, record: ExtensionRecord, *, enabled: bool = True) -> None:
        with self._lock:
            records = self._load()
            records[record.manifest.extension_id] = {
                "sha256": record.sha256,
                "permissions": list(record.manifest.permissions),
                "enabled": bool(enabled),
            }
            self._save(records)

    def set_enabled(self, record: ExtensionRecord, enabled: bool) -> None:
        with self._lock:
            status = self.status(record)
            if enabled and not status["trusted"]:
                raise ValueError("Extension must be approved before it can be enabled")
            records = self._load()
            saved = records.get(record.manifest.extension_id)
            if saved is None:
                if enabled:
                    raise ValueError("Extension must be approved before it can be enabled")
                return
            saved["enabled"] = bool(enabled)
            records[record.manifest.extension_id] = saved
            self._save(records)

    def revoke(self, extension_id: str) -> None:
        with self._lock:
            records = self._load()
            records.pop(extension_id, None)
            self._save(records)


class ExtensionRegistry:
    """Hot-scanned extension registry. No plugin code is imported into the MCP process."""

    def __init__(
        self,
        *,
        user_root: Path | None = None,
        bundled_root: Path | None = None,
        trust_store: ExtensionTrustStore | None = None,
    ) -> None:
        self.user_root = user_root or extension_root_path()
        self.bundled_root = bundled_root or bundled_extension_root()
        self.trust_store = trust_store or ExtensionTrustStore()
        self.jobs = ExtensionJobManager()

    def scan(self) -> tuple[dict[str, ExtensionRecord], list[dict[str, str]]]:
        records: dict[str, ExtensionRecord] = {}
        errors: list[dict[str, str]] = []
        for root, bundled in ((self.bundled_root, True), (self.user_root, False)):
            try:
                children = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
            except OSError as exc:
                errors.append({"path": str(root), "error": f"cannot scan extension root: {exc}"})
                continue
            for child in children:
                try:
                    record = load_extension(child, bundled=bundled)
                except (OSError, ValueError) as exc:
                    errors.append({"path": str(child), "error": str(exc)})
                    continue
                extension_id = record.manifest.extension_id
                if extension_id in records:
                    errors.append({"path": str(child), "error": f"duplicate extension id: {extension_id}"})
                    continue
                records[extension_id] = record
        return records, errors

    def get(self, extension_id: str) -> ExtensionRecord:
        records, _errors = self.scan()
        record = records.get(extension_id)
        if record is None:
            raise ToolError("EXTENSION_NOT_FOUND", "Extension is not installed.", extension_id=extension_id)
        return record

    def describe(self, workspace: Path | None = None) -> dict[str, Any]:
        records, errors = self.scan()
        rendered: list[dict[str, Any]] = []
        for extension_id in sorted(records):
            record = records[extension_id]
            trust = self.trust_store.status(record)
            applicable = _workspace_applicable(record.manifest.workspace_adapter, workspace)
            rendered.append(
                {
                    "id": extension_id,
                    "name": record.manifest.name,
                    "version": record.manifest.version,
                    "description": record.manifest.description,
                    "bundled": record.bundled,
                    "sha256": record.sha256,
                    "permissions": list(record.manifest.permissions),
                    "actions": [
                        {
                            "name": action.name,
                            "read_only": action.read_only,
                            "requires_workspace": action.requires_workspace,
                            "authorization": action.authorization,
                            "input_schema": action.input_schema,
                            "run_mode": action.run_mode,
                            "timeout_seconds": _action_timeout(record, action),
                        }
                        for action in record.manifest.actions.values()
                    ],
                    "workspace_adapter": record.manifest.workspace_adapter,
                    "applicable": applicable,
                    **trust,
                    "loaded": bool(trust["trusted"] and trust["enabled"] and applicable),
                }
            )
        return {
            "extension_root": str(self.user_root),
            "extensions": rendered,
            "errors": errors,
        }

    def run(
        self,
        extension_id: str,
        action_name: str,
        params: dict[str, Any],
        *,
        workspace: Workspace | None,
        read_only: bool,
    ) -> dict[str, Any]:
        record = self.get(extension_id)
        action = record.manifest.actions.get(action_name)
        if action is None:
            raise ToolError(
                "EXTENSION_ACTION_NOT_FOUND",
                "Extension action does not exist.",
                extension_id=extension_id,
                available=sorted(record.manifest.actions),
            )
        if action.requires_workspace and workspace is None:
            raise ToolError("WORKSPACE_REQUIRED", "This extension action requires a selected workspace.")
        if not action.read_only and read_only:
            raise ToolError("READ_ONLY", "This extension action is unavailable while FolderBridge is read-only.")
        if workspace is not None and not _workspace_applicable(record.manifest.workspace_adapter, workspace.root):
            raise ToolError(
                "EXTENSION_NOT_APPLICABLE",
                "The extension's dynamic workspace adapter does not match this workspace yet.",
                extension_id=extension_id,
            )
        trust = self.trust_store.status(record)
        # External code is never executed before exact-hash approval. Bundled
        # read-only discovery/status actions may explicitly opt out of global authorization.
        if not record.bundled and not trust["trusted"]:
            raise ToolError(
                "EXTENSION_NOT_TRUSTED",
                "External extension code must be approved locally before any action can run.",
                extension_id=extension_id,
            )
        if action.authorization == "global" and not trust["enabled"]:
            raise ToolError(
                "EXTENSION_NOT_ENABLED",
                "This extension action requires one-time global approval and enablement in the FolderBridge sidebar.",
                extension_id=extension_id,
            )
        validate_json_schema(params, action.input_schema, path="params")
        if action.run_mode == "job":
            return self.jobs.start(record, action, params, workspace=workspace, read_only=read_only)
        return _run_worker(record, action, params, workspace=workspace, read_only=read_only)

    def job_status(self, job_id: str, *, workspace: Workspace | None) -> dict[str, Any]:
        return self.jobs.status(job_id, workspace=workspace)

    def job_cancel(self, job_id: str, *, workspace: Workspace | None) -> dict[str, Any]:
        return self.jobs.cancel(job_id, workspace=workspace)


def extension_root_path() -> Path:
    return _config_base() / "extensions"


def extension_trust_path() -> Path:
    return _config_base() / "extension-trust.json"


def extension_state_root() -> Path:
    return _config_base() / "extension-state"


def bundled_extension_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root) / "extensions"
    return Path(__file__).resolve().parents[1] / "extensions"


def snapshot_extension(source: Path, destination: Path) -> Path:
    """Copy only hash-covered Extension files into a private execution snapshot."""

    if source.is_symlink() or _is_reparse_point(source):
        raise ValueError("extension directory may not be a link or reparse point")
    root = source.resolve(strict=True)
    if destination.exists():
        raise ValueError("extension snapshot destination must not already exist")
    destination.mkdir(parents=True)
    count = 0
    total = 0
    ignored_parts = {"__pycache__", ".git", ".svn"}
    for directory, dirs, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path != root and (directory_path.is_symlink() or _is_reparse_point(directory_path)):
            raise ValueError("extension trees may not contain links or reparse points")
        relative_dir = directory_path.relative_to(root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = directory_path / name
            relative = child.relative_to(root)
            if any(part in ignored_parts for part in relative.parts):
                continue
            if child.is_symlink() or _is_reparse_point(child):
                raise ValueError("extension trees may not contain links or reparse points")
            if not child.is_dir():
                raise ValueError("extension tree contains a non-directory directory entry")
            destination.joinpath(*relative.parts).mkdir(parents=True, exist_ok=True)
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            child = directory_path / name
            relative = child.relative_to(root)
            if any(part in ignored_parts for part in relative.parts) or child.suffix.lower() in {".pyc", ".pyo"}:
                continue
            if child.is_symlink() or _is_reparse_point(child) or not child.is_file():
                raise ValueError("extension trees may contain only regular files")
            count += 1
            if count > MAX_EXTENSION_FILES:
                raise ValueError(f"extension exceeds {MAX_EXTENSION_FILES} files")
            try:
                before = child.stat()
                if before.st_size > MAX_EXTENSION_BYTES - total:
                    raise ValueError(f"extension exceeds {MAX_EXTENSION_BYTES} bytes")
                data = child.read_bytes()
                after = child.stat()
            except OSError as exc:
                raise ValueError(f"could not read extension file while snapshotting: {relative.as_posix()}") from exc
            before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if len(data) != before.st_size or before_identity != after_identity:
                raise ValueError(f"extension file changed while snapshotting: {relative.as_posix()}")
            total += len(data)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return destination


def load_extension(path: Path, *, bundled: bool) -> ExtensionRecord:
    if path.is_symlink() or _is_reparse_point(path):
        raise ValueError("extension directory may not be a link or reparse point")
    root = path.resolve(strict=True)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink() or _is_reparse_point(manifest_path):
        raise ValueError(f"missing regular {MANIFEST_NAME}")
    digest, data = _hash_extension(root)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("extension manifest is too large")
    try:
        raw = json.loads(data, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("extension manifest must be strict UTF-8 JSON") from exc
    manifest = _parse_manifest(raw, root)
    if not bundled and any(action.authorization == "none" for action in manifest.actions.values()):
        raise ValueError("external extensions may not declare authorization=none")
    return ExtensionRecord(root, manifest, digest, bundled)


def _parse_manifest(raw: Any, root: Path) -> ExtensionManifest:
    if not isinstance(raw, dict):
        raise ValueError("extension manifest must be an object")
    allowed = {
        "schema_version",
        "id",
        "name",
        "version",
        "description",
        "entrypoint",
        "permissions",
        "actions",
        "execution",
        "workspace_adapter",
    }
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise ValueError(f"unknown manifest fields: {', '.join(unknown)}")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != EXTENSION_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be integer {EXTENSION_SCHEMA_VERSION}")
    extension_id = raw.get("id")
    name = raw.get("name")
    version = raw.get("version")
    description = raw.get("description", "")
    entrypoint = raw.get("entrypoint", "plugin.py")
    if not isinstance(extension_id, str) or not EXTENSION_ID_RE.fullmatch(extension_id):
        raise ValueError("extension id must match [a-z0-9][a-z0-9._-]{0,63}")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("extension name must be 1..120 characters")
    if not isinstance(version, str) or not version.strip() or len(version) > 64:
        raise ValueError("extension version must be 1..64 characters")
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError("extension description must be <= 1000 characters")
    entrypoint_path = _safe_relative_file(root, entrypoint)
    if entrypoint_path.suffix.lower() != ".py":
        raise ValueError("extension entrypoint must be a .py file")

    raw_permissions = raw.get("permissions", [])
    if not isinstance(raw_permissions, list) or not all(isinstance(item, str) for item in raw_permissions):
        raise ValueError("permissions must be a list of strings")
    if len(raw_permissions) != len(set(raw_permissions)):
        raise ValueError("permissions may not contain duplicates")
    for permission in raw_permissions:
        _validate_permission(permission)
    permissions = tuple(raw_permissions)

    raw_execution = raw.get("execution", {})
    if not isinstance(raw_execution, dict):
        raise ValueError("execution must be an object")
    if set(raw_execution).difference({"mode", "timeout_seconds"}):
        raise ValueError("execution supports only mode and timeout_seconds")
    if raw_execution.get("mode", "isolated-process") != "isolated-process":
        raise ValueError("extension schema v1 supports only execution.mode=isolated-process")
    timeout = raw_execution.get("timeout_seconds", 180)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 <= timeout <= MAX_JOB_TIMEOUT_SECONDS:
        raise ValueError(f"execution.timeout_seconds must be 0..{MAX_JOB_TIMEOUT_SECONDS}; 0 disables automatic timeout termination")

    adapter = _parse_workspace_adapter(raw.get("workspace_adapter", {"mode": "none"}))
    if adapter.get("mode") == "dynamic" and "workspace.adapter" not in permissions:
        raise ValueError("workspace_adapter.mode=dynamic requires the workspace.adapter permission")
    if adapter.get("state") == "profile" and "extension.state" not in permissions:
        raise ValueError("workspace_adapter.state=profile requires the extension.state permission")

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, dict) or not raw_actions or len(raw_actions) > MAX_ACTIONS:
        raise ValueError(f"actions must contain 1..{MAX_ACTIONS} actions")
    actions: dict[str, ExtensionAction] = {}
    for action_name, spec in raw_actions.items():
        if not isinstance(action_name, str) or not ACTION_NAME_RE.fullmatch(action_name):
            raise ValueError(f"invalid action name: {action_name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"action {action_name} must be an object")
        if set(spec).difference({"read_only", "requires_workspace", "authorization", "input_schema", "run_mode", "timeout_seconds"}):
            raise ValueError(f"action {action_name} has unknown fields")
        read_only = spec.get("read_only")
        requires_workspace = spec.get("requires_workspace", True)
        authorization = spec.get("authorization", "global")
        schema = spec.get("input_schema", {"type": "object", "properties": {}, "additionalProperties": False})
        run_mode = spec.get("run_mode", "foreground")
        action_timeout = spec.get("timeout_seconds")
        if not isinstance(read_only, bool) or not isinstance(requires_workspace, bool):
            raise ValueError(f"action {action_name} read_only/requires_workspace must be boolean")
        if authorization not in {"none", "global"}:
            raise ValueError(f"action {action_name} authorization must be none or global")
        if authorization == "none" and not read_only:
            raise ValueError(f"action {action_name} may use authorization=none only when read_only=true")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"action {action_name} input_schema must be an object schema")
        if run_mode not in {"foreground", "job"}:
            raise ValueError(f"action {action_name} run_mode must be foreground or job")
        if action_timeout is not None:
            max_timeout = MAX_JOB_TIMEOUT_SECONDS if run_mode == "job" else MAX_FOREGROUND_TIMEOUT_SECONDS
            if not isinstance(action_timeout, int) or isinstance(action_timeout, bool) or not 0 <= action_timeout <= max_timeout:
                raise ValueError(f"action {action_name} timeout_seconds must be 0..{max_timeout} for run_mode={run_mode}; 0 disables automatic timeout termination")
        actions[action_name] = ExtensionAction(
            action_name,
            read_only,
            requires_workspace,
            authorization,
            schema,
            run_mode,
            action_timeout,
        )
    return ExtensionManifest(
        extension_id=extension_id,
        name=name.strip(),
        version=version.strip(),
        description=description.strip(),
        entrypoint=entrypoint_path.relative_to(root).as_posix(),
        permissions=permissions,
        actions=actions,
        execution_timeout_seconds=timeout,
        workspace_adapter=adapter,
    )


def _parse_workspace_adapter(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("workspace_adapter must be an object")
    if set(raw).difference({"mode", "detect", "state"}):
        raise ValueError("workspace_adapter supports only mode, detect, and state")
    mode = raw.get("mode", "none")
    if mode not in {"none", "dynamic"}:
        raise ValueError("workspace_adapter.mode must be none or dynamic")
    state = raw.get("state", "profile")
    if state not in {"none", "profile"}:
        raise ValueError("workspace_adapter.state must be none or profile")
    detect = raw.get("detect", {})
    if not isinstance(detect, dict) or set(detect).difference({"any_of", "all_of"}):
        raise ValueError("workspace_adapter.detect supports only any_of/all_of")
    parsed_detect: dict[str, list[str]] = {}
    for key in ("any_of", "all_of"):
        values = detect.get(key, [])
        if not isinstance(values, list) or len(values) > MAX_ADAPTER_PATTERNS or not all(isinstance(item, str) for item in values):
            raise ValueError(f"workspace_adapter.detect.{key} must be a list of <= {MAX_ADAPTER_PATTERNS} strings")
        for pattern in values:
            _validate_relative_pattern(pattern)
        parsed_detect[key] = values
    if mode == "none" and any(parsed_detect.values()):
        raise ValueError("workspace_adapter.mode=none may not declare detect patterns")
    return {"mode": mode, "detect": parsed_detect, "state": state}


def _validate_permission(permission: str) -> None:
    if permission in EXACT_PERMISSIONS:
        return
    match = LOOPBACK_PERMISSION_RE.fullmatch(permission)
    if match:
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return
    match = PROCESS_PERMISSION_RE.fullmatch(permission)
    if match and EXECUTABLE_NAME_RE.fullmatch(match.group(1)):
        return
    match = ENVIRONMENT_PERMISSION_RE.fullmatch(permission)
    if match:
        name = match.group(1)
        if name in RESERVED_EXTENSION_ENV_NAMES or name.startswith("FOLDERBRIDGE_") or name.startswith("CONTROL_PLANE_"):
            raise ValueError(f"reserved environment variable may not be inherited by extensions: {name}")
        return
    raise ValueError(f"unknown or overbroad extension permission: {permission}")


def _validate_relative_pattern(pattern: str) -> None:
    if not pattern or "\x00" in pattern or "\\" in pattern:
        raise ValueError("workspace adapter patterns must be clean POSIX relative patterns")
    path = PurePosixPath(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("workspace adapter patterns may not escape the workspace")


def _workspace_applicable(adapter: dict[str, Any], workspace: Path | None) -> bool:
    if adapter.get("mode") == "none":
        return True
    if workspace is None:
        return False
    root = workspace.resolve(strict=True)
    detect = adapter.get("detect", {})
    any_of = detect.get("any_of", [])
    all_of = detect.get("all_of", [])
    any_ok = True if not any_of else any(_glob_has_safe_match(root, pattern) for pattern in any_of)
    all_ok = all(_glob_has_safe_match(root, pattern) for pattern in all_of)
    return any_ok and all_ok


def _glob_has_safe_match(root: Path, pattern: str) -> bool:
    try:
        for candidate in root.glob(pattern):
            if candidate.is_symlink() or _is_reparse_point(candidate):
                continue
            try:
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                continue
            return True
    except (OSError, ValueError):
        return False
    return False


def _json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality shortcut."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


def validate_json_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    allowed_schema_keys = {
        "type", "properties", "required", "additionalProperties", "items", "enum",
        "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
        "description", "default",
    }
    unknown = sorted(set(schema).difference(allowed_schema_keys))
    if unknown:
        raise ToolError("EXTENSION_SCHEMA_UNSUPPORTED", f"Unsupported input schema keys at {path}: {', '.join(unknown)}")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(_json_values_equal(value, item) for item in enum):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be one of the declared enum values")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be an object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if not isinstance(properties, dict) or not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ToolError("EXTENSION_SCHEMA_UNSUPPORTED", f"Invalid object schema at {path}")
        missing = [item for item in required if item not in value]
        if missing:
            raise ToolError("INVALID_ARGUMENT", f"Missing required fields at {path}: {', '.join(missing)}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if additional is False:
                    raise ToolError("INVALID_ARGUMENT", f"Unknown field at {path}: {key}")
                if isinstance(additional, dict):
                    child_schema = additional
                else:
                    continue
            if not isinstance(child_schema, dict):
                raise ToolError("EXTENSION_SCHEMA_UNSUPPORTED", f"Invalid schema for {path}.{key}")
            validate_json_schema(item, child_schema, path=f"{path}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be an array")
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} has too few items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} has too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, path=f"{path}[{index}]")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be a string")
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} is too short")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            raise ToolError("INVALID_ARGUMENT", f"{path} is too long")
        return
    if expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be an integer")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be a number")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolError("INVALID_ARGUMENT", f"{path} must be a boolean")
        return
    elif expected in {None, "null"}:
        if expected == "null" and value is not None:
            raise ToolError("INVALID_ARGUMENT", f"{path} must be null")
        return
    elif expected not in {"integer", "number"}:
        raise ToolError("EXTENSION_SCHEMA_UNSUPPORTED", f"Unsupported type at {path}: {expected}")
    if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
        raise ToolError("INVALID_ARGUMENT", f"{path} is below minimum")
    if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
        raise ToolError("INVALID_ARGUMENT", f"{path} is above maximum")


def _action_timeout(record: ExtensionRecord, action: ExtensionAction) -> int:
    return action.timeout_seconds if action.timeout_seconds is not None else record.manifest.execution_timeout_seconds


def _wait_timeout(timeout_seconds: int) -> int | None:
    return None if timeout_seconds == 0 else timeout_seconds


def _worker_context_and_environment(
    record: ExtensionRecord,
    *,
    workspace: Workspace | None,
    read_only: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    workspace_root = workspace.root if workspace is not None else None
    state_dir: str | None = None
    if record.manifest.workspace_adapter.get("state") == "profile":
        suffix = workspace_id(workspace_root) if workspace_root is not None else "global"
        state_path = extension_state_root() / record.manifest.extension_id / suffix
        try:
            state_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError("EXTENSION_STATE_FAILED", f"Could not prepare extension state directory: {exc}") from exc
        state_dir = str(state_path)

    env_root = workspace_root or record.path
    env = clean_environment(env_root)
    env[INTERNAL_CONFIG_ROOT_ENV] = str(user_config_root())
    inherited_names: list[str] = []
    for permission in record.manifest.permissions:
        match = ENVIRONMENT_PERMISSION_RE.fullmatch(permission)
        if not match:
            continue
        name = match.group(1)
        if name not in os.environ:
            continue
        value = os.environ[name]
        if len(value.encode("utf-8", errors="strict")) > MAX_INHERITED_ENV_VALUE_BYTES:
            raise ToolError(
                "EXTENSION_ENV_TOO_LARGE",
                f"Environment variable {name} exceeds the extension inheritance limit.",
                environment_name=name,
            )
        env[name] = value
        inherited_names.append(name)
    env["PYTHONUTF8"] = "1"
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

    context = {
        "extension_id": record.manifest.extension_id,
        "extension_version": record.manifest.version,
        "permissions": list(record.manifest.permissions),
        "workspace_root": str(workspace_root) if workspace_root is not None else None,
        "workspace_read_only": bool(read_only),
        "state_dir": state_dir,
        "workspace_adapter": record.manifest.workspace_adapter,
        "inherited_environment": inherited_names,
    }
    return context, env


def _inherited_secret_values(context: dict[str, Any], env: dict[str, str]) -> tuple[str, ...]:
    names = context.get("inherited_environment") or []
    secret_name = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)", re.IGNORECASE)
    return tuple(
        env[name]
        for name in names
        if isinstance(name, str) and name in env and env[name] and secret_name.search(name)
    )


def _redact_secrets(text: str, secrets: tuple[str, ...]) -> str:
    redacted = text
    # Replace longer values first so one secret that prefixes another cannot
    # partially redact the longer value and leak its suffix.
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _sanitize_secret_values(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact_secrets(value, secrets)
    if isinstance(value, list):
        return [_sanitize_secret_values(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            (_redact_secrets(key, secrets) if isinstance(key, str) else key): _sanitize_secret_values(item, secrets)
            for key, item in value.items()
        }
    return value


def _worker_request(
    record: ExtensionRecord,
    action: ExtensionAction,
    params: dict[str, Any],
    *,
    workspace: Workspace | None,
    read_only: bool,
) -> tuple[bytes, dict[str, str], tuple[str, ...]]:
    context, env = _worker_context_and_environment(record, workspace=workspace, read_only=read_only)
    try:
        request = json.dumps(
            {
                "action": action.name,
                "params": params,
                "context": context,
                "extension_sha256": record.sha256,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolError("INVALID_ARGUMENT", "Extension request must contain only strict JSON values.") from exc
    if len(request) > MAX_WORKER_REQUEST_BYTES:
        raise ToolError("EXTENSION_REQUEST_TOO_LARGE", "Extension request is too large.")
    return request, env, _inherited_secret_values(context, env)


def _start_worker_process(record: ExtensionRecord, request: bytes, env: dict[str, str]) -> tuple[subprocess.Popen[bytes], _BoundedCapture, _BoundedCapture]:
    kwargs: dict[str, Any] = {
        # The worker itself starts from a neutral directory. After it creates
        # and verifies its private Extension snapshot it temporarily chdirs
        # into that snapshot, so relative plugin file access cannot fall back
        # to the mutable source directory or keep it locked on Windows.
        "cwd": tempfile.gettempdir(),
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "close_fds": True,
    }
    kwargs.update(owned_process_group_kwargs())
    try:
        process = subprocess.Popen(_worker_argv(record), **kwargs)
    except OSError as exc:
        raise ToolError("EXTENSION_START_FAILED", f"Could not start extension worker: {exc}") from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout = _BoundedCapture(process.stdout, MAX_WORKER_RESPONSE_BYTES)
    stderr = _BoundedCapture(process.stderr, MAX_WORKER_LOG_BYTES)
    stdout.start()
    stderr.start()
    try:
        process.stdin.write(request)
        process.stdin.close()
    except OSError as exc:
        terminate_owned_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            process.stdin.close()
        except OSError:
            pass
        stdout.join(timeout=1)
        stderr.join(timeout=1)
        try:
            process.stdout.close()
        except OSError:
            pass
        try:
            process.stderr.close()
        except OSError:
            pass
        raise ToolError("EXTENSION_PROTOCOL_ERROR", f"Could not send request to extension worker: {exc}") from exc
    return process, stdout, stderr


def _close_worker_streams(process: subprocess.Popen[bytes], stdout: _BoundedCapture, stderr: _BoundedCapture) -> str:
    stdout.join(timeout=5)
    stderr.join(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if stdout.truncated:
        raise ToolError("EXTENSION_RESPONSE_TOO_LARGE", "Extension response exceeded the protocol limit.")
    return bytes(stderr.data).decode("utf-8", errors="replace").strip()


def _workspace_artifact_metadata(workspace: Workspace, raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        path_value = raw
        label = None
        kind = None
    elif isinstance(raw, dict):
        unknown = sorted(set(raw).difference({"path", "label", "kind"}))
        if unknown:
            raise ToolError("EXTENSION_ARTIFACT_INVALID", f"Unknown workspace_artifacts fields: {', '.join(unknown)}")
        path_value = raw.get("path")
        label = raw.get("label")
        kind = raw.get("kind")
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            raise ToolError("EXTENSION_ARTIFACT_INVALID", "workspace_artifacts label must be a string <= 200 characters")
        if kind is not None and (not isinstance(kind, str) or len(kind) > 80):
            raise ToolError("EXTENSION_ARTIFACT_INVALID", "workspace_artifacts kind must be a string <= 80 characters")
    else:
        raise ToolError("EXTENSION_ARTIFACT_INVALID", "workspace_artifacts entries must be strings or objects")
    if not isinstance(path_value, str) or not path_value:
        raise ToolError("EXTENSION_ARTIFACT_INVALID", "workspace_artifacts path is required")
    path = workspace.resolve(path_value)
    if not path.is_file():
        raise ToolError("EXTENSION_ARTIFACT_NOT_FOUND", "Declared workspace artifact does not exist.", path=path_value)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError("EXTENSION_ARTIFACT_READ_FAILED", f"Could not inspect workspace artifact: {exc}", path=path_value) from exc
    result: dict[str, Any] = {
        "path": path.relative_to(workspace.root).as_posix(),
        "size": size,
        "sha256": digest.hexdigest(),
    }
    if label is not None:
        result["label"] = label
    if kind is not None:
        result["kind"] = kind
    return result


def _finalize_worker_result(
    record: ExtensionRecord,
    action: ExtensionAction,
    envelope: Any,
    *,
    exit_code: int,
    stderr_text: str,
    workspace: Workspace | None,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    envelope = _sanitize_secret_values(envelope, secrets)
    stderr_text = _redact_secrets(stderr_text, secrets)
    if not isinstance(envelope, dict):
        raise ToolError("EXTENSION_PROTOCOL_ERROR", "Extension worker response must be an object.")
    if not envelope.get("ok"):
        error = envelope.get("error")
        if isinstance(error, dict):
            raise ToolError(
                str(error.get("code") or "EXTENSION_ERROR"),
                str(error.get("message") or "Extension action failed."),
                **(error.get("details") if isinstance(error.get("details"), dict) else {}),
            )
        raise ToolError("EXTENSION_ERROR", "Extension action failed.")
    if exit_code != 0:
        raise ToolError(
            "EXTENSION_FAILED",
            f"Extension worker exited with code {exit_code} despite returning success.",
            stderr=stderr_text[:4000],
        )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise ToolError("EXTENSION_PROTOCOL_ERROR", "Extension result must be an object.")
    result = dict(result)
    artifacts = result.get("workspace_artifacts")
    if artifacts is not None:
        if workspace is None:
            raise ToolError("EXTENSION_ARTIFACT_INVALID", "workspace_artifacts requires a selected workspace")
        if not isinstance(artifacts, list) or len(artifacts) > MAX_WORKSPACE_ARTIFACTS:
            raise ToolError("EXTENSION_ARTIFACT_INVALID", f"workspace_artifacts must be a list of <= {MAX_WORKSPACE_ARTIFACTS} entries")
        result["workspace_artifacts"] = [_workspace_artifact_metadata(workspace, item) for item in artifacts]
    result.setdefault("extension_id", record.manifest.extension_id)
    result.setdefault("extension_action", action.name)
    if stderr_text:
        result.setdefault("extension_log", stderr_text[:4000])
    return result


def _decode_worker_result(
    record: ExtensionRecord,
    action: ExtensionAction,
    stdout: _BoundedCapture,
    stderr_text: str,
    *,
    exit_code: int,
    workspace: Workspace | None,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    safe_stderr = _redact_secrets(stderr_text, secrets)
    try:
        envelope = json.loads(bytes(stdout.data), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ToolError("EXTENSION_PROTOCOL_ERROR", "Extension worker returned invalid JSON.", stderr=safe_stderr[:4000]) from exc
    return _finalize_worker_result(record, action, envelope, exit_code=exit_code, stderr_text=stderr_text, workspace=workspace, secrets=secrets)


def _run_worker(
    record: ExtensionRecord,
    action: ExtensionAction,
    params: dict[str, Any],
    *,
    workspace: Workspace | None,
    read_only: bool,
) -> dict[str, Any]:
    request, env, secrets = _worker_request(record, action, params, workspace=workspace, read_only=read_only)
    process, stdout, stderr = _start_worker_process(record, request, env)
    timeout = _action_timeout(record, action)
    try:
        try:
            exit_code = process.wait(timeout=_wait_timeout(timeout))
        except subprocess.TimeoutExpired:
            terminate_owned_process_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise ToolError(
                "EXTENSION_TIMEOUT",
                f"Extension exceeded {timeout} seconds.",
                extension_id=record.manifest.extension_id,
                action=action.name,
            )
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
    stderr_text = _close_worker_streams(process, stdout, stderr)
    return _decode_worker_result(record, action, stdout, stderr_text, exit_code=exit_code, workspace=workspace, secrets=secrets)


@dataclass
class _ExtensionJob:
    job_id: str
    extension_id: str
    action_name: str
    workspace_root: str | None
    timeout_seconds: int
    process: subprocess.Popen[bytes]
    stdout: _BoundedCapture
    stderr: _BoundedCapture
    started_at: float
    secrets: tuple[str, ...]
    status: str = "running"
    finished_at: float | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancel_requested: bool = False


class ExtensionJobManager:
    """Owns long-running extension workers so plugins never need detached subprocesses."""

    def __init__(self) -> None:
        self._jobs: dict[str, _ExtensionJob] = {}
        self._starting_jobs = 0
        self._lock = threading.Lock()
        atexit.register(self.close)

    def _prune_finished_locked(self) -> None:
        finished = sorted(
            (job for job in self._jobs.values() if job.status != "running"),
            key=lambda job: job.finished_at or job.started_at,
        )
        excess = len(finished) - MAX_RETAINED_FINISHED_JOBS
        for job in finished[: max(0, excess)]:
            self._jobs.pop(job.job_id, None)

    def start(
        self,
        record: ExtensionRecord,
        action: ExtensionAction,
        params: dict[str, Any],
        *,
        workspace: Workspace | None,
        read_only: bool,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune_finished_locked()
            running = sum(job.status == "running" for job in self._jobs.values())
            if running + self._starting_jobs >= MAX_RUNNING_EXTENSION_JOBS:
                raise ToolError(
                    "EXTENSION_JOB_LIMIT",
                    f"At most {MAX_RUNNING_EXTENSION_JOBS} extension jobs may run concurrently.",
                    limit=MAX_RUNNING_EXTENSION_JOBS,
                )
            self._starting_jobs += 1
        try:
            request, env, secrets = _worker_request(record, action, params, workspace=workspace, read_only=read_only)
            process, stdout, stderr = _start_worker_process(record, request, env)
            job = _ExtensionJob(
                job_id=uuid.uuid4().hex,
                extension_id=record.manifest.extension_id,
                action_name=action.name,
                workspace_root=str(workspace.root) if workspace is not None else None,
                timeout_seconds=_action_timeout(record, action),
                process=process,
                stdout=stdout,
                stderr=stderr,
                started_at=time.time(),
                secrets=secrets,
            )
        except Exception:
            with self._lock:
                self._starting_jobs -= 1
            raise
        with self._lock:
            self._starting_jobs -= 1
            self._jobs[job.job_id] = job
        monitor = threading.Thread(
            target=self._monitor,
            args=(job, record, action, workspace),
            name=f"folderbridge-extension-job-{job.job_id[:8]}",
            daemon=True,
        )
        monitor.start()
        return {
            "job_id": job.job_id,
            "status": "running",
            "extension_id": record.manifest.extension_id,
            "extension_action": action.name,
            "timeout_seconds": job.timeout_seconds,
        }

    def _monitor(
        self,
        job: _ExtensionJob,
        record: ExtensionRecord,
        action: ExtensionAction,
        workspace: Workspace | None,
    ) -> None:
        timed_out = False
        try:
            try:
                exit_code = job.process.wait(timeout=_wait_timeout(job.timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_owned_process_tree(job.process)
                try:
                    exit_code = job.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    exit_code = job.process.poll() if job.process.poll() is not None else -1
            stderr_text = _close_worker_streams(job.process, job.stdout, job.stderr)
            with self._lock:
                cancel_requested = job.cancel_requested
            if cancel_requested:
                status = "cancelled"
                result = None
                error = None
            elif timed_out:
                status = "timed_out"
                result = None
                error = {
                    "code": "EXTENSION_JOB_TIMEOUT",
                    "message": f"Extension job exceeded {job.timeout_seconds} seconds.",
                }
            else:
                try:
                    result = _decode_worker_result(record, action, job.stdout, stderr_text, exit_code=exit_code, workspace=workspace, secrets=job.secrets)
                    status = "succeeded"
                    error = None
                except ToolError as exc:
                    status = "failed"
                    result = None
                    error = {"code": exc.code, "message": str(exc), "details": exc.details}
        except Exception as exc:
            exit_code = job.process.poll()
            status = "failed"
            result = None
            error = {
                "code": "EXTENSION_JOB_INTERNAL_ERROR",
                "message": _redact_secrets(f"{type(exc).__name__}: {exc}", job.secrets),
            }
        with self._lock:
            job.status = status
            job.exit_code = exit_code
            job.result = result
            job.error = error
            job.finished_at = time.time()
            self._prune_finished_locked()

    def _get(self, job_id: str, *, workspace: Workspace | None) -> _ExtensionJob:
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ToolError("INVALID_ARGUMENT", "job_id must be a FolderBridge extension job id")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("EXTENSION_JOB_NOT_FOUND", "Extension job is not known to this FolderBridge process.", job_id=job_id)
        if job.workspace_root is not None:
            if workspace is None:
                raise ToolError("WORKSPACE_REQUIRED", "This extension job belongs to a workspace; pass workspace_id.")
            if str(workspace.root) != job.workspace_root:
                raise ToolError("EXTENSION_JOB_WORKSPACE_MISMATCH", "Extension job belongs to a different workspace.", job_id=job_id)
        return job

    def status(self, job_id: str, *, workspace: Workspace | None) -> dict[str, Any]:
        job = self._get(job_id, workspace=workspace)
        with self._lock:
            payload: dict[str, Any] = {
                "job_id": job.job_id,
                "status": job.status,
                "extension_id": job.extension_id,
                "extension_action": job.action_name,
                "timeout_seconds": job.timeout_seconds,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "exit_code": job.exit_code,
            }
            if job.result is not None:
                payload["result"] = job.result
            if job.error is not None:
                payload["error"] = job.error
            return payload

    def cancel(self, job_id: str, *, workspace: Workspace | None) -> dict[str, Any]:
        job = self._get(job_id, workspace=workspace)
        with self._lock:
            if job.status != "running":
                return {"job_id": job.job_id, "status": job.status, "already_finished": True}
            job.cancel_requested = True
        terminate_owned_process_tree(job.process)
        return {"job_id": job.job_id, "status": "cancelling"}

    def close(self) -> None:
        with self._lock:
            running = [job for job in self._jobs.values() if job.status == "running"]
            for job in running:
                job.cancel_requested = True
        for job in running:
            terminate_owned_process_tree(job.process)


def _worker_argv(record: ExtensionRecord) -> list[str]:
    suffix = ["--extension-path", str(record.path)]
    if record.bundled:
        suffix.append("--bundled")
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "extension-worker", *suffix]
    launcher = Path(__file__).resolve().parents[1] / "folderbridge_launcher.py"
    return [sys.executable, str(launcher), "extension-worker", *suffix]


class _BoundedCapture(threading.Thread):
    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        while True:
            try:
                chunk = self.stream.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = self.limit - len(self.data)
            if room > 0:
                self.data.extend(chunk[:room])
            if len(chunk) > room:
                self.truncated = True


def _safe_relative_file(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("entrypoint must be a clean POSIX relative path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("entrypoint may not escape the extension directory")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
        raise ValueError("entrypoint must name a regular non-link file")
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("entrypoint escapes extension directory") from exc
    return path


def _hash_extension(root: Path) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    manifest_data: bytes | None = None
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if any(part in {"__pycache__", ".git", ".svn"} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or _is_reparse_point(path):
            raise ValueError("extension trees may not contain links or reparse points")
        if not path.is_file():
            continue
        if path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        count += 1
        if count > MAX_EXTENSION_FILES:
            raise ValueError(f"extension exceeds {MAX_EXTENSION_FILES} files")
        try:
            before = path.stat()
            if before.st_size > MAX_EXTENSION_BYTES - total:
                raise ValueError(f"extension exceeds {MAX_EXTENSION_BYTES} bytes")
            data = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise ValueError(f"could not read extension file while hashing: {path.relative_to(root).as_posix()}") from exc
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if len(data) != before.st_size or before_identity != after_identity:
            raise ValueError(f"extension file changed while hashing: {path.relative_to(root).as_posix()}")
        if path.relative_to(root).as_posix() == MANIFEST_NAME:
            manifest_data = data
        total += len(data)
        if total > MAX_EXTENSION_BYTES:
            raise ValueError(f"extension exceeds {MAX_EXTENSION_BYTES} bytes")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    if manifest_data is None:
        raise ValueError(f"missing regular {MANIFEST_NAME}")
    return digest.hexdigest(), manifest_data


def _config_base() -> Path:
    return user_config_root()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
