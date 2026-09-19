from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

try:
    from folderbridge_mcp.extension_api import ExtensionError, owned_process_group_kwargs, terminate_owned_process_tree
except ModuleNotFoundError:
    class ExtensionError(RuntimeError):
        def __init__(self, code: str, message: str, *, retryable: bool = False):
            super().__init__(message)
            self.code = code
            self.retryable = retryable

    def owned_process_group_kwargs(*, hide_window: bool = False) -> dict[str, Any]:
        if os.name != "nt":
            return {}
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hide_window:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags} if flags else {}

    def terminate_owned_process_tree(process: subprocess.Popen[bytes], *, hide_window: bool = True) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if hide_window else 0
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=flags,
            )
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _configured_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1024 or value > 65535:
        raise RuntimeError(f"{name} must be between 1024 and 65535")
    return value


HOST = "127.0.0.1"
PORT = _configured_port("CHATGPT_WEB_ADAPTER_PORT", 8767)
DEVTOOLS_PORT = _configured_port("CHATGPT_WEB_ADAPTER_DEVTOOLS_PORT", 8768)
if PORT == DEVTOOLS_PORT:
    raise RuntimeError("API and DevTools ports must be different")
BASE_URL = f"http://{HOST}:{PORT}/v1"
MODEL_ID = "chatgpt-web-current"
ADAPTER_ID = "chatgpt-web-llm-adapter"
ADAPTER_VERSION = "0.3.2"
FRESH_CHAT_MIN_INTERVAL_SECONDS = 20
FRESH_CHAT_WINDOW_SECONDS = 300
FRESH_CHAT_WINDOW_MAX = 8
HISTORY_COOLDOWN_BASE_SECONDS = 120
HISTORY_COOLDOWN_MAX_SECONDS = 600
HISTORY_COOLDOWN_RESET_SECONDS = 1800
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_MESSAGE_CHARS = 350_000
MAX_SINGLE_MESSAGE_CHARS = 180_000
MAX_MESSAGES = 96
MAX_WEB_PARALLEL = 4
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


def _opener():
    return build_opener(ProxyHandler({}))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def connection_path(state_dir: Path) -> Path:
    return state_dir / "connection.json"


def profile_dir(state_dir: Path) -> Path:
    return state_dir / "browser-profile-v1"


def locate_browser() -> Path:
    drive = os.environ.get("SYSTEMDRIVE") or "C:"
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates = [
        Path(drive + r"\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(drive + r"\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(drive + r"\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(drive + r"\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    if local:
        candidates.extend(
            [
                Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    for candidate in candidates:
        if candidate.is_file() and candidate.name.lower() in {"chrome.exe", "msedge.exe"}:
            return candidate
    raise ExtensionError(
        "BROWSER_NOT_FOUND",
        "No ordinary Chrome or Edge executable was found in the supported Windows locations.",
        retryable=False,
    )


def ensure_loopback_port_available(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOST, int(port)))
    except OSError as exc:
        raise ExtensionError(
            "DEVTOOLS_PORT_BUSY",
            f"Dedicated browser DevTools port {HOST}:{port} is already in use. Close the conflicting process and retry.",
            retryable=True,
        ) from exc
    finally:
        probe.close()


class WebSocketProtocolError(RuntimeError):
    pass


class RawWebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise WebSocketProtocolError("CDP websocket must be local ws:// only")
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
        header = self._read_until(b"\r\n\r\n", 64 * 1024)
        lines = header.decode("latin1", errors="replace").split("\r\n")
        if not lines or " 101 " not in (" " + lines[0] + " "):
            raise WebSocketProtocolError("Chrome DevTools websocket upgrade failed")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketProtocolError("Chrome DevTools websocket accept key mismatch")

    def _recv_raw(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise WebSocketProtocolError("CDP websocket closed unexpectedly")
            self.buffer.extend(chunk)
        output = bytes(self.buffer[:size])
        del self.buffer[:size]
        return output

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketProtocolError("websocket handshake closed unexpectedly")
            self.buffer.extend(chunk)
            if len(self.buffer) > limit:
                raise WebSocketProtocolError("websocket handshake exceeded limit")
        index = self.buffer.index(marker) + len(marker)
        output = bytes(self.buffer[:index])
        del self.buffer[:index]
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
                raise WebSocketProtocolError("CDP websocket closed")
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
            self._send_frame(0x8, b"")
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
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("ChatGPT page JavaScript evaluation failed")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.websocket.close()


def _selector_array(selectors: list[str]) -> str:
    return json.dumps(selectors, ensure_ascii=False)


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
      btn.click();return true;
    }})()"""


def _conversation_state_js() -> str:
    return f"""(() => {{
      const assistants=[...document.querySelectorAll('[data-message-author-role="assistant"]')];
      const source=assistants.length?assistants[assistants.length-1]:null;
      let last='';
      if(source){{
        const clone=source.cloneNode(true);
        clone.querySelectorAll('button,[role="button"],svg').forEach(el=>el.remove());
        last=clone.innerText||clone.textContent||'';
      }}
      const stopping={_selector_array(STOP_BUTTONS)}.some(s=>!!document.querySelector(s));
      return {{count:assistants.length,text:last,stopping,url:location.href}};
    }})()"""


HISTORY_RATE_LIMIT_PHRASES = [
    "你的请求过于频繁",
    "暂时限制你访问对话记录",
    "请稍等几分钟后再重试",
    "your requests are too frequent",
    "temporarily restricted your access to conversation history",
    "please wait a few minutes and try again",
]


def _history_guard_js(*, include_body: bool) -> str:
    body_fallback = "true" if include_body else "false"
    return f"""(() => {{
      const phrases={json.dumps(HISTORY_RATE_LIMIT_PHRASES, ensure_ascii=False)}.map(x=>x.toLowerCase());
      const visible=el=>{{const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;}};
      const selectors=['[role="alert"]','[role="dialog"]','[data-sonner-toast]','[data-testid*="toast"]','[aria-live="assertive"]','[aria-live="polite"]'];
      const parts=[...document.querySelectorAll(selectors.join(','))].filter(visible).map(el=>el.innerText||el.textContent||'');
      const composer={_composer_present_js()};
      if({body_fallback}&&!composer&&document.body)parts.push(document.body.innerText||document.body.textContent||'');
      const text=parts.join('\n').toLowerCase();
      return {{limited:phrases.some(p=>text.includes(p))}};
    }})()"""


def _http_json(url: str, *, method: str = "GET", timeout: float = 5.0) -> Any:
    request = Request(url, method=method, headers={"Accept": "application/json"})
    with _opener().open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_target(port: int, target_id: str) -> None:
    try:
        _http_json(f"http://{HOST}:{port}/json/close/{quote(target_id, safe='')}", timeout=3.0)
    except Exception:
        pass


def _existing_chatgpt_target(port: int) -> dict[str, Any] | None:
    try:
        targets = _http_json(f"http://{HOST}:{port}/json/list", timeout=3.0)
    except Exception:
        return None
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        if not str(target.get("url") or "").startswith("https://chatgpt.com"):
            continue
        if isinstance(target.get("webSocketDebuggerUrl"), str) and target.get("id"):
            return target
    return None


def _new_target(port: int) -> dict[str, Any]:
    encoded = quote("https://chatgpt.com/", safe="")
    url = f"http://{HOST}:{port}/json/new?{encoded}"
    try:
        value = _http_json(url, method="PUT", timeout=10.0)
    except Exception:
        value = _http_json(url, method="GET", timeout=10.0)
    if not isinstance(value, dict) or not value.get("webSocketDebuggerUrl") or not value.get("id"):
        raise RuntimeError("Chrome did not create a debuggable ChatGPT tab")
    return value


def build_wrapped_prompt(messages: list[dict[str, str]], json_mode: bool) -> str:
    system = [item["content"] for item in messages if item["role"] == "system"]
    conversation = [item for item in messages if item["role"] != "system"]
    payload = {"system_messages": system, "conversation": conversation}
    suffix = (
        "Return exactly one JSON object and nothing else. Do not use Markdown fences or explanatory text."
        if json_mode
        else "Return only the assistant response that this API request asks for."
    )
    return (
        "CHATGPT-WEB TEST ADAPTER REQUEST\n"
        "This is one stateless compatibility request. Treat system_messages as the highest-priority application "
        "instructions within this request, then follow conversation in order. Do not refer to this wrapper unless "
        "the application asks about transport. Each API request is isolated in a fresh ChatGPT conversation.\n"
        + suffix
        + "\n\nREQUEST_JSON:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def validate_chat_request(value: Any) -> tuple[list[dict[str, str]], bool, bool, str]:
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    model = value.get("model")
    if model != MODEL_ID:
        raise ValueError(f"model must be {MODEL_ID}")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise ValueError("messages must be a non-empty bounded array")
    normalized: list[dict[str, str]] = []
    total = 0
    for message in messages:
        if not isinstance(message, dict) or set(message).difference({"role", "content", "name"}):
            raise ValueError("each message may contain only role, content and optional name")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("only string system/user/assistant messages are supported")
        if len(content) > MAX_SINGLE_MESSAGE_CHARS:
            raise ValueError("one message exceeds the browser test adapter limit")
        total += len(content)
        normalized.append({"role": role, "content": content})
    if total > MAX_TOTAL_MESSAGE_CHARS:
        raise ValueError("combined message content exceeds the browser test adapter limit")
    stream = value.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be boolean")
    response_format = value.get("response_format")
    json_mode = False
    if response_format is not None:
        if response_format != {"type": "json_object"}:
            raise ValueError("only response_format={type:json_object} is supported")
        json_mode = True
    allowed = {
        "model",
        "messages",
        "stream",
        "response_format",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "reasoning_effort",
        "thinking",
        "stop",
    }
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ValueError("unsupported request fields: " + ", ".join(unknown))
    return normalized, json_mode, stream, model


def _wrapper_only(value: str) -> bool:
    text = " ".join(str(value or "").lower().replace("\ufeff", "").replace("\u200b", "").split())
    for token in ("```json", "```", "copy code", "复制代码", "json", "copy", "复制", "`"):
        text = text.replace(token, " ")
    return not text.strip(" \t\r\n:|·-")


def normalize_json_object_response(output: str) -> str:
    """Accept one JSON object, plus only non-semantic code-block display wrappers.

    This keeps response_format strict while tolerating ChatGPT Web rendering such
    as ```json fences or a visible "Copy code" label. Explanatory prose, a second
    object, arrays, or malformed JSON are still rejected.
    """
    text = str(output or "").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            if _wrapper_only(text[:start]) and _wrapper_only(text[end:]):
                candidates.append(value)
        if len(candidates) != 1:
            raise ValueError("response was not exactly one JSON object")
        parsed = candidates[0]
    if not isinstance(parsed, dict):
        raise TypeError("JSON response must be an object")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


class AdjustableConcurrencyGate:
    def __init__(self, limit: int):
        self._condition = threading.Condition()
        self._limit = 1
        self._active = 0
        self.set_limit(limit)

    def set_limit(self, limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_WEB_PARALLEL:
            raise ValueError(f"max_parallel must be 1–{MAX_WEB_PARALLEL}")
        with self._condition:
            self._limit = limit
            self._condition.notify_all()
            return self._limit

    def state(self) -> dict[str, int]:
        with self._condition:
            return {"max_parallel": self._limit, "active_requests": self._active}

    def __enter__(self):
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


class FreshChatSafetyGate:
    """Bound fresh ChatGPT page creation by cadence, rolling volume and cooldown.

    This is intentionally independent from completion concurrency: two long web
    turns may overlap, but their *new chat starts* must still be spaced out.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: int = FRESH_CHAT_MIN_INTERVAL_SECONDS,
        window_seconds: int = FRESH_CHAT_WINDOW_SECONDS,
        window_max: int = FRESH_CHAT_WINDOW_MAX,
        cooldown_base_seconds: int = HISTORY_COOLDOWN_BASE_SECONDS,
        cooldown_max_seconds: int = HISTORY_COOLDOWN_MAX_SECONDS,
        cooldown_reset_seconds: int = HISTORY_COOLDOWN_RESET_SECONDS,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        values = [min_interval_seconds, window_seconds, window_max, cooldown_base_seconds, cooldown_max_seconds, cooldown_reset_seconds]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("fresh chat safety settings must be positive integers")
        if cooldown_max_seconds < cooldown_base_seconds:
            raise ValueError("cooldown max must not be smaller than cooldown base")
        self.min_interval_seconds = min_interval_seconds
        self.window_seconds = window_seconds
        self.window_max = window_max
        self.cooldown_base_seconds = cooldown_base_seconds
        self.cooldown_max_seconds = cooldown_max_seconds
        self.cooldown_reset_seconds = cooldown_reset_seconds
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._starts: deque[float] = deque()
        self._next_start_at = 0.0
        self._cooldown_until = 0.0
        self._strikes = 0
        self._last_rate_limit_at = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._starts and self._starts[0] <= cutoff:
            self._starts.popleft()
        if self._last_rate_limit_at and now - self._last_rate_limit_at >= self.cooldown_reset_seconds:
            self._strikes = 0
            self._last_rate_limit_at = 0.0

    def _earliest_start(self, now: float) -> float:
        earliest = max(self._next_start_at, self._cooldown_until)
        if len(self._starts) >= self.window_max:
            earliest = max(earliest, self._starts[0] + self.window_seconds)
        return earliest

    @staticmethod
    def _seconds(value: float) -> int:
        return max(0, int(value + 0.999))

    def state(self) -> dict[str, int]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            earliest = self._earliest_start(now)
            return {
                "fresh_chat_min_interval_seconds": self.min_interval_seconds,
                "fresh_chat_window_seconds": self.window_seconds,
                "fresh_chat_window_max": self.window_max,
                "fresh_chat_starts_in_window": len(self._starts),
                "fresh_chat_next_start_in_seconds": self._seconds(earliest - now),
                "history_cooldown_remaining_seconds": self._seconds(self._cooldown_until - now),
                "history_rate_limit_strikes": self._strikes,
            }

    def wait_for_slot(self) -> dict[str, int]:
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                earliest = self._earliest_start(now)
                if now >= earliest:
                    self._starts.append(now)
                    self._next_start_at = now + self.min_interval_seconds
                    return self.state_unlocked(now)
                delay = earliest - now
            self._sleep(min(delay, 1.0))

    def state_unlocked(self, now: float) -> dict[str, int]:
        earliest = self._earliest_start(now)
        return {
            "fresh_chat_min_interval_seconds": self.min_interval_seconds,
            "fresh_chat_window_seconds": self.window_seconds,
            "fresh_chat_window_max": self.window_max,
            "fresh_chat_starts_in_window": len(self._starts),
            "fresh_chat_next_start_in_seconds": self._seconds(earliest - now),
            "history_cooldown_remaining_seconds": self._seconds(self._cooldown_until - now),
            "history_rate_limit_strikes": self._strikes,
        }

    def note_history_rate_limit(self) -> dict[str, int]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._strikes += 1
            self._last_rate_limit_at = now
            cooldown = min(self.cooldown_base_seconds * (2 ** (self._strikes - 1)), self.cooldown_max_seconds)
            self._cooldown_until = max(self._cooldown_until, now + cooldown)
            return self.state_unlocked(now)


@dataclass
class BrowserController:
    state_dir: Path
    max_parallel: int
    response_timeout_seconds: int
    process: subprocess.Popen[bytes] | None = None
    debug_port: int | None = None

    def __post_init__(self) -> None:
        self.concurrency = AdjustableConcurrencyGate(self.max_parallel)
        self.fresh_chat_safety = FreshChatSafetyGate()
        self.executable = locate_browser()
        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()

    def _devtools_online(self) -> bool:
        try:
            value = _http_json(f"http://{HOST}:{DEVTOOLS_PORT}/json/version", timeout=2.0)
        except Exception:
            return False
        if isinstance(value, dict):
            self.debug_port = DEVTOOLS_PORT
            return True
        return False

    def ensure_running(self) -> bool:
        """Ensure the dedicated browser exists. Returns True when relaunched."""
        with self._lifecycle_lock:
            if self._devtools_online():
                return False
            if self.process is not None and self.process.poll() is None:
                try:
                    terminate_owned_process_tree(self.process, hide_window=True)
                except Exception:
                    try:
                        self.process.terminate()
                    except Exception:
                        pass
            self.process = None
            self.debug_port = None
            self.start()
            return True

    def _drain_browser_stderr(self) -> None:
        pipe = self.process.stderr if self.process is not None else None
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_tail.extend(chunk)
                    if len(self._stderr_tail) > 16384:
                        del self._stderr_tail[:-16384]
        except Exception:
            return

    def _browser_diagnostic_tail(self) -> str:
        with self._stderr_lock:
            raw = bytes(self._stderr_tail[-4096:])
        text = raw.decode("utf-8", errors="replace").strip()
        profile = str(profile_dir(self.state_dir))
        if profile:
            text = text.replace(profile, "<profile>")
        return text[-2000:]

    def start(self) -> None:
        profile = profile_dir(self.state_dir)
        profile.mkdir(parents=True, exist_ok=True)
        active = profile / "DevToolsActivePort"
        try:
            active.unlink()
        except FileNotFoundError:
            pass
        ensure_loopback_port_available(DEVTOOLS_PORT)
        args = [
            str(self.executable),
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={DEVTOOLS_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-extensions",
            "--disable-features=Translate",
            "--start-maximized",
            "https://chatgpt.com/",
        ]
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            **owned_process_group_kwargs(hide_window=False),
        )
        threading.Thread(target=self._drain_browser_stderr, name="chatgpt-web-browser-stderr", daemon=True).start()
        deadline = time.monotonic() + 45.0
        exited_at: float | None = None
        while time.monotonic() < deadline:
            # The fixed loopback DevTools endpoint is the authority. Chrome/Edge do
            # not consistently create DevToolsActivePort when a non-zero fixed port
            # is supplied, so that profile file is auxiliary evidence only.
            if self._devtools_online():
                return
            if self.process.poll() is not None:
                exited_at = exited_at or time.monotonic()
                # Chrome can hand ownership to another process before the fixed
                # DevTools endpoint is observable. Give that handoff a short grace
                # period instead of treating the launcher PID as authority.
                if time.monotonic() - exited_at >= 3.0:
                    raise RuntimeError("Chrome/Edge exited before DevTools became ready; the dedicated profile may be locked")
            try:
                lines = active.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                if port != DEVTOOLS_PORT:
                    raise RuntimeError(f"Chrome/Edge reported unexpected DevTools port {port}")
            except FileNotFoundError:
                pass
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(0.25)
        diagnostic = self._browser_diagnostic_tail()
        message = f"Chrome/Edge DevTools endpoint {HOST}:{DEVTOOLS_PORT} did not become ready."
        if diagnostic:
            message += " Browser diagnostic tail: " + diagnostic
        raise ExtensionError("DEVTOOLS_UNAVAILABLE", message, retryable=True)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                terminate_owned_process_tree(self.process, hide_window=True)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
        self.process = None
        self.debug_port = None

    def _history_limited(self, session: CdpSession, *, include_body: bool) -> bool:
        try:
            state = session.evaluate(_history_guard_js(include_body=include_body)) or {}
            return bool(state.get("limited"))
        except Exception:
            return False

    def _raise_history_limit(self) -> None:
        state = self.fresh_chat_safety.note_history_rate_limit()
        seconds = int(state.get("history_cooldown_remaining_seconds") or HISTORY_COOLDOWN_BASE_SECONDS)
        raise ExtensionError(
            "HISTORY_RATE_LIMITED",
            f"ChatGPT web temporarily limited conversation/history access. Adapter cooldown: {seconds} seconds.",
            retryable=True,
        )

    def _wait_composer(self, session: CdpSession, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if self._history_limited(session, include_body=True):
                    self._raise_history_limit()
                if session.evaluate(_composer_present_js()):
                    return
            except ExtensionError:
                raise
            except Exception:
                pass
            time.sleep(0.5)
        raise ExtensionError(
            "WAITING_PROVIDER",
            "ChatGPT composer is not ready. Complete login/security checks in the dedicated browser window, then retry.",
            retryable=True,
        )

    def provider_state(self) -> str:
        if not self._devtools_online():
            return "devtools_unavailable" if self.process is not None and self.process.poll() is None else "browser_offline"
        try:
            targets = _http_json(f"http://{HOST}:{self.debug_port}/json/list", timeout=2.0)
        except Exception:
            return "devtools_unavailable"
        found_chatgpt = False
        saw_loading = False
        saw_ready = False
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            if not str(target.get("url") or "").startswith("https://chatgpt.com"):
                continue
            found_chatgpt = True
            ws = target.get("webSocketDebuggerUrl")
            if not isinstance(ws, str):
                continue
            session = None
            try:
                session = CdpSession(ws, timeout=3.0)
                if self._history_limited(session, include_body=True):
                    return "history_rate_limited"
                snapshot = session.evaluate(
                    f"(() => ({{readyState:document.readyState,composer:{_composer_present_js()}}}))()"
                ) or {}
                if bool(snapshot.get("composer")):
                    saw_ready = True
                if str(snapshot.get("readyState") or "") == "loading":
                    saw_loading = True
            except Exception:
                pass
            finally:
                if session:
                    session.close()
        if saw_ready:
            return "ready"
        if saw_loading:
            return "page_loading"
        if found_chatgpt:
            return "waiting_login_or_challenge"
        return "chatgpt_page_missing"

    def ready(self) -> bool:
        return self.provider_state() == "ready"

    def open_login_page(self) -> dict[str, Any]:
        restarted = self.ensure_running()
        target = _existing_chatgpt_target(self.debug_port)
        reused = target is not None
        if target is None:
            self.fresh_chat_safety.wait_for_slot()
            target = _new_target(self.debug_port)
        target_id = str(target["id"])
        session = CdpSession(str(target["webSocketDebuggerUrl"]), timeout=15.0)
        try:
            try:
                session.call("Page.bringToFront")
            except Exception:
                pass
        finally:
            session.close()
        return {"ok": True, "opened": True, "target_id": target_id, "browser_restarted": restarted, "reused_existing_chatgpt_page": reused}

    def concurrency_state(self) -> dict[str, int]:
        return {**self.concurrency.state(), **self.fresh_chat_safety.state()}

    def set_max_parallel(self, value: int) -> dict[str, int]:
        self.max_parallel = self.concurrency.set_limit(value)
        return self.concurrency_state()

    def complete(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        with self.concurrency:
            restarted = self.ensure_running()
            if restarted:
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline and self.provider_state() in {"page_loading", "chatgpt_page_missing", "devtools_unavailable"}:
                    time.sleep(0.5)
            provider = self.provider_state()
            if provider == "history_rate_limited":
                self._raise_history_limit()
            if provider != "ready":
                raise ExtensionError(
                    "WAITING_PROVIDER",
                    "ChatGPT is waiting for login/security completion in the dedicated browser window.",
                    retryable=True,
                )
            self.fresh_chat_safety.wait_for_slot()
            target = _new_target(self.debug_port)
            target_id = str(target["id"])
            session = CdpSession(str(target["webSocketDebuggerUrl"]), timeout=30.0)
            try:
                self._wait_composer(session, 120.0)
                before = session.evaluate(_conversation_state_js()) or {}
                before_count = int(before.get("count") or 0)
                prompt = build_wrapped_prompt(messages, json_mode)
                if not session.evaluate(_set_composer_js(prompt)):
                    raise RuntimeError("ChatGPT composer disappeared before input")
                if not session.evaluate(_send_js()):
                    raise RuntimeError("ChatGPT send button was unavailable")
                deadline = time.monotonic() + self.response_timeout_seconds
                last = ""
                stable = 0
                while time.monotonic() < deadline:
                    if self._history_limited(session, include_body=False):
                        self._raise_history_limit()
                    state = session.evaluate(_conversation_state_js()) or {}
                    count = int(state.get("count") or 0)
                    text = str(state.get("text") or "")
                    stopping = bool(state.get("stopping"))
                    if count > before_count and text.strip() and not stopping:
                        if text == last:
                            stable += 1
                        else:
                            last = text
                            stable = 0
                        if stable >= 2:
                            output = text.strip()
                            if json_mode:
                                try:
                                    output = normalize_json_object_response(output)
                                except ValueError as exc:
                                    raise ExtensionError(
                                        "JSON_RESPONSE_INVALID",
                                        "ChatGPT web response was not exactly one valid JSON object.",
                                        retryable=True,
                                    ) from exc
                                except TypeError as exc:
                                    raise ExtensionError(
                                        "JSON_RESPONSE_INVALID",
                                        "ChatGPT web JSON response must be an object.",
                                        retryable=True,
                                    ) from exc
                            return output
                    time.sleep(1.0)
                raise ExtensionError("RESPONSE_TIMEOUT", "ChatGPT web response timed out.", retryable=True)
            finally:
                session.close()
                _close_target(self.debug_port, target_id)


HTTP_HEADER_LIMIT = 64 * 1024
HTTP_READ_TIMEOUT_SECONDS = 15.0
HTTP_STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    413: "Payload Too Large",
    429: "Too Many Requests",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class AdapterHttpServer:
    """Small loopback-only HTTP/1.1 server using modules present in the frozen host.

    The public Extension must remain usable from the one-file FolderBridge build,
    where rarely imported stdlib helpers such as http.server may be absent.  This
    server deliberately supports only the exact bounded surface needed here.
    """

    def __init__(self, address: tuple[str, int], token: str, backend: BrowserController):
        host, port = address
        if host != HOST:
            raise ValueError("adapter server is loopback-only")
        self.token = token
        self.backend = backend
        self.started_at = time.time()
        self._closed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind((host, port))
            self._socket.listen(64)
        except Exception:
            self._socket.close()
            raise
        self.server_address = self._socket.getsockname()
        self.server_port = int(self.server_address[1])

    def serve_forever(self, poll_interval: float = 0.25) -> None:
        self._socket.settimeout(max(0.05, float(poll_interval)))
        while not self._closed.is_set():
            try:
                client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed.is_set():
                    break
                raise
            threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()

    def shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    def server_close(self) -> None:
        self.shutdown()

    @staticmethod
    def _read_until(client: socket.socket, marker: bytes, limit: int) -> tuple[bytes, bytes]:
        buffer = bytearray()
        while marker not in buffer:
            chunk = client.recv(4096)
            if not chunk:
                raise ValueError("HTTP request ended before headers completed")
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise ValueError("HTTP request headers exceeded limit")
        index = buffer.index(marker) + len(marker)
        return bytes(buffer[:index]), bytes(buffer[index:])

    @staticmethod
    def _parse_request(client: socket.socket) -> tuple[str, str, dict[str, str], bytes]:
        header_bytes, remainder = AdapterHttpServer._read_until(client, b"\r\n\r\n", HTTP_HEADER_LIMIT)
        try:
            header_text = header_bytes.decode("latin1")
        except UnicodeDecodeError as exc:
            raise ValueError("HTTP request headers are invalid") from exc
        lines = header_text[:-4].split("\r\n")
        if not lines:
            raise ValueError("HTTP request line is missing")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[0] not in {"GET", "POST", "OPTIONS"} or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError("HTTP request line is unsupported")
        method, target, _ = parts
        if not target.startswith("/") or " " in target:
            raise ValueError("HTTP request target is invalid")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                raise ValueError("HTTP request header is malformed")
            name, value = line.split(":", 1)
            key = name.strip().lower()
            if not key or key in headers:
                raise ValueError("duplicate or empty HTTP request header")
            headers[key] = value.strip()
        if headers.get("transfer-encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        try:
            length = int(headers.get("content-length") or "0")
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise OverflowError("request body exceeds adapter limit")
        body = bytearray(remainder)
        if len(body) > length:
            body = body[:length]
        while len(body) < length:
            chunk = client.recv(min(64 * 1024, length - len(body)))
            if not chunk:
                raise ValueError("HTTP request body ended early")
            body.extend(chunk)
        return method, target.split("?", 1)[0], headers, bytes(body)

    @staticmethod
    def _origin(headers: dict[str, str]) -> str | None:
        value = headers.get("origin")
        if not value:
            return None
        if value == "null":
            return value
        try:
            parsed = urlparse(value)
        except ValueError:
            return "__denied__"
        if parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost"}:
            return value
        return "__denied__"

    def _authorized(self, headers: dict[str, str]) -> bool:
        expected = "Bearer " + self.token
        return secrets.compare_digest(headers.get("authorization") or "", expected)

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _error_bytes(code: str, message: str) -> bytes:
        return AdapterHttpServer._json_bytes({"error": {"message": message, "type": "chatgpt_web_test_error", "code": code}})

    def _send(
        self,
        client: socket.socket,
        status: int,
        body: bytes = b"",
        *,
        content_type: str = "application/json; charset=utf-8",
        origin: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        lines = [
            f"HTTP/1.1 {status} {HTTP_STATUS_TEXT.get(status, 'Error')}",
            f"Content-Type: {content_type}",
            "Cache-Control: no-store",
            "X-ChatGPT-Web-Test-Adapter: prompt-wrapped-system; fresh-chat-per-request",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if origin not in {None, "__denied__"}:
            lines.extend([f"Access-Control-Allow-Origin: {origin}", "Vary: Origin"])
        for name, value in (extra_headers or {}).items():
            lines.append(f"{name}: {value}")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin1") + body
        client.sendall(payload)

    def _send_error(self, client: socket.socket, status: int, code: str, message: str, origin: str | None = None) -> None:
        self._send(client, status, self._error_bytes(code, message), origin=origin)

    def _dispatch(self, client: socket.socket, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        origin = self._origin(headers)
        if method == "OPTIONS":
            if origin == "__denied__":
                self._send_error(client, 403, "origin_denied", "Browser origin is not allowed by this loopback test adapter.")
                return
            self._send(
                client,
                204,
                b"",
                origin=origin,
                extra_headers={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "authorization, content-type",
                    "Access-Control-Max-Age": "600",
                },
            )
            return
        if method == "GET":
            if path == "/health":
                self._send(
                    client,
                    200,
                    self._json_bytes({
                        "ok": True,
                        "adapter": ADAPTER_ID,
                        "adapter_version": ADAPTER_VERSION,
                        "ready": self.backend.ready(),
                        "provider_state": self.backend.provider_state(),
                        "model": MODEL_ID,
                        "system_semantics": "prompt_wrapped",
                        "fresh_chat_per_request": True,
                        **self.backend.concurrency_state(),
                        "uptime_seconds": max(0, int(time.time() - self.started_at)),
                    }),
                    origin=origin,
                )
                return
            if path == "/v1/models":
                if not self._authorized(headers):
                    self._send_error(client, 401, "invalid_api_key", "Invalid local adapter bearer token.", origin)
                    return
                self._send(client, 200, self._json_bytes({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "chatgpt-web-test"}]}), origin=origin)
                return
            self._send_error(client, 404, "not_found", "Unknown loopback adapter endpoint.", origin)
            return
        if path == "/control/max-parallel":
            if origin == "__denied__":
                self._send_error(client, 403, "origin_denied", "Browser origin is not allowed by this loopback test adapter.")
                return
            if not self._authorized(headers):
                self._send_error(client, 401, "invalid_api_key", "Invalid local adapter bearer token.", origin)
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict) or set(payload) != {"max_parallel"}:
                    raise ValueError("body must contain only max_parallel")
                state = self.backend.set_max_parallel(payload["max_parallel"])
            except (UnicodeDecodeError, ValueError) as exc:
                self._send_error(client, 400, "invalid_parallel", str(exc), origin)
                return
            self._send(client, 200, self._json_bytes({"ok": True, **state}), origin=origin)
            return
        if path == "/control/open-login":
            if origin == "__denied__":
                self._send_error(client, 403, "origin_denied", "Browser origin is not allowed by this loopback test adapter.")
                return
            if not self._authorized(headers):
                self._send_error(client, 401, "invalid_api_key", "Invalid local adapter bearer token.", origin)
                return
            try:
                opened = self.backend.open_login_page()
            except ExtensionError as exc:
                status = 503 if exc.code == "BROWSER_OFFLINE" else 502
                self._send_error(client, status, exc.code.lower(), str(exc), origin)
                return
            except Exception:
                self._send_error(client, 502, "browser_transport_error", "Could not open the dedicated ChatGPT login page.", origin)
                return
            self._send(client, 200, self._json_bytes({"ok": True, "opened": bool(opened.get("opened"))}), origin=origin)
            return
        if path != "/v1/chat/completions":
            self._send_error(client, 404, "not_found", "Unknown loopback adapter endpoint.", origin)
            return
        if origin == "__denied__":
            self._send_error(client, 403, "origin_denied", "Browser origin is not allowed by this loopback test adapter.")
            return
        if not self._authorized(headers):
            self._send_error(client, 401, "invalid_api_key", "Invalid local adapter bearer token.", origin)
            return
        if not body:
            self._send_error(client, 400, "invalid_request", "Request body size is invalid.", origin)
            return
        try:
            parsed = json.loads(body.decode("utf-8"))
            messages, json_mode, stream, model = validate_chat_request(parsed)
            content = self.backend.complete(messages, json_mode)
        except ExtensionError as exc:
            status = 429 if exc.code == "HISTORY_RATE_LIMITED" else 503 if exc.code in {"WAITING_PROVIDER", "BROWSER_OFFLINE"} else 504 if exc.code == "RESPONSE_TIMEOUT" else 502
            self._send_error(client, status, exc.code.lower(), str(exc), origin)
            return
        except (UnicodeDecodeError, ValueError) as exc:
            self._send_error(client, 400, "invalid_request", str(exc), origin)
            return
        except Exception:
            self._send_error(client, 502, "browser_transport_error", "The ChatGPT web transport failed without exposing page or credential details.", origin)
            return
        response_id = "chatcmpl-web-" + uuid.uuid4().hex
        created = int(time.time())
        fingerprint = "chatgpt-web-test-prompt-wrapped"
        if stream:
            first = {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "system_fingerprint": fingerprint, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}
            final = {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "system_fingerprint": fingerprint, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            payload = ("data: " + json.dumps(first, ensure_ascii=False, separators=(",", ":")) + "\n\n" + "data: " + json.dumps(final, ensure_ascii=False, separators=(",", ":")) + "\n\n" + "data: [DONE]\n\n").encode("utf-8")
            self._send(client, 200, payload, content_type="text/event-stream; charset=utf-8", origin=origin)
            return
        self._send(client, 200, self._json_bytes({"id": response_id, "object": "chat.completion", "created": created, "model": model, "system_fingerprint": fingerprint, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": None}), origin=origin)

    def _serve_client(self, client: socket.socket) -> None:
        try:
            client.settimeout(HTTP_READ_TIMEOUT_SECONDS)
            method, path, headers, body = self._parse_request(client)
            self._dispatch(client, method, path, headers, body)
        except OverflowError:
            try:
                self._send_error(client, 413, "invalid_request", "Request body size is invalid.")
            except OSError:
                pass
        except (ValueError, socket.timeout):
            try:
                self._send_error(client, 400, "invalid_request", "Malformed or incomplete HTTP request.")
            except OSError:
                pass
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass


def health(port: int = PORT, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        value = _http_json(f"http://{HOST}:{port}/health", timeout=timeout)
        if isinstance(value, dict) and value.get("adapter") == ADAPTER_ID:
            return value
    except Exception:
        return None
    return None


def run_service(state_dir: Path, cancel_path: str | None, max_parallel: int, response_timeout_seconds: int) -> dict[str, Any]:
    if health():
        raise ExtensionError("ALREADY_RUNNING", "The ChatGPT Web LLM Test Adapter is already running.", retryable=False)
    token = secrets.token_urlsafe(32)
    controller = BrowserController(state_dir=state_dir, max_parallel=max_parallel, response_timeout_seconds=response_timeout_seconds)
    server: AdapterHttpServer | None = None
    stop_event = threading.Event()
    try:
        controller.start()
        try:
            server = AdapterHttpServer((HOST, PORT), token, controller)
        except OSError as exc:
            raise ExtensionError("PORT_UNAVAILABLE", f"Loopback port {PORT} is unavailable.", retryable=False) from exc
        _atomic_json(connection_path(state_dir), {"version": 1, "active": True, "adapter_version": ADAPTER_VERSION, "base_url": BASE_URL, "model": MODEL_ID, "api_key": token, "pid": os.getpid(), "browser_pid": controller.process.pid if controller.process else None, "max_parallel": max_parallel, "response_timeout_seconds": response_timeout_seconds, "system_semantics": "prompt_wrapped", "fresh_chat_per_request": True, "fresh_chat_min_interval_seconds": FRESH_CHAT_MIN_INTERVAL_SECONDS, "fresh_chat_window_seconds": FRESH_CHAT_WINDOW_SECONDS, "fresh_chat_window_max": FRESH_CHAT_WINDOW_MAX, "temporary_chat_enforced": False, "started_at": int(time.time())})

        def watch_cancel() -> None:
            if not cancel_path:
                return
            marker = Path(cancel_path)
            while not stop_event.wait(0.5):
                if marker.exists():
                    stop_event.set()
                    if server:
                        server.shutdown()
                    return

        watcher = threading.Thread(target=watch_cancel, name="chatgpt-web-adapter-cancel", daemon=True)
        watcher.start()
        server.serve_forever(poll_interval=0.25)
        return {"ok": True, "status": "stopped"}
    finally:
        stop_event.set()
        if server:
            try:
                server.server_close()
            except Exception:
                pass
        controller.stop()
        current = _read_json(connection_path(state_dir)) or {}
        current.update({"active": False, "stopped_at": int(time.time())})
        current.pop("api_key", None)
        _atomic_json(connection_path(state_dir), current)


def connection_info(state_dir: Path, include_token: bool) -> dict[str, Any]:
    record = _read_json(connection_path(state_dir)) or {}
    live = health()
    result: dict[str, Any] = {"ok": True, "running": bool(live), "ready": bool(live and live.get("ready")), "base_url": BASE_URL, "model": MODEL_ID, "system_semantics": "prompt_wrapped", "fresh_chat_per_request": True, "fresh_chat_min_interval_seconds": FRESH_CHAT_MIN_INTERVAL_SECONDS, "fresh_chat_window_seconds": FRESH_CHAT_WINDOW_SECONDS, "fresh_chat_window_max": FRESH_CHAT_WINDOW_MAX, "history_rate_limit_cooldown_seconds": [120, 240, 480, 600], "login_page_reuse": True, "temporary_chat_enforced": False, "intended_use": "test-only semantic acceptance for local OpenAI-compatible clients", "not_equivalent_to_official_api": True}
    if live:
        result["health"] = live
    if include_token and live and record.get("active") and isinstance(record.get("api_key"), str):
        result["api_key"] = record["api_key"]
    return result
