from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from folderbridge_mcp.extension_api import ExtensionError, owned_process_group_kwargs

HOST = "127.0.0.1"
DEVTOOLS_PORT = 8771
FRESH_CHAT_MIN_INTERVAL_SECONDS = 20
FRESH_CHAT_WINDOW_SECONDS = 300
FRESH_CHAT_WINDOW_MAX = 8
NAVIGATION_MIN_INTERVAL_SECONDS = 20
HISTORY_COOLDOWN_BASE_SECONDS = 120
HISTORY_COOLDOWN_MAX_SECONDS = 600
HISTORY_COOLDOWN_RESET_SECONDS = 1800
COMPOSERS = [
    "#prompt-textarea",
    'textarea[data-testid="prompt-textarea"]',
    'div[contenteditable="true"][data-testid="prompt-textarea"]',
    'div[contenteditable="true"]',
]
SEND_BUTTONS = [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
]
STOP_BUTTONS = [
    'button[data-testid="stop-button"]',
    'button[aria-label*="Stop"]',
    'button[aria-label*="停止"]',
]
HISTORY_RATE_LIMIT_PHRASES = [
    "你的请求过于频繁",
    "暂时限制你访问对话记录",
    "请稍等几分钟后再重试",
    "your requests are too frequent",
    "temporarily restricted your access to conversation history",
    "please wait a few minutes and try again",
]
CID_RE = re.compile(r"/c/([^/?#]+)")


def _extension_error(code: str, message: str, *, retryable: bool = False, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, details=details or None, retryable=retryable)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(5))
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _opener():
    return build_opener(ProxyHandler({}))


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
    request = Request(url, method=method, headers={"Accept": "application/json"})
    with _opener().open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _selector_array(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _composer_present_js() -> str:
    return f"(() => {_selector_array(COMPOSERS)}.some(s => !!document.querySelector(s)))()"


def _set_composer_js(text: str) -> str:
    return f"""(() => {{
      const selectors={_selector_array(COMPOSERS)};
      const value={json.dumps(text, ensure_ascii=False)};
      const el=selectors.map(s=>document.querySelector(s)).find(Boolean);
      if(!el)return false;
      el.focus();
      if(el instanceof HTMLTextAreaElement){{
        const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')?.set;
        if(setter)setter.call(el,value);else el.value=value;
        el.dispatchEvent(new Event('input',{{bubbles:true}}));
      }}else{{
        el.textContent='';
        const inserted=document.execCommand?.('insertText',false,value);
        if(!inserted)el.textContent=value;
        el.dispatchEvent(new InputEvent('input',{{bubbles:true,inputType:'insertText',data:value}}));
      }}
      return true;
    }})()"""


def _send_js() -> str:
    return f"""(() => {{
      const selectors={_selector_array(SEND_BUTTONS)};
      const btn=selectors.map(s=>document.querySelector(s)).find(Boolean);
      if(!btn||btn.disabled)return false;
      btn.click();
      return true;
    }})()"""


def _thread_state_js() -> str:
    return f"""(() => {{
      const rows=[...document.querySelectorAll('[data-message-author-role]')].map(el=>({{
        role:el.getAttribute('data-message-author-role')||'',
        text:(el.innerText||el.textContent||'').trim()
      }}));
      const stopping={_selector_array(STOP_BUTTONS)}.some(s=>!!document.querySelector(s));
      return {{rows,stopping,url:location.href}};
    }})()"""


def _history_guard_js() -> str:
    return f"""(() => {{
      const phrases={json.dumps(HISTORY_RATE_LIMIT_PHRASES, ensure_ascii=False)}.map(x=>x.toLowerCase());
      const selectors=['[role="alert"]','[role="dialog"]','[data-sonner-toast]','[data-testid*="toast"]','[aria-live="assertive"]','[aria-live="polite"]'];
      const visible=el=>{{const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;}};
      const parts=[...document.querySelectorAll(selectors.join(','))].filter(visible).map(el=>el.innerText||el.textContent||'');
      if(!{_composer_present_js()} && document.body)parts.push(document.body.innerText||document.body.textContent||'');
      const text=parts.join('\n').toLowerCase();
      return phrases.some(p=>text.includes(p));
    }})()"""


class RawWebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("CDP websocket must be local ws:// only")
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buffer = bytearray()
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        header = self._read_until(b"\r\n\r\n", 65536)
        lines = header.decode("latin1", errors="replace").split("\r\n")
        if not lines or " 101 " not in (" " + lines[0] + " "):
            raise RuntimeError("Chrome DevTools websocket upgrade failed")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("Chrome DevTools websocket accept key mismatch")

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed unexpectedly")
            self.buffer.extend(chunk)
            if len(self.buffer) > limit:
                raise RuntimeError("websocket handshake exceeded limit")
        index = self.buffer.index(marker) + len(marker)
        output = bytes(self.buffer[:index])
        del self.buffer[:index]
        return output

    def _recv_raw(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise RuntimeError("CDP websocket closed unexpectedly")
            self.buffer.extend(chunk)
        output = bytes(self.buffer[:size])
        del self.buffer[:size]
        return output

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        header = bytearray([first])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        fragments = bytearray()
        started = False
        while True:
            first, second = self._recv_raw(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_raw(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_raw(8))[0]
            mask = self._recv_raw(4) if masked else None
            payload = self._recv_raw(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("CDP websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                started = True
            elif opcode == 0x0 and started:
                fragments.extend(payload)
            else:
                continue
            if fin:
                return fragments.decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class CdpSession:
    def __init__(self, url: str, timeout: float = 30.0):
        self.websocket = RawWebSocket(url, timeout=timeout)
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.websocket.send_text(json.dumps({"id": request_id, "method": method, "params": params or {}}, separators=(",", ":")))
        while True:
            message = json.loads(self.websocket.recv_text())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result") or {}

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise RuntimeError("ChatGPT page JavaScript evaluation failed")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.websocket.close()


def _profile_dir(state_dir: Path) -> Path:
    return state_dir / "storyboard-chatgpt-web-browser-profile-v1"


def _safety_path(state_dir: Path) -> Path:
    return state_dir / "storyboard-chatgpt-web-safety.json"


def _locate_browser() -> Path:
    drive = os.environ.get("SYSTEMDRIVE") or "C:"
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates = [
        Path(drive + r"\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(drive + r"\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(drive + r"\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(drive + r"\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    if local:
        candidates.extend([
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ])
    for candidate in candidates:
        if candidate.is_file() and candidate.name.lower() in {"chrome.exe", "msedge.exe"}:
            return candidate
    raise _extension_error("BROWSER_NOT_FOUND", "No supported ordinary Chrome or Edge executable was found")


def _devtools_online() -> bool:
    try:
        value = _http_json(f"http://{HOST}:{DEVTOOLS_PORT}/json/version", timeout=2.0)
        return isinstance(value, dict)
    except Exception:
        return False


def _ensure_browser(state_dir: Path) -> None:
    if _devtools_online():
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOST, DEVTOOLS_PORT))
    except OSError as exc:
        raise _extension_error("DEVTOOLS_PORT_BUSY", f"Dedicated Storyboard ChatGPT Web DevTools port {DEVTOOLS_PORT} is busy", retryable=True) from exc
    finally:
        probe.close()
    profile = _profile_dir(state_dir)
    profile.mkdir(parents=True, exist_ok=True)
    executable = _locate_browser()
    subprocess.Popen(
        [
            str(executable),
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={DEVTOOLS_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-extensions",
            "--disable-features=Translate",
            "--start-maximized",
            # Browser startup itself must not cause a ChatGPT home/history navigation.
            # A later guarded bind/reopen owns the first provider navigation lease.
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **owned_process_group_kwargs(hide_window=False),
    )
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if _devtools_online():
            return
        time.sleep(0.25)
    raise _extension_error("DEVTOOLS_UNAVAILABLE", "Dedicated Storyboard ChatGPT Web browser did not expose DevTools in time", retryable=True)


def _new_target(url: str) -> dict[str, Any]:
    encoded = quote(url, safe="")
    endpoint = f"http://{HOST}:{DEVTOOLS_PORT}/json/new?{encoded}"
    try:
        value = _http_json(endpoint, method="PUT", timeout=10.0)
    except Exception:
        value = _http_json(endpoint, method="GET", timeout=10.0)
    if not isinstance(value, dict) or not value.get("webSocketDebuggerUrl") or not value.get("id"):
        raise _extension_error("DEVTOOLS_TARGET_FAILED", "Chrome did not create a debuggable ChatGPT tab", retryable=True)
    return value


def _close_target(target_id: str) -> None:
    try:
        _http_json(f"http://{HOST}:{DEVTOOLS_PORT}/json/close/{quote(target_id, safe='')}", timeout=3.0)
    except Exception:
        pass


def _history_limited(session: CdpSession) -> bool:
    try:
        return bool(session.evaluate(_history_guard_js()))
    except Exception:
        return False


def _load_safety(state_dir: Path) -> dict[str, Any]:
    state = _read_json(_safety_path(state_dir)) or {}
    return {
        "starts": [float(x) for x in state.get("starts", []) if isinstance(x, (int, float))],
        "next_start_at": float(state.get("next_start_at") or 0),
        "navigations": [float(x) for x in state.get("navigations", []) if isinstance(x, (int, float))],
        "next_navigation_at": float(state.get("next_navigation_at") or 0),
        "last_navigation_kind": str(state.get("last_navigation_kind") or ""),
        "cooldown_until": float(state.get("cooldown_until") or 0),
        "strikes": int(state.get("strikes") or 0),
        "last_rate_limit_at": float(state.get("last_rate_limit_at") or 0),
    }


def _save_safety(state_dir: Path, state: dict[str, Any]) -> None:
    _atomic_json(_safety_path(state_dir), state)


def _prune_safety(state: dict[str, Any], now: float) -> None:
    state["starts"] = [x for x in state["starts"] if x > now - FRESH_CHAT_WINDOW_SECONDS]
    state["navigations"] = [x for x in state["navigations"] if x > now - FRESH_CHAT_WINDOW_SECONDS]
    if state["last_rate_limit_at"] and now - state["last_rate_limit_at"] >= HISTORY_COOLDOWN_RESET_SECONDS:
        state["strikes"] = 0
        state["last_rate_limit_at"] = 0


def _history_safe_mode(state: dict[str, Any], now: float) -> str:
    # Runtime safety is intentionally simple: only an actual provider cooldown
    # blocks work. Old probe/rebind bookkeeping is retained only for backward-
    # compatible state-file reads and is not a production gate.
    return "COOLDOWN" if state["cooldown_until"] > now else "NORMAL"


def safety_state(state_dir: Path) -> dict[str, Any]:
    now = time.time()
    state = _load_safety(state_dir)
    _prune_safety(state, now)
    fresh_earliest = max(state["next_start_at"], state["next_navigation_at"], state["cooldown_until"])
    if len(state["starts"]) >= FRESH_CHAT_WINDOW_MAX:
        fresh_earliest = max(fresh_earliest, state["starts"][0] + FRESH_CHAT_WINDOW_SECONDS)
    navigation_earliest = max(state["next_navigation_at"], state["cooldown_until"])
    return {
        "fresh_chat_min_interval_seconds": FRESH_CHAT_MIN_INTERVAL_SECONDS,
        "fresh_chat_window_seconds": FRESH_CHAT_WINDOW_SECONDS,
        "fresh_chat_window_max": FRESH_CHAT_WINDOW_MAX,
        "fresh_chat_starts_in_window": len(state["starts"]),
        "fresh_chat_next_start_in_seconds": max(0, int(fresh_earliest - now + 0.999)),
        "history_sensitive_navigation_min_interval_seconds": NAVIGATION_MIN_INTERVAL_SECONDS,
        "history_sensitive_navigations_in_window": len(state["navigations"]),
        "history_sensitive_navigation_next_in_seconds": max(0, int(navigation_earliest - now + 0.999)),
        "history_cooldown_remaining_seconds": max(0, int(state["cooldown_until"] - now + 0.999)),
        "history_rate_limit_strikes": state["strikes"],
        "history_safe_state": _history_safe_mode(state, now),
        "last_navigation_kind": state["last_navigation_kind"] or None,
        "shared_account_budget": "unavailable",
    }


def _reserve_navigation(state_dir: Path, kind: str, *, fresh_chat: bool = False) -> None:
    status = safety_state(state_dir)
    mode = str(status["history_safe_state"])
    if mode == "COOLDOWN":
        wait = int(status["history_cooldown_remaining_seconds"])
        raise _extension_error("HISTORY_RATE_LIMITED", f"ChatGPT history-sensitive navigation is blocked during cooldown for {wait} seconds", retryable=True, retry_after_seconds=wait)
    wait = int(status["history_sensitive_navigation_next_in_seconds"])
    if fresh_chat:
        wait = max(wait, int(status["fresh_chat_next_start_in_seconds"]))
    if wait > 0:
        code = "FRESH_CHAT_COOLDOWN" if fresh_chat else "NAVIGATION_COOLDOWN"
        raise _extension_error(code, f"ChatGPT history-sensitive navigation is cooling down for {wait} seconds", retryable=True, retry_after_seconds=wait)
    now = time.time()
    state = _load_safety(state_dir)
    _prune_safety(state, now)
    state["navigations"].append(now)
    state["next_navigation_at"] = now + NAVIGATION_MIN_INTERVAL_SECONDS
    state["last_navigation_kind"] = kind
    if fresh_chat:
        state["starts"].append(now)
        state["next_start_at"] = now + FRESH_CHAT_MIN_INTERVAL_SECONDS
    _save_safety(state_dir, state)


def _reserve_fresh_chat(state_dir: Path) -> None:
    _reserve_navigation(state_dir, "fresh_bind", fresh_chat=True)


def _note_history_rate_limit(state_dir: Path) -> None:
    now = time.time()
    state = _load_safety(state_dir)
    _prune_safety(state, now)
    state["strikes"] += 1
    state["last_rate_limit_at"] = now
    cooldown = min(HISTORY_COOLDOWN_BASE_SECONDS * (2 ** (state["strikes"] - 1)), HISTORY_COOLDOWN_MAX_SECONDS)
    state["cooldown_until"] = max(state["cooldown_until"], now + cooldown)
    _save_safety(state_dir, state)


def _raise_history_limit(state_dir: Path) -> None:
    _note_history_rate_limit(state_dir)
    status = safety_state(state_dir)
    seconds = int(status["history_cooldown_remaining_seconds"])
    raise _extension_error("HISTORY_RATE_LIMITED", f"ChatGPT history access is rate-limited; cooldown {seconds} seconds", retryable=True, retry_after_seconds=seconds)


def _wait_composer(session: CdpSession, state_dir: Path, timeout_seconds: float = 120.0) -> None:
    status = safety_state(state_dir)
    if status["history_cooldown_remaining_seconds"] > 0:
        raise _extension_error("HISTORY_RATE_LIMITED", "Provider cooldown is active", retryable=True, retry_after_seconds=status["history_cooldown_remaining_seconds"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _history_limited(session):
            _raise_history_limit(state_dir)
        try:
            if session.evaluate(_composer_present_js()):
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise _extension_error("WAITING_PROVIDER", "ChatGPT composer is not ready; complete login/security checks in the dedicated Judge browser", retryable=True)


def _conversation_id(url: str) -> str | None:
    match = CID_RE.search(str(url or ""))
    return match.group(1) if match else None


def _wait_conversation_url(session: CdpSession, timeout_seconds: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = session.evaluate("(() => ({url:location.href}))()") or {}
        url = str(state.get("url") or "")
        if _conversation_id(url):
            return url
        time.sleep(0.5)
    raise _extension_error("WAITING_PROVIDER", "Stable ChatGPT conversation URL was not established", retryable=True)


def _rows(session: CdpSession) -> dict[str, Any]:
    value = session.evaluate(_thread_state_js()) or {}
    if not isinstance(value, dict):
        return {"rows": [], "stopping": False, "url": ""}
    rows = value.get("rows")
    if not isinstance(rows, list):
        rows = []
    return {"rows": rows, "stopping": bool(value.get("stopping")), "url": str(value.get("url") or "")}


def _find_operation(state: dict[str, Any], operation_key: str) -> tuple[bool, str | None]:
    rows = state.get("rows") or []
    index = -1
    for i, row in enumerate(rows):
        if isinstance(row, dict) and row.get("role") == "user" and f"operation_key={operation_key}" in str(row.get("text") or ""):
            # Use the latest matching turn. A retry may legitimately resend the
            # same semantic Judge operation after an interrupted/invalid reply.
            index = i
    if index < 0:
        return False, None
    for row in rows[index + 1:]:
        if isinstance(row, dict) and row.get("role") == "assistant":
            text = str(row.get("text") or "").strip()
            if text:
                return True, text
    return True, None


def _submit_text(session: CdpSession, text: str) -> None:
    if not session.evaluate(_set_composer_js(text)):
        raise _extension_error("WAITING_PROVIDER", "ChatGPT composer disappeared before input", retryable=True)
    if not session.evaluate(_send_js()):
        raise _extension_error("WAITING_PROVIDER", "ChatGPT send button is unavailable", retryable=True)


def _ensure_file_input(session: CdpSession, timeout_seconds: float = 15.0) -> int:
    clicked = session.evaluate("""(() => {
      if(document.querySelector('input[type="file"]'))return true;
      const buttons=[...document.querySelectorAll('button')];
      const b=buttons.find(x=>/attach|upload|add photos|添加|上传|照片|文件/i.test((x.getAttribute('aria-label')||'')+' '+(x.innerText||'')));
      if(!b)return false;
      b.click();
      return true;
    })()""")
    if not clicked:
        raise _extension_error("ATTACHMENT_CONTROL_MISSING", "ChatGPT attachment control was not found", retryable=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        root = session.call("DOM.getDocument", {"depth": 2}).get("root") or {}
        root_id = root.get("nodeId")
        if root_id:
            found = session.call("DOM.querySelector", {"nodeId": root_id, "selector": 'input[type="file"]'})
            node_id = int(found.get("nodeId") or 0)
            if node_id:
                return node_id
        time.sleep(0.3)
    raise _extension_error("ATTACHMENT_INPUT_MISSING", "ChatGPT file input did not appear", retryable=True)


def _upload_file(session: CdpSession, file_path: Path) -> None:
    node_id = _ensure_file_input(session)
    session.call("DOM.setFileInputFiles", {"files": [str(file_path)], "nodeId": node_id})
    time.sleep(2.0)


def _strict_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    fence = chr(96) * 3
    cleaned = raw.replace(fence + "json", "").replace(fence + "JSON", "").replace(fence, "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise _extension_error(
            "JUDGE_JSON_INVALID",
            "Judge response does not yet contain a complete JSON object",
            retryable=True,
        )
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except ValueError as exc:
        raise _extension_error(
            "JUDGE_JSON_INVALID",
            "Judge response does not yet contain one parseable JSON object",
            retryable=True,
        ) from exc
    if not isinstance(parsed, dict):
        raise _extension_error(
            "JUDGE_JSON_INVALID",
            "Judge JSON must be an object",
            retryable=True,
        )
    return parsed


def provider_status(state_dir: Path) -> dict[str, Any]:
    return {
        "devtools_online": _devtools_online(),
        "devtools_port": DEVTOOLS_PORT,
        "safety": safety_state(state_dir),
    }


def _binding_path(run_dir: Path) -> Path:
    return run_dir / "provider-bindings.json"


def _persist_judge_binding(
    run_dir: Path,
    run_id: str,
    target: dict[str, Any],
    session: CdpSession,
) -> dict[str, Any]:
    path = _binding_path(run_dir)
    binding = _read_json(path) or {
        "schema_version": "storyboard-chatgpt-web-provider-binding-1.0",
        "run_id": run_id,
    }
    prior = binding.get("judge") if isinstance(binding.get("judge"), dict) else {}
    state = _rows(session)
    observed_url = str(state.get("url") or "")
    observed_id = _conversation_id(observed_url)
    conversation_id = observed_id or str(prior.get("conversation_id") or "") or f"WEB:{target['id']}"
    route_kind = "stable" if observed_id else "ephemeral_web"
    binding["judge"] = {
        **prior,
        "role": "judge",
        "conversation_id": conversation_id,
        "conversation_url": observed_url or str(prior.get("conversation_url") or "https://chatgpt.com/"),
        "target_id": str(target["id"]),
        "websocket_debugger_url": str(target["webSocketDebuggerUrl"]),
        "route_kind": route_kind,
        "updated_at": time.time(),
    }
    _atomic_json(path, binding)
    return binding


def _provider_gate_error(exc: Exception) -> bool:
    return str(getattr(exc, "code", "")) in {
        "HISTORY_RATE_LIMITED",
        "FRESH_CHAT_COOLDOWN",
        "NAVIGATION_COOLDOWN",
    }


def _open_judge_session(
    state_dir: Path,
    run_dir: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], CdpSession, bool]:
    """
    Best-effort reuse of the current Judge page.

    Transport identity is deliberately not a semantic gate:
    1. reuse the saved live target when it is still a usable ChatGPT page;
    2. otherwise reopen the saved stable conversation URL;
    3. otherwise start a fresh ChatGPT page.

    Only provider rate-limit/cooldown state blocks these fallbacks.
    """
    _ensure_browser(state_dir)
    path = _binding_path(run_dir)
    existing = _read_json(path)
    judge = (existing or {}).get("judge")
    if not isinstance(judge, dict):
        judge = {}

    stored_id = str(judge.get("conversation_id") or "")
    stored_url = str(judge.get("conversation_url") or "")
    websocket_url = str(judge.get("websocket_debugger_url") or "")
    target_id = str(judge.get("target_id") or "")

    target: dict[str, Any] | None = None
    session: CdpSession | None = None
    reused = False

    if websocket_url and target_id:
        try:
            candidate = CdpSession(websocket_url, timeout=30.0)
            _wait_composer(candidate, state_dir)
            state = _rows(candidate)
            observed_id = _conversation_id(str(state.get("url") or ""))
            if stored_id and not stored_id.startswith("WEB:") and observed_id != stored_id:
                candidate.close()
            else:
                session = candidate
                target = {
                    "id": target_id,
                    "webSocketDebuggerUrl": websocket_url,
                    "keep_open": True,
                }
                reused = True
        except Exception as exc:
            if _provider_gate_error(exc):
                raise
            try:
                candidate.close()
            except Exception:
                pass

    if session is None and stored_url and stored_id and not stored_id.startswith("WEB:"):
        opened: dict[str, Any] | None = None
        try:
            _reserve_navigation(state_dir, "stable_conversation_direct_reopen")
            opened = _new_target(stored_url)
            candidate = CdpSession(str(opened["webSocketDebuggerUrl"]), timeout=30.0)
            _wait_composer(candidate, state_dir)
            state = _rows(candidate)
            observed_id = _conversation_id(str(state.get("url") or ""))
            if observed_id != stored_id:
                candidate.close()
                _close_target(str(opened["id"]))
                opened = None
            else:
                session = candidate
                target = {
                    **opened,
                    "keep_open": True,
                }
                reused = True
        except Exception as exc:
            if opened is not None:
                _close_target(str(opened.get("id") or ""))
            if _provider_gate_error(exc):
                raise
            session = None
            target = None

    if session is None:
        _reserve_fresh_chat(state_dir)
        opened = _new_target("https://chatgpt.com/")
        try:
            candidate = CdpSession(str(opened["webSocketDebuggerUrl"]), timeout=30.0)
            _wait_composer(candidate, state_dir)
        except Exception:
            _close_target(str(opened["id"]))
            raise
        session = candidate
        target = {
            **opened,
            "keep_open": True,
        }
        reused = False

    binding = _persist_judge_binding(run_dir, run_id, target, session)
    return binding, target, session, reused


def bind_judge(state_dir: Path, run_dir: Path, run_id: str) -> dict[str, Any]:
    binding, target, session, reused = _open_judge_session(state_dir, run_dir, run_id)
    try:
        return {
            "already_bound": reused,
            "binding": binding,
        }
    finally:
        session.close()


def _journal_result(
    journal_path: Path,
    journal: dict[str, Any],
    response_text: str,
    result: dict[str, Any],
) -> None:
    _atomic_json(
        journal_path,
        {
            **journal,
            "phase": "completed",
            "completed_at": time.time(),
            "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
            "model_verdict": result.get("model_verdict"),
        },
    )


def _wait_for_operation_reply(
    session: CdpSession,
    state_dir: Path,
    operation_key: str,
    timeout_seconds: int,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    stable = 0
    unusable_stable = 0
    while time.monotonic() < deadline:
        if _history_limited(session):
            _raise_history_limit(state_dir)
        state = _rows(session)
        present, response_text = _find_operation(state, operation_key)
        if not present:
            return None
        if response_text:
            if response_text == last:
                stable += 1
            else:
                last = response_text
                stable = 0
                unusable_stable = 0
            if not state["stopping"] and stable >= 2:
                try:
                    _strict_json(response_text)
                except Exception as exc:
                    if str(getattr(exc, "code", "")) != "JUDGE_JSON_INVALID":
                        raise
                    unusable_stable += 1
                    if unusable_stable >= 10:
                        return None
                else:
                    return response_text
        time.sleep(1.0)
    return None


def review_candidate(
    state_dir: Path,
    run_dir: Path,
    *,
    operation_key: str,
    candidate_path: Path,
    judge_prompt: str,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", operation_key):
        raise _extension_error("OPERATION_KEY_INVALID", "Judge operation_key must be a SHA-256 hex string")

    run_record = _read_json(run_dir / "run.json") or {}
    run_id = str(run_record.get("run_id") or run_dir.name)
    journal_path = run_dir / "provider-journal" / f"judge-{operation_key}.json"
    prior_journal = _read_json(journal_path) or {}

    binding, target, session, _ = _open_judge_session(state_dir, run_dir, run_id)
    try:
        if _history_limited(session):
            _raise_history_limit(state_dir)

        state = _rows(session)
        present, _ = _find_operation(state, operation_key)

        # Pragmatic transport recovery: if the same operation is already visible,
        # give its latest assistant reply a short chance to become stable and
        # parseable. If it never does, simply resend the same Judge operation.
        # The browser transcript is transport, not semantic authority.
        if present:
            response_text = _wait_for_operation_reply(
                session,
                state_dir,
                operation_key,
                min(timeout_seconds, 20),
            )
            if response_text:
                result = _strict_json(response_text)
                journal = {
                    **prior_journal,
                    "operation_key": operation_key,
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                    "conversation_id": binding["judge"].get("conversation_id"),
                }
                _journal_result(journal_path, journal, response_text, result)
                return {
                    "recovered": True,
                    "submitted": True,
                    "raw_text": response_text,
                    "judge_result": result,
                    "binding": binding["judge"],
                }

        # No usable answer is visible. Sending again is allowed; the journal is an
        # observability record, not a transport veto. The normal provider cadence
        # and history cooldown still apply.
        send_count = int(prior_journal.get("send_count") or 0) + 1
        journal = {
            "schema_version": "storyboard-forge-provider-journal-1.1",
            "operation_key": operation_key,
            "phase": "send_intent",
            "send_count": send_count,
            "candidate_path": str(candidate_path),
            "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            "conversation_id": binding["judge"].get("conversation_id"),
            "created_at": float(prior_journal.get("created_at") or time.time()),
            "updated_at": time.time(),
        }
        _atomic_json(journal_path, journal)

        _upload_file(session, candidate_path)
        prompt = f"operation_key={operation_key}\n\n{judge_prompt}"
        _submit_text(session, prompt)

        deadline = time.monotonic() + 30.0
        confirmed = False
        while time.monotonic() < deadline:
            if _history_limited(session):
                _raise_history_limit(state_dir)
            state = _rows(session)
            confirmed, _ = _find_operation(state, operation_key)
            if confirmed:
                break
            time.sleep(0.5)

        if not confirmed:
            _atomic_json(
                journal_path,
                {
                    **journal,
                    "phase": "submission_uncertain",
                    "updated_at": time.time(),
                },
            )
            raise _extension_error(
                "WAITING_PROVIDER",
                "Judge send was not confirmed in the current ChatGPT page; retry is allowed after normal provider cadence",
                retryable=True,
            )

        binding = _persist_judge_binding(run_dir, run_id, target, session)
        journal = {
            **journal,
            "phase": "submitted",
            "submitted_at": time.time(),
            "conversation_id": binding["judge"].get("conversation_id"),
        }
        _atomic_json(journal_path, journal)

        response_text = _wait_for_operation_reply(
            session,
            state_dir,
            operation_key,
            timeout_seconds,
        )
        if not response_text:
            raise _extension_error(
                "WAITING_PROVIDER",
                "Judge prompt is submitted but no complete response is visible yet",
                retryable=True,
            )

        result = _strict_json(response_text)
        _journal_result(journal_path, journal, response_text, result)
        return {
            "recovered": False,
            "submitted": True,
            "raw_text": response_text,
            "judge_result": result,
            "binding": binding["judge"],
        }
    finally:
        session.close()
