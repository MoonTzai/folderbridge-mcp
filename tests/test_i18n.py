from __future__ import annotations

import ast
import unittest
from pathlib import Path

from folderbridge_mcp.i18n import contains_cjk, normalize_language, translate_text


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE_FILES = (
    ROOT / "folderbridge_mcp" / "gui.py",
    ROOT / "folderbridge_mcp" / "launcher_backend.py",
    ROOT / "folderbridge_mcp" / "capabilities.py",
    ROOT / "folderbridge_mcp" / "setup_guide.py",
)


def source_chinese_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and contains_cjk(node.value)
        and node.value.strip()
    }


class I18nTests(unittest.TestCase):
    def test_language_codes_are_strict_and_default_to_chinese(self) -> None:
        self.assertEqual(normalize_language("zh"), "zh")
        self.assertEqual(normalize_language("en"), "en")
        self.assertEqual(normalize_language("EN"), "zh")
        self.assertEqual(normalize_language(None), "zh")

    def test_user_paths_are_not_word_by_word_translated(self) -> None:
        raw = r"C:\\工作区\\测试\\权限.txt"
        self.assertEqual(translate_text(raw, "en"), raw)
        self.assertEqual(translate_text("工作区", "en"), "Workspace")
        self.assertEqual(translate_text("权限", "en"), "Permissions")

    def test_all_launcher_chinese_literals_have_complete_english_rendering(self) -> None:
        uncovered: list[tuple[str, str]] = []
        for path in UI_SOURCE_FILES:
            for text in source_chinese_literals(path):
                if text == "中文 / EN":
                    continue
                rendered = translate_text(text, "en")
                if contains_cjk(rendered):
                    uncovered.append((path.name, text))
        escaped = "\n".join(
            f"{name}: {text.encode('unicode_escape').decode('ascii')}"
            for name, text in uncovered
        )
        self.assertEqual(uncovered, [], "Untranslated launcher literals:\n" + escaped)


if __name__ == "__main__":
    unittest.main()
