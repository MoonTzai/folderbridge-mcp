from __future__ import annotations

from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError

from comfyui_runtime import (
    cancel_prompt,
    comfyui_status,
    get_features,
    get_job_status,
    get_health_check,
    get_progress,
    get_node_info,
    list_jobs,
    list_models,
    release_comfyui_memory,
    preflight_workflow,
    run_workflow,
)


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return comfyui_status()
    if action == "free":
        return release_comfyui_memory(
            unload_models=params.get("unload_models", True),
            free_memory=params.get("free_memory", True),
        )
    if action == "jobs":
        return list_jobs(
            statuses=params.get("statuses"),
            limit=params.get("limit", 20),
            offset=params.get("offset", 0),
            sort_by=params.get("sort_by", "created_at"),
            sort_order=params.get("sort_order", "desc"),
        )
    if action == "job-status":
        return get_job_status(params["prompt_id"])
    if action == "progress":
        return get_progress(
            params["prompt_id"],
            node_id=params.get("node_id"),
            observe_seconds=params.get("observe_seconds", 2.0),
        )
    if action == "health-check":
        return get_health_check(params["prompt_id"])
    if action == "cancel-prompt":
        return cancel_prompt(params["prompt_id"])
    if action == "node-info":
        return get_node_info(params["class_name"])
    if action == "models":
        return list_models(
            folder=params.get("folder"),
            contains=params.get("contains"),
            max_items=params.get("max_items", 200),
        )
    if action == "features":
        return get_features()
    if action not in {"preflight", "run"}:
        raise ExtensionError("EXTENSION_ACTION_NOT_FOUND", f"Unsupported ComfyUI action: {action}")

    workspace_root = context.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        raise ExtensionError("WORKSPACE_REQUIRED", "ComfyUI run requires a selected workspace.")
    try:
        root = Path(workspace_root).resolve(strict=True)
    except OSError as exc:
        raise ExtensionError("WORKSPACE_REQUIRED", f"ComfyUI workspace is unavailable: {exc}") from exc
    if not root.is_dir():
        raise ExtensionError("WORKSPACE_REQUIRED", "ComfyUI workspace root is not a directory.")

    if action == "preflight":
        return preflight_workflow(
            root,
            params["workflow_path"],
            overrides=params.get("overrides"),
            production_profile=params.get("production_profile", "auto"),
        )

    return run_workflow(
        root,
        params["workflow_path"],
        overrides=params.get("overrides"),
        save_directory=params.get("save_directory"),
        timeout_seconds=params.get("timeout_seconds", 2 * 60 * 60),
        production_profile=params.get("production_profile", "auto"),
        include_image_data=False,
        cancel_token_path=context.get("job_cancel_path"),
    )
