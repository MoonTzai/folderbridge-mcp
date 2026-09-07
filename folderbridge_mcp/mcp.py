from __future__ import annotations

import json
import re
import sys
import threading
import time
from typing import Any, BinaryIO, Callable

from .concurrency import (
    CONTROL_MAX_INFLIGHT,
    CONTROL_WORKERS,
    DATA_MAX_INFLIGHT,
    DATA_WORKERS,
    SERVER_BUSY_CODE,
    BoundedExecutorLane,
)
from .flight_recorder import FlightRecorder
from .tools import ToolRuntime
from .transport_limits import MAX_MCP_MESSAGE_BYTES, MAX_MCP_MESSAGE_MIB


MAX_MESSAGE_BYTES = MAX_MCP_MESSAGE_BYTES
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_VERSION = "2026-07-28"
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
SERVER_META = "io.modelcontextprotocol/serverInfo"
RECOVERY_CONTROL_ONLY_CODE = -32024


class McpRequestScheduler:
    """Transport-neutral bounded MCP request scheduler."""

    def __init__(
        self,
        dispatch,
        *,
        control_workers: int = CONTROL_WORKERS,
        control_max_inflight: int = CONTROL_MAX_INFLIGHT,
        data_workers: int = DATA_WORKERS,
        data_max_inflight: int = DATA_MAX_INFLIGHT,
    ) -> None:
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        self._dispatch = dispatch
        self._control = BoundedExecutorLane(
            workers=control_workers,
            max_inflight=control_max_inflight,
            thread_name_prefix="folderbridge-control",
        )
        self._data = BoundedExecutorLane(
            workers=data_workers,
            max_inflight=data_max_inflight,
            thread_name_prefix="folderbridge-data",
        )
        self._close_lock = threading.Lock()
        self._closed = False

    def submit(self, request: Any, operation) -> bool:
        if not callable(operation):
            raise TypeError("operation must be callable")
        lane = self._control if _request_lane(request) == "control" else self._data
        return lane.submit(operation)

    def dispatch_sync(self, request: Any) -> dict[str, Any] | None:
        completed = threading.Event()
        holder: dict[str, Any] = {}

        def run() -> None:
            try:
                holder["response"] = self._dispatch(request)
            except Exception:
                request_id = request.get("id") if isinstance(request, dict) else None
                holder["response"] = _rpc_error(request_id, -32603, "Internal error")
            finally:
                completed.set()

        if not self.submit(request, run):
            request_id = request.get("id") if isinstance(request, dict) else None
            return _rpc_error(request_id, SERVER_BUSY_CODE, "Server busy")
        completed.wait()
        return holder.get("response")

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._control.close()
        self._data.close()


class McpServer:
    def __init__(
        self,
        runtime: ToolRuntime,
        *,
        flight_recorder: FlightRecorder | None = None,
        request_admission: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        if request_admission is not None and not callable(request_admission):
            raise TypeError("request_admission must be callable when supplied")
        self.runtime = runtime
        self.flight_recorder = flight_recorder or getattr(runtime, "flight_recorder", None)
        self._request_admission = request_admission
        self._write_lock = threading.Lock()

    def _record(self, event: str, *, severity: str = "info", text: str | None = None, **fields: Any) -> None:
        recorder = self.flight_recorder
        if recorder is None:
            return
        try:
            recorder.record(event, severity=severity, text=text, **fields)
        except Exception:
            pass

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
        admission = self._request_admission
        if admission is not None:
            try:
                allowed = bool(admission(request))
            except Exception:
                return None if notification else _rpc_error(request_id, -32603, "Recovery admission check failed")
            if not allowed:
                return None if notification else _rpc_error(
                    request_id,
                    RECOVERY_CONTROL_ONLY_CODE,
                    "Runtime is recovery-control only",
                )
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
        scheduler = McpRequestScheduler(self.dispatch)
        try:
            while True:
                try:
                    line = source.readline(MAX_MESSAGE_BYTES + 1)
                except Exception as exc:
                    self._record("mcp.read_error", severity="error", text=str(exc), exception_type=type(exc).__name__)
                    raise
                if not line:
                    self._record("mcp.eof")
                    break
                if len(line) > MAX_MESSAGE_BYTES:
                    observation = _oversized_request_observation(line)
                    complete_line = line.endswith(b"\n")
                    self._record(
                        "mcp.message_too_large",
                        severity="warning",
                        request_bytes=len(line),
                        request_bytes_is_lower_bound=not complete_line,
                        **observation,
                    )
                    # Emit the bounded rejection before waiting for the remainder
                    # of an oversized line. A shared-stdio transport can otherwise
                    # retire/close the logical request while the server is still
                    # blocked draining bytes that it has already decided to reject.
                    self._write(
                        destination,
                        _rpc_error(observation.get("request_id"), -32700, f"Message exceeds {MAX_MCP_MESSAGE_MIB} MiB"),
                        observation=observation,
                    )
                    if not complete_line:
                        drained_bytes, terminated_by_newline = _discard_line(source)
                        self._record(
                            "mcp.oversize_discard_complete",
                            severity="info" if terminated_by_newline else "warning",
                            initial_bytes=len(line),
                            drained_bytes=drained_bytes,
                            observed_bytes=len(line) + drained_bytes,
                            terminated_by_newline=terminated_by_newline,
                            eof_during_discard=not terminated_by_newline,
                            **observation,
                        )
                    continue
                try:
                    request = json.loads(line, parse_constant=_reject_json_constant)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._record("mcp.parse_error", severity="warning", text=str(exc), request_bytes=len(line))
                    self._write(destination, _rpc_error(None, -32700, "Parse error"))
                    continue
                lane_name = _request_lane(request)
                observation = _request_observation(request)
                enqueued_at = time.monotonic()
                self._record("mcp.request", lane=lane_name, request_bytes=len(line), **observation)
                if not scheduler.submit(
                    request,
                    lambda request=request, enqueued_at=enqueued_at, lane_name=lane_name, observation=observation: self._dispatch_and_write(
                        request,
                        destination,
                        enqueued_at=enqueued_at,
                        lane_name=lane_name,
                        observation=observation,
                    ),
                ):
                    self._record("mcp.busy", severity="warning", lane=lane_name, **observation)
                    if isinstance(request, dict) and "id" in request:
                        self._write(destination, _rpc_error(request.get("id"), SERVER_BUSY_CODE, "Server busy"), observation=observation)
        finally:
            self._record("mcp.shutdown_begin")
            try:
                begin_shutdown = getattr(self.runtime, "begin_shutdown", None)
                if callable(begin_shutdown):
                    begin_shutdown()
            finally:
                scheduler.close()
                close_runtime = getattr(self.runtime, "close", None)
                if callable(close_runtime):
                    close_runtime()
                self._record("mcp.shutdown_complete")

    def _dispatch_and_write(
        self,
        request: Any,
        destination: BinaryIO,
        *,
        enqueued_at: float,
        lane_name: str,
        observation: dict[str, Any],
    ) -> None:
        dispatch_started = time.monotonic()
        try:
            response = self.dispatch(request)
        except Exception as exc:
            self._record(
                "mcp.dispatch_exception",
                severity="error",
                text=str(exc),
                lane=lane_name,
                exception_type=type(exc).__name__,
                **observation,
            )
            request_id = request.get("id") if isinstance(request, dict) else None
            response = _rpc_error(request_id, -32603, "Internal error")
        response_bytes = 0
        if response is not None:
            response_bytes = self._write(destination, response, observation=observation)
        rpc_error_code = None
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            rpc_error_code = response["error"].get("code")
        self._record(
            "mcp.complete",
            lane=lane_name,
            duration_ms=round((time.monotonic() - enqueued_at) * 1000, 3),
            dispatch_ms=round((time.monotonic() - dispatch_started) * 1000, 3),
            response_bytes=response_bytes,
            rpc_error_code=rpc_error_code,
            **observation,
        )

    def _write(
        self,
        destination: BinaryIO,
        response: dict[str, Any],
        *,
        observation: dict[str, Any] | None = None,
    ) -> int:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with self._write_lock:
                destination.write(encoded)
                destination.flush()
        except Exception as exc:
            fields = dict(observation or {})
            fields.setdefault("request_id", response.get("id"))
            self._record(
                "mcp.write_error",
                severity="error",
                text=str(exc),
                exception_type=type(exc).__name__,
                response_bytes=len(encoded),
                **fields,
            )
            raise
        return len(encoded)


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
    if name in {"server_info", "flight_recorder"}:
        return "control"
    if name == "extension" and arguments.get("action") in {"list", "info", "job_list", "job_status", "job_cancel"}:
        return "control"
    if name in {"run_task", "run_capability"} and arguments.get("action") in {"list", "status", "cancel"}:
        return "control"
    if name == "write_file" and arguments.get("action") in {"status", "abort"}:
        return "control"
    return "data"


def _request_observation(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    observation: dict[str, Any] = {"request_id": request.get("id"), "method": request.get("method")}
    if request.get("method") != "tools/call":
        return observation
    params = request.get("params")
    if not isinstance(params, dict):
        return observation
    name = params.get("name")
    if isinstance(name, str):
        observation["tool"] = name
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return observation
    for key in ("action", "workspace_id", "extension_id", "extension_action", "job_id"):
        value = arguments.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            observation[key] = value
    if name == "run_capability" and isinstance(arguments.get("name"), str):
        observation["capability"] = arguments["name"]
    return observation


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


_OVERSIZE_IDENTITY_SCAN_BYTES = 64 * 1024


def _bounded_json_string_field(prefix: bytes, key: str, *, max_value_bytes: int = 256) -> str | None:
    sample = prefix[:_OVERSIZE_IDENTITY_SCAN_BYTES]
    encoded_key = re.escape(key.encode("ascii"))
    pattern = rb'"' + encoded_key + rb'"\s*:\s*"([^"\\]{0,' + str(max_value_bytes).encode("ascii") + rb'})"'
    match = re.search(pattern, sample)
    if match is None:
        return None
    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _oversized_request_observation(prefix: bytes) -> dict[str, Any]:
    """Best-effort bounded identity extraction for a request too large to parse.

    Only small structural identifiers are retained; payload bodies are never
    logged. The first MAX_MESSAGE_BYTES+1 bytes are a lower bound when the
    rejected line did not yet contain its terminating newline.
    """

    observation: dict[str, Any] = {}
    sample = prefix[:_OVERSIZE_IDENTITY_SCAN_BYTES]
    request_id = re.search(rb'"id"\s*:\s*(-?\d{1,20})', sample)
    if request_id is not None:
        try:
            observation["request_id"] = int(request_id.group(1))
        except ValueError:
            pass
    else:
        string_request_id = _bounded_json_string_field(sample, "id", max_value_bytes=256)
        if string_request_id is not None:
            observation["request_id"] = string_request_id
    method = _bounded_json_string_field(sample, "method", max_value_bytes=64)
    if method is not None:
        observation["method"] = method
    if method == "tools/call":
        tool = _bounded_json_string_field(sample, "name", max_value_bytes=128)
        if tool is not None:
            observation["tool"] = tool
        for key in ("action", "workspace_id", "extension_id", "extension_action", "job_id"):
            value = _bounded_json_string_field(sample, key, max_value_bytes=256)
            if value is not None:
                observation[key] = value
    return observation


def _discard_line(source: BinaryIO) -> tuple[int, bool]:
    drained_bytes = 0
    while True:
        chunk = source.readline(MAX_MESSAGE_BYTES + 1)
        drained_bytes += len(chunk)
        if not chunk:
            return drained_bytes, False
        if chunk.endswith(b"\n"):
            return drained_bytes, True


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")
