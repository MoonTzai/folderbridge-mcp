from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from folderbridge_mcp.flight_recorder import FlightRecorder, classify_tunnel_output
from folderbridge_mcp.mcp import McpServer


class _Runtime:
    identity = {"name": "folderbridge", "title": "FolderBridge MCP", "version": "test"}
    instructions = "test"

    def list_tools(self):
        return []

    def call(self, name, arguments):
        return {"structuredContent": {"ok": True, "name": name}}

    def begin_shutdown(self):
        pass

    def close(self):
        pass


class _BrokenDestination(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError("diagnostic broken pipe")


class FlightRecorderTests(unittest.TestCase):
    def test_records_compact_metadata_and_redacts_secrets(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            recorder = FlightRecorder("mcp", root=Path(temporary), clock=lambda: now[0])
            self.assertTrue(recorder.record(
                "mcp.request",
                tool="workspace",
                action="read",
                workspace_id="abc123",
                text="Authorization: Bearer TOPSECRET sk-ABCDEFGH123456",
            ))
            result = recorder.recent(minutes=15, limit=10)
            self.assertEqual(result["returned"], 1)
            event = result["events"][0]
            self.assertEqual(event["tool"], "workspace")
            self.assertEqual(event["action"], "read")
            rendered = json.dumps(event, ensure_ascii=False)
            self.assertNotIn("TOPSECRET", rendered)
            self.assertNotIn("sk-ABCDEFGH123456", rendered)
            self.assertIn("<redacted>", rendered)

    def test_recent_is_time_bounded(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            recorder = FlightRecorder("mcp", root=Path(temporary), clock=lambda: now[0])
            recorder.record("mcp.request", tool="old")
            now[0] = 2_000.0
            recorder.record("mcp.request", tool="new")
            result = recorder.recent(minutes=15, limit=10)
            self.assertEqual([event["tool"] for event in result["events"]], ["new"])

    def test_cleanup_enforces_total_byte_cap(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            recorder = FlightRecorder(
                "mcp",
                root=Path(temporary),
                clock=lambda: now[0],
                max_total_bytes=1_200,
            )
            for index in range(6):
                now[0] += 61
                recorder.record("mcp.error", severity="error", text=("x" * 500), index=index)
            stats = recorder.cleanup()
            self.assertLessEqual(stats["total_bytes"], 1_200)
            self.assertGreater(stats["deleted_files"], 0)

    def test_export_recent_jsonl_exports_full_window_across_roles_and_keeps_redaction(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            root = Path(temporary)
            mcp = FlightRecorder("mcp", root=root, clock=lambda: now[0])
            launcher = FlightRecorder("launcher", root=root, clock=lambda: now[0])
            for index in range(220):
                mcp.record("mcp.request", tool="workspace", index=index, text="token=TOPSECRET")
            for index in range(5):
                launcher.record("tunnel.warning", severity="warning", index=index)

            preview = mcp.recent(minutes=15, limit=200)
            self.assertTrue(preview["truncated"])
            exported = mcp.export_recent_jsonl(minutes=15)
            lines = exported["data"].splitlines()
            self.assertEqual(exported["event_count"], 225)
            self.assertEqual(len(lines), 225)
            events = [json.loads(line) for line in lines]
            self.assertEqual({event["role"] for event in events}, {"mcp", "launcher"})
            rendered = exported["data"].decode("utf-8")
            self.assertNotIn("TOPSECRET", rendered)
            self.assertIn("<redacted>", rendered)

    def test_export_recent_jsonl_ignores_malformed_pid_for_sorting(self) -> None:
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            recorder = FlightRecorder("mcp", root=Path(temporary), clock=lambda: now[0])
            recorder.root.mkdir(parents=True, exist_ok=True)
            path = recorder.root / "mcp-corrupt-19700101T0016.jsonl"
            path.write_text(
                json.dumps({
                    "schema": 1,
                    "ts_unix": 1000.0,
                    "role": "mcp",
                    "pid": "not-a-number",
                    "event": "mcp.request",
                    "severity": "info",
                }) + "\n",
                encoding="utf-8",
            )
            now[0] = 1001.0
            exported = recorder.export_recent_jsonl(minutes=15)
            self.assertEqual(exported["event_count"], 1)
            event = json.loads(exported["data"].splitlines()[0])
            self.assertEqual(event["pid"], "not-a-number")

    def test_record_failure_is_best_effort_and_never_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            bad_root = Path(temporary) / "not-a-directory"
            bad_root.write_text("x", encoding="utf-8")
            recorder = FlightRecorder("mcp", root=bad_root)
            self.assertFalse(recorder.record("mcp.request", tool="workspace"))
            self.assertGreaterEqual(recorder.dropped_events, 1)

    def test_tunnel_output_classifier_only_promotes_diagnostics(self) -> None:
        self.assertIsNone(classify_tunnel_output("connected successfully"))
        self.assertEqual(classify_tunnel_output("ExceptionGroup: TaskGroup failed"), "error")
        self.assertEqual(classify_tunnel_output("warning: retrying connection"), "warning")

    def test_mcp_records_ingress_and_completion_without_arguments(self) -> None:
        with TemporaryDirectory() as temporary:
            recorder = FlightRecorder("mcp", root=Path(temporary))
            server = McpServer(_Runtime(), flight_recorder=recorder)
            request = {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "workspace",
                    "arguments": {"workspace_id": "abc123", "action": "read", "path": "DO_NOT_RECORD_THIS.txt"},
                },
            }
            source = io.BytesIO(json.dumps(request).encode("utf-8") + b"\n")
            destination = io.BytesIO()
            server.serve(source, destination)
            result = recorder.recent(minutes=15, limit=20)
            events = result["events"]
            self.assertTrue(any(event["event"] == "mcp.request" for event in events))
            self.assertTrue(any(event["event"] == "mcp.complete" for event in events))
            rendered = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("DO_NOT_RECORD_THIS.txt", rendered)
            request_event = next(event for event in events if event["event"] == "mcp.request")
            self.assertEqual(request_event["tool"], "workspace")
            self.assertEqual(request_event["action"], "read")
            self.assertEqual(request_event["workspace_id"], "abc123")

    def test_mcp_write_failure_is_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            recorder = FlightRecorder("mcp", root=Path(temporary))
            server = McpServer(_Runtime(), flight_recorder=recorder)
            request = {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}
            server.serve(io.BytesIO(json.dumps(request).encode("utf-8") + b"\n"), _BrokenDestination())
            result = recorder.recent(minutes=15, limit=20, errors_only=True)
            self.assertTrue(any(event["event"] == "mcp.write_error" for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
