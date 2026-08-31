from __future__ import annotations

import ctypes
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

try:
    from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree
except ImportError:  # FolderBridge 0.8.21 compatibility before the public process-helper re-export.
    from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


DEFAULT_PROFILE = "default"
DEFAULT_TIMEOUT = 180
CRED_TYPE_GENERIC = 1
CRED_PREFIX = "FolderBridge/ftp-toolkit/"
MAX_LIST_ITEMS = 2000
MAX_TREE_FILES = 4096
MAX_RESULT_SAMPLES = 50

_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\d{1,3}(?:\.\d{1,3}){3})$")
_SENSITIVE_NAMES = {
    ".env", ".git", ".svn", ".hg", "node_modules", "__pycache__",
    "id_rsa", "id_ed25519", "credentials", "credential", "secrets", "secret",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore"}


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWrittenLow", ctypes.c_uint32),
        ("LastWrittenHigh", ctypes.c_uint32),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class _ProcessFailure(RuntimeError):
    def __init__(self, returncode: int, message: str):
        super().__init__(message)
        self.returncode = int(returncode)


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _profile_name(value: Any) -> str:
    name = DEFAULT_PROFILE if value in {None, ""} else value
    if not isinstance(name, str) or not _PROFILE_RE.fullmatch(name):
        raise RuntimeError("profile must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return name


def _credential_target(profile: str) -> str:
    return CRED_PREFIX + _profile_name(profile)


def _wincred_api() -> Any:
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is required")
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    advapi32.CredReadW.restype = ctypes.c_int
    advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    advapi32.CredDeleteW.restype = ctypes.c_int
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


def _read_profile(profile: str) -> tuple[dict[str, Any], tuple[str, str]]:
    target = _credential_target(profile)
    api = _wincred_api()
    ptr = ctypes.POINTER(_CredentialW)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        raise RuntimeError(f"FTP profile is not configured: {profile}")
    try:
        cred = ptr.contents
        username = cred.UserName or ""
        raw = ctypes.string_at(cred.CredentialBlob, int(cred.CredentialBlobSize)) if cred.CredentialBlobSize else b""
    finally:
        api.CredFree(ptr)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored FTP profile is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise RuntimeError("stored FTP profile schema is unsupported")
    mode = payload.get("mode")
    if mode not in {"ftps-explicit", "ftp-plain"}:
        raise RuntimeError("stored FTP profile mode is unsupported")
    host = payload.get("host")
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host) or ":" in host:
        raise RuntimeError("stored FTP host is invalid")
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeError("stored FTP port is invalid")
    remote_root = _validate_remote_root(payload.get("remote_root"))
    insecure_tls = payload.get("insecure_tls", False)
    if not isinstance(insecure_tls, bool):
        raise RuntimeError("stored insecure_tls value is invalid")
    if mode == "ftp-plain" and insecure_tls:
        raise RuntimeError("insecure_tls only applies to FTPS")
    proxy_mode = payload.get("proxy_mode", "none")
    if proxy_mode not in {"none", "http-connect"}:
        raise RuntimeError("stored FTP proxy mode is unsupported")
    proxy_host = payload.get("proxy_host", "")
    proxy_port = payload.get("proxy_port", 0)
    if proxy_mode == "http-connect":
        if not isinstance(proxy_host, str) or not _HOST_RE.fullmatch(proxy_host) or ":" in proxy_host:
            raise RuntimeError("stored FTP proxy host is invalid")
        if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 1 <= proxy_port <= 65535:
            raise RuntimeError("stored FTP proxy port is invalid")
    else:
        proxy_host = ""
        proxy_port = 0
    password = payload.get("password")
    if not isinstance(username, str) or not username or any(ch in username for ch in ":\r\n\x00"):
        raise RuntimeError("stored FTP username is invalid")
    if not isinstance(password, str) or not password or any(ch in password for ch in "\r\n\x00"):
        raise RuntimeError("stored FTP password is invalid")
    profile_data = {
        "mode": mode,
        "host": host,
        "port": port,
        "remote_root": remote_root,
        "insecure_tls": insecure_tls,
        "proxy_mode": proxy_mode,
        "proxy_host": proxy_host,
        "proxy_port": proxy_port,
    }
    return profile_data, (username, password)


def _delete_profile(profile: str) -> bool:
    api = _wincred_api()
    if api.CredDeleteW(_credential_target(profile), CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise RuntimeError(f"Windows Credential Manager delete failed with error {error}")


def _validate_remote_root(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 1024:
        raise RuntimeError("remote root must be an absolute FTP path")
    if any(ch in value for ch in "\r\n\x00?#") or "\\" in value:
        raise RuntimeError("remote root contains unsupported characters")
    parts = PurePosixPath(value).parts
    if any(part == ".." for part in parts):
        raise RuntimeError("remote root may not contain parent traversal")
    normalized = "/" + "/".join(part for part in parts if part not in {"/", "", "."})
    return normalized.rstrip("/") or "/"


def _clean_remote_relative(value: Any, *, allow_empty: bool = False) -> str:
    if value in {None, ""} and allow_empty:
        return ""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RuntimeError("remote path must be a relative POSIX path")
    if value.startswith("/") or "\\" in value or any(ch in value for ch in "\r\n\x00?#"):
        raise RuntimeError("remote path must be a clean relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("remote path contains traversal or empty segments")
    return path.as_posix()


def _remote_path(profile: dict[str, Any], relative: str = "") -> str:
    clean = _clean_remote_relative(relative, allow_empty=True)
    root = profile["remote_root"]
    if not clean:
        return root
    joined = posixpath.join(root, clean)
    return joined if joined.startswith("/") else "/" + joined


def _remote_url(profile: dict[str, Any], relative: str = "", *, directory: bool = False) -> str:
    encoded = quote(_remote_path(profile, relative), safe="/._-~")
    if directory and not encoded.endswith("/"):
        encoded += "/"
    return f"ftp://{profile['host']}:{profile['port']}{encoded}"


def _server_url(profile: dict[str, Any]) -> str:
    return f"ftp://{profile['host']}:{profile['port']}/"


def _is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _is_sensitive_part(part: str) -> bool:
    lower = part.casefold()
    if lower in _SENSITIVE_NAMES:
        return True
    return Path(lower).suffix in _SENSITIVE_SUFFIXES


def _clean_local_relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RuntimeError("local path must be a workspace-relative POSIX path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise RuntimeError("local path must be a workspace-relative POSIX path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("local path contains traversal or empty segments")
    if any(_is_sensitive_part(part) for part in path.parts):
        raise RuntimeError("local path refers to a sensitive or dependency location")
    return path


def _check_existing_chain(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            return
        if _is_reparse(current):
            raise RuntimeError("local paths may not traverse links or reparse points")


def _resolve_local_input(root: Path, value: Any, *, directory: bool) -> tuple[PurePosixPath, Path]:
    rel = _clean_local_relative(value)
    _check_existing_chain(root, rel)
    path = root.joinpath(*rel.parts).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("local path escapes workspace") from exc
    if directory and not path.is_dir():
        raise RuntimeError("local_dir must name a directory")
    if not directory and not path.is_file():
        raise RuntimeError("local_path must name a regular file")
    if _is_reparse(path):
        raise RuntimeError("local path may not be a link or reparse point")
    return rel, path


def _resolve_local_output(root: Path, value: Any, *, overwrite: bool) -> tuple[PurePosixPath, Path]:
    rel = _clean_local_relative(value)
    parent_rel = PurePosixPath(*rel.parts[:-1]) if len(rel.parts) > 1 else None
    if parent_rel:
        _check_existing_chain(root, parent_rel)
        parent = root.joinpath(*parent_rel.parts)
        parent.mkdir(parents=True, exist_ok=True)
        _check_existing_chain(root, parent_rel)
    else:
        parent = root
    target = parent / rel.name
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise RuntimeError("local output escapes workspace") from exc
    if target.exists():
        if _is_reparse(target) or not target.is_file():
            raise RuntimeError("local output target must be a regular file")
        if not overwrite:
            raise RuntimeError("local output already exists; set overwrite=true to replace it")
    return rel, target


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _curl_path() -> str:
    curl = shutil.which("curl.exe")
    if not curl:
        raise RuntimeError("curl.exe was not found on the trusted PATH")
    return curl


def _redact(text: str, secrets: tuple[str, str] | None) -> str:
    redacted = text or ""
    if secrets:
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[-6000:]


def _run_process(
    argv: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    secrets: tuple[str, str] | None = None,
    hide_window: bool = True,
) -> tuple[bytes, bytes]:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=hide_window),
        )
    except OSError as exc:
        raise RuntimeError(f"could not start required local tool: {exc}") from exc
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=None if timeout == 0 else timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_owned_process_tree(process, hide_window=True)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        raise RuntimeError(f"local tool exceeded {timeout} seconds") from exc
    if process.returncode != 0:
        detail = _redact(stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace"), secrets)
        raise _ProcessFailure(process.returncode, detail or f"local tool failed with exit code {process.returncode}")
    return stdout, stderr


def _curl_config(profile: dict[str, Any], credential: tuple[str, str]) -> bytes:
    username, password = credential

    def quoted(value: str) -> str:
        if any(ch in value for ch in "\r\n\x00"):
            raise RuntimeError("credential contains unsupported control characters")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [
        "silent",
        "show-error",
        "fail",
        "connect-timeout = 30",
        "user = " + quoted(username + ":" + password),
    ]
    if profile["mode"] == "ftps-explicit":
        lines.append("ssl-reqd")
        if profile["insecure_tls"]:
            lines.append("insecure")
    if profile.get("proxy_mode", "none") == "http-connect":
        proxy_host = profile.get("proxy_host", "")
        proxy_port = profile.get("proxy_port", 0)
        if not isinstance(proxy_host, str) or not _HOST_RE.fullmatch(proxy_host) or ":" in proxy_host:
            raise RuntimeError("FTP proxy host is invalid")
        if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 1 <= proxy_port <= 65535:
            raise RuntimeError("FTP proxy port is invalid")
        lines.append("proxy = " + quoted(f"http://{proxy_host}:{proxy_port}"))
        lines.append("proxytunnel")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _curl_exec(
    profile: dict[str, Any],
    credential: tuple[str, str],
    args: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    allow_codes: set[int] | None = None,
) -> tuple[int, bytes, bytes]:
    argv = [_curl_path(), "--config", "-"] + args
    try:
        stdout, stderr = _run_process(
            argv,
            input_bytes=_curl_config(profile, credential),
            timeout=timeout,
            secrets=credential,
        )
        return 0, stdout, stderr
    except _ProcessFailure as exc:
        if allow_codes and exc.returncode in allow_codes:
            return exc.returncode, b"", str(exc).encode("utf-8", errors="replace")
        raise RuntimeError("FTP operation failed: " + str(exc)) from exc


def _remote_bytes(profile: dict[str, Any], credential: tuple[str, str], relative: str, *, timeout: int = 0) -> bytes | None:
    code, stdout, _ = _curl_exec(
        profile,
        credential,
        ["--output", "-", _remote_url(profile, relative)],
        timeout=timeout,
        allow_codes={78},
    )
    return None if code == 78 else stdout


def _stat_remote(profile: dict[str, Any], credential: tuple[str, str], relative: str) -> dict[str, Any]:
    clean = _clean_remote_relative(relative)
    code, stdout, _ = _curl_exec(
        profile,
        credential,
        ["--head", _remote_url(profile, clean)],
        allow_codes={9, 78},
    )
    if code in {9, 78}:
        return {"exists": False, "path": clean, "size": None, "last_modified": None}
    text = stdout.decode("utf-8", errors="replace")
    size = None
    modified = None
    for line in text.replace("\r", "").split("\n"):
        lower = line.casefold()
        if lower.startswith("content-length:"):
            raw = line.split(":", 1)[1].strip()
            if raw.isdigit():
                size = int(raw)
        elif lower.startswith("last-modified:"):
            modified = line.split(":", 1)[1].strip() or None
    return {"exists": True, "path": clean, "size": size, "last_modified": modified}


def _remote_matches(
    profile: dict[str, Any], credential: tuple[str, str], relative: str, local: Path, verify: str
) -> bool:
    if verify == "none":
        return False
    stat = _stat_remote(profile, credential, relative)
    if not stat["exists"] or stat["size"] != local.stat().st_size:
        return False
    if verify == "size":
        return True
    payload = _remote_bytes(profile, credential, relative, timeout=0)
    return payload is not None and hashlib.sha256(payload).hexdigest() == _sha256_file(local)


def _verify_remote(
    profile: dict[str, Any], credential: tuple[str, str], relative: str, local: Path, verify: str
) -> None:
    if verify == "none":
        return
    stat = _stat_remote(profile, credential, relative)
    expected_size = local.stat().st_size
    if not stat["exists"] or stat["size"] != expected_size:
        raise RuntimeError(f"remote size verification failed: {relative}")
    if verify == "sha256":
        payload = _remote_bytes(profile, credential, relative, timeout=0)
        if payload is None or hashlib.sha256(payload).hexdigest() != _sha256_file(local):
            raise RuntimeError(f"remote SHA-256 verification failed: {relative}")


def _remote_parent_dirs(relative: str) -> list[str]:
    clean = _clean_remote_relative(relative)
    parts = PurePosixPath(clean).parts[:-1]
    return [PurePosixPath(*parts[:index]).as_posix() for index in range(1, len(parts) + 1)]


def _upload_one(
    profile: dict[str, Any], credential: tuple[str, str], local: Path, remote_relative: str, verify: str
) -> bool:
    clean = _clean_remote_relative(remote_relative)
    if _remote_matches(profile, credential, clean, local, verify):
        return False
    args: list[str] = []
    for remote_dir in _remote_parent_dirs(clean):
        args.extend(["--quote", "*MKD " + _remote_path(profile, remote_dir)])
    args.extend(["--upload-file", str(local), _remote_url(profile, clean)])
    _curl_exec(profile, credential, args, timeout=0)
    _verify_remote(profile, credential, clean, local, verify)
    return True


def _tree_plan(root: Path, local_dir: Any, remote_dir: Any, max_files: int) -> tuple[Path, str, list[dict[str, Any]]]:
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= MAX_TREE_FILES:
        raise RuntimeError(f"max_files must be 1..{MAX_TREE_FILES}")
    _, base = _resolve_local_input(root, local_dir, directory=True)
    remote_base = _clean_remote_relative(remote_dir, allow_empty=True)
    items: list[dict[str, Any]] = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        directory_path = Path(directory)
        if _is_reparse(directory_path):
            raise RuntimeError("upload-tree encountered a link or reparse point")
        kept_dirs = []
        for name in sorted(dirs):
            child = directory_path / name
            if _is_sensitive_part(name):
                continue
            if _is_reparse(child):
                raise RuntimeError("upload-tree encountered a link or reparse point")
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if _is_sensitive_part(name):
                continue
            path = directory_path / name
            if _is_reparse(path) or not path.is_file():
                raise RuntimeError("upload-tree encountered a non-regular file")
            relative = path.relative_to(base).as_posix()
            remote = posixpath.join(remote_base, relative) if remote_base else relative
            items.append({"local": path, "relative": relative, "remote": remote, "size": path.stat().st_size})
            if len(items) > max_files:
                raise RuntimeError(f"upload-tree exceeds max_files={max_files}")
    if not items:
        raise RuntimeError("upload-tree found no publishable regular files")
    return base, remote_base, items


def _configure(params: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_name(params.get("profile"))
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("powershell.exe was not found on the trusted PATH")
    script = Path(__file__).resolve().with_name("configure.ps1")
    if not script.is_file():
        raise RuntimeError("configure.ps1 is missing from the approved extension snapshot")
    argv = [
        powershell,
        "-NoProfile",
        "-STA",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProfileName",
        profile,
        "-CredentialTarget",
        _credential_target(profile),
    ]
    stdout, _ = _run_process(argv, timeout=600, hide_window=False)
    marker = stdout.decode("utf-8-sig", errors="replace").strip()
    if marker == "CANCELLED":
        return {"ok": True, "profile": profile, "configured": False, "cancelled": True}
    if marker != "CONFIGURED":
        raise RuntimeError("configuration dialog returned an unexpected result")
    profile_data, _ = _read_profile(profile)
    return {
        "ok": True,
        "profile": profile,
        "configured": True,
        "mode": profile_data["mode"],
        "remote_root": profile_data["remote_root"],
        "tls_verification": not profile_data["insecure_tls"] if profile_data["mode"] == "ftps-explicit" else None,
    }


def _status(params: dict[str, Any]) -> dict[str, Any]:
    profile_value = params.get("profile")
    result: dict[str, Any] = {
        "ok": True,
        "curl_available": bool(shutil.which("curl.exe")),
        "powershell_available": bool(shutil.which("powershell.exe")),
    }
    if profile_value in {None, ""}:
        result["profile_checked"] = False
        return result
    profile = _profile_name(profile_value)
    try:
        profile_data, _ = _read_profile(profile)
        result.update({
            "profile_checked": True,
            "profile": profile,
            "configured": True,
            "mode": profile_data["mode"],
            "remote_root": profile_data["remote_root"],
            "tls_verification": not profile_data["insecure_tls"] if profile_data["mode"] == "ftps-explicit" else None,
            "proxy_mode": profile_data.get("proxy_mode", "none"),
            "proxy_enabled": profile_data.get("proxy_mode", "none") != "none",
        })
    except Exception as exc:
        result.update({"profile_checked": True, "profile": profile, "configured": False, "error": str(exc)})
    return result


def _check(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    _curl_exec(profile, credential, ["--list-only", _remote_url(profile, directory=True)])
    return {
        "ok": True,
        "profile": profile_name,
        "reachable": True,
        "mode": profile["mode"],
        "remote_root": profile["remote_root"],
    }


def _list(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    remote_dir = _clean_remote_relative(params.get("remote_dir", ""), allow_empty=True)
    max_items = int(params.get("max_items", 500))
    if not 1 <= max_items <= MAX_LIST_ITEMS:
        raise RuntimeError(f"max_items must be 1..{MAX_LIST_ITEMS}")
    _, stdout, _ = _curl_exec(profile, credential, ["--list-only", _remote_url(profile, remote_dir, directory=True)])
    names = [line.strip() for line in stdout.decode("utf-8", errors="replace").replace("\r", "").split("\n") if line.strip()]
    truncated = len(names) > max_items
    return {
        "ok": True,
        "profile": profile_name,
        "remote_dir": remote_dir,
        "items": names[:max_items],
        "count_returned": min(len(names), max_items),
        "truncated": truncated,
    }


def _stat(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    result = _stat_remote(profile, credential, params["remote_path"])
    result.update({"ok": True, "profile": profile_name})
    return result


def _mkdir(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    remote_dir = _clean_remote_relative(params["remote_dir"])
    _curl_exec(profile, credential, ["--quote", "MKD " + _remote_path(profile, remote_dir), _server_url(profile)])
    return {"ok": True, "profile": profile_name, "created": remote_dir}


def _rename(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    source = _clean_remote_relative(params["from_path"])
    target = _clean_remote_relative(params["to_path"])
    _curl_exec(
        profile,
        credential,
        [
            "--quote", "RNFR " + _remote_path(profile, source),
            "--quote", "RNTO " + _remote_path(profile, target),
            _server_url(profile),
        ],
    )
    return {"ok": True, "profile": profile_name, "from_path": source, "to_path": target}


def _delete(params: dict[str, Any]) -> dict[str, Any]:
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    remote = _clean_remote_relative(params["remote_path"])
    if any(ch in remote for ch in "*[]{}"):
        raise RuntimeError("delete requires one exact remote file path without wildcard characters")
    _curl_exec(
        profile,
        credential,
        ["--quote", "DELE " + _remote_path(profile, remote), "--head", _server_url(profile)],
    )
    return {"ok": True, "profile": profile_name, "deleted": remote}


def _upload(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    rel, local = _resolve_local_input(root, params["local_path"], directory=False)
    verify = params.get("verify", "sha256")
    remote = _clean_remote_relative(params.get("remote_path") or rel.name)
    changed = _upload_one(profile, credential, local, remote, verify)
    return {
        "ok": True,
        "profile": profile_name,
        "local_path": rel.as_posix(),
        "remote_path": remote,
        "size": local.stat().st_size,
        "sha256": _sha256_file(local),
        "uploaded": changed,
        "skipped_verified": not changed,
        "verification": verify,
    }


def _upload_tree(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    verify = params.get("verify", "size")
    max_files = int(params.get("max_files", 1024))
    base, remote_base, items = _tree_plan(root, params["local_dir"], params.get("remote_dir", ""), max_files)
    uploaded = 0
    skipped = 0
    uploaded_bytes = 0
    uploaded_sample: list[str] = []
    for item in items:
        changed = _upload_one(profile, credential, item["local"], item["remote"], verify)
        if changed:
            uploaded += 1
            uploaded_bytes += item["size"]
            if len(uploaded_sample) < MAX_RESULT_SAMPLES:
                uploaded_sample.append(item["remote"])
        else:
            skipped += 1
    return {
        "ok": True,
        "profile": profile_name,
        "local_dir": base.relative_to(root).as_posix(),
        "remote_dir": remote_base,
        "file_count": len(items),
        "uploaded_files": uploaded,
        "skipped_verified_files": skipped,
        "uploaded_bytes": uploaded_bytes,
        "verification": verify,
        "uploaded_sample": uploaded_sample,
        "sample_truncated": uploaded > len(uploaded_sample),
    }


def _download(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    profile_name = _profile_name(params.get("profile"))
    profile, credential = _read_profile(profile_name)
    remote = _clean_remote_relative(params["remote_path"])
    rel, target = _resolve_local_output(root, params["local_path"], overwrite=bool(params.get("overwrite", False)))
    fd, temp_name = tempfile.mkstemp(prefix=".fbftp-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        _curl_exec(profile, credential, ["--output", str(temp), _remote_url(profile, remote)], timeout=0)
        if not temp.is_file():
            raise RuntimeError("download did not produce a regular file")
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return {
        "ok": True,
        "profile": profile_name,
        "remote_path": remote,
        "local_path": rel.as_posix(),
        "size": target.stat().st_size,
        "sha256": _sha256_file(target),
        "workspace_artifacts": [rel.as_posix()],
    }


def _forget(params: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_name(params.get("profile"))
    removed = _delete_profile(profile)
    return {"ok": True, "profile": profile, "configured": False, "credential_removed": removed}


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _status(params)
    if action == "configure":
        return _configure(params)
    if action == "forget":
        return _forget(params)
    if action == "check":
        return _check(params)
    if action == "list":
        return _list(params)
    if action == "stat":
        return _stat(params)
    if action == "mkdir":
        return _mkdir(params)
    if action == "rename":
        return _rename(params)
    if action == "delete":
        return _delete(params)
    if action == "upload":
        return _upload(params, context)
    if action == "upload-tree":
        return _upload_tree(params, context)
    if action == "download":
        return _download(params, context)
    raise RuntimeError(f"unsupported action: {action}")
