from __future__ import annotations

import atexit
import codecs
import hashlib
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .security import ToolError, Workspace, _encode_utf8_text, _fsync_directory
from .user_paths import user_config_root


MAX_TRANSACTION_TEXT_BYTES = 512 * 1024 * 1024
# Keep any single chunk safely below the 1 MiB MCP JSON envelope even when
# every one-byte control character expands to a six-byte JSON escape.
MAX_TRANSACTION_CHUNK_BYTES = 128 * 1024
MAX_ACTIVE_TEXT_TRANSACTIONS = 16
TRANSACTION_TTL_SECONDS = 24 * 60 * 60
MAX_STALE_STAGING_SCAN = 4096
MAX_STALE_CLEANUP_SECONDS = 2.0
COPY_CHUNK_BYTES = 1024 * 1024


@dataclass
class _TextTransaction:
    transaction_id: str
    workspace_root: Path
    target_raw: str
    target_relative: str
    mode: str
    baseline_sha256: str | None
    staging_path: Path
    received_bytes: int
    created_at: float
    updated_at: float
    phase: str = "staging"
    state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)


class TextWriteManager:
    """Host-owned transactional staging for large UTF-8 file writes.

    Chunks are staged outside workspaces. Commit revalidates the target policy and
    optimistic-lock baseline, copies through a same-directory temporary file,
    fsyncs it, and only then replaces the target atomically.
    """

    def __init__(self, staging_root: Path | None = None) -> None:
        self.staging_root = (staging_root or (user_config_root() / "write-staging")).resolve(strict=False)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            try:
                os.chmod(self.staging_root, 0o700)
            except OSError as exc:
                raise ToolError("WRITE_FAILED", f"Could not secure text staging directory: {exc}") from exc
        self._transactions: dict[str, _TextTransaction] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._cleanup_stale_files()
        atexit.register(self.close)

    def begin(
        self,
        workspace: Workspace,
        raw: str,
        *,
        mode: str,
        expected_target_sha256: str | None,
    ) -> dict[str, Any]:
        if mode not in {"create", "replace"}:
            raise ToolError("INVALID_ARGUMENT", "mode must be create or replace")
        target = workspace.resolve(raw, for_write=True)
        baseline: str | None = None
        if mode == "create":
            if expected_target_sha256 is not None:
                raise ToolError("INVALID_ARGUMENT", "create mode does not accept expected_target_sha256")
            if target.exists():
                raise ToolError("TARGET_EXISTS", "create mode requires a path that does not exist.", path=raw)
        else:
            if not _is_sha256(expected_target_sha256):
                raise ToolError("INVALID_ARGUMENT", "replace mode requires a 64-character expected_target_sha256")
            if not target.is_file():
                raise ToolError("NOT_FOUND", "replace mode requires an existing regular file.", path=raw)
            baseline, _ = _hash_utf8_file(target, MAX_TRANSACTION_TEXT_BYTES)
            if baseline != expected_target_sha256.lower():
                raise ToolError(
                    "STALE_FILE",
                    "The target changed or the supplied SHA-256 does not match.",
                    actual_sha256=baseline,
                )

        with self._lock:
            self._ensure_open()
            if len(self._transactions) >= MAX_ACTIVE_TEXT_TRANSACTIONS:
                raise ToolError(
                    "TOO_MANY_TRANSACTIONS",
                    f"At most {MAX_ACTIVE_TEXT_TRANSACTIONS} text write transactions may be active.",
                    limit=MAX_ACTIVE_TEXT_TRANSACTIONS,
                )
            transaction_id = secrets.token_hex(16)
            staging_path = self.staging_root / f"{transaction_id}.part"
            try:
                descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb"):
                    pass
            except OSError as exc:
                raise ToolError("WRITE_FAILED", f"Could not create text staging file: {exc}") from exc
            now = time.time()
            record = _TextTransaction(
                transaction_id=transaction_id,
                workspace_root=workspace.root,
                target_raw=raw,
                target_relative=target.relative_to(workspace.root).as_posix(),
                mode=mode,
                baseline_sha256=baseline,
                staging_path=staging_path,
                received_bytes=0,
                created_at=now,
                updated_at=now,
            )
            self._transactions[transaction_id] = record
            return {
                "action": "begin",
                "transaction_id": transaction_id,
                "path": record.target_relative,
                "mode": mode,
                "received_bytes": 0,
                "max_chunk_bytes": MAX_TRANSACTION_CHUNK_BYTES,
                "max_file_bytes": MAX_TRANSACTION_TEXT_BYTES,
            }

    def append(
        self,
        workspace: Workspace,
        transaction_id: str,
        *,
        offset: int,
        chunk: str,
    ) -> dict[str, Any]:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError("INVALID_ARGUMENT", "offset must be a non-negative integer byte offset")
        if not isinstance(chunk, str) or not chunk:
            raise ToolError("INVALID_ARGUMENT", "chunk must be a non-empty UTF-8 string")
        encoded = _encode_utf8_text(chunk)
        if len(encoded) > MAX_TRANSACTION_CHUNK_BYTES:
            raise ToolError(
                "CHUNK_TOO_LARGE",
                f"A text write chunk may not exceed {MAX_TRANSACTION_CHUNK_BYTES} UTF-8 bytes.",
                chunk_bytes=len(encoded),
                limit=MAX_TRANSACTION_CHUNK_BYTES,
            )
        with self._locked_record(workspace, transaction_id) as record:
            if offset != record.received_bytes:
                raise ToolError(
                    "OFFSET_MISMATCH",
                    "Chunk offset does not match the next expected byte offset.",
                    expected_offset=record.received_bytes,
                    supplied_offset=offset,
                )
            new_size = record.received_bytes + len(encoded)
            if new_size > MAX_TRANSACTION_TEXT_BYTES:
                raise ToolError(
                    "FILE_TOO_LARGE",
                    f"Transactional text files may not exceed {MAX_TRANSACTION_TEXT_BYTES} bytes.",
                    size=new_size,
                    limit=MAX_TRANSACTION_TEXT_BYTES,
                )
            try:
                with record.staging_path.open("ab") as handle:
                    handle.write(encoded)
            except OSError as exc:
                raise ToolError("WRITE_FAILED", f"Could not append text staging data: {exc}") from exc
            with record.state_lock:
                record.received_bytes = new_size
                record.updated_at = time.time()
            return {
                "action": "append",
                "transaction_id": transaction_id,
                "path": record.target_relative,
                "mode": record.mode,
                "received_bytes": new_size,
                "appended_bytes": len(encoded),
            }

    def status(self, workspace: Workspace, transaction_id: str) -> dict[str, Any]:
        record = self._record(workspace, transaction_id)
        # Status is a control-plane snapshot and must not wait behind a long
        # commit operation. The small state lock only protects mutable counters
        # and phase metadata; commit I/O remains under operation_lock.
        with record.state_lock:
            return {
                "action": "status",
                "transaction_id": transaction_id,
                "path": record.target_relative,
                "mode": record.mode,
                "phase": record.phase,
                "received_bytes": record.received_bytes,
                "max_chunk_bytes": MAX_TRANSACTION_CHUNK_BYTES,
                "max_file_bytes": MAX_TRANSACTION_TEXT_BYTES,
            }

    def commit(
        self,
        workspace: Workspace,
        transaction_id: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            raise ToolError("INVALID_ARGUMENT", "expected_size must be a non-negative integer")
        if not _is_sha256(expected_sha256):
            raise ToolError("INVALID_ARGUMENT", "expected_sha256 must be a 64-character SHA-256 hex digest")
        with self._locked_record(workspace, transaction_id) as record:
            with record.state_lock:
                record.phase = "committing"
                received_bytes = record.received_bytes
            committed = False
            try:
                if expected_size != received_bytes:
                    raise ToolError(
                        "SIZE_MISMATCH",
                        "expected_size does not match staged UTF-8 bytes.",
                        expected_size=received_bytes,
                        supplied_size=expected_size,
                    )
                staged_hash, staged_size = _hash_utf8_file(record.staging_path, MAX_TRANSACTION_TEXT_BYTES)
                if staged_size != received_bytes:
                    raise ToolError("STAGING_CHANGED", "The staging file changed unexpectedly; abort and restart the transaction.")
                if staged_hash != expected_sha256.lower():
                    raise ToolError(
                        "HASH_MISMATCH",
                        "expected_sha256 does not match staged content.",
                        actual_sha256=staged_hash,
                    )

                target = workspace.resolve(record.target_raw, for_write=True)
                if record.mode == "create":
                    if target.exists():
                        raise ToolError("TARGET_EXISTS", "The target appeared after begin; commit refused.", path=record.target_relative)
                else:
                    if not target.is_file():
                        raise ToolError("STALE_FILE", "The replacement target disappeared or changed type.", path=record.target_relative)
                    actual, _ = _hash_utf8_file(target, MAX_TRANSACTION_TEXT_BYTES)
                    if actual != record.baseline_sha256:
                        raise ToolError(
                            "STALE_FILE",
                            "The replacement target changed after begin; commit refused.",
                            actual_sha256=actual,
                        )

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise ToolError("WRITE_FAILED", f"Could not create target directory: {exc}") from exc
                target = workspace.resolve(record.target_raw, for_write=True)
                if record.mode == "create" and target.exists():
                    raise ToolError("TARGET_EXISTS", "The target appeared before atomic replace; commit refused.", path=record.target_relative)
                if record.mode == "replace" and not target.is_file():
                    raise ToolError("STALE_FILE", "The replacement target changed type before commit.", path=record.target_relative)

                _commit_staged_text(
                    record.staging_path,
                    target,
                    mode=record.mode,
                    baseline_sha256=record.baseline_sha256,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256.lower(),
                )
                self._drop(record)
                committed = True
                return {
                    "action": "commit",
                    "transaction_id": transaction_id,
                    "path": record.target_relative,
                    "mode": record.mode,
                    "size": staged_size,
                    "sha256": staged_hash,
                    "created": record.mode == "create",
                }
            finally:
                if not committed:
                    with record.state_lock:
                        record.phase = "staging"

    def abort(self, workspace: Workspace, transaction_id: str) -> dict[str, Any]:
        record = self._record(workspace, transaction_id)
        if not record.operation_lock.acquire(blocking=False):
            raise ToolError(
                "TRANSACTION_BUSY",
                "The transaction is currently committing; abort cannot interrupt an atomic publish in progress.",
                transaction_id=transaction_id,
            )
        try:
            with self._lock:
                self._ensure_open()
                if self._transactions.get(record.transaction_id) is not record:
                    raise ToolError("UNKNOWN_TRANSACTION", "Text write transaction was not found or has ended.")
                if record.workspace_root != workspace.root:
                    raise ToolError("TRANSACTION_WORKSPACE_MISMATCH", "Transaction belongs to a different workspace.")
            self._drop(record)
            return {
                "action": "abort",
                "transaction_id": transaction_id,
                "path": record.target_relative,
                "mode": record.mode,
                "aborted": True,
            }
        finally:
            record.operation_lock.release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = list(self._transactions.values())
        for record in records:
            with record.operation_lock:
                self._drop(record)

    def _record(self, workspace: Workspace, transaction_id: str) -> _TextTransaction:
        with self._lock:
            self._ensure_open()
            if not isinstance(transaction_id, str) or not transaction_id:
                raise ToolError("INVALID_ARGUMENT", "transaction_id is required")
            record = self._transactions.get(transaction_id)
            if record is None:
                raise ToolError("UNKNOWN_TRANSACTION", "Text write transaction was not found or has ended.")
            if record.workspace_root != workspace.root:
                raise ToolError("TRANSACTION_WORKSPACE_MISMATCH", "Transaction belongs to a different workspace.")
            return record

    @contextmanager
    def _locked_record(self, workspace: Workspace, transaction_id: str):
        record = self._record(workspace, transaction_id)
        with record.operation_lock:
            with self._lock:
                self._ensure_open()
                if self._transactions.get(record.transaction_id) is not record:
                    raise ToolError("UNKNOWN_TRANSACTION", "Text write transaction was not found or has ended.")
                if record.workspace_root != workspace.root:
                    raise ToolError("TRANSACTION_WORKSPACE_MISMATCH", "Transaction belongs to a different workspace.")
            yield record

    def _drop(self, record: _TextTransaction) -> None:
        with self._lock:
            if self._transactions.get(record.transaction_id) is record:
                self._transactions.pop(record.transaction_id, None)
        try:
            record.staging_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise ToolError("WRITE_MANAGER_CLOSED", "Text write manager is closed.")

    def _cleanup_stale_files(self) -> None:
        cutoff = time.time() - TRANSACTION_TTL_SECONDS
        deadline = time.monotonic() + MAX_STALE_CLEANUP_SECONDS
        try:
            with os.scandir(self.staging_root) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_STALE_STAGING_SCAN or time.monotonic() > deadline:
                        break
                    name = entry.name
                    if not name.endswith(".part"):
                        continue
                    stem = name[:-5]
                    if len(stem) != 32 or any(char not in "0123456789abcdef" for char in stem):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            Path(entry.path).unlink()
                    except OSError:
                        continue
        except OSError:
            return


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _hash_utf8_file(path: Path, limit: int) -> tuple[str, int]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ToolError("FILE_TOO_LARGE", f"Text file exceeds the {limit}-byte limit.", size=total, limit=limit)
                digest.update(chunk)
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ToolError("NOT_UTF8", "Only UTF-8 text files may use transactional text writes.") from exc
    except OSError as exc:
        raise ToolError("READ_FAILED", f"Could not read text file: {exc}") from exc
    return digest.hexdigest(), total


def _commit_staged_text(
    staging_path: Path,
    target: Path,
    *,
    mode: str,
    baseline_sha256: str | None,
    expected_size: int,
    expected_sha256: str,
) -> None:
    temporary_name: str | None = None
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    copied_hash = hashlib.sha256()
    copied_size = 0
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            suffix=".folderbridge.tmp",
            dir=target.parent,
            delete=False,
        ) as output:
            temporary_name = output.name
            with staging_path.open("rb") as source:
                while True:
                    chunk = source.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    decoder.decode(chunk, final=False)
                    copied_hash.update(chunk)
                    copied_size += len(chunk)
                    output.write(chunk)
                decoder.decode(b"", final=True)
            output.flush()
            os.fsync(output.fileno())

        if copied_size != expected_size or copied_hash.hexdigest() != expected_sha256:
            raise ToolError(
                "STAGING_CHANGED",
                "Staged content changed after validation; commit refused.",
                actual_size=copied_size,
                actual_sha256=copied_hash.hexdigest(),
            )

        if mode == "replace":
            if baseline_sha256 is None or not target.is_file():
                raise ToolError("STALE_FILE", "The replacement target disappeared before atomic commit.")
            actual, _ = _hash_utf8_file(target, MAX_TRANSACTION_TEXT_BYTES)
            if actual != baseline_sha256:
                raise ToolError(
                    "STALE_FILE",
                    "The replacement target changed immediately before atomic commit.",
                    actual_sha256=actual,
                )
            try:
                os.chmod(temporary_name, stat.S_IMODE(target.stat().st_mode))
            except OSError:
                pass
            os.replace(temporary_name, target)
            temporary_name = None
            _fsync_directory(target.parent)
        elif mode == "create":
            try:
                os.link(temporary_name, target)
            except FileExistsError as exc:
                raise ToolError("TARGET_EXISTS", "The target appeared before atomic publish; commit refused.") from exc
            except OSError as exc:
                raise ToolError(
                    "WRITE_FAILED",
                    "Could not atomically publish a new file without clobbering an existing target.",
                ) from exc
            _fsync_directory(target.parent)
        else:
            raise ToolError("INVALID_ARGUMENT", "Unknown transactional write mode.")
    except UnicodeDecodeError as exc:
        raise ToolError("NOT_UTF8", "Staged content is not valid UTF-8.") from exc
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("WRITE_FAILED", f"Could not atomically commit text file: {exc}") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
