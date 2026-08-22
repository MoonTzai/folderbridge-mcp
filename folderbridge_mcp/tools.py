from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .binary_tools import file_info, inspect_pptx, open_image
from .capabilities import (
    EXECUTION_CAPABILITY_NAMES,
    discover_capabilities,
    normalize_capability_names,
    run_capability,
)
from .config import ProjectConfig, canonical_workspaces, config_is_trusted, load_config, workspace_id
from .extensions import ExtensionRegistry
from .security import ToolError, Workspace
from .task_runner import run_task


@dataclass(frozen=True)
class _WorkspaceTarget:
    root: Path
    workspace_id: str
    workspace: Workspace
    config: ProjectConfig


class ToolRuntime:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
        capabilities: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._configure(
            (self._make_target(root, config),),
            read_only=read_only,
            allow_tasks=allow_tasks,
            capabilities=capabilities,
        )

    @classmethod
    def from_roots(
        cls,
        roots: tuple[Path, ...] | list[Path],
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
        capabilities: tuple[str, ...] | list[str] = (),
    ) -> ToolRuntime:
        canonical = canonical_workspaces(list(roots))
        runtime = cls.__new__(cls)
        targets = tuple(
            cls._make_target(root, load_config(root, required=False))
            for root in canonical
        )
        runtime._configure(
            targets,
            read_only=read_only,
            allow_tasks=allow_tasks,
            capabilities=capabilities,
        )
        return runtime

    @staticmethod
    def _make_target(root: Path, config: ProjectConfig) -> _WorkspaceTarget:
        resolved = root.resolve(strict=True)
        return _WorkspaceTarget(resolved, workspace_id(resolved), Workspace(resolved), config)

    def _configure(
        self,
        targets: tuple[_WorkspaceTarget, ...],
        *,
        read_only: bool,
        allow_tasks: bool,
        capabilities: tuple[str, ...] | list[str],
    ) -> None:
        if not targets:
            raise ValueError("ToolRuntime needs at least one workspace")
        target_ids = [target.workspace_id for target in targets]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("Workspace ID collision")
        self._targets = targets
        self._targets_by_id = {target.workspace_id: target for target in targets}
        # Preserve the original single-workspace attributes for existing local callers.
        self.root = targets[0].root
        self.workspace = targets[0].workspace
        self.config = targets[0].config
        self.read_only = read_only
        self.allow_tasks = allow_tasks
        self.capabilities = normalize_capability_names(capabilities)
        self.execution_capabilities = tuple(
            name for name in self.capabilities if name in EXECUTION_CAPABILITY_NAMES
        )
        self.extensions = ExtensionRegistry()

    @property
    def identity(self) -> dict[str, str]:
        return {"name": "folderbridge", "title": "FolderBridge MCP", "version": __version__}

    @property
    def instructions(self) -> str:
        mode = "read-only" if self.read_only else "read/write"
        task_note = "approved named tasks are enabled" if self.allow_tasks else "custom task execution is disabled"
        capability_note = (
            f"global capabilities enabled: {', '.join(self.capabilities)}" if self.capabilities else "global capabilities are disabled"
        )
        selection_note = (
            "Call server_info first and pass workspace_id with every workspace-specific tool call. "
            if len(self._targets) > 1
            else ""
        )
        return (
            f"This server exposes {len(self._targets)} explicitly selected local {mode} workspace(s). {selection_note}"
            f"Use workspace(read) before edit_file so edits carry "
            f"the current SHA-256. Credential-like files, links, dependencies, and VCS internals are hidden. "
            f"Exact replacements are atomic; arbitrary shell commands are unavailable; {task_note}; {capability_note}."
        )

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [SERVER_INFO_TOOL, WORKSPACE_TOOL, FILE_INFO_TOOL, PPTX_INSPECT_TOOL, IMAGE_OPEN_TOOL, EXTENSION_TOOL]
        if not self.read_only:
            tools.append(EDIT_FILE_TOOL)
        if self.execution_capabilities:
            tools.append(RUN_CAPABILITY_TOOL)
        if self.allow_tasks:
            tools.append(RUN_TASK_TOOL)
        rendered = deepcopy(tools)
        for tool in rendered:
            if tool["name"] == "run_capability":
                tool["inputSchema"]["properties"]["name"]["enum"] = list(self.execution_capabilities)
        if len(self._targets) > 1:
            for tool in rendered:
                if tool["name"] in {"workspace", "file_info", "pptx_inspect", "image_open", "edit_file", "run_capability", "run_task"}:
                    tool["inputSchema"]["required"] = [
                        "workspace_id",
                        *tool["inputSchema"].get("required", []),
                    ]
        return rendered

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "server_info": self._server_info,
            "workspace": self._workspace,
            "file_info": self._file_info,
            "pptx_inspect": self._pptx_inspect,
            "image_open": self._image_open,
            "extension": self._extension,
        }
        if not self.read_only:
            handlers["edit_file"] = self._edit_file
        if self.execution_capabilities:
            handlers["run_capability"] = self._run_capability
        if self.allow_tasks:
            handlers["run_task"] = self._run_task
        handler = handlers.get(name)
        if handler is None:
            return error_result("UNKNOWN_TOOL", f"Unknown or disabled tool: {name}")
        try:
            return success_result(handler(arguments))
        except ToolError as exc:
            return error_result(exc.code, str(exc), exc.details)
        except Exception as exc:  # keep protocol stdout clean while returning bounded diagnostics
            return error_result("INTERNAL_ERROR", f"Unexpected local tool failure: {type(exc).__name__}")

    def _server_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, set())
        summaries = [self._target_summary(target) for target in self._targets]
        result: dict[str, Any] = {
            "workspace_count": len(summaries),
            "workspaces": summaries,
            "mode": "read-only" if self.read_only else "read/write",
            "network_listener": False,
            "telemetry": False,
            "arbitrary_shell": False,
            "task_execution_enabled": self.allow_tasks,
            "global_capabilities_enabled": list(self.capabilities),
            "builtin_tools": ["workspace", "file_info", "pptx_inspect", "image_open", "extension"],
            "extensions": self._extension_summary(None),
            "security": {
                "workspace_confined": True,
                "workspace_selector_required_when_multiple": True,
                "overlapping_roots_denied": True,
                "links_denied": True,
                "sensitive_names_denied": True,
                "edit_requires_sha256": True,
                "config_protected_from_mcp": True,
                "task_warning": "Approved tasks and build/package capabilities execute repository code with the current OS user's permissions.",
                "git_push_policy": "GitHub HTTPS origin only; current branch only; no force push; local pre-push hook bypassed.",
                "extension_policy": "Extensions are hot-scanned, exact-hash approved, permission-declared, and executed out of process with a cleaned environment and bounded I/O. External plugin code is not an OS sandbox; use a VM/container for untrusted code.",
            },
        }
        if len(summaries) == 1:
            result.update(summaries[0])
            result["workspace"] = summaries[0]["name"]
        return result

    def _target_summary(self, target: _WorkspaceTarget) -> dict[str, Any]:
        discovered = discover_capabilities(target.root)
        return {
            "name": target.root.name,
            "workspace_id": target.workspace_id,
            "task_config_trusted": config_is_trusted(target.root, target.config),
            "tasks": [
                {"name": task.name, "timeout_seconds": task.timeout_seconds}
                for task in target.config.tasks.values()
            ],
            "capabilities": [
                {
                    "name": name,
                    "available": name in discovered,
                    **({"source": discovered[name]["source"]} if name in discovered else {}),
                }
                for name in self.capabilities
            ],
            "extensions": self._extension_summary(target.root),
        }

    def _extension_summary(self, workspace: Path | None) -> dict[str, Any]:
        description = self.extensions.describe(workspace)
        return {
            "installed": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "version": item["version"],
                    "bundled": item["bundled"],
                    "trusted": item["trusted"],
                    "enabled": item["enabled"],
                    "loaded": item["loaded"],
                    "approval_stale": item["approval_stale"],
                    "applicable": item["applicable"],
                }
                for item in description["extensions"]
            ],
            "errors": description["errors"],
        }

    def _select_target(self, raw_workspace_id: object) -> _WorkspaceTarget:
        if raw_workspace_id is None and len(self._targets) == 1:
            return self._targets[0]
        if not isinstance(raw_workspace_id, str) or not raw_workspace_id:
            raise ToolError(
                "WORKSPACE_REQUIRED",
                "workspace_id is required when multiple workspaces are configured; call server_info first.",
                available=[
                    {"workspace_id": target.workspace_id, "name": target.root.name}
                    for target in self._targets
                ],
            )
        target = self._targets_by_id.get(raw_workspace_id)
        if target is None:
            raise ToolError(
                "UNKNOWN_WORKSPACE",
                "workspace_id is not one of the configured workspaces.",
                available=[
                    {"workspace_id": item.workspace_id, "name": item.root.name}
                    for item in self._targets
                ],
            )
        return target

    @staticmethod
    def _scope_result(payload: dict[str, Any], target: _WorkspaceTarget) -> dict[str, Any]:
        return {**payload, "workspace_id": target.workspace_id, "workspace": target.root.name}

    def _workspace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            arguments,
            {"workspace_id", "action", "path", "query", "pattern", "case_sensitive", "max_results", "offset", "limit"},
        )
        target = self._select_target(arguments.get("workspace_id"))
        action = arguments.get("action")
        path = arguments.get("path", ".")
        if not isinstance(action, str) or not isinstance(path, str):
            raise ToolError("INVALID_ARGUMENT", "action and path must be strings")
        if action == "list":
            return self._scope_result(target.workspace.list_files(
                path,
                pattern=arguments.get("pattern", "*"),
                max_results=arguments.get("max_results", 100),
            ), target)
        if action == "read":
            return self._scope_result(target.workspace.read_text(
                path,
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 64 * 1024),
            ), target)
        if action == "search":
            return self._scope_result(target.workspace.search_text(
                arguments.get("query"),
                raw=path,
                case_sensitive=arguments.get("case_sensitive", False),
                max_results=arguments.get("max_results", 100),
            ), target)
        if action in {"status", "diff"}:
            if path != ".":
                raise ToolError("INVALID_ARGUMENT", "status and diff apply to the workspace root")
            return self._scope_result(target.workspace.git_view(action), target)
        raise ToolError("INVALID_ARGUMENT", "action must be list, read, search, status, or diff")

    def _file_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "path"})
        target = self._select_target(arguments.get("workspace_id"))
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("INVALID_ARGUMENT", "path is required")
        return self._scope_result(file_info(target.workspace, path), target)

    def _pptx_inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "path", "page_start", "page_end"})
        target = self._select_target(arguments.get("workspace_id"))
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("INVALID_ARGUMENT", "path is required")
        return self._scope_result(
            inspect_pptx(
                target.workspace,
                path,
                page_start=arguments.get("page_start"),
                page_end=arguments.get("page_end"),
            ),
            target,
        )

    def _image_open(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "path", "archive_path", "member"})
        target = self._select_target(arguments.get("workspace_id"))
        path = arguments.get("path")
        archive_path = arguments.get("archive_path")
        member = arguments.get("member")
        if path is not None and not isinstance(path, str):
            raise ToolError("INVALID_ARGUMENT", "path must be a string")
        if archive_path is not None and not isinstance(archive_path, str):
            raise ToolError("INVALID_ARGUMENT", "archive_path must be a string")
        if member is not None and not isinstance(member, str):
            raise ToolError("INVALID_ARGUMENT", "member must be a string")
        return self._scope_result(
            open_image(target.workspace, raw=path, archive_raw=archive_path, member=member),
            target,
        )

    def _extension(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "action", "extension_id", "extension_action", "params"})
        operation = arguments.get("action")
        if operation not in {"list", "info", "run"}:
            raise ToolError("INVALID_ARGUMENT", "action must be list, info, or run")
        raw_workspace_id = arguments.get("workspace_id")
        target = self._select_target(raw_workspace_id) if raw_workspace_id is not None else None

        if operation == "list":
            return self.extensions.describe(target.root if target is not None else None)

        extension_id = arguments.get("extension_id")
        if not isinstance(extension_id, str) or not extension_id:
            raise ToolError("INVALID_ARGUMENT", "extension_id is required for info/run")
        if operation == "info":
            description = self.extensions.describe(target.root if target is not None else None)
            item = next((item for item in description["extensions"] if item.get("id") == extension_id), None)
            if item is None:
                raise ToolError("EXTENSION_NOT_FOUND", "Extension is not installed.", extension_id=extension_id)
            return {"extension": item, "errors": description.get("errors", [])}

        extension_action = arguments.get("extension_action")
        params = arguments.get("params", {})
        if not isinstance(extension_action, str) or not extension_action:
            raise ToolError("INVALID_ARGUMENT", "extension_action is required for run")
        if not isinstance(params, dict):
            raise ToolError("INVALID_ARGUMENT", "params must be an object")
        record = self.extensions.get(extension_id)
        action_spec = record.manifest.actions.get(extension_action)
        if action_spec is None:
            raise ToolError(
                "EXTENSION_ACTION_NOT_FOUND",
                "Extension action does not exist.",
                extension_id=extension_id,
                available=sorted(record.manifest.actions),
            )
        if target is None and action_spec.requires_workspace:
            target = self._select_target(None)
        result = self.extensions.run(
            extension_id,
            extension_action,
            params,
            workspace=target.workspace if target is not None else None,
            read_only=self.read_only,
        )
        return self._scope_result(result, target) if target is not None else result

    def _edit_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "path", "expected_sha256", "replacements", "create_content"})
        target = self._select_target(arguments.get("workspace_id"))
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("INVALID_ARGUMENT", "path is required")
        return self._scope_result(target.workspace.edit_file(
            path,
            expected_sha256=arguments.get("expected_sha256"),
            replacements=arguments.get("replacements"),
            create_content=arguments.get("create_content"),
        ), target)

    def _run_capability(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "name"})
        target = self._select_target(arguments.get("workspace_id"))
        name = arguments.get("name")
        if not isinstance(name, str):
            raise ToolError("INVALID_ARGUMENT", "name is required")
        if name not in self.execution_capabilities:
            raise ToolError(
                "CAPABILITY_NOT_AUTHORIZED",
                "This execution capability is not globally pre-authorized in the FolderBridge launcher.",
                enabled=list(self.execution_capabilities),
            )
        return self._scope_result(run_capability(target.root, name), target)

    def _run_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"workspace_id", "name"})
        target = self._select_target(arguments.get("workspace_id"))
        name = arguments.get("name")
        if not isinstance(name, str):
            raise ToolError("INVALID_ARGUMENT", "name is required")
        if not config_is_trusted(target.root, target.config):
            raise ToolError(
                "CONFIG_NOT_TRUSTED",
                "Task config is new or changed; inspect it locally and run the approve command.",
            )
        task = target.config.tasks.get(name)
        if task is None:
            raise ToolError("UNKNOWN_TASK", "Only locally approved named tasks can run.", available=sorted(target.config.tasks))
        return self._scope_result(run_task(target.root, task), target)


def _require_only(arguments: dict[str, Any], allowed: set[str]) -> None:
    if not isinstance(arguments, dict):
        raise ToolError("INVALID_ARGUMENT", "arguments must be an object")
    unknown = sorted(set(arguments).difference(allowed))
    if unknown:
        raise ToolError("INVALID_ARGUMENT", f"Unknown arguments: {', '.join(unknown)}")


def success_result(payload: dict[str, Any]) -> dict[str, Any]:
    content_override = payload.get("_content")
    structured_payload = {key: value for key, value in payload.items() if key != "_content"}
    if isinstance(content_override, list) and content_override:
        content = content_override
    else:
        content = [{"type": "text", "text": _human_content(structured_payload)}]
    return {
        "content": content,
        "structuredContent": {"ok": True, **structured_payload},
        "isError": False,
    }


def error_result(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    structured = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        structured["error"]["details"] = details
    return {
        "content": [{"type": "text", "text": f"{code}: {message}"}],
        "structuredContent": structured,
        "isError": True,
    }


def _human_content(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("text"), str):
        header = {
            "workspace": payload.get("workspace"),
            "workspace_id": payload.get("workspace_id"),
            "path": payload.get("path"),
            "sha256": payload.get("sha256"),
            "size": payload.get("size"),
            "truncated": payload.get("truncated"),
            "next_offset": payload.get("next_offset"),
        }
        return json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n\n" + payload["text"]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if len(text) <= 16 * 1024:
        return text
    return text[: 16 * 1024] + "\n... model-facing preview truncated; use structuredContent ..."


SERVER_INFO_TOOL = {
    "name": "server_info",
    "title": "Local workspace safety status",
    "description": "List the fixed workspace IDs, enabled capabilities, approved tasks, and safety boundary.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

WORKSPACE_TOOL = {
    "name": "workspace",
    "title": "Inspect the local workspace",
    "description": (
        "List files, read UTF-8 text, perform bounded literal search, or view git status/diff. "
        "When multiple workspaces are configured, pass a workspace_id returned by server_info. "
        "All paths are relative; links, credentials, dependencies, and VCS internals are denied."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "action": {"type": "string", "enum": ["list", "read", "search", "status", "diff"]},
            "path": {"type": "string", "default": "."},
            "query": {"type": "string", "description": "Required for literal search."},
            "pattern": {"type": "string", "default": "*", "description": "Glob for list."},
            "case_sensitive": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 262144, "default": 65536},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

FILE_INFO_TOOL = {
    "name": "file_info",
    "title": "Inspect binary file metadata",
    "description": (
        "Return bounded metadata and SHA-256 for a regular file without exposing its bytes. "
        "Uses the same workspace confinement, link denial, and sensitive-path policy as text tools."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "path": {"type": "string"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

PPTX_INSPECT_TOOL = {
    "name": "pptx_inspect",
    "title": "Inspect PPTX OOXML and SmartArt",
    "description": (
        "Safely inspect a PPTX inside the selected workspace using Python standard-library ZIP/XML parsing. "
        "Returns slide text, diagram-to-slide mappings, dgm:pt points, dgm:cxn connections, and orphan/duplicate diagnostics. "
        "Does not execute macros, embedded objects, or workspace code."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "path": {"type": "string"},
            "page_start": {"type": "integer", "minimum": 1},
            "page_end": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

IMAGE_OPEN_TOOL = {
    "name": "image_open",
    "title": "Open a workspace image or ZIP image member",
    "description": (
        "Return one bounded PNG/JPEG/GIF/WebP as MCP image content, either from a direct workspace path "
        "or from one exact member inside a workspace ZIP. The archive is never extracted to disk."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "path": {"type": "string", "description": "Direct image path. Mutually exclusive with archive_path."},
            "archive_path": {"type": "string", "description": "ZIP path containing the image. Mutually exclusive with path."},
            "member": {"type": "string", "description": "Exact POSIX member path inside archive_path."},
        },
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

EXTENSION_TOOL = {
    "name": "extension",
    "title": "Use FolderBridge extensions",
    "description": (
        "Stable extension gateway. action=list discovers installed/hot-reloaded extensions and their action schemas; "
        "action=info inspects one extension; action=run invokes one declared extension action. "
        "Installing more extensions does not add MCP tool names. External extension code requires exact-hash local approval; "
        "globally authorized actions must also be enabled in the FolderBridge extension sidebar. "
        "Dynamic workspace adapters are re-evaluated at call time, so later project changes do not require workspace task injection."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Optional for list/info; required by run actions whose manifest requires a workspace when multiple workspaces are configured."},
            "action": {"type": "string", "enum": ["list", "info", "run"]},
            "extension_id": {"type": "string", "description": "Required for info/run."},
            "extension_action": {"type": "string", "description": "Required for run; choose an action declared by extension info/list."},
            "params": {"type": "object", "description": "Action parameters validated against the extension's declared input_schema."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
}

EDIT_FILE_TOOL = {
    "name": "edit_file",
    "title": "Create or exactly edit one file",
    "description": (
        "Atomically create a UTF-8 file, or edit one using unique exact replacements and the SHA-256 returned by workspace(read). "
        "When multiple workspaces are configured, pass the same workspace_id used for the read. "
        "Cannot delete files, edit the local task config, follow links, or access credential-like paths."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "path": {"type": "string"},
            "expected_sha256": {"type": "string", "description": "Required when editing an existing file."},
            "replacements": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string", "minLength": 1}, "new": {"type": "string"}},
                    "required": ["old", "new"],
                    "additionalProperties": False,
                },
            },
            "create_content": {"type": "string", "description": "Used only when creating a new file."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
}

RUN_CAPABILITY_TOOL = {
    "name": "run_capability",
    "title": "Run a globally pre-authorized capability",
    "description": (
        "Run one capability that the user enabled once in the FolderBridge launcher. "
        "Availability is detected from the selected workspace at call time, so a capability can become usable later without editing workspace config. "
        "Build/package capabilities may execute repository code. git-push is restricted to a GitHub HTTPS origin and the current branch, without force push."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "name": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
}

RUN_TASK_TOOL = {
    "name": "run_task",
    "title": "Run a locally approved task",
    "description": (
        "Run one exact, locally approved task by name. No command text or arguments come from MCP. "
        "Repository code still executes with the current OS user's permissions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string", "description": "Required when server_info lists multiple workspaces."},
            "name": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
}
