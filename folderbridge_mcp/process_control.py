from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Maximum time a synchronous MCP-facing external-process execution may own the
# transport response. This is a transport safety budget, never a business
# timeout; longer work must remain host-owned and become queryable as a Job.
#
# Upstream command-response deadlines are finite and independent from the
# control-plane poll cadence. A long project task must therefore surrender the
# synchronous response path early and remain host-owned as a Job instead of
# relying on any particular Tunnel long-poll interval.
TRANSPORT_RESPONSE_BUDGET_SECONDS = 20.0

# Running Job status is intentionally non-blocking. This hint keeps callers from
# replacing one long-held request with a tight status-poll loop during quiet
# work, while remaining responsive enough for interactive use.
JOB_STATUS_POLL_HINT_SECONDS = 3.0


@dataclass(frozen=True)
class ProcessGenerationQuiescence:
    """Bounded evidence about whether an exited process generation is gone."""

    quiescent: bool
    proof: str


def prove_owned_process_generation_quiescent(process: Any) -> ProcessGenerationQuiescence:
    """Fail closed unless the previous owned process generation is provably gone.

    ``CREATE_NEW_PROCESS_GROUP`` plus ``taskkill /T`` is useful while a Windows
    leader is alive, but it does not provide a durable handle after that leader
    has already exited.  Until FolderBridge owns the generation with a Windows
    Job Object (or equivalent verifiable primitive), parent exit alone is not a
    quiescence proof and automatic restart must stay blocked.

    POSIX sessions retain a process-group identity after the leader exits, so a
    missing process group is a bounded proof that no member of that generation
    remains.  A present or unqueryable group is deliberately treated as
    ambiguous rather than guessed safe.
    """

    if process.poll() is None:
        return ProcessGenerationQuiescence(False, "leader-still-running")
    if sys.platform == "win32":
        return ProcessGenerationQuiescence(False, "windows-parent-exit-unverified")
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return ProcessGenerationQuiescence(True, "posix-process-group-absent")
    except PermissionError:
        return ProcessGenerationQuiescence(False, "posix-process-group-permission-denied")
    except OSError:
        return ProcessGenerationQuiescence(False, "posix-process-group-query-failed")
    return ProcessGenerationQuiescence(False, "posix-process-group-still-present")


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
