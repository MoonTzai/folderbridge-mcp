import tkinter as tk
import unittest
from tkinter import ttk

from folderbridge_mcp.gui import FolderBridgeLauncher


class _WidgetStub:
    def __init__(self) -> None:
        self.state = "unknown"

    def configure(self, *, state: str) -> None:
        self.state = state


class _RunningSupervisor:
    @staticmethod
    def running() -> bool:
        return True


class GuideGuiTests(unittest.TestCase):
    def test_running_connection_keeps_guide_button_enabled(self) -> None:
        launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
        launcher.supervisor = _RunningSupervisor()
        launcher.apply_button = _WidgetStub()
        launcher.doctor_button = _WidgetStub()
        launcher.copy_button = _WidgetStub()
        launcher.guide_button = _WidgetStub()
        launcher.start_button = _WidgetStub()
        launcher._set_form_state = lambda _enabled: None

        launcher._set_busy(False)

        self.assertEqual(launcher.guide_button.state, "normal")
        self.assertEqual(launcher.apply_button.state, "disabled")
        self.assertEqual(launcher.start_button.state, "normal")

    def test_guide_instructions_are_read_only_and_selectable(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        try:
            launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
            launcher._ui_scale = 1.0
            notebook = ttk.Notebook(root)
            tab = launcher._guide_tab(
                notebook,
                "可复制标题",
                ("1. tunnel-client-v<版本>-windows-amd64.zip",),
                720,
                warning="保持只读。",
            )
            guide_text = tab.guide_text  # type: ignore[attr-defined]
            self.assertEqual(str(guide_text.cget("state")), "disabled")
            self.assertIn("tunnel-client-v<版本>-windows-amd64.zip", guide_text.get("1.0", "end-1c"))

            guide_text.tag_add("sel", "1.0", "1.5")
            self.assertEqual(tuple(map(str, guide_text.tag_ranges("sel"))), ("1.0", "1.5"))
            guide_text.event_generate("<<Copy>>")
            root.update()
            self.assertEqual(root.clipboard_get(), "可复制标题")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
