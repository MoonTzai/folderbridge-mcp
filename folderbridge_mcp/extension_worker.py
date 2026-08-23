from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .extensions import MAX_WORKER_REQUEST_BYTES, MAX_WORKER_RESPONSE_BYTES, load_extension, snapshot_extension
from .security import ToolError


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


class _BoundedText(io.TextIOBase):
    def __init__(self, limit: int = 128 * 1024) -> None:
        super().__init__()
        self.limit = limit
        self.parts: list[str] = []
        self.total = 0

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        encoded = text.encode("utf-8", errors="replace")
        self.total += len(encoded)
        current = sum(len(part.encode("utf-8", errors="replace")) for part in self.parts)
        room = self.limit - current
        if room > 0:
            self.parts.append(encoded[:room].decode("utf-8", errors="ignore"))
        return len(text)

    def value(self) -> str:
        text = "".join(self.parts)
        if self.total > self.limit:
            text += "\n... extension output truncated ..."
        return text


def worker_main(extension_path: str, *, bundled: bool = False) -> int:
    try:
        request_bytes = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
        if len(request_bytes) > MAX_WORKER_REQUEST_BYTES:
            return _write_error("EXTENSION_REQUEST_TOO_LARGE", "Extension request exceeds the worker limit.")
        try:
            request = json.loads(request_bytes, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError):
            return _write_error("EXTENSION_PROTOCOL_ERROR", "Worker request must be strict UTF-8 JSON.")
        if not isinstance(request, dict):
            return _write_error("EXTENSION_PROTOCOL_ERROR", "Worker request must be an object.")

        path = Path(extension_path).resolve(strict=True)
        expected_sha256 = request.get("extension_sha256")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            return _write_error("EXTENSION_PROTOCOL_ERROR", "Worker request is missing a valid extension_sha256.")
        with tempfile.TemporaryDirectory(prefix="folderbridge-extension-exec-") as snapshot_parent:
            snapshot_path = snapshot_extension(path, Path(snapshot_parent) / "extension")
            record = load_extension(snapshot_path, bundled=bundled)
            if record.sha256 != expected_sha256:
                return _write_error(
                    "EXTENSION_HASH_MISMATCH",
                    "Extension files changed after host approval and before the private worker snapshot; execution was blocked.",
                    {"expected_sha256": expected_sha256, "actual_sha256": record.sha256},
                )
            action = request.get("action")
            params = request.get("params")
            context = request.get("context")
            if not isinstance(action, str) or action not in record.manifest.actions:
                return _write_error("EXTENSION_ACTION_NOT_FOUND", "Worker action is not declared by the manifest.")
            if not isinstance(params, dict) or not isinstance(context, dict):
                return _write_error("EXTENSION_PROTOCOL_ERROR", "Worker params/context must be objects.")

            entrypoint = record.path / Path(record.manifest.entrypoint)
            spec = importlib.util.spec_from_file_location(
                f"folderbridge_extension_{record.manifest.extension_id.replace('-', '_').replace('.', '_')}",
                entrypoint,
            )
            if spec is None or spec.loader is None:
                return _write_error("EXTENSION_IMPORT_FAILED", "Could not load extension entrypoint.")
            module = importlib.util.module_from_spec(spec)
            old_path = list(sys.path)
            old_cwd = Path.cwd()
            sys.path.insert(0, str(record.path))
            os.chdir(record.path)
            stdout = _BoundedText()
            stderr = _BoundedText()
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    spec.loader.exec_module(module)
                    handler = getattr(module, "handle", None)
                    if not callable(handler):
                        return _write_error("EXTENSION_ABI_ERROR", "Extension entrypoint must define handle(action, params, context).")
                    result = handler(action, params, context)
            finally:
                sys.path[:] = old_path
                os.chdir(old_cwd)
            if not isinstance(result, dict):
                return _write_error("EXTENSION_ABI_ERROR", "Extension handle() must return a JSON object.")
            logs = "\n".join(part for part in (stdout.value().strip(), stderr.value().strip()) if part)
            if logs:
                result.setdefault("extension_worker_log", logs)
            return _write_envelope({"ok": True, "result": result})
    except ToolError as exc:
        return _write_error(exc.code, str(exc), exc.details)
    except Exception as exc:
        return _write_error("EXTENSION_WORKER_EXCEPTION", f"{type(exc).__name__}: {exc}")


def _write_error(code: str, message: str, details: dict[str, Any] | None = None) -> int:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return _write_envelope({"ok": False, "error": error}, exit_code=1)


def _write_envelope(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        data = json.dumps(
            {"ok": False, "error": {"code": "EXTENSION_SERIALIZE_FAILED", "message": str(exc)}},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        exit_code = 1
    if len(data) > MAX_WORKER_RESPONSE_BYTES:
        data = json.dumps(
            {"ok": False, "error": {"code": "EXTENSION_RESPONSE_TOO_LARGE", "message": "Extension response exceeds the worker limit."}},
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        exit_code = 1
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return exit_code
