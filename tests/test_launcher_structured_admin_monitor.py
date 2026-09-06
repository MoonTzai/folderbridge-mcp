from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from folderbridge_mcp.launcher_backend import (
    CommandResult,
    LauncherSettingsStore,
    TunnelAdminCapability,
    TunnelSupervisor,
    build_run_argv,
    build_structured_admin_run_argv,
    prepare_tunnel_run,
    probe_tunnel_admin_capability,
)
from folderbridge_mcp.tunnel_health import (
    TunnelAdminHealthMonitor,
    TunnelAdminSnapshot,
)


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
    pid = 4321

    def __init__(self, returncode=None) -> None:
        self.returncode = returncode
        self.stdout = None

    def poll(self):
        return self.returncode


ROOT = Path(__file__).resolve().parents[1]
REGISTERED_WORKSPACES = [Path(value) for value in LauncherSettingsStore().load().workspaces]
TOOLS_CANDIDATES = REGISTERED_WORKSPACES + [ROOT.parent / "Tools", ROOT.parent.parent / "Tools"]
TOOLS = next(
    (path for path in TOOLS_CANDIDATES if (path / "tunnel-client-v0.0.14-windows-amd64.zip").is_file()),
    TOOLS_CANDIDATES[0],
)
V0014_ZIP = TOOLS / "tunnel-client-v0.0.14-windows-amd64.zip"
V0014_ZIP_SHA256 = "784ab8da7b5a88f0109f1fd8aaf0a1c86067430b896dddf307ef7e3cc49fa1a5"


class StructuredAdminCapabilityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and V0014_ZIP.is_file(), "exact v0.0.14 Windows asset unavailable")
    def test_exact_official_v0014_binary_declares_normal_run_structured_admin_contract(self) -> None:
        self.assertEqual(hashlib.sha256(V0014_ZIP.read_bytes()).hexdigest(), V0014_ZIP_SHA256)
        with tempfile.TemporaryDirectory() as temp:
            with zipfile.ZipFile(V0014_ZIP) as archive:
                member = next(
                    item for item in archive.infolist()
                    if not item.is_dir()
                    and Path(item.filename.replace("\\\\", "/")).name.lower() == "tunnel-client.exe"
                )
                executable = Path(temp) / "tunnel-client.exe"
                executable.write_bytes(archive.read(member))
            capability = probe_tunnel_admin_capability(executable, env=dict(os.environ))

        self.assertTrue(capability.supported, capability)
        self.assertEqual(capability.version, (0, 0, 14))
        self.assertEqual(
            set(capability.capabilities),
            {"health.listen-addr", "health.url-file"},
        )

    def test_v0013_keeps_legacy_run_argv_and_never_gets_health_flags(self) -> None:
        executable = Path("tunnel-client.exe")
        version = CommandResult(0, "0.0.13+legacy\n", False, False)

        with mock.patch(
            "folderbridge_mcp.launcher_backend.run_short_command",
            return_value=version,
        ) as runner:
            capability = probe_tunnel_admin_capability(executable, env={})

        self.assertFalse(capability.supported)
        self.assertEqual(capability.version, (0, 0, 13))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(
            build_run_argv(executable, "folderbridge"),
            [str(executable), "run", "--profile", "folderbridge"],
        )

    def test_v0014_requires_both_version_and_exact_normal_run_help_flags(self) -> None:
        executable = Path("tunnel-client.exe")
        version = CommandResult(0, "0.0.14+abcdef (git sha: abcdef)\n", False, False)
        help_result = CommandResult(
            0,
            "usage\n  --health.listen-addr string\n  --health.url-file string\n",
            False,
            False,
        )

        with mock.patch(
            "folderbridge_mcp.launcher_backend.run_short_command",
            side_effect=[version, help_result],
        ):
            capability = probe_tunnel_admin_capability(executable, env={})

        self.assertTrue(capability.supported)
        self.assertEqual(capability.version, (0, 0, 14))
        self.assertEqual(
            set(capability.capabilities),
            {"health.listen-addr", "health.url-file"},
        )

    def test_v0014_version_without_help_contract_fails_closed(self) -> None:
        executable = Path("tunnel-client.exe")
        version = CommandResult(0, "0.0.14\n", False, False)
        help_result = CommandResult(0, "usage\n  --health.listen-addr string\n", False, False)

        with mock.patch(
            "folderbridge_mcp.launcher_backend.run_short_command",
            side_effect=[version, help_result],
        ):
            capability = probe_tunnel_admin_capability(executable, env={})

        self.assertFalse(capability.supported)
        self.assertIn("health.url-file", capability.reason)

    def test_structured_admin_builder_is_separate_from_legacy_builder(self) -> None:
        executable = Path("tunnel-client.exe")
        health_file = Path("C:/safe/health-generation.url")

        self.assertEqual(
            build_structured_admin_run_argv(executable, "folderbridge", health_file),
            [
                str(executable),
                "run",
                "--profile",
                "folderbridge",
                "--health.listen-addr",
                "127.0.0.1:0",
                "--health.url-file",
                str(health_file),
            ],
        )
        self.assertEqual(
            build_run_argv(executable, "folderbridge"),
            [str(executable), "run", "--profile", "folderbridge"],
        )

    def test_prepare_run_falls_back_to_legacy_when_capability_is_unavailable(self) -> None:
        executable = Path("tunnel-client.exe")
        unavailable = TunnelAdminCapability(
            supported=False,
            version=(0, 0, 13),
            version_text="0.0.13",
            capabilities=(),
            reason="version-too-old",
        )
        with mock.patch(
            "folderbridge_mcp.launcher_backend.probe_tunnel_admin_capability",
            return_value=unavailable,
        ):
            plan = prepare_tunnel_run(executable, "folderbridge", env={})

        self.assertEqual(plan.argv, build_run_argv(executable, "folderbridge"))
        self.assertIsNone(plan.health_url_file)
        self.assertFalse(plan.admin_capability.supported)

    def test_prepare_run_uses_private_generation_url_only_after_capability_gate(self) -> None:
        executable = Path("tunnel-client.exe")
        available = TunnelAdminCapability(
            supported=True,
            version=(0, 0, 14),
            version_text="0.0.14",
            capabilities=("health.listen-addr", "health.url-file"),
            reason="supported",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch(
                "folderbridge_mcp.launcher_backend.probe_tunnel_admin_capability",
                return_value=available,
            ):
                plan = prepare_tunnel_run(
                    executable,
                    "folderbridge",
                    env={},
                    config_root=root,
                )

            self.assertIsNotNone(plan.health_url_file)
            assert plan.health_url_file is not None
            self.assertEqual(plan.health_url_file.parent, root / "tunnel-health")
            self.assertFalse(plan.health_url_file.exists())
            self.assertIn("--health.listen-addr", plan.argv)
            self.assertIn("--health.url-file", plan.argv)


class GenerationBoundAdminEvidenceTests(unittest.TestCase):
    def test_stale_generation_admin_snapshot_is_ignored(self) -> None:
        supervisor = TunnelSupervisor(lambda _text: None)
        supervisor._process = _FakeProcess()  # type: ignore[assignment]
        supervisor._generation_id = 2
        supervisor._desired_running = True

        self.assertFalse(supervisor.update_admin_health_for_generation(1, good_admin()))
        self.assertEqual(supervisor.health_snapshot().summary, "unknown")

        self.assertTrue(supervisor.update_admin_health_for_generation(2, good_admin()))
        health = supervisor.health_snapshot()
        self.assertEqual(health.summary, "ready_not_exercised")
        self.assertEqual(health.control_plane_alive, "unknown")
        self.assertEqual(health.end_to_end_data_plane_healthy, "unknown")


class StructuredAdminMonitorTests(unittest.TestCase):
    def test_monitor_skips_startup_unavailability_then_publishes_success_and_later_failure(self) -> None:
        first = TunnelAdminSnapshot(status=None, system=None, error="health URL file unavailable")
        second = good_admin()
        third = TunnelAdminSnapshot(status=None, system=None, error="admin endpoint unavailable")
        snapshots = [first, second, third]
        published: list[TunnelAdminSnapshot] = []
        finished = threading.Event()

        class FakeClient:
            def __init__(self, _path: Path, **_kwargs: object) -> None:
                pass

            def snapshot(self) -> TunnelAdminSnapshot:
                if snapshots:
                    return snapshots.pop(0)
                return third

        def publish(snapshot: TunnelAdminSnapshot) -> bool:
            published.append(snapshot)
            if len(published) >= 2:
                finished.set()
                return False
            return True

        monitor = TunnelAdminHealthMonitor(
            Path("health.url"),
            publish_snapshot=publish,
            interval_seconds=0.01,
            client_factory=FakeClient,
            cleanup_file=False,
        )
        monitor.start()
        self.assertTrue(finished.wait(1.0))
        monitor.stop()

        self.assertEqual(published, [second, third])


class GuiStructuredAdminSourceTests(unittest.TestCase):
    def test_gui_start_worker_uses_gated_plan_and_background_supervisor_monitor(self) -> None:
        gui = (
            Path(__file__).resolve().parents[1] / "folderbridge_mcp" / "gui.py"
        ).read_text(encoding="utf-8")
        start_block = gui.split("def _start_connection", 1)[1].split("def _stop_connection", 1)[0]
        poll_block = gui.split("def _poll_process", 1)[1].split("def _set_connection_state", 1)[0]

        self.assertIn("prepare_tunnel_run(", start_block)
        self.assertIn("start_admin_monitor(", start_block)
        self.assertNotIn("TunnelAdminHealthClient", poll_block)


if __name__ == "__main__":
    unittest.main()
