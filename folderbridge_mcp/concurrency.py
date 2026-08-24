from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Hashable, Iterator


CONTROL_WORKERS = 2
DATA_WORKERS = 6
CONTROL_MAX_INFLIGHT = 8
DATA_MAX_INFLIGHT = 12
SERVER_BUSY_CODE = -32001


class MutationGateClosed(RuntimeError):
    """Raised when shutdown has closed mutation admission for a workspace."""


class BoundedExecutorLane:
    """Fixed worker pool with fail-fast bounded admission.

    ThreadPoolExecutor's internal queue is intentionally hidden behind a
    semaphore so callers can never enqueue unbounded work. submit() returns
    False instead of blocking the ingress thread when the lane is full.
    """

    def __init__(self, *, workers: int, max_inflight: int, thread_name_prefix: str) -> None:
        if workers < 1 or max_inflight < workers:
            raise ValueError("max_inflight must be at least the worker count")
        self.workers = workers
        self.max_inflight = max_inflight
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)
        self._state_lock = threading.Lock()
        self._closed = False

    def submit(self, operation: Callable[[], None]) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        with self._state_lock:
            if self._closed:
                self._slots.release()
                return False
            try:
                self._executor.submit(self._run, operation)
            except RuntimeError:
                self._slots.release()
                return False
        return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        finally:
            self._slots.release()


@dataclass
class _ResourceLockEntry:
    lock: threading.Lock
    users: int = 0


@dataclass
class _WorkspaceGateState:
    condition: threading.Condition
    shared_holders: int = 0
    exclusive_holder: bool = False
    waiting_exclusive: int = 0


class MutationLease:
    """Idempotent cross-thread lease used by long-lived opaque mutations."""

    def __init__(self, state: _WorkspaceGateState) -> None:
        self._state = state
        self._release_lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        with self._state.condition:
            self._state.exclusive_holder = False
            self._state.condition.notify_all()


class WorkspaceMutationGate:
    """Shared leases for known file writes, exclusive leases for opaque mutations.

    Writer preference prevents a stream of independent file writes from starving
    a queued task/build/plugin mutation whose touched paths cannot be predicted.
    The table is intentionally tiny because FolderBridge accepts at most a small
    fixed set of workspaces per server.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._states: dict[Hashable, _WorkspaceGateState] = {}
        self._closed = threading.Event()

    def _state(self, key: Hashable) -> _WorkspaceGateState:
        with self._guard:
            state = self._states.get(key)
            if state is None:
                state = _WorkspaceGateState(threading.Condition(threading.Lock()))
                self._states[key] = state
            return state

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._guard:
            states = list(self._states.values())
        for state in states:
            with state.condition:
                state.condition.notify_all()

    def _ensure_open(self) -> None:
        if self._closed.is_set():
            raise MutationGateClosed("Workspace mutation admission is closed during shutdown")

    @contextmanager
    def shared(self, key: Hashable) -> Iterator[None]:
        state = self._state(key)
        with state.condition:
            while state.exclusive_holder or state.waiting_exclusive:
                self._ensure_open()
                state.condition.wait()
            self._ensure_open()
            state.shared_holders += 1
        try:
            yield
        finally:
            with state.condition:
                state.shared_holders -= 1
                if state.shared_holders == 0:
                    state.condition.notify_all()

    def acquire_exclusive(self, key: Hashable) -> MutationLease:
        state = self._state(key)
        with state.condition:
            state.waiting_exclusive += 1
            try:
                while state.exclusive_holder or state.shared_holders:
                    self._ensure_open()
                    state.condition.wait()
                self._ensure_open()
                state.exclusive_holder = True
            finally:
                state.waiting_exclusive -= 1
        return MutationLease(state)

    @contextmanager
    def exclusive(self, key: Hashable) -> Iterator[None]:
        lease = self.acquire_exclusive(key)
        try:
            yield
        finally:
            lease.release()


class ResourceLockTable:
    """Serializes mutations of one resource without serializing unrelated keys."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[Hashable, _ResourceLockEntry] = {}

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _ResourceLockEntry(threading.Lock())
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)
