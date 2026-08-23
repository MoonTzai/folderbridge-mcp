import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

from folderbridge_mcp.extension_spec import EXTENSION_FORMAT_SUMMARY, EXTENSION_LLM_PROMPT
from folderbridge_mcp.gui import FolderBridgeLauncher, bounded_dialog_geometry


class _WidgetStub:
    def __init__(self) -> None:
        self.state = "unknown"

    def configure(self, *, state: str) -> None:
        self.state = state


class _BoolVarStub:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def set(self, value: bool) -> None:
        self.value = bool(value)

    def get(self) -> bool:
        return self.value


class _RunningSupervisor:
    @staticmethod
    def running() -> bool:
        return True


class GuideGuiTests(unittest.TestCase):
    def test_extension_llm_prompt_requires_active_file_requests(self) -> None:
        self.assertIn("主动要求我上传/提供", EXTENSION_LLM_PROMPT)
        self.assertIn("优先让用户上传文件", EXTENSION_LLM_PROMPT)
        self.assertIn("workspace_adapter.mode=dynamic", EXTENSION_LLM_PROMPT)
        self.assertIn("folderbridge-extension.json", EXTENSION_FORMAT_SUMMARY)
        self.assertIn("独立子进程", EXTENSION_FORMAT_SUMMARY)

    def test_extension_standard_discourages_large_aggregate_actions(self) -> None:
        self.assertIn("run-all", EXTENSION_FORMAT_SUMMARY)
        self.assertIn("verification-plan", EXTENSION_FORMAT_SUMMARY)
        self.assertIn("聚合", EXTENSION_LLM_PROMPT)
        root = Path(__file__).resolve().parents[1]
        for relative in ("README.md", "README.zh-CN.md", "docs/extensions.md"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIn("run-all", text, relative)

    def test_bounded_dialog_geometry_caps_to_work_area_and_clamps_position(self) -> None:
        self.assertEqual(
            bounded_dialog_geometry(
                (100, 50, 1100, 750),
                (1200, 900),
                (800, 100, 250, 500),
                min_size=(720, 560),
            ),
            (180, 50, 920, 630),
        )

    def test_bounded_dialog_geometry_respects_minimum_when_space_allows(self) -> None:
        self.assertEqual(
            bounded_dialog_geometry(
                (0, 0, 1600, 1000),
                (500, 400),
                (300, 200, 900, 700),
                min_size=(720, 560),
            )[2:],
            (720, 560),
        )

    def test_connection_guide_uses_work_area_and_content_fit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        gui = (root / "folderbridge_mcp" / "gui.py").read_text(encoding="utf-8")
        block = gui.split("def _open_web_setup", 1)[1].split("def _guide_tab", 1)[0]
        self.assertIn("window_work_area(self.root)", block)
        self.assertNotIn("self.root.winfo_screenwidth()", block)
        self.assertIn("self._fit_guide_dialog(dialog, body)", block)
        self.assertIn("width - self._px(", block)

    def test_global_capability_select_all_and_clear(self) -> None:
        launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
        launcher.capability_vars = {"a": _BoolVarStub(), "b": _BoolVarStub()}
        launcher._refresh_status_cards = lambda: None

        launcher._set_all_capabilities(True)
        self.assertTrue(all(variable.get() for variable in launcher.capability_vars.values()))
        launcher._set_all_capabilities(False)
        self.assertFalse(any(variable.get() for variable in launcher.capability_vars.values()))

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

    def test_managed_fonts_resize_in_place_across_monitor_dpi_changes(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        try:
            launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
            launcher.root = root
            launcher._fonts = {}
            launcher._dpi = 96
            launcher._refresh_fonts()
            body = launcher._font("body")
            self.assertEqual(int(body.cget("size")), -12)

            launcher._dpi = 144
            launcher._refresh_fonts()
            self.assertIs(launcher._font("body"), body)
            self.assertEqual(int(body.cget("size")), -18)

            launcher._dpi = 96
            launcher._refresh_fonts()
            self.assertEqual(int(body.cget("size")), -12)
        finally:
            root.destroy()

    def test_guide_instructions_are_read_only_and_selectable(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        try:
            launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
            launcher.root = root
            launcher._dpi = 96
            launcher._ui_scale = 1.0
            launcher._fonts = {}
            launcher._guide_text_widgets = []
            launcher._refresh_fonts()
            notebook = ttk.Notebook(root)
            tab = launcher._guide_tab(
                notebook,
                "可复制标题",
                (
                    "1. tunnel-client-v<版本>-windows-amd64.zip",
                    "2. 选择 tunnel-client.exe。",
                ),
                720,
                warnings_after_steps={1: "不要选择 tunnel-client-runtime-*。"},
            )
            guide_text = tab.guide_text  # type: ignore[attr-defined]
            self.assertEqual(str(guide_text.cget("state")), "disabled")
            rendered = guide_text.get("1.0", "end-1c")
            self.assertIn("tunnel-client-v<版本>-windows-amd64.zip", rendered)
            self.assertLess(rendered.index("1. tunnel-client"), rendered.index("注意：不要选择"))
            self.assertLess(rendered.index("注意：不要选择"), rendered.index("2. 选择"))

            guide_text.tag_add("sel", "1.0", "1.5")
            self.assertEqual(tuple(map(str, guide_text.tag_ranges("sel"))), ("1.0", "1.5"))
            guide_text.event_generate("<<Copy>>")
            root.update()
            self.assertEqual(root.clipboard_get(), "可复制标题")
        finally:
            root.destroy()

    def test_guide_rejects_warning_for_missing_step(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        try:
            launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
            launcher.root = root
            launcher._dpi = 96
            launcher._ui_scale = 1.0
            launcher._fonts = {}
            launcher._guide_text_widgets = []
            launcher._refresh_fonts()
            notebook = ttk.Notebook(root)
            with self.assertRaisesRegex(ValueError, "步骤号不存在"):
                launcher._guide_tab(
                    notebook,
                    "标题",
                    ("1. 唯一步骤",),
                    720,
                    warnings_after_steps={2: "无对应步骤"},
                )
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
