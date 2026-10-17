from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.config import (
    CONFIG_NAME,
    approve_config,
    config_is_trusted,
    load_config,
)
from folderbridge_mcp.node_task_registration import (
    NodeTaskRegistrationError,
    plan_node_task,
    install_node_task,
)


class NodeTaskRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.state = self.base / "state"
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.state), "XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "ok.cjs").write_text("console.log('NODE_TASK_SMOKE_OK')\n", encoding="utf-8")

    def test_plan_only_has_no_side_effects_and_fixed_node_argv(self):
        before = load_config(self.repo)
        plan = plan_node_task(self.repo, "judge-code-closure", "scripts/ok.cjs", 300)
        self.assertEqual(plan.old_config_sha256, before.sha256)
        self.assertFalse((self.repo / CONFIG_NAME).exists())
        self.assertEqual(plan.task.argv, ("node", "scripts/ok.cjs"))
        self.assertEqual(plan.task.timeout_seconds, 300)
        self.assertEqual(plan.script_path, "scripts/ok.cjs")
        self.assertRegex(plan.new_config_sha256, "^[0-9a-f]{64}$")
        self.assertFalse(config_is_trusted(self.repo, before))

    def test_install_preserves_approved_old_task_and_revokes_old_config_trust(self):
        initial = {
            "version": 1,
            "tasks": {
                "old-check": {"argv": ["python", "-m", "unittest"], "timeout_seconds": 150}
            },
        }
        (self.repo / CONFIG_NAME).write_text(json.dumps(initial), encoding="utf-8")
        approve_config(self.repo, load_config(self.repo))
        self.assertTrue(config_is_trusted(self.repo, load_config(self.repo)))
        p = plan_node_task(self.repo, "node-test", "scripts/ok.cjs", 120)
        result = install_node_task(p)
        self.assertEqual(result.sha256, p.new_config_sha256)
        self.assertEqual(result.tasks["old-check"].argv, ("python", "-m", "unittest"))
        self.assertEqual(result.tasks["node-test"].argv, ("node", "scripts/ok.cjs"))
        self.assertFalse(config_is_trusted(self.repo, result), "adding a task cannot inherit prior approval")
        approve_config(self.repo, result)
        self.assertTrue(config_is_trusted(self.repo, load_config(self.repo)))

    def test_unapproved_node_task_cannot_execute_until_local_approval(self):
        from folderbridge_mcp.tools import ToolRuntime

        plan = plan_node_task(self.repo, "safe-node", "scripts/ok.cjs", 30)
        installed = install_node_task(plan)
        runtime = ToolRuntime(self.repo, load_config(self.repo), allow_tasks=True)
        denied = runtime.call("run_task", {"name": "safe-node"})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "CONFIG_NOT_TRUSTED")
        runtime.close()

        approve_config(self.repo, installed)
        if shutil.which("node") is None:
            self.skipTest("Native Node executable not present")
        runtime = ToolRuntime(self.repo, load_config(self.repo), allow_tasks=True)
        try:
            result = runtime.call("run_task", {"name": "safe-node"})
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["structuredContent"]["exit_code"], 0)
            self.assertIn("NODE_TASK_SMOKE_OK", result["structuredContent"]["stdout"])
        finally:
            runtime.close()

    def test_race_or_manual_config_edit_is_not_overwritten(self):
        p = plan_node_task(self.repo, "node-check", "scripts/ok.cjs", 60)
        (self.repo / CONFIG_NAME).write_text('{"version":1,"tasks":{}}\n', encoding="utf-8")
        with self.assertRaises(NodeTaskRegistrationError):
            install_node_task(p)
        self.assertEqual(load_config(self.repo).tasks, {})

    def test_reject_script_outside_workspace_or_traversal(self):
        elsewhere = self.base / "outside.js"
        elsewhere.write_text("console.log('not allowed')", encoding="utf-8")
        for value in ["../outside.js", str(elsewhere), "scripts/../scripts/ok.cjs"]:
            with self.subTest(value=value), self.assertRaises(NodeTaskRegistrationError):
                plan_node_task(self.repo, "escape", value, 10)

    def test_reject_disallowed_path_and_not_node_source(self):
        (self.repo / "scripts" / "not-node.py").write_text("print(1)", encoding="utf-8")
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "unsafe.js").write_text("console.log(1)", encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "dep.js").write_text("console.log(1)", encoding="utf-8")
        for value in ["scripts/not-node.py", ".git/unsafe.js", "node_modules/dep.js", "scripts/missing.js"]:
            with self.subTest(value=value), self.assertRaises(NodeTaskRegistrationError):
                plan_node_task(self.repo, "reject", value, 10)

    def test_symlinks_are_rejected_including_in_parent_path(self):
        link_file = self.repo / "scripts" / "alias.cjs"
        try:
            link_file.symlink_to(self.repo / "scripts" / "ok.cjs")
        except OSError:
            self.skipTest("Creating symlink requires OS permission")
        with self.assertRaises(NodeTaskRegistrationError):
            plan_node_task(self.repo, "alias", "scripts/alias.cjs", 10)
        link_dir = self.repo / "links"
        link_dir.symlink_to(self.repo / "scripts", target_is_directory=True)
        with self.assertRaises(NodeTaskRegistrationError):
            plan_node_task(self.repo, "parent-alias", "links/ok.cjs", 10)

    def test_name_time_collision_and_config_capacity(self):
        for args in [
            ("with space", "scripts/ok.cjs", 30),
            ("a", "scripts/ok.cjs", 0),
            ("b", "scripts/ok.cjs", 7201),
            ("c", "scripts/ok.cjs", True),
        ]:
            with self.subTest(args=args), self.assertRaises(NodeTaskRegistrationError):
                plan_node_task(self.repo, *args)
        old = {"version": 1, "tasks": {"duplicated": {"argv": ["node", "scripts/ok.cjs"], "timeout_seconds": 15}}}
        (self.repo / CONFIG_NAME).write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaises(NodeTaskRegistrationError):
            plan_node_task(self.repo, "duplicated", "scripts/ok.cjs", 30)
        old["tasks"] = {f"task{i}": {"argv": ["node", "scripts/ok.cjs"], "timeout_seconds": 10} for i in range(20)}
        (self.repo / CONFIG_NAME).write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaises(NodeTaskRegistrationError):
            plan_node_task(self.repo, "twenty-one", "scripts/ok.cjs", 30)

    def test_cli_requires_explicit_local_approval_and_preserves_tasks(self):
        from folderbridge_mcp.cli import main

        argv = ["node-task-add", "--workspace", str(self.repo),
                "--name", "node-lint", "--script", "scripts/ok.cjs",
                "--timeout", "90"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(argv), 0)
        self.assertIn("node-lint", output.getvalue())
        self.assertFalse(config_is_trusted(self.repo, load_config(self.repo)))
        self.assertIn("node-lint", load_config(self.repo).tasks)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["approve", "--workspace", str(self.repo)]), 0)
        self.assertTrue(config_is_trusted(self.repo, load_config(self.repo)))

    def test_gui_has_local_node_registration_entry_and_never_remote_node_run(self):
        gui = (Path(__file__).resolve().parents[1] / "folderbridge_mcp" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("_register_node_task", gui)
        self.assertIn("plan_node_task", gui)
        self.assertIn("install_node_task", gui)
        self.assertIn("approve_config", gui)
        self.assertIn("注册 Node 任务", gui)
        tools = (Path(__file__).resolve().parents[1] / "folderbridge_mcp" / "tools.py").read_text(encoding="utf-8")
        self.assertNotIn('"run_node"', tools)


if __name__ == "__main__":
    unittest.main()
