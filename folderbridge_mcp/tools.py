from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import ProjectConfig, canonical_workspaces, config_is_trusted, load_config, workspace_id
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
    ) -> None:
        self._configure((self._make_target(root, config),), read_only=read_only, allow_tasks=allow_tasks)

    @classmethod
    def from_roots(
        cls,
        roots: tuple[Path, ...] | list[Path],
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
    ) -> ToolRuntime:
        canonical = canonical_workspaces(list(roots))
        runtime = cls.__new__(cls)
        targets = tuple(
            cls._make_target(root, load_config(root, required=allow_tasks))
            for root in canonical
        )
        runtime._configure(targets, read_only=read_only, allow_tasks=allow_tasks)
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

    @property
    def identity(self) -> dict[str, str]:
        return {"name": "folderbridge", "title": "FolderBridge MCP", "version": __version__}

    @property
    def instructions(self) -> str:
        mode = "read-only" if self.read_only else "read/write"
        task_note = "approved named tasks are enabled" if self.allow_tasks else "task execution is disabled"
        selection_note = (
            "Call server_info first and pass workspace_id with every workspace-specific tool call. "
            if len(self._targets) > 1
            else ""
        )
        return (
            f"This server exposes {len(self._targets)} explicitly selected local {mode} workspace(s). {selection_note}"
            f"Use workspace(read) before edit_file so edits carry "
            f"the current SHA-256. Credential-like files, links, dependencies, and VCS internals are hidden. "
            f"Exact replacements are atomic; arbitrary shell commands are unavailable; {task_note}."
        )

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [SERVER_INFO_TOOL, WORKSPACE_TOOL]
        if not self.read_only:
            tools.append(EDIT_FILE_TOOL)
        if self.allow_tasks:
            tools.append(RUN_TASK_TOOL)
        rendered = deepcopy(tools)
        if len(self._targets) > 1:
            for tool in rendered:
                if tool["name"] in {"workspace", "edit_file", "run_task"}:
                    tool["inputSchema"]["required"] = [
                        "workspace_id",
                        *tool["inputSchema"].get("required", []),
                    ]
        return rendered

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "server_info": self._server_info,
            "workspace": self._workspace,
        }
        if not self.read_only:
            handlers["edit_file"] = self._edit_file
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
            "security": {
                "workspace_confined": True,
                "workspace_selector_required_when_multiple": True,
                "overlapping_roots_denied": True,
                "links_denied": True,
                "sensitive_names_denied": True,
                "edit_requires_sha256": True,
                "config_protected_from_mcp": True,
                "task_warning": "Approved tasks execute repository code with the current OS user's permissions.",
            },
        }
        if len(summaries) == 1:
            result.update(summaries[0])
            result["workspace"] = summaries[0]["name"]
        return result

    def _target_summary(self, target: _WorkspaceTarget) -> dict[str, Any]:
        return {
            "name": target.root.name,
            "workspace_id": target.workspace_id,
            "task_config_trusted": config_is_trusted(target.root, target.config),
            "tasks": [
                {"name": task.name, "timeout_seconds": task.timeout_seconds}
                for task in target.config.tasks.values()
            ],
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
    content = _human_content(payload)
    return {
        "content": [{"type": "text", "text": content}],
        "structuredContent": {"ok": True, **payload},
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
