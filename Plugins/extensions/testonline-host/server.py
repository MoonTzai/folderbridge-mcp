from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit

from tls_cert import ensure_certificate, CertificateError

PLUGIN_ID = "testonline-host"
VERSION = "0.1.2"
FOLDER_PATTERN = re.compile(r"^([0-9]{4,5})-(http|https)$", re.IGNORECASE)
ALLOWED_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".webmanifest", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".txt", ".webm", ".mp3", ".mp4",
    ".pdf",
})
MAX_FILE_BYTES = 256 * 1024 * 1024
BOOTSTRAP_PORT = 18888
STATUS_PORT = 18889


class MultiThreadHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def unsafe_component(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attr = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attr & 0x400)  # Windows FILE_ATTRIBUTE_REPARSE_POINT / junction
    except OSError:
        return True


def safe_file(folder: Path, raw_url: str) -> Path | None:
    if unsafe_component(folder):
        return None
    try:
        pathname = unquote(urlsplit(raw_url).path)
    except ValueError:
        return None
    if "\x00" in pathname or "\\" in pathname:
        return None
    parts = pathname.strip("/").split("/")
    if any(part in {"..", "."} or part.startswith(".") or ":" in part for part in parts):
        return None
    if pathname.endswith("/") or pathname == "/":
        # Prefer conventional entrypoint; otherwise render an HTML-only link list.
        parts = ["index.html"]
    target = folder.joinpath(*parts)
    try:
        if not target.is_relative_to(folder):
            return None
        current = folder
        for part in parts:
            current = current / part
            if unsafe_component(current):
                return None
        if not target.is_file() or target.suffix.lower() not in ALLOWED_SUFFIXES:
            return None
        if target.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    return target


def public_html_list(folder: Path, bind_ip: str, port: int, scheme: str) -> bytes:
    from html import escape
    links = []
    if unsafe_component(folder):
        raise OSError("Linked hosting folder refused")
    for f in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if f.is_file() and not unsafe_component(f) and f.suffix.lower() in {".html", ".htm"}:
            from urllib.parse import quote
            links.append('<li><a href="/' + quote(f.name) + '">' + escape(f.name) + "</a></li>")
    page = ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>TestOnline · HTML 文件</title>"
            "<body style='font:16px/1.8 system-ui;padding:20px'><h2>TestOnline</h2><ul>"
            + "".join(links) + "</ul></body></html>")
    return page.encode("utf-8")


class HostingManager:
    def __init__(self, root: Path, bind_ip: str, state_dir: Path):
        self.root = root.absolute()
        self.bind_ip = str(ipaddress.IPv4Address(bind_ip))
        self.network = ipaddress.IPv4Network(self.bind_ip + "/24", strict=False)
        self.state_dir = state_dir
        self.ca_file = state_dir / "certificates" / "testonline-root.cer"
        self.ssl_context: ssl.SSLContext | None = None
        self.lock = threading.RLock()
        self.servers: dict[int, tuple[str, Path, MultiThreadHTTPServer, threading.Thread]] = {}
        self.errors: dict[str, str] = {}
        self.started = time.monotonic()
        self.scanned_at = ""
        self.stop_flag = threading.Event()

    def same_lan(self, remote: str) -> bool:
        if remote in {"127.0.0.1", "::1"}:
            return True
        try:
            return ipaddress.IPv4Address(remote) in self.network
        except ValueError:
            return False

    def status(self) -> dict:
        with self.lock:
            return {
                "service_id": PLUGIN_ID,
                "version": VERSION,
                "online": True,
                "status": "READY",
                "bind_ip": self.bind_ip,
                "publish_root": str(self.root),
                "bootstrap_url": f"http://{self.bind_ip}:{BOOTSTRAP_PORT}/",
                "root_cert_url": f"http://{self.bind_ip}:{BOOTSTRAP_PORT}/CA",
                "root_cert_sha256": (
                    hashlib.sha256(self.ca_file.read_bytes()).hexdigest() if self.ca_file.is_file() else None
                ),
                "sites": [
                    {"folder": folder.name, "port": port, "mode": mode,
                     "url": f"{mode}://{self.bind_ip}:{port}/"}
                    for port, (mode, folder, _, _) in sorted(self.servers.items())
                ],
                "errors": [{"name": k, "error": v} for k, v in sorted(self.errors.items())],
                "scanned_at": self.scanned_at,
                "uptime_seconds": round(time.monotonic() - self.started, 1),
            }

    def serve_handler(self, folder: Path):
        manager = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_HEAD(self):
                self._serve(True)

            def do_GET(self):
                self._serve(False)

            def _send(self, status: int, content: bytes, kind: str, head: bool = False):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                if not head:
                    self.wfile.write(content)

            def _serve(self, head: bool):
                if not manager.same_lan(self.client_address[0]):
                    return self._send(403, b"LAN only", "text/plain", head)
                target = safe_file(folder, self.path)
                parsed = urlsplit(self.path)
                if target is None and parsed.path == "/":
                    try:
                        return self._send(200, public_html_list(folder, manager.bind_ip, self.server.server_port,
                                      "https" if isinstance(self.connection, ssl.SSLSocket) else "http"),
                                          "text/html; charset=utf-8", head)
                    except OSError:
                        pass
                if target is None:
                    return self._send(404, b"Not found", "text/plain", head)
                try:
                    size = target.stat().st_size
                    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if target.suffix.lower() in {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".txt"}:
                        mime += "; charset=utf-8"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store, max-age=0")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    if not head:
                        with target.open("rb") as source:
                            while (chunk := source.read(64 * 1024)):
                                self.wfile.write(chunk)
                except (OSError, ConnectionError, BrokenPipeError):
                    self.close_connection = True

        return Handler

    def system_handler(self, loopback_only: bool):
        manager = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_HEAD(self):
                self._serve(True)

            def do_GET(self):
                self._serve(False)

            def do_POST(self):
                # The plugin owns the refresh UI. Only a browser on the host PC
                # (same-origin portal) or a loopback control client may rescan.
                local = (self.client_address[0] == "127.0.0.1" if loopback_only
                         else self.client_address[0] == manager.bind_ip)
                if not local:
                    return self._send(403, b"Local only", "text/plain", False)
                if self.path != "/rescan":
                    return self._send(404, b"Not found", "text/plain", False)
                origin = self.headers.get("Origin")
                expected_origin = f"http://{manager.bind_ip}:{BOOTSTRAP_PORT}"
                if ((origin not in (None, "") if loopback_only else origin != expected_origin)
                        or self.headers.get("Transfer-Encoding")
                        or self.headers.get("Content-Length", "0") != "0"
                        or self.headers.get("X-FolderBridge-Rescan") != "1"):
                    return self._send(403, b"Invalid rescan request", "text/plain", False)
                manager.scan()
                raw = json.dumps(manager.status(), ensure_ascii=False).encode("utf-8")
                return self._send(200, raw, "application/json; charset=utf-8", False)

            def _serve(self, head: bool):
                if loopback_only:
                    if self.client_address[0] != "127.0.0.1":
                        return self._send(403, b"Local only", "text/plain", head)
                elif not manager.same_lan(self.client_address[0]):
                    return self._send(403, b"LAN only", "text/plain", head)
                p = urlsplit(self.path).path
                if p == "/status":
                    raw = json.dumps(manager.status(), ensure_ascii=False).encode("utf-8")
                    return self._send(200, raw, "application/json; charset=utf-8", head)
                if not loopback_only and p in {"/CA", "/testonline-root.cer"} and manager.ca_file.is_file():
                    return self._send(200, manager.ca_file.read_bytes(), "application/x-x509-ca-cert", head,
                                      filename="testonline-root.cer")
                if not loopback_only and p == "/":
                    from html import escape
                    data = manager.status()
                    links = "".join(
                        "<li><a href='" + escape(row["url"], quote=True) + "'>" +
                        escape(row["folder"]) + " — " + escape(row["url"]) + "</a></li>"
                        for row in data["sites"]
                    )
                    errors = "".join("<li>" + escape(x["name"]) + ": " + escape(x["error"]) + "</li>" for x in data["errors"])
                    cert_link = (
                        "<p><a href='/CA'>下载 HTTPS 测试根证书（公开证书，不含私钥）</a></p>"
                        if manager.ca_file.is_file() else ""
                    )
                    refresh = (
                        "<p><button type='button' id='refresh-sites'>刷新站点</button>"
                        " <span id='refresh-result' role='status'></span></p>"
                        "<script>document.getElementById('refresh-sites').addEventListener('click',"
                        "async function(){this.disabled=true;const note=document.getElementById('refresh-result');"
                        "note.textContent='正在刷新…';try{const r=await fetch('/rescan',"
                        "{method:'POST',headers:{'X-FolderBridge-Rescan':'1'},body:'' });"
                        "if(!r.ok)throw Error('HTTP '+r.status);location.reload();}"
                        "catch(e){note.textContent='刷新失败：'+e.message;this.disabled=false;}});</script>"
                        if self.client_address[0] == manager.bind_ip else ""
                    )
                    html = ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
                            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                            "<title>TestOnline</title><body style='font:16px/1.8 system-ui;padding:24px'>"
                            "<h2>FolderBridge · TestOnline</h2>" + refresh + "<ul>" + links + "</ul>" +
                            cert_link + ("<h3>不可用</h3><ul>" + errors + "</ul>" if errors else "") +
                            "</body></html>")
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", head)
                return self._send(404, b"Not found", "text/plain", head)

            def _send(self, status, content, kind, head, *, filename=None):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                if filename is not None:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head:
                    self.wfile.write(content)

        return Handler

    def prepare_tls(self):
        if self.ssl_context:
            return
        cert, key = ensure_certificate(self.state_dir / "certificates", self.bind_ip)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        self.ssl_context = ctx

    def start_site(self, port: int, mode: str, folder: Path):
        if mode == "https":
            self.prepare_tls()
        server = MultiThreadHTTPServer((self.bind_ip, port), self.serve_handler(folder))
        if mode == "https":
            assert self.ssl_context is not None
            server.socket = self.ssl_context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, name=f"testonline-{port}", daemon=True)
        worker.start()
        return mode, folder, server, worker

    def scan(self):
        desired: dict[int, tuple[str, Path]] = {}
        errors: dict[str, str] = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            children = list(self.root.iterdir())
        except OSError as exc:
            with self.lock:
                self.errors = {"TestOnline": str(exc)[:200]}
            return
        for item in children:
            match = FOLDER_PATTERN.fullmatch(item.name)
            if match is None:
                continue
            port = int(match.group(1))
            mode = match.group(2).lower()
            if port < 1024 or port > 65535 or port in {BOOTSTRAP_PORT, STATUS_PORT}:
                errors[item.name] = "Invalid or reserved port"
                continue
            if unsafe_component(item) or not item.is_dir():
                errors[item.name] = "Symlink/junction/non-directory refused"
                continue
            if port in desired:
                errors[item.name] = "Duplicate TCP port"
                continue
            desired[port] = mode, item
        with self.lock:
            for port, (mode, folder, server, thread) in list(self.servers.items()):
                if port not in desired or desired[port] != (mode, folder):
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
                    del self.servers[port]
            for port, (mode, folder) in desired.items():
                if port in self.servers:
                    continue
                try:
                    self.servers[port] = self.start_site(port, mode, folder)
                except (OSError, CertificateError, ssl.SSLError) as exc:
                    errors[folder.name] = str(exc)[:220]
            self.errors = errors
            self.scanned_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def run(self):
        local = MultiThreadHTTPServer(("127.0.0.1", STATUS_PORT), self.system_handler(True))
        bootstrap = MultiThreadHTTPServer((self.bind_ip, BOOTSTRAP_PORT), self.system_handler(False))
        tlocal = threading.Thread(target=local.serve_forever, daemon=True)
        tbootstrap = threading.Thread(target=bootstrap.serve_forever, daemon=True)
        tlocal.start()
        tbootstrap.start()
        try:
            # Initial scan happens in serve(); later rescans require explicit POST /rescan.
            self.stop_flag.wait()
        finally:
            with self.lock:
                for _, _, srv, thread in self.servers.values():
                    srv.shutdown()
                    srv.server_close()
                    thread.join(timeout=2)
                self.servers.clear()
            local.shutdown()
            bootstrap.shutdown()
            local.server_close()
            bootstrap.server_close()


def serve(root: Path, bind_ip: str, state_dir: Path) -> None:
    """Stable plugin entrypoint for the FolderBridge-managed service child."""
    root = root.absolute()
    if unsafe_component(root):
        raise RuntimeError("TestOnline root cannot be symlink/reparse point")
    manager = HostingManager(root, bind_ip, state_dir.absolute())
    manager.scan()
    manager.run()


def main():
    parser = argparse.ArgumentParser(description="FolderBridge TestOnline LAN hosting")
    parser.add_argument("--root", required=True)
    parser.add_argument("--bind-ip", required=True, help="Private LAN address supplied by FolderBridge Launcher")
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    serve(Path(args.root), args.bind_ip, Path(args.state_dir))


if __name__ == "__main__":
    main()
