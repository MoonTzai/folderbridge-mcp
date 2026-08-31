from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugin.py"
SPEC = importlib.util.spec_from_file_location("folderbridge_godot_ai_plugin", PLUGIN_PATH)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeMcpHandler(BaseHTTPRequestHandler):
    project_path = ""
    calls: list[dict] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.calls.append(payload)
        method = payload.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Godot AI", "version": "3.2.4"},
            }
        else:
            call = payload["params"]
            name = call["name"]
            arguments = call["arguments"]
            if name == "session_manage":
                data = {
                    "sessions": [{
                        "session_id": "godot@test",
                        "project_path": self.__class__.project_path,
                        "readiness": "ready",
                    }],
                    "count": 1,
                }
                content = [{"type": "text", "text": json.dumps(data)}]
            elif name == "editor_screenshot":
                data = {"source": arguments["source"], "width": 1, "height": 1}
                content = [
                    {"type": "text", "text": json.dumps(data)},
                    {"type": "image", "data": "AA==", "mimeType": "image/png"},
                ]
            else:
                data = {"tool": name, "arguments": arguments}
                content = [{"type": "text", "text": json.dumps(data)}]
            result = {"content": content, "structuredContent": data, "isError": False}
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        body = ("event: message\ndata: " + json.dumps(response) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "fake-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.project = self.workspace / "Godot"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        FakeMcpHandler.project_path = str(self.project)
        FakeMcpHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old_url = plugin.MCP_URL
        plugin.MCP_URL = f"http://127.0.0.1:{self.server.server_port}/mcp"
        self.context = {"workspace_root": str(self.workspace)}

    def tearDown(self) -> None:
        plugin.MCP_URL = self.old_url
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_status_matches_nested_godot_project(self) -> None:
        result = plugin.handle("status", {}, self.context)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session"]["session_id"], "godot@test")

    def test_inspect_editor_pins_matching_session(self) -> None:
        result = plugin.handle("inspect-editor", {}, self.context)
        self.assertEqual(result["godot_tool"], "editor_state")
        call = next(item for item in FakeMcpHandler.calls if item.get("params", {}).get("name") == "editor_state")
        self.assertEqual(call["params"]["arguments"]["session_id"], "godot@test")

    def test_screenshot_forwards_mcp_image_content(self) -> None:
        result = plugin.handle("screenshot", {"source": "viewport_2d"}, self.context)
        self.assertEqual(result["_content"][-1]["type"], "image")
        self.assertEqual(result["_content"][-1]["mimeType"], "image/png")

    def test_open_scene_normalizes_workspace_path(self) -> None:
        plugin.handle("open-scene", {"path": "Godot/scenes/main.tscn"}, self.context)
        call = next(item for item in FakeMcpHandler.calls if item.get("params", {}).get("name") == "scene_open")
        self.assertEqual(call["params"]["arguments"]["path"], "res://scenes/main.tscn")

    def test_resource_path_escape_is_rejected(self) -> None:
        with self.assertRaises(plugin.GodotMcpError):
            plugin.handle("open-scene", {"path": "../outside.tscn"}, self.context)


if __name__ == "__main__":
    unittest.main()
