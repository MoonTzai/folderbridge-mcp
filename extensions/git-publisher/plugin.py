from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


MAX_OUTPUT = 32_000
MAX_COMMIT_FILE_BYTES = 100 * 1024 * 1024
MAX_COMMIT_PATHS = 128
MAX_RELEASE_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_RELEASE_ASSETS = 64
RELEASE_GH_TIMEOUT_SECONDS = 115 * 60
MAX_STATUS_PAGE = 500
DEFAULT_STATUS_PAGE = 200
DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "target", "vendor", ".idea", ".vscode",
}
DENIED_BASENAMES = {
    ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", "credentials.json",
}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
RELEASE_DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode",
}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
INVALID_RELEASE_NAME_RE = re.compile(r'[^A-Za-z0-9._+-]')
TOKEN_RE = re.compile(r"(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)")
URL_CREDENTIAL_RE = re.compile(r"(https://)[^/@\s]+@", re.IGNORECASE)
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    if action == "status":
        return _status(
            root,
            offset=params.get("offset", 0),
            limit=params.get("limit", DEFAULT_STATUS_PAGE),
        )
    if bool(context.get("workspace_read_only")):
        raise RuntimeError("FolderBridge is read-only; Git Publisher mutations are unavailable.")
    if action == "connect":
        return _connect(root, username=params.get("username"), force=bool(params.get("force", False)))
    if action == "commit":
        return _commit(root, params.get("paths"), params.get("message"))
    if action == "push":
        return _push(root)
    if action == "release":
        return _release(root)
    if action == "release-assets":
        return _release_assets(
            root,
            params.get("tag"),
            params.get("title"),
            params.get("assets"),
            latest=params.get("latest", True),
        )
    raise RuntimeError(f"unsupported action: {action}")


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _git_executable() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Git Publisher browser authentication currently requires Windows/Git for Windows.")
    git = shutil.which("git.exe")
    if not git:
        raise RuntimeError("git.exe was not found on the trusted PATH; install Git for Windows first.")
    return str(Path(git).resolve(strict=True))


def _gh_executable() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Git Publisher Release publishing currently requires Windows GitHub CLI.")
    gh = shutil.which("gh.exe")
    if not gh:
        raise RuntimeError("gh.exe was not found on the trusted PATH; install GitHub CLI first.")
    return str(Path(gh).resolve(strict=True))


def _redact(text: str) -> str:
    text = TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    text = URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    return text[:MAX_OUTPUT]


def _run_git(
    root: Path,
    *args: str,
    timeout: int = 30,
    check: bool = True,
    interactive_auth: bool = False,
    gcm_only: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    git = _git_executable()
    env = os.environ.copy()
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if interactive_auth:
        env["GCM_INTERACTIVE"] = "Always"
        env["GCM_GITHUB_AUTHMODES"] = "browser"
    else:
        env["GIT_TERMINAL_PROMPT"] = "0"
    argv = [git]
    if gcm_only:
        argv += ["-c", "credential.helper=", "-c", "credential.helper=manager"]
    argv += [*args]
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise RuntimeError(f"could not execute git: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_owned_process_tree(process, hide_window=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        raise RuntimeError(f"git operation exceeded {timeout} seconds") from exc
    completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        stderr = _redact(completed.stderr.decode("utf-8", errors="replace").strip())
        stdout = _redact(completed.stdout.decode("utf-8", errors="replace").strip())
        raise RuntimeError(stderr or stdout or f"git command failed with exit code {completed.returncode}")
    return completed


def _text(completed: subprocess.CompletedProcess[bytes]) -> str:
    return _redact(completed.stdout.decode("utf-8", errors="replace").strip())


def _github_token_from_gcm(root: Path) -> str:
    git = _git_executable()
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    argv = [git, "-c", "credential.helper=", "-c", "credential.helper=manager", "credential", "fill"]
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise RuntimeError(f"could not query Git Credential Manager: {exc}") from exc
    try:
        stdout, _stderr = process.communicate(input=b"protocol=https\nhost=github.com\n\n", timeout=30)
    except subprocess.TimeoutExpired as exc:
        terminate_owned_process_tree(process, hide_window=True)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        raise RuntimeError("Git Credential Manager credential lookup exceeded 30 seconds") from exc
    if process.returncode != 0:
        raise RuntimeError("Git Credential Manager could not provide GitHub credentials; reconnect Git Publisher first")
    password = ""
    for line in stdout.decode("utf-8", errors="strict").splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "password":
            password = value
            break
    if not password or len(password) > 4096 or any(char in password for char in "\r\n\x00"):
        raise RuntimeError("Git Credential Manager returned no usable GitHub credential")
    return password


def _run_gh(
    root: Path,
    *args: str,
    token: str,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    gh = _gh_executable()
    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)
    env["GH_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_PAGER"] = "cat"
    try:
        process = subprocess.Popen(
            [gh, *args],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise RuntimeError(f"could not execute gh: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_owned_process_tree(process, hide_window=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        raise RuntimeError(f"gh operation exceeded {timeout} seconds") from exc
    completed = subprocess.CompletedProcess([gh, *args], process.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        stderr_text = _redact(completed.stderr.decode("utf-8", errors="replace").strip())
        stdout_text = _redact(completed.stdout.decode("utf-8", errors="replace").strip())
        raise RuntimeError(stderr_text or stdout_text or f"gh command failed with exit code {completed.returncode}")
    return completed


def _repo_info(root: Path) -> dict[str, str]:
    top = _text(_run_git(root, "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"))
    try:
        if Path(top).resolve(strict=True) != root:
            raise RuntimeError("selected FolderBridge workspace must itself be the Git repository root")
    except OSError as exc:
        raise RuntimeError("could not validate Git repository root") from exc
    branch = _text(_run_git(root, "-c", "core.fsmonitor=false", "symbolic-ref", "--quiet", "--short", "HEAD"))
    if not branch or branch == "HEAD" or not BRANCH_RE.fullmatch(branch):
        raise RuntimeError("Git Publisher requires a normal named current branch")
    verify = _run_git(root, "check-ref-format", "--branch", branch, check=False)
    if verify.returncode != 0:
        raise RuntimeError("current branch name is not safe for publication")
    origin = _text(_run_git(root, "remote", "get-url", "--push", "origin"))
    owner, repo, sanitized = _validate_origin(origin)
    _reject_unsafe_local_git_config(root)
    return {"branch": branch, "origin": sanitized, "owner": owner, "repo": repo}


def _validate_origin(raw: str) -> tuple[str, str, str]:
    parts = urlsplit(raw.strip())
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower() != "github.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("origin must be a credential-free https://github.com/<owner>/<repo>[.git] URL")
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) != 2:
        raise RuntimeError("origin must target exactly one GitHub owner/repository pair")
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    safe_piece = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
    if not owner or not repo or not safe_piece.fullmatch(owner) or not safe_piece.fullmatch(repo):
        raise RuntimeError("origin contains an unsupported GitHub owner or repository name")
    return owner, repo, f"https://github.com/{owner}/{repo}.git"


def _reject_unsafe_local_git_config(root: Path) -> None:
    completed = _run_git(root, "config", "--local", "--null", "--list")
    unsafe: set[str] = set()
    for record in completed.stdout.decode("utf-8", errors="replace").split("\x00"):
        if not record:
            continue
        key = record.split("\n", 1)[0].split("=", 1)[0].strip().lower()
        if (
            key == "credential.helper"
            or (key.startswith("credential.") and key.endswith(".helper"))
            or key in {"core.sshcommand", "core.hookspath", "core.fsmonitor", "diff.external", "remote.origin.pushurl", "remote.origin.receivepack"}
            or (key.startswith("url.") and (key.endswith(".insteadof") or key.endswith(".pushinsteadof")))
            or (key.startswith("filter.") and key.rsplit(".", 1)[-1] in {"clean", "smudge", "process"})
        ):
            unsafe.add(key)
    if unsafe:
        raise RuntimeError("repository-local Git configuration contains unsafe execution or push-target settings: " + ", ".join(sorted(unsafe)))


def _gcm_status(root: Path) -> dict[str, Any]:
    version = _run_git(root, "credential-manager", "--version", check=False)
    if version.returncode != 0:
        return {
            "available": False,
            "secure_store": "Windows Credential Manager",
            "accounts": [],
            "reason": "Git Credential Manager is not available through Git for Windows.",
        }
    listed = _run_git(root, "credential-manager", "github", "list", "--url", "https://github.com", check=False)
    accounts: list[str] = []
    if listed.returncode == 0:
        for line in _text(listed).splitlines():
            clean = line.strip()
            if clean and len(clean) <= 256 and "token" not in clean.lower() and "password" not in clean.lower():
                accounts.append(clean)
    return {
        "available": True,
        "version": _text(version),
        "secure_store": "Windows Credential Manager",
        "accounts": accounts[:20],
        "authenticated": bool(accounts),
    }


def _status_page(root: Path, *, offset: Any = 0, limit: Any = DEFAULT_STATUS_PAGE) -> dict[str, Any]:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise RuntimeError("status offset must be a non-negative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_STATUS_PAGE:
        raise RuntimeError(f"status limit must be between 1 and {MAX_STATUS_PAGE}")
    completed = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z", "--untracked-files=all")
    raw = completed.stdout.decode("utf-8", errors="replace")
    pieces = raw.split("\x00")
    changes: list[dict[str, str]] = []
    total = 0
    index = 0
    while index < len(pieces):
        record = pieces[index]
        index += 1
        if not record or len(record) < 4:
            continue
        code = record[:2]
        path = record[3:]
        item = {"code": code, "path": path}
        if ("R" in code or "C" in code) and index < len(pieces) and pieces[index]:
            item["original_path"] = pieces[index]
            index += 1
        if total >= offset and len(changes) < limit:
            changes.append(item)
        total += 1
    next_offset = offset + len(changes) if offset + len(changes) < total else None
    return {
        "changes": changes,
        "change_count": total,
        "offset": offset,
        "limit": limit,
        "truncated": next_offset is not None,
        "next_offset": next_offset,
    }


def _staged_paths(root: Path) -> list[str]:
    completed = _run_git(
        root,
        "-c", "core.fsmonitor=false",
        "diff", "--cached", "--no-ext-diff", "--no-renames", "--name-only", "-z",
    )
    return [item for item in completed.stdout.decode("utf-8", errors="replace").split("\x00") if item]


def _status(root: Path, *, offset: Any = 0, limit: Any = DEFAULT_STATUS_PAGE) -> dict[str, Any]:
    repo = _repo_info(root)
    name = _text(_run_git(root, "config", "user.name", check=False))
    email = _text(_run_git(root, "config", "user.email", check=False))
    head = _text(_run_git(root, "rev-parse", "HEAD", check=False))
    page = _status_page(root, offset=offset, limit=limit)
    return {
        **repo,
        "head": head or None,
        "identity": {"name": name or None, "email": email or None, "ready": bool(name and email)},
        **page,
        "staged_paths": _staged_paths(root),
        "credential_manager": _gcm_status(root),
        "auth_model": "browser OAuth via Git Credential Manager; credentials remain in Windows Credential Manager",
        "token_input_exposed_to_model": False,
    }


def _connect(root: Path, *, username: Any, force: bool) -> dict[str, Any]:
    repo = _repo_info(root)
    gcm = _gcm_status(root)
    if not gcm.get("available"):
        raise RuntimeError("Git Credential Manager is required for browser authorization; install/repair Git for Windows with GCM enabled")
    argv = ["credential-manager", "github", "login", "--url", "https://github.com", "--web"]
    if isinstance(username, str) and username.strip():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", username.strip()):
            raise RuntimeError("username contains unsupported characters")
        argv += ["--username", username.strip()]
    if force:
        argv.append("--force")
    completed = _run_git(root, *argv, timeout=570, interactive_auth=True)
    refreshed = _gcm_status(root)
    return {
        **repo,
        "connected": bool(refreshed.get("authenticated")),
        "credential_manager": refreshed,
        "browser_flow": True,
        "credential_store": "Windows Credential Manager",
        "result": _redact((completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip())[:4000],
        "note": "No token is accepted through the MCP action or written into FolderBridge configuration, remotes, or logs.",
    }


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _reject_links(root: Path, candidate: Path) -> None:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeError("selected path escapes the workspace") from exc
    current = root
    for part in parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink() or _is_reparse(current):
            raise RuntimeError(f"linked/reparse path component is not allowed: {part}")


def _clean_commit_path(root: Path, raw: Any) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise RuntimeError("commit paths must be non-empty POSIX-style workspace-relative strings")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RuntimeError("commit path must stay inside the selected workspace")
    if any(part.lower() in DENIED_PARTS for part in rel.parts):
        raise RuntimeError("commit path targets a denied VCS/dependency/generated directory")
    basename = rel.name.lower()
    suffix = Path(rel.name).suffix.lower()
    if basename in DENIED_BASENAMES or basename.startswith(".env.") or suffix in DENIED_SUFFIXES:
        raise RuntimeError("credential/key-like files are not eligible for Git Publisher commits")
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    normalized = rel.as_posix()
    tracked = _run_git(root, "ls-files", "--error-unmatch", "--", normalized, check=False).returncode == 0
    changed = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--", normalized)
    status_bytes = changed.stdout.strip()

    if candidate.exists() or candidate.is_symlink():
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeError("commit path does not resolve to a regular workspace file") from exc
        if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
            raise RuntimeError("Git Publisher commits only explicit regular files or tracked deletions")
        if resolved.stat().st_size > MAX_COMMIT_FILE_BYTES:
            raise RuntimeError(
                f"commit file exceeds GitHub's regular-Git {MAX_COMMIT_FILE_BYTES}-byte file limit; "
                "use Git LFS or a Release asset for larger files"
            )
        normalized = resolved.relative_to(root).as_posix()
        _reject_transforming_attributes(root, normalized)
        tracked = _run_git(root, "ls-files", "--error-unmatch", "--", normalized, check=False).returncode == 0
        if not tracked:
            ignored = _run_git(root, "check-ignore", "-q", "--", normalized, check=False).returncode == 0
            if ignored:
                raise RuntimeError(f"untracked ignored file cannot be published: {normalized}")
    else:
        if not tracked or not status_bytes:
            raise RuntimeError("missing commit path must be a tracked Git deletion")
        status_code = status_bytes[:2].decode("ascii", errors="replace")
        if "D" not in status_code:
            raise RuntimeError("missing commit path must be a tracked Git deletion")

    if not status_bytes:
        raise RuntimeError(f"selected file has no Git change to commit: {normalized}")
    return normalized


def _reject_transforming_attributes(root: Path, rel: str) -> None:
    completed = _run_git(root, "check-attr", "-z", "filter", "working-tree-encoding", "--", rel)
    parts = completed.stdout.decode("utf-8", errors="replace").split("\x00")
    for index in range(0, len(parts) - 2, 3):
        _path, attribute, value = parts[index:index + 3]
        if attribute in {"filter", "working-tree-encoding"} and value not in {"", "unspecified", "unset"}:
            raise RuntimeError(f"selected file uses Git attribute {attribute}={value}; Publisher refuses content-transforming attributes")


def _rollback_stage(root: Path, paths: list[str]) -> None:
    if not paths:
        return
    _run_git(root, "reset", "-q", "HEAD", "--", *paths, check=False)


def _commit(root: Path, raw_paths: Any, raw_message: Any) -> dict[str, Any]:
    repo = _repo_info(root)
    _run_git(root, "rev-parse", "--verify", "HEAD")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise RuntimeError("paths must contain at least one explicit file")
    if len(raw_paths) > MAX_COMMIT_PATHS:
        raise RuntimeError(f"paths may contain at most {MAX_COMMIT_PATHS} explicit files per commit")
    if not isinstance(raw_message, str) or not raw_message.strip() or "\x00" in raw_message:
        raise RuntimeError("commit message must be non-empty")
    message = raw_message.strip()
    if len(message) > 1000:
        raise RuntimeError("commit message exceeds 1000 characters")
    existing_staged = _staged_paths(root)
    if existing_staged:
        raise RuntimeError("Git Publisher refuses to commit while unrelated staged changes already exist: " + ", ".join(existing_staged[:20]))
    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        normalized = _clean_commit_path(root, raw)
        key = normalized.casefold()
        if key in seen:
            raise RuntimeError(f"duplicate commit path: {normalized}")
        seen.add(key)
        paths.append(normalized)
    existing_paths: list[str] = []
    deleted_paths: list[str] = []
    for path in paths:
        candidate = root.joinpath(*PurePosixPath(path).parts)
        if candidate.exists() or candidate.is_symlink():
            existing_paths.append(path)
        else:
            deleted_paths.append(path)
    try:
        if existing_paths:
            _run_git(root, "-c", "core.fsmonitor=false", "add", "--", *existing_paths)
        if deleted_paths:
            _run_git(
                root,
                "-c", "core.fsmonitor=false",
                "update-index", "--force-remove", "--", *deleted_paths,
            )
        staged = _staged_paths(root)
        if {item.casefold() for item in staged} != {item.casefold() for item in paths}:
            raise RuntimeError("staged file set differs from the explicit Publisher allowlist")
        completed = _run_git(
            root,
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=NUL",
            "-c", "commit.gpgSign=false",
            "commit", "--no-gpg-sign", "--no-verify", "-m", message,
            timeout=120,
        )
    except Exception:
        _rollback_stage(root, paths)
        raise
    commit_sha = _text(_run_git(root, "rev-parse", "HEAD"))
    remaining = _status_page(root, offset=0, limit=DEFAULT_STATUS_PAGE)
    return {
        **repo,
        "commit": commit_sha,
        "paths": paths,
        "message": message,
        "git_output": _redact((completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip())[:4000],
        "remaining_changes": remaining["changes"],
        "remaining_change_count": remaining["change_count"],
        "remaining_changes_truncated": remaining["truncated"],
        "remaining_changes_next_offset": remaining["next_offset"],
        "safety": {
            "explicit_file_allowlist": True,
            "preexisting_staged_changes_rejected": True,
            "hooks_disabled": True,
            "commit_signing_disabled": True,
            "git_add_dot_used": False,
        },
    }


def _push(root: Path) -> dict[str, Any]:
    repo = _repo_info(root)
    gcm = _gcm_status(root)
    if not gcm.get("available"):
        raise RuntimeError("Git Credential Manager is required for Publisher push authentication")
    branch = repo["branch"]
    completed = _run_git(
        root,
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=NUL",
        "push", "--porcelain", "--no-verify", "origin", f"HEAD:refs/heads/{branch}",
        timeout=300,
        gcm_only=True,
    )
    return {
        **repo,
        "pushed": True,
        "head": _text(_run_git(root, "rev-parse", "HEAD")),
        "git_output": _redact((completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip())[:4000],
        "safety": {
            "current_branch_only": True,
            "github_https_origin_only": True,
            "force_push": False,
            "pre_push_hooks_disabled": True,
            "credential_helper_forced_to_gcm": True,
            "interactive_prompt_disabled": True,
        },
    }


def _clean_release_tag(root: Path, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise RuntimeError("release tag must be a non-empty string")
    tag = raw.strip()
    if len(tag) > 128 or tag.startswith("-") or tag.lower().startswith("refs/"):
        raise RuntimeError("release tag is not a safe bounded tag name")
    checked = _run_git(root, "check-ref-format", f"refs/tags/{tag}", check=False)
    if checked.returncode != 0:
        raise RuntimeError("release tag is not a valid Git tag name")
    return tag


def _clean_release_title(raw: Any) -> str:
    if not isinstance(raw, str) or "\x00" in raw:
        raise RuntimeError("release title must be a string")
    title = raw.strip()
    if not title or len(title) > 256 or "\r" in title or "\n" in title:
        raise RuntimeError("release title must be one non-empty line of at most 256 characters")
    return title


def _clean_release_asset_name(raw: Any, default: str) -> str:
    name = default if raw is None else raw
    if not isinstance(name, str) or not name or name != name.strip() or len(name) > 255:
        raise RuntimeError("Release asset name must be a non-empty filename of at most 255 characters")
    if name in {".", ".."} or INVALID_RELEASE_NAME_RE.search(name) or name.endswith("."):
        raise RuntimeError("Release asset name must use GitHub-stable ASCII filename characters only")
    stem = name.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise RuntimeError("Release asset name uses a reserved Windows filename")
    return name


def _clean_release_asset_label(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip() or raw != raw.strip() or len(raw) > 128:
        raise RuntimeError("Release asset label must be a non-empty trimmed string of at most 128 characters")
    if any(char in raw for char in "\r\n\x00"):
        raise RuntimeError("Release asset label must be one line")
    return raw


def _clean_release_assets(root: Path, raw_assets: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_assets, list) or not raw_assets:
        raise RuntimeError("release assets must contain at least one explicit file")
    if len(raw_assets) > MAX_RELEASE_ASSETS:
        raise RuntimeError(f"release assets may contain at most {MAX_RELEASE_ASSETS} files")
    cleaned: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_assets:
        if not isinstance(raw, dict) or set(raw) - {"path", "name", "label"} or "path" not in raw:
            raise RuntimeError("each Release asset must contain path plus optional name/label only")
        value = raw.get("path")
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
            raise RuntimeError("Release asset paths must be non-empty POSIX-style workspace-relative strings")
        rel = PurePosixPath(value)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise RuntimeError("Release asset path must stay inside the selected workspace")
        if any(part.lower() in RELEASE_DENIED_PARTS for part in rel.parts):
            raise RuntimeError("Release asset path targets a denied VCS/dependency directory")
        basename = rel.name.lower()
        suffix = Path(rel.name).suffix.lower()
        if basename in DENIED_BASENAMES or basename.startswith(".env.") or suffix in DENIED_SUFFIXES:
            raise RuntimeError("credential/key-like files are not eligible for GitHub Releases")
        candidate = root.joinpath(*rel.parts)
        _reject_links(root, candidate)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Release asset does not resolve to a regular workspace file") from exc
        if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
            raise RuntimeError("Git Publisher Releases accept only explicit regular workspace files")
        size = resolved.stat().st_size
        if size > MAX_RELEASE_ASSET_BYTES:
            raise RuntimeError(f"Release asset exceeds the {MAX_RELEASE_ASSET_BYTES}-byte safety limit")
        normalized = resolved.relative_to(root).as_posix()
        name = _clean_release_asset_name(raw.get("name"), resolved.name)
        label = _clean_release_asset_label(raw.get("label"))
        path_key = normalized.casefold()
        name_key = name.casefold()
        if path_key in seen_paths:
            raise RuntimeError(f"duplicate Release asset path: {normalized}")
        if name_key in seen_names:
            raise RuntimeError(f"duplicate Release asset name: {name}")
        seen_paths.add(path_key)
        seen_names.add(name_key)
        cleaned.append({
            "path": normalized,
            "name": name,
            "label": label,
            "size": size,
            "sha256": _sha256_file(resolved),
        })
    return cleaned


def _remote_tag_target(root: Path, tag: str) -> tuple[str, list[str]]:
    completed = _run_git(
        root,
        "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}",
        timeout=120,
        gcm_only=True,
    )
    lines = [line for line in _text(completed).splitlines() if line.strip()]
    target = ""
    for line in lines:
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            target = sha
            break
    if not target and lines:
        target = lines[0].split("\t", 1)[0]
    return target, lines


def _ensure_generic_release_tag(root: Path, repo: dict[str, str], tag: str, title: str) -> str:
    if _staged_paths(root):
        raise RuntimeError("Release publishing requires no staged changes")
    tracked = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=no")
    if tracked.stdout.strip():
        raise RuntimeError("Release publishing requires a clean tracked working tree; explicit untracked assets are allowed")
    head = _text(_run_git(root, "rev-parse", "HEAD"))
    branch = repo["branch"]
    remote = _run_git(root, "ls-remote", "origin", f"refs/heads/{branch}", timeout=120, gcm_only=True)
    remote_lines = _text(remote).splitlines()
    remote_head = remote_lines[0].split("\t", 1)[0] if remote_lines else ""
    if remote_head != head:
        raise RuntimeError(f"origin/{branch} does not match current HEAD; push the release commit first")

    local_tag = _run_git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{}}", check=False)
    if local_tag.returncode == 0 and _text(local_tag) != head:
        raise RuntimeError(f"local tag {tag} already points to a different commit")
    remote_target, remote_lines = _remote_tag_target(root, tag)
    if remote_target and remote_target != head:
        raise RuntimeError(f"remote tag {tag} already points to a different commit")
    if not remote_lines:
        if local_tag.returncode != 0:
            _run_git(
                root,
                "-c", "core.hooksPath=NUL",
                "-c", "tag.gpgSign=false",
                "tag", "-a", tag, "-m", title, head,
            )
        _run_git(
            root,
            "-c", "core.hooksPath=NUL",
            "push", "--porcelain", "--no-verify", "origin", f"refs/tags/{tag}:refs/tags/{tag}",
            timeout=300,
            gcm_only=True,
        )
    verified_target, _ = _remote_tag_target(root, tag)
    if verified_target != head:
        raise RuntimeError("remote Release tag verification failed after push")
    return head


def _prepare_release_upload_paths(root: Path, assets: list[dict[str, Any]], temp_root: Path) -> list[str]:
    uploads: list[str] = []
    for asset in assets:
        source = root.joinpath(*PurePosixPath(asset["path"]).parts)
        _reject_links(root, source)
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Release asset changed or escaped after validation") from exc
        if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
            raise RuntimeError("Release asset changed after validation")
        if resolved.stat().st_size != asset["size"] or _sha256_file(resolved) != asset["sha256"]:
            raise RuntimeError(f"Release asset changed after validation: {asset['path']}")
        target = temp_root / asset["name"]
        shutil.copyfile(resolved, target)
        if target.stat().st_size != asset["size"] or _sha256_file(target) != asset["sha256"]:
            raise RuntimeError(f"temporary Release asset snapshot verification failed: {asset['name']}")
        uploads.append(str(target) + ("#" + asset["label"] if asset.get("label") else ""))
    return uploads


def _verify_release_latest(
    root: Path,
    repo_name: str,
    tag: str,
    latest: bool,
    token: str,
) -> None:
    completed = _run_gh(
        root,
        "repo", "view", repo_name, "--json", "latestRelease",
        token=token,
    )
    try:
        repository = json.loads(_text(completed))
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Release latest-state verification returned invalid JSON") from exc
    if not isinstance(repository, dict):
        raise RuntimeError("GitHub Release latest-state verification returned an invalid repository object")
    latest_release = repository.get("latestRelease")
    if latest_release is None:
        latest_tag = None
    elif isinstance(latest_release, dict) and isinstance(latest_release.get("tagName"), str):
        latest_tag = latest_release["tagName"]
    else:
        raise RuntimeError("GitHub Release latest-state verification returned an invalid latestRelease object")
    is_latest = latest_tag == tag
    if is_latest is not latest:
        raise RuntimeError(
            f"GitHub Release latest-state mismatch for {tag}: expected {latest}, got {is_latest}"
        )


def _release_assets(
    root: Path,
    raw_tag: Any,
    raw_title: Any,
    raw_assets: Any,
    *,
    latest: Any = True,
) -> dict[str, Any]:
    repo = _repo_info(root)
    if not isinstance(latest, bool):
        raise RuntimeError("latest must be a boolean")
    tag = _clean_release_tag(root, raw_tag)
    title = _clean_release_title(raw_title)
    assets = _clean_release_assets(root, raw_assets)
    token = _github_token_from_gcm(root)
    repo_name = f"{repo['owner']}/{repo['repo']}"

    with tempfile.TemporaryDirectory(prefix="folderbridge-release-") as temp_dir:
        uploads = _prepare_release_upload_paths(root, assets, Path(temp_dir))
        head = _ensure_generic_release_tag(root, repo, tag, title)
        existing = _run_gh(root, "release", "view", tag, "--repo", repo_name, token=token, check=False)
        if existing.returncode == 0:
            _run_gh(
                root,
                "release", "upload", tag, *uploads, "--clobber", "--repo", repo_name,
                token=token,
                timeout=RELEASE_GH_TIMEOUT_SECONDS,
            )
            edit_args = [
                "release", "edit", tag, "--title", title,
                "--latest" if latest else "--latest=false",
                "--repo", repo_name,
            ]
            _run_gh(root, *edit_args, token=token)
        else:
            create_args = [
                "release", "create", tag, *uploads,
                "--verify-tag", "--title", title, "--notes", "",
                "--latest" if latest else "--latest=false",
                "--repo", repo_name,
            ]
            _run_gh(root, *create_args, token=token, timeout=RELEASE_GH_TIMEOUT_SECONDS)

    verified = _run_gh(
        root,
        "release", "view", tag, "--repo", repo_name, "--json", "tagName,url,assets",
        token=token,
    )
    try:
        release = json.loads(_text(verified))
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Release verification returned invalid JSON") from exc
    if release.get("tagName") != tag or not isinstance(release.get("url"), str) or not release.get("url"):
        raise RuntimeError("GitHub Release tag/url verification failed")
    remote_assets = release.get("assets")
    if not isinstance(remote_assets, list):
        raise RuntimeError("GitHub Release asset verification returned an invalid asset list")
    by_name = {str(item.get("name", "")): item for item in remote_assets if isinstance(item, dict)}
    for asset in assets:
        remote_asset = by_name.get(asset["name"])
        if remote_asset is None:
            raise RuntimeError(f"GitHub Release asset missing after upload: {asset['name']}")
        if remote_asset.get("size") != asset["size"]:
            raise RuntimeError(f"GitHub Release asset size mismatch after upload: {asset['name']}")
        if asset.get("label") is not None and remote_asset.get("label") != asset["label"]:
            raise RuntimeError(f"GitHub Release asset label mismatch after upload: {asset['name']}")
    _verify_release_latest(root, repo_name, tag, latest, token)
    remote_target, _ = _remote_tag_target(root, tag)
    if remote_target != head:
        raise RuntimeError("GitHub Release tag moved away from current HEAD during publication")
    return {
        **repo,
        "released": True,
        "tag": tag,
        "title": title,
        "head": head,
        "latest_requested": latest,
        "url": release["url"],
        "assets": assets,
        "safety": {
            "current_workspace_repo_only": True,
            "current_head_only": True,
            "origin_current_branch_must_match_head": True,
            "explicit_release_asset_allowlist": True,
            "tracked_worktree_clean": True,
            "untracked_explicit_assets_allowed": True,
            "github_https_origin_only": True,
            "credential_manager_only": True,
            "token_input_exposed_to_model": False,
            "force_push": False,
        },
    }


def _project_release_version(root: Path) -> str:
    path = root / "pyproject.toml"
    _reject_links(root, path)
    try:
        if path.stat().st_size > 64 * 1024:
            raise RuntimeError("pyproject.toml is unexpectedly large")
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"could not read project version from pyproject.toml: {exc}") from exc
    in_project = False
    versions: list[str] = []
    for line in text.splitlines():
        section = re.fullmatch(r"\s*\[([^\]]+)\]\s*(?:#.*)?", line)
        if section:
            in_project = section.group(1).strip() == "project"
            continue
        if not in_project:
            continue
        match = re.fullmatch(r'\s*version\s*=\s*"(\d+\.\d+\.\d+)"\s*(?:#.*)?', line)
        if match:
            versions.append(match.group(1))
    if len(versions) != 1:
        raise RuntimeError("pyproject.toml [project] must declare exactly one stable numeric version = \"x.y.z\"")
    return versions[0]


def _release_asset_paths(root: Path) -> tuple[Path, Path]:
    release_dir = root / "release" / "windows-x64"
    exe = release_dir / "FolderBridge.exe"
    checksum = release_dir / "FolderBridge.exe.sha256"
    for path in (exe, checksum):
        _reject_links(root, path)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"required Release asset is missing or unsafe: {path.relative_to(root).as_posix()}") from exc
        if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
            raise RuntimeError(f"required Release asset is not a regular file: {path.relative_to(root).as_posix()}")
    return exe, checksum


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _release(root: Path) -> dict[str, Any]:
    repo = _repo_info(root)
    if repo["branch"] != "main":
        raise RuntimeError("Release publishing is locked to the main branch")
    if _staged_paths(root):
        raise RuntimeError("Release publishing requires no staged changes")
    tracked = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=no")
    if tracked.stdout.strip():
        raise RuntimeError("Release publishing requires a clean tracked working tree; untracked local files are allowed")

    version = _project_release_version(root)
    tag = f"v{version}"
    head = _text(_run_git(root, "rev-parse", "HEAD"))
    title = _text(_run_git(root, "log", "-1", "--pretty=%s"))
    expected_title = f"Release FolderBridge {version}"
    if title != expected_title:
        raise RuntimeError(f"HEAD commit title must be exactly '{expected_title}'")

    remote = _run_git(root, "ls-remote", "origin", "refs/heads/main", timeout=120, gcm_only=True)
    remote_line = _text(remote).splitlines()
    remote_head = remote_line[0].split("\t", 1)[0] if remote_line else ""
    if remote_head != head:
        raise RuntimeError("origin/main does not match current HEAD; push the release commit first")

    exe, checksum = _release_asset_paths(root)
    actual_sha = _sha256_file(exe)
    try:
        checksum_text = checksum.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"could not read Release checksum file: {exc}") from exc
    declared_sha = checksum_text.split()[0].lower() if checksum_text else ""
    if declared_sha != actual_sha:
        raise RuntimeError("FolderBridge.exe.sha256 does not match FolderBridge.exe")

    local_tag = _run_git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{}}", check=False)
    if local_tag.returncode == 0 and _text(local_tag) != head:
        raise RuntimeError(f"local tag {tag} already points to a different commit")

    remote_tags = _run_git(
        root,
        "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}",
        timeout=120,
        gcm_only=True,
    )
    lines = [line for line in _text(remote_tags).splitlines() if line.strip()]
    remote_tag_target = ""
    for line in lines:
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            remote_tag_target = sha
            break
    if not remote_tag_target and lines:
        remote_tag_target = lines[0].split("\t", 1)[0]
    if remote_tag_target and remote_tag_target != head:
        raise RuntimeError(f"remote tag {tag} already points to a different commit")

    if not lines:
        if local_tag.returncode != 0:
            _run_git(
                root,
                "-c", "core.hooksPath=NUL",
                "-c", "tag.gpgSign=false",
                "tag", "-a", tag, "-m", f"FolderBridge {version}", head,
            )
        _run_git(
            root,
            "-c", "core.hooksPath=NUL",
            "push", "--porcelain", "--no-verify", "origin", f"refs/tags/{tag}:refs/tags/{tag}",
            timeout=300,
            gcm_only=True,
        )

    token = _github_token_from_gcm(root)
    repo_name = f"{repo['owner']}/{repo['repo']}"
    existing = _run_gh(root, "release", "view", tag, "--repo", repo_name, token=token, check=False)
    if existing.returncode == 0:
        _run_gh(
            root,
            "release", "upload", tag, str(exe), str(checksum), "--clobber", "--repo", repo_name,
            token=token,
            timeout=300,
        )
        _run_gh(
            root,
            "release", "edit", tag, "--title", f"FolderBridge {version}", "--latest", "--repo", repo_name,
            token=token,
        )
    else:
        _run_gh(
            root,
            "release", "create", tag, str(exe), str(checksum),
            "--verify-tag", "--generate-notes", "--title", f"FolderBridge {version}", "--latest", "--repo", repo_name,
            token=token,
            timeout=300,
        )
    verified = _run_gh(
        root,
        "release", "view", tag, "--repo", repo_name, "--json", "tagName,url",
        token=token,
    )
    return {
        **repo,
        "released": True,
        "version": version,
        "tag": tag,
        "head": head,
        "exe_sha256": actual_sha,
        "release": _text(verified),
        "assets": ["release/windows-x64/FolderBridge.exe", "release/windows-x64/FolderBridge.exe.sha256"],
        "safety": {
            "main_branch_only": True,
            "version_from_pyproject_only": True,
            "tag_from_version_only": True,
            "current_head_only": True,
            "origin_main_must_match_head": True,
            "fixed_release_assets_only": True,
            "untracked_local_files_ignored": True,
            "force_push": False,
        },
    }
