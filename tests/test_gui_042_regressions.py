from __future__ import annotations

import unittest
from pathlib import Path


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
