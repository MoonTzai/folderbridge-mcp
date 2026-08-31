from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.cli import build_parser
from folderbridge_mcp.config import load_config
from folderbridge_mcp.concurrency import (
    CONTROL_MAX_INFLIGHT,
    CONTROL_WORKERS,
    DATA_MAX_INFLIGHT,
    DATA_WORKERS,
)
from folderbridge_mcp.extensions import MAX_FOREGROUND_EXTENSION_WORKERS
from folderbridge_mcp.mcp import MAX_MESSAGE_BYTES, McpServer
from folderbridge_mcp.security import MAX_EDIT_TEXT_BYTES
from folderbridge_mcp.text_writes import (
    MAX_TRANSACTION_CHUNK_BYTES,
    MAX_TRANSACTION_TEXT_BYTES,
    TRANSACTION_TTL_SECONDS,
)
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
        self.assertEqual(
            [tool["name"] for tool in tools["result"]["tools"]],
            ["server_info", "flight_recorder", "workspace", "file_info", "pptx_inspect", "image_open", "extension", "edit_file", "write_file"],
        )
        flight = self.call(20, "tools/call", {"name": "flight_recorder", "arguments": {"action": "status"}})
        self.assertTrue(flight["result"]["structuredContent"]["enabled"])
        self.assertEqual(flight["result"]["structuredContent"]["window_minutes"], 15)
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

    def test_file_info_sha_drives_one_hundred_mib_exact_edit_over_stdio(self) -> None:
        large_path = self.workspace / "large-edit.txt"
        digest = hashlib.sha256()
        with large_path.open("wb") as handle:
            block = b"A" * (1024 * 1024)
            for _ in range(100):
                handle.write(block)
                digest.update(block)
            tail = b"\nneedle\n"
            handle.write(tail)
            digest.update(tail)
        self.assertGreater(large_path.stat().st_size, 100 * 1024 * 1024)
        self.call(
            10,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        info = self.call(
            11,
            "tools/call",
            {"name": "file_info", "arguments": {"path": "large-edit.txt"}},
        )["result"]["structuredContent"]
        self.assertEqual(info["sha256"], digest.hexdigest())
        edited = self.call(
            12,
            "tools/call",
            {
                "name": "edit_file",
                "arguments": {
                    "path": "large-edit.txt",
                    "expected_sha256": info["sha256"],
                    "replacements": [{"old": "needle", "new": "replacement"}],
                },
            },
        )
        self.assertFalse(edited["result"]["isError"])
        with large_path.open("rb") as handle:
            handle.seek(-64, 2)
            tail_bytes = handle.read()
        self.assertTrue(tail_bytes.endswith(b"\nreplacement\n"))

    def test_transactional_write_creates_one_hundred_mib_file_over_stdio(self) -> None:
        self.call(
            20,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        begin = self.call(
            21,
            "tools/call",
            {"name": "write_file", "arguments": {"action": "begin", "path": "large.txt", "mode": "create"}},
        )
        started = begin["result"]["structuredContent"]
        self.assertTrue(started["ok"])
        transaction_id = started["transaction_id"]
        chunk = "C" * MAX_TRANSACTION_CHUNK_BYTES
        chunk_bytes = chunk.encode("utf-8")
        chunk_count = (100 * 1024 * 1024) // len(chunk_bytes)
        self.assertEqual(chunk_count * len(chunk_bytes), 100 * 1024 * 1024)
        digest = hashlib.sha256()
        offset = 0
        for index in range(chunk_count):
            appended = self.call(
                22 + index,
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {
                        "action": "append",
                        "transaction_id": transaction_id,
                        "offset": offset,
                        "chunk": chunk,
                    },
                },
            )
            self.assertFalse(appended["result"]["isError"])
            digest.update(chunk_bytes)
            offset = appended["result"]["structuredContent"]["received_bytes"]
        self.assertEqual(offset, 100 * 1024 * 1024)
        status = self.call(
            900,
            "tools/call",
            {"name": "write_file", "arguments": {"action": "status", "transaction_id": transaction_id}},
        )
        self.assertEqual(status["result"]["structuredContent"]["received_bytes"], offset)
        duplicate = self.call(
            901,
            "tools/call",
            {
                "name": "write_file",
                "arguments": {"action": "append", "transaction_id": transaction_id, "offset": 0, "chunk": "duplicate"},
            },
        )
        self.assertTrue(duplicate["result"]["isError"])
        self.assertEqual(duplicate["result"]["structuredContent"]["error"]["code"], "OFFSET_MISMATCH")
        expected_sha256 = digest.hexdigest()
        committed = self.call(
            902,
            "tools/call",
            {
                "name": "write_file",
                "arguments": {
                    "action": "commit",
                    "transaction_id": transaction_id,
                    "expected_size": offset,
                    "expected_sha256": expected_sha256,
                },
            },
        )
        self.assertFalse(committed["result"]["isError"])
        info = self.call(
            903,
            "tools/call",
            {"name": "file_info", "arguments": {"path": "large.txt"}},
        )["result"]["structuredContent"]
        self.assertEqual(info["size"], 100 * 1024 * 1024)
        self.assertEqual(info["sha256"], expected_sha256)

    def test_transactional_replace_one_hundred_mib_multibyte_utf8_file_over_stdio(self) -> None:
        target = self.workspace / "large-replace.txt"
        original_digest = hashlib.sha256()
        with target.open("wb") as handle:
            block = b"O" * (1024 * 1024)
            for _ in range(100):
                handle.write(block)
                original_digest.update(block)
        self.assertEqual(target.stat().st_size, 100 * 1024 * 1024)
        self.call(
            40,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        info = self.call(
            41,
            "tools/call",
            {"name": "file_info", "arguments": {"path": "large-replace.txt"}},
        )["result"]["structuredContent"]
        self.assertEqual(info["sha256"], original_digest.hexdigest())

        begin = self.call(
            42,
            "tools/call",
            {
                "name": "write_file",
                "arguments": {
                    "action": "begin",
                    "path": "large-replace.txt",
                    "mode": "replace",
                    "expected_target_sha256": info["sha256"],
                },
            },
        )["result"]["structuredContent"]
        self.assertTrue(begin["ok"])
        transaction_id = begin["transaction_id"]

        chunk = "新" * 43_000
        chunk_bytes = chunk.encode("utf-8")
        self.assertLessEqual(len(chunk_bytes), MAX_TRANSACTION_CHUNK_BYTES)
        chunk_count = ((100 * 1024 * 1024) + len(chunk_bytes) - 1) // len(chunk_bytes)
        digest = hashlib.sha256()
        offset = 0
        for index in range(chunk_count):
            appended = self.call(
                43 + index,
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {
                        "action": "append",
                        "transaction_id": transaction_id,
                        "offset": offset,
                        "chunk": chunk,
                    },
                },
            )
            self.assertFalse(appended["result"]["isError"])
            digest.update(chunk_bytes)
            offset = appended["result"]["structuredContent"]["received_bytes"]
        self.assertGreaterEqual(offset, 100 * 1024 * 1024)
        expected_sha256 = digest.hexdigest()

        committed = self.call(
            950,
            "tools/call",
            {
                "name": "write_file",
                "arguments": {
                    "action": "commit",
                    "transaction_id": transaction_id,
                    "expected_size": offset,
                    "expected_sha256": expected_sha256,
                },
            },
        )
        self.assertFalse(committed["result"]["isError"])
        result = committed["result"]["structuredContent"]
        self.assertEqual(result["size"], offset)
        self.assertEqual(result["sha256"], expected_sha256)
        final_info = self.call(
            951,
            "tools/call",
            {"name": "file_info", "arguments": {"path": "large-replace.txt"}},
        )["result"]["structuredContent"]
        self.assertEqual(final_info["size"], offset)
        self.assertEqual(final_info["sha256"], expected_sha256)

    def test_transaction_chunk_bound_fits_worst_case_json_escape_inside_mcp_message(self) -> None:
        self.assertEqual(MAX_MESSAGE_BYTES, 1024 * 1024)
        request = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {
                    "action": "append",
                    "transaction_id": "a" * 32,
                    "offset": 0,
                    "chunk": "\x01" * MAX_TRANSACTION_CHUNK_BYTES,
                },
            },
        }
        encoded = json.dumps(request).encode("utf-8") + b"\n"
        self.assertLessEqual(len(encoded), MAX_MESSAGE_BYTES)

    def test_writable_runtime_lazily_initializes_text_write_staging(self) -> None:
        runtime = ToolRuntime(self.workspace, load_config(self.workspace), read_only=False)
        self.assertIsNone(runtime.text_writes)
        self.assertIn("write_file", [tool["name"] for tool in runtime.list_tools()])
        invalid = runtime.call("write_file", {"action": "not-an-action"})["structuredContent"]
        self.assertFalse(invalid["ok"])
        self.assertIsNone(runtime.text_writes)
        unknown = runtime.call("write_file", {"action": "status", "transaction_id": "missing"})["structuredContent"]
        self.assertEqual(unknown["error"]["code"], "UNKNOWN_TRANSACTION")
        self.assertIsNone(runtime.text_writes)
        started = runtime.call(
            "write_file",
            {"action": "begin", "path": "lazy.txt", "mode": "create"},
        )["structuredContent"]
        self.assertTrue(started["ok"])
        self.assertIsNotNone(runtime.text_writes)
        runtime.call(
            "write_file",
            {"action": "abort", "transaction_id": started["transaction_id"]},
        )

    def test_read_only_runtime_does_not_initialize_or_expose_text_writes(self) -> None:
        runtime = ToolRuntime(self.workspace, load_config(self.workspace), read_only=True)
        self.assertIsNone(runtime.text_writes)
        names = [tool["name"] for tool in runtime.list_tools()]
        self.assertNotIn("edit_file", names)
        self.assertNotIn("write_file", names)

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

    def test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body(self) -> None:
        runtime = ToolRuntime(self.workspace, load_config(self.workspace))
        instructions = runtime.instructions
        self.assertIn("skill-engine", instructions)
        self.assertIn("match", instructions)
        self.assertIn("folderbridge-engineering/diagnosing-bugs", instructions)
        self.assertNotIn("the first deliverable is a tight feedback loop", instructions)
        self.assertLessEqual(len(instructions), 5000)

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
        self.assertRegex(info["version"], r"^\d+\.\d+\.\d+$")
        concurrency = info["mcp_concurrency"]
        self.assertEqual(concurrency["control_workers"], CONTROL_WORKERS)
        self.assertEqual(concurrency["control_max_inflight"], CONTROL_MAX_INFLIGHT)
        self.assertEqual(concurrency["data_workers"], DATA_WORKERS)
        self.assertEqual(concurrency["data_max_inflight"], DATA_MAX_INFLIGHT)
        self.assertEqual(concurrency["busy_policy"], "fail-fast")
        self.assertTrue(concurrency["stdout_serialized"])
        self.assertGreaterEqual(info["extension_jobs"]["max_running"], 1)
        self.assertTrue(info["extension_jobs"]["process_local"])
        self.assertFalse(info["extension_jobs"]["survives_server_restart"])
        self.assertTrue(info["extension_jobs"]["independent_from_mcp_request_workers"])
        self.assertEqual(info["extension_workers"]["max_foreground"], MAX_FOREGROUND_EXTENSION_WORKERS)
        self.assertTrue(info["extension_workers"]["holds_workspace_lease_until_process_exit"])
        self.assertEqual(info["extension_workers"]["termination_pending_status"], "termination_pending")
        self.assertEqual(info["mcp_concurrency"]["shutdown_mutation_admission"], "close-and-wake-waiters")
        write_security = info["security"]["transactional_text_writes"]
        self.assertEqual(info["security"]["exact_edit_max_bytes"], MAX_EDIT_TEXT_BYTES)
        self.assertEqual(write_security["max_chunk_bytes"], MAX_TRANSACTION_CHUNK_BYTES)
        self.assertEqual(write_security["max_file_bytes"], MAX_TRANSACTION_TEXT_BYTES)
        self.assertEqual(write_security["stale_cleanup_seconds"], TRANSACTION_TTL_SECONDS)
        self.assertTrue(write_security["process_local"])
        self.assertFalse(write_security["survives_server_restart"])
        self.assertTrue(write_security["mcp_message_limit_unchanged"])
        self.assertEqual(info["skill_engine"]["gateway"], "extension/skill-engine")
        self.assertEqual(info["skill_engine"]["automatic_invocation"], "model-routed-not-forced")
        self.assertIn("folderbridge-engineering", {item["id"] for item in info["skill_engine"]["packs"]})
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

    def test_skills_cli_reports_bundled_pack_as_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(LAUNCHER), "skills", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        payload = json.loads(completed.stdout)
        self.assertIn("folderbridge-engineering", {item["id"] for item in payload["packs"]})

    def test_cli_preserves_repeated_workspace_arguments(self) -> None:
        parsed = build_parser().parse_args(
            ["serve", "--workspace", str(self.workspace), "--workspace", str(self.workspace.parent / "other"), "--read-only"]
        )

        self.assertEqual(parsed.workspaces, [str(self.workspace), str(self.workspace.parent / "other")])
        self.assertTrue(parsed.read_only)

    def test_cli_preserves_global_capability_flags(self) -> None:
        parsed = build_parser().parse_args(
            [
                "serve",
                "--workspace",
                str(self.workspace),
                "--capability",
                "package-windows",
                "--capability",
                "git-push",
            ]
        )
        self.assertEqual(parsed.capabilities, ["package-windows", "git-push"])


if __name__ == "__main__":
    unittest.main()

