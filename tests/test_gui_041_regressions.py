from __future__ import annotations

import unittest
from pathlib import Path


class Gui041RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.gui = (cls.root / "folderbridge_mcp" / "gui.py").read_text(encoding="utf-8")
        cls.dpi = (cls.root / "folderbridge_mcp" / "dpi.py").read_text(encoding="utf-8")
        cls.manifest = (cls.root / "packaging" / "windows_dpi_manifest.xml").read_text(encoding="utf-8")

    def test_per_monitor_v2_and_get_dpi_for_window_are_present(self) -> None:
        self.assertIn("PerMonitorV2", self.manifest)
        self.assertIn("true/pm", self.manifest)
        self.assertIn("SetProcessDpiAwarenessContext", self.dpi)
        self.assertIn("GetDpiForWindow", self.dpi)

    def test_dpi_fallback_poll_only_applies_when_dpi_changes(self) -> None:
        self.assertIn("def _poll_dpi", self.gui)
        self.assertIn("self.root.after(400, self._poll_dpi)", self.gui)
        self.assertIn("if current_dpi != self._dpi:\n            self._apply_dpi(current_dpi)", self.gui)
        self.assertIn("def _refresh_dpi_metrics", self.gui)
        self.assertIn('self.workspace_tree.column("workspace_id", width=self._px(115))', self.gui)
        self.assertIn('self.extension_sidebar.configure(width=self._px(320)', self.gui)
        self.assertIn('self.extension_canvas.configure(width=self._px(285))', self.gui)
        self.assertIn("self.status_dot.coords", self.gui)

    def test_compact_buttons_are_limited_to_select_all_and_clear(self) -> None:
        self.assertIn('style.configure("Compact.TButton"', self.gui)
        self.assertEqual(self.gui.count('style="Compact.TButton"'), 2)
        self.assertIn('text="全选",\n            style="Compact.TButton"', self.gui)
        self.assertIn('text="清空",\n            style="Compact.TButton"', self.gui)
        self.assertNotIn('text="应用配置", style="Compact.TButton"', self.gui)
        self.assertNotIn('text="诊断", style="Compact.TButton"', self.gui)

    def test_shutdown_is_worker_orchestrated_and_destroy_is_main_thread_event(self) -> None:
        self.assertIn('name="folderbridge-shutdown"', self.gui)
        self.assertIn('self.managed_services.shutdown(loaded_extension_ids)', self.gui)
        self.assertLess(
            self.gui.index('self.managed_services.shutdown(loaded_extension_ids)'),
            self.gui.index('self.supervisor.stop()', self.gui.index('def _shutdown_application')),
        )
        self.assertIn('elif kind == "shutdown-complete":\n                self._finish_shutdown()', self.gui)
        self.assertIn("self.root.destroy()", self.gui)

    def test_managed_service_controls_distinguish_external_and_owned(self) -> None:
        self.assertIn("FolderBridge 托管", self.gui)
        self.assertIn("外部服务（不会被 FolderBridge 终止）", self.gui)
        self.assertIn('text="选择目录…"', self.gui)
        self.assertIn('text="启动"', self.gui)
        self.assertIn('text="停止"', self.gui)
        self.assertIn("if busy or not owned:\n                    stop_button.configure(state=\"disabled\")", self.gui)


if __name__ == "__main__":
    unittest.main()
