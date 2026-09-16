from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from folderbridge_mcp.flight_recorder import FlightRecorder, TUNNEL_GENERATION_NONCE_ENV
from folderbridge_mcp.launcher_backend import TunnelSupervisor
from folderbridge_mcp.tunnel_health import TunnelAdminSnapshot


def good_admin() -> TunnelAdminSnapshot:
    return TunnelAdminSnapshot(
        status={
            "channels": [
                {
                    "name": "main",
                    "enabled": True,
                    "transport_kind": "stdio",
                    "probe_status": "ok",
                    "details": [{"key": "pid", "value": "1234"}],
                }
            ]
        },
        system={
            "proxy_health": [
                {
                    "route": {"kind": "control_plane", "route_mode": "proxy"},
                    "health_state": "healthy",
                }
            ]
        },
        error=None,
    )


class _FakeProcess:
    pid = 123

    def __init__(self, returncode=None) -> None:
        self.returncode = returncode
        self.stdout = None

    def poll(self):
        return self.returncode


class TunnelSupervisorHealthWiringTests(unittest.TestCase):
    def test_process_only_supervisor_state_is_unknown_not_green(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        supervisor._process = _FakeProcess()  # type: ignore[assignment]

        health = supervisor.health_snapshot()

        self.assertEqual(health.summary, "unknown")
        self.assertNotEqual(health.summary, "healthy")

    def test_cached_admin_state_can_raise_only_to_ready_not_exercised(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        supervisor._process = _FakeProcess()  # type: ignore[assignment]
        supervisor.update_admin_health(good_admin())

        health = supervisor.health_snapshot()

        self.assertEqual(health.summary, "ready_not_exercised")
        self.assertEqual(health.mcp_child_alive, "healthy")
        self.assertEqual(health.control_plane_alive, "unknown")

    def test_explicit_future_e2e_authority_is_required_for_healthy(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        supervisor._process = _FakeProcess()  # type: ignore[assignment]
        supervisor.update_admin_health(good_admin())
        supervisor.update_end_to_end_health("healthy")

        self.assertEqual(supervisor.health_snapshot().summary, "healthy")

    def test_generation_bound_successful_remote_tool_call_promotes_to_healthy(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            root = Path(temporary)
            nonce = "c" * 32
            launcher_recorder = FlightRecorder("launcher", root=root, clock=lambda: now[0])
            with mock.patch.dict(os.environ, {TUNNEL_GENERATION_NONCE_ENV: nonce}, clear=False):
                mcp_recorder = FlightRecorder("mcp", root=root, clock=lambda: now[0])
            supervisor = TunnelSupervisor(lambda _text: None, flight_recorder=launcher_recorder)
            process = _FakeProcess()
            supervisor._process = process  # type: ignore[assignment]
            supervisor._desired_running = True
            supervisor._generation_id = 1
            supervisor._generation_health_nonce = nonce
            supervisor._generation_started_unix = 999.0
            supervisor.update_admin_health(good_admin())

            self.assertEqual(supervisor.health_snapshot().summary, "ready_not_exercised")
            mcp_recorder.record(
                "mcp.complete",
                method="tools/call",
                tool="server_info",
                response_bytes=512,
                rpc_error_code=None,
            )

            health = supervisor.health_snapshot()
            self.assertEqual(health.summary, "healthy")
            self.assertEqual(health.end_to_end_data_plane_healthy, "healthy")
            events = launcher_recorder.recent(minutes=15, limit=20)["events"]
            self.assertTrue(any(event["event"] == "tunnel.end_to_end_verified" for event in events))

    def test_wrong_generation_tool_call_does_not_promote_to_healthy(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            root = Path(temporary)
            launcher_recorder = FlightRecorder("launcher", root=root, clock=lambda: now[0])
            with mock.patch.dict(os.environ, {TUNNEL_GENERATION_NONCE_ENV: "d" * 32}, clear=False):
                old_mcp = FlightRecorder("mcp", root=root, clock=lambda: now[0])
            supervisor = TunnelSupervisor(lambda _text: None, flight_recorder=launcher_recorder)
            supervisor._process = _FakeProcess()  # type: ignore[assignment]
            supervisor._desired_running = True
            supervisor._generation_id = 2
            supervisor._generation_health_nonce = "e" * 32
            supervisor._generation_started_unix = 999.0
            supervisor.update_admin_health(good_admin())
            old_mcp.record(
                "mcp.complete",
                method="tools/call",
                response_bytes=256,
                rpc_error_code=None,
            )

            self.assertEqual(supervisor.health_snapshot().summary, "ready_not_exercised")

    def test_process_death_invalidates_cached_e2e_success(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        process = _FakeProcess()
        supervisor._process = process  # type: ignore[assignment]
        supervisor.update_admin_health(good_admin())
        supervisor.update_end_to_end_health("healthy")
        self.assertEqual(supervisor.health_snapshot().summary, "healthy")

        process.returncode = 0
        health = supervisor.health_snapshot()
        self.assertEqual(health.summary, "stopped")
        self.assertEqual(health.end_to_end_data_plane_healthy, "unknown")

    def test_new_start_resets_stale_health_authority(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        supervisor.update_admin_health(good_admin())
        supervisor.update_end_to_end_health("healthy")

        supervisor.reset_health_evidence()

        health = supervisor.health_snapshot()
        self.assertEqual(health.summary, "stopped")
        self.assertEqual(health.end_to_end_data_plane_healthy, "unknown")


class GuiHealthSemanticsSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from pathlib import Path

        cls.gui = (
            Path(__file__).resolve().parents[1] / "folderbridge_mcp" / "gui.py"
        ).read_text(encoding="utf-8")

    def test_started_process_is_not_immediately_promoted_to_green_running(self) -> None:
        drain = self.gui.split("def _drain_events", 1)[1].split("def _poll_process", 1)[0]
        started = drain.split('elif kind == "started":', 1)[1].split('elif kind == "stopped":', 1)[0]

        self.assertIn('_set_connection_state("unknown"', started)
        self.assertNotIn('_set_connection_state("running"', started)

    def test_poll_process_consumes_layered_health_summary(self) -> None:
        block = self.gui.split("def _poll_process", 1)[1].split("def _set_connection_state", 1)[0]

        self.assertIn("self.supervisor.health_snapshot()", block)
        self.assertIn('"ready_not_exercised"', block)
        self.assertIn('"degraded"', block)
        self.assertIn('"healthy"', block)

    def test_only_healthy_state_is_green(self) -> None:
        block = self.gui.split("def _set_connection_state", 1)[1].split("def _set_busy", 1)[0]

        self.assertIn('"unknown": "#f59e0b"', block)
        self.assertIn('"ready_not_exercised": "#f59e0b"', block)
        self.assertIn('"degraded": "#dc2626"', block)
        self.assertIn('"running": "#16a34a"', block)
        self.assertIn("健康未知", block)
        self.assertIn('"就绪 · 未验证端到端"', block)


if __name__ == "__main__":
    unittest.main()
