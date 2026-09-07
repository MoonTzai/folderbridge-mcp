from __future__ import annotations

import hmac
import json
import math
import socket
import socketserver
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .transport_limits import MAX_MCP_MESSAGE_BYTES


MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_MCP_REQUEST_BYTES = MAX_MCP_MESSAGE_BYTES
DEFAULT_HTTP_READ_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_OPEN_CONNECTIONS = 16


class GenerationAdmissionError(RuntimeError):
    pass


class GenerationUnknown(GenerationAdmissionError):
    pass


class GenerationRetired(GenerationAdmissionError):
    pass


class GenerationControlOnly(GenerationAdmissionError):
    pass


class GenerationAdmissionBusy(GenerationAdmissionError):
    pass


@dataclass(frozen=True)
class AdmissionTicket:
    request_id: str
    generation: int
    control: bool


@dataclass
class _GenerationState:
    token: str
    generation: int
    state: str
    accepted: int = 0


@dataclass
class _LedgerEntry:
    request_id: str
    token: str
    generation: int
    control: bool
    status: str = "queued"
    effect_attempted: bool = False


class GenerationAdmission:
    """Atomic generation token state + bounded accepted-request ledger.

    Token validation, state validation and ledger enrollment happen under one
    lock. Retirement changes state under that same lock and terminalizes queued
    requests that never crossed the durable effect boundary.
    """

    def __init__(self, *, max_accepted: int = 4096) -> None:
        if not isinstance(max_accepted, int) or isinstance(max_accepted, bool) or not 1 <= max_accepted <= 65536:
            raise ValueError("max_accepted must be 1..65536")
        self.max_accepted = max_accepted
        self._lock = threading.RLock()
        self._generations: dict[int, _GenerationState] = {}
        self._ledger: dict[str, _LedgerEntry] = {}

    @staticmethod
    def _validate_state(state: str) -> None:
        if state not in {"control_only", "active", "retired"}:
            raise ValueError("generation state must be control_only, active, or retired")

    def register_generation(self, token: str, *, generation: int, state: str = "control_only") -> None:
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("generation token must be a non-empty secret string")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        self._validate_state(state)
        with self._lock:
            if generation in self._generations:
                raise ValueError("generation counter is already registered")
            for current in self._generations.values():
                if hmac.compare_digest(current.token, token):
                    raise ValueError("generation token is already registered")
            self._generations[generation] = _GenerationState(token, generation, state)

    def set_state(self, token: str, state: str) -> None:
        self._validate_state(state)
        if state == "retired":
            self.retire_generation(token)
            return
        with self._lock:
            generation = self._match_token_locked(token)
            if generation.state == "retired":
                raise GenerationRetired("generation token is retired")
            generation.state = state

    def _match_token_locked(self, token: str) -> _GenerationState:
        if not isinstance(token, str):
            raise GenerationUnknown("unknown generation token")
        match: _GenerationState | None = None
        # Keep request-scoped authentication constant-time with respect to each
        # currently registered secret rather than using a plain secret-key dict.
        for candidate in self._generations.values():
            if hmac.compare_digest(candidate.token, token):
                match = candidate
        if match is None:
            raise GenerationUnknown("unknown generation token")
        return match

    def validate_token(self, token: str) -> int:
        """Authenticate a token early without granting request admission.

        The authoritative state check and ledger enrollment still happen
        atomically in admit immediately before dispatch.
        """

        with self._lock:
            generation = self._match_token_locked(token)
            if generation.state == "retired":
                raise GenerationRetired("generation token is retired")
            return generation.generation

    def admit(self, token: str, request_id: str, *, control: bool = False) -> AdmissionTicket:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        with self._lock:
            generation = self._match_token_locked(token)
            if generation.state == "retired":
                raise GenerationRetired("generation token is retired")
            if generation.state == "control_only" and not control:
                raise GenerationControlOnly("generation admits recovery-control requests only")
            if request_id in self._ledger:
                raise ValueError("request_id is already ledgered")
            if len(self._ledger) >= self.max_accepted:
                raise GenerationAdmissionBusy("accepted-request ledger is full")
            entry = _LedgerEntry(request_id, generation.token, generation.generation, bool(control))
            self._ledger[request_id] = entry
            generation.accepted += 1
            return AdmissionTicket(request_id, generation.generation, bool(control))

    def mark_started(self, request_id: str) -> None:
        with self._lock:
            entry = self._ledger.get(request_id)
            if entry is None:
                raise GenerationUnknown("request is not in the accepted ledger")
            if entry.status == "generation_retired_no_effect":
                raise GenerationRetired("queued request was terminalized by generation retirement")
            if entry.status not in {"queued", "executing"}:
                raise GenerationAdmissionError(f"request cannot start from state {entry.status}")
            entry.status = "executing"

    def mark_effect_attempted(self, request_id: str) -> None:
        with self._lock:
            entry = self._ledger.get(request_id)
            if entry is None:
                raise GenerationUnknown("request is not in the accepted ledger")
            if entry.status == "generation_retired_no_effect":
                raise GenerationRetired("request was terminalized before the effect boundary")
            entry.effect_attempted = True
            if entry.status == "queued":
                entry.status = "executing"

    def complete(self, request_id: str) -> None:
        with self._lock:
            entry = self._ledger.get(request_id)
            if entry is None:
                raise GenerationUnknown("request is not in the accepted ledger")
            if entry.status == "generation_retired_no_effect":
                # This terminal record remains discoverable until the generation
                # is reconciled/retired by the Runtime lifecycle.
                return
            entry.status = "complete"
            del self._ledger[request_id]

    def retire_generation(self, token: str) -> dict[str, Any]:
        with self._lock:
            generation = self._match_token_locked(token)
            generation.state = "retired"
            terminalized: list[str] = []
            draining: list[str] = []
            for entry in self._ledger.values():
                if entry.generation != generation.generation:
                    continue
                if entry.status == "queued" and not entry.effect_attempted:
                    entry.status = "generation_retired_no_effect"
                    terminalized.append(entry.request_id)
                elif entry.status != "complete":
                    draining.append(entry.request_id)
            return {
                "generation": generation.generation,
                "state": generation.state,
                "terminalized_no_effect": sorted(terminalized),
                "draining": sorted(draining),
            }

    def snapshot(self, token: str) -> dict[str, Any]:
        with self._lock:
            generation = self._match_token_locked(token)
            entries = [entry for entry in self._ledger.values() if entry.generation == generation.generation]
            return {
                "generation": generation.generation,
                "state": generation.state,
                "accepted": generation.accepted,
                "terminalized_no_effect": sorted(
                    entry.request_id for entry in entries if entry.status == "generation_retired_no_effect"
                ),
                "draining": sorted(
                    entry.request_id
                    for entry in entries
                    if entry.status not in {"generation_retired_no_effect", "complete"}
                ),
            }


class TunnelHealthClassifier:
    """Version-pinned structured-diagnostic decision scaffold.

    This object deliberately classifies only evidence supplied as already
    acceptance-verified by the caller. Free-form tunnel log text never enters
    this interface.
    """

    def __init__(self, *, failure_threshold: int = 3, window_seconds: float = 10.0) -> None:
        if not isinstance(failure_threshold, int) or isinstance(failure_threshold, bool) or failure_threshold < 2:
            raise ValueError("failure_threshold must be an integer >= 2")
        if not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool) or window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.failure_threshold = failure_threshold
        self.window_seconds = float(window_seconds)
        self._failures: deque[float] = deque(maxlen=max(8, failure_threshold * 2))
        self._auth_terminal_snapshot: str | None = None

    def auth_terminal(self, credential_snapshot: str) -> bool:
        current = self._auth_terminal_snapshot
        return bool(
            current is not None
            and isinstance(credential_snapshot, str)
            and hmac.compare_digest(current, credential_snapshot)
        )

    def observe(
        self,
        event: dict[str, Any],
        *,
        runtime_healthy: bool,
        diagnostic_contract_verified: bool,
        credential_snapshot: str | None = None,
        now: float | None = None,
    ) -> str:
        if credential_snapshot is not None and self.auth_terminal(credential_snapshot):
            return "auth_terminal"
        if not diagnostic_contract_verified or not isinstance(event, dict):
            return "unknown"

        status_code = event.get("status_code")
        if status_code == 401 and event.get("error_code") == "invalid_api_key":
            if not isinstance(credential_snapshot, str) or not credential_snapshot:
                return "auth_terminal"
            self._auth_terminal_snapshot = credential_snapshot
            return "auth_terminal"

        upstream_dead_signal = (
            status_code == 502
            and event.get("failure_source") == "client_internal"
            and event.get("upstream_response_received") is False
        )
        if not upstream_dead_signal:
            return "unknown"
        if not runtime_healthy:
            return "runtime_loss"

        timestamp = time.monotonic() if now is None else float(now)
        if not math.isfinite(timestamp):
            return "unknown"
        cutoff = timestamp - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(timestamp)
        if len(self._failures) >= self.failure_threshold:
            return "upstream_path_dead"
        return "transient"


def is_recovery_control_request(request: dict[str, Any]) -> bool:
    """Classify only read-only or Host-token-guarded recovery controls.

    Owner-specific actions never gain recovery admission from a control-like
    action name. Their Effect/Recovery Contract remains authoritative.
    """

    method = request.get("method")
    if method in {
        "initialize",
        "ping",
        "server/discover",
        "tools/list",
        "notifications/initialized",
        "notifications/cancelled",
    }:
        return True
    if method != "tools/call":
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return False
    name = params.get("name")
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if name in {"server_info", "flight_recorder"}:
        return True
    if name == "extension":
        action = arguments.get("action")
        if action in {"list", "info", "job_list", "job_status"}:
            return True
        if action == "job_cancel":
            job_id = arguments.get("job_id")
            return isinstance(job_id, str) and bool(job_id)
        return False
    if name in {"run_task", "run_capability"}:
        action = arguments.get("action")
        if action in {"list", "status"}:
            return True
        if action == "cancel":
            job_id = arguments.get("job_id")
            return isinstance(job_id, str) and bool(job_id)
        return False
    if name == "write_file":
        action = arguments.get("action")
        if action == "status":
            return True
        if action == "abort":
            transaction_id = arguments.get("transaction_id")
            return isinstance(transaction_id, str) and bool(transaction_id)
    return False


class _BoundedThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[socketserver.BaseRequestHandler], *, max_open: int) -> None:
        self._open_slots = threading.BoundedSemaphore(max_open)
        self.request_queue_size = max_open
        super().__init__(address, handler, bind_and_activate=True)

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self._open_slots.acquire(blocking=False):
            try:
                request.close()
            finally:
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._open_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._open_slots.release()


class PrivateMcpHttpServer:
    """Narrow private loopback MCP-over-HTTP Phase-0 adapter.

    It is intentionally not wired into Launcher yet. The server exists so the
    exact framing/auth/generation contract can be acceptance-tested before any
    persistent Runtime cutover.
    """

    _REASONS = {
        200: "OK",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        408: "Request Timeout",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }

    def __init__(
        self,
        dispatch: Callable[[dict[str, Any]], dict[str, Any] | None],
        admission: GenerationAdmission,
        *,
        max_open_connections: int = DEFAULT_MAX_OPEN_CONNECTIONS,
        read_timeout_seconds: float = DEFAULT_HTTP_READ_TIMEOUT_SECONDS,
        control_classifier: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        if not isinstance(admission, GenerationAdmission):
            raise TypeError("admission must be GenerationAdmission")
        if control_classifier is not None and not callable(control_classifier):
            raise TypeError("control_classifier must be callable when supplied")
        if not isinstance(max_open_connections, int) or isinstance(max_open_connections, bool) or max_open_connections < 1:
            raise ValueError("max_open_connections must be positive")
        self._dispatch = dispatch
        self._admission = admission
        self._control_classifier = control_classifier
        self._read_timeout = float(read_timeout_seconds)
        self._server = _BoundedThreadingTCPServer(
            ("127.0.0.1", 0),
            _PrivateMcpRequestHandler,
            max_open=max_open_connections,
        )
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"folderbridge-private-mcp-{self.port}",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def close(self) -> None:
        thread = self._thread
        if thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None


class _PrivateMcpRequestHandler(socketserver.BaseRequestHandler):
    server: _BoundedThreadingTCPServer

    @property
    def owner(self) -> PrivateMcpHttpServer:
        return self.server.owner  # type: ignore[attr-defined,no-any-return]

    def handle(self) -> None:
        self.request.settimeout(self.owner._read_timeout)
        try:
            parsed = self._read_request()
        except socket.timeout:
            self._send(408, {"error": "request timeout"})
            return
        except _HttpReject as exc:
            self._send(exc.status, {"error": exc.message})
            return
        if parsed is None:
            return
        body, token = parsed
        try:
            self.owner._admission.validate_token(token)
        except (GenerationUnknown, GenerationRetired):
            self._send(401, {"error": "invalid or retired generation token"})
            return

        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "body must be strict UTF-8 JSON"})
            return
        if not isinstance(request, dict):
            self._send(400, {"error": "MCP request must be a JSON object"})
            return

        control = False
        classifier = self.owner._control_classifier
        if classifier is not None:
            try:
                control = bool(classifier(request))
            except Exception:
                self._send(500, {"error": "recovery-control classification failed"})
                return

        request_id = uuid.uuid4().hex
        try:
            ticket = self.owner._admission.admit(token, request_id, control=control)
        except (GenerationUnknown, GenerationRetired):
            self._send(401, {"error": "invalid or retired generation token"})
            return
        except GenerationControlOnly:
            self._send(503, {"error": "generation is recovery-control only"})
            return
        except GenerationAdmissionBusy:
            self._send(503, {"error": "accepted-request ledger is full"})
            return

        try:
            self.owner._admission.mark_started(ticket.request_id)
            try:
                response = self.owner._dispatch(request)
            except Exception:
                self._send(500, {"error": "MCP dispatch failed"})
                return
            if response is None:
                self._send(204, None)
                return
            self._send(200, response)
        finally:
            try:
                self.owner._admission.complete(ticket.request_id)
            except GenerationAdmissionError:
                pass

    def _read_request(self) -> tuple[bytes, str] | None:
        head, rest = self._read_headers()
        try:
            header_text = head.decode("iso-8859-1")
        except UnicodeDecodeError as exc:
            raise _HttpReject(400, "invalid HTTP headers") from exc
        lines = header_text.split("\r\n")
        if not lines or not lines[0]:
            raise _HttpReject(400, "missing request line")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[2] != "HTTP/1.1":
            raise _HttpReject(400, "HTTP/1.1 request required")
        method, path, _version = parts
        if method != "POST":
            raise _HttpReject(400, "POST required")
        if path != "/mcp":
            raise _HttpReject(404, "unknown path")

        headers: dict[str, list[str]] = {}
        for raw in lines[1:]:
            if not raw:
                continue
            if ":" not in raw:
                raise _HttpReject(400, "malformed header")
            name, value = raw.split(":", 1)
            key = name.strip().lower()
            if not key:
                raise _HttpReject(400, "malformed header name")
            headers.setdefault(key, []).append(value.strip())

        host_values = headers.get("host", [])
        expected_host = f"127.0.0.1:{self.owner.port}"
        if len(host_values) != 1 or host_values[0] != expected_host:
            raise _HttpReject(400, "invalid Host")
        if "origin" in headers:
            raise _HttpReject(403, "Origin is not allowed")
        if "transfer-encoding" in headers:
            raise _HttpReject(400, "Transfer-Encoding is not supported")
        lengths = headers.get("content-length", [])
        if len(lengths) != 1:
            raise _HttpReject(400, "exactly one Content-Length is required")
        try:
            content_length = int(lengths[0], 10)
        except ValueError as exc:
            raise _HttpReject(400, "invalid Content-Length") from exc
        if content_length < 0:
            raise _HttpReject(400, "invalid Content-Length")
        if content_length > MAX_MCP_REQUEST_BYTES:
            raise _HttpReject(413, "MCP request exceeds 1 MiB")
        content_types = headers.get("content-type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise _HttpReject(415, "Content-Type must be application/json")
        tokens = headers.get("x-folderbridge-generation-token", [])
        if len(tokens) != 1 or not tokens[0]:
            raise _HttpReject(401, "missing generation token")

        if len(rest) > content_length:
            raise _HttpReject(400, "unexpected trailing bytes")
        body = bytearray(rest)
        while len(body) < content_length:
            chunk = self.request.recv(min(65536, content_length - len(body)))
            if not chunk:
                raise _HttpReject(400, "request body ended early")
            body.extend(chunk)

        # One request per connection. A tiny nonblocking probe catches bytes
        # already queued after the declared body without turning keep-alive into
        # an authenticated session.
        self.request.settimeout(0.01)
        try:
            trailing = self.request.recv(1)
        except socket.timeout:
            trailing = b""
        finally:
            self.request.settimeout(self.owner._read_timeout)
        if trailing:
            raise _HttpReject(400, "unexpected trailing bytes")
        return bytes(body), tokens[0]

    def _read_headers(self) -> tuple[bytes, bytes]:
        data = bytearray()
        marker = b"\r\n\r\n"
        while True:
            split = data.find(marker)
            if split >= 0:
                head_end = split + len(marker)
                if head_end > MAX_HTTP_HEADER_BYTES:
                    raise _HttpReject(431, "HTTP headers exceed 64 KiB")
                return bytes(data[:split]), bytes(data[head_end:])
            if len(data) >= MAX_HTTP_HEADER_BYTES:
                raise _HttpReject(431, "HTTP headers exceed 64 KiB")
            chunk = self.request.recv(min(4096, MAX_HTTP_HEADER_BYTES + 1 - len(data)))
            if not chunk:
                raise _HttpReject(400, "connection ended before headers")
            data.extend(chunk)

    def _send(self, status: int, payload: dict[str, Any] | None) -> None:
        reason = PrivateMcpHttpServer._REASONS.get(status, "Error")
        if payload is None:
            body = b""
            content_type = ""
        else:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            content_type = "Content-Type: application/json; charset=utf-8\r\n"
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{content_type}"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n"
        ).encode("ascii") + body
        try:
            self.request.sendall(response)
        except OSError:
            pass


class _HttpReject(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
