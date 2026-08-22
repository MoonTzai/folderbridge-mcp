from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .config import workspace_id
from .security import ToolError, Workspace, clean_environment


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
EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXECUTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}(?:\.exe|\.cmd|\.bat|\.com)?$", re.IGNORECASE)
LOOPBACK_PERMISSION_RE = re.compile(r"^network\.loopback:(?:127\.0\.0\.1|localhost):([1-9][0-9]{0,4})$")
PROCESS_PERMISSION_RE = re.compile(r"^process\.execute:([A-Za-z0-9][A-Za-z0-9._+-]{0,127}(?:\.exe|\.cmd|\.bat|\.com)?)$", re.IGNORECASE)
EXACT_PERMISSIONS = {
    "workspace.read",
    "workspace.write",
    "workspace.adapter",
    "extension.state",
    "git.push-current-branch",
}


@dataclass(frozen=True)
class ExtensionAction:
    name: str
    read_only: bool
    requires_workspace: bool
    authorization: str
    input_schema: dict[str, Any]


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

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if self.path.is_symlink() or _is_reparse_point(self.path):
                return {}
            data = self.path.read_bytes()
            if len(data) > MAX_MANIFEST_BYTES:
                return {}
            parsed = json.loads(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict) or parsed.get("version") != TRUST_STORE_VERSION:
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
        records = self._load()
        records[record.manifest.extension_id] = {
            "sha256": record.sha256,
            "permissions": list(record.manifest.permissions),
            "enabled": bool(enabled),
        }
        self._save(records)

    def set_enabled(self, record: ExtensionRecord, enabled: bool) -> None:
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
        return _run_worker(record, action, params, workspace=workspace, read_only=read_only)


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


def load_extension(path: Path, *, bundled: bool) -> ExtensionRecord:
    if path.is_symlink() or _is_reparse_point(path):
        raise ValueError("extension directory may not be a link or reparse point")
    root = path.resolve(strict=True)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink() or _is_reparse_point(manifest_path):
        raise ValueError(f"missing regular {MANIFEST_NAME}")
    data = manifest_path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("extension manifest is too large")
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("extension manifest must be UTF-8 JSON") from exc
    manifest = _parse_manifest(raw, root)
    if not bundled and any(action.authorization == "none" for action in manifest.actions.values()):
        raise ValueError("external extensions may not declare authorization=none")
    digest = _hash_extension(root)
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
    if raw.get("schema_version") != EXTENSION_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {EXTENSION_SCHEMA_VERSION}")
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
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ValueError("execution.timeout_seconds must be 1..600")

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
        if set(spec).difference({"read_only", "requires_workspace", "authorization", "input_schema"}):
            raise ValueError(f"action {action_name} has unknown fields")
        read_only = spec.get("read_only")
        requires_workspace = spec.get("requires_workspace", True)
        authorization = spec.get("authorization", "global")
        schema = spec.get("input_schema", {"type": "object", "properties": {}, "additionalProperties": False})
        if not isinstance(read_only, bool) or not isinstance(requires_workspace, bool):
            raise ValueError(f"action {action_name} read_only/requires_workspace must be boolean")
        if authorization not in {"none", "global"}:
            raise ValueError(f"action {action_name} authorization must be none or global")
        if authorization == "none" and not read_only:
            raise ValueError(f"action {action_name} may use authorization=none only when read_only=true")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"action {action_name} input_schema must be an object schema")
        actions[action_name] = ExtensionAction(
            action_name,
            read_only,
            requires_workspace,
            authorization,
            schema,
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
        if not isinstance(enum, list) or value not in enum:
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


def _run_worker(
    record: ExtensionRecord,
    action: ExtensionAction,
    params: dict[str, Any],
    *,
    workspace: Workspace | None,
    read_only: bool,
) -> dict[str, Any]:
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
    context = {
        "extension_id": record.manifest.extension_id,
        "extension_version": record.manifest.version,
        "permissions": list(record.manifest.permissions),
        "workspace_root": str(workspace_root) if workspace_root is not None else None,
        "workspace_read_only": bool(read_only),
        "state_dir": state_dir,
        "workspace_adapter": record.manifest.workspace_adapter,
    }
    request = json.dumps(
        {"action": action.name, "params": params, "context": context},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > MAX_WORKER_REQUEST_BYTES:
        raise ToolError("EXTENSION_REQUEST_TOO_LARGE", "Extension request is too large.")
    argv = _worker_argv(record)
    env_root = workspace_root or record.path
    env = clean_environment(env_root)
    env["PYTHONUTF8"] = "1"
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = subprocess.Popen(
        argv,
        cwd=record.path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout = _BoundedCapture(process.stdout, MAX_WORKER_RESPONSE_BYTES)
    stderr = _BoundedCapture(process.stderr, MAX_WORKER_LOG_BYTES)
    stdout.start()
    stderr.start()
    try:
        process.stdin.write(request)
        process.stdin.close()
        try:
            exit_code = process.wait(timeout=record.manifest.execution_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise ToolError(
                "EXTENSION_TIMEOUT",
                f"Extension exceeded {record.manifest.execution_timeout_seconds} seconds.",
                extension_id=record.manifest.extension_id,
                action=action.name,
            )
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
    stdout.join(timeout=5)
    stderr.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    if stdout.truncated:
        raise ToolError("EXTENSION_RESPONSE_TOO_LARGE", "Extension response exceeded the protocol limit.")
    stderr_text = bytes(stderr.data).decode("utf-8", errors="replace").strip()
    try:
        envelope = json.loads(bytes(stdout.data))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("EXTENSION_PROTOCOL_ERROR", "Extension worker returned invalid JSON.", stderr=stderr_text[:4000]) from exc
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
    result.setdefault("extension_id", record.manifest.extension_id)
    result.setdefault("extension_action", action.name)
    if stderr_text:
        result.setdefault("extension_log", stderr_text[:4000])
    return result


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


def _hash_extension(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    total = 0
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
        data = path.read_bytes()
        total += len(data)
        if total > MAX_EXTENSION_BYTES:
            raise ValueError(f"extension exceeds {MAX_EXTENSION_BYTES} bytes")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _config_base() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "folderbridge-mcp"
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "folderbridge-mcp"
    return Path.home() / ".config" / "folderbridge-mcp"


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
