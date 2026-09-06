from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from folderbridge_mcp.tunnel_health import (
    TunnelAdminHealthClient,
    TunnelAdminSnapshot,
    evaluate_tunnel_health,
)


def admin_snapshot(*, probe_status: str = "ok", proxy_state: str = "healthy") -> TunnelAdminSnapshot:
    return TunnelAdminSnapshot(
        status={
            "version": "0.0.14",
            "channels": [
                {
                    "name": "main",
                    "enabled": True,
                    "transport_kind": "stdio",
                    "probe_status": probe_status,
                    "details": [
                        {"key": "pid", "value": "4242"},
                        {"key": "command", "value": "folderbridge serve"},
                    ],
                }
            ],
        },
        system={
            "main_channel_probe_status": probe_status,
            "proxy_health": [
                {
                    "route": {
                        "kind": "control_plane",
                        "name": "control-plane",
                        "route_mode": "proxy",
                    },
                    "health_state": proxy_state,
                    "last_check": "2026-09-05T00:00:00Z",
                    "last_success": "2026-09-05T00:00:00Z" if proxy_state == "healthy" else None,
                    "history": [],
                }
            ],
        },
        error=None,
    )


class TunnelHealthEvaluationTests(unittest.TestCase):
    def test_process_only_is_unknown_not_healthy(self) -> None:
        snapshot = evaluate_tunnel_health(process_alive=True, admin=None)

        self.assertEqual(snapshot.summary, "unknown")
        self.assertEqual(snapshot.end_to_end_data_plane_healthy, "unknown")
        self.assertNotEqual(snapshot.summary, "healthy")

    def test_probe_ok_is_ready_not_exercised_without_e2e_proof(self) -> None:
        snapshot = evaluate_tunnel_health(process_alive=True, admin=admin_snapshot())

        self.assertEqual(snapshot.control_plane_route_preflight, "healthy")
        self.assertEqual(snapshot.control_plane_alive, "unknown")
        self.assertEqual(snapshot.mcp_child_alive, "healthy")
        self.assertEqual(snapshot.end_to_end_data_plane_healthy, "unknown")
        self.assertEqual(snapshot.summary, "ready_not_exercised")

    def test_proxy_preflight_failure_is_degraded(self) -> None:
        snapshot = evaluate_tunnel_health(
            process_alive=True,
            admin=admin_snapshot(proxy_state="unhealthy"),
        )

        self.assertEqual(snapshot.control_plane_route_preflight, "unhealthy")
        self.assertEqual(snapshot.summary, "degraded")

    def test_mcp_probe_failure_is_degraded(self) -> None:
        snapshot = evaluate_tunnel_health(
            process_alive=True,
            admin=admin_snapshot(probe_status="failed"),
        )

        self.assertEqual(snapshot.mcp_child_alive, "degraded")
        self.assertEqual(snapshot.summary, "degraded")

    def test_only_explicit_e2e_authority_can_make_summary_healthy(self) -> None:
        snapshot = evaluate_tunnel_health(
            process_alive=True,
            admin=admin_snapshot(),
            end_to_end_state="healthy",
        )

        self.assertEqual(snapshot.control_plane_alive, "healthy")
        self.assertEqual(snapshot.end_to_end_data_plane_healthy, "healthy")
        self.assertEqual(snapshot.summary, "healthy")

    def test_stopped_process_is_stopped_even_with_stale_admin_payload(self) -> None:
        snapshot = evaluate_tunnel_health(
            process_alive=False,
            admin=admin_snapshot(),
            end_to_end_state="healthy",
        )

        self.assertEqual(snapshot.summary, "stopped")
        self.assertFalse(snapshot.process_alive)
        self.assertEqual(snapshot.end_to_end_data_plane_healthy, "unknown")


class _AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/api/status":
            payload = {
                "version": "0.0.14",
                "channels": [
                    {
                        "name": "main",
                        "enabled": True,
                        "transport_kind": "stdio",
                        "probe_status": "ok",
                        "details": [{"key": "pid", "value": "321"}],
                    }
                ],
            }
        elif self.path == "/api/system":
            payload = {
                "main_channel_probe_status": "ok",
                "proxy_health": [
                    {
                        "route": {"kind": "control_plane", "route_mode": "direct"},
                        "health_state": "direct",
                    }
                ],
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TunnelAdminHealthClientTests(unittest.TestCase):
    def test_loopback_admin_status_and_system_are_bounded_and_parsed(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _AdminHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "health.url"
                path.write_text(f"http://127.0.0.1:{server.server_address[1]}\n", encoding="utf-8")
                snapshot = TunnelAdminHealthClient(path, timeout_seconds=1.0).snapshot()

            self.assertIsNone(snapshot.error)
            self.assertEqual(snapshot.status["version"], "0.0.14")
            self.assertEqual(snapshot.system["main_channel_probe_status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_non_loopback_health_url_is_rejected_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.url"
            path.write_text("https://example.com:443\n", encoding="utf-8")
            snapshot = TunnelAdminHealthClient(path).snapshot()

        self.assertIsNone(snapshot.status)
        self.assertIsNone(snapshot.system)
        self.assertIn("loopback", snapshot.error.lower())


if __name__ == "__main__":
    unittest.main()
