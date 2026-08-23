from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import folderbridge_mcp.capabilities as capabilities_module
from folderbridge_mcp.capabilities import normalize_capability_names
from folderbridge_mcp.config import load_config
from folderbridge_mcp.security import ToolError
from folderbridge_mcp.tools import ToolRuntime


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builtin_document_and_image_tools_do_not_require_workspace_tasks(self) -> None:
        runtime = ToolRuntime(self.root, load_config(self.root))
        names = [tool["name"] for tool in runtime.list_tools()]

        self.assertIn("file_info", names)
        self.assertIn("pptx_inspect", names)
        self.assertIn("image_open", names)
        self.assertIn("extension", names)
        self.assertNotIn("run_task", names)
        self.assertNotIn("run_capability", names)

    def test_capability_can_become_available_after_workspace_was_added(self) -> None:
        runtime = ToolRuntime(
            self.root,
            load_config(self.root),
            capabilities=["package-windows"],
        )
        before = runtime.call("server_info", {})["structuredContent"]
        before_capability = before["capabilities"][0]
        self.assertEqual(before_capability["name"], "package-windows")
        self.assertFalse(before_capability["available"])

        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "build_windows.ps1").write_text("Write-Host 'build'\n", encoding="utf-8")

        after = runtime.call("server_info", {})["structuredContent"]
        after_capability = after["capabilities"][0]
        self.assertTrue(after_capability["available"])
        self.assertIn("build_windows.ps1", after_capability["source"])

    def test_global_test_capability_runs_without_workspace_task_config(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["test"])

        result = runtime.call("run_capability", {"name": "test"})

        self.assertFalse(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["capability"], "test")
        self.assertEqual(structured["exit_code"], 0)
        self.assertFalse((self.root / ".folderbridge.json").exists())

    def test_git_push_rejects_unsafe_repository_local_helpers(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git unavailable")
        subprocess.run([git, "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            [git, "remote", "add", "origin", "https://github.com/example/example.git"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            [git, "config", "--local", "credential.helper", "!malicious-helper"],
            cwd=self.root,
            check=True,
        )
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["git-push"])

        result = runtime.call("run_capability", {"name": "git-push"})

        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "UNSAFE_GIT_CONFIG")

    def test_git_inspection_fails_closed_when_bounded_runner_truncates_output(self) -> None:
        with mock.patch.object(
            capabilities_module,
            "run_task",
            return_value={
                "exit_code": 0,
                "timed_out": False,
                "stdout": "partial",
                "stderr": "",
                "truncated": True,
            },
        ):
            with self.assertRaises(ToolError) as raised:
                capabilities_module._git_text(self.root, "--version")
        self.assertEqual(raised.exception.code, "GIT_FAILED")

    def test_capability_names_are_canonical_and_reject_unknown_values(self) -> None:
        self.assertEqual(
            normalize_capability_names(["git-push", "test", "package-windows"]),
            ("test", "package-windows", "git-push"),
        )
        with self.assertRaises(ValueError):
            normalize_capability_names(["shell"])


if __name__ == "__main__":
    unittest.main()
