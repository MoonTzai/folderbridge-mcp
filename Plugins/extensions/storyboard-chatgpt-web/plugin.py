from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError
import live_judge
import live_generator
import live_state


RUN_RE = re.compile(r"^[A-Za-z0-9._-]{1,180}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DENIED_PARTS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
MAX_PACK_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
BRIDGE = Path(__file__).resolve().with_name("bridge.cjs")
EXECUTION_MODES = {"GENERATE_AND_JUDGE", "GENERATE_ONLY"}


def _fail(code: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, details=details or None, retryable=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(context: dict[str, Any]) -> Path:
    raw = context.get("state_dir")
    if not isinstance(raw, str) or not raw:
        raise _fail("STATE_UNAVAILABLE", "storyboard-chatgpt-web requires extension.state")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise _fail("WORKSPACE_REQUIRED", "A selected FolderBridge workspace is required")
    try:
        root = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise _fail("WORKSPACE_UNAVAILABLE", "Selected workspace root is unavailable") from exc
    if not root.is_dir():
        raise _fail("WORKSPACE_UNAVAILABLE", "Selected workspace root is not a directory")
    return root


def _is_reparse(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _clean_relative(raw: Any) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or "\x00" in raw or "\\" in raw:
        raise _fail("PATH_INVALID", "Workspace paths must be bounded POSIX-style relative strings")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise _fail("PATH_INVALID", "Workspace path must stay inside the selected workspace")
    for part in rel.parts:
        folded = part.casefold()
        if folded in DENIED_PARTS:
            raise _fail("PATH_BLOCKED", "Dependency/VCS paths are not allowed", segment=part)
        if folded in SENSITIVE_NAMES or PurePosixPath(folded).suffix in SENSITIVE_SUFFIXES:
            raise _fail("PATH_BLOCKED", "Credential/key-like paths are not allowed", segment=part)
        if folded == ".folderbridge.json":
            raise _fail("PATH_BLOCKED", "FolderBridge control files are not allowed")
    return rel


def _resolve_file(root: Path, raw: Any, *, max_bytes: int, suffixes: set[str] | None = None) -> tuple[PurePosixPath, Path]:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or _is_reparse(current)):
            raise _fail("PATH_BLOCKED", "Linked/reparse workspace paths are not allowed", segment=part)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _fail("FILE_NOT_FOUND", "Workspace input file does not exist") from exc
    if not resolved.is_file() or resolved.is_symlink() or _is_reparse(resolved):
        raise _fail("PATH_BLOCKED", "Workspace input must be a regular non-link file")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise _fail("FILE_TOO_LARGE", "Workspace input exceeds size limit", size=size, max_bytes=max_bytes)
    if suffixes is not None and resolved.suffix.casefold() not in suffixes:
        raise _fail("FILE_TYPE_UNSUPPORTED", "Workspace input file type is not supported", suffix=resolved.suffix.casefold())
    return rel, resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_pack(root: Path, raw_path: Any) -> tuple[PurePosixPath, str, str]:
    rel, path = _resolve_file(root, raw_path, max_bytes=MAX_PACK_BYTES, suffixes={".json"})
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise _fail("PACK_ENCODING_INVALID", "Prompt Pack must be UTF-8 JSON") from exc
    return rel, raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _node_executable() -> str:
    found = shutil.which("node.exe") or shutil.which("node")
    if found:
        return found
    drive = os.environ.get("SYSTEMDRIVE") or "C:"
    candidates = [
        Path(drive + "\\Program Files\\nodejs\\node.exe"),
        Path(drive + "\\Program Files (x86)\\nodejs\\node.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise _fail("NODE_NOT_FOUND", "Node.js is required for the Storyboard Forge compiler bundle")


def _bridge(payload: dict[str, Any]) -> dict[str, Any]:
    if not BRIDGE.is_file():
        raise _fail("BUNDLE_MISSING", "bridge.cjs is missing from the approved extension snapshot")
    proc = subprocess.run(
        [_node_executable(), str(BRIDGE)],
        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=75,
        shell=False,
        check=False,
    )
    output = (proc.stdout or "").strip()
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError as exc:
        raise _fail(
            "BRIDGE_RESPONSE_INVALID",
            "Compiler bridge returned invalid JSON",
            exit_code=proc.returncode,
            stderr=(proc.stderr or "")[-2000:],
        ) from exc
    if proc.returncode != 0 or not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope, dict) else None
        code = str((error or {}).get("code") or "BRIDGE_FAILED")
        message = str((error or {}).get("message") or "Compiler bridge failed")
        raise _fail(code, message)
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise _fail("BRIDGE_RESPONSE_INVALID", "Compiler bridge result must be an object")
    return result


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail("STATE_CORRUPT", f"Invalid local state file: {path.name}") from exc
    if not isinstance(value, dict):
        raise _fail("STATE_CORRUPT", f"Local state file is not an object: {path.name}")
    return value


def _run_dir(context: dict[str, Any], run_id: Any, *, must_exist: bool = True) -> Path:
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise _fail("RUN_ID_INVALID", "run_id must match [A-Za-z0-9._-]{1,180}")
    root = _state_dir(context) / "runs" / run_id
    if must_exist and not root.is_dir():
        raise _fail("RUN_NOT_FOUND", "Run does not exist", run_id=run_id)
    return root


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def _snapshot_payload(run_dir: Path) -> tuple[str, dict[str, Any]]:
    run = _read_json(run_dir / "run.json")
    try:
        raw = (run_dir / "pack.snapshot.json").read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail("STATE_CORRUPT", "Prompt Pack snapshot is missing") from exc
    return raw, run


def _artifact_registry(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "artifacts.json"
    if not path.exists():
        return {"schema_version": "storyboard-chatgpt-web-artifacts-1.0", "artifacts": {}}
    registry = _read_json(path)
    if registry.get("schema_version") != "storyboard-chatgpt-web-artifacts-1.0" or not isinstance(registry.get("artifacts"), dict):
        raise _fail("STATE_CORRUPT", "Artifact registry schema mismatch")
    return registry


def _sequence_registry(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "sequence-artifacts.json"
    if not path.exists():
        return {"schema_version": "storyboard-chatgpt-web-sequence-1.0", "frames": {}}
    value = _read_json(path)
    if value.get("schema_version") != "storyboard-chatgpt-web-sequence-1.0" or not isinstance(value.get("frames"), dict):
        raise _fail("STATE_CORRUPT", "Sequence artifact registry schema mismatch")
    return value


def _resolver(
    registry: dict[str, Any],
    sequence: dict[str, Any] | None = None,
    *,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {"frames": {}, "anchors": {}}
    for record in registry.get("artifacts", {}).values():
        if not isinstance(record, dict):
            continue
        source_type = record.get("source_type")
        source_id = record.get("source_id")
        if source_type == "frame":
            out["frames"][source_id] = record
        elif source_type == "anchor":
            out["anchors"][source_id] = record
    if allow_unreviewed and isinstance(sequence, dict):
        for source_id, record in sequence.get("frames", {}).items():
            if isinstance(record, dict):
                out["frames"][source_id] = record
    return out


def _execution_mode(run: dict[str, Any]) -> str:
    mode = str(run.get("execution_mode") or "GENERATE_AND_JUDGE")
    if mode not in EXECUTION_MODES:
        raise _fail("STATE_CORRUPT", "Run execution_mode is invalid")
    return mode


def _run_mode_set(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    run = _read_json(run_dir / "run.json")
    requested = params.get("execution_mode")
    if requested not in EXECUTION_MODES:
        raise _fail("EXECUTION_MODE_INVALID", "execution_mode must be GENERATE_AND_JUDGE or GENERATE_ONLY")
    current = _execution_mode(run)
    if requested == current:
        return {"ok": True, "changed": False, "run": run}
    sequence = _sequence_registry(run_dir)
    if requested == "GENERATE_AND_JUDGE" and sequence.get("frames"):
        raise _fail(
            "MODE_SWITCH_REQUIRES_FRESH_RUN",
            "A run that already contains generated_unreviewed sequence artifacts cannot switch into PASS-only Judge mode; start a fresh run.",
        )
    updated = {**run, "execution_mode": requested, "updated_at": _now()}
    _atomic_json(run_dir / "run.json", updated)
    return {"ok": True, "changed": True, "run": updated}


def _frame_id(params: dict[str, Any], run: dict[str, Any]) -> str:
    value = params.get("frame_id") or run.get("current_frame_id")
    if not isinstance(value, str) or not value:
        raise _fail("FRAME_ID_MISSING", "No current frame_id is available")
    return value


def _pack_inspect(root: Path, prompt_pack_path: Any) -> dict[str, Any]:
    rel, raw, sha = _read_pack(root, prompt_pack_path)
    result = _bridge({
        "action": "pack-inspect",
        "raw_pack": raw,
        "source_path": rel.as_posix(),
        "source_sha256": sha,
    })
    result["workspace_path"] = rel.as_posix()
    return result


def _run_init(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    rel, raw, sha = _read_pack(root, params.get("prompt_pack_path"))
    summary = _bridge({
        "action": "pack-inspect",
        "raw_pack": raw,
        "source_path": rel.as_posix(),
        "source_sha256": sha,
    })
    run_id = params.get("run_id") or _new_run_id()
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise _fail("RUN_ID_INVALID", "run_id must match [A-Za-z0-9._-]{1,180}")
    run_dir = _run_dir(context, run_id, must_exist=False)
    if run_dir.exists():
        existing = _read_json(run_dir / "run.json")
        if existing.get("source_sha256") == sha and existing.get("source_path") == rel.as_posix():
            return {"ok": True, "already_initialized": True, "run": existing}
        raise _fail("RUN_COLLISION", "run_id already exists with different authority", run_id=run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    mode = params.get("mode_requested") or "AUTO"
    execution_mode = params.get("execution_mode") or "GENERATE_AND_JUDGE"
    if execution_mode not in EXECUTION_MODES:
        raise _fail("EXECUTION_MODE_INVALID", "execution_mode must be GENERATE_AND_JUDGE or GENERATE_ONLY")
    retry_budget = params.get("retry_budget_per_frame")
    if retry_budget is None:
        retry_budget = 2
    if not isinstance(retry_budget, int) or isinstance(retry_budget, bool) or retry_budget < 0 or retry_budget > 10:
        raise _fail("RETRY_BUDGET_INVALID", "retry_budget_per_frame must be integer 0..10")
    run = {
        "schema_version": "storyboard-chatgpt-web-run-1.0",
        "run_id": run_id,
        "status": "READY",
        "mode_requested": mode,
        "mode_effective": mode,
        "execution_mode": execution_mode,
        "retry_budget_per_frame": retry_budget,
        "source_path": rel.as_posix(),
        "source_sha256": sha,
        "pack_id": summary["pack_id"],
        "pack_revision": summary["pack_revision"],
        "compiler_version": summary["compiler_version"],
        "task_count": summary["task_count"],
        "current_index": 0,
        "current_frame_id": summary.get("first_frame_id"),
        "created_at": _now(),
        "updated_at": _now(),
    }
    _atomic_text(run_dir / "pack.snapshot.json", raw)
    _atomic_json(run_dir / "plan.json", summary)
    _atomic_json(run_dir / "artifacts.json", {"schema_version": "storyboard-chatgpt-web-artifacts-1.0", "artifacts": {}})
    _atomic_json(run_dir / "sequence-artifacts.json", {"schema_version": "storyboard-chatgpt-web-sequence-1.0", "frames": {}})
    _atomic_json(run_dir / "run.json", run)
    return {"ok": True, "already_initialized": False, "run": run}


def _run_status(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    run = _read_json(run_dir / "run.json")
    registry = _artifact_registry(run_dir)
    bindings_dir = run_dir / "bindings"
    binding_count = len([p for p in bindings_dir.glob("*.json") if p.is_file()]) if bindings_dir.is_dir() else 0
    return {
        "ok": True,
        "run": run,
        "artifact_count": len(registry["artifacts"]),
        "binding_count": binding_count,
    }


def _run_current(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id({}, run)
    entry = _bridge({
        "action": "task-inspect",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
    })
    return {"ok": True, "run_id": run["run_id"], "status": run["status"], "task": entry}


def _artifact_register(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    root = _workspace_root(context)
    rel, artifact = _resolve_file(root, params.get("artifact_path"), max_bytes=MAX_ARTIFACT_BYTES, suffixes=IMAGE_SUFFIXES)
    digest = _sha256_file(artifact)
    expected = params.get("expected_sha256")
    if expected is not None and digest != expected:
        raise _fail("ARTIFACT_SHA_MISMATCH", "Artifact bytes do not match expected_sha256", expected_sha256=expected, actual_sha256=digest)
    source_type = params.get("source_type")
    status_value = params.get("status")
    if source_type == "frame" and status_value != "accepted":
        raise _fail("ARTIFACT_STATUS_INVALID", "Frame references must be registered as accepted")
    if source_type == "anchor" and status_value not in {"approved", "accepted"}:
        raise _fail("ARTIFACT_STATUS_INVALID", "Anchor references must be approved or accepted")
    source_id = str(params.get("source_id"))
    record = {
        "source_type": source_type,
        "source_id": source_id,
        "status": status_value,
        "artifact_id": params.get("artifact_id"),
        "sha256": digest,
        "path": rel.as_posix(),
        "registered_at": _now(),
    }
    registry = _artifact_registry(run_dir)
    key = f"{source_type}:{source_id}"
    prior = registry["artifacts"].get(key)
    if prior is not None:
        comparable_old = {k: v for k, v in prior.items() if k != "registered_at"}
        comparable_new = {k: v for k, v in record.items() if k != "registered_at"}
        if comparable_old != comparable_new:
            raise _fail("ARTIFACT_COLLISION", "Artifact identity already registered with different bytes or metadata", key=key)
        return {"ok": True, "already_registered": True, "artifact": prior}
    registry["artifacts"][key] = record
    _atomic_json(run_dir / "artifacts.json", registry)
    return {"ok": True, "already_registered": False, "artifact": record}


def _reference_bind(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id(params, run)
    registry = _artifact_registry(run_dir)
    sequence = _sequence_registry(run_dir)
    allow_unreviewed = _execution_mode(run) == "GENERATE_ONLY"
    receipt = _bridge({
        "action": "reference-bind",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "resolver": _resolver(registry, sequence, allow_unreviewed=allow_unreviewed),
        "allow_unreviewed": allow_unreviewed,
    })
    name = hashlib.sha256(frame_id.encode("utf-8")).hexdigest()[:20] + ".json"
    _atomic_json(run_dir / "bindings" / name, receipt)
    return {"ok": True, "run_id": run["run_id"], "frame_id": frame_id, "binding": receipt}


def _operation_preview(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id(params, run)
    registry = _artifact_registry(run_dir)
    sequence = _sequence_registry(run_dir)
    allow_unreviewed = _execution_mode(run) == "GENERATE_ONLY"
    result = _bridge({
        "action": "operation-preview",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "resolver": _resolver(registry, sequence, allow_unreviewed=allow_unreviewed),
        "allow_unreviewed": allow_unreviewed,
        "session_id": run["run_id"],
        "attempt": params.get("attempt") or 1,
    })
    return {"ok": True, "run_id": run["run_id"], "frame_id": frame_id, "preview": result}


def _image_info(data: bytes) -> tuple[str, str, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return "image/png", "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":
        index = 2
        sof = {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            code = data[index + 1]
            if code in sof:
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return "image/jpeg", "jpg", width, height
            if index + 4 > len(data):
                break
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if length <= 0:
                break
            index += 2 + length
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", "webp", width, height
        if kind == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return "image/webp", "webp", width, height
        return "image/webp", "webp", 0, 0
    raise _fail("GENERATOR_IMAGE_UNSUPPORTED", "ChatGPT generator returned unsupported image bytes")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _resolve_output_dir(root: Path, raw: Any) -> tuple[PurePosixPath, Path]:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.exists() and (current.is_symlink() or _is_reparse(current)):
            raise _fail("PATH_BLOCKED", "Linked/reparse output paths are not allowed", segment=part)
    candidate.mkdir(parents=True, exist_ok=True)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _fail("PATH_BLOCKED", "Output path escaped selected workspace") from exc
    return rel, resolved


def _persist_generated_bytes(
    root: Path,
    save_directory: Any,
    *,
    frame_id: str,
    attempt: int,
    data: bytes,
) -> dict[str, Any]:
    output_rel, output_dir = _resolve_output_dir(root, save_directory)
    mime, extension, width, height = _image_info(data)
    digest = hashlib.sha256(data).hexdigest()
    safe_frame = re.sub(r"[^A-Za-z0-9._-]+", "-", frame_id).strip("-") or "frame"
    name = f"{safe_frame}-attempt-{attempt}-{digest[:12]}.{extension}"
    target = output_dir / name
    if target.exists():
        if _sha256_file(target) != digest:
            raise _fail("ARTIFACT_COLLISION", "Generated output filename already exists with different bytes")
    else:
        _atomic_bytes(target, data)
    rel = output_rel / name
    return {
        "path": rel.as_posix(),
        "sha256": digest,
        "mime": mime,
        "width": width,
        "height": height,
        "size": len(data),
    }


def _repair_plan_for_attempt(run_dir: Path, frame_id: str, attempt: int) -> dict[str, Any] | None:
    if attempt <= 1:
        return None
    name = hashlib.sha256(frame_id.encode("utf-8")).hexdigest()[:20] + f"-{attempt}.json"
    path = run_dir / "repairs" / name
    if not path.exists():
        raise _fail("REPAIR_PLAN_REQUIRED", "Attempt > 1 requires persisted repair plan")
    return _read_json(path)


def _verified_generator_inputs(
    root: Path,
    reference_binding: dict[str, Any],
    repair_substrate: dict[str, Any] | None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    paths: list[Path] = []
    seen: set[str] = set()

    def add(role: str, raw_path: str, expected_sha: str, source_id: str | None = None) -> None:
        if expected_sha in seen:
            return
        rel, file_path = _resolve_file(root, raw_path, max_bytes=MAX_ARTIFACT_BYTES, suffixes=IMAGE_SUFFIXES)
        actual = _sha256_file(file_path)
        if actual != expected_sha:
            raise _fail(
                "REFERENCE_SHA_DRIFT",
                "Generator input bytes changed before ChatGPT upload",
                expected_sha256=expected_sha,
                actual_sha256=actual,
                path=rel.as_posix(),
            )
        seen.add(expected_sha)
        paths.append(file_path)
        rows.append({
            "role": role,
            "source_id": source_id,
            "path": rel.as_posix(),
            "sha256": actual,
        })

    if repair_substrate:
        add(
            "repair_substrate",
            str(repair_substrate["path"]),
            str(repair_substrate["sha256"]),
            str(repair_substrate.get("artifact_id") or ""),
        )
    for ref in reference_binding.get("references", []):
        if not isinstance(ref, dict):
            continue
        add(
            "reference",
            str(ref.get("path") or ""),
            str(ref.get("artifact_sha256") or ""),
            str(ref.get("source_id") or ""),
        )
    return paths, rows


def _generator_bind(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    run = _read_json(run_dir / "run.json")
    result = live_generator.bind_generator(_state_dir(context), run_dir, run["run_id"])
    return {"ok": True, **result}


def _advance_generated_only(
    run_dir: Path,
    run: dict[str, Any],
    frame_id: str,
    sequence_record: dict[str, Any],
) -> dict[str, Any]:
    plan = _read_json(run_dir / "plan.json")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise _fail("STATE_CORRUPT", "Run plan has no task list")
    index = next(
        (i for i, item in enumerate(tasks) if isinstance(item, dict) and item.get("frame_id") == frame_id),
        None,
    )
    if index is None or run.get("current_frame_id") != frame_id:
        raise _fail("RUN_FRAME_MISMATCH", "GENERATE_ONLY can advance only the current frame")
    sequence = _sequence_registry(run_dir)
    sequence["frames"][frame_id] = sequence_record
    _atomic_json(run_dir / "sequence-artifacts.json", sequence)
    next_index = index + 1
    next_frame = tasks[next_index].get("frame_id") if next_index < len(tasks) and isinstance(tasks[next_index], dict) else None
    updated = {
        **run,
        "current_index": next_index,
        "current_frame_id": next_frame,
        "status": "COMPLETE" if next_frame is None else "READY",
        "updated_at": _now(),
    }
    _atomic_json(run_dir / "run.json", updated)
    return updated


def _generator_run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    root = _workspace_root(context)
    raw, run = _snapshot_payload(run_dir)
    if run.get("status") not in {"READY", "RUNNING"}:
        raise _fail("RUN_NOT_READY", "Generator requires READY or RUNNING run state", status=run.get("status"))
    mode = _execution_mode(run)
    frame_id = _frame_id(params, run)
    attempt = params.get("attempt") or 1
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise _fail("ATTEMPT_INVALID", "attempt must be integer >= 1")
    if mode == "GENERATE_ONLY" and attempt != 1:
        raise _fail("GENERATE_ONLY_ATTEMPT_INVALID", "GENERATE_ONLY uses exactly one generation attempt per frame")

    registry = _artifact_registry(run_dir)
    sequence = _sequence_registry(run_dir)
    allow_unreviewed = mode == "GENERATE_ONLY"
    preview = _bridge({
        "action": "operation-preview",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "resolver": _resolver(registry, sequence, allow_unreviewed=allow_unreviewed),
        "allow_unreviewed": allow_unreviewed,
        "session_id": run["run_id"],
        "attempt": attempt,
    })
    if not preview.get("provider_required"):
        raise _fail("GENERATOR_NOT_REQUIRED", "Current task does not require ChatGPT image generation")

    task_entry = _bridge({
        "action": "task-inspect",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
    })
    task = task_entry.get("task") if isinstance(task_entry, dict) else None
    if not isinstance(task, dict):
        raise _fail("STATE_CORRUPT", "Compiled task body is unavailable")

    repair_plan = _repair_plan_for_attempt(run_dir, frame_id, attempt)
    repair_substrate = None
    prompt_text = str(task.get("prompt_text") or "")
    provider_operation_key = str(preview.get("generator_operation_key") or "")
    if attempt > 1:
        if mode != "GENERATE_AND_JUDGE":
            raise _fail("REPAIR_NOT_APPLICABLE", "Repair attempts are available only in GENERATE_AND_JUDGE mode")
        prior = live_state.immediate_failed_substrate(run_dir, frame_id, attempt)
        if not prior:
            raise _fail("FAILED_CANDIDATE_REQUIRED", "Repair must use the immediately failed candidate")
        repair_substrate = {
            "artifact_id": prior.get("artifact_id"),
            "sha256": prior.get("sha256"),
            "path": prior.get("path"),
            "attempt": prior.get("attempt"),
        }
        repair = repair_plan.get("repair") if isinstance(repair_plan, dict) else None
        if not isinstance(repair, dict):
            raise _fail("REPAIR_PLAN_REQUIRED", "Repair plan body is missing")
        expected = repair.get("substrate", {}).get("artifact_sha256")
        if expected != repair_substrate["sha256"]:
            raise _fail("REPAIR_SUBSTRATE_MISMATCH", "Repair plan does not bind the immediate failed candidate")
        prompt_text = str(repair.get("repair_prompt_text") or "")
        provider_operation_key = str(repair.get("repair_operation_key") or "")

    binding = preview.get("reference_binding") or {}
    reference_paths, upload_rows = _verified_generator_inputs(root, binding, repair_substrate)
    timeout_seconds = params.get("response_timeout_seconds") or 900
    provider = live_generator.generate_image(
        _state_dir(context),
        run_dir,
        operation_key=provider_operation_key,
        prompt=prompt_text,
        reference_paths=reference_paths,
        timeout_seconds=timeout_seconds,
    )
    data = provider.pop("bytes")
    artifact = _persist_generated_bytes(
        root,
        params.get("save_directory"),
        frame_id=frame_id,
        attempt=attempt,
        data=data,
    )
    artifact_id = f"{frame_id}-A{attempt}-{artifact['sha256'][:12]}"
    status = "generated_unreviewed" if mode == "GENERATE_ONLY" else "candidate"
    record = {
        "schema_version": "storyboard-chatgpt-web-candidate-1.0",
        "run_id": run["run_id"],
        "frame_id": frame_id,
        "attempt": attempt,
        "status": status,
        "artifact_id": artifact_id,
        "path": artifact["path"],
        "sha256": artifact["sha256"],
        "mime": artifact["mime"],
        "width": artifact["width"],
        "height": artifact["height"],
        "size": artifact["size"],
        "compiled_task_sha256": preview.get("compiled_task_sha256"),
        "generator_operation_key": preview.get("generator_operation_key"),
        "provider_operation_key": provider_operation_key,
        "repair_plan": repair_plan,
        "reference_input_fingerprint": binding.get("reference_input_fingerprint"),
        "reference_binding": binding,
        "repair_substrate": repair_substrate,
        "provider_input_proof": {
            "proof_kind": "chatgpt-web-direct-upload-sha256",
            "verified": True,
            "uploaded_inputs": upload_rows,
        },
        "provider_generation_receipt": provider.get("receipt"),
        "provider_binding": provider.get("binding"),
        "provider_recovered": bool(provider.get("recovered")),
        "registered_at": _now(),
    }
    state_path = live_state.candidate_state_path(run_dir, frame_id, attempt)
    if state_path.exists():
        prior = _read_json(state_path)
        comparable_old = {k: v for k, v in prior.items() if k not in {"registered_at", "status", "judge"}}
        comparable_new = {k: v for k, v in record.items() if k not in {"registered_at", "status", "judge"}}
        if comparable_old != comparable_new:
            raise _fail("CANDIDATE_COLLISION", "Generator attempt already has different identity")
        record = prior
    else:
        _atomic_json(state_path, record)

    updated_run = run
    if mode == "GENERATE_ONLY":
        sequence_record = {
            "source_type": "frame",
            "source_id": frame_id,
            "status": "generated_unreviewed",
            "artifact_id": artifact_id,
            "sha256": artifact["sha256"],
            "path": artifact["path"],
            "generated_at": _now(),
        }
        updated_run = _advance_generated_only(run_dir, run, frame_id, sequence_record)

    return {
        "ok": True,
        "execution_mode": mode,
        "candidate": record,
        "run": updated_run,
        "provider": {k: v for k, v in provider.items() if k != "bytes"},
    }


def _candidate_record(run_dir: Path, frame_id: str, attempt: int) -> dict[str, Any]:
    value = live_state.read_candidate(run_dir, frame_id, attempt)
    if value is None:
        raise _fail("CANDIDATE_NOT_FOUND", "Candidate attempt is not registered", frame_id=frame_id, attempt=attempt)
    return value


def _provider_status(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    provider = live_judge.provider_status(_state_dir(context))
    run_id = params.get("run_id")
    binding: dict[str, Any] = {}
    execution_mode = None
    if run_id:
        run_dir = _run_dir(context, run_id)
        binding = _read_json(run_dir / "provider-bindings.json") if (run_dir / "provider-bindings.json").exists() else {}
        execution_mode = _execution_mode(_read_json(run_dir / "run.json"))

    def role_status(role: str) -> dict[str, Any]:
        value = binding.get(role) if isinstance(binding, dict) else None
        if not isinstance(value, dict):
            return {
                "bound": False,
                "exact_target_bound": False,
                "route_kind": "none",
                "conversation_id": None,
            }
        return {
            "bound": True,
            "exact_target_bound": bool(value.get("target_id") and value.get("websocket_debugger_url")),
            "route_kind": str(value.get("route_kind") or "unknown"),
            "conversation_id": value.get("conversation_id"),
            "role_seeded": value.get("role_seeded") if role == "generator" else None,
        }

    safety = provider.get("safety") or {}
    return {
        "ok": True,
        "provider": {
            "devtools_online": provider.get("devtools_online"),
            "devtools_port": provider.get("devtools_port"),
            "execution_mode": execution_mode,
            "generator": role_status("generator"),
            "judge": role_status("judge"),
            "safety": safety,
            "history_safe_state": safety.get("history_safe_state"),
            "history_cooldown_remaining_seconds": safety.get("history_cooldown_remaining_seconds"),
            "history_rate_limit_strikes": safety.get("history_rate_limit_strikes"),
            "history_sensitive_navigation_next_in_seconds": safety.get("history_sensitive_navigation_next_in_seconds"),
            "shared_account_budget": safety.get("shared_account_budget"),
        },
    }


def _judge_preview(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id(params, run)
    attempt = params.get("attempt") or 1
    candidate = _candidate_record(run_dir, frame_id, attempt)
    frozen_binding = candidate.get("reference_binding")
    result = _bridge({
        "action": "judge-preview-frozen",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "reference_binding": frozen_binding,
        "session_id": run["run_id"],
        "attempt": attempt,
        "candidate_artifact_sha256": candidate["sha256"],
    })
    rebound = result.get("reference_binding") if isinstance(result, dict) else None
    if not isinstance(rebound, dict) or rebound.get("reference_input_fingerprint") != candidate.get("reference_input_fingerprint"):
        raise _fail("CANDIDATE_REFERENCE_BINDING_DRIFT", "Judge reference binding does not match the candidate's frozen provenance")
    return {
        "ok": True,
        "run_id": run["run_id"],
        "frame_id": frame_id,
        "attempt": attempt,
        "candidate": candidate,
        "preview": result,
    }


def _judge_bind(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    run = _read_json(run_dir / "run.json")
    result = live_judge.bind_judge(_state_dir(context), run_dir, run["run_id"])
    return {"ok": True, **result}


def _judge_run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    preview_result = _judge_preview(params, context)
    run_dir = _run_dir(context, preview_result["run_id"])
    root = _workspace_root(context)
    candidate = preview_result["candidate"]
    _, candidate_file = _resolve_file(
        root,
        candidate["path"],
        max_bytes=MAX_ARTIFACT_BYTES,
        suffixes=IMAGE_SUFFIXES,
    )
    actual_sha = _sha256_file(candidate_file)
    if actual_sha != candidate["sha256"]:
        raise _fail(
            "CANDIDATE_SHA_DRIFT",
            "Candidate bytes changed after registration",
            expected_sha256=candidate["sha256"],
            actual_sha256=actual_sha,
        )
    preview = preview_result["preview"]
    envelope = preview.get("judge_envelope")
    if not isinstance(envelope, str) or not envelope:
        raise _fail("JUDGE_ENVELOPE_MISSING", "Native Storyboard Forge Judge envelope is missing")
    operation_key = preview.get("judge_operation_key")
    provider = live_judge.review_candidate(
        _state_dir(context),
        run_dir,
        operation_key=operation_key,
        candidate_path=candidate_file,
        judge_prompt=envelope,
        timeout_seconds=params.get("response_timeout_seconds") or 600,
    )
    judge_result = provider["judge_result"]
    expected_identity = {
        "frame_id": preview_result["frame_id"],
        "attempt": preview_result["attempt"],
        "artifact_sha256": candidate["sha256"],
        "compiled_task_sha256": candidate["compiled_task_sha256"],
        "reference_input_fingerprint": candidate["reference_input_fingerprint"],
    }
    effective = _bridge({
        "action": "judge-validate",
        "raw_pack": (run_dir / "pack.snapshot.json").read_text(encoding="utf-8"),
        "source_path": _read_json(run_dir / "run.json")["source_path"],
        "source_sha256": _read_json(run_dir / "run.json")["source_sha256"],
        "frame_id": preview_result["frame_id"],
        "judge_result": judge_result,
        "expected_identity": expected_identity,
    })
    if effective.get("effective_verdict") == "FAIL":
        delta = judge_result.get("repair_delta")
        if not isinstance(delta, str) or not delta.strip():
            effective = {
                "effective_verdict": "REVIEW",
                "reasons": ["failed required axis but minimum repair_delta is missing"],
            }
    status_map = {"PASS": "judged_pass", "FAIL": "failed", "REVIEW": "review"}
    verdict = effective.get("effective_verdict")
    if verdict not in status_map:
        raise _fail("JUDGE_EFFECTIVE_INVALID", "Native Judge returned an invalid effective verdict")
    judge_record = {
        "schema_version": "storyboard-chatgpt-web-judge-record-1.0",
        "run_id": preview_result["run_id"],
        "frame_id": preview_result["frame_id"],
        "attempt": preview_result["attempt"],
        "judge_operation_key": operation_key,
        "candidate_sha256": candidate["sha256"],
        "judge_result": judge_result,
        "effective": effective,
        "provider_binding": provider.get("binding"),
        "provider_recovered": bool(provider.get("recovered")),
        "judged_at": _now(),
    }
    _atomic_json(
        live_state.judge_state_path(run_dir, preview_result["frame_id"], preview_result["attempt"]),
        judge_record,
    )
    updated_candidate = {
        **candidate,
        "status": status_map[verdict],
        "judge": {
            "judge_operation_key": operation_key,
            "effective_verdict": verdict,
            "repair_delta": judge_result.get("repair_delta"),
        },
    }
    _atomic_json(
        live_state.candidate_state_path(run_dir, preview_result["frame_id"], preview_result["attempt"]),
        updated_candidate,
    )
    return {
        "ok": True,
        "candidate": updated_candidate,
        "judge": judge_record,
    }


def _candidate_accept(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    run = _read_json(run_dir / "run.json")
    frame_id = _frame_id(params, run)
    attempt = params.get("attempt") or 1
    candidate = _candidate_record(run_dir, frame_id, attempt)
    judge = live_state.read_judge(run_dir, frame_id, attempt)
    if candidate.get("status") != "judged_pass":
        if candidate.get("status") == "accepted" and judge and judge.get("effective", {}).get("effective_verdict") == "PASS":
            return {"ok": True, "already_accepted": True, "candidate": candidate, "run": run}
        raise _fail(
            "CANDIDATE_NOT_PASS",
            "Only a candidate with native effective PASS can become canonical",
            status=candidate.get("status"),
        )
    if not judge or judge.get("effective", {}).get("effective_verdict") != "PASS":
        raise _fail("JUDGE_PASS_REQUIRED", "Accepted canonical frame requires a stored native Judge PASS")
    artifact_result = _artifact_register(
        {
            "run_id": run["run_id"],
            "source_type": "frame",
            "source_id": frame_id,
            "artifact_path": candidate["path"],
            "status": "accepted",
            "artifact_id": candidate["artifact_id"],
            "expected_sha256": candidate["sha256"],
        },
        context,
    )
    plan = _read_json(run_dir / "plan.json")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise _fail("STATE_CORRUPT", "Run plan has no task list")
    index = next(
        (i for i, item in enumerate(tasks) if isinstance(item, dict) and item.get("frame_id") == frame_id),
        None,
    )
    if index is None:
        raise _fail("STATE_CORRUPT", "Accepted frame is missing from run plan")
    if run.get("current_frame_id") != frame_id:
        raise _fail(
            "RUN_FRAME_MISMATCH",
            "Cannot advance canonical run from a frame that is not current",
            expected_frame_id=run.get("current_frame_id"),
            candidate_frame_id=frame_id,
        )
    next_index = index + 1
    next_frame = tasks[next_index].get("frame_id") if next_index < len(tasks) and isinstance(tasks[next_index], dict) else None
    updated_run = {
        **run,
        "current_index": next_index,
        "current_frame_id": next_frame,
        "status": "COMPLETE" if next_frame is None else "READY",
        "updated_at": _now(),
    }
    _atomic_json(run_dir / "run.json", updated_run)
    accepted_candidate = {
        **candidate,
        "status": "accepted",
        "accepted_at": _now(),
        "canonical_artifact": artifact_result["artifact"],
    }
    _atomic_json(live_state.candidate_state_path(run_dir, frame_id, attempt), accepted_candidate)
    return {
        "ok": True,
        "already_accepted": False,
        "candidate": accepted_candidate,
        "run": updated_run,
        "canonical_artifact": artifact_result["artifact"],
    }


def _repair_preview(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id(params, run)
    source_attempt = params.get("attempt") or 1
    if not isinstance(source_attempt, int) or isinstance(source_attempt, bool) or source_attempt < 1:
        raise _fail("ATTEMPT_INVALID", "repair source attempt must be integer >= 1")
    candidate = _candidate_record(run_dir, frame_id, source_attempt)
    if candidate.get("status") != "failed":
        raise _fail("FAILED_CANDIDATE_REQUIRED", "Repair requires the immediately failed candidate")
    judge = live_state.read_judge(run_dir, frame_id, source_attempt)
    if not judge or judge.get("effective", {}).get("effective_verdict") != "FAIL":
        raise _fail("FAILED_JUDGE_REQUIRED", "Repair requires a stored native Judge FAIL")
    delta = judge.get("judge_result", {}).get("repair_delta")
    if not isinstance(delta, str) or not delta.strip():
        raise _fail("REPAIR_DELTA_MISSING", "Native Judge FAIL has no minimum repair delta")
    result = _bridge({
        "action": "repair-preview-frozen",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "reference_binding": candidate.get("reference_binding"),
        "session_id": run["run_id"],
        "next_attempt": source_attempt + 1,
        "failed_artifact": {
            "frame_id": frame_id,
            "attempt": source_attempt,
            "artifact_id": candidate["artifact_id"],
            "sha256": candidate["sha256"],
            "path": candidate["path"],
            "status": candidate["status"],
        },
        "minimum_repair_delta": delta.strip(),
    })
    repair_name = hashlib.sha256(frame_id.encode("utf-8")).hexdigest()[:20] + f"-{source_attempt + 1}.json"
    _atomic_json(run_dir / "repairs" / repair_name, {"run_id": run["run_id"], "frame_id": frame_id, "source_attempt": source_attempt, "next_attempt": source_attempt + 1, "repair": result, "created_at": _now()})
    return {
        "ok": True,
        "run_id": run["run_id"],
        "frame_id": frame_id,
        "source_attempt": source_attempt,
        "next_attempt": source_attempt + 1,
        "repair": result,
    }


def _advance_index(run_dir: Path, run: dict[str, Any], frame_id: str) -> dict[str, Any]:
    plan = _read_json(run_dir / "plan.json")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise _fail("STATE_CORRUPT", "Run plan has no task list")
    index = next(
        (i for i, item in enumerate(tasks) if isinstance(item, dict) and item.get("frame_id") == frame_id),
        None,
    )
    if index is None or run.get("current_frame_id") != frame_id:
        raise _fail("RUN_FRAME_MISMATCH", "Can advance only the current frame")
    next_index = index + 1
    next_frame = tasks[next_index].get("frame_id") if next_index < len(tasks) and isinstance(tasks[next_index], dict) else None
    updated = {
        **run,
        "current_index": next_index,
        "current_frame_id": next_frame,
        "status": "COMPLETE" if next_frame is None else "READY",
        "updated_at": _now(),
    }
    _atomic_json(run_dir / "run.json", updated)
    return updated


def _advance_nonprovider(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_dir = _run_dir(context, params.get("run_id"))
    raw, run = _snapshot_payload(run_dir)
    frame_id = _frame_id({}, run)
    registry = _artifact_registry(run_dir)
    sequence = _sequence_registry(run_dir)
    allow_unreviewed = _execution_mode(run) == "GENERATE_ONLY"
    preview = _bridge({
        "action": "operation-preview",
        "raw_pack": raw,
        "source_path": run["source_path"],
        "source_sha256": run["source_sha256"],
        "frame_id": frame_id,
        "resolver": _resolver(registry, sequence, allow_unreviewed=allow_unreviewed),
        "allow_unreviewed": allow_unreviewed,
        "session_id": run["run_id"],
        "attempt": 1,
    })
    if preview.get("provider_required"):
        raise _fail("PROVIDER_TASK_REQUIRED", "Current task requires generator provider")
    operation = preview.get("operation")
    if operation == "reuse":
        reused = preview.get("reuse_artifact") or {}
        if allow_unreviewed:
            source_frame_id = str(reused.get("source_frame_id") or "")
            source = sequence.get("frames", {}).get(source_frame_id) or registry.get("artifacts", {}).get(f"frame:{source_frame_id}")
            if not isinstance(source, dict):
                raise _fail("REUSE_PREDECESSOR_MISSING", "GENERATE_ONLY reuse predecessor is unavailable")
            record = {
                **source,
                "source_type": "frame",
                "source_id": frame_id,
                "status": "generated_unreviewed",
                "artifact_id": f"{frame_id}-reuse-{str(source.get('sha256') or '')[:12]}",
                "generated_at": _now(),
                "reused_from": source_frame_id,
            }
            sequence["frames"][frame_id] = record
            _atomic_json(run_dir / "sequence-artifacts.json", sequence)
        else:
            source_path = str(reused.get("path") or "")
            source_sha = str(reused.get("artifact_sha256") or "")
            _artifact_register({
                "run_id": run["run_id"],
                "source_type": "frame",
                "source_id": frame_id,
                "artifact_path": source_path,
                "status": "accepted",
                "artifact_id": f"{frame_id}-reuse-{source_sha[:12]}",
                "expected_sha256": source_sha,
            }, context)
    updated = _advance_index(run_dir, run, frame_id)
    return {"ok": True, "operation": operation, "run": updated}


def _highest_candidate(run_dir: Path, frame_id: str, limit: int = 32) -> dict[str, Any] | None:
    latest = None
    for attempt in range(1, limit + 1):
        value = live_state.read_candidate(run_dir, frame_id, attempt)
        if value is None:
            break
        latest = value
    return latest


def _mark_waiting_human(run_dir: Path, run: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = {**run, "status": "WAITING_HUMAN", "pause_reason": reason, "updated_at": _now()}
    _atomic_json(run_dir / "run.json", updated)
    return updated


def _auto_run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_id = params.get("run_id")
    save_directory = params.get("save_directory")
    response_timeout = params.get("response_timeout_seconds") or 900
    max_steps = params.get("max_steps") or 100
    events: list[dict[str, Any]] = []

    for _ in range(max_steps):
        run_dir = _run_dir(context, run_id)
        run = _read_json(run_dir / "run.json")
        if run.get("status") == "COMPLETE":
            return {"ok": True, "completed": True, "run": run, "events": events}
        if run.get("status") == "WAITING_HUMAN":
            return {"ok": True, "completed": False, "run": run, "events": events}
        current = _run_current({"run_id": run_id}, context)
        task_entry = current.get("task") or {}
        operation = task_entry.get("operation")
        frame_id = str(task_entry.get("frame_id") or run.get("current_frame_id") or "")
        if operation in {"reuse", "post"}:
            advanced = _advance_nonprovider({"run_id": run_id}, context)
            events.append({"frame_id": frame_id, "action": operation})
            continue

        mode = _execution_mode(run)
        if mode == "GENERATE_ONLY":
            generated = _generator_run({
                "run_id": run_id,
                "frame_id": frame_id,
                "attempt": 1,
                "save_directory": save_directory,
                "response_timeout_seconds": response_timeout,
            }, context)
            events.append({
                "frame_id": frame_id,
                "action": "generated_unreviewed",
                "sha256": generated["candidate"]["sha256"],
            })
            continue

        candidate = _highest_candidate(run_dir, frame_id)
        if candidate is None:
            generated = _generator_run({
                "run_id": run_id,
                "frame_id": frame_id,
                "attempt": 1,
                "save_directory": save_directory,
                "response_timeout_seconds": response_timeout,
            }, context)
            events.append({"frame_id": frame_id, "attempt": 1, "action": "generated", "sha256": generated["candidate"]["sha256"]})
            continue

        attempt = int(candidate.get("attempt") or 1)
        status = str(candidate.get("status") or "")
        if status == "candidate":
            judged = _judge_run({
                "run_id": run_id,
                "frame_id": frame_id,
                "attempt": attempt,
                "response_timeout_seconds": response_timeout,
            }, context)
            verdict = judged["judge"]["effective"]["effective_verdict"]
            events.append({"frame_id": frame_id, "attempt": attempt, "action": "judged", "verdict": verdict})
            continue

        if status == "judged_pass":
            if str(run.get("mode_effective") or "AUTO") == "HUMAN_CONFIRM":
                paused = _mark_waiting_human(run_dir, run, "HUMAN_CONFIRM_PASS")
                return {"ok": True, "completed": False, "run": paused, "events": events}
            accepted = _candidate_accept({"run_id": run_id, "frame_id": frame_id, "attempt": attempt}, context)
            events.append({"frame_id": frame_id, "attempt": attempt, "action": "accepted", "sha256": candidate.get("sha256")})
            continue

        if status == "failed":
            if str(run.get("mode_effective") or "AUTO") == "HUMAN_CONFIRM":
                paused = _mark_waiting_human(run_dir, run, "HUMAN_CONFIRM_FAIL")
                return {"ok": True, "completed": False, "run": paused, "events": events}
            retry_budget = int(run.get("retry_budget_per_frame") or 0)
            if attempt >= 1 + retry_budget:
                paused = _mark_waiting_human(run_dir, run, "AUTO_RETRY_EXHAUSTED")
                return {"ok": True, "completed": False, "run": paused, "events": events}
            repair = _repair_preview({"run_id": run_id, "frame_id": frame_id, "attempt": attempt}, context)
            next_attempt = int(repair["next_attempt"])
            generated = _generator_run({
                "run_id": run_id,
                "frame_id": frame_id,
                "attempt": next_attempt,
                "save_directory": save_directory,
                "response_timeout_seconds": response_timeout,
            }, context)
            events.append({"frame_id": frame_id, "attempt": next_attempt, "action": "repair_generated", "sha256": generated["candidate"]["sha256"]})
            continue

        if status == "review":
            paused = _mark_waiting_human(run_dir, run, "JUDGE_REVIEW")
            return {"ok": True, "completed": False, "run": paused, "events": events}

        if status == "accepted":
            run = _read_json(run_dir / "run.json")
            if run.get("current_frame_id") == frame_id:
                raise _fail("STATE_CORRUPT", "Accepted candidate did not advance run")
            continue

        raise _fail("CANDIDATE_STATUS_INVALID", "Unsupported candidate status in auto-run", status=status)

    run = _read_json(_run_dir(context, run_id) / "run.json")
    return {"ok": True, "completed": run.get("status") == "COMPLETE", "run": run, "events": events, "step_limit_reached": True}


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return {
            "ok": True,
            "extension": "storyboard-chatgpt-web",
            "version": "0.2.0",
            "provider_enabled": True,
            "judge_provider_enabled": True,
            "generator_provider": "chatgpt-web-native-image",
            "network_permission": True,
            "node_executable": _node_executable(),
            "state_dir_ready": _state_dir(context).is_dir(),
        }
    if action == "verification-plan":
        return {
            "ok": True,
            "phase": "ChatGPT Web Native Generator + Optional Native Visual Judge",
            "provider_enabled": True,
            "execution_modes": ["GENERATE_AND_JUDGE", "GENERATE_ONLY"],
            "steps": [
                "pack-inspect exported Prompt Pack JSON",
                "run-init freezes exact pack bytes plus execution_mode",
                "generator-bind opens or reuses a dedicated ChatGPT Web Generator conversation",
                "generator-run uploads exact references and sends the compiled Prompt Pack task",
                "exact ChatGPT image_asset_pointer bytes are recovered and persisted with SHA provenance",
                "GENERATE_ONLY records generated_unreviewed and advances without Judge",
                "GENERATE_AND_JUDGE uploads exact candidate bytes to the independent Judge conversation",
                "FAIL binds minimum repair delta to the immediate failed candidate; PASS-only becomes canonical",
                "auto-run resumes from durable state and alternates Generator/Judge until completion or human boundary",
            ],
        }
    if action == "pack-inspect":
        return _pack_inspect(_workspace_root(context), params.get("prompt_pack_path"))
    if action == "task-inspect":
        root = _workspace_root(context)
        rel, raw, sha = _read_pack(root, params.get("prompt_pack_path"))
        result = _bridge({
            "action": "task-inspect",
            "raw_pack": raw,
            "source_path": rel.as_posix(),
            "source_sha256": sha,
            "frame_id": params.get("frame_id"),
        })
        return {"ok": True, "workspace_path": rel.as_posix(), "task": result}
    if action == "run-init":
        return _run_init(params, context)
    if action == "run-status":
        return _run_status(params, context)
    if action == "run-mode-set":
        return _run_mode_set(params, context)
    if action == "run-current":
        return _run_current(params, context)
    if action == "artifact-register":
        return _artifact_register(params, context)
    if action == "reference-bind":
        return _reference_bind(params, context)
    if action == "operation-preview":
        return _operation_preview(params, context)
    if action == "provider-status": return _provider_status(params, context)
    if action == "generator-bind": return _generator_bind(params, context)
    if action == "generator-run": return _generator_run(params, context)
    if action == "auto-run": return _auto_run(params, context)
    if action == "judge-preview": return _judge_preview(params, context)
    if action == "judge-bind": return _judge_bind(params, context)
    if action == "judge-run": return _judge_run(params, context)
    if action == "candidate-accept": return _candidate_accept(params, context)
    if action == "repair-preview": return _repair_preview(params, context)
    raise _fail("EXTENSION_ACTION_NOT_FOUND", f"Unsupported storyboard-chatgpt-web action: {action}")
