from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .security import ToolError, Workspace


COMFYUI_HOST = "127.0.0.1"
COMFYUI_PORT = 8188
MAX_WORKFLOW_BYTES = 4 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_IMAGES = 4
MAX_OUTPUT_ARTIFACTS = 64
MAX_DYNAMIC_PREFLIGHT_CLASSES = 64
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"})
AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".mp3", ".opus", ".ogg", ".m4a", ".aac"})


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
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    port: int = COMFYUI_PORT,
    include_image_data: bool = True,
    cancel_token_path: str | None = None,
) -> dict[str, Any]:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 0 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ToolError("INVALID_ARGUMENT", f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS}; 0 disables automatic timeout.")
    if not isinstance(include_image_data, bool):
        raise ToolError("INVALID_ARGUMENT", "include_image_data must be boolean.")
    workflow = _load_workflow(workspace, workflow_path)
    _apply_overrides(workflow, overrides)
    _preflight_dynamic_inputs(workflow, port=port)
    if _cancel_requested(cancel_token_path):
        raise ToolError("COMFYUI_CANCELLED", "ComfyUI workflow cancellation was requested before prompt submission.")

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
    if _cancel_requested(cancel_token_path):
        cancel_dispatched = _cancel_prompt(prompt_id, port=port)
        raise ToolError(
            "COMFYUI_CANCELLED",
            "ComfyUI workflow cancellation was requested during prompt submission.",
            prompt_id=prompt_id,
            cancel_dispatched=cancel_dispatched,
        )
    node_errors = queued.get("node_errors") if isinstance(queued, dict) else None
    if isinstance(node_errors, dict) and node_errors:
        raise ToolError("COMFYUI_NODE_ERROR", "ComfyUI rejected one or more workflow nodes.", node_errors=node_errors)

    deadline = None if timeout_seconds == 0 else time.monotonic() + timeout_seconds
    history_entry: dict[str, Any] | None = None
    cancel_stop = threading.Event()
    cancel_attempted = threading.Event()
    cancel_dispatched_event = threading.Event()
    cancel_watcher: threading.Thread | None = None
    if cancel_token_path:
        cancel_watcher = threading.Thread(
            target=_watch_cancel_token,
            args=(cancel_token_path, prompt_id, port, cancel_stop, cancel_attempted, cancel_dispatched_event),
            name=f"folderbridge-comfyui-cancel-{prompt_id[:8]}",
            daemon=True,
        )
        cancel_watcher.start()
    try:
        while deadline is None or time.monotonic() < deadline:
            if _cancel_requested(cancel_token_path):
                if not cancel_attempted.is_set():
                    if _cancel_prompt(prompt_id, port=port):
                        cancel_dispatched_event.set()
                    cancel_attempted.set()
                raise ToolError(
                    "COMFYUI_CANCELLED",
                    "ComfyUI workflow cancellation was requested.",
                    prompt_id=prompt_id,
                    cancel_dispatched=cancel_dispatched_event.is_set(),
                )
            history = _json_request("GET", f"/history/{prompt_id}", port=port, timeout=10)
            if isinstance(history, dict):
                candidate = history.get(prompt_id)
                if isinstance(candidate, dict):
                    history_entry = candidate
                    break
            time.sleep(0.5)
    finally:
        cancel_stop.set()
        if cancel_watcher is not None:
            cancel_watcher.join(timeout=0.5)
    if history_entry is None:
        cancel_dispatched = _cancel_prompt(prompt_id, port=port)
        raise ToolError(
            "COMFYUI_TIMEOUT",
            f"ComfyUI workflow did not finish within {timeout_seconds} seconds.",
            prompt_id=prompt_id,
            cancel_dispatched=cancel_dispatched,
        )

    status = history_entry.get("status")
    if isinstance(status, dict) and status.get("status_str") == "error":
        raise ToolError("COMFYUI_EXECUTION_ERROR", "ComfyUI reported workflow execution failure.", status=status)

    descriptors = _output_artifact_descriptors(history_entry)
    storage_roots = _comfyui_storage_roots(port=port, workspace=workspace)
    artifact_descriptors = descriptors[:MAX_OUTPUT_ARTIFACTS]
    artifacts = [
        _artifact_metadata(descriptor, storage_roots=storage_roots, workspace=workspace, index=index)
        for index, descriptor in enumerate(artifact_descriptors, start=1)
    ]
    image_descriptors = [descriptor for descriptor in descriptors if descriptor["kind"] == "image"]
    save_root = _prepare_save_directory(workspace, save_directory) if save_directory else None
    should_fetch_images = include_image_data or save_root is not None
    selected = image_descriptors[:MAX_OUTPUT_IMAGES] if should_fetch_images else []
    rendered: list[dict[str, Any]] = []
    image_content: list[dict[str, str]] = []
    total_bytes = 0

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
        if include_image_data:
            image_content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type})

    metadata = {
        "online": True,
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "workflow_path": workflow_path,
        "prompt_id": prompt_id,
        "artifacts_found": len(descriptors),
        "artifacts_returned": len(artifacts),
        "artifacts_truncated": len(descriptors) > len(artifacts),
        "artifacts": artifacts,
        "images_found": len(image_descriptors),
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


def _preflight_dynamic_inputs(workflow: dict[str, Any], *, port: int) -> None:
    suspects: list[tuple[str, str, dict[str, Any]]] = []
    class_types: set[str] = set()
    for node_id, node in workflow.items():
        inputs = node.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        if not any(isinstance(value, dict) or "." in name for name, value in inputs.items()):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            continue
        suspects.append((node_id, class_type, inputs))
        class_types.add(class_type)
    if not suspects:
        return
    if len(class_types) > MAX_DYNAMIC_PREFLIGHT_CLASSES:
        raise ToolError(
            "INVALID_COMFYUI_WORKFLOW",
            f"Workflow uses more than {MAX_DYNAMIC_PREFLIGHT_CLASSES} node classes requiring dynamic-input preflight.",
        )

    schemas: dict[str, dict[str, Any]] = {}
    for class_type in sorted(class_types):
        response = _json_request(
            "GET",
            f"/object_info/{quote(class_type, safe='')}",
            port=port,
            timeout=5,
        )
        info = response.get(class_type) if isinstance(response, dict) else None
        if not isinstance(info, dict):
            raise ToolError(
                "INVALID_COMFYUI_WORKFLOW",
                f"ComfyUI did not return schema information for node class {class_type}.",
                class_type=class_type,
            )
        schemas[class_type] = info

    for node_id, class_type, inputs in suspects:
        schema_input = schemas[class_type].get("input")
        if not isinstance(schema_input, dict):
            continue
        for section_name in ("required", "optional"):
            section = schema_input.get(section_name)
            if not isinstance(section, dict):
                continue
            for input_name, descriptor in section.items():
                if not (
                    isinstance(input_name, str)
                    and isinstance(descriptor, list)
                    and len(descriptor) >= 2
                    and descriptor[0] == "COMFY_DYNAMICCOMBO_V3"
                ):
                    continue
                if input_name not in inputs:
                    if section_name == "required":
                        raise ToolError(
                            "INVALID_COMFYUI_WORKFLOW",
                            f"Node {node_id} ({class_type}) is missing required dynamic input {input_name}.",
                            node_id=node_id,
                            class_type=class_type,
                            input_name=input_name,
                        )
                    continue
                value = inputs[input_name]
                options_raw = descriptor[1].get("options") if isinstance(descriptor[1], dict) else None
                option_keys = [
                    option.get("key")
                    for option in options_raw
                    if isinstance(option, dict) and isinstance(option.get("key"), str)
                ] if isinstance(options_raw, list) else []
                if not isinstance(value, str) or (option_keys and value not in option_keys):
                    raise ToolError(
                        "INVALID_COMFYUI_WORKFLOW",
                        f"Node {node_id} ({class_type}) dynamic input {input_name} must be an option key string, not a nested object.",
                        node_id=node_id,
                        class_type=class_type,
                        input_name=input_name,
                        allowed_options=option_keys[:32],
                    )


def _watch_cancel_token(
    cancel_token_path: str,
    prompt_id: str,
    port: int,
    stop: threading.Event,
    attempted: threading.Event,
    dispatched: threading.Event,
) -> None:
    while not stop.wait(0.1):
        if not _cancel_requested(cancel_token_path):
            continue
        if not attempted.is_set():
            if _cancel_prompt(prompt_id, port=port):
                dispatched.set()
            attempted.set()
        return


def _cancel_requested(cancel_token_path: str | None) -> bool:
    if not cancel_token_path:
        return False
    try:
        path = Path(cancel_token_path)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _cancel_prompt(prompt_id: str, *, port: int) -> bool:
    encoded_prompt_id = quote(prompt_id, safe="")
    try:
        response = _json_request(
            "POST",
            f"/api/jobs/{encoded_prompt_id}/cancel",
            port=port,
            timeout=5,
        )
        return bool(isinstance(response, dict) and response.get("cancelled"))
    except ToolError:
        # Fail closed: legacy /interrupt is process-global and cannot guarantee
        # that only this FolderBridge-owned ComfyUI prompt would be stopped.
        return False


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


def _output_artifact_descriptors(history_entry: dict[str, Any]) -> list[dict[str, str]]:
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for output_key, values in node_output.items():
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                output_type = item.get("type", "output")
                if not all(isinstance(value, str) for value in (filename, subfolder, output_type)) or not filename:
                    continue
                _validate_artifact_reference(filename, subfolder, output_type)
                identity = (filename, subfolder, output_type)
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(
                    {
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": output_type,
                        "node_id": str(node_id),
                        "output_key": str(output_key),
                        "kind": _artifact_kind(filename, str(output_key)),
                    }
                )
    return result


def _artifact_kind(filename: str, output_key: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES or output_key == "audio":
        return "audio"
    return "file"


def _validate_artifact_reference(filename: str, subfolder: str, output_type: str) -> None:
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact filename.")
    if "\\" in subfolder:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact subfolder.")
    relative = PurePosixPath(subfolder)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact subfolder.")
    if output_type not in {"output", "input", "temp"}:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unknown artifact storage type.")


def _argv_option(argv: list[str], name: str) -> str | None:
    prefix = name + "="
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(prefix):
            return value[len(prefix):]
    return None


def _comfyui_storage_roots(*, port: int, workspace: Workspace) -> dict[str, Path]:
    try:
        stats = _json_request("GET", "/system_stats", port=port, timeout=3)
    except ToolError:
        return {}
    system = stats.get("system") if isinstance(stats, dict) else None
    argv = system.get("argv") if isinstance(system, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        return {}
    main_path = Path(argv[0])
    if not main_path.is_absolute():
        candidate = (workspace.root / main_path).resolve(strict=False)
        if not candidate.is_file():
            return {}
        main_path = candidate
    if main_path.suffix.lower() != ".py":
        return {}
    base = main_path.parent
    roots: dict[str, Path] = {}
    for output_type, option_name, default_name in (
        ("output", "--output-directory", "output"),
        ("input", "--input-directory", "input"),
        ("temp", "--temp-directory", "temp"),
    ):
        raw = _argv_option(argv, option_name)
        path = Path(raw) if raw else base / default_name
        if raw and not path.is_absolute():
            path = base / path
        roots[output_type] = path.resolve(strict=False)
    return roots


def _artifact_metadata(
    descriptor: dict[str, str],
    *,
    storage_roots: dict[str, Path],
    workspace: Workspace,
    index: int,
) -> dict[str, Any]:
    output_type = descriptor["type"]
    relative_parts = list(PurePosixPath(descriptor["subfolder"]).parts) if descriptor["subfolder"] else []
    reference = "/".join([output_type, *relative_parts, descriptor["filename"]])
    result: dict[str, Any] = {
        "index": index,
        "kind": descriptor["kind"],
        "source": descriptor,
        "comfyui_reference": reference,
        "path": None,
        "workspace_path": None,
        "size": None,
    }
    root = storage_roots.get(output_type)
    if root is None:
        return result
    candidate = root.joinpath(*relative_parts, descriptor["filename"]).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ToolError("COMFYUI_INVALID_RESPONSE", "ComfyUI artifact path escaped its declared storage root.") from exc
    result["path"] = str(candidate)
    try:
        result["workspace_path"] = candidate.relative_to(workspace.root).as_posix()
    except ValueError:
        pass
    try:
        if candidate.is_file():
            result["size"] = candidate.stat().st_size
    except OSError:
        pass
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
