from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable


REGISTRY_SCHEMA_VERSION = 6
REGISTRY_COMPAT_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6})
REGISTRY_STATE_NAME = "operation-registry-v1.json"
REGISTRY_PRE_MIGRATION_BACKUP_NAME = "operation-registry-v1.pre-migration.json"
REGISTRY_WRITER_LOCK_NAME = ".operation-registry-v1.writer.lock"
REGISTRY_WRITER_LOCK_TIMEOUT_SECONDS = 10.0
SCHEMA_LIVENESS_STATE_NAME = "operation-schema-liveness-v1.json"
SCHEMA_LIVENESS_STATE_SCHEMA_VERSION = 1
SCHEMA_LIVENESS_LEASE_PREFIX = ".operation-schema-live-"
MAX_SCHEMA_LIVENESS_RECORDS = 128
MAX_SCHEMA_LIVENESS_BYTES = 256 * 1024
MAX_BINARY_VERSION_LENGTH = 80
MAX_CONTAINMENT_IDENTITY_LENGTH = 200
SCHEMA_LIVENESS_PROCESS_ROLES = frozenset({"runtime", "stdio-supervisor"})
OWNER_SNAPSHOT_DIR_NAME = "operation-owner-snapshots-v1"
RECONCILIATION_CAPSULE_DIR_NAME = "operation-reconciliation-capsules-v1"
RECONCILIATION_CAPSULE_SCHEMA_VERSION = 1
MAX_RECONCILIATION_CAPSULE_BYTES = 4 * 1024
MAX_RECONCILIATION_CAPSULE_FILE_BYTES = 8 * 1024
MAX_RECONCILIATION_CAPSULE_FIELDS = 32
MAX_RECONCILIATION_CAPSULE_FIELD_VALUE_LENGTH = 1024
MAX_RECONCILIATION_CAPSULE_GC_ENTRIES_PER_PASS = 4096
RECONCILIATION_CAPSULE_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,79}$")
RECONCILIATION_CAPSULE_FIELD_KINDS = frozenset(
    {
        "opaque_id",
        "sha256",
        "safe_profile",
        "path_class",
        "service_identity",
        "repository_identity",
        "target_identity",
    }
)
DEFAULT_MAX_RECORDS = 4096
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PINNED_VERSIONS = 64
DEFAULT_MAX_PINNED_BYTES = 256 * 1024 * 1024
CORRELATION_KEY_STATE_NAME = "operation-correlation-keys-v1.json"
CORRELATION_KEY_STORE_SCHEMA_VERSION = 1
CORRELATION_KEY_BYTES = 32
MAX_CORRELATION_KEY_STORE_BYTES = 256 * 1024
MAX_CORRELATION_KEY_VERSIONS = 64
MAX_RETRY_FINGERPRINT_MATERIAL_BYTES = 64 * 1024
MAX_RETRY_IDENTITY_BYTES = 4096
MAX_RETRY_AUTHORITY_PROFILE_LENGTH = 160
MAX_RETRY_IDENTITY_NAMESPACE_LENGTH = 160
MAX_RETRY_HORIZON_SECONDS = 2_147_483_647
MAX_OWNER_SNAPSHOT_GC_ENTRIES_PER_PASS = 4096
CORRELATION_KEY_VERSION_RE = re.compile(r"^ck1:[0-9a-f]{32}$")
DPAPI_OPTIONAL_ENTROPY = b"FolderBridge operation correlation key v1"
MAX_OWNER_LENGTH = 160
MAX_WORKSPACE_KEY_LENGTH = 160
MAX_PUBLIC_WORKSPACE_ID_LENGTH = 128
MAX_KEY_VERSION_LENGTH = 80
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EFFECT_SEMANTICS = frozenset(
    {"read_only", "guarded_replay_safe", "external_effect", "unclassified_unsafe"}
)
LIFETIMES = frozenset({"foreground", "job_owned", "managed_service"})
STATES = frozenset(
    {
        "prepared",
        "effect_not_started",
        "effect_attempted",
        "effect_observed",
        "complete",
        "failed",
        "cancelled",
        "termination_uncertain",
        "delivery_ambiguous",
        "runtime_lost",
        "reconciled",
    }
)
SAFE_TERMINAL_STATES = frozenset({"effect_not_started", "complete", "failed", "cancelled", "reconciled"})


class OperationRegistryError(RuntimeError):
    pass


class OperationNotFound(OperationRegistryError):
    pass


class RegistryCapacityExceeded(OperationRegistryError):
    pass


class RegistryPinCapacityExceeded(RegistryCapacityExceeded):
    pass


class CorrelationKeyUnavailable(OperationRegistryError):
    pass


class RecoveryCapsuleUnavailable(OperationRegistryError):
    pass


class RetryIdentityReserved(OperationRegistryError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(f"retry identity is already reserved by operation {operation_id}")


class RegistryRecoveryOnly(OperationRegistryError):
    pass


class RegistrySchemaMigrationBlocked(OperationRegistryError):
    pass


class RegistrySchemaLivenessUnavailable(OperationRegistryError):
    pass


class RegistryWriterBusy(OperationRegistryError):
    pass


class InvalidRegistryTransition(OperationRegistryError):
    pass


@dataclass(frozen=True)
class SchemaLivenessRecord:
    instance_id: str
    boot_id: str
    binary_version: str
    process_role: str
    containment_identity: str
    pid: int
    readable_schema_min: int
    readable_schema_max: int
    writable_schema_min: int
    writable_schema_max: int
    created_at: float


@dataclass(frozen=True)
class ProvenRetryIdentity:
    """Host-internal evidence token for an acceptance-proven retry identity seam.

    Construction is intentionally not exposed through MCP schemas. Callers may
    supply it only after the transport/client capability has independently
    proven retry stability, namespace non-reuse, and a finite retry horizon.
    Raw namespace/identity bytes are never persisted by OperationRegistry.
    """

    authority_profile: str
    identity_namespace: str
    identity: bytes
    retry_horizon_seconds: int

    def __post_init__(self) -> None:
        _bounded_string(
            self.authority_profile,
            field="authority_profile",
            maximum=MAX_RETRY_AUTHORITY_PROFILE_LENGTH,
        )
        _bounded_string(
            self.identity_namespace,
            field="identity_namespace",
            maximum=MAX_RETRY_IDENTITY_NAMESPACE_LENGTH,
        )
        if (
            not isinstance(self.identity, bytes)
            or not self.identity
            or len(self.identity) > MAX_RETRY_IDENTITY_BYTES
        ):
            raise ValueError(
                f"identity must be non-empty bytes <= {MAX_RETRY_IDENTITY_BYTES} bytes"
            )
        if (
            not isinstance(self.retry_horizon_seconds, int)
            or isinstance(self.retry_horizon_seconds, bool)
            or not 1 <= self.retry_horizon_seconds <= MAX_RETRY_HORIZON_SECONDS
        ):
            raise ValueError(
                f"retry_horizon_seconds must be 1..{MAX_RETRY_HORIZON_SECONDS}"
            )


@dataclass(frozen=True)
class RetryAuthorityReservation:
    key_version: str
    identity_mac: str
    authority_profile: str
    retry_horizon_expires_at: float


@dataclass(frozen=True)
class DedupeTombstone:
    key_version: str
    identity_mac: str
    authority_profile: str
    workspace_recovery_key: str
    owner_contract_digest: str
    outcome: str
    retry_horizon_expires_at: float
    settled_at: float


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    boot_id: str
    owner: str
    public_workspace_id: str
    workspace_recovery_key: str
    effect_semantics: str
    lifetime: str
    owner_contract_digest: str
    key_version: str
    state: str
    terminal_reason: str | None
    created_at: float
    updated_at: float

    @property
    def protected(self) -> bool:
        return self.state not in SAFE_TERMINAL_STATES


@dataclass(frozen=True)
class ReconciliationCapsule:
    """Bounded typed non-secret recovery evidence owned by one exact contract."""

    capsule_type: str
    pre_effect_correlation: tuple[tuple[str, str, str], ...]
    evidence: tuple[tuple[str, str, str], ...]
    schema_version: int = RECONCILIATION_CAPSULE_SCHEMA_VERSION

    @staticmethod
    def _normalize_fields(value: Any, *, field: str) -> tuple[tuple[str, str, str], ...]:
        if not isinstance(value, dict) or len(value) > MAX_RECONCILIATION_CAPSULE_FIELDS:
            raise ValueError(
                f"{field} must be an object with at most {MAX_RECONCILIATION_CAPSULE_FIELDS} typed fields"
            )
        normalized: list[tuple[str, str, str]] = []
        for name in sorted(value):
            if not isinstance(name, str) or not RECONCILIATION_CAPSULE_TOKEN_RE.fullmatch(name):
                raise ValueError(f"{field} field names must be bounded non-secret tokens")
            record = value[name]
            if not isinstance(record, dict) or set(record) != {"kind", "value"}:
                raise ValueError(f"{field}.{name} must be a typed field object")
            kind = record["kind"]
            item_value = record["value"]
            if kind not in RECONCILIATION_CAPSULE_FIELD_KINDS:
                raise ValueError(f"{field}.{name} has an unsupported/non-persistable field kind")
            if (
                not isinstance(item_value, str)
                or not item_value
                or len(item_value) > MAX_RECONCILIATION_CAPSULE_FIELD_VALUE_LENGTH
                or any(ord(char) < 0x20 for char in item_value)
            ):
                raise ValueError(f"{field}.{name}.value must be a bounded printable string")
            if kind == "sha256" and not HEX_SHA256_RE.fullmatch(item_value):
                raise ValueError(f"{field}.{name}.value must be lowercase SHA-256")
            normalized.append((name, kind, item_value))
        return tuple(normalized)

    @classmethod
    def create(
        cls,
        *,
        capsule_type: str,
        pre_effect_correlation: dict[str, Any],
        evidence: dict[str, Any],
    ) -> "ReconciliationCapsule":
        if not isinstance(capsule_type, str) or not RECONCILIATION_CAPSULE_TOKEN_RE.fullmatch(capsule_type):
            raise ValueError("capsule_type must be a bounded non-secret token")
        capsule = cls(
            capsule_type=capsule_type,
            pre_effect_correlation=cls._normalize_fields(
                pre_effect_correlation,
                field="pre_effect_correlation",
            ),
            evidence=cls._normalize_fields(evidence, field="evidence"),
        )
        if len(capsule.canonical_bytes()) > MAX_RECONCILIATION_CAPSULE_BYTES:
            raise ValueError("reconciliation capsule exceeds the 4 KiB serialized limit")
        return capsule

    @property
    def has_pre_effect_correlation(self) -> bool:
        return bool(self.pre_effect_correlation)

    def as_payload(self) -> dict[str, Any]:
        def fields(items: tuple[tuple[str, str, str], ...]) -> dict[str, dict[str, str]]:
            return {
                name: {"kind": kind, "value": value}
                for name, kind, value in items
            }

        return {
            "schema_version": self.schema_version,
            "capsule_type": self.capsule_type,
            "pre_effect_correlation": fields(self.pre_effect_correlation),
            "evidence": fields(self.evidence),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.as_payload())

    @classmethod
    def from_payload(cls, value: Any) -> "ReconciliationCapsule":
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "capsule_type",
            "pre_effect_correlation",
            "evidence",
        }:
            raise ValueError("reconciliation capsule payload has invalid fields")
        if value["schema_version"] != RECONCILIATION_CAPSULE_SCHEMA_VERSION or isinstance(
            value["schema_version"], bool
        ):
            raise ValueError("reconciliation capsule schema is unsupported")
        return cls.create(
            capsule_type=value["capsule_type"],
            pre_effect_correlation=value["pre_effect_correlation"],
            evidence=value["evidence"],
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _state_checksum(state_without_checksum: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(state_without_checksum)).hexdigest()


def _retry_identity_mac(secret: bytes, retry_identity: ProvenRetryIdentity) -> str:
    material = (
        b"FolderBridge authoritative retry identity v1\0"
        + retry_identity.identity_namespace.encode("utf-8")
        + b"\0"
        + retry_identity.identity
    )
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _windows_dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def _input_blob(value: bytes) -> tuple[_DataBlob, Any]:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        blob = _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    source, source_buffer = _input_blob(data)
    entropy, entropy_buffer = _input_blob(DPAPI_OPTIONAL_ENTROPY)
    output = _DataBlob()
    _ = source_buffer, entropy_buffer
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source),
            "FolderBridge correlation key",
            ctypes.byref(entropy),
            None,
            None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(output),
        )
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(output),
        )
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(error, "Windows DPAPI operation failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            local_free(ctypes.cast(output.pbData, ctypes.c_void_p))


def _protect_correlation_key(secret: bytes) -> tuple[str, bytes]:
    if os.name == "nt":
        return "windows-dpapi-current-user", _windows_dpapi_transform(secret, protect=True)
    return "posix-user-private", secret


def _unprotect_correlation_key(protection: str, protected: bytes) -> bytes:
    if os.name == "nt":
        if protection != "windows-dpapi-current-user":
            raise CorrelationKeyUnavailable("correlation key protection does not match this platform")
        try:
            return _windows_dpapi_transform(protected, protect=False)
        except OSError as exc:
            raise CorrelationKeyUnavailable("Windows could not recover the persisted correlation key") from exc
    if protection != "posix-user-private":
        raise CorrelationKeyUnavailable("correlation key protection does not match this platform")
    return protected


class _RegistryWriterGate:
    """Short cross-process metadata writer gate.

    Windows uses a non-inheritable byte-range file lock; POSIX uses flock.
    The lock file carries no authority or mutable state, so owner death simply
    releases the OS lock while the checksummed committed snapshot remains the
    recovery authority.
    """

    def __init__(self, path: Path, *, timeout_seconds: float = REGISTRY_WRITER_LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout_seconds = float(timeout_seconds)
        self.fd: int | None = None
        self._locked = False

    def __enter__(self) -> "_RegistryWriterGate":
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self.fd = fd
        try:
            os.set_inheritable(fd, False)
        except OSError:
            os.close(fd)
            self.fd = None
            raise
        try:
            if os.fstat(fd).st_size < 1:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    return self
                except (OSError, BlockingIOError) as exc:
                    if time.monotonic() >= deadline:
                        raise RegistryWriterBusy("operation registry writer gate is busy") from exc
                    time.sleep(0.01)
        except BaseException:
            os.close(fd)
            self.fd = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        fd = self.fd
        self.fd = None
        if fd is None:
            return
        try:
            if self._locked:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            self._locked = False
            os.close(fd)


_PROCESS_SCHEMA_LEASES_LOCK = threading.Lock()
_PROCESS_SCHEMA_LEASES: dict[str, int] = {}


def _try_lock_schema_lease_byte(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_schema_lease_byte(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class RegistrySchemaLivenessLease:
    def __init__(self, registry: Any, instance_id: str, lease_path: Path, fd: int) -> None:
        self._registry = registry
        self.instance_id = instance_id
        self._lease_path = lease_path
        self._fd: int | None = fd

    @property
    def closed(self) -> bool:
        return self._fd is None

    def close(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._registry._release_schema_liveness(self.instance_id)
        self._fd = None
        with _PROCESS_SCHEMA_LEASES_LOCK:
            _PROCESS_SCHEMA_LEASES.pop(str(self._lease_path), None)
        try:
            _unlock_schema_lease_byte(fd)
        finally:
            os.close(fd)
        try:
            self._lease_path.unlink()
        except (FileNotFoundError, PermissionError, OSError):
            pass

    def __enter__(self) -> "RegistrySchemaLivenessLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _bounded_string(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field} must be a non-empty string <= {maximum} characters")
    if any(ord(char) < 0x20 for char in value):
        raise ValueError(f"{field} may not contain control characters")
    return value


def _validate_receipt_dict(raw: Any) -> OperationReceipt:
    expected = {
        "operation_id",
        "boot_id",
        "owner",
        "public_workspace_id",
        "workspace_recovery_key",
        "effect_semantics",
        "lifetime",
        "owner_contract_digest",
        "key_version",
        "state",
        "terminal_reason",
        "created_at",
        "updated_at",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError("registry receipt has an invalid field set")
    operation_id = raw["operation_id"]
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("registry receipt operation_id is invalid")
    boot_id = _bounded_string(raw["boot_id"], field="boot_id", maximum=160)
    owner = _bounded_string(raw["owner"], field="owner", maximum=MAX_OWNER_LENGTH)
    public_workspace_id = _bounded_string(
        raw["public_workspace_id"],
        field="public_workspace_id",
        maximum=MAX_PUBLIC_WORKSPACE_ID_LENGTH,
    )
    workspace_recovery_key = _bounded_string(
        raw["workspace_recovery_key"],
        field="workspace_recovery_key",
        maximum=MAX_WORKSPACE_KEY_LENGTH,
    )
    effect_semantics = raw["effect_semantics"]
    if effect_semantics not in EFFECT_SEMANTICS:
        raise ValueError("registry receipt effect_semantics is invalid")
    lifetime = raw["lifetime"]
    if lifetime not in LIFETIMES:
        raise ValueError("registry receipt lifetime is invalid")
    owner_contract_digest = raw["owner_contract_digest"]
    if not isinstance(owner_contract_digest, str) or not HEX_SHA256_RE.fullmatch(owner_contract_digest):
        raise ValueError("registry receipt owner_contract_digest must be lowercase SHA-256")
    key_version = _bounded_string(
        raw["key_version"],
        field="key_version",
        maximum=MAX_KEY_VERSION_LENGTH,
    )
    state = raw["state"]
    if state not in STATES:
        raise ValueError("registry receipt state is invalid")
    terminal_reason = raw["terminal_reason"]
    if terminal_reason is not None:
        terminal_reason = _bounded_string(
            terminal_reason,
            field="terminal_reason",
            maximum=160,
        )
    created_at = raw["created_at"]
    updated_at = raw["updated_at"]
    if (
        not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not isinstance(updated_at, (int, float))
        or isinstance(updated_at, bool)
        or created_at < 0
        or updated_at < created_at
    ):
        raise ValueError("registry receipt timestamps are invalid")
    return OperationReceipt(
        operation_id=operation_id,
        boot_id=boot_id,
        owner=owner,
        public_workspace_id=public_workspace_id,
        workspace_recovery_key=workspace_recovery_key,
        effect_semantics=effect_semantics,
        lifetime=lifetime,
        owner_contract_digest=owner_contract_digest,
        key_version=key_version,
        state=state,
        terminal_reason=terminal_reason,
        created_at=float(created_at),
        updated_at=float(updated_at),
    )


class OperationRegistry:
    """Bounded durable operation-recovery authority.

    Phase 0 deliberately separates this protected state from ordinary in-memory
    Job retention. This first scaffold uses a checksummed atomic snapshot and a
    process-local lock; the cross-process Registry Writer Gate is layered on by
    the next acceptance slice before production multi-process admission.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pinned_versions: int = DEFAULT_MAX_PINNED_VERSIONS,
        max_pinned_bytes: int = DEFAULT_MAX_PINNED_BYTES,
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be 1..1000000")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 4096 <= max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("max_bytes must be 4096..1073741824")
        if not isinstance(max_pinned_versions, int) or isinstance(max_pinned_versions, bool) or not 1 <= max_pinned_versions <= 1_000_000:
            raise ValueError("max_pinned_versions must be 1..1000000")
        if not isinstance(max_pinned_bytes, int) or isinstance(max_pinned_bytes, bool) or not 1 <= max_pinned_bytes <= 16 * 1024 * 1024 * 1024:
            raise ValueError("max_pinned_bytes must be 1..17179869184")
        self.root = Path(root)
        self.path = self.root / REGISTRY_STATE_NAME
        self.pre_migration_backup_path = self.root / REGISTRY_PRE_MIGRATION_BACKUP_NAME
        self.writer_lock_path = self.root / REGISTRY_WRITER_LOCK_NAME
        self.schema_liveness_path = self.root / SCHEMA_LIVENESS_STATE_NAME
        self.owner_snapshot_dir = self.root / OWNER_SNAPSHOT_DIR_NAME
        self.reconciliation_capsule_dir = self.root / RECONCILIATION_CAPSULE_DIR_NAME
        self.correlation_key_path = self.root / CORRELATION_KEY_STATE_NAME
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.max_pinned_versions = max_pinned_versions
        self.max_pinned_bytes = max_pinned_bytes
        self._lock = threading.RLock()
        self._generation = 0
        self._loaded_schema_version: int | None = None
        self._loaded_committed_bytes: bytes | None = None
        self._records: dict[str, OperationReceipt] = {}
        self._owner_snapshot_pins: dict[str, str] = {}
        self._retry_fingerprints: dict[str, str] = {}
        self._retry_authorities: dict[str, RetryAuthorityReservation] = {}
        self._dedupe_tombstones: dict[str, DedupeTombstone] = {}
        self.recovery_only = False
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._load_committed()

    def _reload_for_transaction(self) -> None:
        """Reload the authoritative committed generation while Writer Gate is held."""

        self._load_committed()

    def _refresh_for_read(self) -> None:
        # Phase-0 keeps reads simple and authoritative. A later optimized reader
        # may use generation/checksum optimistic validation, but never a stale
        # process-local cache as recovery authority.
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._load_committed()
            if self.recovery_only:
                raise RegistryRecoveryOnly("operation registry committed state is corrupt")

    def _schema_lease_path(self, instance_id: str) -> Path:
        return self.root / f"{SCHEMA_LIVENESS_LEASE_PREFIX}{instance_id}.lock"

    def _write_schema_liveness_state_locked(
        self,
        records: dict[str, SchemaLivenessRecord],
        generation: int,
    ) -> None:
        unsigned = {
            "schema_version": SCHEMA_LIVENESS_STATE_SCHEMA_VERSION,
            "generation": generation,
            "records": {
                instance_id: asdict(records[instance_id])
                for instance_id in sorted(records)
            },
        }
        payload = dict(unsigned)
        payload["checksum"] = _state_checksum(unsigned)
        data = _canonical_json(payload) + b"\n"
        if len(data) > MAX_SCHEMA_LIVENESS_BYTES:
            raise RegistrySchemaLivenessUnavailable(
                "schema liveness metadata exceeds its bounded size"
            )
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{SCHEMA_LIVENESS_STATE_NAME}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.schema_liveness_path)
            temporary = None
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _load_schema_liveness_locked(
        self,
        *,
        reap_stale: bool,
    ) -> tuple[int, dict[str, SchemaLivenessRecord]]:
        if not self.schema_liveness_path.exists():
            return 0, {}
        try:
            if self.schema_liveness_path.is_symlink() or not self.schema_liveness_path.is_file():
                raise ValueError("schema liveness state path is not a regular file")
            data = self.schema_liveness_path.read_bytes()
            if len(data) > MAX_SCHEMA_LIVENESS_BYTES:
                raise ValueError("schema liveness metadata exceeds its bounded size")
            raw = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "generation",
                "records",
                "checksum",
            }:
                raise ValueError("schema liveness metadata has invalid fields")
            if (
                raw["schema_version"] != SCHEMA_LIVENESS_STATE_SCHEMA_VERSION
                or isinstance(raw["schema_version"], bool)
            ):
                raise ValueError("unsupported schema liveness metadata version")
            generation = raw["generation"]
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                raise ValueError("schema liveness generation is invalid")
            records_raw = raw["records"]
            if (
                not isinstance(records_raw, dict)
                or len(records_raw) > MAX_SCHEMA_LIVENESS_RECORDS
            ):
                raise ValueError("schema liveness record set exceeds its bounded capacity")
            unsigned = {
                "schema_version": raw["schema_version"],
                "generation": generation,
                "records": records_raw,
            }
            checksum = raw["checksum"]
            if not isinstance(checksum, str) or not HEX_SHA256_RE.fullmatch(checksum):
                raise ValueError("schema liveness checksum is invalid")
            if _state_checksum(unsigned) != checksum:
                raise ValueError("schema liveness checksum mismatch")

            records: dict[str, SchemaLivenessRecord] = {}
            expected_fields = {
                "instance_id",
                "boot_id",
                "binary_version",
                "process_role",
                "containment_identity",
                "pid",
                "readable_schema_min",
                "readable_schema_max",
                "writable_schema_min",
                "writable_schema_max",
                "created_at",
            }
            for instance_id, item in records_raw.items():
                if (
                    not isinstance(instance_id, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", instance_id)
                    or not isinstance(item, dict)
                    or set(item) != expected_fields
                    or item["instance_id"] != instance_id
                ):
                    raise ValueError("schema liveness record identity is invalid")
                boot_id = _bounded_string(item["boot_id"], field="boot_id", maximum=160)
                binary_version = _bounded_string(
                    item["binary_version"],
                    field="binary_version",
                    maximum=MAX_BINARY_VERSION_LENGTH,
                )
                process_role = item["process_role"]
                if process_role not in SCHEMA_LIVENESS_PROCESS_ROLES:
                    raise ValueError("schema liveness process role is invalid")
                containment_identity = _bounded_string(
                    item["containment_identity"],
                    field="containment_identity",
                    maximum=MAX_CONTAINMENT_IDENTITY_LENGTH,
                )
                pid = item["pid"]
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    raise ValueError("schema liveness pid is invalid")
                ranges: list[int] = []
                for field in (
                    "readable_schema_min",
                    "readable_schema_max",
                    "writable_schema_min",
                    "writable_schema_max",
                ):
                    value = item[field]
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or not 1 <= value <= 1_000_000
                    ):
                        raise ValueError(f"{field} is invalid")
                    ranges.append(value)
                readable_min, readable_max, writable_min, writable_max = ranges
                if readable_min > readable_max or writable_min > writable_max:
                    raise ValueError("schema liveness range is inverted")
                created_at = item["created_at"]
                if (
                    not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(float(created_at))
                    or float(created_at) < 0
                ):
                    raise ValueError("schema liveness timestamp is invalid")
                records[instance_id] = SchemaLivenessRecord(
                    instance_id=instance_id,
                    boot_id=boot_id,
                    binary_version=binary_version,
                    process_role=process_role,
                    containment_identity=containment_identity,
                    pid=pid,
                    readable_schema_min=readable_min,
                    readable_schema_max=readable_max,
                    writable_schema_min=writable_min,
                    writable_schema_max=writable_max,
                    created_at=float(created_at),
                )

            if not reap_stale or not records:
                return generation, records

            live: dict[str, SchemaLivenessRecord] = {}
            stale_paths: list[Path] = []
            for instance_id, record in records.items():
                lease_path = self._schema_lease_path(instance_id)
                local_live = False
                with _PROCESS_SCHEMA_LEASES_LOCK:
                    local_fd = _PROCESS_SCHEMA_LEASES.get(str(lease_path))
                    if local_fd is not None:
                        try:
                            os.fstat(local_fd)
                            local_live = True
                        except OSError:
                            _PROCESS_SCHEMA_LEASES.pop(str(lease_path), None)
                if local_live:
                    live[instance_id] = record
                    continue
                if (
                    not lease_path.exists()
                    or lease_path.is_symlink()
                    or not lease_path.is_file()
                ):
                    if lease_path.is_symlink():
                        raise ValueError("schema liveness lease path is a symlink")
                    stale_paths.append(lease_path)
                    continue
                fd = os.open(lease_path, os.O_RDWR)
                try:
                    os.set_inheritable(fd, False)
                    if os.fstat(fd).st_size < 1:
                        raise ValueError("schema liveness lease file is truncated")
                    acquired = _try_lock_schema_lease_byte(fd)
                    if acquired:
                        try:
                            stale_paths.append(lease_path)
                        finally:
                            _unlock_schema_lease_byte(fd)
                    else:
                        live[instance_id] = record
                finally:
                    os.close(fd)

            if len(live) != len(records):
                generation += 1
                self._write_schema_liveness_state_locked(live, generation)
                for lease_path in stale_paths:
                    try:
                        lease_path.unlink()
                    except (FileNotFoundError, PermissionError, OSError):
                        pass
            return generation, live
        except RegistrySchemaLivenessUnavailable:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RegistrySchemaLivenessUnavailable(
                "schema liveness metadata is unavailable or corrupt"
            ) from exc

    def _assert_schema_write_target_locked(self, target_schema: int) -> None:
        if (
            self._loaded_schema_version is not None
            and target_schema < self._loaded_schema_version
        ):
            raise RegistrySchemaMigrationBlocked(
                "operation registry schema downgrade is blocked"
            )
        _, records = self._load_schema_liveness_locked(reap_stale=True)
        blockers = [
            record
            for record in records.values()
            if not record.readable_schema_min <= target_schema <= record.readable_schema_max
        ]
        if blockers:
            identities = ",".join(sorted(record.instance_id for record in blockers)[:4])
            raise RegistrySchemaMigrationBlocked(
                f"target registry schema {target_schema} is unreadable by "
                f"{len(blockers)} live process(es): {identities}"
            )

    def register_schema_liveness(
        self,
        *,
        boot_id: str,
        binary_version: str,
        process_role: str,
        containment_identity: str,
        readable_schema_min: int,
        readable_schema_max: int,
        writable_schema_min: int,
        writable_schema_max: int,
    ) -> RegistrySchemaLivenessLease:
        boot_id = _bounded_string(boot_id, field="boot_id", maximum=160)
        binary_version = _bounded_string(
            binary_version,
            field="binary_version",
            maximum=MAX_BINARY_VERSION_LENGTH,
        )
        if process_role not in SCHEMA_LIVENESS_PROCESS_ROLES:
            raise ValueError("process_role must be runtime or stdio-supervisor")
        containment_identity = _bounded_string(
            containment_identity,
            field="containment_identity",
            maximum=MAX_CONTAINMENT_IDENTITY_LENGTH,
        )
        ranges = (
            readable_schema_min,
            readable_schema_max,
            writable_schema_min,
            writable_schema_max,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 1_000_000
            for value in ranges
        ):
            raise ValueError("schema ranges must contain integers in 1..1000000")
        if readable_schema_min > readable_schema_max or writable_schema_min > writable_schema_max:
            raise ValueError("schema ranges may not be inverted")

        instance_id = ""
        lease_path: Path | None = None
        fd: int | None = None
        for _ in range(8):
            candidate = uuid.uuid4().hex
            candidate_path = self._schema_lease_path(candidate)
            try:
                candidate_fd = os.open(
                    candidate_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
            except FileExistsError:
                continue
            instance_id = candidate
            lease_path = candidate_path
            fd = candidate_fd
            break
        if fd is None or lease_path is None:
            raise RegistrySchemaLivenessUnavailable(
                "could not allocate a unique schema liveness lease"
            )
        try:
            os.set_inheritable(fd, False)
            os.write(fd, b"\0")
            os.fsync(fd)
            if not _try_lock_schema_lease_byte(fd):
                raise RegistrySchemaLivenessUnavailable(
                    "new schema liveness lease could not be acquired"
                )
            with self._lock:
                with _RegistryWriterGate(self.writer_lock_path):
                    self._load_committed()
                    if self.recovery_only:
                        raise RegistryRecoveryOnly("operation registry is recovery-only")
                    if (
                        self._loaded_schema_version is not None
                        and not readable_schema_min
                        <= self._loaded_schema_version
                        <= readable_schema_max
                    ):
                        raise RegistrySchemaMigrationBlocked(
                            f"existing registry schema {self._loaded_schema_version} "
                            "is unreadable by this process"
                        )
                    generation, records = self._load_schema_liveness_locked(reap_stale=True)
                    if len(records) >= MAX_SCHEMA_LIVENESS_RECORDS:
                        raise RegistrySchemaLivenessUnavailable(
                            "schema liveness record capacity is exhausted"
                        )
                    records = dict(records)
                    records[instance_id] = SchemaLivenessRecord(
                        instance_id=instance_id,
                        boot_id=boot_id,
                        binary_version=binary_version,
                        process_role=process_role,
                        containment_identity=containment_identity,
                        pid=os.getpid(),
                        readable_schema_min=readable_schema_min,
                        readable_schema_max=readable_schema_max,
                        writable_schema_min=writable_schema_min,
                        writable_schema_max=writable_schema_max,
                        created_at=time.time(),
                    )
                    self._write_schema_liveness_state_locked(records, generation + 1)
            with _PROCESS_SCHEMA_LEASES_LOCK:
                _PROCESS_SCHEMA_LEASES[str(lease_path)] = fd
            return RegistrySchemaLivenessLease(self, instance_id, lease_path, fd)
        except BaseException:
            try:
                _unlock_schema_lease_byte(fd)
            except OSError:
                pass
            os.close(fd)
            try:
                lease_path.unlink()
            except (FileNotFoundError, PermissionError, OSError):
                pass
            raise

    def _release_schema_liveness(self, instance_id: str) -> None:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                generation, records = self._load_schema_liveness_locked(reap_stale=True)
                if instance_id not in records:
                    return
                updated = dict(records)
                updated.pop(instance_id, None)
                self._write_schema_liveness_state_locked(updated, generation + 1)

    def schema_liveness_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                _, records = self._load_schema_liveness_locked(reap_stale=True)
                return [
                    asdict(records[instance_id])
                    for instance_id in sorted(records)
                ]

    def _write_correlation_key_state(self, unsigned: dict[str, Any]) -> None:
        payload = dict(unsigned)
        payload["checksum"] = _state_checksum(unsigned)
        data = _canonical_json(payload) + b"\n"
        if len(data) > MAX_CORRELATION_KEY_STORE_BYTES:
            raise CorrelationKeyUnavailable("correlation key store exceeds its bounded size")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{CORRELATION_KEY_STATE_NAME}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.correlation_key_path)
            temporary = None
            if os.name != "nt":
                os.chmod(self.correlation_key_path, 0o600)
                try:
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _load_correlation_keys_locked(
        self,
        *,
        create_if_missing: bool,
    ) -> tuple[str, dict[str, bytes]]:
        if not self.correlation_key_path.exists():
            if not create_if_missing:
                raise CorrelationKeyUnavailable("correlation key store does not exist")
            version = f"ck1:{uuid.uuid4().hex}"
            secret = secrets.token_bytes(CORRELATION_KEY_BYTES)
            try:
                protection, protected = _protect_correlation_key(secret)
            except OSError as exc:
                raise CorrelationKeyUnavailable("could not protect a new correlation key") from exc
            unsigned = {
                "schema_version": CORRELATION_KEY_STORE_SCHEMA_VERSION,
                "active_version": version,
                "keys": {
                    version: {
                        "protection": protection,
                        "material": base64.b64encode(protected).decode("ascii"),
                    }
                },
            }
            self._write_correlation_key_state(unsigned)
            return version, {version: secret}
        try:
            if self.correlation_key_path.is_symlink() or not self.correlation_key_path.is_file():
                raise ValueError("correlation key state path is not a regular file")
            data = self.correlation_key_path.read_bytes()
            if len(data) > MAX_CORRELATION_KEY_STORE_BYTES:
                raise ValueError("correlation key store exceeds its bounded size")
            raw = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "active_version",
                "keys",
                "checksum",
            }:
                raise ValueError("correlation key store has invalid fields")
            if (
                raw["schema_version"] != CORRELATION_KEY_STORE_SCHEMA_VERSION
                or isinstance(raw["schema_version"], bool)
            ):
                raise ValueError("unsupported correlation key store schema")
            active_version = raw["active_version"]
            if not isinstance(active_version, str) or not CORRELATION_KEY_VERSION_RE.fullmatch(active_version):
                raise ValueError("active correlation key version is invalid")
            keys_raw = raw["keys"]
            if not isinstance(keys_raw, dict) or not 1 <= len(keys_raw) <= MAX_CORRELATION_KEY_VERSIONS:
                raise ValueError("correlation key version set is invalid")
            unsigned = {
                "schema_version": raw["schema_version"],
                "active_version": active_version,
                "keys": keys_raw,
            }
            checksum = raw["checksum"]
            if not isinstance(checksum, str) or not HEX_SHA256_RE.fullmatch(checksum):
                raise ValueError("correlation key store checksum is invalid")
            if _state_checksum(unsigned) != checksum:
                raise ValueError("correlation key store checksum mismatch")
            keys: dict[str, bytes] = {}
            for version, record in keys_raw.items():
                if not isinstance(version, str) or not CORRELATION_KEY_VERSION_RE.fullmatch(version):
                    raise ValueError("correlation key version is invalid")
                if not isinstance(record, dict) or set(record) != {"protection", "material"}:
                    raise ValueError("correlation key record is invalid")
                protection = record["protection"]
                material = record["material"]
                if not isinstance(protection, str) or not isinstance(material, str):
                    raise ValueError("correlation key record fields are invalid")
                try:
                    protected = base64.b64decode(material, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("correlation key material is not valid base64") from exc
                secret = _unprotect_correlation_key(protection, protected)
                if len(secret) != CORRELATION_KEY_BYTES:
                    raise ValueError("correlation key material has an invalid length")
                keys[version] = secret
            if active_version not in keys:
                raise ValueError("active correlation key is missing")
            return active_version, keys
        except CorrelationKeyUnavailable:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CorrelationKeyUnavailable("correlation key store is unavailable or corrupt") from exc

    def active_correlation_key(self) -> tuple[str, bytes]:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                active_version, keys = self._load_correlation_keys_locked(create_if_missing=True)
                return active_version, keys[active_version]

    def correlation_key(self, key_version: str) -> bytes:
        if not isinstance(key_version, str) or not CORRELATION_KEY_VERSION_RE.fullmatch(key_version):
            raise CorrelationKeyUnavailable("correlation key version is invalid")
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                _, keys = self._load_correlation_keys_locked(create_if_missing=False)
                secret = keys.get(key_version)
                if secret is None:
                    raise CorrelationKeyUnavailable("correlation key version is unavailable")
                return secret

    def _persist_correlation_keys_locked(
        self,
        *,
        active_version: str,
        keys: dict[str, bytes],
    ) -> None:
        if active_version not in keys:
            raise CorrelationKeyUnavailable("active correlation key is missing")
        protected_records: dict[str, dict[str, str]] = {}
        try:
            for item_version, item_secret in keys.items():
                protection, protected = _protect_correlation_key(item_secret)
                protected_records[item_version] = {
                    "protection": protection,
                    "material": base64.b64encode(protected).decode("ascii"),
                }
        except OSError as exc:
            raise CorrelationKeyUnavailable(
                "could not protect the complete correlation key version set"
            ) from exc
        unsigned = {
            "schema_version": CORRELATION_KEY_STORE_SCHEMA_VERSION,
            "active_version": active_version,
            "keys": protected_records,
        }
        self._write_correlation_key_state(unsigned)

    def rotate_correlation_key(self) -> tuple[str, bytes]:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                _, keys = self._load_correlation_keys_locked(create_if_missing=False)
                if len(keys) >= MAX_CORRELATION_KEY_VERSIONS:
                    raise CorrelationKeyUnavailable("correlation key version capacity is exhausted")
                version = ""
                for _ in range(8):
                    candidate = f"ck1:{uuid.uuid4().hex}"
                    if candidate not in keys:
                        version = candidate
                        break
                if not version:
                    raise CorrelationKeyUnavailable("could not allocate a unique correlation key version")
                secret = secrets.token_bytes(CORRELATION_KEY_BYTES)
                updated = dict(keys)
                updated[version] = secret
                self._persist_correlation_keys_locked(
                    active_version=version,
                    keys=updated,
                )
                return version, secret

    def expire_dedupe_tombstones(self, *, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        if (
            isinstance(current_time, bool)
            or not isinstance(current_time, (int, float))
            or not math.isfinite(float(current_time))
        ):
            raise ValueError("now must be a finite timestamp")
        cutoff = float(current_time)
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                tombstones = {
                    operation_id: tombstone
                    for operation_id, tombstone in self._dedupe_tombstones.items()
                    if tombstone.retry_horizon_expires_at > cutoff
                }
                removed = len(self._dedupe_tombstones) - len(tombstones)
                if not removed:
                    return 0
                self._commit(
                    dict(self._records),
                    dedupe_tombstones=tombstones,
                )
                return removed

    def garbage_collect_correlation_keys(self) -> int:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                active_version, keys = self._load_correlation_keys_locked(create_if_missing=False)
                referenced: set[str] = {active_version}
                for operation_id, receipt in self._records.items():
                    if (
                        CORRELATION_KEY_VERSION_RE.fullmatch(receipt.key_version)
                        and (
                            receipt.protected
                            or operation_id in self._retry_fingerprints
                            or operation_id in self._retry_authorities
                        )
                    ):
                        referenced.add(receipt.key_version)
                referenced.update(
                    authority.key_version for authority in self._retry_authorities.values()
                )
                referenced.update(
                    tombstone.key_version for tombstone in self._dedupe_tombstones.values()
                )
                retired = [version for version in keys if version not in referenced]
                if not retired:
                    return 0
                retained = {
                    version: secret
                    for version, secret in keys.items()
                    if version in referenced
                }
                self._persist_correlation_keys_locked(
                    active_version=active_version,
                    keys=retained,
                )
                return len(retired)

    def _load_committed(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._generation = 0
                self._loaded_schema_version = None
                self._loaded_committed_bytes = None
                self._records = {}
                self._owner_snapshot_pins = {}
                self._retry_fingerprints = {}
                self._retry_authorities = {}
                self._dedupe_tombstones = {}
                self.recovery_only = False
                return
            try:
                if self.path.is_symlink() or not self.path.is_file():
                    raise ValueError("registry state path is not a regular file")
                data = self.path.read_bytes()
                if len(data) > self.max_bytes:
                    raise ValueError("registry state exceeds configured byte cap")
                raw = json.loads(data)
                if not isinstance(raw, dict):
                    raise ValueError("registry state must be an object")
                schema_version = raw.get("schema_version")
                if (
                    not isinstance(schema_version, int)
                    or isinstance(schema_version, bool)
                    or schema_version not in REGISTRY_COMPAT_SCHEMA_VERSIONS
                ):
                    raise ValueError("unsupported registry schema version")
                expected_fields = {
                    "schema_version",
                    "generation",
                    "records",
                    "checksum",
                }
                if schema_version >= 2:
                    expected_fields.add("owner_snapshot_pins")
                if schema_version >= 3:
                    expected_fields.add("retry_fingerprints")
                if schema_version >= 4:
                    expected_fields.add("retry_authorities")
                if schema_version >= 5:
                    expected_fields.add("dedupe_tombstones")
                if set(raw) != expected_fields:
                    raise ValueError("registry state has invalid fields")
                generation = raw["generation"]
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                    raise ValueError("registry generation is invalid")
                records_raw = raw["records"]
                if not isinstance(records_raw, list) or len(records_raw) > self.max_records:
                    raise ValueError("registry record set exceeds configured cap")
                pins_raw = raw.get("owner_snapshot_pins", {})
                if not isinstance(pins_raw, dict) or len(pins_raw) > self.max_records:
                    raise ValueError("registry owner snapshot pins are invalid")
                fingerprints_raw = raw.get("retry_fingerprints", {})
                if not isinstance(fingerprints_raw, dict) or len(fingerprints_raw) > self.max_records:
                    raise ValueError("registry retry fingerprints are invalid")
                authorities_raw = raw.get("retry_authorities", {})
                if not isinstance(authorities_raw, dict) or len(authorities_raw) > self.max_records:
                    raise ValueError("registry retry authorities are invalid")
                tombstones_raw = raw.get("dedupe_tombstones", {})
                if not isinstance(tombstones_raw, dict) or len(tombstones_raw) > self.max_records:
                    raise ValueError("registry dedupe tombstones are invalid")
                checksum = raw["checksum"]
                unsigned = {
                    "schema_version": schema_version,
                    "generation": generation,
                    "records": records_raw,
                }
                if schema_version >= 2:
                    unsigned["owner_snapshot_pins"] = pins_raw
                if schema_version >= 3:
                    unsigned["retry_fingerprints"] = fingerprints_raw
                if schema_version >= 4:
                    unsigned["retry_authorities"] = authorities_raw
                if schema_version >= 5:
                    unsigned["dedupe_tombstones"] = tombstones_raw
                if not isinstance(checksum, str) or not HEX_SHA256_RE.fullmatch(checksum):
                    raise ValueError("registry checksum is invalid")
                if not hashlib.sha256(_canonical_json(unsigned)).hexdigest() == checksum:
                    raise ValueError("registry checksum mismatch")
                records: dict[str, OperationReceipt] = {}
                for item in records_raw:
                    receipt = _validate_receipt_dict(item)
                    if receipt.operation_id in records:
                        raise ValueError("duplicate operation_id in registry")
                    records[receipt.operation_id] = receipt
                pins: dict[str, str] = {}
                for operation_id, digest in pins_raw.items():
                    if operation_id not in records:
                        raise ValueError("owner snapshot pin references an unknown operation")
                    if not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
                        raise ValueError("owner snapshot digest is invalid")
                    pins[operation_id] = digest
                for digest in set(pins.values()):
                    self._read_owner_snapshot_blob(digest)
                fingerprints: dict[str, str] = {}
                for operation_id, fingerprint in fingerprints_raw.items():
                    receipt = records.get(operation_id)
                    if receipt is None:
                        raise ValueError("retry fingerprint references an unknown operation")
                    if not isinstance(fingerprint, str) or not HEX_SHA256_RE.fullmatch(fingerprint):
                        raise ValueError("retry fingerprint is invalid")
                    if not CORRELATION_KEY_VERSION_RE.fullmatch(receipt.key_version):
                        raise ValueError("retry fingerprint receipt key version is invalid")
                    fingerprints[operation_id] = fingerprint
                authorities: dict[str, RetryAuthorityReservation] = {}
                for operation_id, item in authorities_raw.items():
                    receipt = records.get(operation_id)
                    if receipt is None:
                        raise ValueError("retry authority references an unknown operation")
                    if not isinstance(item, dict) or set(item) != {
                        "key_version",
                        "identity_mac",
                        "authority_profile",
                        "retry_horizon_expires_at",
                    }:
                        raise ValueError("retry authority record is invalid")
                    authority_key_version = item["key_version"]
                    identity_mac = item["identity_mac"]
                    authority_profile = item["authority_profile"]
                    expires_at = item["retry_horizon_expires_at"]
                    if (
                        not isinstance(authority_key_version, str)
                        or not CORRELATION_KEY_VERSION_RE.fullmatch(authority_key_version)
                    ):
                        raise ValueError("retry authority key version is invalid")
                    if not isinstance(identity_mac, str) or not HEX_SHA256_RE.fullmatch(identity_mac):
                        raise ValueError("retry authority identity MAC is invalid")
                    authority_profile = _bounded_string(
                        authority_profile,
                        field="authority_profile",
                        maximum=MAX_RETRY_AUTHORITY_PROFILE_LENGTH,
                    )
                    if (
                        not isinstance(expires_at, (int, float))
                        or isinstance(expires_at, bool)
                        or not math.isfinite(float(expires_at))
                        or float(expires_at) < receipt.created_at
                    ):
                        raise ValueError("retry authority horizon is invalid")
                    authorities[operation_id] = RetryAuthorityReservation(
                        key_version=authority_key_version,
                        identity_mac=identity_mac,
                        authority_profile=authority_profile,
                        retry_horizon_expires_at=float(expires_at),
                    )
                tombstones: dict[str, DedupeTombstone] = {}
                for operation_id, item in tombstones_raw.items():
                    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                        raise ValueError("dedupe tombstone operation_id is invalid")
                    if not isinstance(item, dict) or set(item) != {
                        "key_version",
                        "identity_mac",
                        "authority_profile",
                        "workspace_recovery_key",
                        "owner_contract_digest",
                        "outcome",
                        "retry_horizon_expires_at",
                        "settled_at",
                    }:
                        raise ValueError("dedupe tombstone record is invalid")
                    tombstone_key_version = item["key_version"]
                    tombstone_identity_mac = item["identity_mac"]
                    tombstone_profile = _bounded_string(
                        item["authority_profile"],
                        field="authority_profile",
                        maximum=MAX_RETRY_AUTHORITY_PROFILE_LENGTH,
                    )
                    tombstone_workspace_key = _bounded_string(
                        item["workspace_recovery_key"],
                        field="workspace_recovery_key",
                        maximum=MAX_WORKSPACE_KEY_LENGTH,
                    )
                    tombstone_owner_digest = item["owner_contract_digest"]
                    tombstone_outcome = item["outcome"]
                    tombstone_expires_at = item["retry_horizon_expires_at"]
                    tombstone_settled_at = item["settled_at"]
                    if (
                        not isinstance(tombstone_key_version, str)
                        or not CORRELATION_KEY_VERSION_RE.fullmatch(tombstone_key_version)
                    ):
                        raise ValueError("dedupe tombstone key version is invalid")
                    if (
                        not isinstance(tombstone_identity_mac, str)
                        or not HEX_SHA256_RE.fullmatch(tombstone_identity_mac)
                    ):
                        raise ValueError("dedupe tombstone identity MAC is invalid")
                    if (
                        not isinstance(tombstone_owner_digest, str)
                        or not HEX_SHA256_RE.fullmatch(tombstone_owner_digest)
                    ):
                        raise ValueError("dedupe tombstone owner contract digest is invalid")
                    if tombstone_outcome != "effect_present":
                        raise ValueError("dedupe tombstone outcome is invalid")
                    if (
                        not isinstance(tombstone_expires_at, (int, float))
                        or isinstance(tombstone_expires_at, bool)
                        or not math.isfinite(float(tombstone_expires_at))
                        or not isinstance(tombstone_settled_at, (int, float))
                        or isinstance(tombstone_settled_at, bool)
                        or not math.isfinite(float(tombstone_settled_at))
                        or float(tombstone_settled_at) < 0
                        or float(tombstone_expires_at) < float(tombstone_settled_at)
                    ):
                        raise ValueError("dedupe tombstone timestamps are invalid")
                    tombstones[operation_id] = DedupeTombstone(
                        key_version=tombstone_key_version,
                        identity_mac=tombstone_identity_mac,
                        authority_profile=tombstone_profile,
                        workspace_recovery_key=tombstone_workspace_key,
                        owner_contract_digest=tombstone_owner_digest,
                        outcome=tombstone_outcome,
                        retry_horizon_expires_at=float(tombstone_expires_at),
                        settled_at=float(tombstone_settled_at),
                    )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                # Never synthesize an empty/successful authority from corrupted
                # committed protected state.
                self._generation = 0
                self._loaded_schema_version = None
                self._loaded_committed_bytes = None
                self._records = {}
                self._owner_snapshot_pins = {}
                self._retry_fingerprints = {}
                self._retry_authorities = {}
                self._dedupe_tombstones = {}
                self.recovery_only = True
                return
            self._generation = generation
            self._loaded_schema_version = schema_version
            self._loaded_committed_bytes = data
            self._records = records
            self._owner_snapshot_pins = pins
            self._retry_fingerprints = fingerprints
            self._retry_authorities = authorities
            self._dedupe_tombstones = tombstones
            self.recovery_only = False

    def _owner_snapshot_path(self, digest: str) -> Path:
        return self.owner_snapshot_dir / f"{digest}.blob"

    def _read_owner_snapshot_blob(self, digest: str) -> bytes:
        if not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
            raise ValueError("owner snapshot digest is invalid")
        path = self._owner_snapshot_path(digest)
        if path.is_symlink() or not path.is_file():
            raise ValueError("committed owner snapshot blob is missing")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("committed owner snapshot blob checksum mismatch")
        return data

    def _write_owner_snapshot_blob(self, digest: str, data: bytes) -> None:
        self.owner_snapshot_dir.mkdir(parents=True, exist_ok=True)
        if self.owner_snapshot_dir.is_symlink() or not self.owner_snapshot_dir.is_dir():
            raise RegistryRecoveryOnly("owner snapshot directory is not a regular directory")
        target = self._owner_snapshot_path(digest)
        if target.exists():
            try:
                existing = self._read_owner_snapshot_blob(digest)
            except (OSError, ValueError) as exc:
                raise RegistryRecoveryOnly("existing owner snapshot blob is corrupt") from exc
            if existing != data:
                raise RegistryRecoveryOnly("owner snapshot digest collision or corruption")
            return
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=self.owner_snapshot_dir,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, target)
            temporary = None
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.owner_snapshot_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _reconciliation_capsule_path(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("reconciliation capsule operation_id is invalid")
        return self.reconciliation_capsule_dir / f"{operation_id}.json"

    def _write_reconciliation_capsule_blob(
        self,
        *,
        operation_id: str,
        workspace_recovery_key: str,
        owner_contract_digest: str,
        owner_snapshot_digest: str | None,
        capsule: ReconciliationCapsule,
    ) -> None:
        if not isinstance(capsule, ReconciliationCapsule):
            raise ValueError("recovery_capsule must be a ReconciliationCapsule")
        # Re-parse through the public schema so direct dataclass construction
        # cannot bypass the typed/non-secret/4 KiB contract.
        capsule = ReconciliationCapsule.from_payload(capsule.as_payload())
        if owner_snapshot_digest is not None and not HEX_SHA256_RE.fullmatch(owner_snapshot_digest):
            raise ValueError("owner snapshot digest is invalid")
        envelope = {
            "schema_version": RECONCILIATION_CAPSULE_SCHEMA_VERSION,
            "operation_id": operation_id,
            "workspace_recovery_key": workspace_recovery_key,
            "owner_contract_digest": owner_contract_digest,
            "owner_snapshot_digest": owner_snapshot_digest,
            "capsule": capsule.as_payload(),
        }
        data = _canonical_json(envelope) + b"\n"
        if len(data) > MAX_RECONCILIATION_CAPSULE_FILE_BYTES:
            raise ValueError("reconciliation capsule sidecar exceeds its bounded file size")
        self.reconciliation_capsule_dir.mkdir(parents=True, exist_ok=True)
        if self.reconciliation_capsule_dir.is_symlink() or not self.reconciliation_capsule_dir.is_dir():
            raise RegistryRecoveryOnly("reconciliation capsule directory is not a regular directory")
        target = self._reconciliation_capsule_path(operation_id)
        if target.exists():
            raise RegistryRecoveryOnly("reconciliation capsule operation_id already exists")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{operation_id}.",
                suffix=".tmp",
                dir=self.reconciliation_capsule_dir,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, target)
            temporary = None
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.reconciliation_capsule_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _read_reconciliation_capsule_blob_locked(
        self,
        *,
        operation_id: str,
        workspace_recovery_key: str,
        owner_contract_digest: str,
        owner_snapshot_digest: str | None,
    ) -> ReconciliationCapsule:
        path = self._reconciliation_capsule_path(operation_id)
        try:
            if path.is_symlink() or not path.is_file():
                raise RecoveryCapsuleUnavailable("reconciliation capsule is unavailable")
            data = path.read_bytes()
            if len(data) > MAX_RECONCILIATION_CAPSULE_FILE_BYTES:
                raise RecoveryCapsuleUnavailable("reconciliation capsule sidecar exceeds its bounded size")
            raw = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "operation_id",
                "workspace_recovery_key",
                "owner_contract_digest",
                "owner_snapshot_digest",
                "capsule",
            }:
                raise RecoveryCapsuleUnavailable("reconciliation capsule sidecar has invalid fields")
            if raw["schema_version"] != RECONCILIATION_CAPSULE_SCHEMA_VERSION or isinstance(
                raw["schema_version"], bool
            ):
                raise RecoveryCapsuleUnavailable("reconciliation capsule sidecar schema is unsupported")
            if raw["operation_id"] != operation_id:
                raise RecoveryCapsuleUnavailable("reconciliation capsule operation binding does not match")
            if raw["workspace_recovery_key"] != workspace_recovery_key:
                raise RecoveryCapsuleUnavailable("reconciliation capsule workspace binding does not match")
            if raw["owner_contract_digest"] != owner_contract_digest:
                raise RecoveryCapsuleUnavailable("reconciliation capsule owner contract does not match")
            if raw["owner_snapshot_digest"] != owner_snapshot_digest:
                raise RecoveryCapsuleUnavailable("reconciliation capsule pinned owner does not match")
            return ReconciliationCapsule.from_payload(raw["capsule"])
        except RecoveryCapsuleUnavailable:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise RecoveryCapsuleUnavailable("reconciliation capsule is unavailable or corrupt") from exc

    def _remove_reconciliation_capsule_blob_locked(self, operation_id: str) -> bool:
        path = self._reconciliation_capsule_path(operation_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except (PermissionError, OSError):
            # A terminal receipt is already authoritative. A failed deletion is
            # a bounded GC orphan, never grounds to roll the receipt backward.
            return False
        return True

    def _garbage_collect_reconciliation_capsules_locked(self) -> int:
        if not self.reconciliation_capsule_dir.exists():
            return 0
        if self.reconciliation_capsule_dir.is_symlink() or not self.reconciliation_capsule_dir.is_dir():
            raise RegistryRecoveryOnly("reconciliation capsule directory is not a regular directory")
        referenced = {
            f"{operation_id}.json"
            for operation_id, receipt in self._records.items()
            if receipt.protected
        }
        removed = 0
        scanned = 0
        try:
            with os.scandir(self.reconciliation_capsule_dir) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_RECONCILIATION_CAPSULE_GC_ENTRIES_PER_PASS:
                        break
                    if entry.name in referenced:
                        continue
                    is_capsule = bool(re.fullmatch(r"[0-9a-f]{32}\.json", entry.name))
                    is_atomic_temp = entry.name.startswith(".") and entry.name.endswith(".tmp")
                    if not (is_capsule or is_atomic_temp):
                        continue
                    try:
                        Path(entry.path).unlink()
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    removed += 1
        except FileNotFoundError:
            return removed
        return removed

    def garbage_collect_reconciliation_capsules(self) -> int:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                return self._garbage_collect_reconciliation_capsules_locked()

    def _owner_snapshot_usage(self, pins: dict[str, str] | None = None) -> tuple[int, int, dict[str, int]]:
        selected = self._owner_snapshot_pins if pins is None else pins
        reference_counts: dict[str, int] = {}
        for digest in selected.values():
            reference_counts[digest] = reference_counts.get(digest, 0) + 1
        total_bytes = 0
        for digest in reference_counts:
            total_bytes += len(self._read_owner_snapshot_blob(digest))
        return len(reference_counts), total_bytes, reference_counts

    def _garbage_collect_owner_snapshot_blobs_locked(self) -> int:
        if not self.owner_snapshot_dir.exists():
            return 0
        if self.owner_snapshot_dir.is_symlink() or not self.owner_snapshot_dir.is_dir():
            raise RegistryRecoveryOnly("owner snapshot directory is not a regular directory")
        referenced = {f"{digest}.blob" for digest in self._owner_snapshot_pins.values()}
        removed = 0
        scanned = 0
        try:
            with os.scandir(self.owner_snapshot_dir) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_OWNER_SNAPSHOT_GC_ENTRIES_PER_PASS:
                        break
                    if entry.name in referenced:
                        continue
                    is_snapshot_blob = bool(re.fullmatch(r"[0-9a-f]{64}\.blob", entry.name))
                    is_atomic_temp = entry.name.startswith(".") and entry.name.endswith(".tmp")
                    if not (is_snapshot_blob or is_atomic_temp):
                        continue
                    try:
                        Path(entry.path).unlink()
                    except (FileNotFoundError, PermissionError, OSError):
                        # The committed pin table is already authoritative.
                        # A deletion failure leaves only reclaimable orphan data
                        # and must not roll back a completed reconciliation.
                        continue
                    removed += 1
        except FileNotFoundError:
            return removed
        return removed

    def garbage_collect_owner_snapshots(self) -> int:
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                return self._garbage_collect_owner_snapshot_blobs_locked()

    def _write_pre_migration_backup_locked(self, data: bytes) -> None:
        if not isinstance(data, bytes) or not data or len(data) > self.max_bytes:
            raise RegistryRecoveryOnly("pre-migration Registry snapshot is unavailable or invalid")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{REGISTRY_PRE_MIGRATION_BACKUP_NAME}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.pre_migration_backup_path)
            temporary = None
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _serialized(
        self,
        records: dict[str, OperationReceipt],
        owner_snapshot_pins: dict[str, str],
        retry_fingerprints: dict[str, str],
        retry_authorities: dict[str, RetryAuthorityReservation],
        dedupe_tombstones: dict[str, DedupeTombstone],
        generation: int,
    ) -> bytes:
        unsigned = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "generation": generation,
            "records": [asdict(records[key]) for key in sorted(records)],
            "owner_snapshot_pins": {
                operation_id: owner_snapshot_pins[operation_id]
                for operation_id in sorted(owner_snapshot_pins)
            },
            "retry_fingerprints": {
                operation_id: retry_fingerprints[operation_id]
                for operation_id in sorted(retry_fingerprints)
            },
            "retry_authorities": {
                operation_id: asdict(retry_authorities[operation_id])
                for operation_id in sorted(retry_authorities)
            },
            "dedupe_tombstones": {
                operation_id: asdict(dedupe_tombstones[operation_id])
                for operation_id in sorted(dedupe_tombstones)
            },
        }
        payload = dict(unsigned)
        payload["checksum"] = _state_checksum(unsigned)
        data = _canonical_json(payload) + b"\n"
        if len(data) > self.max_bytes:
            raise RegistryCapacityExceeded("operation registry byte capacity is exhausted")
        return data

    def _commit(
        self,
        records: dict[str, OperationReceipt],
        owner_snapshot_pins: dict[str, str] | None = None,
        retry_fingerprints: dict[str, str] | None = None,
        retry_authorities: dict[str, RetryAuthorityReservation] | None = None,
        dedupe_tombstones: dict[str, DedupeTombstone] | None = None,
    ) -> None:
        if self.recovery_only:
            raise RegistryRecoveryOnly("operation registry is recovery-only after committed-state corruption")
        self._assert_schema_write_target_locked(REGISTRY_SCHEMA_VERSION)
        if len(records) > self.max_records:
            raise RegistryCapacityExceeded("operation registry record capacity is exhausted")
        pins = dict(self._owner_snapshot_pins if owner_snapshot_pins is None else owner_snapshot_pins)
        fingerprints = dict(
            self._retry_fingerprints if retry_fingerprints is None else retry_fingerprints
        )
        authorities = dict(
            self._retry_authorities if retry_authorities is None else retry_authorities
        )
        tombstones = dict(
            self._dedupe_tombstones if dedupe_tombstones is None else dedupe_tombstones
        )
        if len(tombstones) > self.max_records:
            raise RegistryCapacityExceeded("dedupe tombstone capacity is exhausted")
        if any(operation_id not in records for operation_id in pins):
            raise OperationRegistryError("owner snapshot pin references an unknown operation")
        if any(operation_id not in records for operation_id in fingerprints):
            raise OperationRegistryError("retry fingerprint references an unknown operation")
        if any(not HEX_SHA256_RE.fullmatch(value) for value in fingerprints.values()):
            raise OperationRegistryError("retry fingerprint is invalid")
        if any(operation_id not in records for operation_id in authorities):
            raise OperationRegistryError("retry authority references an unknown operation")
        for operation_id, authority in authorities.items():
            if not isinstance(authority, RetryAuthorityReservation):
                raise OperationRegistryError("retry authority record is invalid")
            if not CORRELATION_KEY_VERSION_RE.fullmatch(authority.key_version):
                raise OperationRegistryError("retry authority key version is invalid")
            if not HEX_SHA256_RE.fullmatch(authority.identity_mac):
                raise OperationRegistryError("retry authority identity MAC is invalid")
            _bounded_string(
                authority.authority_profile,
                field="authority_profile",
                maximum=MAX_RETRY_AUTHORITY_PROFILE_LENGTH,
            )
            if (
                not math.isfinite(authority.retry_horizon_expires_at)
                or authority.retry_horizon_expires_at < records[operation_id].created_at
            ):
                raise OperationRegistryError("retry authority horizon is invalid")
        for operation_id, tombstone in tombstones.items():
            if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                raise OperationRegistryError("dedupe tombstone operation_id is invalid")
            if not isinstance(tombstone, DedupeTombstone):
                raise OperationRegistryError("dedupe tombstone record is invalid")
            if not CORRELATION_KEY_VERSION_RE.fullmatch(tombstone.key_version):
                raise OperationRegistryError("dedupe tombstone key version is invalid")
            if not HEX_SHA256_RE.fullmatch(tombstone.identity_mac):
                raise OperationRegistryError("dedupe tombstone identity MAC is invalid")
            _bounded_string(
                tombstone.authority_profile,
                field="authority_profile",
                maximum=MAX_RETRY_AUTHORITY_PROFILE_LENGTH,
            )
            _bounded_string(
                tombstone.workspace_recovery_key,
                field="workspace_recovery_key",
                maximum=MAX_WORKSPACE_KEY_LENGTH,
            )
            if not HEX_SHA256_RE.fullmatch(tombstone.owner_contract_digest):
                raise OperationRegistryError("dedupe tombstone owner contract digest is invalid")
            if tombstone.outcome != "effect_present":
                raise OperationRegistryError("dedupe tombstone outcome is invalid")
            if (
                not math.isfinite(tombstone.retry_horizon_expires_at)
                or not math.isfinite(tombstone.settled_at)
                or tombstone.settled_at < 0
                or tombstone.retry_horizon_expires_at < tombstone.settled_at
            ):
                raise OperationRegistryError("dedupe tombstone timestamps are invalid")
        for digest in set(pins.values()):
            try:
                self._read_owner_snapshot_blob(digest)
            except (OSError, ValueError) as exc:
                raise RegistryRecoveryOnly("owner snapshot pin references a missing or corrupt blob") from exc
        generation = self._generation + 1
        data = self._serialized(records, pins, fingerprints, authorities, tombstones, generation)
        if (
            self._loaded_schema_version is not None
            and self._loaded_schema_version < REGISTRY_SCHEMA_VERSION
        ):
            previous = self._loaded_committed_bytes
            if previous is None:
                raise RegistryRecoveryOnly(
                    "validated pre-migration Registry snapshot is unavailable"
                )
            self._write_pre_migration_backup_locked(previous)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{REGISTRY_STATE_NAME}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            temporary = None
            # POSIX directory fsync makes the rename durable. Windows does not
            # offer portable directory fsync through Python, while replace is
            # still atomic on the same volume.
            if os.name != "nt":
                try:
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass
        self._generation = generation
        self._loaded_schema_version = REGISTRY_SCHEMA_VERSION
        self._loaded_committed_bytes = data
        self._records = records
        self._owner_snapshot_pins = pins
        self._retry_fingerprints = fingerprints
        self._retry_authorities = authorities
        self._dedupe_tombstones = tombstones

    def register_prepared(
        self,
        *,
        boot_id: str,
        owner: str,
        public_workspace_id: str,
        workspace_recovery_key: str,
        effect_semantics: str,
        lifetime: str,
        owner_contract_digest: str,
        key_version: str,
        owner_snapshot: bytes | None = None,
        recovery_capsule: ReconciliationCapsule | None = None,
        retry_fingerprint_material: bytes | None = None,
        proven_retry_identity: ProvenRetryIdentity | None = None,
        operation_id: str | None = None,
    ) -> OperationReceipt:
        if operation_id is not None and (
            not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
        ):
            raise ValueError("operation_id must be 32 lowercase hex characters when supplied")
        boot_id = _bounded_string(boot_id, field="boot_id", maximum=160)
        owner = _bounded_string(owner, field="owner", maximum=MAX_OWNER_LENGTH)
        public_workspace_id = _bounded_string(
            public_workspace_id,
            field="public_workspace_id",
            maximum=MAX_PUBLIC_WORKSPACE_ID_LENGTH,
        )
        workspace_recovery_key = _bounded_string(
            workspace_recovery_key,
            field="workspace_recovery_key",
            maximum=MAX_WORKSPACE_KEY_LENGTH,
        )
        if effect_semantics not in EFFECT_SEMANTICS:
            raise ValueError("effect_semantics is unsupported")
        if lifetime not in LIFETIMES:
            raise ValueError("lifetime is unsupported")
        if not isinstance(owner_contract_digest, str) or not HEX_SHA256_RE.fullmatch(owner_contract_digest):
            raise ValueError("owner_contract_digest must be lowercase SHA-256")
        key_version = _bounded_string(
            key_version,
            field="key_version",
            maximum=MAX_KEY_VERSION_LENGTH,
        )
        if retry_fingerprint_material is not None:
            if not isinstance(retry_fingerprint_material, bytes):
                raise ValueError("retry_fingerprint_material must be bytes when supplied")
            if not retry_fingerprint_material:
                raise ValueError("retry_fingerprint_material must not be empty")
            if len(retry_fingerprint_material) > MAX_RETRY_FINGERPRINT_MATERIAL_BYTES:
                raise ValueError("retry_fingerprint_material exceeds the bounded Host policy representation")
        if proven_retry_identity is not None and not isinstance(
            proven_retry_identity,
            ProvenRetryIdentity,
        ):
            raise ValueError("proven_retry_identity must be a Host-internal ProvenRetryIdentity")
        if recovery_capsule is not None:
            if not isinstance(recovery_capsule, ReconciliationCapsule):
                raise ValueError("recovery_capsule must be a ReconciliationCapsule when supplied")
            recovery_capsule = ReconciliationCapsule.from_payload(recovery_capsule.as_payload())
        owner_snapshot_digest: str | None = None
        owner_snapshot_bytes: bytes | None = None
        if owner_snapshot is not None:
            if not isinstance(owner_snapshot, bytes):
                raise ValueError("owner_snapshot must be bytes when supplied")
            if not owner_snapshot:
                raise ValueError("owner_snapshot must not be empty")
            if len(owner_snapshot) > self.max_pinned_bytes:
                raise RegistryPinCapacityExceeded("owner snapshot exceeds configured pin byte capacity")
            owner_snapshot_bytes = owner_snapshot
            owner_snapshot_digest = hashlib.sha256(owner_snapshot).hexdigest()
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                authorities = dict(self._retry_authorities)
                authority_key_version: str | None = None
                authority_identity_mac: str | None = None
                if proven_retry_identity is not None:
                    active_authority_version, authority_keys = self._load_correlation_keys_locked(
                        create_if_missing=True
                    )
                    for existing_operation_id, reservation in authorities.items():
                        existing_receipt = self._records[existing_operation_id]
                        if (
                            existing_receipt.workspace_recovery_key != workspace_recovery_key
                            or existing_receipt.owner_contract_digest != owner_contract_digest
                        ):
                            continue
                        reservation_secret = authority_keys.get(reservation.key_version)
                        if reservation_secret is None:
                            raise CorrelationKeyUnavailable(
                                "protected retry authority key version is unavailable"
                            )
                        candidate_mac = _retry_identity_mac(
                            reservation_secret,
                            proven_retry_identity,
                        )
                        if hmac.compare_digest(candidate_mac, reservation.identity_mac):
                            raise RetryIdentityReserved(existing_operation_id)
                    admission_now = time.time()
                    for tombstone_operation_id, tombstone in self._dedupe_tombstones.items():
                        if tombstone.retry_horizon_expires_at <= admission_now:
                            continue
                        if (
                            tombstone.workspace_recovery_key != workspace_recovery_key
                            or tombstone.owner_contract_digest != owner_contract_digest
                        ):
                            continue
                        tombstone_secret = authority_keys.get(tombstone.key_version)
                        if tombstone_secret is None:
                            raise CorrelationKeyUnavailable(
                                "protected dedupe tombstone key version is unavailable"
                            )
                        candidate_mac = _retry_identity_mac(
                            tombstone_secret,
                            proven_retry_identity,
                        )
                        if hmac.compare_digest(candidate_mac, tombstone.identity_mac):
                            raise RetryIdentityReserved(tombstone_operation_id)
                    authority_secret = authority_keys.get(active_authority_version)
                    if authority_secret is None:
                        raise CorrelationKeyUnavailable(
                            "active retry authority key version is unavailable"
                        )
                    authority_key_version = active_authority_version
                    authority_identity_mac = _retry_identity_mac(
                        authority_secret,
                        proven_retry_identity,
                    )
                if len(self._records) >= self.max_records:
                    raise RegistryCapacityExceeded("operation registry record capacity is exhausted")
                fingerprint: str | None = None
                fingerprints = dict(self._retry_fingerprints)
                if retry_fingerprint_material is not None:
                    if not CORRELATION_KEY_VERSION_RE.fullmatch(key_version):
                        raise CorrelationKeyUnavailable(
                            "fingerprinted registration requires a persistent correlation key version"
                        )
                    active_version, keys = self._load_correlation_keys_locked(create_if_missing=False)
                    if key_version != active_version:
                        raise CorrelationKeyUnavailable(
                            "new fingerprinted registration must use the current active correlation key"
                        )
                    secret = keys.get(key_version)
                    if secret is None:
                        raise CorrelationKeyUnavailable("correlation key version is unavailable")
                    fingerprint = hmac.new(
                        secret,
                        retry_fingerprint_material,
                        hashlib.sha256,
                    ).hexdigest()
                pins = dict(self._owner_snapshot_pins)
                if owner_snapshot_digest is not None and owner_snapshot_digest not in set(pins.values()):
                    distinct_versions, total_bytes, _ = self._owner_snapshot_usage(pins)
                    if distinct_versions + 1 > self.max_pinned_versions:
                        raise RegistryPinCapacityExceeded("owner snapshot version capacity is exhausted")
                    assert owner_snapshot_bytes is not None
                    if total_bytes + len(owner_snapshot_bytes) > self.max_pinned_bytes:
                        raise RegistryPinCapacityExceeded("owner snapshot byte capacity is exhausted")
                if operation_id is not None:
                    if (
                        operation_id in self._records
                        or self._reconciliation_capsule_path(operation_id).exists()
                    ):
                        raise OperationRegistryError("operation_id is already reserved")
                else:
                    for _ in range(8):
                        candidate = uuid.uuid4().hex
                        if (
                            candidate not in self._records
                            and not self._reconciliation_capsule_path(candidate).exists()
                        ):
                            operation_id = candidate
                            break
                    if operation_id is None:
                        raise OperationRegistryError("could not allocate a unique operation_id")
                now = time.time()
                receipt = OperationReceipt(
                    operation_id=operation_id,
                    boot_id=boot_id,
                    owner=owner,
                    public_workspace_id=public_workspace_id,
                    workspace_recovery_key=workspace_recovery_key,
                    effect_semantics=effect_semantics,
                    lifetime=lifetime,
                    owner_contract_digest=owner_contract_digest,
                    key_version=key_version,
                    state="prepared",
                    terminal_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                records = dict(self._records)
                records[operation_id] = receipt
                if owner_snapshot_digest is not None:
                    assert owner_snapshot_bytes is not None
                    self._write_owner_snapshot_blob(owner_snapshot_digest, owner_snapshot_bytes)
                    pins[operation_id] = owner_snapshot_digest
                if recovery_capsule is not None:
                    self._write_reconciliation_capsule_blob(
                        operation_id=operation_id,
                        workspace_recovery_key=workspace_recovery_key,
                        owner_contract_digest=owner_contract_digest,
                        owner_snapshot_digest=owner_snapshot_digest,
                        capsule=recovery_capsule,
                    )
                if fingerprint is not None:
                    fingerprints[operation_id] = fingerprint
                if proven_retry_identity is not None:
                    assert authority_key_version is not None
                    assert authority_identity_mac is not None
                    authorities[operation_id] = RetryAuthorityReservation(
                        key_version=authority_key_version,
                        identity_mac=authority_identity_mac,
                        authority_profile=proven_retry_identity.authority_profile,
                        retry_horizon_expires_at=(
                            now + float(proven_retry_identity.retry_horizon_seconds)
                        ),
                    )
                self._commit(records, pins, fingerprints, authorities)
                return receipt

    def settle_effect_outcome(
        self,
        operation_id: str,
        *,
        workspace_recovery_key: str,
        outcome: str,
    ) -> OperationReceipt:
        if outcome not in {
            "effect_present",
            "effect_absent",
            "indeterminate_keep_blocked",
        }:
            raise ValueError("unsupported effect settlement outcome")
        workspace_recovery_key = _bounded_string(
            workspace_recovery_key,
            field="workspace_recovery_key",
            maximum=MAX_WORKSPACE_KEY_LENGTH,
        )
        settleable_states = {
            "effect_attempted",
            "effect_observed",
            "delivery_ambiguous",
            "termination_uncertain",
            "runtime_lost",
        }
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                current = self._records.get(operation_id)
                if current is None or current.workspace_recovery_key != workspace_recovery_key:
                    raise OperationNotFound("operation not found")
                if current.state not in settleable_states:
                    raise InvalidRegistryTransition(
                        f"operation state {current.state} cannot accept an effect settlement outcome"
                    )
                now = max(time.time(), current.updated_at)
                records = dict(self._records)
                pins = dict(self._owner_snapshot_pins)
                authorities = dict(self._retry_authorities)
                tombstones = dict(self._dedupe_tombstones)

                if outcome == "indeterminate_keep_blocked":
                    updated = replace(
                        current,
                        terminal_reason="indeterminate_keep_blocked",
                        updated_at=now,
                    )
                    records[operation_id] = updated
                    self._commit(
                        records,
                        owner_snapshot_pins=pins,
                        retry_authorities=authorities,
                        dedupe_tombstones=tombstones,
                    )
                    return updated

                pins.pop(operation_id, None)
                authority = authorities.pop(operation_id, None)
                tombstones.pop(operation_id, None)
                if (
                    outcome == "effect_present"
                    and authority is not None
                    and authority.retry_horizon_expires_at > now
                ):
                    tombstones[operation_id] = DedupeTombstone(
                        key_version=authority.key_version,
                        identity_mac=authority.identity_mac,
                        authority_profile=authority.authority_profile,
                        workspace_recovery_key=current.workspace_recovery_key,
                        owner_contract_digest=current.owner_contract_digest,
                        outcome="effect_present",
                        retry_horizon_expires_at=authority.retry_horizon_expires_at,
                        settled_at=now,
                    )
                updated = replace(
                    current,
                    state="reconciled",
                    terminal_reason=outcome,
                    updated_at=now,
                )
                records[operation_id] = updated
                self._commit(
                    records,
                    owner_snapshot_pins=pins,
                    retry_authorities=authorities,
                    dedupe_tombstones=tombstones,
                )
                self._remove_reconciliation_capsule_blob_locked(operation_id)
                self._garbage_collect_reconciliation_capsules_locked()
                self._garbage_collect_owner_snapshot_blobs_locked()
                return updated

    def compact_reconciled_receipts(self, *, updated_before: float) -> int:
        if (
            isinstance(updated_before, bool)
            or not isinstance(updated_before, (int, float))
            or not math.isfinite(float(updated_before))
        ):
            raise ValueError("updated_before must be a finite timestamp")
        cutoff = float(updated_before)
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                removable = {
                    operation_id
                    for operation_id, receipt in self._records.items()
                    if receipt.state in {"reconciled", "effect_not_started"}
                    and receipt.updated_at <= cutoff
                    and operation_id not in self._owner_snapshot_pins
                    and operation_id not in self._retry_authorities
                }
                if not removable:
                    return 0
                records = {
                    operation_id: receipt
                    for operation_id, receipt in self._records.items()
                    if operation_id not in removable
                }
                fingerprints = {
                    operation_id: fingerprint
                    for operation_id, fingerprint in self._retry_fingerprints.items()
                    if operation_id not in removable
                }
                # Authoritative dedupe tombstones are deliberately independent
                # from full receipt-body retention.  They remain until their
                # separately proven retry horizon expires; compaction must not
                # weaken that authority or turn a replay into a new intent.
                self._commit(
                    records,
                    retry_fingerprints=fingerprints,
                )
                self._garbage_collect_owner_snapshot_blobs_locked()
                return len(removable)

    def retry_fingerprint_candidates(
        self,
        *,
        workspace_recovery_key: str,
        owner_contract_digest: str,
        key_version: str,
        fingerprint_material: bytes,
    ) -> list[OperationReceipt]:
        workspace_recovery_key = _bounded_string(
            workspace_recovery_key,
            field="workspace_recovery_key",
            maximum=MAX_WORKSPACE_KEY_LENGTH,
        )
        if not isinstance(owner_contract_digest, str) or not HEX_SHA256_RE.fullmatch(owner_contract_digest):
            raise ValueError("owner_contract_digest must be lowercase SHA-256")
        if not isinstance(key_version, str) or not CORRELATION_KEY_VERSION_RE.fullmatch(key_version):
            raise CorrelationKeyUnavailable("correlation key version is invalid")
        if (
            not isinstance(fingerprint_material, bytes)
            or not fingerprint_material
            or len(fingerprint_material) > MAX_RETRY_FINGERPRINT_MATERIAL_BYTES
        ):
            raise ValueError("fingerprint_material must be bounded non-empty bytes")
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._load_committed()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry committed state is corrupt")
                _, keys = self._load_correlation_keys_locked(create_if_missing=False)
                secret = keys.get(key_version)
                if secret is None:
                    raise CorrelationKeyUnavailable("correlation key version is unavailable")
                fingerprint = hmac.new(secret, fingerprint_material, hashlib.sha256).hexdigest()
                return sorted(
                    (
                        receipt
                        for operation_id, receipt in self._records.items()
                        if self._retry_fingerprints.get(operation_id) == fingerprint
                        and receipt.key_version == key_version
                        and receipt.workspace_recovery_key == workspace_recovery_key
                        and receipt.owner_contract_digest == owner_contract_digest
                    ),
                    key=lambda item: (item.created_at, item.operation_id),
                )

    def _read_reconciliation_capsule_for_operation(
        self,
        operation_id: str,
        *,
        workspace_recovery_key: str,
        owner_contract_digest: str,
        automatic: bool,
    ) -> ReconciliationCapsule:
        workspace_recovery_key = _bounded_string(
            workspace_recovery_key,
            field="workspace_recovery_key",
            maximum=MAX_WORKSPACE_KEY_LENGTH,
        )
        if not isinstance(owner_contract_digest, str) or not HEX_SHA256_RE.fullmatch(owner_contract_digest):
            raise ValueError("owner_contract_digest must be lowercase SHA-256")
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                receipt = self._records.get(operation_id)
                if receipt is None or receipt.workspace_recovery_key != workspace_recovery_key:
                    raise OperationNotFound("operation not found")
                if not receipt.protected:
                    raise RecoveryCapsuleUnavailable("terminal operation no longer retains a recovery capsule")
                if receipt.owner_contract_digest != owner_contract_digest:
                    raise RecoveryCapsuleUnavailable("operation owner contract does not match")
                owner_snapshot_digest = self._owner_snapshot_pins.get(operation_id)
                capsule = self._read_reconciliation_capsule_blob_locked(
                    operation_id=operation_id,
                    workspace_recovery_key=workspace_recovery_key,
                    owner_contract_digest=owner_contract_digest,
                    owner_snapshot_digest=owner_snapshot_digest,
                )
                if automatic:
                    if owner_snapshot_digest is None:
                        raise RecoveryCapsuleUnavailable(
                            "automatic reconciliation requires the exact pinned owner snapshot"
                        )
                    if not capsule.has_pre_effect_correlation:
                        raise RecoveryCapsuleUnavailable(
                            "automatic reconciliation requires durable pre-effect correlation"
                        )
                    if receipt.state not in {
                        "effect_attempted",
                        "effect_observed",
                        "delivery_ambiguous",
                        "termination_uncertain",
                        "runtime_lost",
                    }:
                        raise RecoveryCapsuleUnavailable(
                            "operation has not crossed an automatic reconciliation-eligible effect boundary"
                        )
                return capsule

    def read_reconciliation_capsule(
        self,
        operation_id: str,
        *,
        workspace_recovery_key: str,
        owner_contract_digest: str,
    ) -> ReconciliationCapsule:
        return self._read_reconciliation_capsule_for_operation(
            operation_id,
            workspace_recovery_key=workspace_recovery_key,
            owner_contract_digest=owner_contract_digest,
            automatic=False,
        )

    def read_automatic_reconciliation_capsule(
        self,
        operation_id: str,
        *,
        workspace_recovery_key: str,
        owner_contract_digest: str,
    ) -> ReconciliationCapsule:
        return self._read_reconciliation_capsule_for_operation(
            operation_id,
            workspace_recovery_key=workspace_recovery_key,
            owner_contract_digest=owner_contract_digest,
            automatic=True,
        )

    def read_owner_snapshot(self, operation_id: str, *, workspace_recovery_key: str) -> bytes:
        self._refresh_for_read()
        with self._lock:
            receipt = self._records.get(operation_id)
            if receipt is None or receipt.workspace_recovery_key != workspace_recovery_key:
                raise OperationNotFound("operation not found")
            digest = self._owner_snapshot_pins.get(operation_id)
            if digest is None:
                raise OperationRegistryError("operation has no pinned owner snapshot")
            try:
                return self._read_owner_snapshot_blob(digest)
            except (OSError, ValueError) as exc:
                self.recovery_only = True
                raise RegistryRecoveryOnly("owner snapshot blob is missing or corrupt") from exc

    def owner_snapshot_stats(self) -> dict[str, Any]:
        self._refresh_for_read()
        with self._lock:
            distinct_versions, total_bytes, reference_counts = self._owner_snapshot_usage()
            return {
                "distinct_versions": distinct_versions,
                "references": len(self._owner_snapshot_pins),
                "total_bytes": total_bytes,
                "max_versions": self.max_pinned_versions,
                "max_bytes": self.max_pinned_bytes,
                "reference_counts": dict(sorted(reference_counts.items())),
            }

    def get(self, operation_id: str, *, workspace_recovery_key: str) -> OperationReceipt:
        self._refresh_for_read()
        with self._lock:
            receipt = self._records.get(operation_id)
            if receipt is None or receipt.workspace_recovery_key != workspace_recovery_key:
                # Deliberately collapse wrong-workspace and absent into the same
                # public result to avoid cross-workspace existence disclosure.
                raise OperationNotFound("operation not found")
            return receipt

    def list_for_workspace(self, workspace_recovery_key: str) -> list[OperationReceipt]:
        self._refresh_for_read()
        with self._lock:
            return sorted(
                (
                    receipt
                    for receipt in self._records.values()
                    if receipt.workspace_recovery_key == workspace_recovery_key
                ),
                key=lambda item: (item.created_at, item.operation_id),
            )

    def list_all_for_operator(self) -> list[OperationReceipt]:
        self._refresh_for_read()
        with self._lock:
            return sorted(self._records.values(), key=lambda item: (item.created_at, item.operation_id))

    def transition(
        self,
        operation_id: str,
        *,
        workspace_recovery_key: str,
        expected_state: str,
        new_state: str,
        terminal_reason: str | None = None,
    ) -> OperationReceipt:
        if expected_state not in STATES or new_state not in STATES:
            raise ValueError("expected_state/new_state is unsupported")
        if terminal_reason is not None:
            terminal_reason = _bounded_string(
                terminal_reason,
                field="terminal_reason",
                maximum=160,
            )
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                current = self._records.get(operation_id)
                if current is None or current.workspace_recovery_key != workspace_recovery_key:
                    raise OperationNotFound("operation not found")
                if current.state != expected_state:
                    raise InvalidRegistryTransition(
                        f"operation state is {current.state}, expected {expected_state}"
                    )
                updated = replace(
                    current,
                    state=new_state,
                    terminal_reason=terminal_reason,
                    updated_at=max(time.time(), current.updated_at),
                )
                records = dict(self._records)
                records[operation_id] = updated
                self._commit(records)
                return updated

    def recover_for_boot(
        self,
        current_boot_id: str,
        *,
        quiescent_boot_ids: Iterable[str] = (),
    ) -> int:
        current_boot_id = _bounded_string(
            current_boot_id,
            field="current_boot_id",
            maximum=160,
        )
        proven_quiescent = frozenset(
            _bounded_string(boot_id, field="quiescent_boot_id", maximum=160)
            for boot_id in quiescent_boot_ids
        )
        with self._lock:
            with _RegistryWriterGate(self.writer_lock_path):
                self._reload_for_transaction()
                if self.recovery_only:
                    raise RegistryRecoveryOnly("operation registry is recovery-only")
                now = time.time()
                changed = 0
                records = dict(self._records)
                pins = dict(self._owner_snapshot_pins)
                authorities = dict(self._retry_authorities)
                released_protected_state = False
                released_capsule_ids: list[str] = []
                for operation_id, receipt in tuple(records.items()):
                    if receipt.boot_id == current_boot_id or receipt.state in SAFE_TERMINAL_STATES:
                        continue
                    if receipt.state == "runtime_lost":
                        continue
                    if receipt.state == "prepared" and receipt.boot_id in proven_quiescent:
                        records[operation_id] = replace(
                            receipt,
                            state="effect_not_started",
                            terminal_reason="effect_not_started",
                            updated_at=max(now, receipt.updated_at),
                        )
                        pins.pop(operation_id, None)
                        authorities.pop(operation_id, None)
                        released_capsule_ids.append(operation_id)
                        released_protected_state = True
                    else:
                        records[operation_id] = replace(
                            receipt,
                            state="runtime_lost",
                            terminal_reason="runtime_lost",
                            updated_at=max(now, receipt.updated_at),
                        )
                    changed += 1
                if changed:
                    self._commit(
                        records,
                        owner_snapshot_pins=pins,
                        retry_authorities=authorities,
                    )
                    if released_protected_state:
                        for operation_id in released_capsule_ids:
                            self._remove_reconciliation_capsule_blob_locked(operation_id)
                        self._garbage_collect_reconciliation_capsules_locked()
                        self._garbage_collect_owner_snapshot_blobs_locked()
                return changed
