from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.cli import _client_config
from folderbridge_mcp.config import CONFIG_NAME, approve_config, load_config, workspace_id
from folderbridge_mcp.launcher_backend import LauncherSettings
from folderbridge_mcp.tools import ToolRuntime


class AllowTasksRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.a = self.base / "A-no-config"
        self.b = self.base / "B-approved-task"
        self.c = self.base / "C-extension-only"
        self.d = self.base / "D-unapproved-config"
        for root in (self.a, self.b, self.c, self.d):
            root.mkdir()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.state)})
        self.env.start()

        (self.b / "task.py").write_text("print('approved-task-ok')\n", encoding="utf-8")
        self._write_config(self.b, "approved", "task.py")
        approve_config(self.b, load_config(self.b, required=True))

        (self.d / "task.py").write_text("print('must-not-run-unapproved')\n", encoding="utf-8")
        self._write_config(self.d, "unapproved", "task.py")

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def _write_config(root: Path, task_name: str, script_name: str) -> None:
        (root / CONFIG_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "tasks": {
                        task_name: {
                            "argv": [sys.executable, script_name],
                            "timeout_seconds": 10,
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_mixed_workspaces_start_with_allow_tasks_and_missing_configs(self) -> None:
        runtime = ToolRuntime.from_roots((self.a, self.b, self.c), allow_tasks=True)
        info = runtime.call("server_info", {})["structuredContent"]
        by_name = {item["name"]: item for item in info["workspaces"]}
        self.assertEqual(by_name[self.a.name]["tasks"], [])
        self.assertEqual(by_name[self.c.name]["tasks"], [])
        self.assertEqual([task["name"] for task in by_name[self.b.name]["tasks"]], ["approved"])
        self.assertTrue(info["task_execution_enabled"])

        result = runtime.call(
            "run_task",
            {"workspace_id": workspace_id(self.b.resolve()), "name": "approved"},
        )
        self.assertFalse(result["isError"])
        self.assertIn("approved-task-ok", result["structuredContent"]["stdout"])

    def test_unapproved_config_does_not_block_runtime_but_run_task_is_rejected(self) -> None:
        runtime = ToolRuntime.from_roots((self.a, self.d, self.c), allow_tasks=True)
        info = runtime.call("server_info", {})["structuredContent"]
        target = next(item for item in info["workspaces"] if item["name"] == self.d.name)
        self.assertFalse(target["task_config_trusted"])
        self.assertEqual([task["name"] for task in target["tasks"]], ["unapproved"])

        denied = runtime.call(
            "run_task",
            {"workspace_id": workspace_id(self.d.resolve()), "name": "unapproved"},
        )
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "CONFIG_NOT_TRUSTED")

    def test_launcher_validation_allows_missing_and_unapproved_configs(self) -> None:
        settings = LauncherSettings(
            workspaces=[str(self.a), str(self.b), str(self.c), str(self.d)],
            allow_tasks=True,
        )
        roots = settings.validate(require_tunnel_id=False)
        self.assertEqual(len(roots), 4)

    def test_client_config_generation_does_not_require_all_workspace_configs_or_approval(self) -> None:
        workspaces = (self.a.resolve(), self.b.resolve(), self.c.resolve(), self.d.resolve())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = _client_config(
                workspaces,
                output_format="json",
                read_only=True,
                allow_tasks=True,
                capabilities=[],
            )
        self.assertEqual(code, 0)
        rendered = json.loads(output.getvalue())
        args = rendered["mcpServers"]["folderbridge"]["args"]
        self.assertIn("--allow-tasks", args)
        self.assertEqual(args.count("--workspace"), 4)

    def test_runtime_source_never_restores_required_equals_allow_tasks(self) -> None:
        root = Path(__file__).resolve().parents[1] / "folderbridge_mcp"
        combined = "\n".join(
            (root / name).read_text(encoding="utf-8")
            for name in ("tools.py", "launcher_backend.py", "cli.py")
        )
        self.assertNotIn("required=allow_tasks", combined)
        self.assertNotIn("required = allow_tasks", combined)


if __name__ == "__main__":
    unittest.main()
