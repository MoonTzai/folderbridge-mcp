from __future__ import annotations

from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError

from comfyui_runtime import comfyui_status, run_workflow


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return comfyui_status()
    if action != "run":
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

    return run_workflow(
        root,
        params["workflow_path"],
        overrides=params.get("overrides"),
        save_directory=params.get("save_directory"),
        timeout_seconds=params.get("timeout_seconds", 2 * 60 * 60),
        include_image_data=False,
        cancel_token_path=context.get("job_cancel_path"),
    )
