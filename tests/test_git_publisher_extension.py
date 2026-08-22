from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import load_extension


ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "extensions" / "git-publisher"


def load_plugin():
    spec = importlib.util.spec_from_file_location("folderbridge_test_git_publisher", EXT_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [shutil.which("git.exe") or shutil.which("git") or "git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


class GitPublisherManifestTests(unittest.TestCase):
    def test_manifest_is_explicit_and_does_not_accept_tokens(self) -> None:
        record = load_extension(EXT_DIR, bundled=True)
        self.assertEqual(record.manifest.extension_id, "git-publisher")
        self.assertEqual(set(record.manifest.actions), {"status", "connect", "commit", "push"})
        self.assertTrue(record.manifest.actions["status"].read_only)
        self.assertEqual(record.manifest.actions["status"].authorization, "none")
        for name in ("connect", "commit", "push"):
            self.assertFalse(record.manifest.actions[name].read_only)
            self.assertEqual(record.manifest.actions[name].authorization, "global")
        self.assertIn("git.commit-selected-files", record.manifest.permissions)
        self.assertIn("git.push-current-branch", record.manifest.permissions)
        self.assertIn("github.web-auth", record.manifest.permissions)
        self.assertIn("process.execute:git.exe", record.manifest.permissions)
        for action in record.manifest.actions.values():
            properties = action.input_schema.get("properties", {})
            self.assertTrue({"token", "password", "pat"}.isdisjoint({str(key).lower() for key in properties}))

    def test_origin_validator_rejects_embedded_credentials_and_non_github(self) -> None:
        plugin = load_plugin()
        self.assertEqual(
            plugin._validate_origin("https://github.com/MoonTzai/folderbridge-mcp.git"),
            ("MoonTzai", "folderbridge-mcp", "https://github.com/MoonTzai/folderbridge-mcp.git"),
        )
        with self.assertRaises(RuntimeError):
            plugin._validate_origin("https://secret@github.com/MoonTzai/folderbridge-mcp.git")
        with self.assertRaises(RuntimeError):
            plugin._validate_origin("https://example.com/MoonTzai/folderbridge-mcp.git")


@unittest.skipUnless(sys.platform == "win32" and shutil.which("git.exe"), "Git Publisher runtime tests require Git for Windows")
class GitPublisherRuntimeTests(unittest.TestCase):
    def make_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "FolderBridge Test")
        git(root, "config", "user.email", "folderbridge-test@example.invalid")
        git(root, "remote", "add", "origin", "https://github.com/example/folderbridge-test.git")
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(root, "add", "--", "tracked.txt")
        git(root, "commit", "-m", "initial")
        return temp, root

    def test_commit_includes_only_explicit_files(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        before = git(root, "rev-parse", "HEAD")
        (root / "tracked.txt").write_text("two\n", encoding="utf-8")
        (root / "unwanted.txt").write_text("do not commit\n", encoding="utf-8")
        result = plugin.handle(
            "commit",
            {"paths": ["tracked.txt"], "message": "Update tracked file"},
            {"workspace_root": str(root), "workspace_read_only": False},
        )
        after = git(root, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertEqual(result["paths"], ["tracked.txt"])
        self.assertIn("unwanted.txt", [item["path"] for item in result["remaining_changes"]])
        committed = git(root, "show", "--pretty=", "--name-only", "HEAD").splitlines()
        self.assertEqual(committed, ["tracked.txt"])
        self.assertEqual(git(root, "diff", "--cached", "--name-only"), "")

    def test_commit_refuses_preexisting_staged_changes(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        (root / "tracked.txt").write_text("two\n", encoding="utf-8")
        (root / "other.txt").write_text("staged\n", encoding="utf-8")
        git(root, "add", "--", "other.txt")
        with self.assertRaisesRegex(RuntimeError, "unrelated staged changes"):
            plugin.handle(
                "commit",
                {"paths": ["tracked.txt"], "message": "Should not happen"},
                {"workspace_root": str(root), "workspace_read_only": False},
            )
        self.assertIn("other.txt", git(root, "diff", "--cached", "--name-only"))

    def test_commit_rejects_sensitive_or_escaping_paths(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            plugin.handle(
                "commit",
                {"paths": [".env"], "message": "No"},
                {"workspace_root": str(root), "workspace_read_only": False},
            )
        with self.assertRaises(RuntimeError):
            plugin.handle(
                "commit",
                {"paths": ["../outside.txt"], "message": "No"},
                {"workspace_root": str(root), "workspace_read_only": False},
            )


if __name__ == "__main__":
    unittest.main()
