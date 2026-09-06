from __future__ import annotations

import hashlib
import http.client
import os
import re
import shutil
import socket
import ssl
import stat
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError


MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 128 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "FolderBridge-Download-Toolkit/0.1.0"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
VCS_BLOCKED_SEGMENTS = {".git", ".hg", ".svn"}
WORKSPACE_ONLY_BLOCKED_SEGMENTS = {
    ".idea", ".mypy_cache", ".next", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", ".vscode", "__pycache__", "build",
    "coverage", "dist", "node_modules", "target", "vendor",
}
SENSITIVE_NAMES = {
    ".api-config.json", ".env", ".netrc", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "api-config.json",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "known_hosts",
    ".folderbridge.json",
}
SENSITIVE_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"}
WINDOWS_RESERVED_BASES = {
    "con", "prn", "aux", "nul", "clock$",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, *, timeout: float = 60.0):
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _LoopbackProxyHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        proxy_host: str,
        proxy_port: int,
        *,
        timeout: float = 60.0,
    ):
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self.set_tunnel(host, port)

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._proxy_host, self._proxy_port),
            self.timeout,
            self.source_address,
        )
        self.sock = sock
        self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _OwnedResponse:
    def __init__(self, connection: _PinnedHTTPSConnection, response: http.client.HTTPResponse):
        self._connection = connection
        self._response = response
        self.status = response.status

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def getheader(self, name: str, default: Any = None) -> Any:
        return self._response.getheader(name, default)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _fail(code: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, **details)


def _prefix_match(value: int, base: int, bits: int, width: int) -> bool:
    if bits == 0:
        return True
    mask = ((1 << bits) - 1) << (width - bits)
    return (value & mask) == (base & mask)


def _is_public_ipv4(packed: bytes) -> bool:
    value = int.from_bytes(packed, "big")
    blocked = (
        (0x00000000, 8),    # 0.0.0.0/8
        (0x0A000000, 8),    # 10.0.0.0/8
        (0x64400000, 10),   # 100.64.0.0/10 shared address space
        (0x7F000000, 8),    # 127.0.0.0/8 loopback
        (0xA9FE0000, 16),   # 169.254.0.0/16 link-local
        (0xAC100000, 12),   # 172.16.0.0/12 private
        (0xC0000000, 24),   # 192.0.0.0/24 protocol assignments
        (0xC0000200, 24),   # 192.0.2.0/24 documentation
        (0xC0586300, 24),   # 192.88.99.0/24 deprecated relay anycast
        (0xC0A80000, 16),   # 192.168.0.0/16 private
        (0xC6120000, 15),   # 198.18.0.0/15 benchmarking
        (0xC6336400, 24),   # 198.51.100.0/24 documentation
        (0xCB007100, 24),   # 203.0.113.0/24 documentation
        (0xE0000000, 4),    # 224.0.0.0/4 multicast
        (0xF0000000, 4),    # 240.0.0.0/4 reserved/broadcast
    )
    return not any(_prefix_match(value, base, bits, 32) for base, bits in blocked)


def _is_public_ipv6(packed: bytes) -> bool:
    if packed[:10] == b"\x00" * 10 and packed[10:12] == b"\xff\xff":
        return _is_public_ipv4(packed[12:])
    value = int.from_bytes(packed, "big")
    blocked = (
        (0x00000000000000000000000000000000, 96),   # ::/96 incl unspecified/IPv4-compatible
        (0x0064FF9B000000000000000000000000, 96),   # 64:ff9b::/96 NAT64 translation
        (0x0064FF9B000100000000000000000000, 48),   # 64:ff9b:1::/48 local-use NAT64
        (0x01000000000000000000000000000000, 64),   # 100::/64 discard-only
        (0x20010000000000000000000000000000, 23),   # 2001:0000::/23 special-purpose block
        (0x20010DB8000000000000000000000000, 32),   # 2001:db8::/32 documentation
        (0x20020000000000000000000000000000, 16),   # 2002::/16 deprecated 6to4 translation
        (0x3FFF0000000000000000000000000000, 20),   # 3fff::/20 documentation
        (0x5F000000000000000000000000000000, 16),   # 5f00::/16 segment-routing local-use
        (0xFC000000000000000000000000000000, 7),    # fc00::/7 unique-local
        (0xFE800000000000000000000000000000, 10),   # fe80::/10 link-local
        (0xFF000000000000000000000000000000, 8),    # ff00::/8 multicast
    )
    return not any(_prefix_match(value, base, bits, 128) for base, bits in blocked)


def _is_public_ip(value: str) -> bool:
    try:
        return _is_public_ipv4(socket.inet_pton(socket.AF_INET, value))
    except OSError:
        pass
    try:
        return _is_public_ipv6(socket.inet_pton(socket.AF_INET6, value))
    except OSError:
        return False


def _is_clash_fake_ipv4(value: str) -> bool:
    try:
        packed = socket.inet_pton(socket.AF_INET, value)
    except OSError:
        return False
    number = int.from_bytes(packed, "big")
    return _prefix_match(number, 0xC6120000, 15, 32)


def _validated_target(url: str, *, allow_clash_fake_ip: bool = False) -> dict[str, Any]:
    if not isinstance(url, str) or not url:
        raise _fail("DOWNLOAD_URL_INVALID", "url must be a non-empty HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise _fail("DOWNLOAD_URL_INVALID", "Could not parse URL") from exc
    if parsed.scheme.lower() != "https":
        raise _fail("DOWNLOAD_URL_BLOCKED", "Only https:// downloads are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise _fail("DOWNLOAD_URL_BLOCKED", "Credentials embedded in download URLs are not allowed")
    if parsed.fragment:
        raise _fail("DOWNLOAD_URL_BLOCKED", "URL fragments are not allowed")
    host = parsed.hostname
    if not host:
        raise _fail("DOWNLOAD_URL_INVALID", "HTTPS URL must contain a hostname")
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _fail("DOWNLOAD_URL_INVALID", "Hostname is not valid IDNA") from exc
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise _fail("DOWNLOAD_URL_INVALID", "HTTPS port is invalid") from exc
    if port != 443:
        raise _fail("DOWNLOAD_URL_BLOCKED", "Only the standard HTTPS port 443 is allowed")
    try:
        addresses = socket.getaddrinfo(host_ascii, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _fail("DOWNLOAD_DNS_FAILED", "Could not resolve download hostname", host=host_ascii) from exc
    ips: list[str] = []
    for item in addresses:
        sockaddr = item[4]
        if not sockaddr:
            continue
        ip = str(sockaddr[0])
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise _fail("DOWNLOAD_DNS_FAILED", "Download hostname resolved to no addresses", host=host_ascii)
    blocked = [
        ip
        for ip in ips
        if not _is_public_ip(ip)
        and not (allow_clash_fake_ip and host_ascii == "codeload.github.com" and _is_clash_fake_ipv4(ip))
    ]
    if blocked:
        raise _fail(
            "DOWNLOAD_URL_BLOCKED",
            "Download hostname resolves to a non-public address",
            host=host_ascii,
            blocked_addresses=blocked[:8],
        )
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    canonical = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    return {
        "url": canonical,
        "host": host_ascii,
        "port": port,
        "path": path,
        "ip": ips[0],
    }


def _parse_loopback_proxy(value: str) -> tuple[str, int] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    selected = ""
    if ";" in raw or "=" in raw:
        mapping: dict[str, str] = {}
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            key, item = entry.split("=", 1)
            mapping[key.strip().casefold()] = item.strip()
        selected = mapping.get("https") or mapping.get("http") or ""
    else:
        selected = raw
    if not selected:
        return None
    if "://" in selected:
        parsed = urllib.parse.urlsplit(selected)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return None
    else:
        parsed = urllib.parse.urlsplit("//" + selected)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return None
    if not host or port is None or not 1 <= int(port) <= 65535:
        return None
    folded = host.casefold()
    if folded == "localhost":
        return ("127.0.0.1", int(port))
    try:
        packed4 = socket.inet_pton(socket.AF_INET, host)
    except OSError:
        packed4 = None
    if packed4 is not None and packed4[0] == 127:
        return (host, int(port))
    try:
        packed6 = socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        packed6 = None
    if packed6 == (b"\x00" * 15 + b"\x01"):
        return (host, int(port))
    return None


def _windows_ie_proxy_string() -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IEProxyConfig(ctypes.Structure):
            _fields_ = [
                ("auto_detect", wintypes.BOOL),
                ("auto_config_url", ctypes.c_void_p),
                ("proxy", ctypes.c_void_p),
                ("proxy_bypass", ctypes.c_void_p),
            ]

        config = _IEProxyConfig()
        getter = ctypes.windll.winhttp.WinHttpGetIEProxyConfigForCurrentUser
        getter.argtypes = [ctypes.POINTER(_IEProxyConfig)]
        getter.restype = wintypes.BOOL
        if not getter(ctypes.byref(config)):
            return None

        proxy = ctypes.wstring_at(config.proxy) if config.proxy else None
        global_free = ctypes.windll.kernel32.GlobalFree
        global_free.argtypes = [ctypes.c_void_p]
        global_free.restype = ctypes.c_void_p
        for pointer in (config.auto_config_url, config.proxy, config.proxy_bypass):
            if pointer:
                global_free(ctypes.c_void_p(pointer))
        return proxy
    except Exception:
        return None


def _windows_system_proxy() -> tuple[str, int] | None:
    proxy_server = _windows_ie_proxy_string()
    if not proxy_server:
        return None
    return _parse_loopback_proxy(proxy_server)


def _request_once(target: dict[str, Any]) -> _OwnedResponse:
    proxy = _windows_system_proxy()
    if proxy is not None:
        connection = _LoopbackProxyHTTPSConnection(
            str(target["host"]),
            int(target["port"]),
            proxy[0],
            proxy[1],
            timeout=60.0,
        )
    else:
        connection = _PinnedHTTPSConnection(
            str(target["host"]),
            int(target["port"]),
            str(target["ip"]),
            timeout=60.0,
        )
    try:
        connection.request(
            "GET",
            str(target["path"]),
            headers={
                "Host": str(target["host"]),
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        return _OwnedResponse(connection, response)
    except Exception:
        connection.close()
        raise


def _open_download(
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    allow_clash_fake_ip_for_codeload: bool = False,
):
    current = url
    for redirect_count in range(max_redirects + 1):
        target = _validated_target(
            current,
            allow_clash_fake_ip=allow_clash_fake_ip_for_codeload,
        )
        try:
            response = _request_once(target)
        except ExtensionError:
            raise
        except Exception as exc:
            raise _fail(
                "DOWNLOAD_CONNECT_FAILED",
                "HTTPS connection failed",
                host=target["host"],
                exception_type=type(exc).__name__,
            ) from exc
        if response.status in REDIRECT_STATUSES:
            location = response.getheader("Location")
            response.close()
            if not location:
                raise _fail("DOWNLOAD_HTTP_ERROR", "Redirect response omitted Location header")
            if redirect_count >= max_redirects:
                raise _fail("DOWNLOAD_REDIRECT_LIMIT", "HTTPS redirect limit exceeded")
            current = urllib.parse.urljoin(str(target["url"]), str(location))
            continue
        if response.status != 200:
            status = response.status
            response.close()
            raise _fail("DOWNLOAD_HTTP_ERROR", f"HTTPS download returned HTTP {status}", status=status)
        return str(target["url"]), response
    raise _fail("DOWNLOAD_REDIRECT_LIMIT", "HTTPS redirect limit exceeded")


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


def _validate_path_segment(part: str, *, workspace_destination: bool = True) -> None:
    if not part or part in {".", ".."}:
        raise _fail("DOWNLOAD_PATH_INVALID", "Workspace destination contains an ambiguous path segment")
    if ":" in part or any(ord(char) < 32 for char in part):
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Windows ADS/control-character path segments are not allowed", segment=part)
    if part.endswith((" ", ".")):
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Windows-trimmed path segments are not allowed", segment=part)
    folded = part.casefold()
    device_base = folded.split(".", 1)[0].rstrip(" .")
    if device_base in WINDOWS_RESERVED_BASES:
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Windows reserved device names are not allowed", segment=part)
    if folded in VCS_BLOCKED_SEGMENTS:
        raise _fail("DOWNLOAD_PATH_BLOCKED", "VCS control directories are not allowed", segment=part)
    if workspace_destination and folded in WORKSPACE_ONLY_BLOCKED_SEGMENTS:
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Dependency/generated workspace destinations are not allowed", segment=part)
    if folded == ".folderbridge.json":
        raise _fail("DOWNLOAD_PATH_BLOCKED", "FolderBridge control files are not allowed", segment=part)
    if workspace_destination and (folded in SENSITIVE_NAMES or folded.startswith(".env.")):
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Credential/protected destinations are not allowed", segment=part)
    if workspace_destination and Path(part).suffix.casefold() in SENSITIVE_SUFFIXES:
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Credential-like file suffix is not allowed", segment=part)


def _clean_relative(value: str, *, allow_directory: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise _fail("DOWNLOAD_PATH_INVALID", "Workspace destination must be a bounded relative path")
    if "\\" in value or "\x00" in value:
        raise _fail("DOWNLOAD_PATH_INVALID", "Workspace destination must use POSIX-style relative separators")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive:
        raise _fail("DOWNLOAD_PATH_INVALID", "Absolute workspace destinations are not allowed")
    parts = candidate.parts
    if not parts:
        raise _fail("DOWNLOAD_PATH_INVALID", "Workspace destination is empty")
    for part in parts:
        _validate_path_segment(part, workspace_destination=True)
    if not allow_directory and value.endswith("/"):
        raise _fail("DOWNLOAD_PATH_INVALID", "File destination may not end with a separator")
    return Path(*parts)


def _workspace_destination(context: Any, raw: str, *, directory: bool) -> tuple[Path, Path]:
    if not isinstance(context, dict):
        raise _fail("DOWNLOAD_CONTEXT_INVALID", "FolderBridge Extension context must be a mapping")
    root_value = context.get("workspace_root")
    if not root_value:
        raise _fail("DOWNLOAD_WORKSPACE_REQUIRED", "A FolderBridge workspace is required")
    if bool(context.get("workspace_read_only", False)):
        raise _fail("READ_ONLY", "FolderBridge is running read-only")
    root = Path(str(root_value)).resolve()
    relative = _clean_relative(raw, allow_directory=directory)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and _is_linkish(current):
            raise _fail("DOWNLOAD_PATH_BLOCKED", "Destination parent contains a link/reparse point")
    target = root.joinpath(*relative.parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _fail("DOWNLOAD_PATH_INVALID", "Destination escapes workspace") from exc
    if target.exists() and _is_linkish(target):
        raise _fail("DOWNLOAD_PATH_BLOCKED", "Destination is a link/reparse point")
    return relative, target


def _normalize_sha(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise _fail("DOWNLOAD_SHA_INVALID", "expected SHA-256 must be 64 hexadecimal characters")
    return value.lower()


def _bounded_max(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_DOWNLOAD_BYTES:
        raise _fail("DOWNLOAD_LIMIT_INVALID", f"max bytes must be between 1 and {MAX_DOWNLOAD_BYTES}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise _fail(
            "DOWNLOAD_TARGET_READ_FAILED",
            "Could not hash existing destination",
            exception_type=type(exc).__name__,
        ) from exc
    return digest.hexdigest()


def _public_source_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _publish_file(
    temp_path: Path,
    target: Path,
    *,
    overwrite: bool,
    expected_target_sha256: str | None,
) -> None:
    if target.exists():
        if not overwrite:
            raise _fail("DOWNLOAD_EXISTS", "Destination already exists; overwrite is false")
        if expected_target_sha256 is None:
            raise _fail("DOWNLOAD_TARGET_SHA_REQUIRED", "overwrite=true requires expected_target_sha256")
        if not target.is_file() or _is_linkish(target):
            raise _fail("DOWNLOAD_PATH_BLOCKED", "Existing destination is not a safe regular file")
        actual_target_sha256 = _sha256_file(target)
        if actual_target_sha256 != expected_target_sha256:
            raise _fail(
                "DOWNLOAD_TARGET_STALE",
                "Existing destination changed; refusing overwrite",
                expected_target_sha256=expected_target_sha256,
                actual_target_sha256=actual_target_sha256,
            )
        os.replace(temp_path, target)
        return
    if overwrite:
        raise _fail("DOWNLOAD_TARGET_STALE", "overwrite=true requires the expected destination to still exist")
    try:
        os.link(temp_path, target)
    except FileExistsError as exc:
        raise _fail("DOWNLOAD_EXISTS", "Destination appeared before publish; refusing to clobber") from exc
    except OSError as exc:
        raise _fail(
            "DOWNLOAD_PUBLISH_FAILED",
            "Atomic no-clobber publish requires same-filesystem hard-link support",
            exception_type=type(exc).__name__,
        ) from exc
    else:
        temp_path.unlink(missing_ok=True)


def _stream_response_to_file(
    response: Any,
    target: Path,
    *,
    max_bytes: int,
    expected_sha256: str | None,
    overwrite: bool,
    expected_target_sha256: str | None,
) -> tuple[int, str]:
    length = response.getheader("Content-Length")
    if length not in (None, ""):
        try:
            declared = int(length)
        except (TypeError, ValueError):
            declared = -1
        if declared > max_bytes:
            raise _fail("DOWNLOAD_TOO_LARGE", "Declared download size exceeds max_bytes", declared_bytes=declared, max_bytes=max_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.parent
    while True:
        if current.exists() and _is_linkish(current):
            raise _fail("DOWNLOAD_PATH_BLOCKED", "Destination parent became a link/reparse point")
        if current.parent == current:
            break
        current = current.parent
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.download-", dir=str(target.parent))
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _fail("DOWNLOAD_TOO_LARGE", "Downloaded bytes exceed max_bytes", received_bytes=total, max_bytes=max_bytes)
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise _fail(
                "DOWNLOAD_SHA_MISMATCH",
                "Downloaded file SHA-256 does not match expected_sha256",
                expected_sha256=expected_sha256,
                actual_sha256=actual,
            )
        _publish_file(
            temp_path,
            target,
            overwrite=overwrite,
            expected_target_sha256=expected_target_sha256,
        )
        return total, actual
    finally:
        temp_path.unlink(missing_ok=True)


def _download_action(params: dict[str, Any], context: Any) -> dict[str, Any]:
    relative, target = _workspace_destination(context, str(params.get("local_path") or ""), directory=False)
    overwrite = bool(params.get("overwrite", False))
    if target.exists() and not overwrite:
        raise _fail("DOWNLOAD_EXISTS", "Destination already exists; overwrite is false")
    expected = _normalize_sha(params.get("expected_sha256"))
    expected_target = _normalize_sha(params.get("expected_target_sha256"))
    if overwrite and expected_target is None:
        raise _fail("DOWNLOAD_TARGET_SHA_REQUIRED", "overwrite=true requires expected_target_sha256")
    if not overwrite and expected_target is not None:
        raise _fail("DOWNLOAD_TARGET_SHA_INVALID", "expected_target_sha256 is only valid with overwrite=true")
    if overwrite:
        if not target.exists() or not target.is_file() or _is_linkish(target):
            raise _fail("DOWNLOAD_TARGET_STALE", "Expected overwrite destination is unavailable or unsafe")
        actual_target = _sha256_file(target)
        if actual_target != expected_target:
            raise _fail(
                "DOWNLOAD_TARGET_STALE",
                "Existing destination does not match expected_target_sha256",
                expected_target_sha256=expected_target,
                actual_target_sha256=actual_target,
            )
    max_bytes = _bounded_max(params.get("max_bytes"), default=256 * 1024 * 1024)
    final_url, response = _open_download(str(params.get("url") or ""))
    try:
        size, digest = _stream_response_to_file(
            response,
            target,
            max_bytes=max_bytes,
            expected_sha256=expected,
            overwrite=overwrite,
            expected_target_sha256=expected_target,
        )
    finally:
        response.close()
    return {
        "ok": True,
        "source_url": _public_source_url(final_url),
        "path": relative.as_posix(),
        "size": size,
        "sha256": digest,
        "workspace_artifacts": [{"path": relative.as_posix(), "label": "download", "kind": "file"}],
    }


def _github_identity(owner: str, repo: str, ref: str) -> tuple[str, str, str]:
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        raise _fail("GITHUB_IDENTITY_INVALID", "GitHub owner is invalid")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or repo in {".", ".."}:
        raise _fail("GITHUB_IDENTITY_INVALID", "GitHub repository name is invalid")
    if (
        not isinstance(ref, str)
        or not REF_RE.fullmatch(ref)
        or ref.startswith("/")
        or ref.endswith("/")
        or ".." in ref.split("/")
    ):
        raise _fail("GITHUB_REF_INVALID", "GitHub ref is invalid")
    return owner, repo, ref


def _zip_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise _fail("GITHUB_ARCHIVE_UNSAFE", "Archive contains an unsafe member path")
    pure = Path(name)
    parts = pure.parts
    if not parts:
        raise _fail("GITHUB_ARCHIVE_UNSAFE", "Archive contains an empty member path")
    for part in parts:
        _validate_path_segment(part, workspace_destination=False)
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        raise _fail("GITHUB_ARCHIVE_UNSAFE", "Archive contains a symbolic link")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise _fail("GITHUB_ARCHIVE_UNSAFE", "Archive contains a non-regular special file")
    if info.flag_bits & 0x1:
        raise _fail("GITHUB_ARCHIVE_UNSAFE", "Encrypted archive members are not supported")
    return tuple(parts)


def _extract_github_zip(
    archive: Path,
    staging: Path,
    *,
    max_expanded_bytes: int,
    max_files: int = 20000,
) -> dict[str, int]:
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_files + 4096:
                raise _fail("GITHUB_ARCHIVE_TOO_LARGE", "Archive contains too many entries")
            validated: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            roots: set[str] = set()
            total = 0
            files = 0
            for info in infos:
                parts = _zip_member_parts(info)
                roots.add(parts[0])
                if info.is_dir():
                    validated.append((info, parts))
                    continue
                files += 1
                if files > max_files:
                    raise _fail("GITHUB_ARCHIVE_TOO_LARGE", "Archive file count exceeds max_files", max_files=max_files)
                total += int(info.file_size)
                if total > max_expanded_bytes:
                    raise _fail(
                        "GITHUB_ARCHIVE_TOO_LARGE",
                        "Expanded archive bytes exceed max_expanded_bytes",
                        expanded_bytes=total,
                        max_expanded_bytes=max_expanded_bytes,
                    )
                validated.append((info, parts))
            if len(roots) != 1:
                raise _fail("GITHUB_ARCHIVE_SHAPE", "GitHub snapshot must contain exactly one top-level directory")
            for info, parts in validated:
                relative_parts = parts[1:]
                if not relative_parts:
                    continue
                output = staging.joinpath(*relative_parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as source, output.open("xb") as dest:
                    copied = 0
                    while True:
                        chunk = source.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise _fail("GITHUB_ARCHIVE_INVALID", "Archive member expanded beyond declared size")
                        dest.write(chunk)
                if copied != info.file_size:
                    raise _fail("GITHUB_ARCHIVE_INVALID", "Archive member size did not match declaration")
        return {"files": files, "expanded_bytes": total}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _download_to_temp(
    url: str,
    *,
    max_bytes: int,
    expected_sha256: str | None,
    allow_clash_fake_ip_for_codeload: bool = False,
) -> tuple[Path, int, str, str]:
    final_url, response = _open_download(
        url,
        allow_clash_fake_ip_for_codeload=allow_clash_fake_ip_for_codeload,
    )
    fd, name = tempfile.mkstemp(prefix="folderbridge-download-", suffix=".zip")
    temp_path = Path(name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _fail("DOWNLOAD_TOO_LARGE", "Downloaded archive exceeds max_archive_bytes")
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise _fail("DOWNLOAD_SHA_MISMATCH", "GitHub archive SHA-256 mismatch", expected_sha256=expected_sha256, actual_sha256=actual)
        return temp_path, total, actual, final_url
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def _github_snapshot_action(params: dict[str, Any], context: Any) -> dict[str, Any]:
    owner, repo, ref = _github_identity(
        str(params.get("owner") or ""),
        str(params.get("repo") or ""),
        str(params.get("ref") or "main"),
    )
    relative, destination = _workspace_destination(
        context,
        str(params.get("destination_dir") or ""),
        directory=True,
    )
    if destination.exists():
        raise _fail("DOWNLOAD_EXISTS", "GitHub snapshot destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    max_archive = _bounded_max(params.get("max_archive_bytes"), default=256 * 1024 * 1024)
    max_expanded = _bounded_max(params.get("max_expanded_bytes"), default=MAX_DOWNLOAD_BYTES)
    max_files = params.get("max_files", 20000)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= 50000:
        raise _fail("GITHUB_LIMIT_INVALID", "max_files must be between 1 and 50000")
    expected = _normalize_sha(params.get("expected_archive_sha256"))
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{encoded_ref}"
    archive, archive_bytes, archive_sha, final_url = _download_to_temp(
        url,
        max_bytes=max_archive,
        expected_sha256=expected,
        allow_clash_fake_ip_for_codeload=True,
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.snapshot-", dir=str(destination.parent)))
    staging.rmdir()
    try:
        stats = _extract_github_zip(
            archive,
            staging,
            max_expanded_bytes=max_expanded,
            max_files=max_files,
        )
        if destination.exists():
            raise _fail("DOWNLOAD_EXISTS", "GitHub snapshot destination appeared before publish")
        try:
            os.rename(staging, destination)
        except OSError as exc:
            raise _fail("DOWNLOAD_PUBLISH_FAILED", "Could not atomically publish GitHub snapshot directory", exception_type=type(exc).__name__) from exc
        return {
            "ok": True,
            "owner": owner,
            "repo": repo,
            "ref": ref,
            "source_url": final_url,
            "destination_dir": relative.as_posix(),
            "archive_bytes": archive_bytes,
            "archive_sha256": archive_sha,
            "files": stats["files"],
            "expanded_bytes": stats["expanded_bytes"],
            "executed_repository_code": False,
            "included_git_metadata": False,
        }
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def handle(action: str, params: dict[str, Any], context: Any) -> dict[str, Any]:
    if action == "download":
        return _download_action(params, context)
    if action == "github-snapshot":
        return _github_snapshot_action(params, context)
    raise _fail("DOWNLOAD_ACTION_UNSUPPORTED", f"Unsupported action: {action}")
