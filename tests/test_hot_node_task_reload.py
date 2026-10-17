from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.config import CONFIG_NAME, approve_config, load_config
from folderbridge_mcp.node_task_registration import install_node_task, plan_node_task
from folderbridge_mcp.tools import ToolRuntime


class HotNodeTaskReloadTests(unittest.TestCase):
    """No subprocess or network requests. All task dispatches are stubbed."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "judge"
        self.repo.mkdir()
        scripts = self.repo / "scripts"
        scripts.mkdir()
        (scripts / "old.cjs").write_text("console.log('old')\n", encoding="utf-8")
        (scripts / "fresh.cjs").write_text("console.log('fresh')\n", encoding="utf-8")
        env = patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path(self.temp.name) / "state"),
            "XDG_STATE_HOME": str(Path(self.temp.name) / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        (self.repo / CONFIG_NAME).write_text(
            json.dumps({"version": 1, "tasks": {
                "old-task": {"argv": ["node", "scripts/old.cjs"], "timeout_seconds": 60},
            }}) + "\n",
            encoding="utf-8",
        )
        approve_config(self.repo, load_config(self.repo))
        self.runtime = ToolRuntime(self.repo, load_config(self.repo), allow_tasks=True)
        self.addCleanup(self.runtime.close)

    def _info(self):
        result = self.runtime.call("server_info", {})
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _run(self, name):
        return self.runtime.call("run_task", {"name": name})

    def _assert_denied(self, name, code):
        result = self._run(name)
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], code)

    def test_add_task_without_restart_stays_untrusted_until_approved(self):
        initial = self._info()["workspaces"][0]
        self.assertEqual([x["name"] for x in initial["tasks"]], ["old-task"])
        self.assertTrue(initial["task_config_trusted"])
        installed = install_node_task(plan_node_task(self.repo, "fresh-task", "scripts/fresh.cjs", 7200))
        unapproved = self._info()["workspaces"][0]
        self.assertEqual({x["name"] for x in unapproved["tasks"]}, {"old-task", "fresh-task"})
        self.assertFalse(unapproved["task_config_trusted"])
        self._assert_denied("fresh-task", "CONFIG_NOT_TRUSTED")
        self._assert_denied("old-task", "CONFIG_NOT_TRUSTED")
        approve_config(self.repo, installed)
        approved = self._info()["workspaces"][0]
        self.assertTrue(approved["task_config_trusted"])
        with patch.object(self.runtime.task_jobs, "run_or_promote",
                          return_value={"exit_code": 0, "stdout": "stub", "stderr": ""}) as dispatch:
            response = self._run("fresh-task")
            self.assertFalse(response["isError"], response)
            self.assertEqual(dispatch.call_args.args[1].name, "fresh-task")
            self.assertEqual(dispatch.call_args.args[1].timeout_seconds, 7200)
        self._assert_denied("not-registered", "UNKNOWN_TASK")

    def test_config_revoke_delete_and_malformed_are_fail_closed(self):
        (self.repo / CONFIG_NAME).unlink()
        self.assertFalse(self._info()["workspaces"][0]["task_config_trusted"])
        self.assertEqual(self._info()["workspaces"][0]["tasks"], [])
        self._assert_denied("old-task", "CONFIG_NOT_TRUSTED")
        (self.repo / CONFIG_NAME).write_text("{invalid", encoding="utf-8")
        unhealthy = self._info()
        self.assertFalse(unhealthy["healthy"])
        self._assert_denied("old-task", "TASK_CONFIG_REFRESH_FAILED")

    def test_approval_or_config_change_while_waiting_for_lease_rejects_dispatch(self):
        real_acquire = self.runtime._workspace_mutations.acquire

        def tamper(*args, **kwargs):
            (self.repo / CONFIG_NAME).write_text(
                json.dumps({"version": 1, "tasks": {
                    "old-task": {"argv": ["node", "scripts/old.cjs"], "timeout_seconds": 120},
                }}) + "\n",
                encoding="utf-8",
            )
            return real_acquire(*args, **kwargs)

        with patch.object(self.runtime._workspace_mutations, "acquire", side_effect=tamper):
            with patch.object(self.runtime.task_jobs, "run_or_promote") as dispatch:
                self._assert_denied("old-task", "TASK_CONFIG_CHANGED")
                dispatch.assert_not_called()

    def test_connected_launcher_keeps_workspace_selection_enabled(self):
        gui = (Path(__file__).resolve().parents[1] /
               "folderbridge_mcp" / "gui.py").read_text(encoding="utf-8")
        block = gui.split("def _set_form_state(self, enabled: bool)", 1)[1].split(
            "def _log(self, text: str)", 1)[0]
        self.assertIn('self.workspace_tree.state(("!disabled",))', block)
        self.assertIn("def _select_node_task_workspace(self)", gui)
        self.assertIn("dialog = tk.Toplevel(self.root)", gui)
        self.assertIn("selectmode=tk.SINGLE, exportselection=False", gui)
        self.assertIn("workspace = self._select_node_task_workspace()", gui)
        self.assertIn("self.add_workspace_button", block)
        self.assertIn("self.remove_workspace_button", block)


if __name__ == "__main__":
    unittest.main()
