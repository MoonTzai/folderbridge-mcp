from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


MAX_OUTPUT = 32_000
MAX_COMMIT_FILE_BYTES = 100 * 1024 * 1024
MAX_COMMIT_PATHS = 128
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


def _run_gh(root: Path, *args: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    gh = _gh_executable()
    env = os.environ.copy()
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
    completed = _run_git(root, "-c", "core.fsmonitor=false", "diff", "--cached", "--no-ext-diff", "--name-only", "-z")
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


def _project_release_version(root: Path) -> str:
    path = root / "pyproject.toml"
    _reject_links(root, path)
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"could not read project version from pyproject.toml: {exc}") from exc
    version = raw.get("project", {}).get("version") if isinstance(raw.get("project"), dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("pyproject.toml project.version must be a stable numeric x.y.z version")
    return version


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

    _run_gh(root, "auth", "status", "--hostname", "github.com", timeout=60)
    repo_name = f"{repo['owner']}/{repo['repo']}"
    existing = _run_gh(root, "release", "view", tag, "--repo", repo_name, check=False)
    if existing.returncode == 0:
        _run_gh(
            root,
            "release", "upload", tag, str(exe), str(checksum), "--clobber", "--repo", repo_name,
            timeout=300,
        )
        _run_gh(root, "release", "edit", tag, "--title", f"FolderBridge {version}", "--latest", "--repo", repo_name)
    else:
        _run_gh(
            root,
            "release", "create", tag, str(exe), str(checksum),
            "--verify-tag", "--generate-notes", "--title", f"FolderBridge {version}", "--latest", "--repo", repo_name,
            timeout=300,
        )
    verified = _run_gh(root, "release", "view", tag, "--repo", repo_name, "--json", "tagName,url")
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
