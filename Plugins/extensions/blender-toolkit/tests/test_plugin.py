from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("blender_toolkit_under_test", ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeBridge(BaseHTTPRequestHandler):
    token = "A" * 64
    calls: list[dict] = []

    def do_POST(self) -> None:
        if self.headers.get("X-FolderBridge-Blender-Token") != self.token:
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.calls.append(payload)
        action = payload["action"]
        if action == "status":
            result = {
                "bridge_version": "0.1.1",
                "blender_version": "5.2.1",
                "comfyui_blender_enabled": True,
                "comfyui_panels_registered": ["ComfyBlenderPanelWorkflow"],
                "comfyui_n_panel_present": True,
            }
        elif action == "screenshot":
            result = {
                "mime_type": "image/png",
                "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
                "captured_bytes": 8,
            }
        elif action in {"save-blend", "export-asset", "render-still"}:
            result = {"workspace_artifact": payload["params"]["path"]}
        else:
            result = {"echo": payload}
        body = json.dumps({"ok": True, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class Context(dict):
    def __init__(self, root: Path, *, read_only: bool = False):
        super().__init__(workspace_root=str(root), workspace_read_only=read_only)


class BlenderToolkitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scene.blend").write_bytes(b"BLENDER")
        (self.root / "asset.obj").write_text("o x\n", encoding="utf-8")
        self.token = self.root / "token.txt"
        self.token.write_text(FakeBridge.token, encoding="ascii")
        self.old_token = plugin.TOKEN_PATH
        self.old_url = plugin.BRIDGE_URL
        plugin.TOKEN_PATH = self.token
        FakeBridge.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        plugin.BRIDGE_URL = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        plugin.TOKEN_PATH = self.old_token
        plugin.BRIDGE_URL = self.old_url
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_manifest_has_broad_but_non_arbitrary_surface(self) -> None:
        manifest = json.loads((ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["runtime_abi"], 1)
        self.assertEqual(manifest["id"], "blender-toolkit")
        self.assertEqual(
            set(manifest["permissions"]),
            {
                "network.loopback:127.0.0.1:8766",
                "workspace.read",
                "workspace.write",
                "process.execute:blender.exe",
            },
        )
        self.assertGreaterEqual(len(manifest["actions"]), 35)
        for forbidden in ("run-script", "eval", "exec", "shell", "run-command"):
            self.assertNotIn(forbidden, manifest["actions"])
        self.assertIn("operator-call", manifest["actions"])
        self.assertIn("node-interface-socket", manifest["actions"])
        self.assertIn("render-animation", manifest["actions"])

    def test_status_reports_live_comfyui_n_panel(self) -> None:
        result = plugin.handle("status", {}, {})
        self.assertTrue(result["bridge_online"])
        self.assertTrue(result["comfyui_n_panel_present"])
        self.assertTrue(result["comfyui_blender_enabled"])

    def test_workspace_relative_read_path_is_forwarded(self) -> None:
        result = plugin.handle("open-blend", {"path": "scene.blend"}, Context(self.root))
        self.assertTrue(result["ok"])
        call = FakeBridge.calls[-1]
        self.assertEqual(call["workspace_root"], str(self.root.resolve()))
        self.assertEqual(call["params"]["path"], "scene.blend")

    def test_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        ctx = Context(self.root)
        for value in ("../scene.blend", "/scene.blend", r"C:\scene.blend", r"dir\scene.blend", ".git/x.blend"):
            with self.subTest(value=value), self.assertRaises(plugin.ExtensionError):
                plugin.handle("open-blend", {"path": value}, ctx)

    def test_read_only_workspace_rejects_mutating_scene_action(self) -> None:
        with self.assertRaises(plugin.ExtensionError):
            plugin.handle(
                "set-transform",
                {"name": "Cube", "location": [1, 2, 3]},
                Context(self.root, read_only=True),
            )

    def test_operator_surface_denies_file_and_script_like_calls(self) -> None:
        ctx = Context(self.root)
        for op in ("wm.open_mainfile", "script.reload", "object.file_export"):
            with self.subTest(op=op), self.assertRaises(plugin.ExtensionError):
                plugin.handle("operator-call", {"operator": op, "properties": {}}, ctx)
        with self.assertRaises(plugin.ExtensionError):
            plugin.handle(
                "operator-call",
                {"operator": "object.modifier_add", "properties": {"filepath": "x"}},
                ctx,
            )

    def test_safe_operator_is_forwarded(self) -> None:
        result = plugin.handle(
            "operator-call",
            {"operator": "mesh.primitive_cube_add", "properties": {"size": 2.0}},
            Context(self.root),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(FakeBridge.calls[-1]["action"], "operator-call")

    def test_screenshot_emits_mcp_image_content(self) -> None:
        result = plugin.handle("screenshot", {"max_width": 640}, Context(self.root))
        self.assertEqual(result["_content"][-1]["type"], "image")
        self.assertEqual(result["_content"][-1]["mimeType"], "image/png")

    def test_save_blend_declares_workspace_artifact(self) -> None:
        result = plugin.handle("save-blend", {"path": "shots/test.blend"}, Context(self.root))
        self.assertEqual(result["workspace_artifacts"][0]["path"], "shots/test.blend")


if __name__ == "__main__":
    unittest.main()
