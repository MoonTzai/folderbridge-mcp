from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    from folderbridge_mcp.extension_api import ExtensionError
except ImportError:
    class ExtensionError(RuntimeError):
        def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None, retryable: bool = False):
            super().__init__(message)
            self.code = code
            self.details = details or {}
            self.retryable = retryable

BRIDGE_URL = "http://127.0.0.1:8766"
TOKEN_PATH = Path(__file__).resolve().with_name("bridge-token.txt")
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024

PATH_READ_ACTIONS = {
    "open-blend": ("path",),
    "import-asset": ("path",),
    "load-image": ("path",),
    "headless-render": ("blend_path",),
}
PATH_WRITE_ACTIONS = {
    "save-blend": ("path",),
    "export-asset": ("path",),
    "render-still": ("path",),
    "render-animation": ("output_dir",),
    "headless-render": ("output_dir",),
}
MUTATING_ACTIONS = {
    "set-context", "create-object", "delete-object", "create-data", "remove-data",
    "load-image", "set-properties", "set-transform", "mesh-set-geometry", "collection-create",
    "collection-link", "modifier-add", "modifier-remove", "constraint-add", "constraint-remove",
    "node-create", "node-remove", "node-set", "node-link", "node-unlink", "node-interface-socket",
    "keyframe-insert", "keyframe-delete", "set-frame", "operator-call", "undo", "redo",
    "open-blend", "save-blend", "import-asset", "export-asset", "render-still", "render-animation",
}
SAFE_OPERATOR_PREFIXES = {
    "object", "mesh", "curve", "armature", "pose", "transform", "view3d", "sculpt",
    "paint", "geometry", "node", "anim", "nla", "constraint", "marker", "rigidbody",
    "uv", "ed", "screen", "grease_pencil", "gpencil", "sequencer", "clip", "mask",
    "fluid", "ptcache", "boid", "particle", "lattice",
}
DANGEROUS_OPERATOR_TOKENS = {
    "open", "save", "import", "export", "script", "python", "url", "path", "file",
    "addon", "extension", "install", "quit", "console", "recover", "factory",
}
PATHLIKE_KEYS = {
    "path", "filepath", "directory", "filename", "url", "script", "command", "executable",
    "python", "module",
}
SENSITIVE_PARTS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx"}
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "headless-probe":
        return _headless_probe()
    if action == "headless-render":
        return _headless_render(params, context)
    if action == "status":
        try:
            data = _request("status", {}, workspace_root="")
            data.setdefault("ok", True)
            data["bridge_online"] = True
        except ExtensionError as exc:
            probe = _headless_probe()
            return {
                "ok": True,
                "bridge_online": False,
                "bridge_error": str(exc),
                "blender": probe,
            }
        return data

    workspace_root = _workspace_root(context)
    if action in MUTATING_ACTIONS and bool(context.get("workspace_read_only")):
        raise ExtensionError("READ_ONLY", "Selected workspace is read-only.", retryable=False)

    clean_params = _normalize_params(action, params, workspace_root)
    if action == "operator-call":
        _validate_operator(clean_params)

    result = _request(action, clean_params, workspace_root=str(workspace_root))
    output: dict[str, Any] = {
        "ok": True,
        "action": action,
        "data": result,
    }

    if action == "screenshot":
        encoded = result.pop("image_base64", "")
        mime = result.pop("mime_type", "image/png")
        if encoded:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_SCREENSHOT_BYTES:
                raise ExtensionError("BLENDER_SCREENSHOT_TOO_LARGE", "Blender screenshot exceeded the bounded response size.")
            output["_content"] = [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)},
                {"type": "image", "data": encoded, "mimeType": mime},
            ]

    artifacts: list[dict[str, str]] = []
    if action in {"save-blend", "export-asset", "render-still"}:
        artifact = result.get("workspace_artifact")
        if isinstance(artifact, str) and artifact:
            artifacts.append({"path": artifact, "label": action, "kind": "file"})
    elif action == "render-animation":
        files = result.get("workspace_artifacts")
        if isinstance(files, list):
            for item in files[:64]:
                if isinstance(item, str) and item:
                    artifacts.append({"path": item, "label": "render-frame", "kind": "file"})
    if artifacts:
        output["workspace_artifacts"] = artifacts
    return output


def _request(action: str, params: dict[str, Any], *, workspace_root: str) -> dict[str, Any]:
    token = _read_token()
    body = json.dumps(
        {"action": action, "params": params, "workspace_root": workspace_root},
        ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        BRIDGE_URL + "/rpc",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-FolderBridge-Blender-Token": token,
        },
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise ExtensionError("BLENDER_BRIDGE_HTTP", f"Blender bridge HTTP {exc.code}: {detail}", retryable=True) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExtensionError(
            "BLENDER_BRIDGE_OFFLINE",
            "Cannot reach FolderBridge Blender Bridge at 127.0.0.1:8766. Open Blender with the bridge add-on enabled.",
            retryable=True,
        ) from exc
    if len(data) > MAX_RESPONSE_BYTES:
        raise ExtensionError("BLENDER_BRIDGE_RESPONSE_TOO_LARGE", "Blender bridge response exceeded 32 MiB.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ExtensionError("BLENDER_BRIDGE_BAD_JSON", "Blender bridge returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ExtensionError("BLENDER_BRIDGE_BAD_RESPONSE", "Blender bridge returned an invalid response envelope.")
    if not payload.get("ok"):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        raise ExtensionError(
            str(error.get("code") or "BLENDER_BRIDGE_ERROR"),
            str(error.get("message") or "Blender bridge action failed."),
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
            retryable=bool(error.get("retryable", False)),
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ExtensionError("BLENDER_BRIDGE_BAD_RESULT", "Blender bridge result must be an object.")
    return result


def _read_token() -> str:
    try:
        token = TOKEN_PATH.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ExtensionError(
            "BLENDER_BRIDGE_NOT_INSTALLED",
            "bridge-token.txt is missing. Re-run Blender Toolkit install.ps1.",
            retryable=False,
        ) from exc
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", token):
        raise ExtensionError("BLENDER_BRIDGE_BAD_TOKEN", "bridge-token.txt is invalid.", retryable=False)
    return token


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise ExtensionError("WORKSPACE_REQUIRED", "This Blender action requires a selected workspace.")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise ExtensionError("WORKSPACE_INVALID", "Selected workspace root is unavailable.")
    return root


def _normalize_params(action: str, params: dict[str, Any], root: Path) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(params, ensure_ascii=False, allow_nan=False))
    for key in PATH_READ_ACTIONS.get(action, ()):
        cleaned[key] = _clean_relative(cleaned[key], root, must_exist=True, directory=False)
    for key in PATH_WRITE_ACTIONS.get(action, ()):
        is_dir = action in {"render-animation", "headless-render"} and key == "output_dir"
        cleaned[key] = _clean_relative(cleaned[key], root, must_exist=False, directory=is_dir)
    return cleaned


def _clean_relative(value: Any, root: Path, *, must_exist: bool, directory: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ExtensionError("INVALID_PATH", "Workspace path must be a non-empty relative string.")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ExtensionError("INVALID_PATH", "Workspace paths must use POSIX-style relative syntax.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ExtensionError("INVALID_PATH", "Workspace path contains an invalid component.")
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if stem in RESERVED_NAMES or part.endswith(".") or ":" in part:
            raise ExtensionError("INVALID_PATH", "Workspace path contains a reserved or stream-like component.")
    if any(part.lower() in SENSITIVE_PARTS for part in parts):
        raise ExtensionError("INVALID_PATH", "Workspace path enters a denied dependency/VCS directory.")
    if Path(value).suffix.lower() in SENSITIVE_SUFFIXES or Path(value).name.lower() in {".env", ".env.local"}:
        raise ExtensionError("INVALID_PATH", "Credential-like workspace paths are denied.")
    candidate = root.joinpath(*parts)
    parent = candidate if directory else candidate.parent
    _reject_links(root, parent, allow_missing=True)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ExtensionError("INVALID_PATH", "Workspace path escapes the selected workspace.") from exc
    if must_exist:
        if directory and not resolved.is_dir():
            raise ExtensionError("PATH_NOT_FOUND", f"Workspace directory does not exist: {value}")
        if not directory and not resolved.is_file():
            raise ExtensionError("PATH_NOT_FOUND", f"Workspace file does not exist: {value}")
        _reject_links(root, resolved, allow_missing=False)
    return value


def _reject_links(root: Path, path: Path, *, allow_missing: bool) -> None:
    try:
        rel = path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ExtensionError("INVALID_PATH", "Path escapes workspace.") from exc
    current = root
    for part in rel.parts:
        current = current / part
        if not current.exists():
            if allow_missing:
                return
            raise ExtensionError("PATH_NOT_FOUND", f"Path component does not exist: {part}")
        try:
            st = current.lstat()
        except OSError as exc:
            raise ExtensionError("PATH_IO_ERROR", f"Cannot inspect path component: {part}") from exc
        if current.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & 0x400):
            raise ExtensionError("REPARSE_POINT_DENIED", "Symlink/reparse-point workspace paths are denied.")


def _validate_operator(params: dict[str, Any]) -> None:
    operator = str(params.get("operator") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", operator):
        raise ExtensionError("BLENDER_OPERATOR_DENIED", "Operator must be in category.operator form.")
    prefix, opname = operator.split(".", 1)
    if prefix not in SAFE_OPERATOR_PREFIXES or any(token in opname for token in DANGEROUS_OPERATOR_TOKENS):
        raise ExtensionError("BLENDER_OPERATOR_DENIED", f"Operator is outside the safe operator surface: {operator}")
    props = params.get("properties") or {}
    if not isinstance(props, dict):
        raise ExtensionError("BLENDER_OPERATOR_BAD_PROPERTIES", "Operator properties must be an object.")
    for key in props:
        low = str(key).lower()
        if low in PATHLIKE_KEYS or any(token in low for token in ("filepath", "directory", "filename", "url", "script", "command")):
            raise ExtensionError("BLENDER_OPERATOR_PATH_DENIED", f"Path/code-like operator property is denied: {key}")


def _find_blender() -> Path | None:
    found = shutil.which("blender.exe")
    if found:
        return Path(found)
    candidates: list[Path] = []
    base = Path(r"C:\Program Files\Blender Foundation")
    if base.is_dir():
        candidates.extend(base.glob(r"Blender *\blender.exe"))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe") as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    if value:
                        candidates.append(Path(value))
            except OSError:
                pass
    except Exception:
        pass
    existing = [p for p in candidates if p.is_file() and p.name.lower() == "blender.exe"]
    return sorted(existing, key=lambda p: str(p).lower(), reverse=True)[0] if existing else None


def _headless_probe() -> dict[str, Any]:
    blender = _find_blender()
    if blender is None:
        return {"ok": True, "installed": False, "path": None}
    try:
        proc = subprocess.run(
            [str(blender), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise ExtensionError("BLENDER_EXEC_FAILED", f"Failed to run blender.exe: {exc}") from exc
    first = (proc.stdout or proc.stderr or "").splitlines()
    return {
        "ok": True,
        "installed": proc.returncode == 0,
        "path": str(blender),
        "returncode": proc.returncode,
        "version_line": first[0][:512] if first else "",
    }


def _headless_render(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if bool(context.get("workspace_read_only")):
        raise ExtensionError("READ_ONLY", "Selected workspace is read-only.")
    root = _workspace_root(context)
    blend_rel = _clean_relative(params["blend_path"], root, must_exist=True, directory=False)
    out_rel = _clean_relative(params["output_dir"], root, must_exist=False, directory=True)
    blender = _find_blender()
    if blender is None:
        raise ExtensionError("BLENDER_NOT_FOUND", "blender.exe was not found.")
    blend = root / Path(blend_rel)
    out_dir = root / Path(out_rel)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = int(params.get("frame", 1))
    output_pattern = out_dir / "frame_#####"
    proc = subprocess.run(
        [str(blender), "--background", str(blend), "--render-output", str(output_pattern), "--render-frame", str(frame)],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=None,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise ExtensionError(
            "BLENDER_HEADLESS_RENDER_FAILED",
            f"Blender headless render failed with exit code {proc.returncode}.",
            details={"stdout_tail": proc.stdout[-4096:], "stderr_tail": proc.stderr[-4096:]},
        )
    files = sorted(p for p in out_dir.iterdir() if p.is_file())[:64]
    return {
        "ok": True,
        "blender_path": str(blender),
        "returncode": proc.returncode,
        "workspace_artifacts": [p.relative_to(root).as_posix() for p in files],
        "stdout_tail": proc.stdout[-4096:],
        "stderr_tail": proc.stderr[-4096:],
    }
