from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from folderbridge_mcp.extensions import load_extension


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
        self.assertEqual(set(record.manifest.actions), {"status", "inspect_docx", "inspect_xlsx", "render"})
        self.assertTrue(record.manifest.actions["status"].read_only)
        self.assertTrue(record.manifest.actions["inspect_docx"].read_only)
        self.assertTrue(record.manifest.actions["inspect_xlsx"].read_only)
        self.assertFalse(record.manifest.actions["render"].read_only)
        self.assertEqual(record.manifest.actions["render"].authorization, "global")
        self.assertIn("process.execute:powershell.exe", record.manifest.permissions)
        self.assertNotIn("workspace.adapter", record.manifest.permissions)
        plugin_text = (OFFICE_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("owned_process_group_kwargs", plugin_text)
        self.assertIn("terminate_owned_process_tree", plugin_text)
        self.assertNotIn("subprocess.run(", plugin_text)

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


if __name__ == "__main__":
    unittest.main()
