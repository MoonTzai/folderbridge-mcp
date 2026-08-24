from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-windows.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_windows_release_is_release_commit_driven_version_locked_and_uploads_assets(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("branches:", text)
        self.assertIn("- main", text)
        self.assertIn("Release FolderBridge", text)
        self.assertIn("github.event.head_commit.message", text)
        self.assertIn("contents: write", text)
        self.assertIn("runs-on: windows-2022", text)
        self.assertIn("python-version: \"3.11\"", text)
        self.assertIn("pyproject.toml", text)
        self.assertIn("python -m unittest discover", text)
        self.assertIn("scripts/build_windows.ps1", text)
        self.assertIn("scripts/verify_windows_bundle.py", text)
        self.assertIn("git tag -a", text)
        self.assertIn("git push origin", text)
        self.assertIn("gh release create", text)
        self.assertIn("--verify-tag", text)
        self.assertIn("--latest", text)
        self.assertIn("release/windows-x64/FolderBridge.exe", text)
        self.assertIn("release/windows-x64/FolderBridge.exe.sha256", text)

    def test_release_workflow_has_no_manual_or_tag_input_surface(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("workflow_dispatch", text)
        self.assertNotIn("tags:", text)
        self.assertNotIn("inputs:", text)


if __name__ == "__main__":
    unittest.main()
