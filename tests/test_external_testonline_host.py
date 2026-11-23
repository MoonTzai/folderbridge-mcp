from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from folderbridge_mcp.managed_services import ManagedServiceError, default_managed_service_manager
from folderbridge_mcp.testonline_service import TestOnlineServiceController, TestOnlineServiceError, run_testonline_service

PLUG = Path(__file__).resolve().parents[1] / "Plugins" / "extensions" / "testonline-host"
if str(PLUG) not in sys.path:
    sys.path.insert(0, str(PLUG))
import server as host  # noqa: E402


def port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port_value: int, path: str = "/") -> tuple[int, bytes, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port_value, timeout=4)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


class TestOnlineServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "TestOnline"
        self.root.mkdir()
        self.manager = host.HostingManager(self.root, "127.0.0.1", Path(self.temp.name) / "state")
        self.addCleanup(self._close_all)

    def _close_all(self):
        for _mode, _folder, server, worker in self.manager.servers.values():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.manager.servers.clear()

    def test_http_folder_name_routes_index_and_live_replacement(self):
        p = port()
        folder = self.root / f"{p}-http"
        folder.mkdir()
        index = folder / "index.html"
        index.write_text("version one", encoding="utf-8")
        self.manager.scan()
        self.assertIn(p, self.manager.servers)
        status, content, headers = get(p)
        self.assertEqual((status, content), (200, b"version one"))
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        replacement = folder / "index-new.html"
        replacement.write_text("version two", encoding="utf-8")
        replacement.replace(index)
        self.assertEqual(get(p)[1], b"version two")
        self.assertEqual(len(self.manager.servers), 1)

    def test_create_remove_folder_detected_at_next_scan(self):
        p = port()
        self.manager.scan()
        self.assertNotIn(p, self.manager.servers)
        folder = self.root / f"{p}-http"
        folder.mkdir()
        (folder / "page.html").write_text("<p>A</p>", encoding="utf-8")
        self.manager.scan()
        self.assertIn(p, self.manager.servers)
        status, html, _ = get(p)
        self.assertEqual(status, 200)
        self.assertIn(b"page.html", html)
        self.assertEqual(get(p, "/page.html")[1], b"<p>A</p>")
        (folder / "page.html").unlink()
        folder.rmdir()
        self.manager.scan()
        self.assertNotIn(p, self.manager.servers)

    def test_short_ca_url_serves_public_certificate_with_cer_filename(self):
        self.manager.ca_file.parent.mkdir(parents=True, exist_ok=True)
        public_cert = b"test-only-public-ca-bytes"
        self.manager.ca_file.write_bytes(public_cert)
        control_port = port()
        control = host.MultiThreadHTTPServer(
            ("127.0.0.1", control_port), self.manager.system_handler(False)
        )
        worker = threading.Thread(target=control.serve_forever, daemon=True)
        worker.start()

        def close_control():
            control.shutdown()
            control.server_close()
            worker.join(timeout=2)

        self.addCleanup(close_control)
        code, payload, headers = get(control_port, "/CA")
        self.assertEqual((code, payload), (200, public_cert))
        self.assertEqual(headers["Content-Type"], "application/x-x509-ca-cert")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="testonline-root.cer"')
        self.assertEqual(get(control_port, "/testonline-root.cer")[1], public_cert)
        self.assertIn(b"href=\'/CA\'", get(control_port)[1])
        self.assertIn(b"id=\'refresh-sites\'", get(control_port)[1])
        self.assertEqual(self.manager.status()["root_cert_url"], "http://127.0.0.1:18888/CA")

    def test_plugin_owns_portal_button_and_same_origin_refresh(self):
        portal_port = port()
        portal = host.MultiThreadHTTPServer(
            ("127.0.0.1", portal_port), self.manager.system_handler(False)
        )
        thread = threading.Thread(target=portal.serve_forever, daemon=True)
        thread.start()

        def close_portal():
            portal.shutdown()
            portal.server_close()
            thread.join(timeout=2)

        self.addCleanup(close_portal)
        site_port = port()
        folder = self.root / f"{site_port}-http"
        folder.mkdir()
        (folder / "index.html").write_text("ready", encoding="utf-8")
        self.assertIn(b"id=\'refresh-sites\'", get(portal_port)[1])
        self.assertEqual(self.manager.status()["sites"], [])  # GET never scans.

        def post(origin=None):
            headers = {"X-FolderBridge-Rescan": "1"}
            if origin is not None:
                headers["Origin"] = origin
            conn = http.client.HTTPConnection("127.0.0.1", portal_port, timeout=4)
            try:
                conn.request("POST", "/rescan", body=b"", headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        self.assertEqual(post()[0], 403)
        self.assertEqual(post("https://foreign.example")[0], 403)
        self.assertEqual(self.manager.status()["sites"], [])
        code, raw = post("http://127.0.0.1:18888")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["sites"][0]["port"], site_port)
        self.assertIn(str(site_port).encode(), get(portal_port)[1])

    def test_only_explicit_local_request_rescans_site_directories(self):
        control_port = port()
        control = host.MultiThreadHTTPServer(
            ("127.0.0.1", control_port), self.manager.system_handler(True)
        )
        worker = threading.Thread(target=control.serve_forever, daemon=True)
        worker.start()

        def close_control():
            control.shutdown()
            control.server_close()
            worker.join(timeout=2)

        self.addCleanup(close_control)

        def post(headers):
            conn = http.client.HTTPConnection("127.0.0.1", control_port, timeout=4)
            try:
                conn.request("POST", "/rescan", body=b"", headers=headers)
                reply = conn.getresponse()
                return reply.status, reply.read()
            finally:
                conn.close()

        site_port = port()
        folder = self.root / f"{site_port}-http"
        folder.mkdir()
        index = folder / "index.html"
        index.write_text("first", encoding="utf-8")
        self.assertEqual(json.loads(get(control_port, "/status")[1])["sites"], [])
        self.assertEqual(post({})[0], 403)
        self.assertEqual(post({"Origin": "https://other.invalid", "X-FolderBridge-Rescan": "1"})[0], 403)
        self.assertNotIn(site_port, self.manager.servers)

        code, raw = post({"X-FolderBridge-Rescan": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["sites"][0]["port"], site_port)
        self.assertEqual(get(site_port)[1], b"first")
        index.write_text("updated", encoding="utf-8")
        self.assertEqual(get(site_port)[1], b"updated")  # Never needs another scan.

        index.unlink()
        folder.rmdir()
        self.assertIn(site_port, self.manager.servers)  # No background poll.
        self.assertEqual(post({"X-FolderBridge-Rescan": "1"})[0], 200)
        self.assertNotIn(site_port, self.manager.servers)

    def test_does_not_serve_traversal_or_unapproved_files(self):
        p = port()
        folder = self.root / f"{p}-http"
        folder.mkdir()
        (folder / "index.html").write_text("OK", encoding="utf-8")
        (folder / ".secret").write_text("NO", encoding="utf-8")
        (folder / "untrusted.py").write_text("NO", encoding="utf-8")
        self.manager.scan()
        for req in ("/.secret", "/untrusted.py", "/%2e%2e/private.html", "/..%2fother.html"):
            status, payload, _ = get(p, req)
            self.assertNotEqual(status, 200, req)
            self.assertNotIn(b"NO", payload)
        self.assertEqual(get(p, "/index.html")[1], b"OK")

    def test_invalid_folders_do_not_take_ports(self):
        (self.root / "8-http").mkdir()
        (self.root / "18888-http").mkdir()
        (self.root / "99999-https").mkdir()
        self.manager.scan()
        self.assertFalse(self.manager.servers)
        self.assertTrue(self.manager.errors)

    def test_status_is_plain_json_not_background_spawn(self):
        data = self.manager.status()
        self.assertEqual(data["service_id"], "testonline-host")
        self.assertEqual(data["sites"], [])
        self.assertEqual(self.manager.servers, {})


class FakeProcess:
    def __init__(self):
        self.pid = 43210
        self.returncode = None
        self.waits = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits += 1
        return self.returncode


class FakeRegistry:
    def __init__(self, path):
        self.record = SimpleNamespace(
            bundled=False, path=path, sha256="0" * 64,
            manifest=SimpleNamespace(version="0.1.0")
        )
        self.trust_store = SimpleNamespace(status=lambda record: {"trusted": True, "enabled": True})

    def get(self, name):
        assert name == "testonline-host"
        return self.record


class TestOnlineLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugin"
        self.plugin.mkdir()
        (self.plugin / "server.py").write_text("def serve(root, bind_ip, state_dir): pass\n", encoding="utf-8")
        self.registry = FakeRegistry(self.plugin)
        self.spawns = []
        self.process = FakeProcess()

        def spawn(argv, **kwargs):
            self.spawns.append((argv, kwargs))
            return self.process

        def terminate(p):
            self.assertIs(p, self.process)
            p.returncode = 0

        self.controller = TestOnlineServiceController(
            self.registry,
            config_path=self.root / "state" / "launcher-service.json",
            publish_root=self.root / "TestOnline",
            bind_ip="127.0.0.1",
            popen_factory=spawn,
            terminate_process=terminate,
        )

    def test_controller_is_launcher_registered(self):
        manager = default_managed_service_manager(self.registry)
        self.assertIsInstance(manager.controller("testonline-host"), TestOnlineServiceController)

    def test_autostart_uses_own_folderbridge_worker_not_separate_exe(self):
        live = {"service_id": "testonline-host", "version": "0.1.0", "sites": []}
        status_values = iter([None, live])
        self.controller._health = lambda: next(status_values, live)
        self.controller._port_open = lambda *args: True
        status = self.controller.ensure_auto_started()
        self.assertTrue(status["started"])
        self.assertTrue(status["owned"])
        self.assertEqual(len(self.spawns), 1)
        argv, opts = self.spawns[0]
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        self.assertIn("testonline-service", argv)
        self.assertIn("--extension-path", argv)
        self.assertIn(str(self.plugin), argv)
        self.assertIn("--expected-sha256", argv)
        self.assertIn("--root", argv)
        self.assertNotIn("TestOnline-Host.exe", " ".join(argv))
        self.assertFalse(opts["shell"])
        self.assertEqual(opts["env"]["PYTHONUTF8"], "1")
        self.assertTrue((self.root / "TestOnline").is_dir())
        self.controller._health = lambda: None
        stopped = self.controller.stop()
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.process.waits, 1)

    def test_autostart_can_be_disabled_and_persisted(self):
        self.controller._health = lambda: None
        cfg = self.controller.set_auto_start(False)
        self.assertFalse(cfg.auto_start)
        self.assertFalse(self.controller.config().auto_start)
        out = self.controller.ensure_auto_started()
        self.assertEqual(out["reason"], "auto-start-disabled")
        self.assertFalse(self.spawns)

    def test_unapproved_extension_cannot_launch(self):
        self.registry.trust_store.status = lambda _: {"trusted": False, "enabled": False}
        self.controller._health = lambda: None
        self.controller._port_open = lambda *args: True
        with self.assertRaises(ManagedServiceError):
            self.controller.start()
        self.assertFalse(self.spawns)

    def test_missing_plugin_service_source_is_rejected(self):
        (self.plugin / "server.py").unlink()
        self.controller._health = lambda: None
        self.controller._port_open = lambda *args: True
        with self.assertRaises(TestOnlineServiceError):
            self.controller.start()
        self.assertFalse(self.spawns)

    def test_comfyui_style_service_status_controls_reused(self):
        ui = self.controller.ui_spec()
        self.assertTrue(ui["show_auto_start"])
        self.assertTrue(ui["supports_restart"])
        self.assertTrue(ui["supports_open_status"])
        self.assertFalse(ui["requires_install_root"])

    def test_child_rejects_stale_or_invalid_plugin_hash(self):
        with mock.patch("folderbridge_mcp.testonline_service.load_extension") as loader:
            result = run_testonline_service(str(self.plugin), "not-a-sha", str(self.root),
                                            "127.0.0.1", str(self.root / "state"))
            self.assertEqual(result, 1)
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
