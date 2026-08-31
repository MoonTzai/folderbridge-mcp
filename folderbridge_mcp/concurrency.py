from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterator


CONTROL_WORKERS = 2
DATA_WORKERS = 6
CONTROL_MAX_INFLIGHT = 8
DATA_MAX_INFLIGHT = 12
SERVER_BUSY_CODE = -32001
WORKSPACE_MUTATION_WAIT_SECONDS = 2.0


class MutationGateClosed(RuntimeError):
    """Raised when shutdown has closed mutation admission for a workspace."""


class WorkspaceMutationBusy(RuntimeError):
    """Raised when a bounded workspace-mutation lease wait expires."""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__("Workspace is busy with another mutation")
        self.details = details


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
    owner: dict[str, Any] | None = None


@dataclass
class _ExclusiveWaiter:
    owner: dict[str, Any]
    wait_started: float
    wait_started_monotonic: float


@dataclass
class _WorkspaceGateState:
    condition: threading.Condition
    shared_holders: int = 0
    shared_owners: dict[object, dict[str, Any]] = field(default_factory=dict)
    exclusive_holder: bool = False
    exclusive_owner: dict[str, Any] | None = None
    waiting_exclusive: list[_ExclusiveWaiter] = field(default_factory=list)


def _owner_metadata(owner: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(owner, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "action", "job_id", "pid", "path", "extension_id", "extension_action",
        "capability", "task", "name",
    ):
        value = owner.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[key] = value
    return result


MAX_MUTATION_CLAIMS = 32


@dataclass(frozen=True)
class MutationClaim:
    """One canonical workspace mutation claim.

    Callers must resolve paths through Workspace before constructing claims. The
    coordinator normalizes case/path spelling again so overlap checks follow the
    host OS path identity rules.
    """

    kind: str
    path: str

    @classmethod
    def exact(cls, path: str | os.PathLike[str]) -> MutationClaim:
        return cls._create("exact", path)

    @classmethod
    def tree(cls, path: str | os.PathLike[str]) -> MutationClaim:
        return cls._create("tree", path)

    @classmethod
    def _create(cls, kind: str, path: str | os.PathLike[str]) -> MutationClaim:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
            raise ValueError("Mutation claim paths must be non-empty absolute paths")
        return cls(kind=kind, path=os.path.normcase(os.path.normpath(raw)))

    def __post_init__(self) -> None:
        if self.kind not in {"exact", "tree"}:
            raise ValueError("Mutation claim kind must be exact or tree")
        if not isinstance(self.path, str) or not self.path or not os.path.isabs(self.path):
            raise ValueError("Mutation claim path must be an absolute string")

    def conflicts(self, other: MutationClaim) -> bool:
        if self.kind == "exact" and other.kind == "exact":
            return self.path == other.path
        if self.kind == "tree" and other.kind == "tree":
            return _path_contains(self.path, other.path) or _path_contains(other.path, self.path)
        exact = self if self.kind == "exact" else other
        tree = other if self.kind == "exact" else self
        return _path_contains(tree.path, exact.path)


def _path_contains(tree: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((tree, candidate)) == tree
    except ValueError:
        return False


@dataclass(frozen=True)
class MutationScope:
    kind: str
    claims: tuple[MutationClaim, ...] = ()

    @classmethod
    def none(cls) -> MutationScope:
        return cls("none")

    @classmethod
    def workspace(cls) -> MutationScope:
        return cls("workspace")

    @classmethod
    def paths(cls, *claims: MutationClaim) -> MutationScope:
        if not claims:
            return cls.none()
        return cls("paths", tuple(claims))

    def __post_init__(self) -> None:
        if self.kind not in {"none", "workspace", "paths"}:
            raise ValueError("Mutation scope kind must be none, workspace, or paths")
        if len(self.claims) > MAX_MUTATION_CLAIMS:
            raise ValueError(f"Mutation scope may contain at most {MAX_MUTATION_CLAIMS} claims")
        if self.kind == "paths":
            if not self.claims or not all(isinstance(claim, MutationClaim) for claim in self.claims):
                raise ValueError("paths mutation scope requires one or more MutationClaim values")
        elif self.claims:
            raise ValueError(f"{self.kind} mutation scope may not contain path claims")

    def conflicts(self, other: MutationScope) -> bool:
        if self.kind == "none" or other.kind == "none":
            return False
        if self.kind == "workspace" or other.kind == "workspace":
            return True
        return any(left.conflicts(right) for left in self.claims for right in other.claims)


@dataclass
class _ScopedMutationHolder:
    token: object
    scope: MutationScope
    owner: dict[str, Any]
    acquired_monotonic: float


@dataclass
class _ScopedMutationWaiter:
    scope: MutationScope
    owner: dict[str, Any]
    wait_started: float
    wait_started_monotonic: float


@dataclass
class _ScopedMutationState:
    condition: threading.Condition
    holders: list[_ScopedMutationHolder] = field(default_factory=list)
    waiters: list[_ScopedMutationWaiter] = field(default_factory=list)


class ScopedMutationLease:
    """Idempotent cross-thread lease for one atomically acquired mutation scope."""

    def __init__(
        self,
        coordinator: WorkspaceMutationCoordinator,
        key: Hashable,
        state: _ScopedMutationState | None,
        holder: _ScopedMutationHolder | None,
    ) -> None:
        self._coordinator = coordinator
        self._key = key
        self._state = state
        self._holder = holder
        self._release_lock = threading.Lock()
        self._released = False

    def update_owner(self, **fields: Any) -> None:
        updates = _owner_metadata(fields)
        if not updates or self._state is None or self._holder is None:
            return
        with self._release_lock:
            if self._released:
                return
            with self._state.condition:
                if self._holder not in self._state.holders:
                    return
                self._holder.owner.update(updates)
                snapshot = dict(self._holder.owner)
        self._coordinator._emit(
            "holder_update",
            self._coordinator._holder_event_fields(self._key, self._holder.scope, snapshot),
        )

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        if self._state is None or self._holder is None:
            return
        with self._state.condition:
            try:
                self._state.holders.remove(self._holder)
            except ValueError:
                return
            self._state.condition.notify_all()
            owner = dict(self._holder.owner)
        fields = self._coordinator._holder_event_fields(self._key, self._holder.scope, owner)
        fields["held_ms"] = round((time.monotonic() - self._holder.acquired_monotonic) * 1000, 3)
        self._coordinator._emit("lease_released", fields)


class WorkspaceMutationCoordinator:
    """Atomically coordinates precise workspace mutation scopes.

    Requests conflict only when their declared scopes overlap. Earlier waiters
    block later requests only when the two requested scopes conflict, preserving
    fairness without serializing unrelated paths.
    """

    def __init__(self, *, event_callback: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._guard = threading.Lock()
        self._states: dict[Hashable, _ScopedMutationState] = {}
        self._closed = threading.Event()
        self._event_callback = event_callback

    def _emit(self, event: str, fields: dict[str, Any]) -> None:
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(event, fields)
        except Exception:
            pass

    def _state(self, key: Hashable) -> _ScopedMutationState:
        with self._guard:
            state = self._states.get(key)
            if state is None:
                state = _ScopedMutationState(threading.Condition(threading.Lock()))
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

    @staticmethod
    def _holder_event_fields(key: Hashable, scope: MutationScope, owner: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": str(key),
            "scope_kind": scope.kind,
            "claim_count": len(scope.claims),
            "holder_action": owner.get("action"),
            "holder_job_id": owner.get("job_id"),
            "holder_pid": owner.get("pid"),
            "holder_extension_id": owner.get("extension_id"),
            "holder_extension_action": owner.get("extension_action"),
            "holder_capability": owner.get("capability"),
            "holder_task": owner.get("task"),
            "holder_path": owner.get("path"),
        }

    @staticmethod
    def _remaining(timeout_seconds: float | None, started_monotonic: float) -> float | None:
        if timeout_seconds is None:
            return None
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        return timeout_seconds - (time.monotonic() - started_monotonic)

    @staticmethod
    def _can_acquire(state: _ScopedMutationState, waiter: _ScopedMutationWaiter) -> bool:
        if any(waiter.scope.conflicts(holder.scope) for holder in state.holders):
            return False
        for queued in state.waiters:
            if queued is waiter:
                break
            if waiter.scope.conflicts(queued.scope):
                return False
        return True

    @staticmethod
    def _blocker(
        state: _ScopedMutationState,
        waiter: _ScopedMutationWaiter,
    ) -> tuple[str, MutationScope, dict[str, Any]]:
        for holder in state.holders:
            if waiter.scope.conflicts(holder.scope):
                return "active_conflict", holder.scope, dict(holder.owner)
        for queued in state.waiters:
            if queued is waiter:
                break
            if waiter.scope.conflicts(queued.scope):
                return "waiting_conflict", queued.scope, dict(queued.owner)
        return "unknown", MutationScope.none(), {}

    def _wait_details(
        self,
        key: Hashable,
        state: _ScopedMutationState,
        waiter: _ScopedMutationWaiter,
    ) -> dict[str, Any]:
        blocking_reason, blocker_scope, holder = self._blocker(state, waiter)
        return {
            "workspace_id": str(key),
            "requested_mode": waiter.scope.kind,
            "requested_claim_count": len(waiter.scope.claims),
            "requester_action": waiter.owner.get("action"),
            "requester_path": waiter.owner.get("path"),
            "requester_extension_id": waiter.owner.get("extension_id"),
            "requester_extension_action": waiter.owner.get("extension_action"),
            "requester_capability": waiter.owner.get("capability"),
            "requester_task": waiter.owner.get("task"),
            "holder_scope_kind": blocker_scope.kind,
            "holder_claim_count": len(blocker_scope.claims),
            "holder_action": holder.get("action"),
            "holder_job_id": holder.get("job_id"),
            "holder_pid": holder.get("pid"),
            "holder_extension_id": holder.get("extension_id"),
            "holder_extension_action": holder.get("extension_action"),
            "holder_capability": holder.get("capability"),
            "holder_task": holder.get("task"),
            "holder_path": holder.get("path"),
            "wait_started": round(waiter.wait_started, 6),
            "wait_ms": round((time.monotonic() - waiter.wait_started_monotonic) * 1000, 3),
            "blocking_reason": blocking_reason,
            "active_holders": len(state.holders),
            "waiting_mutations": len(state.waiters),
        }

    def acquire(
        self,
        key: Hashable,
        scope: MutationScope,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> ScopedMutationLease:
        if not isinstance(scope, MutationScope):
            raise TypeError("scope must be a MutationScope")
        self._ensure_open()
        if scope.kind == "none":
            return ScopedMutationLease(self, key, None, None)
        state = self._state(key)
        waiter = _ScopedMutationWaiter(scope, _owner_metadata(owner), time.time(), time.monotonic())
        wait_event_emitted = False
        initial_wait_details: dict[str, Any] | None = None
        acquired = False
        with state.condition:
            state.waiters.append(waiter)
            try:
                while not self._can_acquire(state, waiter):
                    self._ensure_open()
                    if not wait_event_emitted:
                        initial_wait_details = self._wait_details(key, state, waiter)
                        self._emit("wait_started", initial_wait_details)
                        wait_event_emitted = True
                    remaining = self._remaining(timeout_seconds, waiter.wait_started_monotonic)
                    if remaining is not None and remaining <= 0:
                        details = self._wait_details(key, state, waiter)
                        self._emit("wait_timeout", details)
                        raise WorkspaceMutationBusy(details)
                    state.condition.wait(timeout=remaining)
                self._ensure_open()
                holder = _ScopedMutationHolder(object(), scope, waiter.owner, time.monotonic())
                state.holders.append(holder)
                acquired = True
                if wait_event_emitted and initial_wait_details is not None:
                    details = dict(initial_wait_details)
                    details["wait_ms"] = round((time.monotonic() - waiter.wait_started_monotonic) * 1000, 3)
                    details["active_holders"] = len(state.holders)
                    details["waiting_mutations"] = len(state.waiters)
                    self._emit("wait_acquired", details)
            finally:
                try:
                    state.waiters.remove(waiter)
                except ValueError:
                    pass
                if not acquired:
                    state.condition.notify_all()
        fields = self._holder_event_fields(key, scope, dict(holder.owner))
        self._emit("lease_acquired", fields)
        return ScopedMutationLease(self, key, state, holder)

    @contextmanager
    def hold(
        self,
        key: Hashable,
        scope: MutationScope,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        lease = self.acquire(key, scope, timeout_seconds=timeout_seconds, owner=owner)
        try:
            yield
        finally:
            lease.release()


class MutationLease:
    """Idempotent cross-thread lease used by long-lived opaque mutations."""

    def __init__(
        self,
        gate: WorkspaceMutationGate,
        key: Hashable,
        state: _WorkspaceGateState,
        owner: dict[str, Any],
    ) -> None:
        self._gate = gate
        self._key = key
        self._state = state
        self._owner = owner
        self._release_lock = threading.Lock()
        self._released = False
        self._acquired_monotonic = time.monotonic()

    def update_owner(self, **fields: Any) -> None:
        updates = _owner_metadata(fields)
        if not updates:
            return
        with self._release_lock:
            if self._released:
                return
            with self._state.condition:
                if self._state.exclusive_holder and self._state.exclusive_owner is self._owner:
                    self._owner.update(updates)
                    snapshot = dict(self._owner)
                else:
                    return
        self._gate._emit("holder_update", self._gate._holder_event_fields(self._key, snapshot))

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        with self._state.condition:
            if self._state.exclusive_holder and self._state.exclusive_owner is self._owner:
                self._state.exclusive_holder = False
                self._state.exclusive_owner = None
                self._state.condition.notify_all()
            snapshot = dict(self._owner)
        fields = self._gate._holder_event_fields(self._key, snapshot)
        fields["held_ms"] = round((time.monotonic() - self._acquired_monotonic) * 1000, 3)
        self._gate._emit("exclusive_released", fields)


class WorkspaceMutationGate:
    """Shared leases for known file writes, exclusive leases for opaque mutations.

    Writer preference prevents a stream of independent file writes from starving
    a queued task/build/plugin mutation whose touched paths cannot be predicted.
    Tool-facing callers should use a bounded timeout so a workspace lease can
    never consume the transport response budget while waiting.
    """

    def __init__(self, *, event_callback: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._guard = threading.Lock()
        self._states: dict[Hashable, _WorkspaceGateState] = {}
        self._closed = threading.Event()
        self._event_callback = event_callback

    def _emit(self, event: str, fields: dict[str, Any]) -> None:
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(event, fields)
        except Exception:
            pass

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

    @staticmethod
    def _blocking_owner(state: _WorkspaceGateState, *, requested_mode: str) -> tuple[str, dict[str, Any]]:
        if state.exclusive_holder:
            return "exclusive_holder", dict(state.exclusive_owner or {})
        if requested_mode == "shared" and state.waiting_exclusive:
            return "waiting_exclusive", dict(state.waiting_exclusive[0].owner)
        if state.shared_holders:
            owner = next(iter(state.shared_owners.values()), {})
            return "shared_holder", dict(owner)
        return "unknown", {}

    @staticmethod
    def _holder_event_fields(key: Hashable, owner: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace_id": str(key),
            "holder_action": owner.get("action"),
            "holder_job_id": owner.get("job_id"),
            "holder_pid": owner.get("pid"),
            "holder_extension_id": owner.get("extension_id"),
            "holder_extension_action": owner.get("extension_action"),
            "holder_capability": owner.get("capability"),
            "holder_task": owner.get("task"),
            "holder_path": owner.get("path"),
        }

    def _wait_details(
        self,
        key: Hashable,
        state: _WorkspaceGateState,
        *,
        requested_mode: str,
        requester: dict[str, Any],
        wait_started: float,
        wait_started_monotonic: float,
    ) -> dict[str, Any]:
        blocking_reason, holder = self._blocking_owner(state, requested_mode=requested_mode)
        details = self._holder_event_fields(key, holder)
        details.update(
            {
                "requested_mode": requested_mode,
                "requester_action": requester.get("action"),
                "requester_path": requester.get("path"),
                "requester_extension_id": requester.get("extension_id"),
                "requester_extension_action": requester.get("extension_action"),
                "requester_capability": requester.get("capability"),
                "requester_task": requester.get("task"),
                "wait_started": round(wait_started, 6),
                "wait_ms": round((time.monotonic() - wait_started_monotonic) * 1000, 3),
                "blocking_reason": blocking_reason,
                "shared_holders": state.shared_holders,
                "waiting_exclusive": len(state.waiting_exclusive),
            }
        )
        return details

    @staticmethod
    def _remaining(timeout_seconds: float | None, started_monotonic: float) -> float | None:
        if timeout_seconds is None:
            return None
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        return timeout_seconds - (time.monotonic() - started_monotonic)

    @contextmanager
    def shared(
        self,
        key: Hashable,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        state = self._state(key)
        requester = _owner_metadata(owner)
        token = object()
        wait_started = 0.0
        wait_started_monotonic = 0.0
        initial_wait_details: dict[str, Any] | None = None
        acquired = False
        with state.condition:
            while state.exclusive_holder or state.waiting_exclusive:
                self._ensure_open()
                if wait_started_monotonic == 0.0:
                    wait_started = time.time()
                    wait_started_monotonic = time.monotonic()
                    initial_wait_details = self._wait_details(
                        key,
                        state,
                        requested_mode="shared",
                        requester=requester,
                        wait_started=wait_started,
                        wait_started_monotonic=wait_started_monotonic,
                    )
                    self._emit("wait_started", initial_wait_details)
                remaining = self._remaining(timeout_seconds, wait_started_monotonic)
                if remaining is not None and remaining <= 0:
                    details = self._wait_details(
                        key,
                        state,
                        requested_mode="shared",
                        requester=requester,
                        wait_started=wait_started,
                        wait_started_monotonic=wait_started_monotonic,
                    )
                    self._emit("wait_timeout", details)
                    raise WorkspaceMutationBusy(details)
                state.condition.wait(timeout=remaining)
            self._ensure_open()
            state.shared_holders += 1
            state.shared_owners[token] = requester
            acquired = True
            if wait_started_monotonic and initial_wait_details is not None:
                acquired_details = dict(initial_wait_details)
                acquired_details["wait_ms"] = round((time.monotonic() - wait_started_monotonic) * 1000, 3)
                acquired_details["shared_holders"] = state.shared_holders
                acquired_details["waiting_exclusive"] = len(state.waiting_exclusive)
                self._emit("wait_acquired", acquired_details)
        try:
            yield
        finally:
            if acquired:
                with state.condition:
                    state.shared_owners.pop(token, None)
                    state.shared_holders -= 1
                    if state.shared_holders == 0:
                        state.condition.notify_all()

    def acquire_exclusive(
        self,
        key: Hashable,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> MutationLease:
        state = self._state(key)
        requester = _owner_metadata(owner)
        waiter = _ExclusiveWaiter(requester, time.time(), time.monotonic())
        acquired = False
        wait_event_emitted = False
        initial_wait_details: dict[str, Any] | None = None
        with state.condition:
            state.waiting_exclusive.append(waiter)
            try:
                while state.exclusive_holder or state.shared_holders:
                    self._ensure_open()
                    if not wait_event_emitted:
                        initial_wait_details = self._wait_details(
                            key,
                            state,
                            requested_mode="exclusive",
                            requester=requester,
                            wait_started=waiter.wait_started,
                            wait_started_monotonic=waiter.wait_started_monotonic,
                        )
                        self._emit("wait_started", initial_wait_details)
                        wait_event_emitted = True
                    remaining = self._remaining(timeout_seconds, waiter.wait_started_monotonic)
                    if remaining is not None and remaining <= 0:
                        details = self._wait_details(
                            key,
                            state,
                            requested_mode="exclusive",
                            requester=requester,
                            wait_started=waiter.wait_started,
                            wait_started_monotonic=waiter.wait_started_monotonic,
                        )
                        self._emit("wait_timeout", details)
                        raise WorkspaceMutationBusy(details)
                    state.condition.wait(timeout=remaining)
                self._ensure_open()
                state.exclusive_holder = True
                state.exclusive_owner = requester
                acquired = True
                if wait_event_emitted and initial_wait_details is not None:
                    acquired_details = dict(initial_wait_details)
                    acquired_details["wait_ms"] = round((time.monotonic() - waiter.wait_started_monotonic) * 1000, 3)
                    acquired_details["shared_holders"] = state.shared_holders
                    acquired_details["waiting_exclusive"] = len(state.waiting_exclusive)
                    self._emit("wait_acquired", acquired_details)
            finally:
                try:
                    state.waiting_exclusive.remove(waiter)
                except ValueError:
                    pass
                if not acquired:
                    state.condition.notify_all()
        self._emit("exclusive_acquired", self._holder_event_fields(key, requester))
        return MutationLease(self, key, state, requester)

    @contextmanager
    def exclusive(
        self,
        key: Hashable,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        lease = self.acquire_exclusive(key, timeout_seconds=timeout_seconds, owner=owner)
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
    def hold(
        self,
        key: Hashable,
        *,
        timeout_seconds: float | None = None,
        owner: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        requester = _owner_metadata(owner)
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _ResourceLockEntry(threading.Lock())
                self._entries[key] = entry
            entry.users += 1
        if timeout_seconds is None:
            acquired = entry.lock.acquire()
        else:
            acquired = entry.lock.acquire(timeout=timeout_seconds)
        if not acquired:
            with self._guard:
                holder = dict(entry.owner or {})
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)
            details: dict[str, Any] = {
                "requested_mode": "resource",
                "blocking_reason": "resource_lock",
                "requester_action": requester.get("action"),
                "requester_path": requester.get("path"),
                "holder_action": holder.get("action"),
                "holder_path": holder.get("path"),
                "wait_ms": round(timeout_seconds * 1000, 3),
            }
            if isinstance(key, tuple) and key:
                details["workspace_id"] = str(key[0])
            raise WorkspaceMutationBusy(details)
        with self._guard:
            entry.owner = requester
        try:
            yield
        finally:
            with self._guard:
                entry.owner = None
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)
