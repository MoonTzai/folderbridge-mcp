"""Source and frozen entry point for FolderBridge MCP."""

import sys

from folderbridge_mcp.cli import main


if __name__ == "__main__":
    if len(sys.argv) == 1:
        from folderbridge_mcp.gui import main as gui_main

        raise SystemExit(gui_main())
    raise SystemExit(main())
