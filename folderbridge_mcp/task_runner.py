from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import Task
from .security import ToolError, clean_environment, resolve_task_executable


STREAM_LIMIT = 64 * 1024


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

    def run(self) -> None:
        half = STREAM_LIMIT // 2
        while True:
            chunk = self.stream.read(8192)
            if not chunk:
                return
            self.total += len(chunk)
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


def run_task(workspace: Path, task: Task) -> dict[str, object]:
    executable = resolve_task_executable(task.argv[0], workspace)
    argv = [executable, *task.argv[1:]]
    creation_flags = 0
    start_new_session = False
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True
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
            creationflags=creation_flags,
            start_new_session=start_new_session,
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
        exit_code = process.wait(timeout=task.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(process)
        try:
            exit_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
    stdout_reader.join(timeout=5)
    stderr_reader.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    stdout_reader.join(timeout=1)
    stderr_reader.join(timeout=1)
    stdout = stdout_reader.result()
    stderr = stderr_reader.result()
    return {
        "task": task.name,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout.data.decode("utf-8", errors="replace"),
        "stderr": stderr.data.decode("utf-8", errors="replace"),
        "stdout_total_bytes": stdout.total_bytes,
        "stderr_total_bytes": stderr.total_bytes,
        "truncated": stdout.truncated or stderr.truncated,
    }


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    process.kill()
