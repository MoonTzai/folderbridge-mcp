from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


MAX_OUTPUT = 32_000
MAX_COMMIT_FILE_BYTES = 64 * 1024 * 1024
DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "target", "vendor", ".idea", ".vscode",
}
DENIED_BASENAMES = {
    ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", "credentials.json",
}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
TOKEN_RE = re.compile(r"(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)")
URL_CREDENTIAL_RE = re.compile(r"(https://)[^/@\s]+@", re.IGNORECASE)
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    if action == "status":
        return _status(root)
    if bool(context.get("workspace_read_only")):
        raise RuntimeError("FolderBridge is read-only; Git Publisher mutations are unavailable.")
    if action == "connect":
        return _connect(root, username=params.get("username"), force=bool(params.get("force", False)))
    if action == "commit":
        return _commit(root, params.get("paths"), params.get("message"))
    if action == "push":
        return _push(root)
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


def _status_entries(root: Path) -> list[dict[str, str]]:
    completed = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z", "--untracked-files=all")
    raw = completed.stdout.decode("utf-8", errors="replace")
    pieces = raw.split("\x00")
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(pieces):
        record = pieces[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            continue
        code = record[:2]
        path = record[3:]
        item = {"code": code, "path": path}
        if ("R" in code or "C" in code) and index < len(pieces) and pieces[index]:
            item["original_path"] = pieces[index]
            index += 1
        entries.append(item)
        if len(entries) >= 500:
            break
    return entries


def _staged_paths(root: Path) -> list[str]:
    completed = _run_git(root, "-c", "core.fsmonitor=false", "diff", "--cached", "--no-ext-diff", "--name-only", "-z")
    return [item for item in completed.stdout.decode("utf-8", errors="replace").split("\x00") if item]


def _status(root: Path) -> dict[str, Any]:
    repo = _repo_info(root)
    name = _text(_run_git(root, "config", "user.name", check=False))
    email = _text(_run_git(root, "config", "user.email", check=False))
    head = _text(_run_git(root, "rev-parse", "HEAD", check=False))
    return {
        **repo,
        "head": head or None,
        "identity": {"name": name or None, "email": email or None, "ready": bool(name and email)},
        "changes": _status_entries(root),
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
    if basename in DENIED_BASENAMES or suffix in DENIED_SUFFIXES:
        raise RuntimeError("credential/key-like files are not eligible for Git Publisher commits")
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("commit path does not resolve to a regular workspace file") from exc
    if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
        raise RuntimeError("Git Publisher commits only explicit regular files; directories/deletions are not supported")
    if resolved.stat().st_size > MAX_COMMIT_FILE_BYTES:
        raise RuntimeError(f"commit file exceeds the {MAX_COMMIT_FILE_BYTES}-byte safety limit")
    normalized = resolved.relative_to(root).as_posix()
    _reject_transforming_attributes(root, normalized)
    tracked = _run_git(root, "ls-files", "--error-unmatch", "--", normalized, check=False).returncode == 0
    if not tracked:
        ignored = _run_git(root, "check-ignore", "-q", "--", normalized, check=False).returncode == 0
        if ignored:
            raise RuntimeError(f"untracked ignored file cannot be published: {normalized}")
    changed = _run_git(root, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--", normalized)
    if not changed.stdout.strip():
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
    try:
        _run_git(root, "-c", "core.fsmonitor=false", "add", "--", *paths)
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
    return {
        **repo,
        "commit": commit_sha,
        "paths": paths,
        "message": message,
        "git_output": _redact((completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip())[:4000],
        "remaining_changes": _status_entries(root),
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
