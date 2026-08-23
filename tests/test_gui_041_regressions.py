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
        self.assertIn("EXTENSION_SIDEBAR_WIDTH = 380", self.gui)
        self.assertIn("EXTENSION_SIDEBAR_CONTENT_WIDTH = 345", self.gui)
        self.assertIn("EXTENSION_SIDEBAR_WRAP_WIDTH = 330", self.gui)
        self.assertIn('self.extension_sidebar.configure(width=self._px(EXTENSION_SIDEBAR_WIDTH)', self.gui)
        self.assertIn('self.extension_canvas.configure(width=self._px(EXTENSION_SIDEBAR_CONTENT_WIDTH))', self.gui)
        self.assertIn("self.status_dot.coords", self.gui)

    def test_sidebar_fit_budget_tracks_the_wider_sidebar_constant(self) -> None:
        self.assertIn("content_width += self._px(EXTENSION_SIDEBAR_WIDTH + 12)", self.gui)
        self.assertIn("wraplength=self._px(EXTENSION_SIDEBAR_WRAP_WIDTH)", self.gui)

    def test_runtime_dpi_changes_explicitly_resize_existing_fonts(self) -> None:
        self.assertIn("import tkinter.font as tkfont", self.gui)
        self.assertIn("DPI_FONT_SPECS", self.gui)
        self.assertIn("def _refresh_fonts", self.gui)
        self.assertIn("font_pixel_size(point_size, self._dpi)", self.gui)
        self.assertIn("managed.configure(family=family, size=pixel_size, weight=weight)", self.gui)
        self.assertIn('font=self._font("log")', self.gui)
        self.assertIn('font=self._font("primary_button")', self.gui)
        self.assertNotIn("font=(\"Segoe UI\"", self.gui)
        self.assertNotIn("font=(\"Cascadia Mono\"", self.gui)

    def test_tunnel_text_entries_use_explicit_dpi_metrics_instead_of_native_ttk_entry(self) -> None:
        tunnel_block = self.gui.split("def _build_tunnel_settings", 1)[1].split("def _build_log", 1)[0]
        self.assertIn("def _build_dpi_entry", self.gui)
        self.assertEqual(tunnel_block.count("self._build_dpi_entry("), 4)
        self.assertNotIn("ttk.Entry(", tunnel_block)
        self.assertIn("self._dpi_entries", self.gui)
        self.assertIn("entry.grid_configure(ipadx=self._px(6), ipady=self._px(5))", self.gui)

    def test_compact_buttons_are_limited_to_select_all_and_clear(self) -> None:
        self.assertIn('style.configure("Compact.TButton"', self.gui)
        self.assertEqual(self.gui.count('style="Compact.TButton"'), 2)
        self.assertIn('text="全选",\n            style="Compact.TButton"', self.gui)
        self.assertIn('text="清空",\n            style="Compact.TButton"', self.gui)
        self.assertNotIn('text="应用配置", style="Compact.TButton"', self.gui)
        self.assertNotIn('text="诊断", style="Compact.TButton"', self.gui)

    def test_managed_service_status_is_polled_and_color_coded(self) -> None:
        self.assertIn("MANAGED_SERVICE_STATUS_POLL_MS = 2_000", self.gui)
        self.assertIn("def _poll_managed_service_statuses", self.gui)
        self.assertIn("if self._sidebar_visible:\n            self._refresh_managed_service_statuses_async()", self.gui)
        self.assertIn('style.configure("ServiceOnline.TLabel"', self.gui)
        self.assertIn('foreground="#16803c"', self.gui)
        self.assertIn('style.configure("ServiceOffline.TLabel"', self.gui)
        self.assertIn('foreground="#c62828"', self.gui)
        self.assertIn('return "服务：在线 · FolderBridge 托管", "ServiceOnline.TLabel"', self.gui)
        self.assertIn('return "服务：离线", "ServiceOffline.TLabel"', self.gui)
        self.assertIn("self._managed_service_status_pending", self.gui)
        self.assertIn("self._update_managed_service_status_label", self.gui)

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
