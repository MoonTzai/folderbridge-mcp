from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .security import ToolError, Workspace


COMFYUI_HOST = "127.0.0.1"
COMFYUI_PORT = 8188
MAX_WORKFLOW_BYTES = 4 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_IMAGES = 4
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 600


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ToolError("COMFYUI_REDIRECT_DENIED", "ComfyUI loopback requests may not redirect.")


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def comfyui_status(*, port: int = COMFYUI_PORT) -> dict[str, Any]:
    try:
        stats = _json_request("GET", "/system_stats", port=port, timeout=3)
    except ToolError as exc:
        if exc.code in {"COMFYUI_OFFLINE", "COMFYUI_HTTP_ERROR", "COMFYUI_INVALID_RESPONSE"}:
            return {
                "online": False,
                "endpoint": f"http://{COMFYUI_HOST}:{port}",
                "detail": str(exc),
            }
        raise
    return {
        "online": True,
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "system_stats": stats,
    }


def run_workflow(
    workspace: Workspace,
    workflow_path: str,
    *,
    overrides: dict[str, Any] | None = None,
    save_directory: str | None = None,
    timeout_seconds: int = 180,
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ToolError("INVALID_ARGUMENT", f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}.")
    workflow = _load_workflow(workspace, workflow_path)
    _apply_overrides(workflow, overrides)

    client_id = f"folderbridge-{uuid.uuid4()}"
    queued = _json_request(
        "POST",
        "/prompt",
        port=port,
        timeout=15,
        payload={"prompt": workflow, "client_id": client_id},
    )
    prompt_id = queued.get("prompt_id") if isinstance(queued, dict) else None
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI did not return a prompt_id.")
    node_errors = queued.get("node_errors") if isinstance(queued, dict) else None
    if isinstance(node_errors, dict) and node_errors:
        raise ToolError("COMFYUI_NODE_ERROR", "ComfyUI rejected one or more workflow nodes.", node_errors=node_errors)

    deadline = time.monotonic() + timeout_seconds
    history_entry: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        history = _json_request("GET", f"/history/{prompt_id}", port=port, timeout=10)
        if isinstance(history, dict):
            candidate = history.get(prompt_id)
            if isinstance(candidate, dict):
                history_entry = candidate
                break
        time.sleep(0.5)
    if history_entry is None:
        raise ToolError("COMFYUI_TIMEOUT", f"ComfyUI workflow did not finish within {timeout_seconds} seconds.", prompt_id=prompt_id)

    status = history_entry.get("status")
    if isinstance(status, dict) and status.get("status_str") == "error":
        raise ToolError("COMFYUI_EXECUTION_ERROR", "ComfyUI reported workflow execution failure.", status=status)

    descriptors = _output_image_descriptors(history_entry)
    selected = descriptors[:MAX_OUTPUT_IMAGES]
    rendered: list[dict[str, Any]] = []
    image_content: list[dict[str, str]] = []
    total_bytes = 0
    save_root = _prepare_save_directory(workspace, save_directory) if save_directory else None

    for index, descriptor in enumerate(selected, start=1):
        data = _image_request(descriptor, port=port)
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ToolError("COMFYUI_OUTPUT_TOO_LARGE", f"Returned images exceed {MAX_TOTAL_IMAGE_BYTES} bytes total.")
        mime_type, extension = _image_type(data)
        sha256 = hashlib.sha256(data).hexdigest()
        saved_path: str | None = None
        if save_root is not None:
            output_path = save_root / f"comfyui-{prompt_id}-{index}{extension}"
            try:
                output_path.write_bytes(data)
            except OSError as exc:
                raise ToolError("WRITE_FAILED", f"Could not save ComfyUI output: {exc}") from exc
            saved_path = output_path.relative_to(workspace.root).as_posix()
        rendered.append(
            {
                "index": index,
                "source": descriptor,
                "size": len(data),
                "sha256": sha256,
                "mime_type": mime_type,
                "saved_path": saved_path,
            }
        )
        image_content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type})

    metadata = {
        "online": True,
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "workflow_path": workflow_path,
        "prompt_id": prompt_id,
        "images_found": len(descriptors),
        "images_returned": len(rendered),
        "images": rendered,
        "status": status,
    }
    return {
        **metadata,
        "_content": [
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True)},
            *image_content,
        ],
    }


def _load_workflow(workspace: Workspace, raw: str) -> dict[str, Any]:
    path = workspace.resolve(raw)
    if not path.is_file():
        raise ToolError("NOT_FOUND", "ComfyUI workflow JSON does not exist.", path=raw)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolError("READ_FAILED", f"Could not read ComfyUI workflow: {exc}") from exc
    if len(data) > MAX_WORKFLOW_BYTES:
        raise ToolError("FILE_TOO_LARGE", f"ComfyUI workflow exceeds {MAX_WORKFLOW_BYTES} bytes.", path=raw)
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("INVALID_COMFYUI_WORKFLOW", "Workflow must be valid UTF-8 JSON in ComfyUI API format.") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ToolError("INVALID_COMFYUI_WORKFLOW", "Workflow must be a non-empty JSON object in ComfyUI API format.")
    for node_id, node in parsed.items():
        if not isinstance(node_id, str) or not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise ToolError("INVALID_COMFYUI_WORKFLOW", "Workflow nodes must use ComfyUI API format with class_type fields.")
        if "inputs" in node and not isinstance(node["inputs"], dict):
            raise ToolError("INVALID_COMFYUI_WORKFLOW", "Workflow node inputs must be objects.")
    return parsed


def _apply_overrides(workflow: dict[str, Any], overrides: dict[str, Any] | None) -> None:
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        raise ToolError("INVALID_ARGUMENT", "overrides must be an object keyed by ComfyUI node id.")
    for node_id, input_values in overrides.items():
        if not isinstance(node_id, str) or node_id not in workflow:
            raise ToolError("INVALID_ARGUMENT", f"Override references unknown ComfyUI node: {node_id}")
        if not isinstance(input_values, dict):
            raise ToolError("INVALID_ARGUMENT", f"Override for node {node_id} must be an object of input values.")
        inputs = workflow[node_id].setdefault("inputs", {})
        if not isinstance(inputs, dict):
            raise ToolError("INVALID_COMFYUI_WORKFLOW", f"Node {node_id} inputs are not an object.")
        inputs.update(input_values)


def _prepare_save_directory(workspace: Workspace, raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ToolError("INVALID_ARGUMENT", "save_directory must be a non-empty workspace-relative path.")
    path = workspace.resolve(raw, for_write=True, allow_directory=True)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError("WRITE_FAILED", f"Could not create ComfyUI output directory: {exc}") from exc
    if not path.is_dir():
        raise ToolError("NOT_A_DIRECTORY", "save_directory is not a directory.", path=raw)
    return path


def _output_image_descriptors(history_entry: dict[str, Any]) -> list[dict[str, str]]:
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        return []
    result: list[dict[str, str]] = []
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        images = node_output.get("images")
        if not isinstance(images, list):
            continue
        for item in images:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            subfolder = item.get("subfolder", "")
            output_type = item.get("type", "output")
            if all(isinstance(value, str) for value in (filename, subfolder, output_type)) and filename:
                result.append({"filename": filename, "subfolder": subfolder, "type": output_type})
    return result


def _image_request(descriptor: dict[str, str], *, port: int) -> bytes:
    query = urlencode(
        {
            "filename": descriptor["filename"],
            "subfolder": descriptor["subfolder"],
            "type": descriptor["type"],
        }
    )
    data = _request_bytes("GET", f"/view?{query}", port=port, timeout=15, limit=MAX_IMAGE_BYTES)
    _image_type(data)
    return data


def _json_request(
    method: str,
    path: str,
    *,
    port: int,
    timeout: int,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    data = _request_bytes(
        method,
        path,
        port=port,
        timeout=timeout,
        limit=MAX_JSON_RESPONSE_BYTES,
        body=body,
        content_type="application/json" if body is not None else None,
    )
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned invalid JSON.") from exc


def _request_bytes(
    method: str,
    path: str,
    *,
    port: int,
    timeout: int,
    limit: int,
    body: bytes | None = None,
    content_type: str | None = None,
) -> bytes:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ToolError("INVALID_ARGUMENT", "Invalid ComfyUI loopback port.")
    if not path.startswith("/") or path.startswith("//"):
        raise ToolError("INVALID_ARGUMENT", "Invalid ComfyUI API path.")
    url = f"http://{COMFYUI_HOST}:{port}{path}"
    headers = {"Accept": "application/json, image/*"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except ToolError:
        raise
    except HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise ToolError("COMFYUI_HTTP_ERROR", f"ComfyUI returned HTTP {exc.code}: {detail[:1000]}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ToolError("COMFYUI_OFFLINE", f"Cannot reach local ComfyUI at {COMFYUI_HOST}:{port}: {exc}") from exc
    if len(data) > limit:
        raise ToolError("COMFYUI_RESPONSE_TOO_LARGE", f"ComfyUI response exceeds {limit} bytes.")
    return data


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ToolError("UNSUPPORTED_IMAGE", "ComfyUI output is not PNG, JPEG, GIF, or WebP.")
