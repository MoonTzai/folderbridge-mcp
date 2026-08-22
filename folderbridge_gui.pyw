"""Source launcher for the FolderBridge MCP desktop interface."""

import os

try:
    from folderbridge_mcp.gui import main
except ModuleNotFoundError as exc:
    if os.name == "nt" and exc.name == "tkinter":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "当前 Python 没有安装 tkinter，无法打开图形界面。\n\n"
            "普通用户请改为双击 FolderBridge.exe；从源码运行时请使用带 tkinter 的 Python 3.11 或更高版本。",
            "FolderBridge MCP 启动失败",
            0x10,
        )
        raise SystemExit(2) from None
    raise


if __name__ == "__main__":
    raise SystemExit(main())
