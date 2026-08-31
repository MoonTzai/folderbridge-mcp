from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from folderbridge_mcp.extensions import load_extension
from folderbridge_mcp.process_control import owned_process_group_kwargs


ROOT = Path(__file__).resolve().parents[1]
OFFICE_DIR = ROOT / "extensions" / "office"


def load_plugin():
    spec = importlib.util.spec_from_file_location("folderbridge_test_office", OFFICE_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text.encode("utf-8"))


class OfficeExtensionTests(unittest.TestCase):
    def test_bundled_manifest_declares_safe_office_actions(self) -> None:
        record = load_extension(OFFICE_DIR, bundled=True)
        self.assertEqual(record.manifest.extension_id, "office")
        self.assertEqual(record.manifest.version, "1.1.4")
        self.assertEqual(set(record.manifest.actions), {"status", "inspect_docx", "inspect_xlsx", "render"})
        self.assertTrue(record.manifest.actions["status"].read_only)
        self.assertTrue(record.manifest.actions["inspect_docx"].read_only)
        self.assertTrue(record.manifest.actions["inspect_xlsx"].read_only)
        self.assertFalse(record.manifest.actions["render"].read_only)
        self.assertEqual(record.manifest.actions["render"].run_mode, "job")
        self.assertEqual(record.manifest.actions["render"].authorization, "global")
        self.assertIn("process.execute:powershell.exe", record.manifest.permissions)
        self.assertNotIn("workspace.adapter", record.manifest.permissions)
        plugin_text = (OFFICE_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("owned_process_group_kwargs", plugin_text)
        self.assertIn("terminate_owned_process_tree", plugin_text)
        self.assertNotIn("subprocess.run(", plugin_text)
        office_text = (OFFICE_DIR / "office.ps1").read_text(encoding="utf-8")
        word_export_text = (OFFICE_DIR / "word_export.ps1").read_text(encoding="utf-8")
        pdf_render_text = (OFFICE_DIR / "pdf_render.ps1").read_text(encoding="utf-8")
        self.assertNotIn("$doc.ComputeStatistics(2)", word_export_text)
        self.assertIn("$doc.ExportAsFixedFormat($PdfPath, 17, $false, 0, 0, 1, 1", word_export_text)
        self.assertIn("Get-NewWordProcessId", word_export_text)
        self.assertIn("BaselinePids", word_export_text)
        self.assertIn("RenderToStreamAsync", pdf_render_text)
        self.assertNotIn("New-Object -ComObject Word.Application", pdf_render_text)
        self.assertIn("Render-PowerPoint", office_text)
        self.assertIn("Render-Excel", office_text)

    def test_docx_inspection_reads_paragraphs_tables_and_structure(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.docx"
            write_zip(path, {
                "word/document.xml": """<?xml version='1.0' encoding='UTF-8'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
  <w:body>
    <w:p><w:pPr><w:pStyle w:val='Heading1'/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p>
    <w:p><w:r><w:t>Hello</w:t><w:tab/></w:r><w:r><w:t>world</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:sectPr><w:pgSz w:w='12240' w:h='15840'/><w:pgMar w:top='1440' w:right='1440' w:bottom='1440' w:left='1440'/></w:sectPr>
  </w:body>
</w:document>""",
                "word/_rels/document.xml.rels": """<?xml version='1.0' encoding='UTF-8'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
  <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink' Target='https://example.com' TargetMode='External'/>
</Relationships>""",
                "word/header1.xml": """<w:hdr xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>""",
                "word/footer1.xml": """<w:ftr xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:p><w:r><w:t>Footer</w:t></w:r></w:p></w:ftr>""",
                "word/styles.xml": """<w:styles xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:style w:type='paragraph' w:styleId='Heading1'/></w:styles>""",
                "word/media/image1.png": "not-a-real-image",
            })
            result = plugin.handle(
                "inspect_docx",
                {"path": "sample.docx", "max_items": 100},
                {"workspace_root": str(root), "workspace_read_only": True},
            )
            self.assertEqual(result["format"], "docx")
            self.assertGreaterEqual(result["paragraph_count"], 4)
            self.assertEqual(result["paragraphs"][0]["style"], "Heading1")
            self.assertIn("Hello\tworld", [item["text"] for item in result["paragraphs"]])
            self.assertEqual(result["table_count"], 1)
            self.assertEqual(result["tables"][0]["rows"][0], ["A", "B"])
            self.assertEqual(result["section_count"], 1)
            self.assertEqual(result["media_count"], 1)
            self.assertEqual(result["headers"][0]["text"], "Header")
            self.assertEqual(result["footers"][0]["text"], "Footer")
            self.assertEqual(result["hyperlinks"][0]["target"], "https://example.com")

    def test_xlsx_inspection_reads_values_formulas_merges_and_ranges(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.xlsx"
            write_zip(path, {
                "xl/workbook.xml": """<?xml version='1.0' encoding='UTF-8'?>
<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
  <sheets><sheet name='Data' sheetId='1' r:id='rId1'/></sheets>
  <definedNames><definedName name='Answer'>Data!$B$1</definedName></definedNames>
  <calcPr calcId='191029' calcMode='auto'/>
</workbook>""",
                "xl/_rels/workbook.xml.rels": """<?xml version='1.0' encoding='UTF-8'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
  <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' Target='worksheets/sheet1.xml'/>
</Relationships>""",
                "xl/sharedStrings.xml": """<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' count='1' uniqueCount='1'><si><t>Hello</t></si></sst>""",
                "xl/worksheets/sheet1.xml": """<?xml version='1.0' encoding='UTF-8'?>
<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>
  <dimension ref='A1:C3'/>
  <cols><col min='3' max='3' width='20' hidden='1'/></cols>
  <sheetData>
    <row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1'><f>SUM(1,2)</f><v>3</v></c></row>
    <row r='2' hidden='1'><c r='A2' t='inlineStr'><is><t>Inline</t></is></c></row>
  </sheetData>
  <mergeCells count='1'><mergeCell ref='A3:B3'/></mergeCells>
</worksheet>""",
            })
            result = plugin.handle(
                "inspect_xlsx",
                {"path": "sample.xlsx", "sheet": "Data", "cell_range": "A1:B1", "max_items": 100},
                {"workspace_root": str(root), "workspace_read_only": True},
            )
            self.assertEqual(result["format"], "xlsx")
            self.assertEqual(result["sheet_count"], 1)
            sheet = result["sheets"][0]
            self.assertEqual(sheet["dimension"], "A1:C3")
            cells = {item["address"]: item for item in sheet["cells"]}
            self.assertEqual(cells["A1"]["value"], "Hello")
            self.assertEqual(cells["B1"]["formula"], "SUM(1,2)")
            self.assertEqual(cells["B1"]["value"], 3)
            self.assertNotIn("A2", cells)
            self.assertEqual(sheet["formula_count_in_selected_range"], 1)
            self.assertIn("A3:B3", sheet["merged_ranges"])
            self.assertEqual(result["defined_names"][0]["name"], "Answer")
            self.assertEqual(result["calculation"]["calcMode"], "auto")

    def test_paths_cannot_escape_workspace(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                plugin.handle(
                    "inspect_docx",
                    {"path": "../outside.docx"},
                    {"workspace_root": str(root), "workspace_read_only": True},
                )

    @unittest.skipUnless(shutil.which("powershell.exe"), "PowerShell unavailable")
    def test_office_powershell_script_parses_before_runtime_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    shutil.which("powershell.exe") or "powershell.exe",
                    "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass",
                    "-File", str(OFFICE_DIR / "office.ps1"),
                    "-InputPath", str(Path(directory) / "missing.pptx"),
                    "-OutputDir", directory,
                    "-TempDir", directory,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            stderr = completed.stderr.decode("utf-8-sig", errors="replace")
            self.assertNotIn("ParserError", stderr)


    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_winrt_pdf_async_bridge_completes_in_sta(self) -> None:
        def write_minimal_pdf(path: Path) -> None:
            objects = [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
            ]
            data = bytearray(b"%PDF-1.4\n")
            offsets = [0]
            for index, body in enumerate(objects, start=1):
                offsets.append(len(data))
                data.extend(f"{index} 0 obj\n".encode("ascii"))
                data.extend(body)
                data.extend(b"\nendobj\n")
            xref = len(data)
            data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
            data.extend(b"0000000000 65535 f \n")
            for offset in offsets[1:]:
                data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
            data.extend(
                (
                    f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                    f"startxref\n{xref}\n%%EOF\n"
                ).encode("ascii")
            )
            path.write_bytes(data)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "probe.pdf"
            script_path = root / "probe.ps1"
            write_minimal_pdf(pdf_path)
            script_path.write_text(
                r'''$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
$generic = $methods | Where-Object { $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
function Await-Operation($Operation, [Type]$ResultType) {
    $method = $generic.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($args[0])) ([Windows.Storage.StorageFile])
$pdf = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
[Console]::Out.WriteLine([int]$pdf.PageCount)
''',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    shutil.which("powershell.exe") or "powershell.exe",
                    "-NoProfile", "-NonInteractive", "-Sta", "-ExecutionPolicy", "Bypass",
                    "-File", str(script_path), str(pdf_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8-sig", errors="replace"))
            self.assertEqual(completed.stdout.decode("utf-8-sig", errors="replace").strip(), "1")


    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_winrt_pdf_render_to_stream_apartment_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "probe.pdf"
            objects = [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
            ]
            data = bytearray(b"%PDF-1.4\n")
            offsets = [0]
            for index, body in enumerate(objects, start=1):
                offsets.append(len(data))
                data.extend(f"{index} 0 obj\n".encode("ascii"))
                data.extend(body)
                data.extend(b"\nendobj\n")
            xref = len(data)
            data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
            data.extend(b"0000000000 65535 f \n")
            for offset in offsets[1:]:
                data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
            data.extend((f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode("ascii"))
            pdf_path.write_bytes(data)
            script_path = root / "render-probe.ps1"
            script_path.write_text(
                r'''$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
$generic = $methods | Where-Object { $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
$action = $methods | Where-Object { $_.Name -eq "AsTask" -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
function Await-Operation($Operation, [Type]$ResultType) {
    $method = $generic.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
function Await-Action($Operation) {
    $task = $action.Invoke($null, @($Operation))
    $task.Wait()
}
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]
$file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($args[0])) ([Windows.Storage.StorageFile])
$pdf = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$page = $pdf.GetPage([uint32]0)
[System.IO.File]::WriteAllBytes($args[1], [byte[]]@())
$pngStorage = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($args[1])) ([Windows.Storage.StorageFile])
$stream = Await-Operation ($pngStorage.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
$options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
$options.DestinationWidth = [uint32]800
$options.DestinationHeight = [uint32]1035
Await-Action ($page.RenderToStreamAsync($stream, $options))
$stream.Dispose()
$page.Dispose()
[Console]::Out.WriteLine((Get-Item -LiteralPath $args[1]).Length)
''',
                encoding="utf-8",
            )

            def run_apartment(flag: str) -> tuple[str, str]:
                png_path = root / f"{flag[1:].lower()}.png"
                try:
                    completed = subprocess.run(
                        [
                            shutil.which("powershell.exe") or "powershell.exe",
                            "-NoProfile", "-NonInteractive", flag, "-ExecutionPolicy", "Bypass",
                            "-File", str(script_path), str(pdf_path), str(png_path),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=15,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    return "timeout", ""
                stderr = completed.stderr.decode("utf-8-sig", errors="replace")
                if completed.returncode != 0:
                    return "error", stderr
                return "ok", completed.stdout.decode("utf-8-sig", errors="replace").strip()

            sta = run_apartment("-Sta")
            mta = run_apartment("-Mta")
            self.assertEqual(sta[0], "ok", f"STA={sta!r}; MTA={mta!r}")
            self.assertEqual(mta[0], "ok", f"STA={sta!r}; MTA={mta!r}")
            self.assertGreater(int(sta[1]), 0)
            self.assertGreater(int(mta[1]), 0)


    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_minimal_word_docx_renders_through_plugin_without_orphan_word(self) -> None:
        plugin = load_plugin()

        def word_pids() -> set[int]:
            completed = subprocess.run(
                [
                    shutil.which("powershell.exe") or "powershell.exe",
                    "-NoProfile", "-NonInteractive", "-Command",
                    "@(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) | ConvertTo-Json -Compress",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8-sig", errors="replace"))
            raw = completed.stdout.decode("utf-8-sig", errors="replace").strip()
            if not raw:
                return set()
            values = json.loads(raw)
            if isinstance(values, int):
                return {values}
            return {int(value) for value in values}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx_path = root / "minimal.docx"
            output_dir = root / "out"
            state_dir = root / "state"
            output_dir.mkdir()
            state_dir.mkdir()
            write_zip(docx_path, {
                "[Content_Types].xml": """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>
  <Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>
  <Default Extension='xml' ContentType='application/xml'/>
  <Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>
</Types>""",
                "_rels/.rels": """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
  <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/>
</Relationships>""",
                "word/document.xml": """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
  <w:body>
    <w:p><w:r><w:t>Hello FolderBridge</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w='11906' w:h='16838'/><w:pgMar w:top='1440' w:right='1440' w:bottom='1440' w:left='1440'/></w:sectPr>
  </w:body>
</w:document>""",
            })
            before = word_pids()
            result = plugin.handle(
                "render",
                {
                    "path": "minimal.docx",
                    "output_dir": "out",
                    "page_start": 1,
                    "page_end": 1,
                    "width": 800,
                    "overwrite": True,
                    "make_zip": False,
                },
                {
                    "workspace_root": str(root),
                    "workspace_read_only": False,
                    "state_dir": str(state_dir),
                    "job_cancel_path": None,
                },
            )
            after = word_pids()
            self.assertEqual(after, before)
            self.assertEqual(result["application"], "Word")
            self.assertEqual(result["source_units"], 1)
            self.assertEqual(result["selected_range"], {"start": 1, "end": 1, "unit": "page"})
            self.assertEqual([item["path"] for item in result["files"]], ["out/P0001.png"])
            png_path = output_dir / "P0001.png"
            self.assertTrue(png_path.is_file())
            self.assertGreater(png_path.stat().st_size, 0)
            png_bytes = png_path.read_bytes()
            self.assertEqual(png_bytes[:8], b"\x89PNG\r\n\x1a\n")
            pixel_width, pixel_height = struct.unpack(">II", png_bytes[16:24])
            self.assertEqual(pixel_width, 800)
            self.assertGreater(pixel_height, 0)


if __name__ == "__main__":
    unittest.main()
