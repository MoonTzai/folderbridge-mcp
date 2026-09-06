from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError


CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024 * 1024
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
SENSITIVE_NAMES = {
    ".api-config.json",
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "api-config.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
SENSITIVE_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"}
PROTECTED_CONFIG = ".folderbridge.json"
WINDOWS_RESERVED_BASES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}


def _fail(code: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, **details)


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


def _validate_segment(part: str) -> None:
    if not part or part in {".", ".."}:
        raise _fail("FILE_OPS_PATH_INVALID", "Path contains an ambiguous segment", segment=part)
    if ":" in part or any(ord(char) < 32 for char in part):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Windows ADS/control-character path segments are not allowed", segment=part)
    if part.endswith((" ", ".")):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Windows-trimmed path segments are not allowed", segment=part)
    folded = part.casefold()
    device_base = folded.split(".", 1)[0].rstrip(" .")
    if device_base in WINDOWS_RESERVED_BASES:
        raise _fail("FILE_OPS_PATH_BLOCKED", "Windows reserved device names are not allowed", segment=part)


def _clean_relative(value: str, *, for_write: bool) -> Path:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise _fail("FILE_OPS_PATH_INVALID", "Path must be a bounded workspace-relative string")
    if "\\" in value or "\x00" in value or value.endswith("/"):
        raise _fail("FILE_OPS_PATH_INVALID", "Use a regular-file POSIX-style relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts:
        raise _fail("FILE_OPS_PATH_INVALID", "Absolute or empty paths are not allowed")
    for part in pure.parts:
        _validate_segment(part)
    relative = Path(*pure.parts)
    lowered = [part.casefold() for part in pure.parts]
    if any(part in IGNORED_DIRS for part in lowered):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Generated, dependency, and VCS directories are not exposed")
    name = pure.name.casefold()
    suffix = Path(pure.name).suffix.casefold()
    if name in SENSITIVE_NAMES or name.startswith(".env.") or suffix in SENSITIVE_SUFFIXES:
        raise _fail("FILE_OPS_PATH_BLOCKED", "Credential-like files are not exposed", path=value)
    if for_write and name == PROTECTED_CONFIG:
        raise _fail("FILE_OPS_PATH_BLOCKED", "FolderBridge control config cannot be changed through MCP", path=value)
    return relative


def _context_root(context: Any) -> Path:
    if not isinstance(context, dict):
        raise _fail("FILE_OPS_CONTEXT_INVALID", "FolderBridge Extension context must be a mapping")
    root_value = context.get("workspace_root")
    if not root_value:
        raise _fail("FILE_OPS_WORKSPACE_REQUIRED", "A FolderBridge workspace is required")
    if bool(context.get("workspace_read_only", False)):
        raise _fail("READ_ONLY", "FolderBridge is running read-only")
    try:
        root = Path(str(root_value)).resolve(strict=True)
    except OSError as exc:
        raise _fail("FILE_OPS_WORKSPACE_INVALID", "Workspace root is unavailable", exception_type=type(exc).__name__) from exc
    if not root.is_dir() or _is_linkish(root):
        raise _fail("FILE_OPS_WORKSPACE_INVALID", "Workspace root must be a real directory")
    return root


def _verify_no_link_components(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise _fail("FILE_OPS_PATH_INVALID", "Path escapes workspace") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_linkish(current):
                raise _fail("FILE_OPS_PATH_BLOCKED", "Symlinks and reparse points are not accessible", path=relative.as_posix())


def _workspace_path(
    context: Any,
    raw: str,
    *,
    for_write: bool,
    must_exist: bool = False,
) -> tuple[Path, Path, Path]:
    root = _context_root(context)
    relative = _clean_relative(raw, for_write=for_write)
    target = root.joinpath(*relative.parts)
    _verify_no_link_components(root, target)
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _fail("FILE_OPS_PATH_INVALID", "Path escapes workspace", path=raw) from exc
    if target.exists() and _is_linkish(target):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Path is a symlink/reparse point", path=raw)
    if must_exist:
        if not target.exists():
            raise _fail("FILE_OPS_SOURCE_NOT_FOUND", "Source file does not exist", path=raw)
        try:
            info = target.lstat()
        except OSError as exc:
            raise _fail("FILE_OPS_SOURCE_STAT_FAILED", "Could not inspect source file", path=raw, exception_type=type(exc).__name__) from exc
        if not stat.S_ISREG(info.st_mode):
            raise _fail("FILE_OPS_NOT_REGULAR_FILE", "Only regular files are supported", path=raw)
    return root, relative, target


def _normalize_sha(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise _fail("FILE_OPS_SHA_INVALID", f"{field} must be 64 hexadecimal characters")
    return value.lower()


def _bounded_max(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_BYTES
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_FILE_BYTES:
        raise _fail("FILE_OPS_LIMIT_INVALID", f"max_bytes must be between 1 and {MAX_FILE_BYTES}")
    return value


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
    except OSError as exc:
        raise _fail("FILE_OPS_SOURCE_STAT_FAILED", "Could not inspect source file", exception_type=type(exc).__name__) from exc
    if not stat.S_ISREG(before.st_mode) or _is_linkish(path):
        raise _fail("FILE_OPS_NOT_REGULAR_FILE", "Source must be a safe regular file")
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise _fail("FILE_OPS_SOURCE_READ_FAILED", "Could not open source file", exception_type=type(exc).__name__) from exc
    try:
        opened = os.fstat(stream.fileno())
        if int(opened.st_dev) != int(before.st_dev) or int(opened.st_ino) != int(before.st_ino):
            raise _fail("FILE_OPS_SOURCE_CHANGED", "Source identity changed while opening")
    except Exception:
        stream.close()
        raise
    return stream, before


def _recheck_source(path: Path, before: os.stat_result) -> None:
    try:
        after = path.lstat()
    except OSError as exc:
        raise _fail("FILE_OPS_SOURCE_CHANGED", "Source disappeared or became unreadable during operation", exception_type=type(exc).__name__) from exc
    if _is_linkish(path) or _signature(after) != _signature(before):
        raise _fail("FILE_OPS_SOURCE_CHANGED", "Source changed during operation")


def _hash_stable_source(path: Path, *, max_bytes: int) -> tuple[int, str, os.stat_result]:
    stream, before = _safe_source_open(path)
    if before.st_size > max_bytes:
        stream.close()
        raise _fail("FILE_OPS_TOO_LARGE", "Source exceeds max_bytes", size=int(before.st_size), max_bytes=max_bytes)
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
                    raise _fail("FILE_OPS_TOO_LARGE", "Source exceeds max_bytes while reading", received_bytes=total, max_bytes=max_bytes)
                digest.update(chunk)
    except ExtensionError:
        raise
    except OSError as exc:
        raise _fail("FILE_OPS_SOURCE_READ_FAILED", "Could not read source file", exception_type=type(exc).__name__) from exc
    if total != before.st_size:
        raise _fail("FILE_OPS_SOURCE_CHANGED", "Source size changed while hashing")
    _recheck_source(path, before)
    return total, digest.hexdigest(), before


def _hash_regular_file(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> tuple[int, str]:
    if not path.exists() or _is_linkish(path):
        raise _fail("FILE_OPS_DESTINATION_STALE", "Destination is unavailable or unsafe")
    try:
        info = path.lstat()
    except OSError as exc:
        raise _fail("FILE_OPS_DESTINATION_STALE", "Could not inspect destination", exception_type=type(exc).__name__) from exc
    if not stat.S_ISREG(info.st_mode):
        raise _fail("FILE_OPS_NOT_REGULAR_FILE", "Destination must be a regular file")
    if info.st_size > max_bytes:
        raise _fail("FILE_OPS_TOO_LARGE", "File exceeds max_bytes", size=int(info.st_size), max_bytes=max_bytes)
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
                    raise _fail("FILE_OPS_TOO_LARGE", "File exceeds max_bytes while hashing", received_bytes=total, max_bytes=max_bytes)
                digest.update(chunk)
    except ExtensionError:
        raise
    except OSError as exc:
        raise _fail("FILE_OPS_DESTINATION_READ_FAILED", "Could not hash destination", exception_type=type(exc).__name__) from exc
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


def _ensure_parent(root: Path, destination: Path, *, create_parents: bool) -> None:
    parent = destination.parent
    if parent.exists():
        if _is_linkish(parent) or not parent.is_dir():
            raise _fail("FILE_OPS_PATH_BLOCKED", "Destination parent is not a safe directory")
        _verify_no_link_components(root, parent)
        return
    if not create_parents:
        raise _fail("FILE_OPS_PARENT_NOT_FOUND", "Destination parent does not exist; set create_parents=true to create it")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail("FILE_OPS_PARENT_CREATE_FAILED", "Could not create destination parent", exception_type=type(exc).__name__) from exc
    _verify_no_link_components(root, parent)
    if not parent.is_dir() or _is_linkish(parent):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Destination parent became unsafe")


def _validate_overwrite(
    destination: Path,
    *,
    overwrite: bool,
    expected_destination_sha256: str | None,
    max_bytes: int,
) -> str | None:
    exists = destination.exists()
    if exists and _is_linkish(destination):
        raise _fail("FILE_OPS_PATH_BLOCKED", "Destination is a link/reparse point")
    if not overwrite:
        if expected_destination_sha256 is not None:
            raise _fail("FILE_OPS_DESTINATION_SHA_INVALID", "expected_destination_sha256 is only valid with overwrite=true")
        if exists:
            raise _fail("FILE_OPS_EXISTS", "Destination already exists; overwrite is false")
        return None
    if not exists:
        raise _fail("FILE_OPS_DESTINATION_STALE", "overwrite=true requires the expected destination to exist")
    if expected_destination_sha256 is None:
        raise _fail("FILE_OPS_DESTINATION_SHA_REQUIRED", "overwrite=true requires expected_destination_sha256")
    _, actual = _hash_regular_file(destination, max_bytes=max_bytes)
    if actual != expected_destination_sha256:
        raise _fail(
            "FILE_OPS_DESTINATION_STALE",
            "Destination changed; refusing overwrite",
            expected_destination_sha256=expected_destination_sha256,
            actual_destination_sha256=actual,
        )
    return actual


def _recheck_destination_for_overwrite(
    destination: Path,
    *,
    expected_destination_sha256: str,
    max_bytes: int,
) -> None:
    _, actual = _hash_regular_file(destination, max_bytes=max_bytes)
    if actual != expected_destination_sha256:
        raise _fail(
            "FILE_OPS_DESTINATION_STALE",
            "Destination changed before publish",
            expected_destination_sha256=expected_destination_sha256,
            actual_destination_sha256=actual,
        )


def _publish_no_clobber(temp_path: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(temp_path, destination)
        except FileExistsError as exc:
            raise _fail("FILE_OPS_EXISTS", "Destination appeared before publish; refusing to clobber") from exc
        except OSError as exc:
            raise _fail("FILE_OPS_PUBLISH_FAILED", "Could not publish copied file", exception_type=type(exc).__name__) from exc
        return
    try:
        os.link(temp_path, destination)
    except FileExistsError as exc:
        raise _fail("FILE_OPS_EXISTS", "Destination appeared before publish; refusing to clobber") from exc
    except OSError as exc:
        raise _fail("FILE_OPS_PUBLISH_FAILED", "No-clobber publish requires same-filesystem hard-link support", exception_type=type(exc).__name__) from exc
    else:
        temp_path.unlink(missing_ok=True)


def _copy_file(params: dict[str, Any], context: Any) -> dict[str, Any]:
    root, source_relative, source = _workspace_path(
        context,
        str(params.get("source_path") or ""),
        for_write=False,
        must_exist=True,
    )
    _, destination_relative, destination = _workspace_path(
        context,
        str(params.get("destination_path") or ""),
        for_write=True,
        must_exist=False,
    )
    if _same_file(source, destination):
        raise _fail("FILE_OPS_SAME_FILE", "Source and destination refer to the same file")

    max_bytes = _bounded_max(params.get("max_bytes"))
    overwrite = bool(params.get("overwrite", False))
    create_parents = bool(params.get("create_parents", False))
    expected_source = _normalize_sha(params.get("expected_source_sha256"), field="expected_source_sha256")
    expected_destination = _normalize_sha(
        params.get("expected_destination_sha256"),
        field="expected_destination_sha256",
    )

    if destination.exists() and not overwrite and expected_source is not None:
        _, existing_sha = _hash_regular_file(destination, max_bytes=max_bytes)
        if existing_sha == expected_source:
            source_size, source_sha, _ = _hash_stable_source(source, max_bytes=max_bytes)
            if source_sha != expected_source:
                raise _fail(
                    "FILE_OPS_SOURCE_SHA_MISMATCH",
                    "Source does not match expected_source_sha256",
                    expected_source_sha256=expected_source,
                    actual_source_sha256=source_sha,
                )
            return {
                "ok": True,
                "operation": "copy-file",
                "already_completed": True,
                "source_path": source_relative.as_posix(),
                "destination_path": destination_relative.as_posix(),
                "size": source_size,
                "sha256": source_sha,
                "workspace_artifacts": [
                    {"path": destination_relative.as_posix(), "label": "copied file", "kind": "file"}
                ],
            }

    _validate_overwrite(
        destination,
        overwrite=overwrite,
        expected_destination_sha256=expected_destination,
        max_bytes=max_bytes,
    )
    _ensure_parent(root, destination, create_parents=create_parents)

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.copy-", dir=str(destination.parent))
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    stream, before = _safe_source_open(source)
    try:
        if before.st_size > max_bytes:
            raise _fail("FILE_OPS_TOO_LARGE", "Source exceeds max_bytes", size=int(before.st_size), max_bytes=max_bytes)
        with stream, os.fdopen(fd, "wb", closefd=True) as output:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _fail("FILE_OPS_TOO_LARGE", "Source exceeds max_bytes while copying", received_bytes=total, max_bytes=max_bytes)
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != before.st_size:
            raise _fail("FILE_OPS_SOURCE_CHANGED", "Source size changed while copying")
        _recheck_source(source, before)
        actual_source = digest.hexdigest()
        if expected_source is not None and actual_source != expected_source:
            raise _fail(
                "FILE_OPS_SOURCE_SHA_MISMATCH",
                "Source does not match expected_source_sha256",
                expected_source_sha256=expected_source,
                actual_source_sha256=actual_source,
            )
        try:
            shutil.copystat(source, temp_path, follow_symlinks=False)
        except OSError as exc:
            raise _fail("FILE_OPS_METADATA_FAILED", "Could not preserve basic file metadata", exception_type=type(exc).__name__) from exc
        if overwrite:
            assert expected_destination is not None
            _recheck_destination_for_overwrite(
                destination,
                expected_destination_sha256=expected_destination,
                max_bytes=max_bytes,
            )
            try:
                os.replace(temp_path, destination)
            except OSError as exc:
                raise _fail("FILE_OPS_PUBLISH_FAILED", "Could not atomically replace destination", exception_type=type(exc).__name__) from exc
        else:
            _publish_no_clobber(temp_path, destination)
        return {
            "ok": True,
            "operation": "copy-file",
            "already_completed": False,
            "source_path": source_relative.as_posix(),
            "destination_path": destination_relative.as_posix(),
            "size": total,
            "sha256": actual_source,
            "workspace_artifacts": [
                {"path": destination_relative.as_posix(), "label": "copied file", "kind": "file"}
            ],
        }
    finally:
        try:
            stream.close()
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)


def _move_no_clobber(source: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise _fail("FILE_OPS_EXISTS", "Destination appeared before move; refusing to clobber") from exc
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                raise _fail("FILE_OPS_CROSS_DEVICE", "Cross-filesystem move is not supported; copy first instead") from exc
            raise _fail("FILE_OPS_MOVE_FAILED", "Could not atomically move file", exception_type=type(exc).__name__) from exc
        return

    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise _fail("FILE_OPS_EXISTS", "Destination appeared before move; refusing to clobber") from exc
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EXDEV:
            raise _fail("FILE_OPS_CROSS_DEVICE", "Cross-filesystem move is not supported; copy first instead") from exc
        raise _fail("FILE_OPS_MOVE_FAILED", "No-clobber move requires same-filesystem hard-link support", exception_type=type(exc).__name__) from exc
    try:
        source.unlink()
    except OSError as exc:
        rollback_error = None
        try:
            destination.unlink()
        except OSError as rollback_exc:
            rollback_error = type(rollback_exc).__name__
        if rollback_error is not None:
            raise _fail(
                "FILE_OPS_MOVE_PARTIAL",
                "Source removal and destination rollback both failed; inspect both paths manually",
                source_path=str(source),
                destination_path=str(destination),
                rollback_exception_type=rollback_error,
            ) from exc
        raise _fail("FILE_OPS_MOVE_FAILED", "Could not remove source after destination link; rollback succeeded", exception_type=type(exc).__name__) from exc


def _move_file(params: dict[str, Any], context: Any) -> dict[str, Any]:
    root, source_relative, source = _workspace_path(
        context,
        str(params.get("source_path") or ""),
        for_write=False,
        must_exist=False,
    )
    _, destination_relative, destination = _workspace_path(
        context,
        str(params.get("destination_path") or ""),
        for_write=True,
        must_exist=False,
    )
    if source == destination:
        raise _fail("FILE_OPS_SAME_FILE", "Source and destination are the same path")

    max_bytes = _bounded_max(params.get("max_bytes"))
    overwrite = bool(params.get("overwrite", False))
    create_parents = bool(params.get("create_parents", False))
    expected_source = _normalize_sha(params.get("expected_source_sha256"), field="expected_source_sha256")
    expected_destination = _normalize_sha(
        params.get("expected_destination_sha256"),
        field="expected_destination_sha256",
    )

    if not source.exists():
        if expected_source is not None and destination.exists() and not _is_linkish(destination):
            _, destination_sha = _hash_regular_file(destination, max_bytes=max_bytes)
            if destination_sha == expected_source:
                return {
                    "ok": True,
                    "operation": "move-file",
                    "already_completed": True,
                    "source_path": source_relative.as_posix(),
                    "destination_path": destination_relative.as_posix(),
                    "size": destination.stat().st_size,
                    "sha256": destination_sha,
                    "workspace_artifacts": [
                        {"path": destination_relative.as_posix(), "label": "moved file", "kind": "file"}
                    ],
                }
        raise _fail("FILE_OPS_SOURCE_NOT_FOUND", "Source file does not exist", path=source_relative.as_posix())

    if _same_file(source, destination):
        raise _fail("FILE_OPS_SAME_FILE", "Source and destination refer to the same file")
    try:
        source_info = source.lstat()
    except OSError as exc:
        raise _fail("FILE_OPS_SOURCE_STAT_FAILED", "Could not inspect source file", exception_type=type(exc).__name__) from exc
    if not stat.S_ISREG(source_info.st_mode) or _is_linkish(source):
        raise _fail("FILE_OPS_NOT_REGULAR_FILE", "Only safe regular files are supported")

    size, source_sha, before = _hash_stable_source(source, max_bytes=max_bytes)
    if expected_source is not None and source_sha != expected_source:
        raise _fail(
            "FILE_OPS_SOURCE_SHA_MISMATCH",
            "Source does not match expected_source_sha256",
            expected_source_sha256=expected_source,
            actual_source_sha256=source_sha,
        )

    _validate_overwrite(
        destination,
        overwrite=overwrite,
        expected_destination_sha256=expected_destination,
        max_bytes=max_bytes,
    )
    _ensure_parent(root, destination, create_parents=create_parents)
    _recheck_source(source, before)

    if overwrite:
        assert expected_destination is not None
        _recheck_destination_for_overwrite(
            destination,
            expected_destination_sha256=expected_destination,
            max_bytes=max_bytes,
        )
        try:
            os.replace(source, destination)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                raise _fail("FILE_OPS_CROSS_DEVICE", "Cross-filesystem move is not supported; copy first instead") from exc
            raise _fail("FILE_OPS_MOVE_FAILED", "Could not atomically replace destination", exception_type=type(exc).__name__) from exc
    else:
        _move_no_clobber(source, destination)

    return {
        "ok": True,
        "operation": "move-file",
        "already_completed": False,
        "source_path": source_relative.as_posix(),
        "destination_path": destination_relative.as_posix(),
        "size": size,
        "sha256": source_sha,
        "workspace_artifacts": [
            {"path": destination_relative.as_posix(), "label": "moved file", "kind": "file"}
        ],
    }


def handle(action: str, params: dict[str, Any], context: Any) -> dict[str, Any]:
    if action == "copy-file":
        return _copy_file(params, context)
    if action == "move-file":
        return _move_file(params, context)
    raise _fail("FILE_OPS_ACTION_UNSUPPORTED", f"Unsupported action: {action}")
