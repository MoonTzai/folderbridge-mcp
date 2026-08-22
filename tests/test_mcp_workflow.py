from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.cli import build_parser
from folderbridge_mcp.config import load_config
from folderbridge_mcp.mcp import McpServer
from folderbridge_mcp.tools import ToolRuntime


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "folderbridge_launcher.py"


class McpWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "repo"
        self.workspace.mkdir()
        (self.workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "serve", "--workspace", str(self.workspace)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdin is not None and self.process.stdout is not None

    def tearDown(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stderr:
            self.process.stderr.close()
        self.temporary.cleanup()

    def call(self, request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
        assert self.process.stdin is not None and self.process.stdout is not None
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        self.assertTrue(line, "server closed before responding")
        return json.loads(line)

    def test_read_edit_review_workflow(self) -> None:
        initialized = self.call(
            1,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "folderbridge")
        tools = self.call(2, "tools/list", {})
        self.assertEqual([tool["name"] for tool in tools["result"]["tools"]], ["server_info", "workspace", "edit_file"])
        read = self.call(3, "tools/call", {"name": "workspace", "arguments": {"action": "read", "path": "calc.py"}})
        content = read["result"]["structuredContent"]
        self.assertIn("return a - b", content["text"])
        edited = self.call(
            4,
            "tools/call",
            {
                "name": "edit_file",
                "arguments": {
                    "path": "calc.py",
                    "expected_sha256": content["sha256"],
                    "replacements": [{"old": "return a - b", "new": "return a + b"}],
                },
            },
        )
        self.assertFalse(edited["result"]["isError"])
        reread = self.call(5, "tools/call", {"name": "workspace", "arguments": {"action": "read", "path": "calc.py"}})
        self.assertIn("return a + b", reread["result"]["structuredContent"]["text"])

    def test_modern_discovery_and_catalog(self) -> None:
        meta = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }
        discovery = self.call(10, "server/discover", meta)
        self.assertEqual(discovery["result"]["supportedVersions"], ["2026-07-28"])
        self.assertEqual(discovery["result"]["resultType"], "complete")
        catalog = self.call(11, "tools/list", meta)
        self.assertEqual(catalog["result"]["cacheScope"], "private")

    def test_tool_notifications_cannot_silently_write(self) -> None:
        server = McpServer(ToolRuntime(self.workspace, load_config(self.workspace)))
        notification = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "edit_file",
                "arguments": {"path": "silent.txt", "create_content": "should not exist"},
            },
        }
        self.assertIsNone(server.dispatch(notification))
        self.assertFalse((self.workspace / "silent.txt").exists())
        null_id = dict(notification, id=None)
        response = server.dispatch(null_id)
        self.assertIn("error", response)
        self.assertFalse((self.workspace / "silent.txt").exists())

    def test_multiple_workspaces_require_an_explicit_selector(self) -> None:
        second = Path(self.temporary.name) / "notes"
        second.mkdir()
        (second / "only-here.txt").write_text("second workspace", encoding="utf-8")
        runtime = ToolRuntime.from_roots((self.workspace, second))

        info = runtime.call("server_info", {})["structuredContent"]
        self.assertEqual(info["workspace_count"], 2)
        ids = {item["name"]: item["workspace_id"] for item in info["workspaces"]}

        missing = runtime.call("workspace", {"action": "list"})
        self.assertEqual(missing["structuredContent"]["error"]["code"], "WORKSPACE_REQUIRED")

        read = runtime.call(
            "workspace",
            {"workspace_id": ids["notes"], "action": "read", "path": "only-here.txt"},
        )
        self.assertIn("second workspace", read["structuredContent"]["text"])

        wrong_workspace = runtime.call(
            "workspace",
            {"workspace_id": ids["repo"], "action": "read", "path": "only-here.txt"},
        )
        self.assertTrue(wrong_workspace["isError"])

        workspace_tool = next(tool for tool in runtime.list_tools() if tool["name"] == "workspace")
        self.assertIn("workspace_id", workspace_tool["inputSchema"]["required"])

    def test_cli_preserves_repeated_workspace_arguments(self) -> None:
        parsed = build_parser().parse_args(
            ["serve", "--workspace", str(self.workspace), "--workspace", str(self.workspace.parent / "other"), "--read-only"]
        )

        self.assertEqual(parsed.workspaces, [str(self.workspace), str(self.workspace.parent / "other")])
        self.assertTrue(parsed.read_only)


if __name__ == "__main__":
    unittest.main()
