from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from folderbridge_mcp.extension_api import (
        ExtensionError,
        owned_process_group_kwargs,
        terminate_owned_process_tree,
    )
except ImportError:  # pragma: no cover - standalone test compatibility.
    class ExtensionError(RuntimeError):
        def __init__(self, code: str, message: str, **details: Any) -> None:
            super().__init__(message)
            self.code = code
            self.message = message
            self.details = dict(details)

    def owned_process_group_kwargs(*, hide_window: bool = False) -> dict[str, Any]:
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if hide_window else 0
        return {"creationflags": flags, "start_new_session": sys.platform != "win32"}

    def terminate_owned_process_tree(process: Any, *, hide_window: bool = False, **_kwargs: Any) -> None:
        del hide_window
        try:
            process.kill()
        except OSError:
            pass


PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DOTNET_DIR = PLUGIN_DIR / "_vendor-dotnet"
PROVENANCE_PATH = PLUGIN_DIR / "VENDOR-PROVENANCE.json"
PDF_INSPECT_SCRIPT = PLUGIN_DIR / "pdf_inspect.ps1"
PDF_RENDER_SCRIPT = PLUGIN_DIR / "pdf_render.ps1"

PINNED_PDFPIG_VERSION = "0.1.16"
CASEFOLD_UNICODE_VERSION = "14.0.0"
INSPECT_STDOUT_LIMIT = 8 * 1024 * 1024
INSPECT_STDERR_LIMIT = 256 * 1024
INSPECT_REQUEST_LIMIT = 64 * 1024
INSPECT_TIMEOUT_SECONDS = 570
INSPECT_ALLOWED_ERROR_CODES = {
    "PAGE_RANGE_INVALID",
    "PAGE_RANGE_TOO_LARGE",
    "QUERY_EMPTY",
    "SOURCE_CHANGED_DURING_CALL",
    "PDF_OPEN_FAILED",
    "PDF_PASSWORD_REQUIRED",
    "PDF_TEXT_EXTRACT_FAILED",
    "PDF_PAGE_GEOMETRY_FAILED",
    "PDF_VENDOR_PROVENANCE_MISSING",
    "PDF_VENDOR_PROVENANCE_INVALID",
    "PDF_VENDOR_PROVENANCE_MISMATCH",
    "PDF_BACKEND_UNTRUSTED",
    "PDF_BACKEND_UNAVAILABLE",
    "PDF_BACKEND_VERSION_MISMATCH",
    "PDF_INSPECT_PROTOCOL_ERROR",
}

DENIED_PARTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    ".build", "dist", "build", "target", "vendor", ".idea", ".vscode",
}
SENSITIVE_BASENAMES = {
    ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", "credentials.json",
}
SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".kdbx", ".keystore"}
WINDOWS_RESERVED_BASENAME = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
WINDOWS_INVALID_CHARS = set('<>:"|?*')

MAX_PDF_BYTES = 512 * 1024 * 1024
MAX_READ_PAGES = 50
MAX_SEARCH_PAGES = 500
MAX_PAGE_TEXT_CHARS = 1_000_000
MAX_METADATA_VALUE_CHARS = 4_096
MAX_TOC_TITLE_CHARS = 512
MAX_RENDER_PAGES = 100
MAX_RENDER_DPI = 400
MIN_RENDER_DPI = 72
MAX_RENDER_PIXELS_PER_PAGE = 30_000_000
MAX_RENDER_PIXELS_TOTAL = 200_000_000
MAX_RENDER_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_HASH_CHUNK = 1024 * 1024
TOC_MAX_DEPTH = 15
POWERSHELL_STDOUT_LIMIT = 1024 * 1024
POWERSHELL_STDERR_LIMIT = 1024 * 1024
CONTENT_TRUST_NOTE = (
    "PDF metadata, bookmarks, and extracted text are document-supplied/untrusted content. "
    "Visually verify critical, layout-sensitive, or rule-text claims with render-pages."
)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _status()
    root = _workspace_root(context)
    if action == "info":
        return _info(root, params, context)
    if action == "outline":
        return _outline(root, params, context)
    if action == "read-pages":
        return _read_pages(root, params, context)
    if action == "search":
        return _search(root, params, context)
    if action == "render-pages":
        if bool(context.get("workspace_read_only")):
            raise ExtensionError("READ_ONLY", "FolderBridge is read-only; PDF rendering writes workspace artifacts.")
        return _render_pages(root, params, context)
    raise ExtensionError("UNSUPPORTED_ACTION", f"unsupported action: {action}")


class _BoundedCapture(threading.Thread):
    def __init__(self, stream: Any, limit: int, overflow: threading.Event) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.overflow = overflow
        self.data = bytearray()
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    return
                self.total += len(chunk)
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if self.total > self.limit:
                    self.overflow.set()
        except Exception:
            self.overflow.set()


def _cancel_requested(cancel_path: str | None) -> bool:
    return bool(cancel_path and Path(cancel_path).is_file())


def _job_cancel_path(context: dict[str, Any] | None) -> str | None:
    raw = (context or {}).get("job_cancel_path")
    return raw if isinstance(raw, str) and raw else None


def _run_inspector(request: dict[str, Any], *, cancel_path: str | None = None) -> dict[str, Any]:
    if _cancel_requested(cancel_path):
        raise ExtensionError("PDF_INSPECT_CANCELLED", "PDF inspection was cancelled before the inspector started.")
    if sys.platform != "win32":
        raise ExtensionError("PDF_BACKEND_UNAVAILABLE", "PdfPig inspection requires Windows PowerShell 5.1 on Windows.")
    powershell = shutil.which("powershell.exe")
    if not powershell or not PDF_INSPECT_SCRIPT.is_file():
        raise ExtensionError("PDF_BACKEND_UNAVAILABLE", "Required powershell.exe or bundled pdf_inspect.ps1 is unavailable.")
    try:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "Inspection request could not be encoded as strict UTF-8 JSON.") from exc
    if len(payload) > INSPECT_REQUEST_LIMIT:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_TOO_LARGE", "Inspection request exceeds the 64 KiB protocol limit.")
    argv = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PDF_INSPECT_SCRIPT),
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(PDF_INSPECT_SCRIPT.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise ExtensionError("PDF_BACKEND_UNAVAILABLE", "Could not start the fixed PDF inspector process.") from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    overflow = threading.Event()
    stdout_capture = _BoundedCapture(process.stdout, INSPECT_STDOUT_LIMIT, overflow)
    stderr_capture = _BoundedCapture(process.stderr, INSPECT_STDERR_LIMIT, overflow)
    stdout_capture.start()
    stderr_capture.start()
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        try:
            process.stdin.close()
        except OSError:
            pass
    deadline = time.monotonic() + INSPECT_TIMEOUT_SECONDS
    reason: str | None = None
    while process.poll() is None:
        if overflow.is_set():
            reason = "overflow"
            break
        if _cancel_requested(cancel_path):
            reason = "cancel"
            break
        if time.monotonic() >= deadline:
            reason = "timeout"
            break
        time.sleep(0.02)
    if reason is not None:
        terminate_owned_process_tree(process, hide_window=True)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    stdout_capture.join(timeout=2)
    stderr_capture.join(timeout=2)
    try:
        process.stdout.close()
    except OSError:
        pass
    try:
        process.stderr.close()
    except OSError:
        pass
    if reason == "cancel":
        raise ExtensionError("PDF_INSPECT_CANCELLED", "PDF inspection was cancelled.")
    if reason == "timeout":
        raise ExtensionError("PDF_INSPECT_TIMEOUT", "PDF inspection exceeded its internal 570-second limit.")
    if reason == "overflow" or overflow.is_set():
        raise ExtensionError("PDF_INSPECT_PROTOCOL_TOO_LARGE", "PDF inspector exceeded its bounded stdout/stderr protocol limits.")
    stdout_bytes = bytes(stdout_capture.data)
    stderr_bytes = bytes(stderr_capture.data)
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector emitted invalid UTF-8.") from exc
    if process.returncode != 0 or stderr_text:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector terminated without a trustworthy controlled envelope.")
    try:
        envelope = json.loads(stdout_text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector returned malformed or extra stdout JSON.") from exc
    if not isinstance(envelope, dict) or envelope.get("protocol") != 1 or not isinstance(envelope.get("ok"), bool):
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector returned an invalid protocol-v1 envelope.")
    expected_keys = {"protocol", "ok", "result"} if envelope["ok"] else {"protocol", "ok", "error"}
    if set(envelope) != expected_keys:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector envelope contained missing or unexpected fields.")
    if envelope["ok"]:
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector success result must be an object.")
        return result
    error = envelope.get("error")
    if not isinstance(error, dict) or set(error) != {"code", "message", "details"}:
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector controlled error envelope is invalid.")
    code = error.get("code")
    message = error.get("message")
    details = error.get("details")
    if code not in INSPECT_ALLOWED_ERROR_CODES or not isinstance(message, str) or len(message) > 4096 or not isinstance(details, dict):
        raise ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", "PDF inspector returned an unapproved controlled error.")
    raise ExtensionError(str(code), message, **details)


def _load_vendor_provenance() -> dict[str, Any]:
    if not PROVENANCE_PATH.is_file():
        raise ExtensionError("PDF_VENDOR_PROVENANCE_MISSING", "VENDOR-PROVENANCE.json is required in the approved extension tree.")
    try:
        raw = PROVENANCE_PATH.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ExtensionError("PDF_VENDOR_PROVENANCE_INVALID", "VENDOR-PROVENANCE.json is unexpectedly large.")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ExtensionError("PDF_VENDOR_PROVENANCE_INVALID", "VENDOR-PROVENANCE.json must be BOM-less UTF-8.")
        data = json.loads(raw.decode("utf-8", errors="strict"), parse_constant=_reject_json_constant)
    except ExtensionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtensionError("PDF_VENDOR_PROVENANCE_INVALID", "Could not read schema-v3 vendor provenance.") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 3:
        raise ExtensionError("PDF_VENDOR_PROVENANCE_INVALID", "Vendor provenance must be a schema-v3 JSON object.")
    if data.get("extension_version") != "0.6.0" or data.get("pdfpig_version") != PINNED_PDFPIG_VERSION:
        raise ExtensionError("PDF_VENDOR_PROVENANCE_MISMATCH", "Vendor provenance does not match the locked PDF Toolkit/PdfPig versions.")
    if data.get("casefold_unicode_version") != CASEFOLD_UNICODE_VERSION:
        raise ExtensionError("PDF_VENDOR_PROVENANCE_MISMATCH", "Vendor provenance does not match the locked Unicode casefold version.")
    runtime_dlls = data.get("runtime_dlls")
    if not isinstance(runtime_dlls, list) or len(runtime_dlls) != 12:
        raise ExtensionError("PDF_VENDOR_PROVENANCE_MISMATCH", "Vendor provenance must declare exactly twelve runtime DLLs.")
    return data


def _status() -> dict[str, Any]:
    provenance = None
    inspection_result: dict[str, Any] | None = None
    inspection_error: dict[str, str] | None = None
    try:
        provenance = _load_vendor_provenance()
        inspection_result = _run_inspector({"protocol": 1, "action": "status"})
        inspection_ready = bool(
            inspection_result.get("inspection_ready") is True
            and inspection_result.get("pdfpig_version") == PINNED_PDFPIG_VERSION
            and inspection_result.get("casefold_unicode_version") == CASEFOLD_UNICODE_VERSION
        )
        loaded_version = PINNED_PDFPIG_VERSION if inspection_ready else None
        if not inspection_ready:
            inspection_error = {"code": "PDF_INSPECT_PROTOCOL_ERROR", "message": "Inspector status result failed locked readiness validation."}
    except ExtensionError as exc:
        inspection_ready = False
        loaded_version = None
        inspection_error = {"code": str(getattr(exc, "code", "PDF_BACKEND_UNAVAILABLE")), "message": str(getattr(exc, "message", str(exc)))[:4096]}

    powershell = shutil.which("powershell.exe") if sys.platform == "win32" else None
    render_ready = bool(sys.platform == "win32" and powershell and PDF_RENDER_SCRIPT.is_file())
    ready = inspection_ready and render_ready
    return {
        "ready": ready,
        "backend": "PdfPig + Windows.Data.Pdf",
        "text_backend": "PdfPig via Windows PowerShell 5.1",
        "renderer": "Windows.Data.Pdf via powershell.exe",
        "inspection_ready": inspection_ready,
        "pinned_pdfpig_version": PINNED_PDFPIG_VERSION,
        "loaded_pdfpig_version": loaded_version,
        "vendor_provenance": provenance,
        "vendor_dir_present": VENDOR_DOTNET_DIR.is_dir(),
        "powershell": powershell,
        "pdf_inspect_script_present": PDF_INSPECT_SCRIPT.is_file(),
        "pdf_render_script_present": PDF_RENDER_SCRIPT.is_file(),
        "casefold_unicode_version": CASEFOLD_UNICODE_VERSION,
        "capabilities": {
            "metadata": inspection_ready,
            "outline": inspection_ready,
            "text_layer": inspection_ready,
            "literal_search": inspection_ready,
            "xmp_metadata": False,
            "page_render_png": render_ready,
            "ocr": False,
            "semantic_search": False,
            "pdf_mutation": False,
        },
        "policy": {
            "workspace_relative_paths_only": True,
            "runtime_network_access": False,
            "password_input": False,
            "vendored_text_backend_only": True,
            "renderer_is_fixed_script_only": True,
            "text_is_untrusted_document_content": True,
            "critical_claims_should_be_visually_verified": True,
            "parser_memory_sandbox": False,
            "deterministic_casefold": True,
            "casefold_unicode_version": CASEFOLD_UNICODE_VERSION,
            "max_pdf_bytes": MAX_PDF_BYTES,
            "max_read_pages_per_call": MAX_READ_PAGES,
            "max_search_pages_per_call": MAX_SEARCH_PAGES,
            "max_page_text_chars": MAX_PAGE_TEXT_CHARS,
            "max_render_pages_per_call": MAX_RENDER_PAGES,
            "min_render_dpi": MIN_RENDER_DPI,
            "max_render_dpi": MAX_RENDER_DPI,
            "max_render_pixels_per_page": MAX_RENDER_PIXELS_PER_PAGE,
            "max_render_pixels_total": MAX_RENDER_PIXELS_TOTAL,
            "max_render_artifact_bytes": MAX_RENDER_ARTIFACT_BYTES,
            "render_output_directory_must_be_new": True,
            "render_completion_marker": "RENDER-COMPLETE.json",
            "toc_max_depth": TOC_MAX_DEPTH,
        },
        "error": None if ready else {
            "text_backend": inspection_error,
            "renderer": None if render_ready else "Windows renderer requires Windows, powershell.exe, and bundled pdf_render.ps1.",
        },
        "install_hint": None if ready else (
            "Install the reviewed PDF Toolkit 0.6.0 tree, then rescan and re-approve its exact tree hash in FolderBridge Extensions & Skills."
        ),
    }


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise ExtensionError("WORKSPACE_REQUIRED", "workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise ExtensionError("WORKSPACE_INVALID", "workspace_root is not a directory")
    return root


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _clean_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ExtensionError("INVALID_PATH", "paths must be non-empty POSIX-style workspace-relative strings")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ExtensionError("PATH_ESCAPE", "path must stay inside the selected workspace")
    for part in rel.parts:
        if not part or part in {".", ".."}:
            raise ExtensionError("INVALID_PATH", "path contains an empty or dot segment")
        if part.endswith(".") or part.endswith(" "):
            raise ExtensionError("INVALID_PATH", "Windows-normalized trailing dot/space path segments are not allowed")
        if any(ord(char) < 32 or char in WINDOWS_INVALID_CHARS for char in part):
            raise ExtensionError("INVALID_PATH", "path contains Windows-reserved characters")
        reserved_base = part.split(".", 1)[0]
        if WINDOWS_RESERVED_BASENAME.fullmatch(reserved_base):
            raise ExtensionError("INVALID_PATH", "Windows device-name path segments are not allowed")
    lowered = [part.lower() for part in rel.parts]
    if any(part in DENIED_PARTS for part in lowered):
        raise ExtensionError("DENIED_PATH", "path targets a denied dependency/VCS/build directory")
    base = lowered[-1]
    suffix = PurePosixPath(base).suffix.lower()
    if base in SENSITIVE_BASENAMES or suffix in SENSITIVE_SUFFIXES:
        raise ExtensionError("SENSITIVE_PATH", "credential/key-like paths are not allowed")
    return rel


def _reject_links(root: Path, candidate: Path) -> None:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise ExtensionError("PATH_ESCAPE", "path escapes workspace") from exc
    current = root
    for part in parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink() or _is_reparse(current):
            raise ExtensionError("LINKED_PATH", f"linked/reparse path component is not allowed: {part}")


def _resolve_existing_pdf(root: Path, raw: str) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ExtensionError("PDF_NOT_FOUND", "PDF must exist inside the selected workspace") from exc
    if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
        raise ExtensionError("PDF_INVALID", "input must be a regular non-link file")
    if resolved.suffix.lower() != ".pdf":
        raise ExtensionError("PDF_REQUIRED", "input must have a .pdf extension")
    size = resolved.stat().st_size
    if size > MAX_PDF_BYTES:
        raise ExtensionError("PDF_TOO_LARGE", f"PDF exceeds the {MAX_PDF_BYTES} byte input limit.", bytes=size)
    return resolved


def _create_fresh_output_dir(root: Path, raw: str) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    parent = candidate.parent
    _reject_links(root, parent)
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ExtensionError("OUTPUT_PARENT_MISSING", "output_dir parent must already exist inside the workspace") from exc
    if not resolved_parent.is_dir() or resolved_parent.is_symlink() or _is_reparse(resolved_parent):
        raise ExtensionError("OUTPUT_PATH_INVALID", "output_dir parent must be a regular non-link directory")
    if candidate.exists() or candidate.is_symlink():
        raise ExtensionError("OUTPUT_EXISTS", "render output_dir must not already exist; choose a fresh directory")
    candidate.mkdir(parents=False, exist_ok=False)
    resolved = candidate.resolve(strict=True)
    if resolved.is_symlink() or _is_reparse(resolved):
        raise ExtensionError("OUTPUT_PATH_INVALID", "new output_dir must be a regular non-link directory")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(MAX_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
    )


def _capture_source_identity(path: Path) -> dict[str, Any]:
    before = _stat_signature(path)
    size = before[0]
    if size > MAX_PDF_BYTES:
        raise ExtensionError("PDF_TOO_LARGE", f"PDF exceeds the {MAX_PDF_BYTES} byte input limit.", bytes=size)
    digest = _sha256_file(path)
    after = _stat_signature(path)
    if before != after:
        raise ExtensionError("SOURCE_CHANGED_DURING_CALL", "PDF changed while its source identity was being captured.")
    return {"bytes": size, "sha256": digest, "signature": before}


def _assert_source_unchanged(path: Path, identity: dict[str, Any]) -> None:
    if _stat_signature(path) != tuple(identity["signature"]):
        raise ExtensionError("SOURCE_CHANGED_DURING_CALL", "PDF changed while the action was running; discard the result.")


def _source_fields(path: Path, root: Path, identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": int(identity["bytes"]),
        "sha256": str(identity["sha256"]),
    }


def _open_document(path: Path) -> Any:
    del path
    raise ExtensionError(
        "PDF_BACKEND_UNAVAILABLE",
        "In-process PDF parsing is disabled in v0.6; inspection must use the fixed out-of-process PdfPig seam.",
    )


def _normalize_pdf_text(text: str) -> str:
    return str(text).replace("\ufffe", "-").replace("\r\n", "\n").replace("\r", "\n")


def _page_text_bounded(doc: Any, index: int, *, hard_cap: int = MAX_PAGE_TEXT_CHARS) -> dict[str, Any]:
    try:
        text = doc.pages[index].extract_text() or ""
    except Exception as exc:  # noqa: BLE001
        raise ExtensionError("PDF_TEXT_EXTRACT_FAILED", f"Could not extract text from page {index + 1}: {type(exc).__name__}: {exc}") from exc
    normalized = _normalize_pdf_text(text)
    page_chars = len(normalized)
    requested = min(page_chars, max(0, int(hard_cap)))
    return {
        "text": normalized[:requested],
        "page_chars": page_chars,
        "requested_chars": requested,
        "extracted_chars": requested,
        "text_truncated": page_chars > requested,
    }


def _page_size(doc: Any, index: int) -> dict[str, Any]:
    try:
        box = doc.pages[index].mediabox
        width = float(box.width)
        height = float(box.height)
    except Exception as exc:  # noqa: BLE001
        raise ExtensionError("PDF_PAGE_GEOMETRY_FAILED", f"Could not read page {index + 1} geometry: {type(exc).__name__}: {exc}") from exc
    if width <= 0 or height <= 0:
        raise ExtensionError("PDF_PAGE_GEOMETRY_FAILED", f"Page {index + 1} has invalid dimensions.")
    return {
        "page": index + 1,
        "width_points": round(width, 3),
        "height_points": round(height, 3),
    }


def _sample_indices(page_count: int, count: int) -> list[int]:
    if page_count <= 0 or count <= 0:
        return []
    count = min(page_count, count)
    if count == 1:
        return [0]
    return sorted({int(round(i * (page_count - 1) / (count - 1))) for i in range(count)})


def _bounded_string(value: Any, maximum: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    return (text[:maximum], len(text) > maximum)


def _metadata(doc: Any) -> dict[str, Any]:
    raw = getattr(doc, "metadata", None) or {}
    truncated_fields: list[str] = []
    key_map = {
        "title": "/Title",
        "author": "/Author",
        "subject": "/Subject",
        "keywords": "/Keywords",
        "creator": "/Creator",
        "producer": "/Producer",
        "creation_date": "/CreationDate",
        "modification_date": "/ModDate",
    }
    result: dict[str, Any] = {}
    for output, source in key_map.items():
        try:
            value = raw.get(source, "")
        except Exception:
            value = ""
        text, truncated = _bounded_string(value, MAX_METADATA_VALUE_CHARS)
        result[output] = text
        if truncated:
            truncated_fields.append(output)
    header = str(getattr(doc, "pdf_header", "") or "")
    result["format"] = header.lstrip("%") if header.startswith("%PDF-") else "PDF"
    result["truncated_fields"] = truncated_fields
    return result


def _toc_entries(doc: Any, max_items: int) -> tuple[list[dict[str, Any]], int | None, int, bool, list[str]]:
    items: list[dict[str, Any]] = []
    seen = 0
    truncation_reasons: set[str] = set()

    try:
        outline = doc.outline
    except Exception:
        outline = []

    def visit(sequence: Any, depth: int) -> bool:
        nonlocal seen
        if depth > TOC_MAX_DEPTH:
            truncation_reasons.add("max_depth")
            return True
        if not isinstance(sequence, list):
            sequence = [sequence]
        for item in sequence:
            if isinstance(item, list):
                if visit(item, depth + 1):
                    return True
                continue
            seen += 1
            if seen > max_items:
                truncation_reasons.add("max_items")
                return True
            try:
                title = getattr(item, "title", item)
            except Exception:
                title = ""
            title_text, title_truncated = _bounded_string(title, MAX_TOC_TITLE_CHARS)
            try:
                page_index = int(doc.get_destination_page_number(item))
            except Exception:
                page_index = -1
            items.append({
                "level": depth,
                "title": title_text,
                "title_truncated": title_truncated,
                "page": page_index + 1 if page_index >= 0 else None,
            })
        return False

    truncated = visit(outline, 1)
    total = None if truncated else seen
    return items, total, seen, truncated, sorted(truncation_reasons)


def _validated_range(page_count: int, start: int, end: int, max_pages: int, purpose: str) -> tuple[int, int]:
    if start < 1 or end < start or end > page_count:
        raise ExtensionError("PAGE_RANGE_INVALID", f"{purpose} page range must satisfy 1 <= start <= end <= {page_count}")
    if end - start + 1 > max_pages:
        raise ExtensionError("PAGE_RANGE_TOO_LARGE", f"{purpose} supports at most {max_pages} pages per call")
    return start, end


def _validated_search_range(page_count: int, start: int, end: int) -> tuple[int, int]:
    return _validated_range(page_count, start, end, MAX_SEARCH_PAGES, "search")


def _inspect_protocol_error(message: str) -> ExtensionError:
    return ExtensionError("PDF_INSPECT_PROTOCOL_ERROR", message)


def _require_exact_keys(value: Any, expected: set[str], surface: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _inspect_protocol_error(f"Inspector {surface} object has missing or unexpected fields.")
    return value


def _require_int(value: Any, surface: str, *, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise _inspect_protocol_error(f"Inspector {surface} integer is outside the locked contract.")
    return value


def _require_bool(value: Any, surface: str) -> bool:
    if type(value) is not bool:
        raise _inspect_protocol_error(f"Inspector {surface} boolean is invalid.")
    return value


def _require_text(value: Any, surface: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise _inspect_protocol_error(f"Inspector {surface} string is invalid or over limit.")
    return value


def _require_nullable_int(value: Any, surface: str, *, minimum: int = 0, maximum: int = 2_147_483_647) -> int | None:
    if value is None:
        return None
    return _require_int(value, surface, minimum=minimum, maximum=maximum)


def _inspection_call(
    root: Path,
    params: dict[str, Any],
    context: dict[str, Any] | None,
    request: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    path = _resolve_existing_pdf(root, params["path"])
    identity = _capture_source_identity(path)
    protocol_request = {"protocol": 1, "action": request["action"], "path": str(path)}
    protocol_request.update({key: value for key, value in request.items() if key != "action"})
    result = _run_inspector(protocol_request, cancel_path=_job_cancel_path(context))
    _assert_source_unchanged(path, identity)
    if result.get("action") != request["action"]:
        raise _inspect_protocol_error("Inspector result action does not match the request.")
    return path, identity, result


def _validate_outline_item(item: Any, page_count: int) -> dict[str, Any]:
    data = _require_exact_keys(item, {"level", "title", "title_truncated", "page"}, "outline item")
    _require_int(data["level"], "outline.level", minimum=1, maximum=TOC_MAX_DEPTH)
    _require_text(data["title"], "outline.title", MAX_TOC_TITLE_CHARS)
    _require_bool(data["title_truncated"], "outline.title_truncated")
    page = _require_nullable_int(data["page"], "outline.page", minimum=1, maximum=max(1, page_count))
    if page_count == 0 and page is not None:
        raise _inspect_protocol_error("Inspector outline page is impossible for an empty document.")
    return data


def _info(root: Path, params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    max_outline = int(params.get("max_outline_items", 40))
    text_sample_pages = int(params.get("text_sample_pages", 8))
    path, identity, body = _inspection_call(
        root,
        params,
        context,
        {
            "action": "info",
            "max_outline_items": max_outline,
            "text_sample_pages": text_sample_pages,
        },
    )
    expected = {
        "action", "page_count", "metadata", "outline", "outline_total", "outline_items_seen_at_least",
        "outline_truncated", "outline_truncation_reasons", "outline_max_depth", "sample_page_sizes",
        "text_layer_sample", "text_sample_complete", "text_sample_errors", "scan_candidate", "scan_candidate_note",
    }
    _require_exact_keys(body, expected, "info result")
    page_count = _require_int(body["page_count"], "info.page_count", minimum=1)
    metadata = _require_exact_keys(
        body["metadata"],
        {"title", "author", "subject", "keywords", "creator", "producer", "creation_date", "modification_date", "format", "truncated_fields"},
        "metadata",
    )
    for field in ("title", "author", "subject", "keywords", "creator", "producer", "creation_date", "modification_date"):
        _require_text(metadata[field], f"metadata.{field}", MAX_METADATA_VALUE_CHARS)
    _require_text(metadata["format"], "metadata.format", 64)
    truncated_fields = metadata["truncated_fields"]
    allowed_metadata_fields = {"title", "author", "subject", "keywords", "creator", "producer", "creation_date", "modification_date"}
    if not isinstance(truncated_fields, list) or any(item not in allowed_metadata_fields for item in truncated_fields):
        raise _inspect_protocol_error("Inspector metadata.truncated_fields is invalid.")
    outline = body["outline"]
    if not isinstance(outline, list) or len(outline) > max_outline:
        raise _inspect_protocol_error("Inspector info outline exceeds the requested bound.")
    for item in outline:
        _validate_outline_item(item, page_count)
    outline_total = _require_nullable_int(body["outline_total"], "info.outline_total")
    outline_seen = _require_int(body["outline_items_seen_at_least"], "info.outline_items_seen_at_least")
    outline_truncated = _require_bool(body["outline_truncated"], "info.outline_truncated")
    if outline_truncated and outline_total is not None:
        raise _inspect_protocol_error("Inspector fabricated an exact outline total after truncation.")
    if not outline_truncated and outline_total != outline_seen:
        raise _inspect_protocol_error("Inspector complete outline totals are inconsistent.")
    reasons = body["outline_truncation_reasons"]
    if not isinstance(reasons, list) or any(item not in {"max_depth", "max_items"} for item in reasons):
        raise _inspect_protocol_error("Inspector outline truncation reasons are invalid.")
    if body["outline_max_depth"] != TOC_MAX_DEPTH:
        raise _inspect_protocol_error("Inspector outline max depth drifted from the locked contract.")
    page_sizes = body["sample_page_sizes"]
    if not isinstance(page_sizes, list) or len(page_sizes) > 6:
        raise _inspect_protocol_error("Inspector page-size sample exceeds the locked bound.")
    for item in page_sizes:
        data = _require_exact_keys(item, {"page", "width_points", "height_points"}, "page-size sample")
        _require_int(data["page"], "page-size page", minimum=1, maximum=page_count)
        for field in ("width_points", "height_points"):
            value = data[field]
            if type(value) not in {int, float} or not math.isfinite(float(value)) or float(value) <= 0:
                raise _inspect_protocol_error("Inspector page geometry is invalid.")
    samples = body["text_layer_sample"]
    if not isinstance(samples, list) or len(samples) > text_sample_pages:
        raise _inspect_protocol_error("Inspector text sample exceeds the requested bound.")
    for item in samples:
        data = _require_exact_keys(item, {"page", "page_chars", "sample_text_chars", "sample_truncated", "error"}, "text sample")
        _require_int(data["page"], "text sample page", minimum=1, maximum=page_count)
        error = _require_bool(data["error"], "text sample error")
        if error:
            if data["page_chars"] is not None or data["sample_text_chars"] is not None or data["sample_truncated"] is not None:
                raise _inspect_protocol_error("Inspector failed text sample contains fabricated counts.")
        else:
            _require_int(data["page_chars"], "text sample page_chars")
            _require_int(data["sample_text_chars"], "text sample sample_text_chars", maximum=2000)
            _require_bool(data["sample_truncated"], "text sample sample_truncated")
    errors = body["text_sample_errors"]
    if not isinstance(errors, list) or len(errors) > text_sample_pages:
        raise _inspect_protocol_error("Inspector text-sample error list is invalid.")
    for item in errors:
        data = _require_exact_keys(item, {"page", "error"}, "text-sample error")
        _require_int(data["page"], "text-sample error page", minimum=1, maximum=page_count)
        _require_text(data["error"], "text-sample error message", 1024)
    complete = _require_bool(body["text_sample_complete"], "info.text_sample_complete")
    if complete != (len(errors) == 0):
        raise _inspect_protocol_error("Inspector text sample completion flag is inconsistent.")
    scan_candidate = body["scan_candidate"]
    if scan_candidate is not None and type(scan_candidate) is not bool:
        raise _inspect_protocol_error("Inspector scan_candidate is invalid.")
    note = body["scan_candidate_note"]
    if note is not None:
        _require_text(note, "info.scan_candidate_note", 512)
    return {
        **_source_fields(path, root, identity),
        **{key: value for key, value in body.items() if key != "action"},
        "content_trust_note": CONTENT_TRUST_NOTE,
    }


def _outline(root: Path, params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    max_items = int(params.get("max_items", 500))
    path, identity, body = _inspection_call(
        root,
        params,
        context,
        {"action": "outline", "max_items": max_items},
    )
    _require_exact_keys(
        body,
        {"action", "page_count", "total_items", "items_seen_at_least", "truncated", "truncation_reasons", "max_depth", "items"},
        "outline result",
    )
    page_count = _require_int(body["page_count"], "outline.page_count", minimum=1)
    total = _require_nullable_int(body["total_items"], "outline.total_items")
    seen = _require_int(body["items_seen_at_least"], "outline.items_seen_at_least")
    truncated = _require_bool(body["truncated"], "outline.truncated")
    if truncated and total is not None:
        raise _inspect_protocol_error("Inspector fabricated an exact outline total after truncation.")
    if not truncated and total != seen:
        raise _inspect_protocol_error("Inspector complete outline totals are inconsistent.")
    reasons = body["truncation_reasons"]
    if not isinstance(reasons, list) or any(item not in {"max_depth", "max_items"} for item in reasons):
        raise _inspect_protocol_error("Inspector outline truncation reasons are invalid.")
    if body["max_depth"] != TOC_MAX_DEPTH:
        raise _inspect_protocol_error("Inspector outline max depth drifted from the locked contract.")
    items = body["items"]
    if not isinstance(items, list) or len(items) > max_items:
        raise _inspect_protocol_error("Inspector outline item list exceeds the requested bound.")
    for item in items:
        _validate_outline_item(item, page_count)
    return {
        **_source_fields(path, root, identity),
        **{key: value for key, value in body.items() if key != "action"},
        "content_trust_note": CONTENT_TRUST_NOTE,
    }


def _read_pages(root: Path, params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    start = int(params["page_start"])
    end = int(params["page_end"])
    max_chars = int(params.get("max_chars", 120000))
    path, identity, body = _inspection_call(
        root,
        params,
        context,
        {"action": "read-pages", "page_start": start, "page_end": end, "max_chars": max_chars},
    )
    _require_exact_keys(
        body,
        {"action", "page_count", "page_start", "page_end", "returned_pages", "max_chars", "response_truncated", "text_truncated_pages", "coverage_complete", "total_truncated", "next_page", "pages"},
        "read-pages result",
    )
    page_count = _require_int(body["page_count"], "read-pages.page_count", minimum=1)
    if body["page_start"] != start or body["page_end"] != end or body["max_chars"] != max_chars:
        raise _inspect_protocol_error("Inspector read-pages range or response budget does not match the request.")
    _validated_range(page_count, start, end, MAX_READ_PAGES, "read-pages")
    pages = body["pages"]
    if not isinstance(pages, list) or len(pages) > end - start + 1:
        raise _inspect_protocol_error("Inspector read-pages page list is invalid.")
    if body["returned_pages"] != len(pages):
        raise _inspect_protocol_error("Inspector read-pages returned_pages count is inconsistent.")
    total_returned_chars = 0
    previous_page = start - 1
    partial_count = 0
    for item in pages:
        data = _require_exact_keys(item, {"page", "text", "chars", "extracted_chars", "text_truncated", "partial"}, "read-pages page")
        page_number = _require_int(data["page"], "read-pages page number", minimum=start, maximum=end)
        if page_number != previous_page + 1:
            raise _inspect_protocol_error("Inspector read-pages pages are not a contiguous prefix of the request.")
        previous_page = page_number
        text = _require_text(data["text"], "read-pages text", max_chars)
        chars = _require_int(data["chars"], "read-pages chars")
        extracted = _require_int(data["extracted_chars"], "read-pages extracted_chars", maximum=MAX_PAGE_TEXT_CHARS)
        if len(text) > extracted or extracted > chars:
            raise _inspect_protocol_error("Inspector read-pages character counts are inconsistent.")
        _require_bool(data["text_truncated"], "read-pages text_truncated")
        if _require_bool(data["partial"], "read-pages partial"):
            partial_count += 1
            if page_number != start or len(pages) != 1:
                raise _inspect_protocol_error("Inspector partial page is only allowed for the first requested page.")
        total_returned_chars += len(text)
    if total_returned_chars > max_chars or partial_count > 1:
        raise _inspect_protocol_error("Inspector read-pages exceeded the response character budget.")
    text_truncated_pages = body["text_truncated_pages"]
    if not isinstance(text_truncated_pages, list) or any(type(page) is not int or page < start or page > end for page in text_truncated_pages):
        raise _inspect_protocol_error("Inspector read-pages text_truncated_pages is invalid.")
    response_truncated = _require_bool(body["response_truncated"], "read-pages.response_truncated")
    coverage_complete = _require_bool(body["coverage_complete"], "read-pages.coverage_complete")
    total_truncated = _require_bool(body["total_truncated"], "read-pages.total_truncated")
    next_page = _require_nullable_int(body["next_page"], "read-pages.next_page", minimum=start, maximum=end)
    expected_coverage = not response_truncated and not text_truncated_pages and partial_count == 0
    if coverage_complete != expected_coverage or total_truncated != (response_truncated or bool(text_truncated_pages)):
        raise _inspect_protocol_error("Inspector read-pages coverage flags are inconsistent.")
    if partial_count and next_page is not None:
        raise _inspect_protocol_error("Inspector advertised fake continuation after a partial first page.")
    return {
        **_source_fields(path, root, identity),
        **{key: value for key, value in body.items() if key != "action"},
        "content_trust_note": CONTENT_TRUST_NOTE,
    }


def _fold_with_origin_map(text: str) -> tuple[str, list[int]]:
    folded_parts: list[str] = []
    mapping: list[int] = []
    for index, char in enumerate(text):
        folded = char.casefold()
        folded_parts.append(folded)
        mapping.extend([index] * len(folded))
    return "".join(folded_parts), mapping


def _snippet(text: str, start: int, end: int, width: int) -> str:
    half = max(20, width // 2)
    left = max(0, start - half)
    right = min(len(text), end + half)
    return re.sub(r"\s+", " ", text[left:right].replace("\n", " ")).strip()


def _search(root: Path, params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    query = str(params["query"])
    if not query.strip():
        raise ExtensionError("QUERY_EMPTY", "query must contain non-whitespace text")
    case_sensitive = bool(params.get("case_sensitive", False))
    max_results = int(params.get("max_results", 50))
    snippet_chars = int(params.get("snippet_chars", 360))
    start = int(params.get("page_start", 1))
    requested_end = params.get("page_end")
    end_value = int(requested_end) if requested_end is not None else None
    path, identity, body = _inspection_call(
        root,
        params,
        context,
        {
            "action": "search",
            "query": query,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "snippet_chars": snippet_chars,
            "page_start": start,
            "page_end": end_value,
        },
    )
    _require_exact_keys(
        body,
        {"action", "page_count", "query", "case_sensitive", "page_start", "page_end", "pages_scanned", "results", "max_results", "results_truncated", "truncated", "matches_total_in_extracted_text", "matches_seen_at_least", "search_window_complete", "text_truncated_pages", "text_coverage_complete", "coverage_complete", "search_mode"},
        "search result",
    )
    page_count = _require_int(body["page_count"], "search.page_count", minimum=1)
    actual_end = _require_int(body["page_end"], "search.page_end", minimum=start, maximum=page_count)
    if body["page_start"] != start or (end_value is not None and actual_end != end_value):
        raise _inspect_protocol_error("Inspector search range does not match the request.")
    _validated_search_range(page_count, start, actual_end)
    if body["query"] != query or body["case_sensitive"] is not case_sensitive or body["max_results"] != max_results:
        raise _inspect_protocol_error("Inspector search parameters do not match the request.")
    if body["search_mode"] != "literal":
        raise _inspect_protocol_error("Inspector search mode drifted from literal matching.")
    results = body["results"]
    if not isinstance(results, list) or len(results) > max_results:
        raise _inspect_protocol_error("Inspector search result list exceeds the requested bound.")
    pages_scanned = _require_int(body["pages_scanned"], "search.pages_scanned", maximum=actual_end - start + 1)
    previous_page = start
    previous_match_on_page = 0
    for item in results:
        data = _require_exact_keys(item, {"page", "match_on_page", "char_offset", "char_end", "snippet"}, "search match")
        page = _require_int(data["page"], "search match page", minimum=start, maximum=actual_end)
        match_on_page = _require_int(data["match_on_page"], "search match index", minimum=1)
        offset = _require_int(data["char_offset"], "search char_offset")
        end = _require_int(data["char_end"], "search char_end", minimum=offset + 1)
        _require_text(data["snippet"], "search snippet", min(4096, snippet_chars * 2 + 256))
        if page < previous_page:
            raise _inspect_protocol_error("Inspector search matches are not in page order.")
        if page == previous_page:
            if match_on_page != previous_match_on_page + 1:
                raise _inspect_protocol_error("Inspector search match_on_page sequence is inconsistent.")
        else:
            if match_on_page != 1:
                raise _inspect_protocol_error("Inspector search page match sequence must restart at one.")
            previous_page = page
            previous_match_on_page = 0
        previous_match_on_page = match_on_page
    results_truncated = _require_bool(body["results_truncated"], "search.results_truncated")
    if body["truncated"] is not results_truncated:
        raise _inspect_protocol_error("Inspector search truncated compatibility alias is inconsistent.")
    window_complete = _require_bool(body["search_window_complete"], "search.search_window_complete")
    if results_truncated == window_complete:
        raise _inspect_protocol_error("Inspector search result truncation/window-complete flags are inconsistent.")
    matches_seen = _require_int(body["matches_seen_at_least"], "search.matches_seen_at_least")
    exact_matches = _require_nullable_int(body["matches_total_in_extracted_text"], "search.matches_total_in_extracted_text")
    if window_complete:
        if exact_matches != matches_seen:
            raise _inspect_protocol_error("Inspector complete search total is inconsistent.")
    elif exact_matches is not None or matches_seen != max_results + 1:
        raise _inspect_protocol_error("Inspector truncated search did not stop at result cap plus one.")
    text_truncated_pages = body["text_truncated_pages"]
    if not isinstance(text_truncated_pages, list) or any(type(page) is not int or page < start or page > actual_end for page in text_truncated_pages):
        raise _inspect_protocol_error("Inspector search text_truncated_pages is invalid.")
    text_complete = _require_bool(body["text_coverage_complete"], "search.text_coverage_complete")
    coverage_complete = _require_bool(body["coverage_complete"], "search.coverage_complete")
    if text_complete != (not text_truncated_pages) or coverage_complete != (window_complete and not text_truncated_pages):
        raise _inspect_protocol_error("Inspector search coverage flags are inconsistent.")
    if pages_scanned == 0 or pages_scanned > actual_end - start + 1:
        raise _inspect_protocol_error("Inspector search pages_scanned is invalid.")
    return {
        **_source_fields(path, root, identity),
        **{key: value for key, value in body.items() if key != "action"},
        "content_trust_note": CONTENT_TRUST_NOTE,
    }


def _render_preflight(doc: Any, start: int, end: int, dpi: int) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    total_pixels = 0
    for page_number in range(start, end + 1):
        size = _page_size(doc, page_number - 1)
        width_px = int(math.ceil(float(size["width_points"]) * dpi / 72.0))
        height_px = int(math.ceil(float(size["height_points"]) * dpi / 72.0))
        pixels = width_px * height_px
        if pixels <= 0 or pixels > MAX_RENDER_PIXELS_PER_PAGE:
            raise ExtensionError(
                "RENDER_PIXEL_BUDGET_EXCEEDED",
                f"Page {page_number} would render {pixels} pixels; per-page limit is {MAX_RENDER_PIXELS_PER_PAGE}.",
                page=page_number,
                pixels=pixels,
            )
        total_pixels += pixels
        if total_pixels > MAX_RENDER_PIXELS_TOTAL:
            raise ExtensionError(
                "RENDER_PIXEL_BUDGET_EXCEEDED",
                f"Render range would exceed the {MAX_RENDER_PIXELS_TOTAL} total pixel limit.",
                pixels=total_pixels,
            )
        pages.append({
            **size,
            "width_pixels_nominal": width_px,
            "height_pixels_nominal": height_px,
            "pixels_nominal": pixels,
        })
    return {"pages": pages, "total_pixels_nominal": total_pixels}


def _atomic_write_json(path: Path, data: dict[str, Any], token: str) -> None:
    temp = path.parent / f".fbpdf-stage-{token}-{path.name}.tmp"
    encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with temp.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _bounded_decode(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8-sig", errors="replace").strip()


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError as exc:
        raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", f"Could not read rendered PNG header: {exc}") from exc
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Renderer output does not contain a valid PNG IHDR header.")
    ihdr_length = int.from_bytes(header[8:12], "big")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if ihdr_length != 13 or width <= 0 or height <= 0:
        raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Renderer output has invalid PNG dimensions.")
    return width, height


def _run_windows_renderer(
    path: Path,
    output_dir: Path,
    start: int,
    end: int,
    dpi: int,
    *,
    cancel_path: str | None = None,
) -> dict[str, Any]:
    if sys.platform != "win32":
        raise ExtensionError("PDF_RENDER_UNAVAILABLE", "Windows.Data.Pdf rendering is available only on Windows.")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise ExtensionError("PDF_RENDER_UNAVAILABLE", "powershell.exe was not found on the trusted PATH.")
    if not PDF_RENDER_SCRIPT.is_file():
        raise ExtensionError("PDF_RENDER_UNAVAILABLE", "Bundled pdf_render.ps1 is missing.")
    argv = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-Sta",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PDF_RENDER_SCRIPT),
        "-PdfPath",
        str(path),
        "-OutputDir",
        str(output_dir),
        "-PageStart",
        str(start),
        "-PageEnd",
        str(end),
        "-Dpi",
        str(dpi),
    ]
    if _cancel_requested(cancel_path):
        raise ExtensionError("PDF_RENDER_CANCELLED", "PDF render was cancelled before the renderer started.")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(PDF_RENDER_SCRIPT.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
    except OSError as exc:
        raise ExtensionError("PDF_RENDER_START_FAILED", f"Could not start Windows PDF renderer: {exc}") from exc
    deadline = time.monotonic() + 7000.0
    while True:
        if _cancel_requested(cancel_path):
            terminate_owned_process_tree(process, hide_window=True)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
            raise ExtensionError("PDF_RENDER_CANCELLED", "PDF render was cancelled.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_owned_process_tree(process, hide_window=True)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
            raise ExtensionError("PDF_RENDER_TIMEOUT", "Windows PDF renderer exceeded its internal 7000-second limit.")
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if len(stdout) > POWERSHELL_STDOUT_LIMIT or len(stderr) > POWERSHELL_STDERR_LIMIT:
        raise ExtensionError("PDF_RENDER_PROTOCOL_TOO_LARGE", "Windows PDF renderer produced excessive diagnostic output.")
    stdout_text = _bounded_decode(stdout, POWERSHELL_STDOUT_LIMIT)
    stderr_text = _bounded_decode(stderr, POWERSHELL_STDERR_LIMIT)
    if process.returncode != 0:
        raise ExtensionError(
            "PDF_RENDER_FAILED",
            f"Windows PDF renderer failed: {(stderr_text or stdout_text or f'exit code {process.returncode}')[:4000]}",
        )
    try:
        result = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", f"Windows PDF renderer returned invalid JSON: {stdout_text[:1000]}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("files"), list):
        raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows PDF renderer returned an invalid result envelope.")
    return result


def _render_pages(root: Path, params: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = _resolve_existing_pdf(root, params["path"])
    identity = _capture_source_identity(path)
    dpi = int(params.get("dpi", 180))
    if dpi < MIN_RENDER_DPI or dpi > MAX_RENDER_DPI:
        raise ExtensionError("DPI_INVALID", f"dpi must be between {MIN_RENDER_DPI} and {MAX_RENDER_DPI}")
    start = int(params["page_start"])
    end = int(params["page_end"])
    if start < 1 or end < start:
        raise ExtensionError("PAGE_RANGE_INVALID", "render-pages page range must satisfy 1 <= start <= end")
    if end - start + 1 > MAX_RENDER_PAGES:
        raise ExtensionError("PAGE_RANGE_TOO_LARGE", f"render-pages supports at most {MAX_RENDER_PAGES} pages per call")
    make_zip = bool(params.get("make_zip", True))
    output_dir = _create_fresh_output_dir(root, params["output_dir"])
    token = uuid.uuid4().hex
    try:
        cancel_path = (context or {}).get("job_cancel_path")
        result = _run_windows_renderer(
            path,
            output_dir,
            start,
            end,
            dpi,
            cancel_path=cancel_path if isinstance(cancel_path, str) else None,
        )
        _assert_source_unchanged(path, identity)
        if type(result.get("source_units")) is not int:
            raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an invalid source_units value.")
        source_units = int(result["source_units"])
        if source_units < 1 or source_units < end:
            raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an impossible page count.")
        selected_range = result.get("selected_range")
        if selected_range != {"start": start, "end": end, "unit": "page"}:
            raise ExtensionError(
                "PDF_RENDER_PROTOCOL_ERROR",
                "Windows renderer returned a range that does not match the requested pages.",
            )
        if type(result.get("dpi_nominal")) is not int or int(result["dpi_nominal"]) != dpi:
            raise ExtensionError(
                "PDF_RENDER_PROTOCOL_ERROR",
                "Windows renderer returned a nominal DPI that does not match the request.",
            )
        expected_count = end - start + 1
        raw_plan = result.get("pages")
        if not isinstance(raw_plan, list) or len(raw_plan) != expected_count:
            raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an invalid nominal render plan.")
        nominal_plan: list[dict[str, int]] = []
        nominal_total = 0
        for offset, raw_page in enumerate(raw_plan):
            page_number = start + offset
            if not isinstance(raw_page, dict) or set(raw_page) != {
                "page", "width_pixels_nominal", "height_pixels_nominal", "pixels_nominal"
            }:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an invalid nominal page record.")
            page_value = raw_page.get("page")
            width_nominal = raw_page.get("width_pixels_nominal")
            height_nominal = raw_page.get("height_pixels_nominal")
            pixels_nominal = raw_page.get("pixels_nominal")
            if any(type(value) is not int for value in (page_value, width_nominal, height_nominal, pixels_nominal)):
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned non-integer nominal geometry.")
            if page_value != page_number or width_nominal <= 0 or height_nominal <= 0:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned inconsistent nominal geometry.")
            if pixels_nominal != width_nominal * height_nominal or pixels_nominal > MAX_RENDER_PIXELS_PER_PAGE:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an invalid nominal pixel count.")
            nominal_total += pixels_nominal
            if nominal_total > MAX_RENDER_PIXELS_TOTAL:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer exceeded the total nominal pixel budget.")
            nominal_plan.append({
                "page": page_number,
                "width_pixels_nominal": width_nominal,
                "height_pixels_nominal": height_nominal,
                "pixels_nominal": pixels_nominal,
            })
        if type(result.get("total_pixels_nominal")) is not int or int(result["total_pixels_nominal"]) != nominal_total:
            raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Windows renderer returned an inconsistent nominal pixel total.")
        raw_files = result.get("files")
        if not isinstance(raw_files, list) or len(raw_files) != expected_count:
            raise ExtensionError(
                "PDF_RENDER_PROTOCOL_ERROR",
                f"Windows renderer returned an invalid file list for {expected_count} requested pages.",
            )

        rendered: list[dict[str, Any]] = []
        artifact_records: list[dict[str, Any]] = []
        workspace_artifacts: list[str] = []
        total_bytes = 0
        total_pixels_actual = 0
        for offset, raw_name in enumerate(raw_files):
            if not isinstance(raw_name, str) or not raw_name or "\\" in raw_name:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Renderer returned an invalid output filename.")
            page_number = start + offset
            expected_name = f"P{page_number:04d}.png"
            if raw_name != expected_name:
                raise ExtensionError(
                    "PDF_RENDER_PROTOCOL_ERROR",
                    f"Renderer returned unexpected filename {raw_name!r}; expected {expected_name!r}.",
                )
            rel_name = PurePosixPath(raw_name)
            if rel_name.is_absolute() or ".." in rel_name.parts or len(rel_name.parts) != 1:
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Renderer outputs must stay directly inside output_dir.")
            candidate = (output_dir / rel_name.name).resolve(strict=True)
            candidate.relative_to(output_dir)
            _reject_links(root, candidate)
            if not candidate.is_file() or candidate.suffix.lower() != ".png":
                raise ExtensionError("PDF_RENDER_PROTOCOL_ERROR", "Renderer output is not a regular PNG file.")
            width_pixels, height_pixels = _read_png_dimensions(candidate)
            actual_pixels = width_pixels * height_pixels
            if actual_pixels > MAX_RENDER_PIXELS_PER_PAGE:
                raise ExtensionError(
                    "RENDER_PIXEL_BUDGET_EXCEEDED",
                    f"Rendered page {page_number} contains {actual_pixels} pixels; per-page limit is {MAX_RENDER_PIXELS_PER_PAGE}.",
                    page=page_number,
                    pixels=actual_pixels,
                )
            total_pixels_actual += actual_pixels
            if total_pixels_actual > MAX_RENDER_PIXELS_TOTAL:
                raise ExtensionError(
                    "RENDER_PIXEL_BUDGET_EXCEEDED",
                    f"Rendered output exceeds the {MAX_RENDER_PIXELS_TOTAL} total pixel limit.",
                    pixels=total_pixels_actual,
                )
            size = candidate.stat().st_size
            total_bytes += size
            if total_bytes > MAX_RENDER_ARTIFACT_BYTES:
                raise ExtensionError(
                    "RENDER_ARTIFACT_BUDGET_EXCEEDED",
                    f"PNG output exceeds {MAX_RENDER_ARTIFACT_BYTES} bytes.",
                    bytes=total_bytes,
                )
            rel = candidate.relative_to(root).as_posix()
            plan = nominal_plan[offset]
            item = {
                "page": page_number,
                "path": rel,
                "bytes": size,
                "sha256": _sha256_file(candidate),
                "width_pixels": width_pixels,
                "height_pixels": height_pixels,
                "pixels": actual_pixels,
                "width_pixels_nominal": plan["width_pixels_nominal"],
                "height_pixels_nominal": plan["height_pixels_nominal"],
            }
            rendered.append(item)
            artifact_records.append({"path": rel, "bytes": size, "sha256": item["sha256"]})
            workspace_artifacts.append(rel)

        zip_meta: dict[str, Any] | None = None
        if make_zip:
            zip_path = output_dir / f"pages-{start:04d}-{end:04d}.zip"
            with zipfile.ZipFile(zip_path, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for item in rendered:
                    source = root / PurePosixPath(item["path"])
                    archive.write(source, arcname=Path(item["path"]).name)
            zip_size = zip_path.stat().st_size
            total_bytes += zip_size
            if total_bytes > MAX_RENDER_ARTIFACT_BYTES:
                raise ExtensionError(
                    "RENDER_ARTIFACT_BUDGET_EXCEEDED",
                    f"PNG + ZIP output exceeds {MAX_RENDER_ARTIFACT_BYTES} bytes.",
                    bytes=total_bytes,
                )
            zip_meta = {
                "path": zip_path.relative_to(root).as_posix(),
                "bytes": zip_size,
                "sha256": _sha256_file(zip_path),
            }
            artifact_records.append(dict(zip_meta))
            workspace_artifacts.append(zip_meta["path"])

        _assert_source_unchanged(path, identity)
        marker = output_dir / "RENDER-COMPLETE.json"
        marker_payload = {
            "schema_version": 3,
            "complete": True,
            "renderer": "Windows.Data.Pdf",
            "text_backend": f"PdfPig {PINNED_PDFPIG_VERSION}",
            "inspection_backend_invoked": False,
            "source": _source_fields(path, root, identity),
            "source_units": source_units,
            "selected_range": selected_range,
            "page_start": start,
            "page_end": end,
            "dpi_nominal": dpi,
            "total_pixels_nominal": nominal_total,
            "total_pixels_actual": total_pixels_actual,
            "artifacts": artifact_records,
            "note": (
                "This marker is written last. A render directory without this marker is incomplete and must not be "
                "treated as successfully committed evidence."
            ),
        }
        _atomic_write_json(marker, marker_payload, token)
        _assert_source_unchanged(path, identity)
        marker_rel = marker.relative_to(root).as_posix()
        workspace_artifacts.append(marker_rel)

        return {
            **_source_fields(path, root, identity),
            "page_count": source_units,
            "source_units": source_units,
            "selected_range": selected_range,
            "page_start": start,
            "page_end": end,
            "rendered_pages": len(rendered),
            "dpi_nominal": dpi,
            "renderer": "Windows.Data.Pdf",
            "text_backend": f"PdfPig {PINNED_PDFPIG_VERSION}",
            "inspection_backend_invoked": False,
            "render_note": (
                "Windows.Data.Pdf owns page count, selected range, nominal geometry, and pre-raster pixel budgets. "
                "Actual PNG dimensions are read back from each IHDR and remain authoritative for post-render pixel-budget enforcement."
            ),
            "total_pixels_nominal": nominal_total,
            "total_pixels_actual": total_pixels_actual,
            "rendered": rendered,
            "zip": zip_meta,
            "completion_marker": marker_rel,
            "workspace_artifacts": workspace_artifacts,
        }
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
