from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import folderbridge_mcp.capabilities as capabilities_module
import folderbridge_mcp.workspace_validation as workspace_validation_module
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

    def test_globally_authorized_test_and_build_are_available_for_plain_workspace(self) -> None:
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["test", "build"])

        info = runtime.call("server_info", {})["structuredContent"]
        by_name = {item["name"]: item for item in info["capabilities"]}

        self.assertTrue(by_name["test"]["available"])
        self.assertEqual(by_name["test"]["provider"], "builtin-workspace-smoke")
        self.assertTrue(by_name["build"]["available"])
        self.assertEqual(by_name["build"]["provider"], "builtin-safe-build")

        test_result = runtime.call("run_capability", {"name": "test"})
        self.assertFalse(test_result["isError"])
        self.assertEqual(test_result["structuredContent"]["exit_code"], 0)
        self.assertEqual(test_result["structuredContent"]["provider"], "builtin-workspace-smoke")

        build_result = runtime.call("run_capability", {"name": "build"})
        self.assertFalse(build_result["isError"])
        self.assertEqual(build_result["structuredContent"]["exit_code"], 0)
        self.assertEqual(build_result["structuredContent"]["provider"], "builtin-safe-build")
        self.assertFalse(build_result["structuredContent"]["generated_artifacts"])
        self.assertFalse((self.root / ".folderbridge.json").exists())

    def test_static_html_uses_identity_build_and_builtin_smoke(self) -> None:
        (self.root / "index.html").write_text(
            "<!doctype html><html><body><script>const answer = 42;</script></body></html>\n",
            encoding="utf-8",
        )
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["test", "build"])

        test_result = runtime.call("run_capability", {"name": "test"})["structuredContent"]
        build_result = runtime.call("run_capability", {"name": "build"})["structuredContent"]

        self.assertEqual(test_result["exit_code"], 0)
        self.assertEqual(test_result["profile"], "static-web")
        self.assertEqual(test_result["checks"]["html_files"], 1)
        self.assertEqual(build_result["build_mode"], "identity")
        self.assertIn("index.html", build_result["deliverables"])

    def test_module_inline_script_is_checked_as_module_and_build_metadata_matches_stdout(self) -> None:
        (self.root / "index.html").write_text(
            '<script type="module">export const answer = 42;</script>\n',
            encoding="utf-8",
        )
        checked_suffixes: list[str] = []

        def fake_node_check(root: Path, node: str, target: Path, *, timeout: int = 10) -> str | None:
            checked_suffixes.append(target.suffix)
            return None

        with mock.patch.object(workspace_validation_module, "_node_executable", return_value="node"), mock.patch.object(
            workspace_validation_module, "_node_check", side_effect=fake_node_check
        ):
            test_result = workspace_validation_module.run_workspace_smoke(self.root)
            build_result = workspace_validation_module.run_safe_build(self.root)

        self.assertIn(".mjs", checked_suffixes)
        self.assertEqual(build_result["stdout_total_bytes"], len(build_result["stdout"].encode("utf-8")))
        self.assertEqual(test_result["exit_code"], 0)

    def test_builtin_smoke_accepts_utf8_bom_and_skips_diagnostic_outputs(self) -> None:
        (self.root / "legacy.json").write_bytes(b"\xef\xbb\xbf{\"ok\":true}\n")
        (self.root / "err.txt").write_bytes(b"\xff\xfelegacy diagnostic output")
        (self.root / "stderr.txt").write_bytes(b"\xff\xfelegacy diagnostic output")

        result = workspace_validation_module.run_workspace_smoke(self.root)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["checks"]["json_files"], 1)
        self.assertFalse(any("legacy.json" in item for item in result["issues"]))
        self.assertFalse(any("err.txt" in item for item in result["issues"]))
        self.assertFalse(any("stderr.txt" in item for item in result["issues"]))

    def test_builtin_smoke_fails_on_invalid_json_but_skips_sensitive_and_dependency_files(self) -> None:
        (self.root / "broken.json").write_text("{broken", encoding="utf-8")
        (self.root / ".env").write_bytes(b"\xff\xfeSECRET=hidden")
        deps = self.root / "node_modules"
        deps.mkdir()
        (deps / "bad.json").write_text("{also broken", encoding="utf-8")
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["test"])

        result = runtime.call("run_capability", {"name": "test"})

        self.assertFalse(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["exit_code"], 1)
        self.assertTrue(any("broken.json" in item for item in structured["issues"]))
        self.assertFalse(any(".env" in item for item in structured["issues"]))
        self.assertFalse(any("node_modules" in item for item in structured["issues"]))

    def test_explicit_project_scripts_still_win_over_builtin_providers(self) -> None:
        (self.root / "package.json").write_text(
            '{"scripts":{"test":"node --test","build":"node build.js"}}',
            encoding="utf-8",
        )
        runtime = ToolRuntime(self.root, load_config(self.root), capabilities=["test", "build"])

        info = runtime.call("server_info", {})["structuredContent"]
        by_name = {item["name"]: item for item in info["capabilities"]}

        self.assertEqual(by_name["test"]["provider"], "project-task")
        self.assertIn("npm", by_name["test"]["source"])
        self.assertEqual(by_name["build"]["provider"], "project-task")
        self.assertIn("npm", by_name["build"]["source"])

    def test_declared_packaging_scripts_are_discovered_generically(self) -> None:
        (self.root / "package.json").write_text(
            '{"scripts":{"package:windows":"node scripts/package-windows.cjs","package:android":"node scripts/package-android.cjs"}}',
            encoding="utf-8",
        )
        runtime = ToolRuntime(
            self.root,
            load_config(self.root),
            capabilities=["package-windows", "package-android"],
        )

        info = runtime.call("server_info", {})["structuredContent"]
        by_name = {item["name"]: item for item in info["capabilities"]}

        self.assertTrue(by_name["package-windows"]["available"])
        self.assertEqual(by_name["package-windows"]["provider"], "project-task")
        self.assertIn("npm run package:windows", by_name["package-windows"]["source"])
        self.assertTrue(by_name["package-android"]["available"])
        self.assertEqual(by_name["package-android"]["provider"], "project-task")
        self.assertIn("npm run package:android", by_name["package-android"]["source"])

    def test_declared_packaging_scripts_use_fixed_npm_run_argv(self) -> None:
        (self.root / "package.json").write_text(
            '{"scripts":{"package:windows":"node scripts/package-windows.cjs --dangerous-looking-body","package:android":"node scripts/package-android.cjs --dangerous-looking-body"}}',
            encoding="utf-8",
        )

        windows_task = capabilities_module._windows_package_task(self.root)
        android_task = capabilities_module._android_package_task(self.root)

        self.assertIsNotNone(windows_task)
        self.assertEqual(windows_task.argv, ("npm", "run", "package:windows"))
        self.assertIsNotNone(android_task)
        self.assertEqual(android_task.argv, ("npm", "run", "package:android"))

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
