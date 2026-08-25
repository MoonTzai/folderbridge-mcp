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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

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
MAX_FOREGROUND_EXTENSION_WORKERS = 16
MAX_EXTENSION_JOB_SHUTDOWN_SECONDS = 5.0
MAX_EXTENSION_JOB_CANCEL_GRACE_SECONDS = 2.0
ACTIVE_EXTENSION_JOB_STATUSES = frozenset({"running", "termination_pending"})
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


@dataclass(frozen=True)
class PreparedExtensionAction:
    record: ExtensionRecord
    action: ExtensionAction


@dataclass(frozen=True)
class PreparedExtensionRun:
    contract: PreparedExtensionAction
    params: dict[str, Any]
    workspace: Workspace | None
    read_only: bool

    @property
    def record(self) -> ExtensionRecord:
        return self.contract.record

    @property
    def action(self) -> ExtensionAction:
        return self.contract.action


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
                    existing = records[extension_id]
                    if existing.bundled and not record.bundled:
                        # A release may absorb a previously external Extension. The
                        # release-trusted bundled copy remains authoritative; an older
                        # user installation with the same id is safely superseded rather
                        # than reported as a persistent upgrade-time load failure.
                        continue
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

    def describe(
        self,
        workspace: Path | None = None,
        *,
        include_action_schemas: bool = True,
    ) -> dict[str, Any]:
        records, errors = self.scan()
        rendered: list[dict[str, Any]] = []
        for extension_id in sorted(records):
            record = records[extension_id]
            trust = self.trust_store.status(record)
            applicable = _workspace_applicable(record.manifest.workspace_adapter, workspace)
            if include_action_schemas:
                actions: list[Any] = [
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
                ]
            else:
                actions = sorted(record.manifest.actions)
            rendered.append(
                {
                    "id": extension_id,
                    "name": record.manifest.name,
                    "version": record.manifest.version,
                    "description": record.manifest.description,
                    "bundled": record.bundled,
                    "sha256": record.sha256,
                    "permissions": list(record.manifest.permissions),
                    "actions": actions,
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

    def prepare_action(self, extension_id: str, action_name: str) -> PreparedExtensionAction:
        record = self.get(extension_id)
        action = record.manifest.actions.get(action_name)
        if action is None:
            raise ToolError(
                "EXTENSION_ACTION_NOT_FOUND",
                "Extension action does not exist.",
                extension_id=extension_id,
                available=sorted(record.manifest.actions),
            )
        return PreparedExtensionAction(record=record, action=action)

    def prepare_run(
        self,
        contract: PreparedExtensionAction,
        params: dict[str, Any],
        *,
        workspace: Workspace | None,
        read_only: bool,
    ) -> PreparedExtensionRun:
        self._authorize_prepared(contract.record, contract.action, workspace=workspace, read_only=read_only)
        validate_json_schema(params, contract.action.input_schema, path="params")
        return PreparedExtensionRun(
            contract=contract,
            params=dict(params),
            workspace=workspace,
            read_only=read_only,
        )

    def execute_prepared(
        self,
        prepared: PreparedExtensionRun,
        *,
        on_job_finish: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        # Recheck mutable authorization/applicability immediately before spawn,
        # but never rescan the manifest: locking and execution must use exactly
        # the same immutable action contract. The worker's private snapshot then
        # verifies that source bytes still match prepared.record.sha256.
        try:
            self._authorize_prepared(
                prepared.record,
                prepared.action,
                workspace=prepared.workspace,
                read_only=prepared.read_only,
            )
        except Exception:
            if on_job_finish is not None:
                on_job_finish()
            raise
        if prepared.action.run_mode == "job":
            return self.jobs.start(
                prepared.record,
                prepared.action,
                prepared.params,
                workspace=prepared.workspace,
                read_only=prepared.read_only,
                on_finish=on_job_finish,
            )
        return _run_worker(
            prepared.record,
            prepared.action,
            prepared.params,
            workspace=prepared.workspace,
            read_only=prepared.read_only,
            owner=self.jobs,
            on_finish=on_job_finish,
        )

    def run(
        self,
        extension_id: str,
        action_name: str,
        params: dict[str, Any],
        *,
        workspace: Workspace | None,
        read_only: bool,
        on_job_finish: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        contract = self.prepare_action(extension_id, action_name)
        prepared = self.prepare_run(
            contract,
            params,
            workspace=workspace,
            read_only=read_only,
        )
        return self.execute_prepared(prepared, on_job_finish=on_job_finish)

    def _authorize_prepared(
        self,
        record: ExtensionRecord,
        action: ExtensionAction,
        *,
        workspace: Workspace | None,
        read_only: bool,
    ) -> None:
        extension_id = record.manifest.extension_id
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
    job_cancel_path: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    workspace_root = workspace.root if workspace is not None else None
    state_dir: str | None = None
    if "extension.state" in record.manifest.permissions:
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
        "job_cancel_path": job_cancel_path,
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
    job_cancel_path: str | None = None,
) -> tuple[bytes, dict[str, str], tuple[str, ...]]:
    context, env = _worker_context_and_environment(
        record,
        workspace=workspace,
        read_only=read_only,
        job_cancel_path=job_cancel_path,
    )
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


class _WorkerLaunchTerminationPending(RuntimeError):
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stdout: _BoundedCapture,
        stderr: _BoundedCapture,
        error: ToolError,
    ) -> None:
        super().__init__(str(error))
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
        self.error = error


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
        error = ToolError("EXTENSION_PROTOCOL_ERROR", f"Could not send request to extension worker: {exc}")
        terminate_owned_process_tree(process)
        try:
            process.stdin.close()
        except OSError:
            pass
        if not _wait_for_process_exit(process, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
            # The caller must retain ownership of the still-live worker and any
            # workspace mutation lease; never hide this process behind a plain
            # protocol error.
            raise _WorkerLaunchTerminationPending(process, stdout, stderr, error) from exc
        try:
            _close_worker_streams(process, stdout, stderr)
        except Exception:
            pass
        raise error from exc
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
    owner: ExtensionJobManager,
    on_finish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    try:
        owner.reserve_foreground()
    except Exception:
        if on_finish is not None:
            on_finish()
        raise
    reservation_active = True
    worker: _ForegroundWorker | None = None
    try:
        request, env, secrets = _worker_request(record, action, params, workspace=workspace, read_only=read_only)
        try:
            process, stdout, stderr = _start_worker_process(record, request, env)
        except _WorkerLaunchTerminationPending as pending:
            worker = owner.adopt_foreground(pending.process, pending.stdout, pending.stderr, on_finish)
            reservation_active = False
            raise ToolError(
                "EXTENSION_TERMINATION_PENDING",
                "Extension worker could not be terminated after a startup protocol failure; workspace mutation protection remains held until the process exits.",
                extension_id=record.manifest.extension_id,
                action=action.name,
                recovery_token=worker.token,
                cause_code=pending.error.code,
            ) from pending

        worker = owner.adopt_foreground(process, stdout, stderr, on_finish)
        reservation_active = False
        timeout = _action_timeout(record, action)
        state = owner.wait_foreground(worker, _wait_timeout(timeout))
        if state == "shutdown":
            terminate_owned_process_tree(worker.process)
            raise ToolError(
                "SERVER_SHUTTING_DOWN",
                "FolderBridge is shutting down; the foreground Extension request was released while process ownership remains with the host.",
                extension_id=record.manifest.extension_id,
                action=action.name,
                recovery_token=worker.token,
            )
        if state == "monitor_failed":
            terminate_owned_process_tree(worker.process)
            if _wait_for_process_exit(worker.process, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
                owner._finalize_foreground(worker, worker.process.poll())
                raise ToolError(
                    "EXTENSION_MONITOR_FAILED",
                    "Could not start the foreground Extension lifecycle monitor.",
                    extension_id=record.manifest.extension_id,
                    action=action.name,
                )
            raise ToolError(
                "EXTENSION_TERMINATION_PENDING",
                "The foreground Extension lifecycle monitor failed and the worker process is still alive; workspace mutation protection remains held.",
                extension_id=record.manifest.extension_id,
                action=action.name,
                recovery_token=worker.token,
            )
        if state == "timeout":
            terminate_owned_process_tree(worker.process)
            if not owner.wait_foreground_exit(worker, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
                raise ToolError(
                    "EXTENSION_TERMINATION_PENDING",
                    "Extension timed out and the worker process is still alive; workspace mutation protection remains held until the process exits.",
                    extension_id=record.manifest.extension_id,
                    action=action.name,
                    recovery_token=worker.token,
                )
            raise ToolError(
                "EXTENSION_TIMEOUT",
                f"Extension exceeded {timeout} seconds.",
                extension_id=record.manifest.extension_id,
                action=action.name,
            )

        if worker.cleanup_error is not None:
            raise worker.cleanup_error
        if worker.exit_code is None:
            raise ToolError("EXTENSION_PROTOCOL_ERROR", "Foreground Extension worker finished without an exit code.")
        return _decode_worker_result(
            record,
            action,
            worker.stdout,
            worker.stderr_text,
            exit_code=worker.exit_code,
            workspace=workspace,
            secrets=secrets,
        )
    finally:
        if reservation_active:
            owner.finish_foreground(on_finish)


@dataclass
class _ForegroundWorker:
    token: str
    process: subprocess.Popen[bytes]
    stdout: _BoundedCapture
    stderr: _BoundedCapture
    on_finish: Callable[[], None] | None
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock()),
        repr=False,
        compare=False,
    )
    finished: bool = False
    finalizing: bool = False
    shutdown_requested: bool = False
    monitor_failed: bool = False
    pending_reaper_started: bool = False
    exit_code: int | None = None
    stderr_text: str = ""
    cleanup_error: ToolError | None = None


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
    on_finish: Callable[[], None] | None = None
    cancel_control_dir: str | None = None
    cancel_token_path: str | None = None
    cancel_reaper_started: bool = False
    status: str = "running"
    finished_at: float | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    finish_notified: bool = False
    pending_terminal_status: str | None = None
    pending_terminal_error: dict[str, Any] | None = None
    reconcile_in_progress: bool = False
    pending_reaper_started: bool = False


class ExtensionJobManager:
    """Owns long-running extension workers so plugins never need detached subprocesses."""

    def __init__(self) -> None:
        self._jobs: dict[str, _ExtensionJob] = {}
        self._starting_jobs = 0
        self._foreground_active = 0
        self._foreground_running: dict[str, _ForegroundWorker] = {}
        self._closed = False
        self._lock = threading.Lock()
        atexit.register(self.close)

    @staticmethod
    def _call_finish(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def _notify_finish(self, job: _ExtensionJob) -> None:
        with self._lock:
            if job.finish_notified:
                return
            job.finish_notified = True
            callback = job.on_finish
        self._call_finish(callback)

    @staticmethod
    def _prepare_cancel_control(job_id: str) -> tuple[str, str]:
        control_dir = tempfile.mkdtemp(prefix=f"folderbridge-extension-job-{job_id[:8]}-")
        try:
            os.chmod(control_dir, 0o700)
        except OSError:
            pass
        return control_dir, str(Path(control_dir) / "cancel")

    @staticmethod
    def _cleanup_cancel_control_paths(control_dir: str | None, token_path: str | None) -> None:
        if not control_dir:
            return
        directory = Path(control_dir)
        token = Path(token_path) if token_path else directory / "cancel"
        try:
            token.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
        try:
            directory.rmdir()
        except OSError:
            pass

    @classmethod
    def _cleanup_cancel_control(cls, job: _ExtensionJob) -> None:
        cls._cleanup_cancel_control_paths(job.cancel_control_dir, job.cancel_token_path)
        job.cancel_control_dir = None
        job.cancel_token_path = None

    @staticmethod
    def _signal_cancel(job: _ExtensionJob) -> bool:
        raw = job.cancel_token_path
        if not raw:
            return False
        try:
            path = Path(raw)
            path.write_text("cancel\n", encoding="ascii")
            return True
        except OSError:
            return False

    def _ensure_cancel_reaper(self, job: _ExtensionJob) -> None:
        with self._lock:
            if job.cancel_reaper_started or job.process.poll() is not None:
                return
            job.cancel_reaper_started = True
        reaper = threading.Thread(
            target=self._cancel_reaper,
            args=(job,),
            name=f"folderbridge-extension-cancel-{job.job_id[:8]}",
            daemon=True,
        )
        try:
            reaper.start()
        except Exception:
            with self._lock:
                job.cancel_reaper_started = False
            terminate_owned_process_tree(job.process)

    def _cancel_reaper(self, job: _ExtensionJob) -> None:
        try:
            if _wait_for_process_exit_without_kill(job.process, MAX_EXTENSION_JOB_CANCEL_GRACE_SECONDS):
                return
            terminate_owned_process_tree(job.process)
        finally:
            with self._lock:
                job.cancel_reaper_started = False

    def reserve_foreground(self) -> None:
        with self._lock:
            if self._closed:
                raise ToolError("SERVER_SHUTTING_DOWN", "FolderBridge is shutting down; new Extension workers are disabled.")
            if self._foreground_active >= MAX_FOREGROUND_EXTENSION_WORKERS:
                raise ToolError(
                    "EXTENSION_FOREGROUND_LIMIT",
                    f"At most {MAX_FOREGROUND_EXTENSION_WORKERS} foreground Extension workers may remain active concurrently.",
                    limit=MAX_FOREGROUND_EXTENSION_WORKERS,
                )
            self._foreground_active += 1

    def finish_foreground(self, on_finish: Callable[[], None] | None) -> None:
        """Release a foreground reservation that never adopted a process."""
        self._call_finish(on_finish)
        with self._lock:
            if self._foreground_active > 0:
                self._foreground_active -= 1

    def adopt_foreground(
        self,
        process: subprocess.Popen[bytes],
        stdout: _BoundedCapture,
        stderr: _BoundedCapture,
        on_finish: Callable[[], None] | None,
    ) -> _ForegroundWorker:
        token = uuid.uuid4().hex
        worker = _ForegroundWorker(token, process, stdout, stderr, on_finish)
        with self._lock:
            self._foreground_running[token] = worker
            closed = self._closed
        monitor = threading.Thread(
            target=self._monitor_foreground,
            args=(worker,),
            name=f"folderbridge-extension-foreground-{token[:8]}",
            daemon=True,
        )
        try:
            monitor.start()
        except Exception:
            with worker.condition:
                worker.monitor_failed = True
                worker.shutdown_requested = True
                worker.condition.notify_all()
            terminate_owned_process_tree(worker.process)
            if worker.process.poll() is not None:
                self._finalize_foreground(worker, worker.process.poll())
            else:
                self._ensure_foreground_pending_reaper(worker)
            return worker
        if closed:
            self.request_foreground_shutdown(worker)
            terminate_owned_process_tree(worker.process)
        return worker

    def _monitor_foreground(self, worker: _ForegroundWorker) -> None:
        try:
            exit_code = worker.process.wait()
        except Exception:
            exit_code = worker.process.poll()
            if exit_code is None:
                with worker.condition:
                    worker.monitor_failed = True
                    worker.condition.notify_all()
                self._ensure_foreground_pending_reaper(worker)
                return
        self._finalize_foreground(worker, exit_code)

    def _ensure_foreground_pending_reaper(self, worker: _ForegroundWorker) -> None:
        with worker.condition:
            if worker.finished or worker.pending_reaper_started:
                return
            worker.pending_reaper_started = True
        reaper = threading.Thread(
            target=self._monitor_pending_foreground,
            args=(worker,),
            name=f"folderbridge-extension-foreground-pending-{worker.token[:8]}",
            daemon=True,
        )
        try:
            reaper.start()
        except Exception:
            with worker.condition:
                worker.pending_reaper_started = False

    def _monitor_pending_foreground(self, worker: _ForegroundWorker) -> None:
        while True:
            exit_code = worker.process.poll()
            if exit_code is not None:
                self._finalize_foreground(worker, exit_code)
                return
            try:
                exit_code = worker.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                time.sleep(0.1)
                continue
            if exit_code is not None:
                self._finalize_foreground(worker, exit_code)
                return

    def _finalize_foreground(self, worker: _ForegroundWorker, exit_code: int | None) -> bool:
        if exit_code is None:
            return False
        with worker.condition:
            if worker.finished or worker.finalizing:
                return False
            worker.finalizing = True
        stderr_text = ""
        cleanup_error: ToolError | None = None
        try:
            stderr_text = _close_worker_streams(worker.process, worker.stdout, worker.stderr)
        except ToolError as exc:
            cleanup_error = exc
        except Exception as exc:
            cleanup_error = ToolError("EXTENSION_PROTOCOL_ERROR", f"Could not finalize Extension worker streams: {type(exc).__name__}")
        self._call_finish(worker.on_finish)
        with worker.condition:
            worker.exit_code = exit_code
            worker.stderr_text = stderr_text
            worker.cleanup_error = cleanup_error
            worker.finished = True
            worker.finalizing = False
            worker.condition.notify_all()
        with self._lock:
            if self._foreground_running.pop(worker.token, None) is not None and self._foreground_active > 0:
                self._foreground_active -= 1
        return True

    def request_foreground_shutdown(self, worker: _ForegroundWorker) -> None:
        with worker.condition:
            worker.shutdown_requested = True
            worker.condition.notify_all()

    def wait_foreground(self, worker: _ForegroundWorker, timeout: float | None) -> str:
        with worker.condition:
            worker.condition.wait_for(
                lambda: worker.finished or worker.shutdown_requested or worker.monitor_failed,
                timeout=timeout,
            )
            if worker.finished:
                return "finished"
            if worker.shutdown_requested:
                return "shutdown"
            if worker.monitor_failed:
                return "monitor_failed"
            return "timeout"

    def wait_foreground_exit(self, worker: _ForegroundWorker, timeout: float | None) -> bool:
        with worker.condition:
            worker.condition.wait_for(lambda: worker.finished, timeout=timeout)
            return worker.finished

    def _prune_finished_locked(self) -> None:
        finished = sorted(
            (job for job in self._jobs.values() if job.status not in ACTIVE_EXTENSION_JOB_STATUSES),
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
        on_finish: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ToolError("SERVER_SHUTTING_DOWN", "FolderBridge is shutting down; new Extension Jobs are disabled.")
            self._prune_finished_locked()
            running = sum(job.status in ACTIVE_EXTENSION_JOB_STATUSES for job in self._jobs.values())
            if running + self._starting_jobs >= MAX_RUNNING_EXTENSION_JOBS:
                raise ToolError(
                    "EXTENSION_JOB_LIMIT",
                    f"At most {MAX_RUNNING_EXTENSION_JOBS} extension jobs may run concurrently.",
                    limit=MAX_RUNNING_EXTENSION_JOBS,
                )
            self._starting_jobs += 1
        job_id = uuid.uuid4().hex
        cancel_control_dir: str | None = None
        cancel_token_path: str | None = None
        try:
            cancel_control_dir, cancel_token_path = self._prepare_cancel_control(job_id)
            request, env, secrets = _worker_request(
                record,
                action,
                params,
                workspace=workspace,
                read_only=read_only,
                job_cancel_path=cancel_token_path,
            )
            process, stdout, stderr = _start_worker_process(record, request, env)
            job = _ExtensionJob(
                job_id=job_id,
                extension_id=record.manifest.extension_id,
                action_name=action.name,
                workspace_root=str(workspace.root) if workspace is not None else None,
                timeout_seconds=_action_timeout(record, action),
                process=process,
                stdout=stdout,
                stderr=stderr,
                started_at=time.time(),
                secrets=secrets,
                on_finish=on_finish,
                cancel_control_dir=cancel_control_dir,
                cancel_token_path=cancel_token_path,
            )
        except _WorkerLaunchTerminationPending as pending:
            terminal_error = {
                "code": pending.error.code,
                "message": str(pending.error),
                "details": pending.error.details,
            }
            job = _ExtensionJob(
                job_id=job_id,
                extension_id=record.manifest.extension_id,
                action_name=action.name,
                workspace_root=str(workspace.root) if workspace is not None else None,
                timeout_seconds=_action_timeout(record, action),
                process=pending.process,
                stdout=pending.stdout,
                stderr=pending.stderr,
                started_at=time.time(),
                secrets=secrets,
                on_finish=on_finish,
                cancel_control_dir=cancel_control_dir,
                cancel_token_path=cancel_token_path,
            )
            with self._lock:
                self._starting_jobs -= 1
                self._jobs[job.job_id] = job
            self._set_termination_pending(
                job,
                terminal_status="failed",
                terminal_error=terminal_error,
                code="EXTENSION_JOB_TERMINATION_PENDING",
                message="Extension Job startup failed but its worker is still alive; workspace mutation protection remains held.",
            )
            return {
                "job_id": job.job_id,
                "status": "termination_pending",
                "extension_id": record.manifest.extension_id,
                "extension_action": action.name,
                "timeout_seconds": job.timeout_seconds,
            }
        except Exception:
            with self._lock:
                self._starting_jobs -= 1
            self._cleanup_cancel_control_paths(cancel_control_dir, cancel_token_path)
            self._call_finish(on_finish)
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
        try:
            monitor.start()
        except Exception as exc:
            self._signal_cancel(job)
            if not _wait_for_process_exit_without_kill(job.process, MAX_EXTENSION_JOB_CANCEL_GRACE_SECONDS):
                terminate_owned_process_tree(job.process)
            if _wait_for_process_exit(job.process, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
                self._cleanup_cancel_control(job)
                with self._lock:
                    self._jobs.pop(job.job_id, None)
                self._notify_finish(job)
                raise
            monitor_error = {
                "code": "EXTENSION_JOB_MONITOR_START_FAILED",
                "message": f"Could not start the Extension Job monitor: {type(exc).__name__}",
            }
            self._set_termination_pending(
                job,
                terminal_status="failed",
                terminal_error=monitor_error,
                code="EXTENSION_JOB_TERMINATION_PENDING",
                message="The Extension Job monitor failed to start and the worker is still alive; workspace mutation protection remains held.",
            )
            return {
                "job_id": job.job_id,
                "status": "termination_pending",
                "extension_id": record.manifest.extension_id,
                "extension_action": action.name,
                "timeout_seconds": job.timeout_seconds,
            }
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
                self._signal_cancel(job)
                if not _wait_for_process_exit_without_kill(job.process, MAX_EXTENSION_JOB_CANCEL_GRACE_SECONDS):
                    terminate_owned_process_tree(job.process)
                if not _wait_for_process_exit(job.process, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
                    timeout_error = {
                        "code": "EXTENSION_JOB_TIMEOUT",
                        "message": f"Extension job exceeded {job.timeout_seconds} seconds.",
                    }
                    self._set_termination_pending(
                        job,
                        terminal_status="timed_out",
                        terminal_error=timeout_error,
                        code="EXTENSION_JOB_TERMINATION_PENDING",
                        message="Extension Job timed out but the worker process is still alive; workspace mutation protection remains held.",
                    )
                    return
                exit_code = job.process.poll()
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
            if job.process.poll() is None:
                terminate_owned_process_tree(job.process)
                if not _wait_for_process_exit(job.process, MAX_EXTENSION_JOB_SHUTDOWN_SECONDS):
                    internal_error = {
                        "code": "EXTENSION_JOB_INTERNAL_ERROR",
                        "message": _redact_secrets(f"{type(exc).__name__}: {exc}", job.secrets),
                    }
                    self._set_termination_pending(
                        job,
                        terminal_status="failed",
                        terminal_error=internal_error,
                        code="EXTENSION_JOB_TERMINATION_PENDING",
                        message=_redact_secrets(
                            f"Extension Job monitor failed and the worker is still alive: {type(exc).__name__}: {exc}",
                            job.secrets,
                        ),
                    )
                    return
            exit_code = job.process.poll()
            status = "failed"
            result = None
            error = {
                "code": "EXTENSION_JOB_INTERNAL_ERROR",
                "message": _redact_secrets(f"{type(exc).__name__}: {exc}", job.secrets),
            }
        # A terminal status must not become visible while the worker still owns
        # a cross-thread workspace mutation lease. The process has exited by
        # this point, so clean up host control state and release before publishing.
        self._cleanup_cancel_control(job)
        self._notify_finish(job)
        with self._lock:
            job.status = status
            job.exit_code = exit_code
            job.result = result
            job.error = error
            job.finished_at = time.time()
            self._prune_finished_locked()

    def _set_termination_pending(
        self,
        job: _ExtensionJob,
        *,
        terminal_status: str,
        terminal_error: dict[str, Any] | None,
        code: str,
        message: str,
    ) -> None:
        with self._lock:
            job.status = "termination_pending"
            job.exit_code = None
            job.result = None
            job.error = {"code": code, "message": message}
            job.finished_at = None
            job.pending_terminal_status = terminal_status
            job.pending_terminal_error = terminal_error
            job.reconcile_in_progress = False
            start_reaper = not job.pending_reaper_started
            if start_reaper:
                job.pending_reaper_started = True
        if start_reaper:
            monitor = threading.Thread(
                target=self._monitor_pending_job,
                args=(job,),
                name=f"folderbridge-extension-pending-{job.job_id[:8]}",
                daemon=True,
            )
            try:
                monitor.start()
            except Exception:
                with self._lock:
                    job.pending_reaper_started = False

    def _monitor_pending_job(self, job: _ExtensionJob) -> None:
        try:
            job.process.wait()
        except Exception:
            return
        self._reconcile_pending(job)

    def _reconcile_pending(self, job: _ExtensionJob) -> bool:
        """Publish a pending terminal state only after the worker is confirmed dead."""
        with self._lock:
            if job.status != "termination_pending" or job.reconcile_in_progress:
                return False
            exit_code = job.process.poll()
            if exit_code is None:
                return False
            job.reconcile_in_progress = True

        # The process is already dead, so bounded stream cleanup cannot reopen a
        # workspace-mutation race. Cleanup diagnostics must not prevent lease
        # release or leave the job permanently stuck in reconciliation.
        try:
            try:
                _close_worker_streams(job.process, job.stdout, job.stderr)
            except Exception:
                pass
            self._cleanup_cancel_control(job)
            self._notify_finish(job)
            with self._lock:
                if job.cancel_requested:
                    final_status = "cancelled"
                    final_error = None
                else:
                    final_status = job.pending_terminal_status or "failed"
                    final_error = job.pending_terminal_error or job.error
                job.status = final_status
                job.exit_code = exit_code
                job.result = None
                job.error = final_error
                job.finished_at = time.time()
                job.pending_terminal_status = None
                job.pending_terminal_error = None
                job.reconcile_in_progress = False
                self._prune_finished_locked()
            return True
        finally:
            with self._lock:
                if job.status == "termination_pending":
                    job.reconcile_in_progress = False

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
        self._reconcile_pending(job)
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
        self._reconcile_pending(job)
        with self._lock:
            if job.status not in ACTIVE_EXTENSION_JOB_STATUSES:
                return {"job_id": job.job_id, "status": job.status, "already_finished": True}
            was_pending = job.status == "termination_pending"
            job.cancel_requested = True
            if was_pending:
                job.pending_terminal_status = "cancelled"
                job.pending_terminal_error = None
        self._signal_cancel(job)
        self._ensure_cancel_reaper(job)
        if was_pending:
            return {"job_id": job.job_id, "status": "termination_pending", "cancel_requested": True}
        return {"job_id": job.job_id, "status": "cancelling"}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            running = [job for job in self._jobs.values() if job.status in ACTIVE_EXTENSION_JOB_STATUSES]
            foreground = list(self._foreground_running.values())
            for job in running:
                job.cancel_requested = True
        for job in running:
            self._signal_cancel(job)
        # Foreground request threads must be released immediately even if an OS
        # process refuses termination. Process ownership and mutation leases stay
        # with the monitor until actual exit.
        for worker in foreground:
            self.request_foreground_shutdown(worker)
            terminate_owned_process_tree(worker.process)
            exit_code = worker.process.poll()
            if exit_code is not None:
                self._finalize_foreground(worker, exit_code)
        cancel_deadline = time.monotonic() + MAX_EXTENSION_JOB_CANCEL_GRACE_SECONDS
        while time.monotonic() < cancel_deadline and any(job.process.poll() is None for job in running):
            time.sleep(0.05)
        for job in running:
            if job.process.poll() is None:
                terminate_owned_process_tree(job.process)
        deadline = time.monotonic() + MAX_EXTENSION_JOB_SHUTDOWN_SECONDS
        for job in running:
            remaining = max(0.0, deadline - time.monotonic())
            if not _wait_for_process_exit(job.process, remaining):
                continue
            if job.status == "termination_pending":
                with self._lock:
                    job.pending_terminal_status = "cancelled"
                    job.pending_terminal_error = None
                self._reconcile_pending(job)
            else:
                # A live monitor will publish the final status; only release the
                # lease here after process death so queued workspace mutations
                # can drain during shutdown even if monitor cleanup lags.
                self._cleanup_cancel_control(job)
                self._notify_finish(job)


def _wait_for_process_exit_without_kill(process: subprocess.Popen[bytes], timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if process.poll() is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return process.poll() is not None
        time.sleep(min(0.05, remaining))


def _wait_for_process_exit(process: subprocess.Popen[bytes], timeout: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return process.poll() is not None
        return True


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
