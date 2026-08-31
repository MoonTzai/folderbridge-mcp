from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .user_paths import user_config_root


FLIGHT_WINDOW_SECONDS = 15 * 60
FLIGHT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
FLIGHT_MAX_TEXT_BYTES = 16 * 1024
FLIGHT_CLEANUP_INTERVAL_SECONDS = 30
FLIGHT_BUCKET_GRACE_SECONDS = 75
FLIGHT_MAX_FILES = 512

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SECRET_KEY_RE = re.compile(r"(?i)(?:api.?key|token|password|passwd|secret|authorization|credential)")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api.?key|token|password|passwd|secret|credential)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(CONTROL_PLANE_API_KEY\s*=\s*)[^\s,;]+"),
)
_TUNNEL_ERROR_RE = re.compile(
    r"(?i)(exceptiongroup|taskgroup|traceback|bad gateway|\b502\b|broken pipe|connection reset|connection closed|"
    r"unexpected eof|transport failure|fatal|panic|\berror\b|\bfailed\b|\bfailure\b)"
)
_TUNNEL_WARNING_RE = re.compile(r"(?i)(\bwarn(?:ing)?\b|retrying|reconnect|timeout|timed out|disconnect)")


def redact_flight_text(value: str) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "<redacted>", text)
        else:
            text = pattern.sub("<redacted>", text)
    return _truncate_utf8(text, FLIGHT_MAX_TEXT_BYTES)


def classify_tunnel_output(text: str) -> str | None:
    value = str(text or "")
    if _TUNNEL_ERROR_RE.search(value):
        return "error"
    if _TUNNEL_WARNING_RE.search(value):
        return "warning"
    return None


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = str(value).encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8", errors="replace")
    marker = b"\n... flight record truncated ..."
    keep = max(0, max_bytes - len(marker))
    prefix = encoded[:keep]
    while prefix:
        try:
            text = prefix.decode("utf-8")
            return text + marker.decode("ascii")
        except UnicodeDecodeError as exc:
            prefix = prefix[:exc.start]
    return marker.decode("ascii")[:max_bytes]


def _safe_scalar(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SECRET_KEY_RE.search(key):
            return "<redacted>"
        return _truncate_utf8(redact_flight_text(value), 2 * 1024)
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(key, item) for item in value[:32]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:64]:
            name = _truncate_utf8(str(raw_key), 128)
            cleaned[name] = _safe_scalar(name, item)
        return cleaned
    return _truncate_utf8(type(value).__name__, 128)


class FlightRecorder:
    """Best-effort local 15-minute diagnostic recorder.

    Writers never share an open file across processes: each process writes a
    role+pid+UTC-minute JSONL shard. Recording failures are swallowed and counted
    in-process so diagnostics can never become an MCP/Tunnel failure source.
    """

    def __init__(
        self,
        role: str,
        *,
        root: Path | None = None,
        window_seconds: int = FLIGHT_WINDOW_SECONDS,
        max_total_bytes: int = FLIGHT_MAX_TOTAL_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
            raise ValueError("invalid flight recorder role")
        if window_seconds < 60 or window_seconds > FLIGHT_WINDOW_SECONDS:
            raise ValueError("window_seconds must be between 60 and 900")
        if max_total_bytes < 1024:
            raise ValueError("max_total_bytes must be at least 1024")
        self.role = role
        self.root = (Path(root) if root is not None else user_config_root()) / "flight-recorder"
        self.window_seconds = int(window_seconds)
        self.max_total_bytes = int(max_total_bytes)
        self._clock = clock or time.time
        self._pid = os.getpid()
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._cleanup_running = False
        self._last_cleanup_monotonic = 0.0
        self._dropped_events = 0

    @property
    def dropped_events(self) -> int:
        with self._state_lock:
            return self._dropped_events

    def record(self, event: str, *, severity: str = "info", text: str | None = None, **fields: Any) -> bool:
        try:
            if not isinstance(event, str) or not _EVENT_RE.fullmatch(event):
                raise ValueError("invalid event")
            level = severity if severity in {"info", "warning", "error"} else "info"
            now = float(self._clock())
            payload: dict[str, Any] = {
                "schema": 1,
                "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "ts_unix": round(now, 3),
                "role": self.role,
                "pid": self._pid,
                "event": event,
                "severity": level,
            }
            if text is not None:
                payload["text"] = redact_flight_text(text)
            for raw_key, value in fields.items():
                key = _truncate_utf8(str(raw_key), 128)
                payload[key] = _safe_scalar(key, value)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
            with self._write_lock:
                self.root.mkdir(parents=True, exist_ok=True)
                path = self._bucket_path(now)
                with path.open("ab", buffering=0) as handle:
                    handle.write(line)
            self._schedule_cleanup()
            return True
        except Exception:
            with self._state_lock:
                self._dropped_events += 1
            return False

    def recent(
        self,
        *,
        minutes: int = 15,
        limit: int = 100,
        errors_only: bool = False,
        role: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1 or minutes > 15:
            raise ValueError("minutes must be an integer from 1 to 15")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
            raise ValueError("limit must be an integer from 1 to 200")
        if role is not None and (not isinstance(role, str) or not _ROLE_RE.fullmatch(role)):
            raise ValueError("role is invalid")
        self.cleanup()
        now = float(self._clock())
        cutoff = now - minutes * 60
        matches: list[dict[str, Any]] = []
        for path in self._candidate_files(cutoff):
            try:
                with path.open("rb") as handle:
                    for raw in handle:
                        if len(raw) > FLIGHT_MAX_TEXT_BYTES + 8 * 1024:
                            continue
                        try:
                            event = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        ts = event.get("ts_unix")
                        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or float(ts) < cutoff:
                            continue
                        if role is not None and event.get("role") != role:
                            continue
                        if errors_only and event.get("severity") not in {"warning", "error"}:
                            continue
                        matches.append(event)
            except OSError:
                continue
        matches.sort(key=lambda item: (float(item.get("ts_unix", 0)), int(item.get("pid", 0))))
        total = len(matches)
        selected = matches[-limit:]
        stats = self._storage_stats()
        return {
            "window_minutes": minutes,
            "errors_only": bool(errors_only),
            "role_filter": role,
            "returned": len(selected),
            "total_matching": total,
            "truncated": total > len(selected),
            "events": selected,
            "storage": stats,
        }

    def export_recent_jsonl(self, *, minutes: int = 15) -> dict[str, Any]:
        """Return the complete recent window as bounded, re-sanitized JSONL for local GUI export."""
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1 or minutes > 15:
            raise ValueError("minutes must be an integer from 1 to 15")
        self.cleanup()
        now = float(self._clock())
        cutoff = now - minutes * 60
        matches: list[tuple[float, int, bytes]] = []
        for path in self._candidate_files(cutoff):
            try:
                with path.open("rb") as handle:
                    for raw in handle:
                        if len(raw) > FLIGHT_MAX_TEXT_BYTES + 8 * 1024:
                            continue
                        try:
                            event = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        ts = event.get("ts_unix")
                        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or float(ts) < cutoff:
                            continue
                        sanitized: dict[str, Any] = {}
                        for raw_key, value in list(event.items())[:128]:
                            key = _truncate_utf8(str(raw_key), 128)
                            sanitized[key] = _safe_scalar(key, value)
                        try:
                            line = json.dumps(
                                sanitized,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8") + b"\n"
                        except (TypeError, ValueError):
                            continue
                        raw_pid = event.get("pid", 0)
                        try:
                            sort_pid = int(raw_pid) if not isinstance(raw_pid, bool) else 0
                        except (TypeError, ValueError, OverflowError):
                            sort_pid = 0
                        matches.append((float(ts), sort_pid, line))
            except OSError:
                continue
        matches.sort(key=lambda item: (item[0], item[1]))
        total_matching = len(matches)
        total_bytes = sum(len(item[2]) for item in matches)
        truncated = total_bytes > self.max_total_bytes
        if truncated:
            selected_reversed: list[tuple[float, int, bytes]] = []
            kept_bytes = 0
            for item in reversed(matches):
                size = len(item[2])
                if kept_bytes + size > self.max_total_bytes:
                    continue
                selected_reversed.append(item)
                kept_bytes += size
            selected = list(reversed(selected_reversed))
        else:
            selected = matches
        data = b"".join(item[2] for item in selected)
        return {
            "window_minutes": minutes,
            "event_count": len(selected),
            "total_matching": total_matching,
            "bytes": len(data),
            "truncated": truncated or len(selected) < total_matching,
            "data": data,
        }

    def status(self) -> dict[str, Any]:
        self.cleanup()
        stats = self._storage_stats()
        return {
            "enabled": True,
            "window_minutes": self.window_seconds // 60,
            "max_total_bytes": self.max_total_bytes,
            "max_text_bytes": FLIGHT_MAX_TEXT_BYTES,
            "storage_dir": str(self.root),
            "dropped_events_this_process": self.dropped_events,
            **stats,
        }

    def cleanup(self) -> dict[str, int]:
        deleted = 0
        now = float(self._clock())
        try:
            if not self.root.exists() or not self.root.is_dir():
                return {"deleted_files": 0, "total_bytes": 0, "file_count": 0}
            files = self._all_files()
            age_cutoff = now - self.window_seconds - FLIGHT_BUCKET_GRACE_SECONDS
            for path in files:
                try:
                    if path.stat().st_mtime < age_cutoff:
                        path.unlink()
                        deleted += 1
                except OSError:
                    continue
            files = self._all_files()
            sized: list[tuple[float, int, Path]] = []
            total = 0
            for path in files:
                try:
                    stat_result = path.stat()
                except OSError:
                    continue
                total += stat_result.st_size
                sized.append((stat_result.st_mtime, stat_result.st_size, path))
            sized.sort(key=lambda item: (item[0], item[2].name))
            while (total > self.max_total_bytes or len(sized) > FLIGHT_MAX_FILES) and sized:
                _, size, path = sized.pop(0)
                try:
                    path.unlink()
                    total -= size
                    deleted += 1
                except OSError:
                    continue
            return {"deleted_files": deleted, "total_bytes": max(0, total), "file_count": len(sized)}
        except Exception:
            return {"deleted_files": deleted, "total_bytes": 0, "file_count": 0}

    def _bucket_path(self, now: float) -> Path:
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M")
        return self.root / f"{self.role}-{self._pid}-{stamp}.jsonl"

    def _all_files(self) -> list[Path]:
        try:
            return [path for path in self.root.glob("*.jsonl") if path.is_file() and not path.is_symlink()]
        except OSError:
            return []

    def _candidate_files(self, cutoff: float) -> list[Path]:
        files: list[tuple[float, Path]] = []
        for path in self._all_files():
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified >= cutoff - FLIGHT_BUCKET_GRACE_SECONDS:
                files.append((modified, path))
        files.sort(key=lambda item: (item[0], item[1].name))
        return [path for _, path in files]

    def _storage_stats(self) -> dict[str, int]:
        total = 0
        count = 0
        for path in self._all_files():
            try:
                total += path.stat().st_size
                count += 1
            except OSError:
                continue
        return {"total_bytes": total, "file_count": count}

    def _schedule_cleanup(self) -> None:
        now = time.monotonic()
        with self._state_lock:
            if self._cleanup_running or now - self._last_cleanup_monotonic < FLIGHT_CLEANUP_INTERVAL_SECONDS:
                return
            self._cleanup_running = True
            self._last_cleanup_monotonic = now
        thread = threading.Thread(target=self._cleanup_worker, name=f"folderbridge-flight-{self.role}", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._state_lock:
                self._cleanup_running = False

    def _cleanup_worker(self) -> None:
        try:
            self.cleanup()
        finally:
            with self._state_lock:
                self._cleanup_running = False
