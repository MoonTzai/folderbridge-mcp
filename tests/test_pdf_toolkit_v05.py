from __future__ import annotations

import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "pdf-toolkit"
SPEC = importlib.util.spec_from_file_location("folderbridge_pdf_toolkit_v06_compat", PLUGIN_ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PdfToolkitBackendCompatibilityTests(unittest.TestCase):
    def test_manifest_preserves_permissions_and_render_job_contract_after_backend_migration(self):
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "0.6.0")
        self.assertEqual(
            manifest["permissions"],
            ["workspace.read", "workspace.write", "process.execute:powershell.exe"],
        )
        render = manifest["actions"]["render-pages"]
        self.assertEqual(render["run_mode"], "job")
        self.assertEqual(render["timeout_seconds"], 7200)
        self.assertEqual(render["input_schema"]["properties"]["dpi"]["maximum"], 400)

    def test_runtime_uses_pdfpig_out_of_process_and_no_python_parser(self):
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('PINNED_PDFPIG_VERSION = "0.1.16"', source)
        self.assertIn('PDF_INSPECT_SCRIPT = PLUGIN_DIR / "pdf_inspect.ps1"', source)
        self.assertIn('PDF_RENDER_SCRIPT = PLUGIN_DIR / "pdf_render.ps1"', source)
        self.assertIn("subprocess.Popen", source)
        self.assertNotIn("PINNED_PYPDF_VERSION", source)
        self.assertNotIn('importlib.import_module("pypdf")', source)

    def test_installer_is_exact_pdfpig_nuget_transaction(self):
        source = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("$ExtensionVersion = '0.6.0'", source)
        self.assertIn("$PdfPigVersion = '0.1.16'", source)
        self.assertIn("PdfPig.0.1.16.nupkg", source)
        self.assertIn("_vendor-dotnet", source)
        self.assertIn("VENDOR-PROVENANCE.json", source)
        self.assertIn("transaction.json", source)
        self.assertNotIn("pypdf-reader-no-xmp-xml-v1", source)

    def test_windows_pdf_renderer_is_dpi_and_pixel_bounded(self):
        script = (PLUGIN_ROOT / "pdf_render.ps1").read_text(encoding="utf-8")
        self.assertIn("[int]$Dpi = 180", script)
        self.assertIn("$Dpi -lt 72 -or $Dpi -gt 400", script)
        self.assertIn("$MaxPixelsPerPage = 30000000", script)
        self.assertIn("$MaxPixelsTotal = 200000000", script)
        self.assertIn("$actualPixels = [int64]$targetWidth * [int64]$targetHeight", script)
        self.assertIn("before rendering", script)
        self.assertIn("Windows.Data.Pdf.PdfDocument", script)
        self.assertIn("total_pixels_nominal", script)

    def test_renderer_uses_owned_process_helpers_and_cancel_contract(self):
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("owned_process_group_kwargs", source)
        self.assertIn("terminate_owned_process_tree", source)
        self.assertIn("job_cancel_path", source)
        self.assertIn("import time", source)
        self.assertNotIn('__import__("time")', source)

    def test_render_rejects_unexpected_renderer_filename_even_if_png_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "docs").mkdir()
            (root / "out").mkdir()
            pdf = root / "docs" / "manual.pdf"
            pdf.write_bytes(b"%PDF-1.7\n")
            identity = {"bytes": pdf.stat().st_size, "sha256": "a" * 64, "signature": (pdf.stat().st_size, 1, 1, 1)}

            def fake_renderer(_path, output_dir, _start, _end, _dpi, **_kwargs):
                bad = output_dir / "unexpected.png"
                bad.write_bytes(
                    b"\x89PNG\r\n\x1a\n"
                    + struct.pack(">I", 13)
                    + b"IHDR"
                    + struct.pack(">II", 10, 10)
                    + b"\x08\x02\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                )
                return {
                    "source_units": 1,
                    "selected_range": {"start": 1, "end": 1, "unit": "page"},
                    "dpi_nominal": 180,
                    "total_pixels_nominal": 100,
                    "pages": [{"page": 1, "width_pixels_nominal": 10, "height_pixels_nominal": 10, "pixels_nominal": 100}],
                    "files": ["unexpected.png"],
                }

            with mock.patch.object(plugin, "_capture_source_identity", return_value=identity), \
                 mock.patch.object(plugin, "_assert_source_unchanged"), \
                 mock.patch.object(plugin, "_run_windows_renderer", side_effect=fake_renderer):
                with self.assertRaises(Exception):
                    plugin._render_pages(
                        root,
                        {"path": "docs/manual.pdf", "page_start": 1, "page_end": 1, "output_dir": "out/render", "dpi": 180, "make_zip": False},
                    )
            self.assertFalse((root / "out" / "render").exists())

    def test_png_dimensions_are_read_from_actual_ihdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.png"
            width, height = 1234, 567
            path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">II", width, height)
                + b"\x08\x02\x00\x00\x00"
                + b"\x00\x00\x00\x00"
            )
            self.assertEqual(plugin._read_png_dimensions(path), (width, height))

    def test_docs_state_process_separation_is_not_parser_memory_sandbox(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("not a parser-memory sandbox", readme)
        self.assertIn("PdfPig", readme)
        self.assertNotIn("`pypdf` may allocate", readme)


if __name__ == "__main__":
    unittest.main()
