from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from .security import ToolError, Workspace


CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024 * 1024


def _normalize_sha(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ToolError("INVALID_ARGUMENT", f"{field} must be 64 hexadecimal characters")
    return value.lower()


def _bounded_max(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_BYTES
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_FILE_BYTES:
        raise ToolError("INVALID_ARGUMENT", f"max_bytes must be between 1 and {MAX_FILE_BYTES}")
    return value


def _is_linkish(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_mode),
    )


def _safe_source_open(path: Path) -> tuple[Any, os.stat_result]:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ToolError("SOURCE_NOT_FOUND", "Source file does not exist.") from exc
    except OSError as exc:
        raise ToolError("SOURCE_STAT_FAILED", "Could not inspect source file.", exception_type=type(exc).__name__) from exc
    if not stat.S_ISREG(before.st_mode) or _is_linkish(path):
        raise ToolError("NOT_A_FILE", "Source must be a safe regular file.")
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise ToolError("SOURCE_READ_FAILED", "Could not open source file.", exception_type=type(exc).__name__) from exc
    try:
        opened = os.fstat(stream.fileno())
        if int(opened.st_dev) != int(before.st_dev) or int(opened.st_ino) != int(before.st_ino):
            raise ToolError("SOURCE_CHANGED", "Source identity changed while opening.")
    except Exception:
        stream.close()
        raise
    return stream, before


def _recheck_source(path: Path, before: os.stat_result) -> None:
    try:
        after = path.lstat()
    except OSError as exc:
        raise ToolError("SOURCE_CHANGED", "Source disappeared or became unreadable during operation.", exception_type=type(exc).__name__) from exc
    if _is_linkish(path) or _signature(after) != _signature(before):
        raise ToolError("SOURCE_CHANGED", "Source changed during operation.")


def _hash_stable_source(path: Path, *, max_bytes: int) -> tuple[int, str, os.stat_result]:
    stream, before = _safe_source_open(path)
    if before.st_size > max_bytes:
        stream.close()
        raise ToolError("FILE_TOO_LARGE", "Source exceeds max_bytes.", size=int(before.st_size), max_bytes=max_bytes)
    digest = hashlib.sha256()
    total = 0
    try:
        with stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ToolError("FILE_TOO_LARGE", "Source exceeds max_bytes while reading.", received_bytes=total, max_bytes=max_bytes)
                digest.update(chunk)
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("SOURCE_READ_FAILED", "Could not read source file.", exception_type=type(exc).__name__) from exc
    if total != before.st_size:
        raise ToolError("SOURCE_CHANGED", "Source size changed while hashing.")
    _recheck_source(path, before)
    return total, digest.hexdigest(), before


def _hash_regular_file(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> tuple[int, str]:
    if not path.exists() or _is_linkish(path):
        raise ToolError("DESTINATION_STALE", "Destination is unavailable or unsafe.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ToolError("DESTINATION_STALE", "Could not inspect destination.", exception_type=type(exc).__name__) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ToolError("NOT_A_FILE", "Destination must be a regular file.")
    if info.st_size > max_bytes:
        raise ToolError("FILE_TOO_LARGE", "File exceeds max_bytes.", size=int(info.st_size), max_bytes=max_bytes)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ToolError("FILE_TOO_LARGE", "File exceeds max_bytes while hashing.", received_bytes=total, max_bytes=max_bytes)
                digest.update(chunk)
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("DESTINATION_READ_FAILED", "Could not hash destination.", exception_type=type(exc).__name__) from exc
    return total, digest.hexdigest()


def _same_file(source: Path, destination: Path) -> bool:
    if source == destination:
        return True
    if source.exists() and destination.exists():
        try:
            return os.path.samefile(source, destination)
        except OSError:
            return False
    return False


def _ensure_parent(workspace: Workspace, destination_raw: str, destination: Path, *, create_parents: bool) -> None:
    parent = destination.parent
    if parent.exists():
        if _is_linkish(parent) or not parent.is_dir():
            raise ToolError("PATH_BLOCKED", "Destination parent is not a safe directory.")
        workspace.resolve(parent.relative_to(workspace.root).as_posix() or ".", for_write=True, allow_directory=True)
        return
    if not create_parents:
        raise ToolError("PARENT_NOT_FOUND", "Destination parent does not exist; set create_parents=true to create it.")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError("PARENT_CREATE_FAILED", "Could not create destination parent.", exception_type=type(exc).__name__) from exc
    # Re-run the authoritative workspace/link policy after creating parents.
    checked = workspace.resolve(destination_raw, for_write=True)
    if checked != destination or not parent.is_dir() or _is_linkish(parent):
        raise ToolError("PATH_BLOCKED", "Destination parent became unsafe.")


def _validate_overwrite(
    destination: Path,
    *,
    overwrite: bool,
    expected_destination_sha256: str | None,
    max_bytes: int,
) -> None:
    exists = destination.exists()
    if exists and _is_linkish(destination):
        raise ToolError("PATH_BLOCKED", "Destination is a link/reparse point.")
    if not overwrite:
        if expected_destination_sha256 is not None:
            raise ToolError("INVALID_ARGUMENT", "expected_destination_sha256 is only valid with overwrite=true")
        if exists:
            raise ToolError("TARGET_EXISTS", "Destination already exists; overwrite is false.")
        return
    if not exists:
        raise ToolError("DESTINATION_STALE", "overwrite=true requires the expected destination to exist.")
    if expected_destination_sha256 is None:
        raise ToolError("INVALID_ARGUMENT", "overwrite=true requires expected_destination_sha256")
    _, actual = _hash_regular_file(destination, max_bytes=max_bytes)
    if actual != expected_destination_sha256:
        raise ToolError(
            "DESTINATION_STALE",
            "Destination changed; refusing overwrite.",
            expected_destination_sha256=expected_destination_sha256,
            actual_destination_sha256=actual,
        )


def _recheck_destination_for_overwrite(destination: Path, *, expected_destination_sha256: str, max_bytes: int) -> None:
    _, actual = _hash_regular_file(destination, max_bytes=max_bytes)
    if actual != expected_destination_sha256:
        raise ToolError(
            "DESTINATION_STALE",
            "Destination changed before publish.",
            expected_destination_sha256=expected_destination_sha256,
            actual_destination_sha256=actual,
        )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish_no_clobber(temp_path: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(temp_path, destination)
        except FileExistsError as exc:
            raise ToolError("TARGET_EXISTS", "Destination appeared before publish; refusing to clobber.") from exc
        except OSError as exc:
            raise ToolError("PUBLISH_FAILED", "Could not publish copied file.", exception_type=type(exc).__name__) from exc
        _fsync_directory(destination.parent)
        return
    try:
        os.link(temp_path, destination)
    except FileExistsError as exc:
        raise ToolError("TARGET_EXISTS", "Destination appeared before publish; refusing to clobber.") from exc
    except OSError as exc:
        raise ToolError("PUBLISH_FAILED", "No-clobber publish requires same-filesystem hard-link support.", exception_type=type(exc).__name__) from exc
    else:
        temp_path.unlink(missing_ok=True)
        _fsync_directory(destination.parent)


def _copy_bytes(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
    expected_source_sha256: str | None,
    overwrite: bool,
    expected_destination_sha256: str | None,
) -> tuple[int, str, os.stat_result]:
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.copy-", dir=str(destination.parent))
    temp_path: Path | None = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    stream, before = _safe_source_open(source)
    try:
        if before.st_size > max_bytes:
            raise ToolError("FILE_TOO_LARGE", "Source exceeds max_bytes.", size=int(before.st_size), max_bytes=max_bytes)
        with stream, os.fdopen(fd, "wb", closefd=True) as output:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ToolError("FILE_TOO_LARGE", "Source exceeds max_bytes while copying.", received_bytes=total, max_bytes=max_bytes)
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != before.st_size:
            raise ToolError("SOURCE_CHANGED", "Source size changed while copying.")
        _recheck_source(source, before)
        actual_source = digest.hexdigest()
        if expected_source_sha256 is not None and actual_source != expected_source_sha256:
            raise ToolError(
                "SOURCE_SHA_MISMATCH",
                "Source does not match expected_source_sha256.",
                expected_source_sha256=expected_source_sha256,
                actual_source_sha256=actual_source,
            )
        try:
            shutil.copystat(source, temp_path, follow_symlinks=False)
        except OSError as exc:
            raise ToolError("METADATA_FAILED", "Could not preserve basic file metadata.", exception_type=type(exc).__name__) from exc
        if overwrite:
            assert expected_destination_sha256 is not None
            _recheck_destination_for_overwrite(
                destination,
                expected_destination_sha256=expected_destination_sha256,
                max_bytes=max_bytes,
            )
            try:
                assert temp_path is not None
                os.replace(temp_path, destination)
                temp_path = None
            except OSError as exc:
                raise ToolError("PUBLISH_FAILED", "Could not atomically replace destination.", exception_type=type(exc).__name__) from exc
            _fsync_directory(destination.parent)
        else:
            assert temp_path is not None
            _publish_no_clobber(temp_path, destination)
            temp_path = None
        return total, actual_source, before
    finally:
        try:
            stream.close()
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def copy_file(
    source_workspace: Workspace,
    destination_workspace: Workspace,
    *,
    source_path: str,
    destination_path: str,
    expected_source_sha256: Any = None,
    overwrite: bool = False,
    expected_destination_sha256: Any = None,
    create_parents: bool = False,
    max_bytes: Any = None,
) -> dict[str, Any]:
    source = source_workspace.resolve(source_path)
    destination = destination_workspace.resolve(destination_path, for_write=True)
    if not source.exists():
        raise ToolError("SOURCE_NOT_FOUND", "Source file does not exist.", path=source_path)
    if _same_file(source, destination):
        raise ToolError("SAME_FILE", "Source and destination refer to the same file.")

    limit = _bounded_max(max_bytes)
    expected_source = _normalize_sha(expected_source_sha256, field="expected_source_sha256")
    expected_destination = _normalize_sha(expected_destination_sha256, field="expected_destination_sha256")

    if destination.exists() and not overwrite and expected_source is not None:
        _, existing_sha = _hash_regular_file(destination, max_bytes=limit)
        if existing_sha == expected_source:
            source_size, source_sha, _ = _hash_stable_source(source, max_bytes=limit)
            if source_sha != expected_source:
                raise ToolError(
                    "SOURCE_SHA_MISMATCH",
                    "Source does not match expected_source_sha256.",
                    expected_source_sha256=expected_source,
                    actual_source_sha256=source_sha,
                )
            return {
                "operation": "copy",
                "already_completed": True,
                "source_path": source.relative_to(source_workspace.root).as_posix(),
                "destination_path": destination.relative_to(destination_workspace.root).as_posix(),
                "size": source_size,
                "sha256": source_sha,
            }

    _validate_overwrite(
        destination,
        overwrite=bool(overwrite),
        expected_destination_sha256=expected_destination,
        max_bytes=limit,
    )
    _ensure_parent(destination_workspace, destination_path, destination, create_parents=bool(create_parents))
    # Re-resolve after parent preparation so the publish path is bound to the current workspace tree.
    destination = destination_workspace.resolve(destination_path, for_write=True)
    size, source_sha, _ = _copy_bytes(
        source,
        destination,
        max_bytes=limit,
        expected_source_sha256=expected_source,
        overwrite=bool(overwrite),
        expected_destination_sha256=expected_destination,
    )
    return {
        "operation": "copy",
        "already_completed": False,
        "source_path": source.relative_to(source_workspace.root).as_posix(),
        "destination_path": destination.relative_to(destination_workspace.root).as_posix(),
        "size": size,
        "sha256": source_sha,
    }


def _atomic_move(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
    expected_destination_sha256: str | None,
    max_bytes: int,
) -> bool:
    if overwrite:
        assert expected_destination_sha256 is not None
        _recheck_destination_for_overwrite(
            destination,
            expected_destination_sha256=expected_destination_sha256,
            max_bytes=max_bytes,
        )
        try:
            os.replace(source, destination)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                return False
            raise ToolError("MOVE_FAILED", "Could not atomically replace destination.", exception_type=type(exc).__name__) from exc
        _fsync_directory(destination.parent)
        return True

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise ToolError("TARGET_EXISTS", "Destination appeared before move; refusing to clobber.") from exc
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                return False
            raise ToolError("MOVE_FAILED", "Could not atomically move file.", exception_type=type(exc).__name__) from exc
        _fsync_directory(destination.parent)
        return True

    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise ToolError("TARGET_EXISTS", "Destination appeared before move; refusing to clobber.") from exc
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EXDEV:
            return False
        raise ToolError("MOVE_FAILED", "No-clobber move requires same-filesystem hard-link support.", exception_type=type(exc).__name__) from exc
    try:
        source.unlink()
    except OSError as exc:
        rollback_error = None
        try:
            destination.unlink()
        except OSError as rollback_exc:
            rollback_error = type(rollback_exc).__name__
        if rollback_error is not None:
            raise ToolError(
                "MOVE_PARTIAL",
                "Source removal and destination rollback both failed; inspect both paths manually.",
                rollback_exception_type=rollback_error,
            ) from exc
        raise ToolError("MOVE_FAILED", "Could not remove source after destination link; rollback succeeded.", exception_type=type(exc).__name__) from exc
    _fsync_directory(destination.parent)
    return True


def move_file(
    source_workspace: Workspace,
    destination_workspace: Workspace,
    *,
    source_path: str,
    destination_path: str,
    expected_source_sha256: Any = None,
    overwrite: bool = False,
    expected_destination_sha256: Any = None,
    create_parents: bool = False,
    max_bytes: Any = None,
) -> dict[str, Any]:
    source = source_workspace.resolve(source_path)
    destination = destination_workspace.resolve(destination_path, for_write=True)
    limit = _bounded_max(max_bytes)
    expected_source = _normalize_sha(expected_source_sha256, field="expected_source_sha256")
    expected_destination = _normalize_sha(expected_destination_sha256, field="expected_destination_sha256")

    if not source.exists():
        if expected_source is not None and destination.exists() and not _is_linkish(destination):
            size, destination_sha = _hash_regular_file(destination, max_bytes=limit)
            if destination_sha == expected_source:
                return {
                    "operation": "move",
                    "already_completed": True,
                    "source_path": source_path,
                    "destination_path": destination.relative_to(destination_workspace.root).as_posix(),
                    "size": size,
                    "sha256": destination_sha,
                }
        raise ToolError("SOURCE_NOT_FOUND", "Source file does not exist.", path=source_path)
    if _same_file(source, destination):
        raise ToolError("SAME_FILE", "Source and destination refer to the same file.")

    size, source_sha, before = _hash_stable_source(source, max_bytes=limit)
    if expected_source is not None and source_sha != expected_source:
        raise ToolError(
            "SOURCE_SHA_MISMATCH",
            "Source does not match expected_source_sha256.",
            expected_source_sha256=expected_source,
            actual_source_sha256=source_sha,
        )
    _validate_overwrite(
        destination,
        overwrite=bool(overwrite),
        expected_destination_sha256=expected_destination,
        max_bytes=limit,
    )
    _ensure_parent(destination_workspace, destination_path, destination, create_parents=bool(create_parents))
    destination = destination_workspace.resolve(destination_path, for_write=True)
    _recheck_source(source, before)

    same_workspace = source_workspace.root == destination_workspace.root
    if same_workspace and _atomic_move(
        source,
        destination,
        overwrite=bool(overwrite),
        expected_destination_sha256=expected_destination,
        max_bytes=limit,
    ):
        return {
            "operation": "move",
            "already_completed": False,
            "source_path": source_path,
            "destination_path": destination.relative_to(destination_workspace.root).as_posix(),
            "size": size,
            "sha256": source_sha,
            "strategy": "atomic-move",
        }

    # Cross-workspace moves, and same-workspace moves that cross a filesystem
    # boundary, publish verified bytes first, verify destination, recheck the
    # original source identity, then remove the source last.
    copied_size, copied_sha, copied_before = _copy_bytes(
        source,
        destination,
        max_bytes=limit,
        expected_source_sha256=source_sha,
        overwrite=bool(overwrite),
        expected_destination_sha256=expected_destination,
    )
    destination_size, destination_sha = _hash_regular_file(destination, max_bytes=limit)
    if copied_size != size or copied_sha != source_sha or destination_size != size or destination_sha != source_sha:
        raise ToolError(
            "MOVE_VERIFY_FAILED",
            "Destination verification failed after cross-volume copy; source was preserved.",
            source_sha256=source_sha,
            destination_sha256=destination_sha,
        )
    _recheck_source(source, copied_before)
    try:
        source.unlink()
    except OSError as exc:
        raise ToolError(
            "MOVE_PARTIAL",
            "Destination was published and verified, but source removal failed; both files may remain.",
            destination_sha256=destination_sha,
            exception_type=type(exc).__name__,
        ) from exc
    _fsync_directory(source.parent)
    return {
        "operation": "move",
        "already_completed": False,
        "source_path": source_path,
        "destination_path": destination.relative_to(destination_workspace.root).as_posix(),
        "size": size,
        "sha256": source_sha,
        "strategy": "verified-copy-delete",
    }
