from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import load_extension


ROOT = Path(__file__).resolve().parents[1]
OFFICE_DIR = ROOT / "extensions" / "office"


def load_plugin():
    spec = importlib.util.spec_from_file_location("folderbridge_v35_office", OFFICE_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V35OfficePhase0AcceptanceTests(unittest.TestCase):
    def test_render_declares_truthful_tree_and_optional_archive_claims(self) -> None:
        record = load_extension(OFFICE_DIR, bundled=True)
        render = record.manifest.actions["render"]
        self.assertEqual(
            render.mutation_scope.describe(),
            {
                "mode": "paths",
                "explicit": True,
                "claims": [
                    {"kind": "tree", "param": "output_dir"},
                    {"kind": "exact", "param": "archive_path", "optional": True},
                ],
            },
        )
        properties = render.input_schema["properties"]
        self.assertIn("archive_path", properties)
        self.assertEqual(properties["archive_path"].get("type"), "string")

    def test_office_protocol_scripts_force_no_bom_utf8_stdout_and_stderr(self) -> None:
        marker = "[Text.UTF8Encoding]::new($false)"
        for name in ("office.ps1", "word_export.ps1", "pdf_render.ps1"):
            text = (OFFICE_DIR / name).read_text(encoding="utf-8")
            self.assertIn(marker, text, name)
            self.assertIn("[Console]::OutputEncoding", text, name)
            self.assertIn("[Console]::InputEncoding", text, name)

    def test_python_protocol_decode_is_strict_and_rejects_invalid_utf8(self) -> None:
        plugin = load_plugin()
        self.assertEqual(plugin._decode_protocol(b'{"name":"\xe6\x97\xa5\xe6\x9c\xac\xf0\x9f\x8c\x9f"}', stream="stdout"), '{"name":"日本🌟"}')
        with self.assertRaisesRegex(RuntimeError, "UTF-8"):
            plugin._decode_protocol(b"\xff\xfe\xfa", stream="stdout")
        plugin_text = (OFFICE_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertNotIn('decode("utf-8-sig", errors="replace")', plugin_text)

    def test_archive_contract_defaults_in_tree_and_validates_explicit_target(self) -> None:
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "out").mkdir()
            source = root / "source.pptx"
            source.write_bytes(b"source")

            default_target = plugin._resolve_archive_target(
                root,
                source,
                root / "out",
                make_zip=True,
                archive_path=None,
            )
            self.assertEqual(default_target, root / "out" / "render.zip")

            sibling = plugin._resolve_archive_target(
                root,
                source,
                root / "out",
                make_zip=True,
                archive_path="out.zip",
            )
            self.assertEqual(sibling, root / "out.zip")

            with self.assertRaisesRegex(RuntimeError, "archive_path"):
                plugin._resolve_archive_target(
                    root,
                    source,
                    root / "out",
                    make_zip=False,
                    archive_path="out.zip",
                )
            with self.assertRaisesRegex(RuntimeError, "source"):
                plugin._resolve_archive_target(
                    root,
                    source,
                    root / "out",
                    make_zip=True,
                    archive_path="source.pptx",
                )


if __name__ == "__main__":
    unittest.main()
