from __future__ import annotations

import json
import sys
import threading
from typing import Any, BinaryIO

from .concurrency import (
    CONTROL_MAX_INFLIGHT,
    CONTROL_WORKERS,
    DATA_MAX_INFLIGHT,
    DATA_WORKERS,
    SERVER_BUSY_CODE,
    BoundedExecutorLane,
)
from .tools import ToolRuntime


MAX_MESSAGE_BYTES = 1024 * 1024
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_VERSION = "2026-07-28"
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
SERVER_META = "io.modelcontextprotocol/serverInfo"


class McpServer:
    def __init__(self, runtime: ToolRuntime) -> None:
        self.runtime = runtime
        self._write_lock = threading.Lock()

    def dispatch(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        notification = "id" not in request
        if not notification and not (
            request_id is None
            or isinstance(request_id, str)
            or (isinstance(request_id, int) and not isinstance(request_id, bool))
        ):
            return _rpc_error(None, -32600, "Invalid Request: id must be string, integer, or null")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return None if notification else _rpc_error(request_id, -32600, "Invalid Request")
        if notification:
            # MCP defines explicit notification methods. Never execute an
            # ordinary request method (especially tools/call) without an id.
            if method not in {"notifications/initialized", "notifications/cancelled"}:
                return None
        elif method == "tools/call" and request_id is None:
            return _rpc_error(None, -32600, "tools/call requires a non-null request id")
        modern = _is_modern(params)
        try:
            result = self._handle(method, params, modern=modern)
        except RpcFailure as exc:
            return None if notification else _rpc_error(request_id, exc.code, str(exc), exc.data)
        if notification or result is None:
            return None
        if modern:
            result = _shape_modern(result, self.runtime.identity, cacheable=method in {"server/discover", "tools/list"})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _handle(self, method: str, params: dict[str, Any], *, modern: bool) -> dict[str, Any] | None:
        if modern:
            meta = params.get("_meta", {})
            version = meta.get(PROTOCOL_META)
            if version != MODERN_VERSION:
                raise RpcFailure(-32022, f"Unsupported MCP protocol version: {version}", {"supported": [MODERN_VERSION]})
            if not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
                raise RpcFailure(-32602, "Modern requests require object clientCapabilities metadata")
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.runtime.identity,
                "instructions": self.runtime.instructions,
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return {}
        if method == "server/discover" and modern:
            return {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": self.runtime.instructions,
            }
        if method == "tools/list":
            return {"tools": self.runtime.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise RpcFailure(-32602, "tools/call needs a string name and object arguments")
            return self.runtime.call(name, arguments)
        raise RpcFailure(-32601, f"Unknown method: {method}")

    def serve(self, source: BinaryIO | None = None, destination: BinaryIO | None = None) -> None:
        source = source or sys.stdin.buffer
        destination = destination or sys.stdout.buffer
        control = BoundedExecutorLane(
            workers=CONTROL_WORKERS,
            max_inflight=CONTROL_MAX_INFLIGHT,
            thread_name_prefix="folderbridge-control",
        )
        data = BoundedExecutorLane(
            workers=DATA_WORKERS,
            max_inflight=DATA_MAX_INFLIGHT,
            thread_name_prefix="folderbridge-data",
        )
        try:
            while True:
                line = source.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES:
                    if not line.endswith(b"\n"):
                        _discard_line(source)
                    self._write(destination, _rpc_error(None, -32700, "Message exceeds 1 MiB"))
                    continue
                try:
                    request = json.loads(line, parse_constant=_reject_json_constant)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self._write(destination, _rpc_error(None, -32700, "Parse error"))
                    continue
                lane = control if _request_lane(request) == "control" else data
                if not lane.submit(lambda request=request: self._dispatch_and_write(request, destination)):
                    if isinstance(request, dict) and "id" in request:
                        self._write(destination, _rpc_error(request.get("id"), SERVER_BUSY_CODE, "Server busy"))
        finally:
            try:
                begin_shutdown = getattr(self.runtime, "begin_shutdown", None)
                if callable(begin_shutdown):
                    begin_shutdown()
            finally:
                control.close()
                data.close()
                close_runtime = getattr(self.runtime, "close", None)
                if callable(close_runtime):
                    close_runtime()

    def _dispatch_and_write(self, request: Any, destination: BinaryIO) -> None:
        try:
            response = self.dispatch(request)
        except Exception:
            request_id = request.get("id") if isinstance(request, dict) else None
            response = _rpc_error(request_id, -32603, "Internal error")
        if response is not None:
            self._write(destination, response)

    def _write(self, destination: BinaryIO, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._write_lock:
            destination.write(encoded)
            destination.flush()


class RpcFailure(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _request_lane(request: Any) -> str:
    if not isinstance(request, dict):
        return "control"
    method = request.get("method")
    if method in {
        "initialize",
        "ping",
        "server/discover",
        "tools/list",
        "notifications/initialized",
        "notifications/cancelled",
    }:
        return "control"
    if method != "tools/call":
        return "control"
    params = request.get("params")
    if not isinstance(params, dict):
        return "control"
    name = params.get("name")
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "server_info":
        return "control"
    if name == "extension" and arguments.get("action") in {"list", "info", "job_status", "job_cancel"}:
        return "control"
    if name == "write_file" and arguments.get("action") in {"status", "abort"}:
        return "control"
    return "data"


def _is_modern(params: dict[str, Any]) -> bool:
    meta = params.get("_meta")
    return isinstance(meta, dict) and PROTOCOL_META in meta


def _shape_modern(result: dict[str, Any], identity: dict[str, str], *, cacheable: bool) -> dict[str, Any]:
    shaped = dict(result)
    shaped["resultType"] = "complete"
    meta = dict(shaped.get("_meta")) if isinstance(shaped.get("_meta"), dict) else {}
    meta[SERVER_META] = identity
    shaped["_meta"] = meta
    if cacheable:
        shaped["ttlMs"] = 0
        shaped["cacheScope"] = "private"
    return shaped


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _discard_line(source: BinaryIO) -> None:
    while True:
        chunk = source.readline(MAX_MESSAGE_BYTES + 1)
        if not chunk or chunk.endswith(b"\n"):
            return


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")
