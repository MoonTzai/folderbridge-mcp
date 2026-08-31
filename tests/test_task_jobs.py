from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import folderbridge_mcp.task_runner as task_runner
import folderbridge_mcp.tools as tools_module
from folderbridge_mcp.config import Task, load_config
from folderbridge_mcp.tools import ToolRuntime


class TaskJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_long_task_auto_promotes_without_restarting_process(self) -> None:
        marker = self.root / "count.json"
        code = (
            "import json,time,pathlib; "
            f"p=pathlib.Path({str(marker)!r}); "
            "n=json.loads(p.read_text())+1 if p.exists() else 1; "
            "p.write_text(json.dumps(n)); "
            "time.sleep(0.12); print('done', flush=True)"
        )
        task = Task("slow", (sys.executable, "-c", code), 2)
        manager = task_runner.TaskJobManager()
        finished = threading.Event()
        try:
            with mock.patch.object(task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 0.03):
                started = manager.run_or_promote(self.root, task, job_kind="task", on_finish=finished.set)
            self.assertEqual(started["status"], "running")
            self.assertFalse(finished.is_set())
            self.assertTrue(started["auto_promoted"])
            listed = manager.list(workspace=self.root)
            self.assertIn(started["job_id"], {item["job_id"] for item in listed["jobs"]})

            deadline = time.monotonic() + 3
            status = manager.status(started["job_id"], workspace=self.root)
            while status["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
                status = manager.status(started["job_id"], workspace=self.root)
            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(json.loads(marker.read_text()), 1)
            self.assertEqual(status["result"]["exit_code"], 0)
            self.assertTrue(finished.wait(timeout=1))
        finally:
            manager.close()

    def test_shutdown_wins_the_boundary_race_instead_of_registering_a_new_job(self) -> None:
        manager = task_runner.TaskJobManager()
        finished = threading.Event()

        class BoundaryProcess:
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.wait_calls = 0
                self.poll_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                raise task_runner.subprocess.TimeoutExpired(cmd="boundary", timeout=timeout)

            def poll(self):
                self.poll_calls += 1
                if self.poll_calls == 1:
                    manager.close()
                return None

        process = BoundaryProcess()
        reader = mock.Mock()
        reader.last_activity_at = None
        task = Task("boundary", (sys.executable, "-c", "pass"), 2)
        with mock.patch.object(task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 0.03), mock.patch.object(
            manager, "_spawn", return_value=(process, reader, reader)
        ), mock.patch.object(manager, "_terminate"), mock.patch.object(
            manager, "_result", return_value={"task": "boundary", "exit_code": 1, "timed_out": False}
        ):
            with self.assertRaisesRegex(Exception, "shutting down"):
                manager.run_or_promote(self.root, task, job_kind="task", on_finish=finished.set)
        self.assertTrue(finished.is_set())
        with manager._lock:
            self.assertEqual(manager._jobs, {})
            self.assertEqual(manager._inline_processes, {})

    def test_shutdown_terminates_inline_owned_process_before_promotion(self) -> None:
        marker = self.root / "inline-started.txt"
        code = (
            "import pathlib,time; "
            f"pathlib.Path({str(marker)!r}).write_text('started'); "
            "time.sleep(5)"
        )
        task = Task("inline", (sys.executable, "-c", code), 10)
        manager = task_runner.TaskJobManager()
        finished = threading.Event()
        results: list[object] = []

        def run() -> None:
            try:
                with mock.patch.object(task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 1.0):
                    results.append(manager.run_or_promote(self.root, task, job_kind="task", on_finish=finished.set))
            except Exception as exc:
                results.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        manager.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.wait(timeout=1))
        with manager._lock:
            self.assertEqual(manager._inline_processes, {})

    def test_short_task_keeps_legacy_synchronous_result(self) -> None:
        task = Task("short", (sys.executable, "-c", "print('ok')"), 2)
        manager = task_runner.TaskJobManager()
        try:
            with mock.patch.object(task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 0.5):
                result = manager.run_or_promote(self.root, task, job_kind="task")
            self.assertNotIn("job_id", result)
            self.assertEqual(result["task"], "short")
            self.assertEqual(result["exit_code"], 0)
            self.assertIn("ok", result["stdout"])
        finally:
            manager.close()

    def test_toolruntime_capability_promotion_holds_workspace_lease_until_true_exit(self) -> None:
        task = Task("capability-slow", (sys.executable, "-c", "import time; time.sleep(0.12); print('done')"), 2)
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=("test",))
        released = threading.Event()

        class Lease:
            def release(self) -> None:
                released.set()

        def fake_run_capability(root, name, *, task_runner=None):
            self.assertEqual(name, "test")
            self.assertIsNotNone(task_runner)
            result = task_runner(root, task)
            result["capability"] = name
            return result

        try:
            with mock.patch.object(runtime._workspace_mutations, "acquire_exclusive", return_value=Lease()), mock.patch.object(
                task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 0.03
            ), mock.patch.object(tools_module, "run_capability", side_effect=fake_run_capability):
                started = runtime._run_capability({"name": "test"})
                self.assertIn("job_id", started)
                self.assertFalse(released.is_set())
                deadline = time.monotonic() + 3
                status = runtime._run_capability({"action": "status", "job_id": started["job_id"]})
                while status["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.02)
                    status = runtime._run_capability({"action": "status", "job_id": started["job_id"]})
                self.assertEqual(status["status"], "succeeded")
                self.assertTrue(released.wait(timeout=1))
        finally:
            runtime.close()

    def test_promoted_timeout_keeps_lease_while_termination_is_pending(self) -> None:
        manager = task_runner.TaskJobManager()
        exited = threading.Event()
        released = threading.Event()

        class StickyProcess:
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.first_wait = True

            def wait(self, timeout=None):
                if self.first_wait and timeout is not None:
                    self.first_wait = False
                    raise task_runner.subprocess.TimeoutExpired(cmd="sticky", timeout=timeout)
                if timeout is None:
                    exited.wait(timeout=3)
                    return 1 if exited.is_set() else None
                if exited.wait(timeout=timeout):
                    return 1
                raise task_runner.subprocess.TimeoutExpired(cmd="sticky", timeout=timeout)

            def poll(self):
                return 1 if exited.is_set() else None

        reader = mock.Mock()
        reader.last_activity_at = None
        reader.result.return_value = task_runner.Capture(data=b"", total_bytes=0, truncated=False)
        process = StickyProcess()
        job = task_runner._TaskJob(
            job_id="a" * 32,
            job_kind="capability",
            logical_name="sticky",
            task=Task("sticky", (sys.executable, "-c", "pass"), 120),
            workspace_root=str(self.root.resolve()),
            process=process,
            stdout_reader=reader,
            stderr_reader=reader,
            started_at=time.time() - 120,
            started_monotonic=time.monotonic() - 120,
            on_finish=released.set,
        )
        with manager._lock:
            manager._jobs[job.job_id] = job

        monitor = threading.Thread(target=manager._monitor, args=(job,), daemon=True)
        with mock.patch.object(manager, "_terminate", return_value=False):
            monitor.start()
            deadline = time.monotonic() + 2
            while job.status != "termination_pending" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(job.status, "termination_pending")
            self.assertFalse(released.is_set())
            self.assertTrue(job.process.poll() is None)
            self.assertEqual(manager.status(job.job_id, workspace=self.root)["runtime_health"]["process_alive"], True)
            exited.set()
            monitor.join(timeout=2)

        self.assertFalse(monitor.is_alive())
        self.assertEqual(job.status, "timed_out")
        self.assertTrue(released.wait(timeout=1))
        self.assertIsNotNone(job.result)
        manager.close()

    def test_quiet_task_is_alive_quiet_not_automatically_stalled(self) -> None:
        task = Task("quiet", (sys.executable, "-c", "import time; time.sleep(0.3)"), 2)
        manager = task_runner.TaskJobManager()
        try:
            with mock.patch.object(task_runner, "TRANSPORT_RESPONSE_BUDGET_SECONDS", 0.03):
                started = manager.run_or_promote(self.root, task, job_kind="capability")
            status = manager.status(started["job_id"], workspace=self.root)
            self.assertEqual(status["runtime_health"]["state"], "alive_quiet")
            self.assertFalse(status["runtime_health"]["stall_suspected"])
            self.assertEqual(status["status"], "running")
        finally:
            manager.cancel(started["job_id"], workspace=self.root)
            manager.close()


if __name__ == "__main__":
    unittest.main()
