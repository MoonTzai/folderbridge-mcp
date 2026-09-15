from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import folderbridge_mcp.tools as tools_module
from folderbridge_mcp.config import workspace_id
from folderbridge_mcp.tools import ToolRuntime


class CoreFileOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.source_root = base / "source"
        self.destination_root = base / "destination"
        self.source_root.mkdir()
        self.destination_root.mkdir()
        self.runtime = ToolRuntime.from_roots([self.source_root, self.destination_root])
        self.source_id = workspace_id(self.source_root)
        self.destination_id = workspace_id(self.destination_root)

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def call(self, **arguments):
        return self.runtime.call("file_ops", arguments)["structuredContent"]

    def test_tool_is_core_write_surface_and_requires_source_selector_for_multi_workspace(self) -> None:
        tool = next(item for item in self.runtime.list_tools() if item["name"] == "file_ops")
        self.assertIn("source_workspace_id", tool["inputSchema"]["required"])
        self.assertIn("destination_workspace_id", tool["inputSchema"]["properties"])
        info = self.runtime.call("server_info", {})["structuredContent"]
        self.assertIn("file_ops", info["builtin_tools"])
        self.assertTrue(info["security"]["file_ops_cross_workspace"])

    def test_cross_workspace_copy_preserves_source_and_exact_bytes(self) -> None:
        body = bytes(range(256)) * 4096
        digest = hashlib.sha256(body).hexdigest()
        (self.source_root / "source.bin").write_bytes(body)
        result = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id=self.destination_id,
            destination_path="nested/copied.bin",
            expected_source_sha256=digest,
            create_parents=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_workspace_id"], self.source_id)
        self.assertEqual(result["destination_workspace_id"], self.destination_id)
        self.assertEqual(result["sha256"], digest)
        self.assertEqual((self.source_root / "source.bin").read_bytes(), body)
        self.assertEqual((self.destination_root / "nested" / "copied.bin").read_bytes(), body)

    def test_cross_workspace_move_is_verified_copy_delete(self) -> None:
        body = b"cross-workspace-move" * 8192
        digest = hashlib.sha256(body).hexdigest()
        (self.source_root / "source.bin").write_bytes(body)
        result = self.call(
            action="move",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id=self.destination_id,
            destination_path="moved.bin",
            expected_source_sha256=digest,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["strategy"], "verified-copy-delete")
        self.assertEqual(result["sha256"], digest)
        self.assertFalse((self.source_root / "source.bin").exists())
        self.assertEqual((self.destination_root / "moved.bin").read_bytes(), body)

    def test_destination_workspace_defaults_to_source_workspace(self) -> None:
        (self.source_root / "source.bin").write_bytes(b"same-workspace")
        result = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_path="copy.bin",
        )
        self.assertEqual(result["destination_workspace_id"], self.source_id)
        self.assertEqual((self.source_root / "copy.bin").read_bytes(), b"same-workspace")

    def test_unknown_destination_workspace_fails_before_write(self) -> None:
        (self.source_root / "source.bin").write_bytes(b"x")
        result = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id="ffffffffffff",
            destination_path="never.bin",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_WORKSPACE")
        self.assertFalse((self.destination_root / "never.bin").exists())

    def test_overwrite_requires_exact_current_destination_hash(self) -> None:
        (self.source_root / "source.bin").write_bytes(b"new")
        (self.destination_root / "target.bin").write_bytes(b"old")
        denied = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id=self.destination_id,
            destination_path="target.bin",
            overwrite=True,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual((self.destination_root / "target.bin").read_bytes(), b"old")

        old_sha = hashlib.sha256(b"old").hexdigest()
        allowed = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id=self.destination_id,
            destination_path="target.bin",
            overwrite=True,
            expected_destination_sha256=old_sha,
        )
        self.assertTrue(allowed["ok"])
        self.assertEqual((self.destination_root / "target.bin").read_bytes(), b"new")

    def test_protected_destination_config_remains_denied(self) -> None:
        (self.source_root / "source.bin").write_bytes(b"x")
        result = self.call(
            action="copy",
            source_workspace_id=self.source_id,
            source_path="source.bin",
            destination_workspace_id=self.destination_id,
            destination_path=".folderbridge.json",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PROTECTED_CONFIG")

    def test_cross_workspace_operation_holds_both_workspace_mutation_leases(self) -> None:
        (self.source_root / "source.txt").write_text("source", encoding="utf-8")
        (self.destination_root / "target.txt").write_text("target", encoding="utf-8")
        source_sha = hashlib.sha256(b"source").hexdigest()
        target_sha = hashlib.sha256(b"target").hexdigest()
        entered = threading.Event()
        release = threading.Event()

        def blocked_copy(*args, **kwargs):
            entered.set()
            release.wait(timeout=1)
            return {
                "operation": "copy",
                "already_completed": False,
                "source_path": "source.txt",
                "destination_path": "target.txt",
                "size": 6,
                "sha256": source_sha,
            }

        with patch.object(tools_module, "copy_file", side_effect=blocked_copy), patch.object(
            tools_module, "WORKSPACE_MUTATION_WAIT_SECONDS", 0.05
        ):
            with ThreadPoolExecutor(max_workers=3) as executor:
                running = executor.submit(
                    self.runtime.call,
                    "file_ops",
                    {
                        "action": "copy",
                        "source_workspace_id": self.source_id,
                        "source_path": "source.txt",
                        "destination_workspace_id": self.destination_id,
                        "destination_path": "target.txt",
                        "overwrite": True,
                        "expected_destination_sha256": target_sha,
                    },
                )
                self.assertTrue(entered.wait(timeout=0.5))
                source_edit = executor.submit(
                    self.runtime.call,
                    "edit_file",
                    {
                        "workspace_id": self.source_id,
                        "path": "source.txt",
                        "expected_sha256": source_sha,
                        "replacements": [{"old": "source", "new": "changed-source"}],
                    },
                )
                destination_edit = executor.submit(
                    self.runtime.call,
                    "edit_file",
                    {
                        "workspace_id": self.destination_id,
                        "path": "target.txt",
                        "expected_sha256": target_sha,
                        "replacements": [{"old": "target", "new": "changed-target"}],
                    },
                )
                source_result = source_edit.result(timeout=1)["structuredContent"]
                destination_result = destination_edit.result(timeout=1)["structuredContent"]
                self.assertEqual(source_result["error"]["code"], "WORKSPACE_BUSY")
                self.assertEqual(destination_result["error"]["code"], "WORKSPACE_BUSY")
                release.set()
                self.assertTrue(running.result(timeout=1)["structuredContent"]["ok"])

    def test_read_only_runtime_does_not_advertise_file_ops(self) -> None:
        read_only = ToolRuntime.from_roots([self.source_root, self.destination_root], read_only=True)
        try:
            self.assertNotIn("file_ops", [item["name"] for item in read_only.list_tools()])
        finally:
            read_only.close()


if __name__ == "__main__":
    unittest.main()
