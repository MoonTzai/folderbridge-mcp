from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path


INTERNAL_CONFIG_ROOT_ENV = "FOLDERBRIDGE_CONFIG_ROOT"


def user_config_root(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return FolderBridge's canonical per-user configuration root.

    Extension workers receive ``FOLDERBRIDGE_CONFIG_ROOT`` from the host so a
    cleaned worker environment cannot accidentally choose a different profile
    directory from the launcher/MCP process.
    """

    values = os.environ if environ is None else environ
    current_platform = sys.platform if platform is None else platform
    internal = values.get(INTERNAL_CONFIG_ROOT_ENV, "").strip()
    if internal:
        return Path(internal).expanduser()

    if current_platform == "win32":
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            base = Path(local_app_data)
        else:
            user_profile = values.get("USERPROFILE", "").strip()
            if user_profile:
                base = Path(user_profile) / "AppData" / "Local"
            else:
                resolved_home = Path.home() if home is None else home
                base = resolved_home / "AppData" / "Local"
        return base / "folderbridge-mcp"

    xdg_config = values.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        base = Path(xdg_config)
    else:
        resolved_home = Path.home() if home is None else home
        base = resolved_home / ".config"
    return base / "folderbridge-mcp"
