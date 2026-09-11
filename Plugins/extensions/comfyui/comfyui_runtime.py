from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import struct
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from folderbridge_mcp.extension_api import ExtensionError


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
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".mypy_cache", ".next", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", ".vscode", "__pycache__", "build", "coverage",
    "dist", "node_modules", "target", "vendor",
})
SENSITIVE_NAMES = frozenset({
    ".api-config.json", ".env", ".netrc", ".npmrc", ".pypirc", "credentials",
    "credentials.json", "api-config.json", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
    "known_hosts",
})
SENSITIVE_SUFFIXES = frozenset({".jks", ".key", ".keystore", ".p12", ".pfx", ".pem"})
PROTECTED_CONFIG = ".folderbridge.json"


JOB_STATUSES = frozenset({"pending", "in_progress", "completed", "failed", "cancelled"})
JOB_SORT_FIELDS = frozenset({"created_at", "execution_duration"})
JOB_SORT_ORDERS = frozenset({"asc", "desc"})
MAX_JOB_LIST_ITEMS = 100
MAX_MODEL_LIST_ITEMS = 1000
MAX_NODE_CLASS_CHARS = 256
MAX_PROGRESS_NODE_CHARS = 128
MAX_PROGRESS_OBSERVE_SECONDS = 10.0
MAX_WS_MESSAGE_BYTES = 256 * 1024
DIRECTOR_CLASS_NAMES = frozenset({"MiniMaxH3Director"})
PRODUCTION_PROFILE_AUTO = "auto"
PRODUCTION_PROFILE_NONE = "none"
PRODUCTION_PROFILE_MINIMAX_H3_V2V_16GB_1344X768 = "minimax_h3_v2v_16gb_1344x768"
PRODUCTION_PROFILES = frozenset({
    PRODUCTION_PROFILE_AUTO,
    PRODUCTION_PROFILE_NONE,
    PRODUCTION_PROFILE_MINIMAX_H3_V2V_16GB_1344X768,
})
MINIMAX_H3_V2V_PACKED_ROW_BUDGET = 100_000
MINIMAX_H3_V2V_VALIDATED_BASELINE_FRAMES = 124
MINIMAX_H3_V2V_PROFILE_WIDTH = 1344
MINIMAX_H3_V2V_PROFILE_HEIGHT = 768
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _production_profile_reject(profile: str, message: str, **details: Any) -> None:
    raise ExtensionError(
        "COMFYUI_PRODUCTION_PROFILE_REJECTED",
        message,
        production_profile=profile,
        **details,
    )


def _minimax_task_key(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    normalized = raw.strip().lower()
    if normalized.startswith("rv2v"):
        return "rv2v"
    if normalized.startswith("v2v"):
        return "v2v"
    return ""


def _numeric_equals(raw: Any, expected: float) -> bool:
    return isinstance(raw, (int, float)) and not isinstance(raw, bool) and float(raw) == float(expected)


def _minimax_align_frame_count(frame_count: int) -> int:
    n = max(5, int(frame_count))
    remainder = (n - 5) % 17
    return n if remainder == 0 else n + (17 - remainder)


def _minimax_video_latent_t(frame_count: int) -> int:
    n = _minimax_align_frame_count(frame_count)
    return 2 if n <= 5 else ((n - 5) // 17) * 5 + 2


def _estimate_minimax_h3_v2v_packed_rows(frame_count: int, width: int, height: int) -> int:
    frame_rows = max(1, (max(32, int(width)) // 32) * (max(32, int(height)) // 32))
    return 2 * _minimax_video_latent_t(frame_count) * frame_rows


def _recommended_minimax_h3_v2v_max_frames(width: int, height: int) -> int:
    frame_rows = max(1, (max(32, int(width)) // 32) * (max(32, int(height)) // 32))
    max_latent_t = max(2, MINIMAX_H3_V2V_PACKED_ROW_BUDGET // (2 * frame_rows))
    cycles = max(0, (max_latent_t - 2) // 5)
    return 5 + 17 * cycles


def _linked_node(
    workflow: dict[str, Any],
    link: Any,
    *,
    profile: str,
    owner_node_id: str,
    input_name: str,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(link, list) or not link:
        _production_profile_reject(
            profile,
            f"Node {owner_node_id} input {input_name} must be linked for this production profile.",
            node_id=owner_node_id,
            input_name=input_name,
        )
    source_id = str(link[0])
    source = workflow.get(source_id)
    if not isinstance(source, dict):
        _production_profile_reject(
            profile,
            f"Node {owner_node_id} input {input_name} references an unknown source node.",
            node_id=owner_node_id,
            input_name=input_name,
            source_node_id=source_id,
        )
    return source_id, source


def _director_guard_default(*, port: int, profile: str) -> bool:
    response = _json_request("GET", "/object_info/MiniMaxH3Director", port=port, timeout=5)
    info = response.get("MiniMaxH3Director") if isinstance(response, dict) else None
    if not isinstance(info, dict):
        _production_profile_reject(
            profile,
            "Live ComfyUI does not expose MiniMaxH3Director node schema required by this production profile.",
        )
    schema_input = info.get("input")
    if not isinstance(schema_input, dict):
        _production_profile_reject(profile, "MiniMaxH3Director live node schema has no input contract.")
    for section_name in ("required", "optional"):
        section = schema_input.get(section_name)
        if not isinstance(section, dict):
            continue
        descriptor = section.get("guard_long_v2v_segments")
        if not isinstance(descriptor, list) or len(descriptor) < 2 or not isinstance(descriptor[1], dict):
            continue
        default = descriptor[1].get("default")
        if isinstance(default, bool):
            return default
    _production_profile_reject(
        profile,
        "Live MiniMaxH3Director lacks guard_long_v2v_segments; update the Director before using this production profile.",
    )


def _profile_segment_checks(director_inputs: dict[str, Any], *, profile: str) -> list[dict[str, Any]]:
    total_frames = director_inputs.get("total_frames")
    if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames < 5:
        _production_profile_reject(profile, "MiniMaxH3Director total_frames must be an integer >= 5.")
    global_task = _minimax_task_key(director_inputs.get("task_type"))
    timeline_raw = director_inputs.get("timeline_data")
    raw_segments: list[Any] | None = None
    if isinstance(timeline_raw, str) and timeline_raw.strip():
        try:
            timeline = json.loads(timeline_raw)
        except json.JSONDecodeError as exc:
            _production_profile_reject(profile, "MiniMaxH3Director timeline_data must be valid JSON.", error=str(exc))
        if not isinstance(timeline, dict):
            _production_profile_reject(profile, "MiniMaxH3Director timeline_data must decode to an object.")
        candidate_segments = timeline.get("segments")
        if candidate_segments is not None:
            if not isinstance(candidate_segments, list) or not candidate_segments:
                _production_profile_reject(profile, "MiniMaxH3Director timeline segments must be a non-empty list when present.")
            raw_segments = candidate_segments
    if raw_segments is None:
        raw_segments = [{"frameCount": total_frames, "taskType": director_inputs.get("task_type")}]

    checks: list[dict[str, Any]] = []
    safe_max = _recommended_minimax_h3_v2v_max_frames(
        MINIMAX_H3_V2V_PROFILE_WIDTH,
        MINIMAX_H3_V2V_PROFILE_HEIGHT,
    )
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            _production_profile_reject(profile, "MiniMaxH3Director timeline contains a non-object segment.", segment=index + 1)
        raw_frames = raw_segment.get("frameCount", raw_segment.get("length", total_frames))
        if not isinstance(raw_frames, int) or isinstance(raw_frames, bool) or raw_frames < 5:
            _production_profile_reject(
                profile,
                "MiniMaxH3Director segment frame count must be an integer >= 5.",
                segment=index + 1,
            )
        task = _minimax_task_key(raw_segment.get("taskType")) or global_task
        if task != "v2v":
            _production_profile_reject(
                profile,
                "This production profile is limited to MiniMax H3 V2V segments.",
                segment=index + 1,
                task=task or None,
            )
        rows = _estimate_minimax_h3_v2v_packed_rows(
            raw_frames,
            MINIMAX_H3_V2V_PROFILE_WIDTH,
            MINIMAX_H3_V2V_PROFILE_HEIGHT,
        )
        if rows > MINIMAX_H3_V2V_PACKED_ROW_BUDGET:
            _production_profile_reject(
                profile,
                "MiniMax H3 V2V segment exceeds the validated 16GB-class packed-row budget; split it before submission.",
                segment=index + 1,
                frames=raw_frames,
                packed_video_rows=rows,
                packed_video_row_budget=MINIMAX_H3_V2V_PACKED_ROW_BUDGET,
                recommended_max_frames=safe_max,
                conservative_baseline_frames=MINIMAX_H3_V2V_VALIDATED_BASELINE_FRAMES,
            )
        checks.append(
            {
                "segment": index + 1,
                "frames": raw_frames,
                "aligned_frames": _minimax_align_frame_count(raw_frames),
                "packed_video_rows": rows,
                "budget": MINIMAX_H3_V2V_PACKED_ROW_BUDGET,
            }
        )
    return checks


def _resolve_production_profile(workflow: dict[str, Any], requested: str | None) -> tuple[str, str | None]:
    normalized = PRODUCTION_PROFILE_AUTO if requested is None else requested
    if not isinstance(normalized, str) or normalized not in PRODUCTION_PROFILES:
        raise ExtensionError(
            "INVALID_ARGUMENT",
            "production_profile must be one of: " + ", ".join(sorted(PRODUCTION_PROFILES)) + ".",
        )
    if normalized == PRODUCTION_PROFILE_NONE:
        return normalized, None
    if normalized == PRODUCTION_PROFILE_MINIMAX_H3_V2V_16GB_1344X768:
        return normalized, normalized
    for node in workflow.values():
        if not isinstance(node, dict) or node.get("class_type") != "MiniMaxH3Director":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if (
            _minimax_task_key(inputs.get("task_type")) == "v2v"
            and inputs.get("width") == MINIMAX_H3_V2V_PROFILE_WIDTH
            and inputs.get("height") == MINIMAX_H3_V2V_PROFILE_HEIGHT
        ):
            return normalized, PRODUCTION_PROFILE_MINIMAX_H3_V2V_16GB_1344X768
    return normalized, None


def _validate_production_profile(
    workflow: dict[str, Any],
    requested: str | None,
    *,
    port: int,
) -> dict[str, Any]:
    requested_value, resolved = _resolve_production_profile(workflow, requested)
    if resolved is None:
        return {
            "requested": requested_value,
            "resolved": None,
            "validated": True,
            "reason": "explicitly_disabled" if requested_value == PRODUCTION_PROFILE_NONE else "no_matching_profile",
        }

    profile = resolved
    directors = [
        (str(node_id), node)
        for node_id, node in workflow.items()
        if isinstance(node, dict) and node.get("class_type") == "MiniMaxH3Director"
    ]
    if len(directors) != 1:
        _production_profile_reject(
            profile,
            "The MiniMax H3 V2V production profile requires exactly one MiniMaxH3Director node.",
            director_node_count=len(directors),
        )
    director_id, director = directors[0]
    inputs = director.get("inputs")
    if not isinstance(inputs, dict):
        _production_profile_reject(profile, "MiniMaxH3Director inputs must be an object.", node_id=director_id)

    expected_values: tuple[tuple[str, Any], ...] = (
        ("width", MINIMAX_H3_V2V_PROFILE_WIDTH),
        ("height", MINIMAX_H3_V2V_PROFILE_HEIGHT),
        ("ref_max_size", 1344),
        ("frame_rate", 24),
        ("cfg", 1),
        ("steps", 8),
        ("sampler", "dpmpp_2m"),
        ("scheduler", "simple"),
        ("shift_video", 12),
        ("shift_audio", 3),
        ("clear_vram_between_segments", True),
    )
    for input_name, expected in expected_values:
        actual = inputs.get(input_name)
        matches = _numeric_equals(actual, float(expected)) if isinstance(expected, (int, float)) and not isinstance(expected, bool) else actual == expected
        if not matches:
            _production_profile_reject(
                profile,
                f"MiniMaxH3Director input {input_name} does not match the validated production contract.",
                node_id=director_id,
                input_name=input_name,
                expected=expected,
                actual=actual,
            )
    if _minimax_task_key(inputs.get("task_type")) != "v2v":
        _production_profile_reject(profile, "MiniMaxH3Director task_type must be v2v for this profile.", node_id=director_id)

    attention_id, attention = _linked_node(
        workflow,
        inputs.get("model"),
        profile=profile,
        owner_node_id=director_id,
        input_name="model",
    )
    if attention.get("class_type") != "MiniMaxLowVRAMAttention":
        _production_profile_reject(
            profile,
            "Director model input must come from MiniMaxLowVRAMAttention.",
            source_node_id=attention_id,
            actual_class=attention.get("class_type"),
        )
    attention_inputs = attention.get("inputs")
    if not isinstance(attention_inputs, dict) or attention_inputs.get("head_chunks") != 4:
        _production_profile_reject(
            profile,
            "MiniMaxLowVRAMAttention must use head_chunks=4.",
            node_id=attention_id,
        )
    ffn_id, ffn = _linked_node(
        workflow,
        attention_inputs.get("model"),
        profile=profile,
        owner_node_id=attention_id,
        input_name="model",
    )
    if ffn.get("class_type") != "MiniMaxChunkFeedForward":
        _production_profile_reject(
            profile,
            "MiniMaxLowVRAMAttention model input must come from MiniMaxChunkFeedForward.",
            source_node_id=ffn_id,
            actual_class=ffn.get("class_type"),
        )
    ffn_inputs = ffn.get("inputs")
    if (
        not isinstance(ffn_inputs, dict)
        or ffn_inputs.get("chunks") != 2
        or ffn_inputs.get("seq_threshold") != 4096
    ):
        _production_profile_reject(
            profile,
            "MiniMaxChunkFeedForward must use chunks=2 and seq_threshold=4096.",
            node_id=ffn_id,
        )

    runtime = _compact_runtime_health(comfyui_status(port=port))
    if runtime.get("online") is not True or runtime.get("fast_disk") is not True:
        _production_profile_reject(
            profile,
            "The validated production profile requires an online ComfyUI runtime launched with --fast-disk.",
            runtime_online=runtime.get("online"),
            fast_disk=runtime.get("fast_disk"),
        )

    live_guard_default = _director_guard_default(port=port, profile=profile)
    if live_guard_default is not True:
        _production_profile_reject(
            profile,
            "Live MiniMaxH3Director guard_long_v2v_segments default must be true.",
            live_default=live_guard_default,
        )
    workflow_guard = inputs.get("guard_long_v2v_segments")
    if workflow_guard is not None and workflow_guard is not True:
        _production_profile_reject(
            profile,
            "Workflow explicitly disables guard_long_v2v_segments.",
            node_id=director_id,
            actual=workflow_guard,
        )

    segment_checks = _profile_segment_checks(inputs, profile=profile)
    return {
        "requested": requested_value,
        "resolved": profile,
        "validated": True,
        "auto_applied": requested_value == PRODUCTION_PROFILE_AUTO,
        "canvas": {"width": MINIMAX_H3_V2V_PROFILE_WIDTH, "height": MINIMAX_H3_V2V_PROFILE_HEIGHT},
        "conservative_baseline_frames": MINIMAX_H3_V2V_VALIDATED_BASELINE_FRAMES,
        "recommended_max_frames": _recommended_minimax_h3_v2v_max_frames(
            MINIMAX_H3_V2V_PROFILE_WIDTH,
            MINIMAX_H3_V2V_PROFILE_HEIGHT,
        ),
        "packed_video_row_budget": MINIMAX_H3_V2V_PACKED_ROW_BUDGET,
        "model_chain": {
            "chunk_feed_forward": {"node_id": ffn_id, "chunks": 2, "seq_threshold": 4096},
            "low_vram_attention": {"node_id": attention_id, "head_chunks": 4},
            "director": {"node_id": director_id},
        },
        "guard_long_v2v_segments": {
            "workflow_value": workflow_guard,
            "live_default": live_guard_default,
        },
        "runtime": {"fast_disk": True},
        "segments": segment_checks,
    }


def _validate_prompt_id(raw: str) -> str:
    if not isinstance(raw, str):
        raise ExtensionError("INVALID_ARGUMENT", "prompt_id must be a canonical lowercase UUID string.")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise ExtensionError("INVALID_ARGUMENT", "prompt_id must be a canonical lowercase UUID string.") from exc
    if str(parsed) != raw:
        raise ExtensionError("INVALID_ARGUMENT", "prompt_id must be a canonical lowercase UUID string.")
    return raw


def release_comfyui_memory(
    *,
    unload_models: bool = True,
    free_memory: bool = True,
    settle_seconds: float = 0.5,
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    if not isinstance(unload_models, bool) or not isinstance(free_memory, bool):
        raise ExtensionError("INVALID_ARGUMENT", "unload_models and free_memory must be boolean.")
    if not unload_models and not free_memory:
        raise ExtensionError("INVALID_ARGUMENT", "At least one of unload_models or free_memory must be true.")
    if not isinstance(settle_seconds, (int, float)) or isinstance(settle_seconds, bool) or not 0 <= float(settle_seconds) <= 10:
        raise ExtensionError("INVALID_ARGUMENT", "settle_seconds must be between 0 and 10.")

    before = comfyui_status(port=port)
    payload = {"unload_models": unload_models, "free_memory": free_memory}
    _request_bytes(
        "POST",
        "/free",
        port=port,
        timeout=5,
        limit=4096,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    if settle_seconds:
        time.sleep(float(settle_seconds))
    after = comfyui_status(port=port)
    return {
        "acknowledged": True,
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "requested": payload,
        "before": before,
        "after": after,
    }


def _safe_job_summary(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id", "status", "priority", "create_time", "execution_start_time", "execution_end_time",
        "execution_error", "outputs_count", "preview_output", "workflow_id",
    )
    return {key: raw[key] for key in allowed if key in raw}


def list_jobs(
    *,
    statuses: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    if statuses is not None:
        if not isinstance(statuses, list) or not statuses or len(statuses) > len(JOB_STATUSES):
            raise ExtensionError("INVALID_ARGUMENT", "statuses must be a non-empty bounded list when provided.")
        if any(not isinstance(item, str) or item not in JOB_STATUSES for item in statuses):
            raise ExtensionError("INVALID_ARGUMENT", f"statuses must use only: {', '.join(sorted(JOB_STATUSES))}.")
        if len(set(statuses)) != len(statuses):
            raise ExtensionError("INVALID_ARGUMENT", "statuses may not contain duplicates.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_JOB_LIST_ITEMS:
        raise ExtensionError("INVALID_ARGUMENT", f"limit must be between 1 and {MAX_JOB_LIST_ITEMS}.")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ExtensionError("INVALID_ARGUMENT", "offset must be a non-negative integer.")
    if sort_by not in JOB_SORT_FIELDS:
        raise ExtensionError("INVALID_ARGUMENT", f"sort_by must be one of: {', '.join(sorted(JOB_SORT_FIELDS))}.")
    if sort_order not in JOB_SORT_ORDERS:
        raise ExtensionError("INVALID_ARGUMENT", f"sort_order must be one of: {', '.join(sorted(JOB_SORT_ORDERS))}.")

    query: dict[str, str | int] = {
        "limit": limit,
        "offset": offset,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    if statuses is not None:
        query["status"] = ",".join(statuses)
    response = _json_request("GET", f"/api/jobs?{urlencode(query)}", port=port, timeout=5)
    if not isinstance(response, dict) or not isinstance(response.get("jobs"), list):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI jobs API returned an invalid response.")
    jobs = [_safe_job_summary(item) for item in response["jobs"] if isinstance(item, dict)]
    pagination = response.get("pagination") if isinstance(response.get("pagination"), dict) else {}
    return {
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "jobs": jobs[:limit],
        "pagination": pagination,
    }


def get_job_status(prompt_id: str, *, port: int = COMFYUI_PORT) -> dict[str, Any]:
    prompt_id = _validate_prompt_id(prompt_id)
    try:
        response = _json_request("GET", f"/api/jobs/{quote(prompt_id, safe='')}", port=port, timeout=5)
    except ExtensionError as exc:
        if exc.code == "COMFYUI_HTTP_ERROR" and exc.details.get("status") == 404:
            raise ExtensionError("COMFYUI_JOB_NOT_FOUND", "ComfyUI does not know this prompt_id.", prompt_id=prompt_id) from exc
        raise
    if not isinstance(response, dict) or response.get("id") != prompt_id:
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI job status response is invalid.", prompt_id=prompt_id)
    result = _safe_job_summary(response)
    outputs = response.get("outputs")
    descriptors = _output_artifact_descriptors({"outputs": outputs}) if isinstance(outputs, dict) else []
    result["artifacts_found"] = len(descriptors)
    result["artifacts"] = [
        {
            "filename": item["filename"],
            "subfolder": item["subfolder"],
            "type": item["type"],
            "kind": item["kind"],
            "node_id": item["node_id"],
            "output_key": item["output_key"],
        }
        for item in descriptors[:MAX_OUTPUT_ARTIFACTS]
    ]
    result["artifacts_truncated"] = len(descriptors) > MAX_OUTPUT_ARTIFACTS
    result["endpoint"] = f"http://{COMFYUI_HOST}:{port}"
    return result


def _headroom_summary(free_value: Any, total_value: Any) -> dict[str, Any]:
    if not isinstance(free_value, int) or isinstance(free_value, bool) or free_value < 0:
        return {"free": free_value, "total": total_value, "free_ratio": None}
    if not isinstance(total_value, int) or isinstance(total_value, bool) or total_value <= 0:
        return {"free": free_value, "total": total_value, "free_ratio": None}
    ratio = max(0.0, min(1.0, free_value / total_value))
    return {
        "free": free_value,
        "total": total_value,
        "free_ratio": round(ratio, 4),
    }


def _compact_runtime_health(status: dict[str, Any]) -> dict[str, Any]:
    if status.get("online") is not True:
        return {
            "online": False,
            "endpoint": status.get("endpoint"),
            "detail": status.get("detail"),
        }
    stats = status.get("system_stats")
    system = stats.get("system") if isinstance(stats, dict) else None
    devices = stats.get("devices") if isinstance(stats, dict) else None
    system = system if isinstance(system, dict) else {}
    argv = system.get("argv")
    argv_list = [item for item in argv if isinstance(item, str)] if isinstance(argv, list) else []
    compact_devices: list[dict[str, Any]] = []
    if isinstance(devices, list):
        for item in devices[:8]:
            if not isinstance(item, dict):
                continue
            compact_devices.append(
                {
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "index": item.get("index"),
                    "vram": _headroom_summary(item.get("vram_free"), item.get("vram_total")),
                    "torch_vram_free": item.get("torch_vram_free"),
                    "torch_vram_total": item.get("torch_vram_total"),
                }
            )
    return {
        "online": True,
        "endpoint": status.get("endpoint"),
        "comfyui_version": system.get("comfyui_version"),
        "os": system.get("os"),
        "ram": _headroom_summary(system.get("ram_free"), system.get("ram_total")),
        "devices": compact_devices,
        "argv": argv_list[:64],
        "fast_disk": "--fast-disk" in argv_list,
    }


def _queue_health_snapshot(prompt_id: str, *, port: int) -> dict[str, Any]:
    queue = _json_request("GET", "/queue", port=port, timeout=5)
    if not isinstance(queue, dict):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI queue response is invalid.")
    running = queue.get("queue_running")
    pending = queue.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI queue response is invalid.")

    def find_exact(items: list[Any]) -> list[Any] | None:
        for item in items:
            if isinstance(item, list) and len(item) >= 5 and item[1] == prompt_id:
                return item
        return None

    bound = find_exact(running)
    queue_state = "running" if bound is not None else None
    if bound is None:
        bound = find_exact(pending)
        if bound is not None:
            queue_state = "pending"

    result: dict[str, Any] = {
        "exact_prompt_state": queue_state,
        "running_count": len(running),
        "pending_count": len(pending),
        "progress_capability": {
            "provider": None,
            "supported": False,
            "node_ids": [],
            "reason": "exact_prompt_not_in_queue" if bound is None else "unsupported_workflow",
        },
    }
    if bound is None:
        return result

    prompt = bound[2]
    extra_data = bound[3]
    if not isinstance(prompt, dict) or not isinstance(extra_data, dict):
        result["progress_capability"]["reason"] = "queue_binding_shape_unavailable"
        return result
    client_id = extra_data.get("client_id")
    private_client = isinstance(client_id, str) and client_id.startswith("folderbridge-")
    director_nodes = [
        str(candidate_id)
        for candidate_id, candidate in prompt.items()
        if isinstance(candidate, dict) and candidate.get("class_type") in DIRECTOR_CLASS_NAMES
    ]
    if not private_client:
        result["progress_capability"]["reason"] = "non_folderbridge_client_session"
    elif not director_nodes:
        result["progress_capability"]["reason"] = "unsupported_workflow"
    else:
        result["progress_capability"] = {
            "provider": "minimax_h3_director",
            "supported": True,
            "node_ids": director_nodes,
            "reason": None,
        }
    return result


def _health_assessment(job: dict[str, Any], queue: dict[str, Any] | None, runtime: dict[str, Any]) -> dict[str, Any]:
    status = job.get("status")
    if runtime.get("online") is not True:
        return {
            "state": "runtime_offline",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "comfyui_runtime_offline",
        }
    if status == "completed":
        return {
            "state": "terminal_completed",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "comfyui_job_completed",
        }
    if status in {"failed", "cancelled"}:
        return {
            "state": f"terminal_{status}",
            "confidence": "high",
            "stall_suspected": False,
            "reason": f"comfyui_job_{status}",
        }
    if status == "pending":
        return {
            "state": "pending",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "prompt_not_executing_yet",
        }
    if status != "in_progress":
        return {
            "state": "unknown",
            "confidence": "low",
            "stall_suspected": False,
            "reason": "unrecognized_job_state",
        }
    if isinstance(queue, dict) and queue.get("exact_prompt_state") == "running":
        return {
            "state": "running_exact_queue_bound",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "job_and_current_queue_agree_prompt_is_running",
        }
    return {
        "state": "running_status_only",
        "confidence": "low",
        "stall_suspected": False,
        "reason": "job_reports_running_without_exact_current_queue_binding",
    }


def _unknown_prompt_health(runtime: dict[str, Any], prompt_id: str, *, started_at: float) -> dict[str, Any]:
    finished_at = time.time()
    return {
        "health_schema_version": 1,
        "endpoint": runtime.get("endpoint"),
        "prompt_id": prompt_id,
        "job": None,
        "runtime": runtime,
        "queue": None,
        "consistency": {
            "snapshot_started_at": started_at,
            "snapshot_finished_at": finished_at,
            "initial_status": "unknown",
            "final_status": "unknown",
            "state_changed_during_snapshot": None,
            "queue_snapshot_attempted": False,
            "queue_snapshot_ok": None,
            "final_refresh_attempted": False,
            "final_refresh_ok": None,
        },
        "assessment": {
            "state": "unknown_prompt",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "comfyui_job_not_found",
        },
        "single_snapshot_stall_rule": "never_infer_stall_without_longitudinal_evidence",
    }


def get_health_check(prompt_id: str, *, port: int = COMFYUI_PORT) -> dict[str, Any]:
    """Return one compact, cross-project, WebSocket-free health snapshot.

    Generic health comes only from standard ComfyUI runtime/job/queue facts. Exact
    semantic progress remains the separate explicit `progress` action. One health
    snapshot never declares a stall and never mutates queue, model, memory, prompt,
    or FolderBridge host state.
    """
    prompt_id = _validate_prompt_id(prompt_id)
    started_at = time.time()
    runtime = _compact_runtime_health(comfyui_status(port=port))
    if runtime.get("online") is not True:
        finished_at = time.time()
        return {
            "health_schema_version": 1,
            "endpoint": runtime.get("endpoint"),
            "prompt_id": prompt_id,
            "job": None,
            "runtime": runtime,
            "queue": None,
            "consistency": {
                "snapshot_started_at": started_at,
                "snapshot_finished_at": finished_at,
                "initial_status": "unknown",
                "final_status": "unknown",
                "state_changed_during_snapshot": None,
                "queue_snapshot_attempted": False,
                "queue_snapshot_ok": None,
                "final_refresh_attempted": False,
                "final_refresh_ok": None,
            },
            "assessment": _health_assessment({"status": "unknown"}, None, runtime),
            "single_snapshot_stall_rule": "never_infer_stall_without_longitudinal_evidence",
        }

    try:
        initial_job = get_job_status(prompt_id, port=port)
    except ExtensionError as exc:
        if exc.code == "COMFYUI_JOB_NOT_FOUND":
            return _unknown_prompt_health(runtime, prompt_id, started_at=started_at)
        raise

    queue_snapshot = None
    queue_snapshot_attempted = False
    queue_snapshot_ok = None
    queue_error = None
    final_job = initial_job
    final_refresh_attempted = False
    final_refresh_ok = None
    refresh_error = None
    if initial_job.get("status") in {"pending", "in_progress"}:
        queue_snapshot_attempted = True
        final_refresh_attempted = True
        try:
            queue_snapshot = _queue_health_snapshot(prompt_id, port=port)
            queue_snapshot_ok = True
        except ExtensionError as exc:
            queue_snapshot_ok = False
            queue_error = {"code": exc.code, "message": exc.message}
        try:
            final_job = get_job_status(prompt_id, port=port)
            final_refresh_ok = True
        except ExtensionError as exc:
            final_refresh_ok = False
            refresh_error = {"code": exc.code, "message": exc.message}
            final_job = None

    finished_at = time.time()
    initial_status = initial_job.get("status")
    final_status = final_job.get("status") if isinstance(final_job, dict) else None
    consistency = {
        "snapshot_started_at": started_at,
        "snapshot_finished_at": finished_at,
        "initial_status": initial_status,
        "final_status": final_status,
        "state_changed_during_snapshot": None if final_status is None else final_status != initial_status,
        "queue_snapshot_attempted": queue_snapshot_attempted,
        "queue_snapshot_ok": queue_snapshot_ok,
        "final_refresh_attempted": final_refresh_attempted,
        "final_refresh_ok": final_refresh_ok,
    }
    if queue_error is not None:
        consistency["queue_error"] = queue_error
    if refresh_error is not None:
        consistency["refresh_error"] = refresh_error
        assessment = {
            "state": "snapshot_incomplete",
            "confidence": "high",
            "stall_suspected": False,
            "reason": "final_job_refresh_failed",
        }
    else:
        assessment = _health_assessment(final_job, queue_snapshot, runtime)  # type: ignore[arg-type]

    return {
        "health_schema_version": 1,
        "endpoint": runtime.get("endpoint"),
        "prompt_id": prompt_id,
        "job": final_job,
        "runtime": runtime,
        "queue": queue_snapshot,
        "consistency": consistency,
        "assessment": assessment,
        "single_snapshot_stall_rule": "never_infer_stall_without_longitudinal_evidence",
    }


def get_progress(
    prompt_id: str,
    *,
    node_id: str | None = None,
    observe_seconds: float = 2.0,
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    """Observe bounded, read-only Director progress for one exact running prompt.

    Binding is established from ComfyUI's current queue tuple: prompt_id -> prompt ->
    Director node + the prompt's unique client_id. The websocket is then attached to
    that exact client_id for a short observation window. No queue, prompt, history,
    model, or runtime state is mutated. If no Director event is seen, the result is
    explicit unavailable rather than an invented percentage.
    """
    prompt_id = _validate_prompt_id(prompt_id)
    if node_id is not None:
        if not isinstance(node_id, str) or not node_id or len(node_id) > MAX_PROGRESS_NODE_CHARS or "\x00" in node_id:
            raise ExtensionError("INVALID_ARGUMENT", f"node_id must be a non-empty string up to {MAX_PROGRESS_NODE_CHARS} characters.")
    if not isinstance(observe_seconds, (int, float)) or isinstance(observe_seconds, bool):
        raise ExtensionError("INVALID_ARGUMENT", "observe_seconds must be numeric.")
    observe_seconds = float(observe_seconds)
    if not 0.1 <= observe_seconds <= MAX_PROGRESS_OBSERVE_SECONDS:
        raise ExtensionError(
            "INVALID_ARGUMENT",
            f"observe_seconds must be between 0.1 and {MAX_PROGRESS_OBSERVE_SECONDS:g} seconds.",
        )

    endpoint = f"http://{COMFYUI_HOST}:{port}"
    try:
        raw_job = _json_request("GET", f"/api/jobs/{quote(prompt_id, safe='')}", port=port, timeout=5)
    except ExtensionError as exc:
        if exc.code == "COMFYUI_HTTP_ERROR" and exc.details.get("status") == 404:
            return {
                "endpoint": endpoint,
                "prompt_id": prompt_id,
                "node_id": node_id,
                "status": "unknown",
                "available": False,
                "reason": "unknown_prompt",
            }
        raise
    if not isinstance(raw_job, dict) or raw_job.get("id") != prompt_id:
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI job status response is invalid.", prompt_id=prompt_id)
    status = raw_job.get("status")
    if status != "in_progress":
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": node_id,
            "status": status,
            "available": False,
            "reason": "prompt_not_in_progress",
        }

    queue = _json_request("GET", "/queue", port=port, timeout=5)
    running = queue.get("queue_running") if isinstance(queue, dict) else None
    if not isinstance(running, list):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI queue response is invalid.")
    bound_item = None
    for item in running:
        if isinstance(item, list) and len(item) >= 5 and item[1] == prompt_id:
            bound_item = item
            break
    if bound_item is None:
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": node_id,
            "status": status,
            "available": False,
            "reason": "running_prompt_binding_unavailable",
        }

    prompt = bound_item[2]
    extra_data = bound_item[3]
    if not isinstance(prompt, dict) or not isinstance(extra_data, dict):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "Running ComfyUI queue item has an invalid binding shape.")
    client_id = extra_data.get("client_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > 256 or "\x00" in client_id:
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": node_id,
            "status": status,
            "available": False,
            "reason": "prompt_client_binding_unavailable",
        }
    # Reusing a WebUI-owned clientId would replace that client's active socket in
    # ComfyUI. FolderBridge workflow submissions intentionally mint a private
    # `folderbridge-*` clientId and never open a websocket for it, so observation
    # of those prompts is side-effect free. Everything else fails closed.
    if not client_id.startswith("folderbridge-"):
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": node_id,
            "status": status,
            "available": False,
            "reason": "non_folderbridge_client_session_not_observed",
        }

    director_nodes = [
        str(candidate_id)
        for candidate_id, candidate in prompt.items()
        if isinstance(candidate, dict) and candidate.get("class_type") in DIRECTOR_CLASS_NAMES
    ]
    if node_id is not None:
        if node_id not in director_nodes:
            return {
                "endpoint": endpoint,
                "prompt_id": prompt_id,
                "node_id": node_id,
                "status": status,
                "available": False,
                "reason": "node_not_bound_to_prompt_director",
                "director_node_count": len(director_nodes),
            }
        selected_node = node_id
    elif len(director_nodes) == 1:
        selected_node = director_nodes[0]
    elif not director_nodes:
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": None,
            "status": status,
            "available": False,
            "reason": "no_director_node",
        }
    else:
        return {
            "endpoint": endpoint,
            "prompt_id": prompt_id,
            "node_id": None,
            "status": status,
            "available": False,
            "reason": "multiple_director_nodes_require_node_id",
            "director_node_count": len(director_nodes),
        }

    observation_started_at = time.time()
    events = _observe_progress_events(
        client_id,
        prompt_id=prompt_id,
        node_id=selected_node,
        observe_seconds=observe_seconds,
        port=port,
    )
    observation_finished_at = time.time()

    director_events = events["director_progress"]
    preview_events = events["director_preview"]
    node_events = events["node_progress"]
    executing_events = events["executing"]
    latest_director = director_events[-1] if director_events else None
    latest_preview = preview_events[-1] if preview_events else None
    latest_node = node_events[-1] if node_events else None
    latest_any = None
    for candidate in (latest_director, latest_preview, latest_node):
        if candidate is not None and (latest_any is None or candidate["observed_at"] > latest_any["observed_at"]):
            latest_any = candidate

    result: dict[str, Any] = {
        "endpoint": endpoint,
        "prompt_id": prompt_id,
        "node_id": selected_node,
        "status": status,
        "available": latest_director is not None,
        "reason": None if latest_director is not None else "director_phase_event_not_observed",
        "binding": "exact_running_prompt_client_and_director_node",
        "observation_seconds": round(observation_finished_at - observation_started_at, 3),
        "observation_started_at": observation_started_at,
        "observation_finished_at": observation_finished_at,
        "director_updates_observed": len(director_events),
        "preview_updates_observed": len(preview_events),
        "node_updates_observed": len(node_events),
        "executing_updates_observed": len(executing_events),
        "executing_node": executing_events[-1]["data"].get("node") if executing_events else None,
        "progress_changed_during_observation": _progress_changed(director_events, preview_events, node_events),
    }
    if latest_director is not None:
        payload = latest_director["data"]
        for key in (
            "phase", "phase_label", "phase_value", "phase_max", "overall_value", "overall_max",
            "segment", "segment_total", "timeline_segment", "timeline_segment_total", "frames_label", "task_key",
        ):
            if key in payload:
                result[key] = payload[key]
    if latest_preview is not None:
        preview = latest_preview["data"]
        if preview.get("live") is True and isinstance(preview.get("step"), int) and isinstance(preview.get("total_steps"), int):
            result["step"] = preview["step"]
            result["total_steps"] = preview["total_steps"]
    if latest_node is not None:
        node_payload = latest_node["data"]
        result["node_state"] = node_payload.get("state")
        result["node_value"] = node_payload.get("value")
        result["node_max"] = node_payload.get("max")
    if latest_any is not None:
        result["last_update"] = latest_any["observed_at"]
        result["age_seconds"] = max(0.0, observation_finished_at - latest_any["observed_at"])
    else:
        result["last_update"] = None
        result["age_seconds"] = None
    return result


def _progress_changed(
    director_events: list[dict[str, Any]],
    preview_events: list[dict[str, Any]],
    node_events: list[dict[str, Any]],
) -> bool:
    def signatures(events: list[dict[str, Any]], keys: tuple[str, ...]) -> set[tuple[Any, ...]]:
        return {tuple(event["data"].get(key) for key in keys) for event in events}

    if len(signatures(director_events, ("phase", "phase_value", "overall_value"))) > 1:
        return True
    if len(signatures(preview_events, ("step", "total_steps"))) > 1:
        return True
    if len(signatures(node_events, ("state", "value", "max"))) > 1:
        return True
    return False


def _observe_progress_events(
    client_id: str,
    *,
    prompt_id: str,
    node_id: str,
    observe_seconds: float,
    port: int,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "director_progress": [],
        "director_preview": [],
        "node_progress": [],
        "executing": [],
    }
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    path = f"/ws?clientId={quote(client_id, safe='')}"
    deadline = time.monotonic() + observe_seconds
    try:
        sock = socket.create_connection((COMFYUI_HOST, port), timeout=min(3.0, observe_seconds))
    except OSError as exc:
        raise ExtensionError("COMFYUI_OFFLINE", f"Cannot reach local ComfyUI websocket at {COMFYUI_HOST}:{port}: {exc}") from exc
    try:
        sock.settimeout(min(1.0, observe_seconds))
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {COMFYUI_HOST}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = _recv_until(sock, b"\r\n\r\n", 16 * 1024, deadline)
        header_blob, _, leftover = response.partition(b"\r\n\r\n")
        header_lines = header_blob.decode("latin-1").split("\r\n")
        if not header_lines or " 101 " not in f" {header_lines[0]} ":
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI websocket upgrade was rejected.")
        headers: dict[str, str] = {}
        for line in header_lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != expected_accept:
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI websocket handshake validation failed.")

        buffer = bytearray(leftover)
        while time.monotonic() < deadline:
            try:
                opcode, payload = _ws_read_frame(sock, buffer, deadline)
            except TimeoutError:
                continue
            if opcode == 8:
                break
            if opcode != 1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            event_type = message.get("type")
            data = message.get("data")
            if not isinstance(data, dict):
                continue
            observed_at = time.time()
            if event_type == "executing" and str(data.get("node")) == node_id:
                result["executing"].append({"observed_at": observed_at, "data": data})
            elif event_type == "minimax_director_progress" and str(data.get("node_id")) == node_id:
                result["director_progress"].append({"observed_at": observed_at, "data": data})
            elif event_type == "minimax_director_preview" and str(data.get("node_id")) == node_id:
                result["director_preview"].append({"observed_at": observed_at, "data": data})
            elif event_type == "progress_state" and data.get("prompt_id") == prompt_id:
                nodes = data.get("nodes")
                current = nodes.get(node_id) if isinstance(nodes, dict) else None
                if isinstance(current, dict):
                    result["node_progress"].append({"observed_at": observed_at, "data": current})
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return result


def _recv_until(sock: socket.socket, marker: bytes, limit: int, deadline: float) -> bytes:
    data = bytearray()
    while marker not in data:
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            chunk = sock.recv(min(4096, limit - len(data)))
        except socket.timeout as exc:
            raise TimeoutError from exc
        if not chunk:
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI websocket closed during handshake.")
        data.extend(chunk)
        if len(data) >= limit and marker not in data:
            raise ExtensionError("COMFYUI_RESPONSE_TOO_LARGE", "ComfyUI websocket handshake exceeded the bounded limit.")
    return bytes(data)


def _ws_read_frame(sock: socket.socket, buffer: bytearray, deadline: float) -> tuple[int, bytes]:
    header = _ws_read_exact(sock, buffer, 2, deadline)
    first, second = header[0], header[1]
    if not (first & 0x80):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "Fragmented ComfyUI websocket frames are unsupported.")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _ws_read_exact(sock, buffer, 2, deadline))[0]
    elif length == 127:
        length = struct.unpack("!Q", _ws_read_exact(sock, buffer, 8, deadline))[0]
    if length > MAX_WS_MESSAGE_BYTES:
        raise ExtensionError("COMFYUI_RESPONSE_TOO_LARGE", f"ComfyUI websocket message exceeds {MAX_WS_MESSAGE_BYTES} bytes.")
    mask = _ws_read_exact(sock, buffer, 4, deadline) if masked else None
    payload = _ws_read_exact(sock, buffer, length, deadline)
    if mask is not None:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _ws_read_exact(sock: socket.socket, buffer: bytearray, size: int, deadline: float) -> bytes:
    while len(buffer) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError
        remaining = max(0.01, min(1.0, deadline - time.monotonic()))
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(max(1, min(65536, size - len(buffer))))
        except socket.timeout as exc:
            raise TimeoutError from exc
        if not chunk:
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI websocket closed unexpectedly.")
        buffer.extend(chunk)
    value = bytes(buffer[:size])
    del buffer[:size]
    return value


def cancel_prompt(prompt_id: str, *, port: int = COMFYUI_PORT) -> dict[str, Any]:
    prompt_id = _validate_prompt_id(prompt_id)
    try:
        response = _json_request(
            "POST",
            f"/api/jobs/{quote(prompt_id, safe='')}/cancel",
            port=port,
            timeout=5,
        )
    except ExtensionError as exc:
        if exc.code == "COMFYUI_HTTP_ERROR" and exc.details.get("status") == 404:
            raise ExtensionError(
                "COMFYUI_TARGETED_CANCEL_UNAVAILABLE",
                "This ComfyUI runtime does not expose targeted prompt cancellation.",
                prompt_id=prompt_id,
            ) from exc
        raise
    cancelled = response.get("cancelled") if isinstance(response, dict) else None
    if not isinstance(cancelled, bool):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI targeted cancel returned an invalid response.")
    return {
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "prompt_id": prompt_id,
        "cancelled": cancelled,
    }


def get_node_info(class_name: str, *, port: int = COMFYUI_PORT) -> dict[str, Any]:
    if not isinstance(class_name, str) or not class_name.strip() or len(class_name) > MAX_NODE_CLASS_CHARS or "\x00" in class_name:
        raise ExtensionError("INVALID_ARGUMENT", f"class_name must be a non-empty string up to {MAX_NODE_CLASS_CHARS} characters.")
    class_name = class_name.strip()
    response = _json_request("GET", f"/object_info/{quote(class_name, safe='')}", port=port, timeout=5)
    info = response.get(class_name) if isinstance(response, dict) else None
    if not isinstance(info, dict):
        raise ExtensionError("COMFYUI_NODE_NOT_FOUND", "ComfyUI node class was not found.", class_name=class_name)
    return {
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "class_name": class_name,
        "info": info,
    }


def list_models(
    *,
    folder: str | None = None,
    contains: str | None = None,
    max_items: int = 200,
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    if folder is not None and (not isinstance(folder, str) or not folder.strip() or len(folder) > 256 or "\x00" in folder):
        raise ExtensionError("INVALID_ARGUMENT", "folder must be a non-empty bounded string when provided.")
    if contains is not None and (not isinstance(contains, str) or len(contains) > 256 or "\x00" in contains):
        raise ExtensionError("INVALID_ARGUMENT", "contains must be a bounded string when provided.")
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= MAX_MODEL_LIST_ITEMS:
        raise ExtensionError("INVALID_ARGUMENT", f"max_items must be between 1 and {MAX_MODEL_LIST_ITEMS}.")

    if folder is None:
        response = _json_request("GET", "/models", port=port, timeout=5)
        if not isinstance(response, list) or not all(isinstance(item, str) for item in response):
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI model-folder response is invalid.")
        values = response
        key = "folders"
    else:
        folder = folder.strip()
        try:
            response = _json_request("GET", f"/models/{quote(folder, safe='')}", port=port, timeout=5)
        except ExtensionError as exc:
            if exc.code == "COMFYUI_HTTP_ERROR" and exc.details.get("status") == 404:
                raise ExtensionError("COMFYUI_MODEL_FOLDER_NOT_FOUND", "ComfyUI model folder was not found.", folder=folder) from exc
            raise
        if not isinstance(response, list) or not all(isinstance(item, str) for item in response):
            raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI model list response is invalid.", folder=folder)
        values = response
        key = "models"

    if contains:
        needle = contains.casefold()
        values = [item for item in values if needle in item.casefold()]
    total = len(values)
    return {
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "folder": folder,
        key: values[:max_items],
        "total": total,
        "truncated": total > max_items,
    }


def get_features(*, port: int = COMFYUI_PORT) -> dict[str, Any]:
    response = _json_request("GET", "/features", port=port, timeout=5)
    if not isinstance(response, dict):
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI features response is invalid.")
    return {
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "features": response,
    }


def _is_transient_history_error(exc: ExtensionError) -> bool:
    if exc.code == "COMFYUI_OFFLINE":
        return True
    if exc.code == "COMFYUI_HTTP_ERROR":
        status = exc.details.get("status")
        return isinstance(status, int) and (status == 408 or status == 429 or status >= 500)
    return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ExtensionError("COMFYUI_REDIRECT_DENIED", "ComfyUI loopback requests may not redirect.")


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


class WorkspaceView:
    def __init__(self, root: Path) -> None:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ExtensionError("WORKSPACE_REQUIRED", f"Workspace is unavailable: {exc}") from exc
        if not resolved.is_dir():
            raise ExtensionError("WORKSPACE_REQUIRED", "Workspace root is not a directory.")
        self.root = resolved

    def resolve(self, raw: str, *, for_write: bool = False, allow_directory: bool = False) -> Path:
        if not isinstance(raw, str):
            raise ExtensionError("INVALID_PATH", "path must be a string")
        if "\x00" in raw:
            raise ExtensionError("INVALID_PATH", "path cannot contain NUL")
        path = Path(raw or ".")
        if path.is_absolute() or path.drive or ".." in path.parts:
            raise ExtensionError("PATH_OUTSIDE_WORKSPACE", "Use a relative path without '..'.", path=raw)
        candidate = self.root.joinpath(path)
        self._reject_linked_components(candidate)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise ExtensionError("PATH_OUTSIDE_WORKSPACE", "Path escapes the workspace.", path=raw) from exc
        relative = resolved.relative_to(self.root)
        self._check_policy(relative, for_write=for_write)
        if resolved.exists() and resolved.is_dir() and not allow_directory:
            raise ExtensionError("NOT_A_FILE", "Expected a file path.", path=raw)
        return resolved

    def _reject_linked_components(self, candidate: Path) -> None:
        current = self.root
        try:
            relative_parts = candidate.relative_to(self.root).parts
        except ValueError as exc:
            raise ExtensionError("PATH_OUTSIDE_WORKSPACE", "Path escapes the workspace.") from exc
        for part in relative_parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                break
            if current.is_symlink() or _is_reparse_point(current):
                raise ExtensionError("LINK_DENIED", "Symlinks and reparse points are not accessible.", path=current.name)

    def _check_policy(self, relative: Path, *, for_write: bool) -> None:
        lowered = [part.lower() for part in relative.parts]
        if any(part in IGNORED_DIRS for part in lowered):
            raise ExtensionError("IGNORED_PATH", "Generated, dependency, and VCS directories are not exposed.")
        if relative.name:
            name = relative.name.lower()
            if name in SENSITIVE_NAMES or name.startswith(".env.") or relative.suffix.lower() in SENSITIVE_SUFFIXES:
                raise ExtensionError("SENSITIVE_PATH", "Credential-like files are not exposed.", path=relative.as_posix())
            if for_write and name == PROTECTED_CONFIG:
                raise ExtensionError("PROTECTED_CONFIG", f"{PROTECTED_CONFIG} cannot be changed through MCP.")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def comfyui_status(*, port: int = COMFYUI_PORT) -> dict[str, Any]:
    try:
        stats = _json_request("GET", "/system_stats", port=port, timeout=3)
    except ExtensionError as exc:
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


def preflight_workflow(
    workspace_root: Path,
    workflow_path: str,
    *,
    overrides: dict[str, Any] | None = None,
    production_profile: str | None = PRODUCTION_PROFILE_AUTO,
    port: int = COMFYUI_PORT,
) -> dict[str, Any]:
    workspace = WorkspaceView(workspace_root)
    workflow = _load_workflow(workspace, workflow_path)
    _apply_overrides(workflow, overrides)
    _preflight_dynamic_inputs(workflow, port=port)
    profile_validation = _validate_production_profile(workflow, production_profile, port=port)
    return {
        "ready": True,
        "endpoint": f"http://{COMFYUI_HOST}:{port}",
        "workflow_path": workflow_path,
        "node_count": len(workflow),
        "production_profile": profile_validation,
    }


def run_workflow(
    workspace_root: Path,
    workflow_path: str,
    *,
    overrides: dict[str, Any] | None = None,
    save_directory: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    production_profile: str | None = PRODUCTION_PROFILE_AUTO,
    port: int = COMFYUI_PORT,
    include_image_data: bool = True,
    cancel_token_path: str | None = None,
) -> dict[str, Any]:
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 0 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ExtensionError("INVALID_ARGUMENT", f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS}; 0 disables automatic timeout.")
    if not isinstance(include_image_data, bool):
        raise ExtensionError("INVALID_ARGUMENT", "include_image_data must be boolean.")
    workspace = WorkspaceView(workspace_root)
    workflow = _load_workflow(workspace, workflow_path)
    _apply_overrides(workflow, overrides)
    _preflight_dynamic_inputs(workflow, port=port)
    profile_validation = _validate_production_profile(workflow, production_profile, port=port)
    if _cancel_requested(cancel_token_path):
        raise ExtensionError("COMFYUI_CANCELLED", "ComfyUI workflow cancellation was requested before prompt submission.")

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
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI did not return a prompt_id.")
    if _cancel_requested(cancel_token_path):
        cancel_dispatched = _cancel_prompt(prompt_id, port=port)
        raise ExtensionError(
            "COMFYUI_CANCELLED",
            "ComfyUI workflow cancellation was requested during prompt submission.",
            prompt_id=prompt_id,
            cancel_dispatched=cancel_dispatched,
        )
    node_errors = queued.get("node_errors") if isinstance(queued, dict) else None
    if isinstance(node_errors, dict) and node_errors:
        raise ExtensionError("COMFYUI_NODE_ERROR", "ComfyUI rejected one or more workflow nodes.", node_errors=node_errors)

    deadline = None if timeout_seconds == 0 else time.monotonic() + timeout_seconds
    history_entry: dict[str, Any] | None = None
    history_transport_failures = 0
    last_history_transport_error: str | None = None
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
                raise ExtensionError(
                    "COMFYUI_CANCELLED",
                    "ComfyUI workflow cancellation was requested.",
                    prompt_id=prompt_id,
                    cancel_dispatched=cancel_dispatched_event.is_set(),
                )
            try:
                history = _json_request("GET", f"/history/{prompt_id}", port=port, timeout=10)
            except ExtensionError as exc:
                if not _is_transient_history_error(exc):
                    raise
                history_transport_failures += 1
                last_history_transport_error = f"{exc.code}: {exc.message}"
                time.sleep(0.5)
                continue
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
        raise ExtensionError(
            "COMFYUI_TIMEOUT",
            f"ComfyUI workflow did not finish within {timeout_seconds} seconds.",
            prompt_id=prompt_id,
            cancel_dispatched=cancel_dispatched,
        )

    status = history_entry.get("status")
    if isinstance(status, dict) and status.get("status_str") == "error":
        raise ExtensionError("COMFYUI_EXECUTION_ERROR", "ComfyUI reported workflow execution failure.", status=status)

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
            raise ExtensionError("COMFYUI_OUTPUT_TOO_LARGE", f"Returned images exceed {MAX_TOTAL_IMAGE_BYTES} bytes total.")
        mime_type, extension = _image_type(data)
        sha256 = hashlib.sha256(data).hexdigest()
        saved_path: str | None = None
        if save_root is not None:
            output_path = save_root / f"comfyui-{prompt_id}-{index}{extension}"
            try:
                output_path.write_bytes(data)
            except OSError as exc:
                raise ExtensionError("WRITE_FAILED", f"Could not save ComfyUI output: {exc}") from exc
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
        "production_profile": profile_validation,
        "prompt_id": prompt_id,
        "artifacts_found": len(descriptors),
        "artifacts_returned": len(artifacts),
        "artifacts_truncated": len(descriptors) > len(artifacts),
        "artifacts": artifacts,
        "images_found": len(image_descriptors),
        "images_returned": len(rendered),
        "images": rendered,
        "status": status,
        "history_transport_failures": history_transport_failures,
        "last_history_transport_error": last_history_transport_error,
    }
    return {
        **metadata,
        "_content": [
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True)},
            *image_content,
        ],
    }


def _load_workflow(workspace: WorkspaceView, raw: str) -> dict[str, Any]:
    path = workspace.resolve(raw)
    if not path.is_file():
        raise ExtensionError("NOT_FOUND", "ComfyUI workflow JSON does not exist.", path=raw)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExtensionError("READ_FAILED", f"Could not read ComfyUI workflow: {exc}") from exc
    if len(data) > MAX_WORKFLOW_BYTES:
        raise ExtensionError("FILE_TOO_LARGE", f"ComfyUI workflow exceeds {MAX_WORKFLOW_BYTES} bytes.", path=raw)
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionError("INVALID_COMFYUI_WORKFLOW", "Workflow must be valid UTF-8 JSON in ComfyUI API format.") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ExtensionError("INVALID_COMFYUI_WORKFLOW", "Workflow must be a non-empty JSON object in ComfyUI API format.")
    for node_id, node in parsed.items():
        if not isinstance(node_id, str) or not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise ExtensionError("INVALID_COMFYUI_WORKFLOW", "Workflow nodes must use ComfyUI API format with class_type fields.")
        if "inputs" in node and not isinstance(node["inputs"], dict):
            raise ExtensionError("INVALID_COMFYUI_WORKFLOW", "Workflow node inputs must be objects.")
    return parsed


def _apply_overrides(workflow: dict[str, Any], overrides: dict[str, Any] | None) -> None:
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        raise ExtensionError("INVALID_ARGUMENT", "overrides must be an object keyed by ComfyUI node id.")
    for node_id, input_values in overrides.items():
        if not isinstance(node_id, str) or node_id not in workflow:
            raise ExtensionError("INVALID_ARGUMENT", f"Override references unknown ComfyUI node: {node_id}")
        if not isinstance(input_values, dict):
            raise ExtensionError("INVALID_ARGUMENT", f"Override for node {node_id} must be an object of input values.")
        inputs = workflow[node_id].setdefault("inputs", {})
        if not isinstance(inputs, dict):
            raise ExtensionError("INVALID_COMFYUI_WORKFLOW", f"Node {node_id} inputs are not an object.")
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
        raise ExtensionError(
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
            raise ExtensionError(
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
                        raise ExtensionError(
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
                    raise ExtensionError(
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
    except ExtensionError:
        return False


def _prepare_save_directory(workspace: WorkspaceView, raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ExtensionError("INVALID_ARGUMENT", "save_directory must be a non-empty workspace-relative path.")
    path = workspace.resolve(raw, for_write=True, allow_directory=True)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExtensionError("WRITE_FAILED", f"Could not create ComfyUI output directory: {exc}") from exc
    if not path.is_dir():
        raise ExtensionError("NOT_A_DIRECTORY", "save_directory is not a directory.", path=raw)
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
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact filename.")
    if "\\" in subfolder:
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact subfolder.")
    relative = PurePosixPath(subfolder)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unsafe artifact subfolder.")
    if output_type not in {"output", "input", "temp"}:
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned an unknown artifact storage type.")


def _argv_option(argv: list[str], name: str) -> str | None:
    prefix = name + "="
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(prefix):
            return value[len(prefix):]
    return None


def _comfyui_storage_roots(*, port: int, workspace: WorkspaceView) -> dict[str, Path]:
    try:
        stats = _json_request("GET", "/system_stats", port=port, timeout=3)
    except ExtensionError:
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
    workspace: WorkspaceView,
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
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI artifact path escaped its declared storage root.") from exc
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
        raise ExtensionError("COMFYUI_INVALID_RESPONSE", "ComfyUI returned invalid JSON.") from exc


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
        raise ExtensionError("INVALID_ARGUMENT", "Invalid ComfyUI loopback port.")
    if not path.startswith("/") or path.startswith("//"):
        raise ExtensionError("INVALID_ARGUMENT", "Invalid ComfyUI API path.")
    url = f"http://{COMFYUI_HOST}:{port}{path}"
    headers = {"Accept": "application/json, image/*"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except ExtensionError:
        raise
    except HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise ExtensionError(
            "COMFYUI_HTTP_ERROR",
            f"ComfyUI returned HTTP {exc.code}: {detail[:1000]}",
            status=exc.code,
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ExtensionError("COMFYUI_OFFLINE", f"Cannot reach local ComfyUI at {COMFYUI_HOST}:{port}: {exc}") from exc
    if len(data) > limit:
        raise ExtensionError("COMFYUI_RESPONSE_TOO_LARGE", f"ComfyUI response exceeds {limit} bytes.")
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
    raise ExtensionError("UNSUPPORTED_IMAGE", "ComfyUI output is not PNG, JPEG, GIF, or WebP.")
