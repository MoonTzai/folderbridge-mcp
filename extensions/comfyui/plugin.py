from __future__ import annotations

from pathlib import Path
from typing import Any

from folderbridge_mcp.comfyui import comfyui_status, run_workflow
from folderbridge_mcp.security import ToolError, Workspace


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return comfyui_status()
    if action != "run":
        raise ToolError("EXTENSION_ACTION_NOT_FOUND", f"Unsupported ComfyUI action: {action}")

    workspace_root = context.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        raise ToolError("WORKSPACE_REQUIRED", "ComfyUI run requires a selected workspace.")
    workspace = Workspace(Path(workspace_root).resolve(strict=True))
    save_directory = params.get("save_directory")
    if context.get("workspace_read_only") and save_directory:
        raise ToolError("READ_ONLY", "save_directory is unavailable while FolderBridge is in read-only mode.")
    return run_workflow(
        workspace,
        params["workflow_path"],
        overrides=params.get("overrides"),
        save_directory=save_directory,
        timeout_seconds=params.get("timeout_seconds", 2 * 60 * 60),
        include_image_data=False,
    )
