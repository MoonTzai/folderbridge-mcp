from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.config import CONFIG_NAME
from folderbridge_mcp.security import ToolError, Workspace, sha256_bytes


class WorkspaceSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rejects_traversal_and_absolute_paths(self) -> None:
        with self.assertRaisesRegex(ToolError, "relative path"):
            self.workspace.read_text("../outside.txt")
        with self.assertRaises(ToolError):
            self.workspace.read_text(str((self.root / "file.txt").resolve()))
        with self.assertRaisesRegex(ToolError, "NUL"):
            self.workspace.read_text("bad\x00name")

    def test_hides_sensitive_and_ignored_paths(self) -> None:
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.root / ".api-config.json").write_text('{"apiKey":"secret"}', encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "bad.js").write_text("bad", encoding="utf-8")
        listing = self.workspace.list_files()
        self.assertEqual(listing["files"], ["src/app.py"])
        with self.assertRaisesRegex(ToolError, "Credential-like"):
            self.workspace.read_text(".env")
        with self.assertRaisesRegex(ToolError, "Credential-like"):
            self.workspace.read_text(".api-config.json")

    def test_rejects_symlinks_when_supported(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ToolError, "Symlinks"):
            self.workspace.read_text("link.txt")

    def test_exact_edit_requires_current_hash(self) -> None:
        path = self.root / "app.py"
        original = b"value = 1\n"
        path.write_bytes(original)
        with self.assertRaisesRegex(ToolError, "changed"):
            self.workspace.edit_file(
                "app.py",
                expected_sha256="0" * 64,
                replacements=[{"old": "1", "new": "2"}],
                create_content=None,
            )
        result = self.workspace.edit_file(
            "app.py",
            expected_sha256=sha256_bytes(original),
            replacements=[{"old": "value = 1", "new": "value = 2"}],
            create_content=None,
        )
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 2\n")
        self.assertFalse(result["created"])

    def test_utf8_pagination_does_not_split_a_code_point(self) -> None:
        (self.root / "unicode.txt").write_text("A你B", encoding="utf-8")
        first = self.workspace.read_text("unicode.txt", limit=2)
        self.assertEqual(first["text"], "A")
        self.assertEqual(first["next_offset"], 1)
        second = self.workspace.read_text("unicode.txt", offset=first["next_offset"], limit=4)
        self.assertEqual(second["text"], "你B")

    def test_git_diff_output_is_bounded(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git unavailable")
        path = self.root / "large.txt"
        path.write_text("".join(f"old-{index:05d}\n" for index in range(10000)), encoding="utf-8")
        subprocess.run([git, "init", "-q"], cwd=self.root, check=True)
        subprocess.run([git, "add", "large.txt"], cwd=self.root, check=True)
        subprocess.run(
            [git, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
            cwd=self.root,
            check=True,
        )
        path.write_text("".join(f"new-{index:05d}\n" for index in range(10000)), encoding="utf-8")
        result = self.workspace.git_view("diff")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].encode("utf-8")), 2 * 64 * 1024)

    def test_rejects_ambiguous_replacement(self) -> None:
        path = self.root / "app.py"
        original = b"x x\n"
        path.write_bytes(original)
        with self.assertRaisesRegex(ToolError, "exactly one"):
            self.workspace.edit_file(
                "app.py",
                expected_sha256=sha256_bytes(original),
                replacements=[{"old": "x", "new": "y"}],
                create_content=None,
            )

    def test_can_create_but_cannot_change_policy_config(self) -> None:
        result = self.workspace.edit_file(
            "src/new.py",
            expected_sha256=None,
            replacements=None,
            create_content="answer = 42\n",
        )
        self.assertTrue(result["created"])
        with self.assertRaisesRegex(ToolError, "cannot be changed"):
            self.workspace.edit_file(
                CONFIG_NAME,
                expected_sha256=None,
                replacements=None,
                create_content='{"version": 1, "tasks": {}}\n',
            )


if __name__ == "__main__":
    unittest.main()
