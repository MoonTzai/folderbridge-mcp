from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "pdf-toolkit"
SPEC = importlib.util.spec_from_file_location("folderbridge_pdf_toolkit_v06_regressions", PLUGIN_ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PdfToolkitBackendMigrationRegressionTests(unittest.TestCase):
    def test_backend_migration_does_not_restore_python_pdf_parser(self):
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertEqual(plugin.PINNED_PDFPIG_VERSION, "0.1.16")
        self.assertIn('PDF_INSPECT_SCRIPT = PLUGIN_DIR / "pdf_inspect.ps1"', source)
        self.assertNotIn("PINNED_PYPDF_VERSION", source)
        self.assertNotIn('importlib.import_module("pypdf")', source)

    def test_public_text_and_range_caps_remain_locked(self):
        self.assertEqual(plugin.MAX_PAGE_TEXT_CHARS, 1_000_000)
        self.assertEqual(plugin.MAX_READ_PAGES, 50)
        self.assertEqual(plugin.MAX_SEARCH_PAGES, 500)
        self.assertEqual(plugin.MAX_METADATA_VALUE_CHARS, 4096)
        self.assertEqual(plugin.MAX_TOC_TITLE_CHARS, 512)
        self.assertEqual(plugin.TOC_MAX_DEPTH, 15)

    def test_search_window_is_hard_bounded(self):
        self.assertEqual(plugin._validated_search_range(1000, 1, 500), (1, 500))
        with self.assertRaises(Exception):
            plugin._validated_search_range(1000, 1, 501)

    def test_unicode_casefold_and_content_order_semantics_are_inspector_owned(self):
        script = (PLUGIN_ROOT / "pdf_inspect.ps1").read_text(encoding="ascii")
        self.assertIn("ContentOrderTextExtractor", script)
        self.assertIn("casefold-map.json", script)
        self.assertIn("Measure-Scalars", script)
        self.assertIn("Fold-WithOrigins", script)
        self.assertNotIn("CompareInfo", script)

    def test_read_pages_contract_keeps_legacy_public_chars_field(self):
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('{"page", "text", "chars", "extracted_chars", "text_truncated", "partial"}', source)
        self.assertNotIn('{"page", "text", "page_chars", "extracted_chars", "text_truncated", "partial"}', source)

    def test_render_source_change_cleans_uncommitted_output_without_inspector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "docs").mkdir()
            (root / "out").mkdir()
            pdf = root / "docs" / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.7\n")
            identity = {"bytes": pdf.stat().st_size, "sha256": "f" * 64, "signature": (pdf.stat().st_size, 1, 1, 1)}

            def fake_renderer(_path, _output_dir, _start, _end, _dpi, **_kwargs):
                return {
                    "source_units": 1,
                    "selected_range": {"start": 1, "end": 1, "unit": "page"},
                    "dpi_nominal": 180,
                    "total_pixels_nominal": 1,
                    "pages": [{"page": 1, "width_pixels_nominal": 1, "height_pixels_nominal": 1, "pixels_nominal": 1}],
                    "files": ["P0001.png"],
                }

            with mock.patch.object(plugin, "_capture_source_identity", return_value=identity), \
                 mock.patch.object(plugin, "_run_windows_renderer", side_effect=fake_renderer), \
                 mock.patch.object(plugin, "_assert_source_unchanged", side_effect=RuntimeError("source changed")):
                with self.assertRaises(RuntimeError):
                    plugin._render_pages(
                        root,
                        {
                            "path": "docs/manual.pdf",
                            "page_start": 1,
                            "page_end": 1,
                            "output_dir": "out/render",
                            "dpi": 180,
                            "make_zip": False,
                        },
                    )
            self.assertFalse((root / "out" / "render").exists())

    def test_render_destination_must_not_preexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "out").mkdir()
            (root / "out" / "existing").mkdir()
            with self.assertRaises(Exception):
                plugin._create_fresh_output_dir(root, "out/existing")


if __name__ == "__main__":
    unittest.main()
