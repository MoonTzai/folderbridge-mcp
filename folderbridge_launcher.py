"""Source and frozen entry point for FolderBridge MCP."""

import sys

from folderbridge_mcp.cli import main


if __name__ == "__main__":
    if len(sys.argv) == 1:
        from folderbridge_mcp.user_paths import clear_internal_config_root_environment

        # The no-argument GUI entry is a top-level state owner, never an
        # internal child. Do not accept a caller-supplied child propagation root.
        clear_internal_config_root_environment()
        from folderbridge_mcp.gui import main as gui_main

        raise SystemExit(gui_main())
    raise SystemExit(main())
