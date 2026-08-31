from __future__ import annotations

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

from .config import Task
from .process_control import (
    TRANSPORT_RESPONSE_BUDGET_SECONDS,
    owned_process_group_kwargs,
    terminate_owned_process_tree,
)
from .security import ToolError, clean_environment, resolve_task_executable


STREAM_LIMIT = 64 * 1024
MAX_RUNNING_TASK_JOBS = 16
MAX_RETAINED_TASK_JOBS = 128
ACTIVE_TASK_JOB_STATUSES = frozenset({"running", "cancelling", "termination_pending"})
TASK_ACTIVITY_FRESH_SECONDS = 300.0
TASK_JOB_SHUTDOWN_SECONDS = 5.0


@dataclass
class Capture:
    data: bytes
    total_bytes: int
    truncated: bool


class _BoundedReader(threading.Thread):
    def __init__(self, stream: BinaryIO) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()
        self.last_activity_at: float | None = None

    def run(self) -> None:
        half = STREAM_LIMIT // 2
        while True:
            try:
                chunk = self.stream.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            self.total += len(chunk)
            self.last_activity_at = time.time()
            head_room = half - len(self.head)
            if head_room > 0:
                self.head.extend(chunk[:head_room])
                chunk = chunk[head_room:]
            if chunk:
                self.tail.extend(chunk)
                if len(self.tail) > half:
                    del self.tail[: len(self.tail) - half]

    def result(self) -> Capture:
        if self.total <= STREAM_LIMIT:
            data = bytes(self.head + self.tail)
            return Capture(data=data, total_bytes=self.total, truncated=False)
        marker = b"\n... output omitted ...\n"
        data = bytes(self.head) + marker + bytes(self.tail)
        return Capture(data=data, total_bytes=self.total, truncated=True)


@dataclass
class _TaskJob:
    job_id: str
    job_kind: str
    logical_name: str
    task: Task
    workspace_root: str
    process: subprocess.Popen[bytes]
    stdout_reader: _BoundedReader
    stderr_reader: _BoundedReader
    started_at: float
    started_monotonic: float
    on_finish: Callable[[], None] | None = None
    status: str = "running"
    finished_at: float | None = None
    result: dict[str, object] | None = None
    cancel_requested: bool = False
    finish_notified: bool = False
    finish_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


class TaskJobManager:
    """Own FolderBridge-launched fixed-argv work after the transport-safe inline window."""

    def __init__(self) -> None:
        self._jobs: dict[str, _TaskJob] = {}
        self._inline_processes: dict[str, subprocess.Popen[bytes]] = {}
        self._promotion_reservations = 0
        self._closed = False
        self._lock = threading.Lock()

    @staticmethod
    def _notify_finish(job: _TaskJob) -> None:
        with job.finish_lock:
            if job.finish_notified:
                return
            job.finish_notified = True
            callback = job.on_finish
            job.on_finish = None
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    @staticmethod
    def _spawn(workspace: Path, task: Task) -> tuple[subprocess.Popen[bytes], _BoundedReader, _BoundedReader]:
        executable = resolve_task_executable(task.argv[0], workspace)
        argv = [executable, *task.argv[1:]]
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=clean_environment(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                **owned_process_group_kwargs(),
            )
        except OSError as exc:
            raise ToolError("TASK_START_FAILED", f"Could not start task {task.name}: {exc}") from exc
        assert process.stdout is not None and process.stderr is not None
        stdout_reader = _BoundedReader(process.stdout)
        stderr_reader = _BoundedReader(process.stderr)
        stdout_reader.start()
        stderr_reader.start()
        return process, stdout_reader, stderr_reader

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> bool:
        if process.poll() is not None:
            return True
        terminate_owned_process_tree(process)
        try:
            process.wait(timeout=TASK_JOB_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return process.poll() is not None
        return process.poll() is not None

    @staticmethod
    def _wait_until_exit(process: subprocess.Popen[bytes]) -> None:
        """Keep host ownership without publishing terminal state until true exit."""
        while process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                time.sleep(0.1)

    @staticmethod
    def _close_streams(
        process: subprocess.Popen[bytes],
        stdout_reader: _BoundedReader,
        stderr_reader: _BoundedReader,
    ) -> tuple[Capture, Capture]:
        stdout_reader.join(timeout=TASK_JOB_SHUTDOWN_SECONDS)
        stderr_reader.join(timeout=TASK_JOB_SHUTDOWN_SECONDS)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        return stdout_reader.result(), stderr_reader.result()

    @classmethod
    def _result(
        cls,
        task: Task,
        process: subprocess.Popen[bytes],
        stdout_reader: _BoundedReader,
        stderr_reader: _BoundedReader,
        *,
        timed_out: bool,
    ) -> dict[str, object]:
        stdout, stderr = cls._close_streams(process, stdout_reader, stderr_reader)
        return {
            "task": task.name,
            "exit_code": process.poll(),
            "timed_out": timed_out,
            "stdout": stdout.data.decode("utf-8", errors="replace"),
            "stderr": stderr.data.decode("utf-8", errors="replace"),
            "stdout_total_bytes": stdout.total_bytes,
            "stderr_total_bytes": stderr.total_bytes,
            "truncated": stdout.truncated or stderr.truncated,
        }

    def _prune_locked(self) -> None:
        finished = sorted(
            (job for job in self._jobs.values() if job.status not in ACTIVE_TASK_JOB_STATUSES),
            key=lambda job: job.finished_at or job.started_at,
        )
        excess = len(finished) - MAX_RETAINED_TASK_JOBS
        for job in finished[: max(0, excess)]:
            self._jobs.pop(job.job_id, None)

    def _release_reservation(self) -> None:
        with self._lock:
            if self._promotion_reservations > 0:
                self._promotion_reservations -= 1

    def _remove_inline(self, token: str) -> None:
        with self._lock:
            self._inline_processes.pop(token, None)

    def run_or_promote(
        self,
        workspace: Path,
        task: Task,
        *,
        job_kind: str,
        logical_name: str | None = None,
        on_finish: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        root = workspace.resolve(strict=True)
        if job_kind not in {"task", "capability"}:
            raise ValueError("job_kind must be task or capability")
        promotable = float(task.timeout_seconds) > TRANSPORT_RESPONSE_BUDGET_SECONDS
        admission_error: ToolError | None = None
        with self._lock:
            if self._closed:
                admission_error = ToolError(
                    "SERVER_SHUTTING_DOWN",
                    "FolderBridge is shutting down; new task jobs are disabled.",
                )
            elif promotable:
                self._prune_locked()
                running = sum(job.status in ACTIVE_TASK_JOB_STATUSES for job in self._jobs.values())
                if running + self._promotion_reservations >= MAX_RUNNING_TASK_JOBS:
                    admission_error = ToolError(
                        "TASK_JOB_LIMIT",
                        f"At most {MAX_RUNNING_TASK_JOBS} promoted task jobs may run concurrently.",
                        limit=MAX_RUNNING_TASK_JOBS,
                    )
                else:
                    self._promotion_reservations += 1
        if admission_error is not None:
            if on_finish is not None:
                on_finish()
            raise admission_error

        started_at = time.time()
        started_monotonic = time.monotonic()
        try:
            process, stdout_reader, stderr_reader = self._spawn(root, task)
        except Exception:
            if promotable:
                self._release_reservation()
            if on_finish is not None:
                on_finish()
            raise

        inline_token = uuid.uuid4().hex
        with self._lock:
            closed_after_spawn = self._closed
            if not closed_after_spawn:
                self._inline_processes[inline_token] = process
        if closed_after_spawn:
            self._terminate(process)
            self._result(task, process, stdout_reader, stderr_reader, timed_out=False)
            if promotable:
                self._release_reservation()
            if on_finish is not None:
                on_finish()
            raise ToolError("SERVER_SHUTTING_DOWN", "FolderBridge is shutting down; the newly started task was terminated.")

        wait_seconds = min(float(task.timeout_seconds), TRANSPORT_RESPONSE_BUDGET_SECONDS) if promotable else float(task.timeout_seconds)
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            if not promotable:
                self._terminate(process)
                self._remove_inline(inline_token)
                result = self._result(task, process, stdout_reader, stderr_reader, timed_out=True)
                if on_finish is not None:
                    on_finish()
                return result

            if process.poll() is not None:
                self._remove_inline(inline_token)
                result = self._result(task, process, stdout_reader, stderr_reader, timed_out=False)
                self._release_reservation()
                if on_finish is not None:
                    on_finish()
                return result

            job: _TaskJob | None = None
            with self._lock:
                self._inline_processes.pop(inline_token, None)
                if self._promotion_reservations > 0:
                    self._promotion_reservations -= 1
                if not self._closed:
                    job = _TaskJob(
                        job_id=uuid.uuid4().hex,
                        job_kind=job_kind,
                        logical_name=logical_name or task.name,
                        task=task,
                        workspace_root=str(root),
                        process=process,
                        stdout_reader=stdout_reader,
                        stderr_reader=stderr_reader,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                        on_finish=on_finish,
                    )
                    self._jobs[job.job_id] = job
            if job is None:
                self._terminate(process)
                self._result(task, process, stdout_reader, stderr_reader, timed_out=False)
                if on_finish is not None:
                    on_finish()
                raise ToolError(
                    "SERVER_SHUTTING_DOWN",
                    "FolderBridge began shutting down before task promotion; the owned process was terminated instead of creating a new Job.",
                )

            monitor = threading.Thread(
                target=self._monitor,
                args=(job,),
                name=f"folderbridge-{job_kind}-job-{job.job_id[:8]}",
                daemon=True,
            )
            try:
                monitor.start()
            except Exception as exc:
                self._terminate(process)
                result = self._result(task, process, stdout_reader, stderr_reader, timed_out=False)
                with self._lock:
                    self._jobs.pop(job.job_id, None)
                self._notify_finish(job)
                raise ToolError(
                    "TASK_JOB_MONITOR_START_FAILED",
                    f"Could not start promoted task monitor: {type(exc).__name__}",
                    task_result=result,
                ) from exc
            return {
                "job_id": job.job_id,
                "status": "running",
                "job_kind": job.job_kind,
                "name": job.logical_name,
                "task": task.name,
                "timeout_seconds": task.timeout_seconds,
                "worker_pid": getattr(job.process, "pid", None),
                "auto_promoted": True,
                "promoted_after_seconds": max(0.0, time.monotonic() - started_monotonic),
            }

        self._remove_inline(inline_token)
        result = self._result(task, process, stdout_reader, stderr_reader, timed_out=False)
        if promotable:
            self._release_reservation()
        if on_finish is not None:
            on_finish()
        return result

    def _monitor(self, job: _TaskJob) -> None:
        timed_out = False
        remaining = max(0.0, float(job.task.timeout_seconds) - (time.monotonic() - job.started_monotonic))
        try:
            job.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            if not self._terminate(job.process):
                with self._lock:
                    job.status = "termination_pending"
                self._wait_until_exit(job.process)
        except Exception:
            if job.process.poll() is None and not self._terminate(job.process):
                with self._lock:
                    job.status = "termination_pending"
                self._wait_until_exit(job.process)
        result = self._result(
            job.task,
            job.process,
            job.stdout_reader,
            job.stderr_reader,
            timed_out=timed_out,
        )
        with self._lock:
            cancel_requested = job.cancel_requested
        self._notify_finish(job)
        with self._lock:
            job.status = (
                "cancelled" if cancel_requested
                else "timed_out" if timed_out
                else "succeeded" if result.get("exit_code") == 0
                else "failed"
            )
            job.result = result
            job.finished_at = time.time()
            self._prune_locked()

    def _get(self, job_id: str, *, workspace: Path, expected_kind: str | None = None) -> _TaskJob:
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ToolError("INVALID_ARGUMENT", "job_id must be a FolderBridge task job id")
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ToolError("TASK_JOB_NOT_FOUND", "Task job is not known to this FolderBridge process.", job_id=job_id)
        if job.workspace_root != str(workspace.resolve(strict=True)):
            raise ToolError("TASK_JOB_WORKSPACE_MISMATCH", "Task job belongs to a different workspace.", job_id=job_id)
        if expected_kind is not None and job.job_kind != expected_kind:
            raise ToolError("TASK_JOB_KIND_MISMATCH", "Task job belongs to a different execution gateway.", job_id=job_id)
        return job

    @staticmethod
    def _runtime_health(job: _TaskJob) -> dict[str, object]:
        process_alive = job.process.poll() is None
        elapsed = max(0.0, time.time() - job.started_at)
        if not process_alive or job.status not in ACTIVE_TASK_JOB_STATUSES:
            return {
                "state": "finished",
                "confidence": "high",
                "process_alive": process_alive,
                "elapsed_seconds": elapsed,
                "stall_suspected": False,
            }
        times = [
            value
            for value in (job.stdout_reader.last_activity_at, job.stderr_reader.last_activity_at)
            if isinstance(value, (int, float))
        ]
        if times:
            last_activity = max(times)
            age = max(0.0, time.time() - last_activity)
            if age <= TASK_ACTIVITY_FRESH_SECONDS:
                return {
                    "state": "active_output",
                    "confidence": "medium",
                    "process_alive": True,
                    "elapsed_seconds": elapsed,
                    "stall_suspected": False,
                    "last_output_activity_at": last_activity,
                    "last_output_activity_age_seconds": age,
                }
        return {
            "state": "alive_quiet",
            "confidence": "low",
            "process_alive": True,
            "elapsed_seconds": elapsed,
            "stall_suspected": False,
        }

    def status(self, job_id: str, *, workspace: Path, expected_kind: str | None = None) -> dict[str, object]:
        job = self._get(job_id, workspace=workspace, expected_kind=expected_kind)
        with self._lock:
            payload: dict[str, object] = {
                "job_id": job.job_id,
                "status": job.status,
                "job_kind": job.job_kind,
                "name": job.logical_name,
                "task": job.task.name,
                "timeout_seconds": job.task.timeout_seconds,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "runtime_health": self._runtime_health(job),
            }
            if job.result is not None:
                payload["result"] = job.result
            return payload

    def list(
        self,
        *,
        workspace: Path,
        expected_kind: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, object]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ToolError("INVALID_ARGUMENT", "offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 200")
        root = str(workspace.resolve(strict=True))
        with self._lock:
            jobs = [
                job for job in self._jobs.values()
                if job.workspace_root == root and (expected_kind is None or job.job_kind == expected_kind)
            ]
        jobs.sort(key=lambda item: item.started_at, reverse=True)
        total = len(jobs)
        page = jobs[offset:offset + limit]
        rendered = [
            {
                "job_id": job.job_id,
                "status": job.status,
                "job_kind": job.job_kind,
                "name": job.logical_name,
                "task": job.task.name,
                "timeout_seconds": job.task.timeout_seconds,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "runtime_health": self._runtime_health(job),
            }
            for job in page
        ]
        next_offset = offset + len(page) if offset + len(page) < total else None
        return {
            "jobs": rendered,
            "total": total,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": next_offset is not None,
        }

    def cancel(self, job_id: str, *, workspace: Path, expected_kind: str | None = None) -> dict[str, object]:
        job = self._get(job_id, workspace=workspace, expected_kind=expected_kind)
        with self._lock:
            if job.status not in ACTIVE_TASK_JOB_STATUSES:
                return {"job_id": job.job_id, "status": job.status, "already_finished": True}
            job.cancel_requested = True
            job.status = "cancelling"
        terminated = self._terminate(job.process)
        if not terminated:
            with self._lock:
                if job.status in ACTIVE_TASK_JOB_STATUSES:
                    job.status = "termination_pending"
            return {"job_id": job.job_id, "status": "termination_pending", "cancel_requested": True}
        return {"job_id": job.job_id, "status": "cancelling"}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            inline = list(self._inline_processes.values())
            jobs = [job for job in self._jobs.values() if job.status in ACTIVE_TASK_JOB_STATUSES]
            for job in jobs:
                job.cancel_requested = True
                job.status = "cancelling"
        for process in inline:
            self._terminate(process)
        for job in jobs:
            if not self._terminate(job.process):
                with self._lock:
                    if job.status in ACTIVE_TASK_JOB_STATUSES:
                        job.status = "termination_pending"


def run_task(workspace: Path, task: Task) -> dict[str, object]:
    """Legacy synchronous helper for local/internal callers."""
    executable = resolve_task_executable(task.argv[0], workspace)
    argv = [executable, *task.argv[1:]]
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=clean_environment(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(),
        )
    except OSError as exc:
        raise ToolError("TASK_START_FAILED", f"Could not start task {task.name}: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    stdout_reader = _BoundedReader(process.stdout)
    stderr_reader = _BoundedReader(process.stderr)
    stdout_reader.start()
    stderr_reader.start()
    timed_out = False
    try:
        process.wait(timeout=task.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        TaskJobManager._terminate(process)
    return TaskJobManager._result(task, process, stdout_reader, stderr_reader, timed_out=timed_out)
