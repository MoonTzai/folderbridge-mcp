from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import ProjectConfig, config_is_trusted
from .security import ToolError, Workspace
from .task_runner import run_task


class ToolRuntime:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        *,
        read_only: bool = False,
        allow_tasks: bool = False,
    ) -> None:
        self.root = root
        self.workspace = Workspace(root)
        self.config = config
        self.read_only = read_only
        self.allow_tasks = allow_tasks

    @property
    def identity(self) -> dict[str, str]:
        return {"name": "folderbridge", "title": "FolderBridge MCP", "version": __version__}

    @property
    def instructions(self) -> str:
        mode = "read-only" if self.read_only else "read/write"
        task_note = "approved named tasks are enabled" if self.allow_tasks else "task execution is disabled"
        return (
            f"This is a local {mode} coding workspace. Use workspace(read) before edit_file so edits carry "
            f"the current SHA-256. Credential-like files, links, dependencies, and VCS internals are hidden. "
            f"Exact replacements are atomic; arbitrary shell commands are unavailable; {task_note}."
        )

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [SERVER_INFO_TOOL, WORKSPACE_TOOL]
        if not self.read_only:
            tools.append(EDIT_FILE_TOOL)
        if self.allow_tasks:
            tools.append(RUN_TASK_TOOL)
        return tools

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
        trusted = config_is_trusted(self.root, self.config)
        return {
            "workspace": self.root.name,
            "workspace_id": hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12],
            "mode": "read-only" if self.read_only else "read/write",
            "network_listener": False,
            "telemetry": False,
            "arbitrary_shell": False,
            "task_execution_enabled": self.allow_tasks,
            "task_config_trusted": trusted,
            "tasks": [
                {"name": task.name, "timeout_seconds": task.timeout_seconds}
                for task in self.config.tasks.values()
            ],
            "security": {
                "workspace_confined": True,
                "links_denied": True,
                "sensitive_names_denied": True,
                "edit_requires_sha256": True,
                "config_protected_from_mcp": True,
                "task_warning": "Approved tasks execute repository code with the current OS user's permissions.",
            },
        }

    def _workspace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            arguments,
            {"action", "path", "query", "pattern", "case_sensitive", "max_results", "offset", "limit"},
        )
        action = arguments.get("action")
        path = arguments.get("path", ".")
        if not isinstance(action, str) or not isinstance(path, str):
            raise ToolError("INVALID_ARGUMENT", "action and path must be strings")
        if action == "list":
            return self.workspace.list_files(
                path,
                pattern=arguments.get("pattern", "*"),
                max_results=arguments.get("max_results", 100),
            )
        if action == "read":
            return self.workspace.read_text(
                path,
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 64 * 1024),
            )
        if action == "search":
            return self.workspace.search_text(
                arguments.get("query"),
                raw=path,
                case_sensitive=arguments.get("case_sensitive", False),
                max_results=arguments.get("max_results", 100),
            )
        if action in {"status", "diff"}:
            if path != ".":
                raise ToolError("INVALID_ARGUMENT", "status and diff apply to the workspace root")
            return self.workspace.git_view(action)
        raise ToolError("INVALID_ARGUMENT", "action must be list, read, search, status, or diff")

    def _edit_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"path", "expected_sha256", "replacements", "create_content"})
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("INVALID_ARGUMENT", "path is required")
        return self.workspace.edit_file(
            path,
            expected_sha256=arguments.get("expected_sha256"),
            replacements=arguments.get("replacements"),
            create_content=arguments.get("create_content"),
        )

    def _run_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_only(arguments, {"name"})
        name = arguments.get("name")
        if not isinstance(name, str):
            raise ToolError("INVALID_ARGUMENT", "name is required")
        if not config_is_trusted(self.root, self.config):
            raise ToolError(
                "CONFIG_NOT_TRUSTED",
                "Task config is new or changed; inspect it locally and run the approve command.",
            )
        task = self.config.tasks.get(name)
        if task is None:
            raise ToolError("UNKNOWN_TASK", "Only locally approved named tasks can run.", available=sorted(self.config.tasks))
        return run_task(self.root, task)


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
    "description": "Show the fixed workspace, enabled capabilities, approved tasks, and safety boundary.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}

WORKSPACE_TOOL = {
    "name": "workspace",
    "title": "Inspect the local workspace",
    "description": (
        "List files, read UTF-8 text, perform bounded literal search, or view git status/diff. "
        "All paths are relative; links, credentials, dependencies, and VCS internals are denied."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
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
        "Cannot delete files, edit the local task config, follow links, or access credential-like paths."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
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
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
}
