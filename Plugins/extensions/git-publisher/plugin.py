from __future__ import annotations

import base64
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
from urllib.parse import quote, urlsplit

from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree


MAX_OUTPUT = 32_000
MAX_COMMIT_FILE_BYTES = 100 * 1024 * 1024
MAX_COMMIT_PATHS = 128
MAX_REMOTE_FILE_BYTES = 6 * 1024 * 1024
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
REPO_DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
}
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
    workspace_root = _workspace_root(context)
    if action in {"remote-update-file", "remote-release-assets", "replace-existing-release-assets"}:
        if bool(context.get("workspace_read_only")):
            raise RuntimeError("FolderBridge is read-only; remote GitHub publishing is unavailable")
        if action == "remote-update-file":
            return {"repo_path": None, **_remote_update_file(workspace_root, params)}
        if action == "replace-existing-release-assets":
            return {"repo_path": None, **_replace_existing_release_assets(workspace_root, params)}
        return {"repo_path": None, **_remote_release_assets(workspace_root, params)}
    if action in {"first-publication-preflight", "init-local-repository", "create-github-repository", "initial-commit"} and "repo_path" not in params:
        raise RuntimeError("first publication requires an explicitly selected repo_path")
    root, repo_path = _repo_root(workspace_root, params.get("repo_path"))
    if action == "first-publication-preflight":
        result = _first_publication_preflight(root)
    elif action == "status":
        result = _status(
            root,
            offset=params.get("offset", 0),
            limit=params.get("limit", DEFAULT_STATUS_PAGE),
        )
    else:
        if bool(context.get("workspace_read_only")):
            raise RuntimeError("FolderBridge is read-only; Git Publisher mutations are unavailable.")
        if action == "init-local-repository":
            result = _init_local_repository(root)
        elif action == "create-github-repository":
            result = _create_github_repository(root, params.get("owner"), params.get("name"), params.get("visibility"), params.get("description", ""))
        elif action == "initial-commit":
            result = _commit(root, params.get("paths"), params.get("message"), initial=True)
        elif action == "connect":
            result = _connect(root, username=params.get("username"), force=bool(params.get("force", False)))
        elif action == "commit":
            result = _commit(root, params.get("paths"), params.get("message"))
        elif action == "push":
            result = _push(root)
        elif action == "release-assets":
            result = _release_assets(
                root,
                params.get("tag"),
                params.get("title"),
                params.get("assets"),
                latest=params.get("latest", True),
            )
        else:
            raise RuntimeError(f"unsupported action: {action}")
    return {"repo_path": repo_path, **result}


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _repo_root(workspace_root: Path, raw_repo_path: Any) -> tuple[Path, str]:
    raw = "." if raw_repo_path is None else raw_repo_path
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw or raw != raw.strip():
        raise RuntimeError("repo_path must be a non-empty trimmed POSIX-style workspace-relative path")
    if raw == ".":
        candidate = workspace_root
        normalized = "."
    else:
        rel = PurePosixPath(raw)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise RuntimeError("repo_path must stay inside the selected workspace")
        if any(part.lower() in REPO_DENIED_PARTS for part in rel.parts):
            raise RuntimeError("repo_path targets a denied VCS/dependency directory")
        candidate = workspace_root.joinpath(*rel.parts)
        normalized = rel.as_posix()
    _reject_links(workspace_root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("repo_path does not resolve to a directory inside the selected workspace") from exc
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise RuntimeError("repo_path must resolve to a regular directory inside the selected workspace")
    return resolved, normalized


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
            raise RuntimeError("selected repo_path must itself be the Git repository root")
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
    origin_check = _run_git(root, "remote", "get-url", "origin", check=False)
    if origin_check.returncode == 0:
        repo = _repo_info(root)
    else:
        # New first-publication repositories do not have origin yet. Only the
        # exact Publisher-initialized, unborn/main Git root can use this seam.
        state = _read_first_publication_state(root)
        if state["phase"] not in {"local_initialized", "remote_request_pending"}:
            raise RuntimeError("missing origin on a repository that should already be bound")
        if Path(_text(_run_git(root, "rev-parse", "--show-toplevel"))).resolve(strict=True) != root:
            raise RuntimeError("first-publication authentication requires the selected Git root")
        if _text(_run_git(root, "symbolic-ref", "--short", "HEAD")) != "main":
            raise RuntimeError("first-publication authentication requires main")
        _reject_unsafe_local_git_config(root)
        repo = {"branch": "main", "origin": None, "owner": None, "repo": None}
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


def _commit(root: Path, raw_paths: Any, raw_message: Any, *, initial: bool = False) -> dict[str, Any]:
    repo = _repo_info(root)
    current_head = _run_git(root, "rev-parse", "--verify", "HEAD", check=False)
    if initial:
        state = _read_first_publication_state(root)
        if state.get("phase") == "initial_committed" and current_head.returncode == 0:
            actual = _text(current_head)
            if actual != state.get("head"):
                raise RuntimeError("existing initial-commit HEAD differs from the recorded publication state")
            if raw_paths != state.get("paths") or raw_message != state.get("message"):
                raise RuntimeError("retry cannot change the already committed file allowlist or message")
            return {**repo, "commit": actual, "already_committed": True}
        if state.get("phase") != "remote_created" or current_head.returncode == 0:
            raise RuntimeError("initial-commit requires an initialized first-publication repository with unborn main and a verified remote")
        if repo["branch"] != "main":
            raise RuntimeError("initial-commit only supports main")
    elif current_head.returncode != 0:
        raise RuntimeError("commit requires an existing HEAD; use initial-commit for first publication")
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
        if initial:
            _assert_staged_bytes_match_worktree(root, existing_paths)
        completed = _run_git(
            root,
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=NUL",
            "-c", "commit.gpgSign=false",
            "commit", "--no-gpg-sign", "--no-verify", "-m", message,
            timeout=120,
        )
    except Exception:
        if initial:
            _run_git(root, "rm", "--cached", "-q", "--ignore-unmatch", "--", *paths, check=False)
        else:
            _rollback_stage(root, paths)
        raise
    commit_sha = _text(_run_git(root, "rev-parse", "HEAD"))
    if initial:
        next_state = {**state, "phase": "initial_committed", "head": commit_sha, "paths": paths, "message": message}
        _write_first_publication_state(root, next_state)
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
    head = _text(_run_git(root, "rev-parse", "HEAD"))
    remote_check = _run_git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", timeout=120, gcm_only=True)
    remote_sha = _text(remote_check).split("\t", 1)[0] if remote_check.stdout else ""
    if remote_sha != head:
        raise RuntimeError("GitHub branch SHA differs from local HEAD after push")
    return {
        **repo,
        "pushed": True,
        "head": head,
        "verified_remote_head": remote_sha,
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

    with tempfile.TemporaryDirectory(prefix="git-publisher-release-") as temp_dir:
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



# Existing remote repository publication WITHOUT changing or creating a local Git repository.
# The source is a regular file in the selected FolderBridge workspace; the remote
# is explicitly identified by owner/repository, stable GitHub repository ID, branch
# and expected branch HEAD. Only one existing remote file can be replaced per call.
REMOTE_SHA40 = re.compile(r"^[0-9a-f]{40}$")
REMOTE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _remote_expected_sha(raw: Any, *, name: str, size: int) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{" + str(size) + r"}", raw):
        raise RuntimeError(f"{name} must be an exact lowercase {size}-character hexadecimal digest")
    return raw


def _remote_source(root: Path, raw_path: Any, raw_sha256: Any) -> dict[str, Any]:
    # Reuse the established workspace confinement, link/reparse denial, secret
    # filename filtering, file stability and size/hash checks of Release assets.
    expected = _remote_expected_sha(raw_sha256, name="source SHA256", size=64)
    item = _clean_release_assets(root, [{"path": raw_path, "name": "Remote-Source.bin"}])[0]
    if item["size"] > MAX_REMOTE_FILE_BYTES:
        raise RuntimeError(f"remote Contents API source exceeds {MAX_REMOTE_FILE_BYTES} bytes")
    if item["sha256"] != expected:
        raise RuntimeError("remote source SHA256 differs from the explicitly approved source")
    return item


def _git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw).hexdigest()


def _remote_target_path(raw: Any) -> str:
    if (
        not isinstance(raw, str) or not raw or len(raw) > 1024
        or "\x00" in raw or "\\" in raw or raw != raw.strip()
    ):
        raise RuntimeError("remote path must be a non-empty bounded POSIX path")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.as_posix() != raw:
        raise RuntimeError("remote path is not a normalized existing-file path")
    if any(part.lower() in REPO_DENIED_PARTS for part in rel.parts):
        raise RuntimeError("remote path cannot target Git/dependency internals")
    basename = rel.name.lower()
    if (basename in DENIED_BASENAMES or basename.startswith(".env.")
            or Path(basename).suffix.lower() in DENIED_SUFFIXES):
        raise RuntimeError("remote path cannot target a credential-like file")
    return raw


def _remote_identity(root: Path, raw: dict[str, Any], token: str) -> dict[str, Any]:
    owner = raw.get("owner")
    repo = raw.get("repo")
    branch = raw.get("branch")
    repo_id = raw.get("repo_id")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
        raise RuntimeError("remote owner must be one explicit GitHub personal-account login")
    if (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo)
            or repo in {".", ".."} or repo.endswith(".git")):
        raise RuntimeError("remote repository name is invalid")
    if not isinstance(repo_id, int) or isinstance(repo_id, bool) or repo_id < 1:
        raise RuntimeError("remote repository identity requires a positive GitHub repo_id")
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise RuntimeError("remote branch name is invalid")
    if _run_git(root, "check-ref-format", f"refs/heads/{branch}", check=False).returncode != 0:
        raise RuntimeError("remote branch name is not a valid Git ref")
    _validate_origin(f"https://github.com/{owner}/{repo}.git")
    full_name = f"{owner}/{repo}"
    who = _json_gh_response(_run_gh(root, "api", "user", token=token, timeout=60), "GitHub authenticated user")
    if str(who.get("login", "")).casefold() != owner.casefold():
        raise RuntimeError("remote publishing requires the authenticated personal account to match the owner")
    info = _json_gh_response(
        _run_gh(root, "api", f"repos/{full_name}", token=token, timeout=60),
        "GitHub remote repository",
    )
    returned_owner = info.get("owner")
    if (type(info.get("id")) is not int or info["id"] != repo_id
            or not isinstance(returned_owner, dict)
            or str(returned_owner.get("login", "")).casefold() != owner.casefold()
            or str(info.get("full_name", "")).casefold() != full_name.casefold()):
        raise RuntimeError("GitHub remote repository identity does not match approved owner/name/repo_id")
    if info.get("archived") or info.get("disabled") or not isinstance(info.get("permissions"), dict) or info["permissions"].get("push") is not True:
        raise RuntimeError("GitHub remote repository is archived/disabled or the credential has no push permission")
    head_data = _json_gh_response(
        _run_gh(root, "api", f"repos/{full_name}/branches/{quote(branch, safe='')}", token=token, timeout=60),
        "GitHub remote branch",
    )
    commit_info = head_data.get("commit")
    head = commit_info.get("sha") if isinstance(commit_info, dict) else None
    if not isinstance(head, str) or not REMOTE_SHA40.fullmatch(head):
        raise RuntimeError("GitHub remote branch did not return a valid commit SHA")
    return {"owner": owner, "repo": repo, "repo_id": repo_id, "full_name": full_name, "branch": branch, "head": head}


def _remote_file_info(root: Path, identity: dict[str, Any], remote_path: str, token: str) -> dict[str, Any]:
    endpoint = (
        f"repos/{identity['full_name']}/contents/{quote(remote_path, safe='/')}"
        f"?ref={quote(identity['branch'], safe='')}"
    )
    info = _json_gh_response(_run_gh(root, "api", endpoint, token=token, timeout=90), "GitHub existing file")
    if info.get("type") != "file" or not isinstance(info.get("sha"), str) or not REMOTE_SHA40.fullmatch(info["sha"]):
        raise RuntimeError("remote target must already be a regular GitHub file with a blob SHA")
    return {"sha": info["sha"], "endpoint": f"repos/{identity['full_name']}/contents/{quote(remote_path, safe='/')}"}


def _remote_update_file(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    source = _remote_source(root, params.get("source_path"), params.get("source_sha256"))
    target = _remote_target_path(params.get("remote_path"))
    expected_blob = _remote_expected_sha(params.get("expected_blob_sha"), name="expected blob SHA", size=40)
    expected_head = _remote_expected_sha(params.get("expected_branch_sha"), name="expected branch SHA", size=40)
    message = params.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 1000 or any(x in message for x in "\r\n\x00"):
        raise RuntimeError("remote commit message must be one non-empty line of at most 1000 characters")
    source_path = root.joinpath(*PurePosixPath(source["path"]).parts)
    snapshot = source_path.read_bytes()
    if len(snapshot) != source["size"] or hashlib.sha256(snapshot).hexdigest() != source["sha256"]:
        raise RuntimeError("remote source bytes changed after preflight; refusing publication")
    intended_blob = _git_blob_sha(snapshot)
    token = _github_token_from_gcm(root)
    identity = _remote_identity(root, params, token)
    remote = _remote_file_info(root, identity, target, token)

    # An exact-content no-op is safe on retry after an ambiguous HTTP response.
    # It is not claimed as proof that this plugin authored the existing commit.
    if remote["sha"] == intended_blob:
        return {**identity, "updated": False, "already_current": True, "remote_path": target,
                "source_sha256": source["sha256"], "blob_sha": intended_blob}
    if remote["sha"] != expected_blob:
        raise RuntimeError("remote file blob SHA changed: another publisher updated the target; refusing overwrite")
    if identity["head"] != expected_head:
        raise RuntimeError("remote branch SHA changed since approval; inspect new HEAD before publication")

    payload = {
        "message": message.strip(), "content": base64.b64encode(snapshot).decode("ascii"),
        "sha": expected_blob, "branch": identity["branch"],
    }
    with tempfile.TemporaryDirectory(prefix="git-publisher-remote-write-") as temporary:
        request_file = Path(temporary) / "request.json"
        request_file.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        update = _json_gh_response(
            _run_gh(root, "api", "-X", "PUT", remote["endpoint"], "--input", str(request_file),
                    token=token, timeout=120),
            "GitHub remote file commit",
        )
    content = update.get("content")
    commit = update.get("commit")
    commit_sha = commit.get("sha") if isinstance(commit, dict) else None
    if (not isinstance(content, dict) or content.get("sha") != intended_blob
            or not isinstance(commit_sha, str) or not REMOTE_SHA40.fullmatch(commit_sha)):
        raise RuntimeError("GitHub remote file update returned no matching blob/commit; inspect before retry")
    verified = _remote_file_info(root, identity, target, token)
    if verified["sha"] != intended_blob:
        raise RuntimeError("GitHub remote file readback does not match the published Git blob")
    return {**identity, "updated": True, "already_current": False, "remote_path": target,
            "source_sha256": source["sha256"], "blob_sha": intended_blob, "commit": commit_sha,
            "safety": {"existing_file_only": True, "expected_old_blob": True,
                       "expected_branch_head": True, "expected_repo_id": True,
                       "credential_manager_only": True, "no_local_git_repository": True}}


def _gh_not_found(result: subprocess.CompletedProcess[bytes]) -> bool:
    if result.returncode == 0:
        return False
    message = (result.stderr + result.stdout).decode("utf-8", errors="replace")
    return bool(re.search(r"(?:\b404\b|\bnot found\b)", message, re.IGNORECASE))


def _remote_tag_sha(root: Path, identity: dict[str, Any], tag: str, token: str) -> str | None:
    endpoint = f"repos/{identity['full_name']}/git/ref/tags/{quote(tag, safe='')}"
    response = _run_gh(root, "api", endpoint, token=token, check=False, timeout=60)
    if _gh_not_found(response):
        return None
    if response.returncode != 0:
        raise RuntimeError("GitHub remote tag inspection failed: " + _redact(response.stderr.decode("utf-8", errors="replace")))
    record = _json_gh_response(response, "GitHub remote tag")
    obj = record.get("object")
    if not isinstance(obj, dict) or obj.get("type") not in {"commit", "tag"}:
        raise RuntimeError("remote tag has an unsupported target")
    if obj.get("type") == "commit":
        sha = obj.get("sha")
    else:
        tag_sha = obj.get("sha")
        if not isinstance(tag_sha, str) or not REMOTE_SHA40.fullmatch(tag_sha):
            raise RuntimeError("remote annotated tag object SHA is invalid")
        tag_record = _json_gh_response(
            _run_gh(root, "api", f"repos/{identity['full_name']}/git/tags/{tag_sha}",
                    token=token, timeout=60),
            "GitHub annotated tag",
        )
        tag_obj = tag_record.get("object")
        if not isinstance(tag_obj, dict) or tag_obj.get("type") != "commit":
            raise RuntimeError("nested annotated tags require independent review")
        sha = tag_obj.get("sha")
    if not isinstance(sha, str) or not REMOTE_SHA40.fullmatch(sha):
        raise RuntimeError("remote tag commit SHA is invalid")
    return sha


def _replace_existing_release_assets(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Replace only pinned existing Release assets without moving/deleting the tag or Release.

    This is intentionally separate from first-time remote-release-assets. Every old
    asset has an exact ID and digest lease; a prior partial retry may be accepted
    only when its new digest and expected label already match.
    """
    raw_assets = params.get("assets")
    if not isinstance(raw_assets, list) or not 1 <= len(raw_assets) <= MAX_RELEASE_ASSETS:
        raise RuntimeError("existing Release replacement requires 1-64 explicit assets")
    cleaned_inputs = []
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise RuntimeError("existing Release assets must be exact objects")
        if "name" not in raw:
            raise RuntimeError("existing Release replacement requires each existing asset name")
        _remote_expected_sha(raw.get("sha256"), name="new Release asset SHA256", size=64)
        _remote_expected_sha(raw.get("expected_asset_sha256"), name="old Release asset SHA256", size=64)
        if type(raw.get("expected_asset_id")) is not int or raw["expected_asset_id"] < 1:
            raise RuntimeError("existing Release replacement requires a positive old asset ID")
        cleaned_inputs.append({k: raw[k] for k in ("path", "name", "label") if k in raw})

    assets = _clean_release_assets(root, cleaned_inputs)
    for raw, asset in zip(raw_assets, assets):
        if raw["sha256"] != asset["sha256"]:
            raise RuntimeError("new Release source SHA256 changed: " + asset["path"])

    expected_head = _remote_expected_sha(
        params.get("expected_branch_sha"), name="expected branch SHA", size=40,
    )
    expected_tag = _remote_expected_sha(
        params.get("expected_tag_commit_sha"), name="expected existing tag commit SHA", size=40,
    )
    release_id = params.get("expected_release_id")
    if type(release_id) is not int or release_id < 1:
        raise RuntimeError("existing Release replacement requires the exact positive Release ID")
    tag = _clean_release_tag(root, params.get("tag"))
    token = _github_token_from_gcm(root)
    identity = _remote_identity(root, params, token)
    if identity["head"] != expected_head:
        raise RuntimeError("remote branch HEAD changed; refusing existing Release replacement")
    if _remote_tag_sha(root, identity, tag, token) != expected_tag:
        raise RuntimeError("existing Release tag commit SHA changed; no tag rewrite is permitted")

    def fetch_release() -> dict[str, Any]:
        record = _json_gh_response(
            _run_gh(
                root, "api",
                f"repos/{identity['full_name']}/releases/tags/{quote(tag, safe='')}",
                token=token, timeout=60,
            ),
            "GitHub existing Release",
        )
        if (
            type(record.get("id")) is not int or record["id"] != release_id
            or record.get("tag_name") != tag
            or record.get("draft") is not False
        ):
            raise RuntimeError("existing Release identity/tag or published state changed")
        if not isinstance(record.get("assets"), list):
            raise RuntimeError("GitHub existing Release has no verifiable asset inventory")
        inventory = record["assets"]
        names = [item.get("name") for item in inventory if isinstance(item, dict)]
        if len(names) != len(inventory) or len(set(names)) != len(names):
            raise RuntimeError("existing Release asset inventory contains invalid/duplicate names")
        return record

    original_release = fetch_release()
    original_assets = {item["name"]: item for item in original_release["assets"]}
    original_header = {
        key: original_release.get(key)
        for key in ("name", "draft", "prerelease", "target_commitish")
    }
    explicit_names = {asset["name"] for asset in assets}
    untouched_assets = {
        name: (item.get("id"), item.get("size"), item.get("digest"), item.get("label"))
        for name, item in original_assets.items() if name not in explicit_names
    }

    def examine(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        inventory = {item["name"]: item for item in record["assets"]}
        pending = []
        already = []
        for raw, asset in zip(raw_assets, assets):
            remote = inventory.get(asset["name"])
            if not isinstance(remote, dict):
                raise RuntimeError("expected existing Release asset is missing: " + asset["name"])
            digest = remote.get("digest")
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise RuntimeError("old Release asset lacks a verified SHA256 digest: " + asset["name"])
            old = "sha256:" + raw["expected_asset_sha256"]
            target = "sha256:" + asset["sha256"]
            label_ok = asset.get("label") is None or remote.get("label") == asset["label"]
            if digest == target and remote.get("size") == asset["size"] and label_ok:
                already.append(asset["name"])
            elif (digest == old and type(remote.get("id")) is int
                  and remote["id"] == raw["expected_asset_id"]):
                pending.append(asset)
            else:
                raise RuntimeError("old Release asset ID/SHA changed; refusing replacement: " + asset["name"])
        return pending, already

    pending, already = examine(original_release)
    if pending:
        with tempfile.TemporaryDirectory(prefix="git-publisher-existing-release-") as directory:
            uploads = _prepare_release_upload_paths(root, pending, Path(directory))
            # Recheck leases after snapshot and immediately before any destructive --clobber.
            if _remote_tag_sha(root, identity, tag, token) != expected_tag:
                raise RuntimeError("Release tag changed before upload")
            if _remote_identity(root, params, token)["head"] != expected_head:
                raise RuntimeError("branch head changed before Release upload")
            latest = fetch_release()
            if latest["id"] != release_id:
                raise RuntimeError("existing Release identity changed before upload")
            rerun_pending, _ = examine(latest)
            if [x["name"] for x in rerun_pending] != [x["name"] for x in pending]:
                raise RuntimeError("Release assets changed between snapshot and upload; retry after inspection")
            _run_gh(
                root, "release", "upload", tag, *uploads, "--clobber",
                "--repo", identity["full_name"],
                token=token, timeout=RELEASE_GH_TIMEOUT_SECONDS,
            )

    final_release = fetch_release()
    if any(final_release.get(key) != val for key, val in original_header.items()):
        raise RuntimeError("existing Release metadata unexpectedly changed")
    final_assets = {item["name"]: item for item in final_release["assets"]}
    for name, reference in untouched_assets.items():
        item = final_assets.get(name)
        if item is None or (item.get("id"), item.get("size"), item.get("digest"), item.get("label")) != reference:
            raise RuntimeError("an unspecified existing Release asset changed: " + name)
    for asset in assets:
        remote = final_assets.get(asset["name"])
        if remote is None or remote.get("size") != asset["size"]:
            raise RuntimeError("Release asset size/readback mismatch: " + asset["name"])
        if remote.get("digest") != "sha256:" + asset["sha256"]:
            raise RuntimeError("Release asset SHA256/readback mismatch: " + asset["name"])
        if asset.get("label") is not None and remote.get("label") != asset["label"]:
            raise RuntimeError("Release asset label/readback mismatch: " + asset["name"])
    if _remote_tag_sha(root, identity, tag, token) != expected_tag:
        raise RuntimeError("existing Release tag moved during asset replacement")
    if _remote_identity(root, params, token)["head"] != expected_head:
        raise RuntimeError("remote branch moved during existing Release replacement")
    return {
        **identity, "tag": tag, "release_id": release_id, "expected_tag_commit_sha": expected_tag,
        "replaced": [asset["name"] for asset in pending],
        "already_current": already,
        "assets": assets, "url": final_release.get("html_url"),
        "safety": {
            "existing_release_id_pinned": True, "old_asset_id_and_sha_pinned": True,
            "new_asset_sha_pinned": True, "tag_rewrite": False, "release_deletion": False,
            "no_local_git_repository": True, "credential_manager_only": True,
            "unlisted_assets_preserved": True,
        },
    }


def _remote_release_assets(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    raw_assets = params.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets or len(raw_assets) > MAX_RELEASE_ASSETS:
        raise RuntimeError("remote Release requires an explicit bounded asset list")
    for raw in raw_assets:
        if not isinstance(raw, dict) or "sha256" not in raw:
            raise RuntimeError("every remote Release asset requires a source SHA256")
        _remote_expected_sha(raw["sha256"], name="Release asset SHA256", size=64)
    stripped = [{k: v for k, v in value.items() if k != "sha256"} for value in raw_assets]
    assets = _clean_release_assets(root, stripped)
    for raw, asset in zip(raw_assets, assets):
        if raw["sha256"] != asset["sha256"]:
            raise RuntimeError(f"remote Release asset SHA256 changed: {asset['path']}")
    expected_head = _remote_expected_sha(params.get("expected_branch_sha"), name="expected branch SHA", size=40)
    latest = params.get("latest", True)
    if not isinstance(latest, bool):
        raise RuntimeError("remote Release latest flag must be a boolean")
    tag = _clean_release_tag(root, params.get("tag"))
    title = _clean_release_title(params.get("title"))
    token = _github_token_from_gcm(root)
    identity = _remote_identity(root, params, token)
    if identity["head"] != expected_head:
        raise RuntimeError("remote branch SHA changed since Release approval; refusing to create a tag")
    existing_tag = _remote_tag_sha(root, identity, tag, token)
    if existing_tag is not None and existing_tag != expected_head:
        raise RuntimeError("remote Release tag already targets a different commit")
    current_release = _run_gh(root, "release", "view", tag, "--repo", identity["full_name"],
                              token=token, check=False, timeout=60)
    if current_release.returncode == 0:
        raise RuntimeError("remote Release already exists: refusing to overwrite or re-upload assets")
    if not _gh_not_found(current_release):
        raise RuntimeError("remote Release preflight could not distinguish absent Release from an error")

    with tempfile.TemporaryDirectory(prefix="git-publisher-remote-release-") as directory:
        temp_root = Path(directory)
        uploads = _prepare_release_upload_paths(root, assets, temp_root)
        # Source checksum, branch and tag are all inspected before any remote mutation.
        if existing_tag is None:
            request_file = temp_root / "tag-request.json"
            request_file.write_text(
                json.dumps({"ref": f"refs/tags/{tag}", "sha": expected_head}, separators=(",", ":")),
                encoding="utf-8",
            )
            _run_gh(root, "api", "-X", "POST", f"repos/{identity['full_name']}/git/refs",
                    "--input", str(request_file), token=token, timeout=90)
        if _remote_tag_sha(root, identity, tag, token) != expected_head:
            raise RuntimeError("remote Release tag verification failed; Release assets were not uploaded")
        _run_gh(
            root, "release", "create", tag, *uploads, "--verify-tag", "--title", title,
            "--notes", "", "--latest" if latest else "--latest=false",
            "--repo", identity["full_name"], token=token, timeout=RELEASE_GH_TIMEOUT_SECONDS,
        )

    verified = _json_gh_response(
        _run_gh(root, "release", "view", tag, "--repo", identity["full_name"],
                "--json", "tagName,url,assets", token=token, timeout=120),
        "GitHub remote Release readback",
    )
    if verified.get("tagName") != tag or not isinstance(verified.get("url"), str) or not verified.get("url"):
        raise RuntimeError("remote Release tag or URL readback mismatch")
    remote_assets = verified.get("assets")
    if not isinstance(remote_assets, list):
        raise RuntimeError("remote Release readback did not include assets")
    by_name = {str(item.get("name")): item for item in remote_assets if isinstance(item, dict)}
    for asset in assets:
        item = by_name.get(asset["name"])
        if item is None or item.get("size") != asset["size"]:
            raise RuntimeError("remote Release asset missing or size mismatch: " + asset["name"])
        if asset.get("label") is not None and item.get("label") != asset["label"]:
            raise RuntimeError("remote Release asset display label mismatch: " + asset["name"])
        digest = item.get("digest")
        if digest is not None and digest != "sha256:" + asset["sha256"]:
            raise RuntimeError("remote Release asset digest mismatch: " + asset["name"])
    _verify_release_latest(root, identity["full_name"], tag, latest, token)
    if _remote_tag_sha(root, identity, tag, token) != expected_head:
        raise RuntimeError("remote Release tag changed during publication")
    return {**identity, "released": True, "tag": tag, "head": expected_head,
            "latest_requested": latest, "url": verified["url"], "assets": assets,
            "safety": {"explicit_remote_repo_id": True, "pinned_branch_commit": True,
                       "no_tag_rewrite": True, "explicit_asset_sha256": True,
                       "no_release_overwrite": True, "credential_manager_only": True,
                       "no_local_git_repository": True}}


# Private, repository-local recovery record; always under the selected .git directory.
# These actions never authorize a remote repository merely because its name exists.
FIRST_PUBLISH_STATE = "folderbridge-first-publication.json"
FIRST_PUBLISH_WORKFLOW = "folderbridge-git-publisher-first-publication"
FIRST_PUBLISH_PHASES = frozenset({
    "local_initialized", "remote_request_pending", "remote_created", "initial_committed",
})


def _first_publication_state_path(root: Path) -> Path:
    git_dir = root / ".git"
    if git_dir.is_symlink() or _is_reparse(git_dir) or not git_dir.is_dir():
        raise RuntimeError("first publication requires an ordinary, non-linked .git directory")
    state = git_dir / FIRST_PUBLISH_STATE
    if state.is_symlink() or _is_reparse(state):
        raise RuntimeError("first-publication state may not be a symlink or reparse point")
    return state


def _read_first_publication_state(root: Path) -> dict[str, Any]:
    state_path = _first_publication_state_path(root)
    try:
        raw = state_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError("repository was not initialized by Git Publisher's first-publication action") from exc
    if len(raw) > 4096:
        raise RuntimeError("first-publication recovery record exceeds 4096 bytes")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("first-publication recovery record is invalid") from exc
    if (
        not isinstance(result, dict)
        or result.get("workflow") != FIRST_PUBLISH_WORKFLOW
        or result.get("version") != 1
        or result.get("branch") != "main"
        or result.get("phase") not in FIRST_PUBLISH_PHASES
    ):
        raise RuntimeError("first-publication recovery record is not recognized")
    return result


def _write_first_publication_state(root: Path, value: dict[str, Any]) -> None:
    state_path = _first_publication_state_path(root)
    if value.get("workflow") != FIRST_PUBLISH_WORKFLOW or value.get("phase") not in FIRST_PUBLISH_PHASES:
        raise RuntimeError("refusing to write an invalid first-publication state")
    encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > 4096:
        raise RuntimeError("first-publication state exceeds the size limit")
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=state_path.parent, prefix=".first-publish-", delete=False) as handle:
            tmp = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, state_path)
        tmp = None
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def _first_publication_preflight(root: Path) -> dict[str, Any]:
    git_dir = root / ".git"
    _reject_links(root, git_dir)
    if not git_dir.exists():
        state = "not_initialized"
        branch = None
    else:
        if not git_dir.is_dir() or git_dir.is_symlink() or _is_reparse(git_dir):
            raise RuntimeError("selected path has an unsupported .git entry")
        top = _text(_run_git(root, "rev-parse", "--show-toplevel"))
        if Path(top).resolve(strict=True) != root:
            raise RuntimeError("selected repository root does not match the intended directory")
        branch = _text(_run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)) or None
        head = _run_git(root, "rev-parse", "--verify", "HEAD", check=False)
        state = "committed" if head.returncode == 0 else "unborn"
        try:
            journal = _read_first_publication_state(root)
        except RuntimeError:
            journal = None
        if journal:
            state = journal["phase"]
    return {
        "repository_state": state,
        "branch": branch,
        "ready_for_init": state == "not_initialized",
        "credential_manager": _gcm_status(root),
        "no_automatic_publication": True,
    }


def _init_local_repository(root: Path) -> dict[str, Any]:
    git_dir = root / ".git"
    _reject_links(root, git_dir)
    if git_dir.exists():
        state = _read_first_publication_state(root)
        top = _text(_run_git(root, "rev-parse", "--show-toplevel"))
        if Path(top).resolve(strict=True) != root:
            raise RuntimeError("existing .git directory is not the selected repository root")
        if _text(_run_git(root, "symbolic-ref", "--short", "HEAD")) != "main":
            raise RuntimeError("first-publication repository changed branch")
        return {"initialized": True, "already_initialized": True, "branch": "main", "phase": state["phase"]}
    if not root.is_dir():
        raise RuntimeError("selected first-publication directory must already exist")
    _run_git(root, "init", "-b", "main")
    top = _text(_run_git(root, "rev-parse", "--show-toplevel"))
    if Path(top).resolve(strict=True) != root:
        raise RuntimeError("git init did not create a repository rooted at the intended directory")
    if _run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        raise RuntimeError("unexpected existing commit after repository initialization")
    state = {"workflow": FIRST_PUBLISH_WORKFLOW, "version": 1, "branch": "main", "phase": "local_initialized"}
    _write_first_publication_state(root, state)
    return {"initialized": True, "already_initialized": False, "branch": "main", "phase": "local_initialized"}


def _clean_new_repository_identity(owner: Any, name: Any, visibility: Any, description: Any) -> tuple[str, str, str, str]:
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
        raise RuntimeError("owner must be one explicit GitHub personal-account login")
    if (
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
        or name in {".", ".."} or name.endswith(".git")
    ):
        raise RuntimeError("repository name contains unsupported characters")
    if visibility not in ("public", "private"):
        raise RuntimeError("repository visibility must explicitly be public or private")
    if not isinstance(description, str) or len(description) > 200 or any(c in description for c in "\r\n\x00"):
        raise RuntimeError("repository description must be a single line of at most 200 characters")
    return owner, name, visibility, description


def _json_gh_response(result: subprocess.CompletedProcess[bytes], label: str) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"{label} returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} did not return a GitHub JSON object")
    return value


def _probe_github_repository(root: Path, full_name: str, token: str) -> dict[str, Any] | None:
    output = _run_gh(root, "api", f"repos/{full_name}", token=token, check=False, timeout=60)
    if output.returncode == 0:
        return _json_gh_response(output, "GitHub repository lookup")
    error = _redact(output.stderr.decode("utf-8", errors="replace"))
    if re.search(r"\b(?:HTTP|status)\s*[:=]?\s*404\b", error, re.IGNORECASE):
        return None
    raise RuntimeError("GitHub repository lookup failed; remote existence is unknown: " + error[:450])


def _assert_expected_github_repository(
    value: dict[str, Any], owner: str, name: str, visibility: str, *, expected_id: int | None = None,
) -> None:
    expected = f"{owner}/{name}"
    remote_owner = value.get("owner")
    if (
        not isinstance(remote_owner, dict)
        or str(remote_owner.get("login", "")).casefold() != owner.casefold()
        or str(value.get("full_name", "")).casefold() != expected.casefold()
        or value.get("private") is not (visibility == "private")
        or not isinstance(value.get("id"), int)
    ):
        raise RuntimeError("GitHub returned an unexpected repository owner, name, identity, or visibility")
    if expected_id is not None and value["id"] != expected_id:
        raise RuntimeError("GitHub repository ID changed since this publication created it")


def _create_github_repository(
    root: Path, owner: Any, name: Any, visibility: Any, description: Any,
) -> dict[str, Any]:
    owner, name, visibility, description = _clean_new_repository_identity(owner, name, visibility, description)
    state = _read_first_publication_state(root)
    if _text(_run_git(root, "symbolic-ref", "--short", "HEAD")) != "main":
        raise RuntimeError("first publication requires a main branch")
    full_name = f"{owner}/{name}"
    _validate_origin(f"https://github.com/{full_name}.git")
    if state["phase"] not in {"local_initialized", "remote_request_pending", "remote_created", "initial_committed"}:
        raise RuntimeError("repository is not in a recognized creation phase")
    previously_bound = state.get("owner")
    if previously_bound is not None and (
        str(previously_bound).casefold() != owner.casefold()
        or state.get("name") != name or state.get("visibility") != visibility
        or state.get("description") != description
    ):
        raise RuntimeError("first-publication intent is already bound to a different repository")
    existing_origin = _run_git(root, "remote", "get-url", "origin", check=False)
    expected_origin = f"https://github.com/{full_name}.git"
    if existing_origin.returncode == 0 and _text(existing_origin) != expected_origin:
        raise RuntimeError("existing Git origin differs from the requested remote; refusing to overwrite")
    if existing_origin.returncode != 0 and state["phase"] == "initial_committed":
        raise RuntimeError("committed first-publication repository unexpectedly lost its origin")

    token = _github_token_from_gcm(root)
    who = _json_gh_response(_run_gh(root, "api", "user", token=token, timeout=60), "GitHub user")
    if str(who.get("login", "")).casefold() != owner.casefold():
        raise RuntimeError("authenticated GitHub account does not match the requested owner")
    existing = _probe_github_repository(root, full_name, token)
    created = False
    if existing is not None:
        if state["phase"] not in {"remote_created", "initial_committed"}:
            raise RuntimeError("remote name already exists or creation response is ambiguous; manual reconciliation required")
        if type(state.get("remote_id")) is not int:
            raise RuntimeError("verified remote identity was not recorded; refusing to adopt an existing repository")
        _assert_expected_github_repository(
            existing, owner, name, visibility, expected_id=state.get("remote_id"),
        )
    else:
        if state["phase"] in {"remote_created", "initial_committed"}:
            raise RuntimeError("previously created GitHub repository disappeared; refusing to recreate it")
        if _run_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
            raise RuntimeError("remote creation requires the initial empty local repository")
        pending = {
            **state, "phase": "remote_request_pending", "owner": owner, "name": name,
            "visibility": visibility, "description": description,
        }
        _write_first_publication_state(root, pending)
        response = _run_gh(
            root, "api", "-X", "POST", "user/repos",
            "-f", f"name={name}", "-f", f"description={description}",
            "-F", f"private={'true' if visibility == 'private' else 'false'}",
            "-F", "auto_init=false",
            token=token, timeout=90, check=False,
        )
        if response.returncode != 0:
            error = _redact(response.stderr.decode("utf-8", errors="replace"))
            if re.search(r"\bHTTP\s*(?:401|403)\b", error):
                raise RuntimeError("GitHub authorization lacks repository-creation access; reconnect Git Publisher. " + error[:300])
            raise RuntimeError("GitHub creation not confirmed; recovery journal retained. " + error[:400])
        existing = _json_gh_response(response, "GitHub repository creation")
        _assert_expected_github_repository(existing, owner, name, visibility)
        verified = _probe_github_repository(root, full_name, token)
        if verified is None:
            raise RuntimeError("GitHub creation returned success but the repository is not yet readable")
        _assert_expected_github_repository(verified, owner, name, visibility, expected_id=existing["id"])
        _write_first_publication_state(root, {**pending, "phase": "remote_created", "remote_id": existing["id"]})
        created = True
    if existing_origin.returncode != 0:
        _run_git(root, "remote", "add", "origin", expected_origin)
    _repo_info(root)
    return {
        "created": created, "already_created": not created, "owner": owner, "repo": name,
        "visibility": visibility, "remote_id": existing["id"], "url": f"https://github.com/{full_name}",
        "branch": "main", "credential_manager_only": True,
    }


def _assert_staged_bytes_match_worktree(root: Path, paths: list[str]) -> None:
    for rel in paths:
        absolute = root.joinpath(*PurePosixPath(rel).parts)
        staged = _text(_run_git(root, "rev-parse", f":{rel}"))
        raw = _text(_run_git(root, "hash-object", "--no-filters", "--", rel))
        if staged != raw:
            raise RuntimeError(
                f"Git staging normalized or transformed bytes of {rel}; "
                "review repository-local .gitattributes before initial publication"
            )
        if not absolute.is_file() or absolute.is_symlink() or _is_reparse(absolute):
            raise RuntimeError("selected first-publication file changed while validating staged bytes")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
