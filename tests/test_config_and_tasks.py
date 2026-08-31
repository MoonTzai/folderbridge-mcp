from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.config import (
    CONFIG_NAME,
    MAX_WORKSPACES,
    ConfigError,
    approve_config,
    canonical_workspace,
    canonical_workspaces,
    config_is_trusted,
    load_config,
)
from folderbridge_mcp.tools import ToolRuntime


class ConfigAndTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.state = self.base / "state"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.state)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def write_config(self, tasks: dict[str, object]) -> None:
        (self.root / CONFIG_NAME).write_text(
            json.dumps({"version": 1, "tasks": tasks}, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_exact_config_hash_must_be_approved(self) -> None:
        self.write_config({})
        config = load_config(self.root, required=True)
        self.assertFalse(config_is_trusted(self.root, config))
        approve_config(self.root, config)
        self.assertTrue(config_is_trusted(self.root, config))
        self.write_config({"check": {"argv": [sys.executable, "task.py"]}})
        self.assertFalse(config_is_trusted(self.root, load_config(self.root, required=True)))

    def test_inline_interpreter_task_is_rejected(self) -> None:
        self.write_config({"bad": {"argv": [sys.executable, "-c", "print('unsafe')"]}})
        with self.assertRaisesRegex(ConfigError, "inline"):
            load_config(self.root, required=True)

    def test_rejects_overbroad_workspace(self) -> None:
        with self.assertRaisesRegex(ConfigError, "too broad"):
            canonical_workspace(Path(self.root.anchor))

    def test_workspace_set_rejects_duplicates_and_overlaps(self) -> None:
        child = self.root / "child"
        child.mkdir()
        sibling = self.base / "sibling"
        sibling.mkdir()

        self.assertEqual(canonical_workspaces([self.root, sibling]), (self.root.resolve(), sibling.resolve()))
        with self.assertRaises(ConfigError):
            canonical_workspaces([self.root, self.root])
        with self.assertRaises(ConfigError):
            canonical_workspaces([self.root, child])
        extras = []
        for index in range(MAX_WORKSPACES):
            extra = self.base / f"extra-{index}"
            extra.mkdir()
            extras.append(extra)
        with self.assertRaises(ConfigError):
            canonical_workspaces([self.root, *extras])

    def test_only_approved_named_task_runs(self) -> None:
        (self.root / "task.py").write_text("print('task-ok')\n", encoding="utf-8")
        self.write_config({"check": {"argv": [sys.executable, "task.py"], "timeout_seconds": 10}})
        config = load_config(self.root, required=True)
        runtime = ToolRuntime(self.root, config, allow_tasks=True)
        denied = runtime.call("run_task", {"name": "check"})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"]["code"], "CONFIG_NOT_TRUSTED")
        approve_config(self.root, config)
        result = runtime.call("run_task", {"name": "check"})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["exit_code"], 0)
        self.assertIn("task-ok", result["structuredContent"]["stdout"])
        unknown = runtime.call("run_task", {"name": "anything-else"})
        self.assertEqual(unknown["structuredContent"]["error"]["code"], "UNKNOWN_TASK")


if __name__ == "__main__":
    unittest.main()
