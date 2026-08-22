from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .config import CONFIG_NAME


MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_RESULTS = 200
MAX_FILES_SCANNED = 5000
MAX_SCAN_SECONDS = 5.0
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
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
SENSITIVE_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"}


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def resolve(self, raw: str, *, for_write: bool = False, allow_directory: bool = False) -> Path:
        if not isinstance(raw, str):
            raise ToolError("INVALID_PATH", "path must be a string")
        if "\x00" in raw:
            raise ToolError("INVALID_PATH", "path cannot contain NUL")
        path = Path(raw or ".")
        if path.is_absolute() or path.drive or ".." in path.parts:
            raise ToolError("PATH_OUTSIDE_WORKSPACE", "Use a relative path without '..'.", path=raw)
        candidate = self.root.joinpath(path)
        self._reject_linked_components(candidate)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise ToolError("PATH_OUTSIDE_WORKSPACE", "Path escapes the workspace.", path=raw) from exc
        relative = resolved.relative_to(self.root)
        self._check_policy(relative, for_write=for_write)
        if resolved.exists() and resolved.is_dir() and not allow_directory:
            raise ToolError("NOT_A_FILE", "Expected a file path.", path=raw)
        return resolved

    def _reject_linked_components(self, candidate: Path) -> None:
        current = self.root
        try:
            relative_parts = candidate.relative_to(self.root).parts
        except ValueError as exc:
            raise ToolError("PATH_OUTSIDE_WORKSPACE", "Path escapes the workspace.") from exc
        for part in relative_parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                break
            if current.is_symlink() or _is_reparse_point(current):
                raise ToolError("LINK_DENIED", "Symlinks and reparse points are not accessible.", path=current.name)

    def _check_policy(self, relative: Path, *, for_write: bool) -> None:
        lowered = [part.lower() for part in relative.parts]
        if any(part in IGNORED_DIRS for part in lowered):
            raise ToolError("IGNORED_PATH", "Generated, dependency, and VCS directories are not exposed.")
        if relative.name:
            name = relative.name.lower()
            if name in SENSITIVE_NAMES or name.startswith(".env.") or relative.suffix.lower() in SENSITIVE_SUFFIXES:
                raise ToolError("SENSITIVE_PATH", "Credential-like files are not exposed.", path=relative.as_posix())
            if for_write and name == CONFIG_NAME.lower():
                raise ToolError("PROTECTED_CONFIG", f"{CONFIG_NAME} cannot be changed through MCP.")

    def read_text(self, raw: str, *, offset: int = 0, limit: int = 64 * 1024) -> dict[str, Any]:
        path = self.resolve(raw)
        if not path.is_file():
            raise ToolError("NOT_FOUND", "File does not exist.", path=raw)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError("INVALID_ARGUMENT", "offset must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_READ_BYTES:
            raise ToolError("INVALID_ARGUMENT", f"limit must be between 1 and {MAX_READ_BYTES}")
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit + 1)
        except OSError as exc:
            raise ToolError("READ_FAILED", f"Could not read {raw}: {exc}") from exc
        if b"\x00" in data:
            raise ToolError("BINARY_FILE", "Binary files are not exposed as text.", path=raw)
        truncated = len(data) > limit
        selected = data[:limit]
        try:
            text = selected.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A bounded chunk can end in the middle of a valid multi-byte code
            # point. Trim only that incomplete suffix; all other decode errors
            # still mean the file/range is not valid UTF-8.
            incomplete_suffix = exc.end == len(selected) and 0 < len(selected) - exc.start <= 3
            if not incomplete_suffix:
                raise ToolError("NOT_UTF8", "Only UTF-8 text files are exposed.", path=raw) from exc
            selected = selected[: exc.start]
            text = selected.decode("utf-8")
            truncated = True
        full_hash = sha256_bytes(_bounded_file_bytes(path, MAX_WRITE_BYTES)) if size <= MAX_WRITE_BYTES else None
        return {
            "path": path.relative_to(self.root).as_posix(),
            "text": text,
            "offset": offset,
            "next_offset": offset + len(selected) if truncated or offset + len(selected) < size else None,
            "size": size,
            "sha256": full_hash,
            "truncated": truncated or offset + len(selected) < size,
        }

    def iter_files(self, raw: str = ".") -> Iterator[Path]:
        start = self.resolve(raw, allow_directory=True)
        if not start.exists():
            return
        if start.is_file():
            yield start
            return
        for directory, dirs, files in os.walk(start, followlinks=False):
            directory_path = Path(directory)
            kept_dirs: list[str] = []
            for name in sorted(dirs):
                child = directory_path / name
                if name.lower() in IGNORED_DIRS or child.is_symlink() or _is_reparse_point(child):
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                candidate = directory_path / name
                try:
                    relative = candidate.relative_to(self.root)
                    self._check_policy(relative, for_write=False)
                    if candidate.is_symlink() or _is_reparse_point(candidate):
                        continue
                except (OSError, ToolError, ValueError):
                    continue
                yield candidate

    def list_files(self, raw: str = ".", *, pattern: str = "*", max_results: int = 100) -> dict[str, Any]:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
            raise ToolError("INVALID_ARGUMENT", "pattern must contain 1 to 500 characters")
        cap = _result_cap(max_results)
        matches: list[str] = []
        scanned = 0
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        truncated = False
        for path in self.iter_files(raw):
            scanned += 1
            if scanned > MAX_FILES_SCANNED or time.monotonic() > deadline:
                truncated = True
                break
            relative = path.relative_to(self.root).as_posix()
            if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(path.name, pattern):
                continue
            if len(matches) < cap:
                matches.append(relative)
            else:
                truncated = True
                break
        return {"files": matches, "count": len(matches), "truncated": truncated, "scanned_files": scanned, "pattern": pattern}

    def search_text(
        self,
        query: str,
        *,
        raw: str = ".",
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ToolError("INVALID_ARGUMENT", "query must contain 1 to 500 characters")
        if not isinstance(case_sensitive, bool):
            raise ToolError("INVALID_ARGUMENT", "case_sensitive must be a boolean")
        cap = _result_cap(max_results)
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        skipped = 0
        scanned = 0
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        for path in self.iter_files(raw):
            scanned += 1
            if scanned > MAX_FILES_SCANNED or time.monotonic() > deadline:
                return {"matches": matches, "truncated": True, "skipped_files": skipped, "scanned_files": scanned}
            try:
                if path.stat().st_size > MAX_READ_BYTES:
                    skipped += 1
                    continue
                data = _bounded_file_bytes(path, MAX_READ_BYTES)
                if b"\x00" in data:
                    skipped += 1
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError, ToolError):
                skipped += 1
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    if len(matches) >= cap:
                        return {"matches": matches, "truncated": True, "skipped_files": skipped, "scanned_files": scanned}
                    matches.append(
                        {
                            "path": path.relative_to(self.root).as_posix(),
                            "line": line_number,
                            "text": line[:1000],
                        }
                    )
        return {"matches": matches, "truncated": False, "skipped_files": skipped, "scanned_files": scanned}

    def edit_file(
        self,
        raw: str,
        *,
        expected_sha256: str | None,
        replacements: list[dict[str, Any]] | None,
        create_content: str | None,
    ) -> dict[str, Any]:
        path = self.resolve(raw, for_write=True)
        exists = path.exists()
        if exists and not path.is_file():
            raise ToolError("NOT_A_FILE", "Expected a file path.", path=raw)
        if exists:
            if create_content is not None:
                raise ToolError("INVALID_ARGUMENT", "create_content is only valid for a new file")
            try:
                original = _bounded_file_bytes(path, MAX_WRITE_BYTES)
            except OSError as exc:
                raise ToolError("READ_FAILED", f"Could not read {raw}: {exc}") from exc
            if len(original) > MAX_WRITE_BYTES or b"\x00" in original:
                raise ToolError("FILE_TOO_LARGE", "Only bounded UTF-8 text files can be edited.")
            actual_hash = sha256_bytes(original)
            if not isinstance(expected_sha256, str) or expected_sha256 != actual_hash:
                raise ToolError(
                    "STALE_FILE",
                    "The file changed or no matching read hash was supplied; read it again before editing.",
                    actual_sha256=actual_hash,
                )
            try:
                updated = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolError("NOT_UTF8", "Only UTF-8 text files can be edited.") from exc
            if not isinstance(replacements, list) or not replacements or len(replacements) > 50:
                raise ToolError("INVALID_ARGUMENT", "replacements must contain 1 to 50 exact replacements")
            for index, replacement in enumerate(replacements):
                if not isinstance(replacement, dict):
                    raise ToolError("INVALID_ARGUMENT", f"replacement {index} must be an object")
                old = replacement.get("old")
                new = replacement.get("new")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise ToolError("INVALID_ARGUMENT", f"replacement {index} needs non-empty old and string new")
                occurrences = updated.count(old)
                if occurrences != 1:
                    raise ToolError(
                        "AMBIGUOUS_REPLACEMENT",
                        f"replacement {index} expected exactly one match, found {occurrences}",
                    )
                updated = updated.replace(old, new, 1)
        else:
            if expected_sha256 is not None or replacements:
                raise ToolError("INVALID_ARGUMENT", "New files use create_content only")
            if not isinstance(create_content, str):
                raise ToolError("INVALID_ARGUMENT", "create_content is required for a new file")
            updated = create_content
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ToolError("FILE_TOO_LARGE", f"Edited files may not exceed {MAX_WRITE_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_linked_components(path)
        _atomic_replace(path, encoded)
        return {
            "path": path.relative_to(self.root).as_posix(),
            "created": not exists,
            "size": len(encoded),
            "sha256": sha256_bytes(encoded),
        }

    def git_view(self, action: str) -> dict[str, Any]:
        if action == "status":
            argv = ["status", "--short", "--branch", "--untracked-files=all"]
        elif action == "diff":
            argv = ["diff", "--no-ext-diff", "--no-textconv", "--stat", "--patch"]
        else:
            raise ToolError("INVALID_ARGUMENT", "git action must be status or diff")
        git = _trusted_executable("git", self.root)
        env = _git_env(self.root)
        try:
            completed = _run_bounded_process(
                [git, "-c", "core.fsmonitor=false", "-c", "diff.external=", *argv],
                cwd=self.root,
                env=env,
                timeout=15,
                output_limit=64 * 1024,
            )
        except OSError as exc:
            raise ToolError("GIT_FAILED", f"Could not run git: {exc}") from exc
        output = (completed[1] + completed[2]).decode("utf-8", errors="replace")
        return {"action": action, "exit_code": completed[0], "output": output, "truncated": completed[3]}


def _result_cap(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_RESULTS:
        raise ToolError("INVALID_ARGUMENT", f"max_results must be between 1 and {MAX_RESULTS}")
    return value


def _bounded_file_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ToolError("FILE_TOO_LARGE", f"File exceeds the {limit}-byte limit.")
    return data


class _PipeCapture(threading.Thread):
    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.total = 0
        self.data = bytearray()

    def run(self) -> None:
        while True:
            chunk = self.stream.read(8192)
            if not chunk:
                return
            self.total += len(chunk)
            room = self.limit - len(self.data)
            if room > 0:
                self.data.extend(chunk[:room])


def _run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    output_limit: int,
) -> tuple[int, bytes, bytes, bool]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout = _PipeCapture(process.stdout, output_limit)
    stderr = _PipeCapture(process.stderr, output_limit)
    stdout.start()
    stderr.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        exit_code = process.wait(timeout=5)
    stdout.join(timeout=5)
    stderr.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    return exit_code, bytes(stdout.data), bytes(stderr.data), timed_out or stdout.total > output_limit or stderr.total > output_limit


def _atomic_replace(path: Path, data: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            try:
                os.chmod(temporary_name, stat.S_IMODE(path.stat().st_mode))
            except OSError:
                pass
        os.replace(temporary_name, path)
    except OSError as exc:
        raise ToolError("WRITE_FAILED", f"Could not update the file: {exc}") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _clean_path(workspace: Path) -> str:
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry).expanduser()
        if _path_is_within(path, workspace):
            continue
        kept.append(str(path))
    return os.pathsep.join(kept)


def clean_environment(workspace: Path) -> dict[str, str]:
    allowed = ("COMSPEC", "LANG", "LC_ALL", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
    if os.name == "nt":
        # pathlib.Path.home() on Windows relies on USERPROFILE, with
        # HOMEDRIVE/HOMEPATH as its fallback. Approved tasks still run with
        # the current OS user's permissions, so preserve only these standard
        # home-resolution variables rather than copying the full environment.
        allowed += ("USERPROFILE", "HOMEDRIVE", "HOMEPATH")
    env = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    env["PATH"] = _clean_path(workspace)
    return env


def _git_env(workspace: Path) -> dict[str, str]:
    env = clean_environment(workspace)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _trusted_executable(command: str, workspace: Path) -> str:
    import shutil

    if Path(command).is_absolute() or len(Path(command).parts) != 1:
        candidate = Path(command).expanduser().resolve(strict=True)
    else:
        found = shutil.which(command, path=_clean_path(workspace))
        if not found:
            raise ToolError("EXECUTABLE_NOT_FOUND", f"Executable not found on trusted PATH: {command}")
        candidate = Path(found).resolve(strict=True)
    if _path_is_within(candidate, workspace):
        raise ToolError("UNTRUSTED_EXECUTABLE", "Executables inside the workspace are not allowed.")
    return str(candidate)


def resolve_task_executable(command: str, workspace: Path) -> str:
    return _trusted_executable(command, workspace)
