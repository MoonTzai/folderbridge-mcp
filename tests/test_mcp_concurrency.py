from __future__ import annotations

import gc
import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from folderbridge_mcp.config import load_config
from folderbridge_mcp.concurrency import WorkspaceMutationGate
from folderbridge_mcp.mcp import McpServer
from folderbridge_mcp.text_writes import TextWriteManager
from folderbridge_mcp.tools import ToolRuntime
import folderbridge_mcp.text_writes as text_writes
import folderbridge_mcp.tools as tools_module


class _FakeRuntime:
    identity = {"name": "folderbridge", "title": "FolderBridge MCP", "version": "test"}
    instructions = "test"

    def __init__(self) -> None:
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()
        self.active_data = 0
        self.max_active_data = 0
        self.guard = threading.Lock()
        self.shutdown_started = False
        self.closed = False

    def begin_shutdown(self) -> None:
        self.shutdown_started = True
        self.release_slow.set()

    def close(self) -> None:
        self.closed = True

    def list_tools(self):
        return []

    def call(self, name, arguments):
        if name == "server_info":
            self.release_slow.set()
            return {"structuredContent": {"ok": True, "kind": "control"}}
        if name == "slow":
            with self.guard:
                self.active_data += 1
                self.max_active_data = max(self.max_active_data, self.active_data)
            self.slow_started.set()
            self.release_slow.wait(timeout=1.0)
            with self.guard:
                self.active_data -= 1
            return {"structuredContent": {"ok": True, "kind": "data"}}
        return {"structuredContent": {"ok": True}}


class _GuardedDestination(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self._state_lock = threading.Lock()
        self._writing = False
        self.concurrent_write_detected = False

    def write(self, data):
        with self._state_lock:
            if self._writing:
                self.concurrent_write_detected = True
            self._writing = True
        try:
            time.sleep(0.002)
            return super().write(data)
        finally:
            with self._state_lock:
                self._writing = False


class McpConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        # GUI regression tests run earlier in the same unittest process. Collect
        # any destroyed Tk wrappers on the main thread before worker threads
        # start, so their delayed finalizers cannot fire from a test worker.
        gc.collect()

    def _line(self, request_id: int, tool: str, arguments=None) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        ).encode("utf-8") + b"\n"

    def test_eof_starts_background_shutdown_before_waiting_for_data_workers(self) -> None:
        runtime = _FakeRuntime()
        server = McpServer(runtime)
        destination = io.BytesIO()
        server.serve(io.BytesIO(self._line(1, "slow")), destination)
        self.assertTrue(runtime.shutdown_started)
        self.assertTrue(runtime.closed)
        response = json.loads(destination.getvalue())
        self.assertEqual(response["id"], 1)

    def test_control_lane_responds_while_slow_data_call_is_running(self) -> None:
        runtime = _FakeRuntime()
        server = McpServer(runtime)
        source = io.BytesIO(self._line(1, "slow") + self._line(2, "server_info"))
        destination = io.BytesIO()
        server.serve(source, destination)
        responses = [json.loads(line) for line in destination.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in responses], [2, 1])
        self.assertEqual(runtime.max_active_data, 1)

    def test_concurrent_responses_are_serialized_as_complete_json_lines(self) -> None:
        runtime = _FakeRuntime()
        runtime.release_slow.set()
        server = McpServer(runtime)
        payload = b"".join(self._line(index, "slow") for index in range(1, 9))
        destination = _GuardedDestination()
        server.serve(io.BytesIO(payload), destination)
        self.assertFalse(destination.concurrent_write_detected)
        responses = [json.loads(line) for line in destination.getvalue().splitlines()]
        self.assertEqual({item["id"] for item in responses}, set(range(1, 9)))

    def test_data_saturation_is_bounded_without_starving_control_lane(self) -> None:
        runtime = _FakeRuntime()
        server = McpServer(runtime)
        payload = b"".join(self._line(index, "slow") for index in range(1, 40))
        payload += self._line(100, "server_info")
        destination = io.BytesIO()
        server.serve(io.BytesIO(payload), destination)
        responses = [json.loads(line) for line in destination.getvalue().splitlines()]
        by_id = {item["id"]: item for item in responses}
        self.assertIn(100, by_id)
        busy = [item for item in responses if item.get("error", {}).get("message") == "Server busy"]
        self.assertGreater(len(busy), 0)
        self.assertLess(runtime.max_active_data, 40)

    def test_same_file_edits_serialize_but_different_files_overlap(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "b.txt").write_text("b", encoding="utf-8")
            runtime = ToolRuntime(root, load_config(root, required=False))
            guard = threading.Lock()
            active_by_path: dict[str, int] = {}
            max_by_path: dict[str, int] = {}
            total_active = 0
            max_total = 0
            overlap = threading.Event()

            def fake_edit(path, **kwargs):
                nonlocal total_active, max_total
                with guard:
                    active_by_path[path] = active_by_path.get(path, 0) + 1
                    max_by_path[path] = max(max_by_path.get(path, 0), active_by_path[path])
                    total_active += 1
                    max_total = max(max_total, total_active)
                    if total_active >= 2:
                        overlap.set()
                overlap.wait(timeout=0.25)
                time.sleep(0.03)
                with guard:
                    active_by_path[path] -= 1
                    total_active -= 1
                return {"path": path, "created": False, "size": 1, "sha256": "0" * 64}

            with patch.object(runtime.workspace, "edit_file", side_effect=fake_edit):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(runtime.call, "edit_file", {"path": "a.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "a", "new": "x"}]})
                    second = executor.submit(runtime.call, "edit_file", {"path": "a.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "a", "new": "y"}]})
                    first.result(timeout=2)
                    second.result(timeout=2)
                self.assertEqual(max_by_path["a.txt"], 1)

                overlap.clear()
                max_total = 0
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(runtime.call, "edit_file", {"path": "a.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "a", "new": "x"}]})
                    second = executor.submit(runtime.call, "edit_file", {"path": "b.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "b", "new": "y"}]})
                    first.result(timeout=2)
                    second.result(timeout=2)
                self.assertGreaterEqual(max_total, 2)

    def test_lazy_text_write_manager_is_singleton_under_concurrent_begin(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            root.mkdir()
            runtime = ToolRuntime(root, load_config(root, required=False))
            original_manager = TextWriteManager
            created: list[TextWriteManager] = []
            created_guard = threading.Lock()

            def slow_manager():
                time.sleep(0.05)
                with created_guard:
                    staging = base / f"staging-{len(created)}"
                    manager = original_manager(staging)
                    created.append(manager)
                    return manager

            with patch.object(tools_module, "TextWriteManager", side_effect=slow_manager):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(runtime.call, "write_file", {"action": "begin", "path": "a.txt", "mode": "create"})
                    second = executor.submit(runtime.call, "write_file", {"action": "begin", "path": "b.txt", "mode": "create"})
                    first_result = first.result(timeout=2)["structuredContent"]
                    second_result = second.result(timeout=2)["structuredContent"]
            self.assertEqual(len(created), 1)
            self.assertTrue(first_result["ok"])
            self.assertTrue(second_result["ok"])
            manager = runtime.text_writes
            self.assertIsNotNone(manager)
            assert manager is not None
            manager.status(runtime.workspace, first_result["transaction_id"])
            manager.status(runtime.workspace, second_result["transaction_id"])
            manager.close()

    def test_closing_workspace_mutation_gate_wakes_waiters_without_releasing_live_holder(self) -> None:
        gate = WorkspaceMutationGate()
        lease = gate.acquire_exclusive("workspace")
        entered = threading.Event()

        def wait_for_shared() -> str:
            try:
                with gate.shared("workspace"):
                    entered.set()
                    return "entered"
            except RuntimeError:
                return "closed"

        result: list[str] = []
        worker = threading.Thread(target=lambda: result.append(wait_for_shared()), daemon=True)
        worker.start()
        time.sleep(0.05)
        self.assertFalse(entered.is_set())
        close_error: Exception | None = None
        try:
            gate.close()
        except Exception as exc:
            close_error = exc
        if close_error is not None:
            # The pre-fix implementation has no close() seam. Release only the
            # test fixture's holder so the expected red cannot strand a waiter.
            lease.release()
            worker.join(timeout=1)
            raise close_error
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["closed"])
        self.assertFalse(entered.is_set())
        # Closing admission must not pretend the still-live exclusive mutation
        # holder has disappeared; release it only after the waiter was rejected.
        lease.release()

    def test_workspace_opaque_mutation_does_not_overlap_core_file_write(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            runtime = ToolRuntime(root, load_config(root, required=False), capabilities=("test",))
            guard = threading.Lock()
            active = 0
            max_active = 0
            start = threading.Event()

            def enter_operation(result):
                nonlocal active, max_active
                start.wait(timeout=1)
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.08)
                with guard:
                    active -= 1
                return result

            def fake_capability(root_path, name):
                return enter_operation({"capability": name, "exit_code": 0})

            def fake_edit(path, **kwargs):
                return enter_operation({"path": path, "created": False, "size": 1, "sha256": "0" * 64})

            with patch.object(tools_module, "run_capability", side_effect=fake_capability), patch.object(runtime.workspace, "edit_file", side_effect=fake_edit):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(runtime.call, "run_capability", {"name": "test"})
                    second = executor.submit(runtime.call, "edit_file", {"path": "a.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "a", "new": "x"}]})
                    time.sleep(0.02)
                    start.set()
                    first.result(timeout=2)
                    second.result(timeout=2)
            self.assertEqual(max_active, 1)

    def test_extension_dispatch_uses_one_prepared_contract_for_locking_and_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ToolRuntime(root, load_config(root, required=False))
            action = SimpleNamespace(read_only=False, run_mode="foreground", requires_workspace=True)
            contract = SimpleNamespace(action=action)
            prepared = SimpleNamespace(action=action)

            class PreparedRegistry:
                def __init__(self) -> None:
                    self.action_calls = 0
                    self.run_calls = 0
                    self.executed = None

                def prepare_action(self, extension_id, action_name):
                    self.action_calls += 1
                    return contract

                def prepare_run(self, value, params, *, workspace, read_only):
                    self.run_calls += 1
                    self.asserted_contract = value
                    return prepared

                def execute_prepared(self, value, *, on_job_finish=None):
                    self.executed = value
                    return {"done": True}

            registry = PreparedRegistry()
            runtime.extensions = registry
            result = runtime.call(
                "extension",
                {"action": "run", "extension_id": "example", "extension_action": "mutate", "params": {}},
            )["structuredContent"]
            self.assertTrue(result["ok"])
            self.assertEqual(registry.action_calls, 1)
            self.assertEqual(registry.run_calls, 1)
            self.assertIs(registry.asserted_contract, contract)
            self.assertIs(registry.executed, prepared)

    def test_non_read_only_extension_job_holds_workspace_mutation_gate_until_finish(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            runtime = ToolRuntime(root, load_config(root, required=False))
            action = SimpleNamespace(read_only=False, run_mode="job", requires_workspace=True)
            contract = SimpleNamespace(action=action)
            prepared = SimpleNamespace(action=action)
            finish_callbacks = []
            edit_entered = threading.Event()

            def fake_execute_prepared(value, **kwargs):
                self.assertIs(value, prepared)
                finish_callbacks.append(kwargs.get("on_job_finish"))
                return {"job_id": "1" * 32, "status": "running"}

            def fake_edit(path, **kwargs):
                edit_entered.set()
                return {"path": path, "created": False, "size": 1, "sha256": "0" * 64}

            with patch.object(runtime.extensions, "prepare_action", return_value=contract), patch.object(runtime.extensions, "prepare_run", return_value=prepared), patch.object(runtime.extensions, "execute_prepared", side_effect=fake_execute_prepared), patch.object(runtime.workspace, "edit_file", side_effect=fake_edit):
                started = runtime.call(
                    "extension",
                    {"action": "run", "extension_id": "example", "extension_action": "mutate", "params": {}},
                )["structuredContent"]
                self.assertTrue(started["ok"])
                self.assertEqual(len(finish_callbacks), 1)
                self.assertIsNotNone(finish_callbacks[0])
                with ThreadPoolExecutor(max_workers=1) as executor:
                    edit = executor.submit(runtime.call, "edit_file", {"path": "a.txt", "expected_sha256": "0" * 64, "replacements": [{"old": "a", "new": "x"}]})
                    time.sleep(0.05)
                    self.assertFalse(edit_entered.is_set())
                    finish_callbacks[0]()
                    edit.result(timeout=2)
                self.assertTrue(edit_entered.is_set())

    def test_toolruntime_serializes_job_launch_against_shutdown(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ToolRuntime(root, load_config(root, required=False))
            action = SimpleNamespace(read_only=True, run_mode="job", requires_workspace=False)
            contract = SimpleNamespace(action=action)
            prepared = SimpleNamespace(action=action)
            launch_entered = threading.Event()
            allow_launch_return = threading.Event()
            jobs_closed = threading.Event()

            class FakeJobs:
                def close(self) -> None:
                    jobs_closed.set()

            class FakeRegistry:
                def __init__(self) -> None:
                    self.jobs = FakeJobs()

                def prepare_action(self, extension_id, action_name):
                    return contract

                def prepare_run(self, value, params, *, workspace, read_only):
                    self.prepared_contract = value
                    return prepared

                def execute_prepared(self, value, *, on_job_finish=None):
                    launch_entered.set()
                    allow_launch_return.wait(timeout=1)
                    return {"job_id": "1" * 32, "status": "running"}

            registry = FakeRegistry()
            runtime.extensions = registry
            with ThreadPoolExecutor(max_workers=2) as executor:
                launch = executor.submit(
                    runtime.call,
                    "extension",
                    {"action": "run", "extension_id": "example", "extension_action": "job", "params": {}},
                )
                self.assertTrue(launch_entered.wait(timeout=1))
                shutdown = executor.submit(runtime.begin_shutdown)
                time.sleep(0.05)
                self.assertFalse(jobs_closed.is_set())
                allow_launch_return.set()
                launched = launch.result(timeout=1)["structuredContent"]
                shutdown.result(timeout=1)
            self.assertTrue(launched["ok"])
            self.assertTrue(jobs_closed.is_set())

    def test_distinct_transaction_commits_can_overlap(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            root.mkdir()
            from folderbridge_mcp.security import Workspace
            workspace = Workspace(root)
            manager = TextWriteManager(base / "staging")
            started_a = manager.begin(workspace, "a.txt", mode="create", expected_target_sha256=None)
            started_b = manager.begin(workspace, "b.txt", mode="create", expected_target_sha256=None)
            manager.append(workspace, started_a["transaction_id"], offset=0, chunk="a")
            manager.append(workspace, started_b["transaction_id"], offset=0, chunk="b")
            guard = threading.Lock()
            active = 0
            max_active = 0
            both_inside = threading.Event()

            def fake_commit(*args, **kwargs):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        both_inside.set()
                both_inside.wait(timeout=0.25)
                time.sleep(0.03)
                with guard:
                    active -= 1

            with patch.object(text_writes, "_commit_staged_text", side_effect=fake_commit):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(manager.commit, workspace, started_a["transaction_id"], expected_size=1, expected_sha256="ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb")
                    second = executor.submit(manager.commit, workspace, started_b["transaction_id"], expected_size=1, expected_sha256="3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d")
                    first.result(timeout=2)
                    second.result(timeout=2)
            manager.close()
            self.assertGreaterEqual(max_active, 2)


if __name__ == "__main__":
    unittest.main()
