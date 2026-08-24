from __future__ import annotations

import unittest
from pathlib import Path

from folderbridge_mcp.gui import extension_sidebar_status


class Gui042RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.gui = (cls.root / "folderbridge_mcp" / "gui.py").read_text(encoding="utf-8")

    def test_main_page_is_scrollable_when_high_dpi_content_exceeds_viewport(self) -> None:
        self.assertIn("self.page_canvas = tk.Canvas", self.gui)
        self.assertIn('ttk.Scrollbar(main, orient="vertical", command=self.page_canvas.yview)', self.gui)
        self.assertIn("self._page_window_id = self.page_canvas.create_window", self.gui)
        self.assertIn('self.page_canvas.bind("<Configure>", self._resize_page_canvas', self.gui)
        self.assertIn("def _resize_page_canvas", self.gui)
        self.assertIn('self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))', self.gui)

    def test_launcher_grows_to_requested_content_before_falling_back_to_scroll(self) -> None:
        self.assertIn("self.root.after_idle(self._fit_window_to_content)", self.gui)
        self.assertIn("def _fit_window_to_content", self.gui)
        self.assertIn("self.page.winfo_reqheight()", self.gui)
        self.assertIn("window_work_area(self.root)", self.gui)
        self.assertNotIn("screen_height * 0.94", self.gui)
        self.assertIn("def _update_page_scrollbar", self.gui)
        self.assertIn("self.page_scrollbar.grid_remove()", self.gui)
        self.assertIn("content_height > viewport_height", self.gui)

    def test_main_page_mousewheel_scrolls_blank_area_without_hijacking_nested_scrollers(self) -> None:
        self.assertIn('self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")', self.gui)
        self.assertIn("def _on_mousewheel", self.gui)
        self.assertIn("def _has_independent_wheel_scroll", self.gui)
        self.assertIn("ttk.Treeview", self.gui)
        self.assertIn("ttk.Combobox", self.gui)
        self.assertIn("tk.Text", self.gui)
        self.assertIn("yscrollcommand", self.gui)
        self.assertIn("self.page_canvas.yview_scroll", self.gui)
        self.assertIn("self.extension_canvas.yview_scroll", self.gui)

    def test_window_fit_uses_monitor_work_area_and_clamps_position(self) -> None:
        self.assertIn("from .dpi import (", self.gui)
        self.assertIn("window_work_area", self.gui)
        fit_block = self.gui.split("def _fit_window_to_content", 1)[1].split("def _build_extension_sidebar", 1)[0]
        self.assertIn("work_left, work_top, work_right, work_bottom", fit_block)
        self.assertIn("max_height", fit_block)
        self.assertIn("self.root.winfo_y()", fit_block)
        self.assertIn("self.root.geometry", fit_block)

    def test_main_settings_cards_can_collapse_individually_or_together(self) -> None:
        self.assertIn("self._collapsible_sections", self.gui)
        self.assertIn("def _register_collapsible_section", self.gui)
        self.assertIn("def _toggle_section", self.gui)
        self.assertIn("def _toggle_all_sections", self.gui)
        self.assertIn('text="全部折叠"', self.gui)
        self.assertIn('text="收起 ▴"', self.gui)
        self.assertIn('self._set_widget_text(button, "展开 ▾" if collapsed else "收起 ▴")', self.gui)
        self.assertIn('self._register_collapsible_section("local"', self.gui)
        self.assertIn('self._register_collapsible_section("tunnel"', self.gui)
        self.assertIn('"log",\n            log_card,', self.gui)
        self.assertIn("widget.grid_remove()", self.gui)
        self.assertIn("self.root.after_idle(self._fit_window_to_content)", self.gui)

    def test_toggle_all_sections_batches_layout_reflow_once(self) -> None:
        set_block = self.gui.split("def _set_section_collapsed", 1)[1].split("def _toggle_all_sections", 1)[0]
        all_block = self.gui.split("def _toggle_all_sections", 1)[1].split("def _sync_sections_toggle_button", 1)[0]
        self.assertIn("defer_layout: bool = False", set_block)
        self.assertIn("if not defer_layout:", set_block)
        self.assertEqual(set_block.count("self.root.after_idle(self._fit_window_to_content)"), 1)
        self.assertIn("self._set_section_collapsed(key, collapse, defer_layout=True)", all_block)
        self.assertEqual(all_block.count("self.root.after_idle(self._fit_window_to_content)"), 1)

    def test_comfyui_first_run_is_explicit_instead_of_silent(self) -> None:
        self.assertIn("服务：正在启动 / 检测…", self.gui)
        self.assertIn("服务：等待配置安装目录 · 自动启动尚未执行", self.gui)
        self.assertIn("未选择（首次需配置）", self.gui)
        self.assertIn("ComfyUI 尚未配置安装目录；自动启动会等待首次选择", self.gui)
        self.assertIn("if not self._sidebar_visible:\n                    self._toggle_extension_sidebar()", self.gui)

    def test_managed_services_get_a_bounded_second_startup_reconciliation_pass(self) -> None:
        self.assertIn("self.root.after(300, lambda: self._initialize_managed_services(final_pass=False))", self.gui)
        self.assertIn("self.root.after(1800, lambda: self._initialize_managed_services(final_pass=True))", self.gui)
        self.assertIn("def _initialize_managed_services(self, *, final_pass: bool = True)", self.gui)
        self.assertIn("托管服务自动启动检查：", self.gui)
        self.assertIn("Extension 当前未加载", self.gui)

    def test_sidebar_toggle_does_not_rebuild_extension_cards_on_every_open(self) -> None:
        toggle_block = self.gui.split("def _toggle_extension_sidebar", 1)[1].split("def _refresh_extension_sidebar", 1)[0]
        self.assertNotIn("self._refresh_extension_sidebar()", toggle_block)
        self.assertIn("self.extension_sidebar.grid()", toggle_block)
        self.assertIn("self.extension_sidebar.grid_remove()", toggle_block)
        self.assertEqual(toggle_block.count("self.root.after_idle(self._fit_window_to_content)"), 1)

    def test_dynamic_workspace_adapter_status_is_not_misreported_as_unloaded(self) -> None:
        item = {
            "trusted": True,
            "enabled": True,
            "loaded": False,
            "approval_stale": False,
            "workspace_adapter": {"mode": "dynamic"},
        }
        self.assertEqual(extension_sidebar_status(item), "✓ 已批准 · 已启用 · 工作区匹配时加载")
        loaded = dict(item, loaded=True)
        self.assertEqual(extension_sidebar_status(loaded), "✓ 已批准 · 已加载")
        ordinary = dict(item, workspace_adapter={"mode": "none"})
        self.assertEqual(extension_sidebar_status(ordinary), "已批准 · 未加载")

    def test_extensions_sidebar_also_manages_skill_packs_through_core_engine(self) -> None:
        self.assertIn("from .skills import SkillEngine, skill_pack_root_path", self.gui)
        self.assertIn("self.skill_engine = SkillEngine()", self.gui)
        self.assertIn('text="Extensions & Skills"', self.gui)
        self.assertIn('text="Skill 目录"', self.gui)
        self.assertIn("self.skill_engine.describe(include_untrusted=True)", self.gui)
        self.assertIn("self.skill_engine.approve_pack(pack_id, item[\"sha256\"])", self.gui)
        self.assertIn("self.skill_engine.set_enabled(pack_id, True)", self.gui)
        self.assertIn("self.skill_engine.set_enabled(pack_id, False)", self.gui)
        self.assertIn("self.skill_engine.revoke_pack(pack_id)", self.gui)
        self.assertIn("Skill 文本不会执行本地代码", self.gui)
        self.assertIn("但会影响模型的方法选择和行为", self.gui)
        approval_block = self.gui.split("def _toggle_skill_enabled", 1)[1].split("def _revoke_skill_pack", 1)[0]
        self.assertIn('item.get("source")', approval_block)
        self.assertIn("来源：", approval_block)

    def test_setup_guide_distinguishes_exe_runtime_from_optional_toolchains(self) -> None:
        self.assertIn("只使用 FolderBridge.exe：无需另外安装 Python 或 Node.js", self.gui)
        self.assertIn("推荐安装 Python 3.11 x64", self.gui)
        self.assertIn("Node.js LTS", self.gui)
        self.assertIn("capability 是授权和受限入口，不是包管理器", self.gui)
        self.assertIn("https://www.python.org/downloads/windows/", self.gui)
        self.assertIn("https://nodejs.org/en/download", self.gui)

    def test_guide_does_not_list_comfyui_as_a_global_capability(self) -> None:
        self.assertNotIn("测试/构建/EXE/APK/GitHub/本地 ComfyUI", self.gui)
        self.assertIn("插件授权与本地 ComfyUI 在右侧 Extensions 单独管理", self.gui)


if __name__ == "__main__":
    unittest.main()
