from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


def owned_process_group_kwargs(*, hide_window: bool = False) -> dict[str, Any]:
    """Return the subprocess kwargs needed to own a child process tree.

    Callers stay responsible for the rest of their Popen contract. Keeping the
    ownership flags here prevents launcher, extension, and managed-service
    process trees from drifting apart as their implementations evolve.
    """

    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hide_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags, "start_new_session": False}
    return {"creationflags": 0, "start_new_session": True}


def terminate_owned_process_tree(
    process: Any,
    *,
    force: bool = True,
    hide_window: bool = False,
    taskkill_timeout_seconds: float = 10.0,
) -> None:
    """Terminate a process tree started with :func:`owned_process_group_kwargs`.

    Windows uses taskkill /T so descendants are included; POSIX targets the
    process group created by start_new_session. A direct process terminate/kill
    is retained as a bounded fallback when group termination is unavailable.
    """

    if process.poll() is not None:
        return

    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
        taskkill = Path(system_root or r"C:\Windows") / "System32" / "taskkill.exe"
        argv = [str(taskkill), "/PID", str(process.pid), "/T"]
        if force:
            argv.append("/F")
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=taskkill_timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if hide_window else 0,
            )
            if completed.returncode == 0 or process.poll() is not None:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, sig)
            return
        except (AttributeError, OSError, ProcessLookupError):
            pass

    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except OSError:
        pass
