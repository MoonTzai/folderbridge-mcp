from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


MCP_URL = "http://127.0.0.1:8000/mcp"
PROTOCOL_VERSION = "2025-03-26"
DEFAULT_TIMEOUT_SECONDS = 25


class GodotMcpError(RuntimeError):
    pass


class GodotMcpClient:
    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.session_id = ""
        self.server_info: dict[str, Any] = {}
        self._request_id = 0
        self._opener = build_opener(ProxyHandler({}))

    def connect(self) -> None:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "folderbridge-godot-ai",
                    "version": "0.1.0",
                },
            },
        )
        self.server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            detail = _content_text(result.get("content")) or f"Godot AI tool {tool!r} failed"
            raise GodotMcpError(detail)
        return result

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            request = Request(MCP_URL, method="DELETE", headers=self._headers())
            self._opener.open(request, timeout=3).close()
        except Exception:
            pass

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response, headers = self._post(payload)
        session_id = headers.get("mcp-session-id", "")
        if session_id:
            self.session_id = session_id
        message = _parse_mcp_response(response, request_id)
        if "error" in message:
            error = message["error"]
            raise GodotMcpError(f"Godot AI MCP error: {error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise GodotMcpError("Godot AI returned an invalid MCP result")
        return result

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def _post(self, payload: dict[str, Any]) -> tuple[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(MCP_URL, data=body, method="POST", headers=self._headers())
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace"), response.headers
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:4096]
            raise GodotMcpError(f"Godot AI HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GodotMcpError(
                "Godot AI is unavailable at 127.0.0.1:8000. Open the configured Godot project "
                "and wait for the Godot AI dock to report Connected."
            ) from exc

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    workspace_root, project_root = _project_roots(context)
    client = GodotMcpClient()
    try:
        client.connect()
        sessions_result = client.call("session_manage", {"op": "list"})
        sessions_data = _structured(sessions_result)
        sessions = sessions_data.get("sessions", [])
        session = _matching_session(sessions, project_root)

        if action == "status":
            return {
                "ok": True,
                "server": client.server_info,
                "workspace_root": str(workspace_root),
                "project_root": str(project_root),
                "session": session,
                "session_count": len(sessions) if isinstance(sessions, list) else 0,
            }

        if session is None:
            available = [s.get("project_path", "") for s in sessions if isinstance(s, dict)]
            raise GodotMcpError(
                f"No connected Godot editor matches {project_root}. Connected projects: {available}"
            )
        session_id = str(session.get("session_id") or "")
        tool, arguments = _translate_action(action, params)
        arguments["session_id"] = session_id
        result = client.call(tool, arguments)
        output: dict[str, Any] = {
            "ok": True,
            "action": action,
            "godot_tool": tool,
            "session_id": session_id,
            "data": _structured(result),
        }
        content = _safe_content(result.get("content"))
        if action == "screenshot" and content:
            output["_content"] = content
        return output
    finally:
        client.close()


def _translate_action(action: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if action == "inspect-editor":
        return "editor_state", {}
    if action == "inspect-scene":
        return "scene_get_hierarchy", {
            "depth": int(params.get("depth", 10)),
            "offset": int(params.get("offset", 0)),
            "limit": int(params.get("limit", 100)),
        }
    if action == "inspect-node":
        arguments: dict[str, Any] = {"path": _node_path(params["path"])}
        if "fields" in params:
            arguments["fields"] = params["fields"]
        return "node_get_properties", arguments
    if action == "read-logs":
        return "logs_read", {
            "source": params.get("source", "all"),
            "count": int(params.get("count", 50)),
            "offset": int(params.get("offset", 0)),
            "include_details": bool(params.get("include_details", True)),
        }
    if action == "screenshot":
        return "editor_screenshot", {
            "source": params.get("source", "viewport_2d"),
            "max_resolution": int(params.get("max_resolution", 640)),
            "include_image": True,
            "user_prompt": str(params.get("user_prompt", "")),
        }
    if action == "open-scene":
        return "scene_open", {
            "path": _res_path(params["path"], ".tscn"),
            "force_reload": bool(params.get("force_reload", False)),
        }
    if action == "save-scene":
        return "scene_save", {}
    if action == "create-node":
        arguments = {
            "type": str(params["type"]),
            "name": str(params.get("name", "")),
            "parent_path": _node_path(params.get("parent_path", ""), allow_empty=True),
        }
        if params.get("scene_file"):
            arguments["scene_file"] = _res_path(params["scene_file"], ".tscn")
        return "node_create", arguments
    if action == "set-node-property":
        arguments = {
            "path": _node_path(params["path"]),
            "property": str(params["property"]),
            "value": params["value"],
        }
        if params.get("scene_file"):
            arguments["scene_file"] = _res_path(params["scene_file"], ".tscn")
        return "node_set_property", arguments
    if action == "delete-node":
        nested: dict[str, Any] = {"path": _node_path(params["path"])}
        if params.get("scene_file"):
            nested["scene_file"] = _res_path(params["scene_file"], ".tscn")
        return "node_manage", {"op": "delete", "params": nested}
    if action == "run-project":
        mode = str(params.get("mode", "main"))
        arguments = {"mode": mode, "autosave": bool(params.get("autosave", True))}
        if mode == "custom":
            scene = str(params.get("scene", ""))
            if not scene:
                raise GodotMcpError("run-project mode=custom requires scene")
            arguments["scene"] = _res_path(scene, ".tscn")
        return "project_run", arguments
    if action == "stop-project":
        return "project_manage", {"op": "stop", "params": {}}
    if action == "inspect-runtime-tree":
        return "game_manage", {
            "op": "get_scene_tree",
            "params": {
                "depth": int(params.get("depth", 10)),
                "root_path": str(params.get("root_path", "")),
            },
        }
    if action == "send-action":
        return "game_manage", {
            "op": "input_action",
            "params": {
                "action": str(params["action"]),
                "pressed": bool(params.get("pressed", True)),
                "strength": float(params.get("strength", 1.0)),
            },
        }
    raise GodotMcpError(f"Unsupported Godot adapter action: {action}")


def _project_roots(context: dict[str, Any]) -> tuple[Path, Path]:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise GodotMcpError("A selected FolderBridge workspace is required")
    workspace_root = Path(raw).resolve(strict=True)
    direct = workspace_root / "project.godot"
    nested = workspace_root / "Godot" / "project.godot"
    if direct.is_file():
        return workspace_root, workspace_root
    if nested.is_file():
        return workspace_root, nested.parent.resolve(strict=True)
    raise GodotMcpError("Selected workspace has no project.godot or Godot/project.godot")


def _matching_session(sessions: Any, project_root: Path) -> dict[str, Any] | None:
    if not isinstance(sessions, list):
        return None
    wanted = _normal_path(project_root)
    for session in sessions:
        if not isinstance(session, dict):
            continue
        raw = session.get("project_path")
        if isinstance(raw, str) and raw and _normal_path(Path(raw)) == wanted:
            return session
    return None


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _res_path(value: Any, required_suffix: str = "") -> str:
    raw = str(value).strip().replace("\\", "/")
    if raw.startswith("res://"):
        raw = raw[6:]
    elif raw.startswith("Godot/"):
        raw = raw[6:]
    if not raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        raise GodotMcpError("Godot resource paths must be project-relative or start with res://")
    normalized = posixpath.normpath(raw)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise GodotMcpError("Godot resource path escapes the project root")
    if required_suffix and not normalized.lower().endswith(required_suffix):
        raise GodotMcpError(f"Godot resource path must end with {required_suffix}")
    return "res://" + normalized


def _node_path(value: Any, allow_empty: bool = False) -> str:
    raw = str(value).strip().replace("\\", "/")
    if not raw and allow_empty:
        return ""
    if not raw.startswith("/") or ".." in raw.split("/"):
        raise GodotMcpError("Node paths must be absolute edited-scene paths without '..'")
    return raw


def _parse_mcp_response(raw: str, request_id: int) -> dict[str, Any]:
    candidates: list[str] = []
    stripped = raw.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for line in raw.splitlines():
        if line.startswith("data:"):
            candidates.append(line[5:].strip())
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise GodotMcpError("Godot AI returned no matching MCP response event")


def _structured(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("structuredContent")
    if isinstance(value, dict):
        return value
    text = _content_text(result.get("content"))
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
    return "\n".join(part for part in parts if isinstance(part, str))


def _safe_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    safe: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            safe.append({"type": "text", "text": item["text"]})
        elif kind == "image" and isinstance(item.get("data"), str) and isinstance(item.get("mimeType"), str):
            safe.append({"type": "image", "data": item["data"], "mimeType": item["mimeType"]})
    return safe
