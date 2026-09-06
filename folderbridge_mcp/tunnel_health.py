from __future__ import annotations

import http.client
import ipaddress
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


MAX_HEALTH_URL_FILE_BYTES = 4096
MAX_ADMIN_RESPONSE_BYTES = 256 * 1024
DEFAULT_ADMIN_TIMEOUT_SECONDS = 1.5

_LAYER_STATES = frozenset({"healthy", "degraded", "unknown"})


@dataclass(frozen=True)
class TunnelAdminSnapshot:
    status: dict[str, Any] | None
    system: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class TunnelHealthSnapshot:
    process_alive: bool
    control_plane_route_preflight: str
    control_plane_alive: str
    mcp_child_alive: str
    end_to_end_data_plane_healthy: str
    summary: str
    reason: str


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _main_channel(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    channels = status.get("channels")
    if not isinstance(channels, list):
        return None
    for channel in channels:
        if isinstance(channel, dict) and channel.get("name") == "main":
            return channel
    return None


def _probe_state(admin: TunnelAdminSnapshot | None) -> str:
    if admin is None:
        return "unknown"
    channel = _main_channel(admin.status)
    if isinstance(channel, dict):
        if channel.get("enabled") is False:
            return "degraded"
        raw = channel.get("probe_status")
        if isinstance(raw, str) and raw:
            probe = raw.strip().lower()
            if probe == "ok":
                return "healthy"
            if probe in {"failed", "auth-required"}:
                return "degraded"
            if probe in {"pending", "timeout"}:
                return "unknown"
    system = admin.system
    if isinstance(system, dict):
        raw = system.get("main_channel_probe_status")
        if isinstance(raw, str) and raw:
            probe = raw.strip().lower()
            if probe == "ok":
                return "healthy"
            if probe in {"failed", "auth-required"}:
                return "degraded"
            if probe in {"pending", "timeout"}:
                return "unknown"
    return "unknown"


def _route_preflight_state(admin: TunnelAdminSnapshot | None) -> str:
    if admin is None or not isinstance(admin.system, dict):
        return "unknown"
    raw_health = admin.system.get("proxy_health")
    if not isinstance(raw_health, list):
        return "unknown"
    for item in raw_health:
        if not isinstance(item, dict):
            continue
        route = item.get("route")
        if not isinstance(route, dict) or route.get("kind") != "control_plane":
            continue
        raw = item.get("health_state")
        if not isinstance(raw, str):
            return "unknown"
        state = raw.strip().lower()
        if state in {"healthy", "unhealthy", "direct"}:
            return state
        return "unknown"
    return "unknown"


def evaluate_tunnel_health(
    *,
    process_alive: bool,
    admin: TunnelAdminSnapshot | None = None,
    end_to_end_state: str = "unknown",
) -> TunnelHealthSnapshot:
    """Compose truthfully separated Tunnel health layers.

    The external tunnel-client admin surface is deliberately not treated as
    end-to-end authority.  Proxy-health is only a network-route preflight and
    MCP probe state is only child/startup evidence.  A green summary therefore
    requires an explicitly injected authoritative end-to-end state.
    """

    if end_to_end_state not in _LAYER_STATES:
        raise ValueError("end_to_end_state must be healthy, degraded, or unknown")

    if not process_alive:
        return TunnelHealthSnapshot(
            process_alive=False,
            control_plane_route_preflight="unknown",
            control_plane_alive="unknown",
            mcp_child_alive="unknown",
            end_to_end_data_plane_healthy="unknown",
            summary="stopped",
            reason="tunnel process is not alive",
        )

    preflight = _route_preflight_state(admin)
    child = _probe_state(admin)

    # A successful real end-to-end command necessarily traversed the control
    # plane.  Without that authority, proxy preflight cannot promote the
    # protocol-layer control-plane state above unknown.
    control_plane = "healthy" if end_to_end_state == "healthy" else "unknown"

    if end_to_end_state == "degraded":
        summary = "degraded"
        reason = "authoritative end-to-end data-plane failure"
    elif admin is not None and admin.error:
        summary = "degraded"
        reason = "structured tunnel admin health became unavailable"
    elif preflight == "unhealthy":
        summary = "degraded"
        reason = "control-plane proxy-route preflight is unhealthy"
    elif child == "degraded":
        summary = "degraded"
        reason = "stdio MCP child startup/probe is degraded"
    elif end_to_end_state == "healthy":
        summary = "healthy"
        reason = "authoritative end-to-end data-plane success"
    elif child == "healthy":
        summary = "ready_not_exercised"
        reason = "process and MCP child are ready, but no authoritative end-to-end proof is available"
    else:
        summary = "unknown"
        reason = "process is alive, but structured child/end-to-end health is not proven"

    return TunnelHealthSnapshot(
        process_alive=True,
        control_plane_route_preflight=preflight,
        control_plane_alive=control_plane,
        mcp_child_alive=child,
        end_to_end_data_plane_healthy=end_to_end_state,
        summary=summary,
        reason=reason,
    )


class TunnelAdminHealthMonitor:
    """Background, generation-scoped polling of the bounded loopback admin API.

    Startup discovery failures are intentionally not published: before the
    health URL exists the truthful state remains UNKNOWN.  Once one structured
    snapshot succeeds, later admin failures are published so the caller can
    downgrade the generation instead of retaining stale READY evidence.
    """

    def __init__(
        self,
        health_url_file: Path,
        *,
        publish_snapshot: Any,
        interval_seconds: float = 1.0,
        client_factory: Any = None,
        cleanup_file: bool = True,
    ) -> None:
        if not callable(publish_snapshot):
            raise TypeError("publish_snapshot must be callable")
        if (
            not isinstance(interval_seconds, (int, float))
            or isinstance(interval_seconds, bool)
            or interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be positive")
        self.health_url_file = Path(health_url_file)
        self.publish_snapshot = publish_snapshot
        self.interval_seconds = float(interval_seconds)
        self.client_factory = client_factory or TunnelAdminHealthClient
        self.cleanup_file = bool(cleanup_file)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="folderbridge-tunnel-admin-health",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.interval_seconds + DEFAULT_ADMIN_TIMEOUT_SECONDS + 0.5))
        self._cleanup_url_file()

    def _run(self) -> None:
        seen_success = False
        try:
            client = self.client_factory(self.health_url_file)
            while not self._stop.is_set():
                snapshot = client.snapshot()
                should_publish = snapshot.error is None or seen_success
                if snapshot.error is None:
                    seen_success = True
                if should_publish:
                    try:
                        keep_running = self.publish_snapshot(snapshot)
                    except Exception:
                        keep_running = False
                    if keep_running is False:
                        break
                if self._stop.wait(self.interval_seconds):
                    break
        finally:
            if self.cleanup_file:
                self._cleanup_url_file()

    def _cleanup_url_file(self) -> None:
        path = self.health_url_file
        try:
            if path.is_symlink() or _is_reparse_point(path):
                return
            path.unlink(missing_ok=True)
        except OSError:
            pass


class TunnelAdminHealthClient:
    """Bounded read-only client for the official tunnel-client loopback admin API."""

    def __init__(
        self,
        health_url_file: Path,
        *,
        timeout_seconds: float = DEFAULT_ADMIN_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_ADMIN_RESPONSE_BYTES,
    ) -> None:
        self.health_url_file = Path(health_url_file)
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes

    def snapshot(self) -> TunnelAdminSnapshot:
        try:
            host, port = self._read_loopback_endpoint()
            status = self._fetch_json(host, port, "/api/status")
            system = self._fetch_json(host, port, "/api/system")
            return TunnelAdminSnapshot(status=status, system=system, error=None)
        except Exception as exc:
            return TunnelAdminSnapshot(
                status=None,
                system=None,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _read_loopback_endpoint(self) -> tuple[str, int]:
        path = self.health_url_file
        if path.is_symlink() or _is_reparse_point(path):
            raise ValueError("health URL file must not be a symlink or reparse point")
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_HEALTH_URL_FILE_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"health URL file unavailable: {exc}") from exc
        if len(data) > MAX_HEALTH_URL_FILE_BYTES:
            raise ValueError("health URL file exceeds bounded size")
        try:
            raw = data.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("health URL file is not UTF-8") from exc
        if not raw:
            raise ValueError("health URL file is empty")

        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "http":
            raise ValueError("health admin URL must use loopback HTTP")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("health admin URL must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("health admin URL must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("health admin URL must be an origin without a path")
        host = parsed.hostname
        if not isinstance(host, str) or not host:
            raise ValueError("health admin URL is missing a loopback host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("health admin host must be a numeric loopback address") from exc
        if not address.is_loopback:
            raise ValueError("health admin host must be loopback")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("health admin URL port is invalid") from exc
        if port is None or not 1 <= port <= 65535:
            raise ValueError("health admin URL must contain an explicit valid port")
        return str(address), port

    def _fetch_json(self, host: str, port: int, path: str) -> dict[str, Any]:
        connection = http.client.HTTPConnection(host, port, timeout=self.timeout_seconds)
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json",
                    "Connection": "close",
                    "Host": f"[{host}]:{port}" if ":" in host else f"{host}:{port}",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"admin endpoint {path} returned HTTP {response.status}")
            content_type = response.getheader("Content-Type", "")
            if "application/json" not in content_type.lower():
                raise ValueError(f"admin endpoint {path} did not return JSON")
            declared = response.getheader("Content-Length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise ValueError(f"admin endpoint {path} has invalid Content-Length") from exc
                if declared_size < 0 or declared_size > self.max_response_bytes:
                    raise ValueError(f"admin endpoint {path} exceeds bounded response size")
            data = response.read(self.max_response_bytes + 1)
            if len(data) > self.max_response_bytes:
                raise ValueError(f"admin endpoint {path} exceeds bounded response size")
            try:
                payload = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"admin endpoint {path} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"admin endpoint {path} must return a JSON object")
            return payload
        finally:
            connection.close()
