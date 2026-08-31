from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from folderbridge_mcp import process_control
from folderbridge_mcp import extension_api


class ProcessControlTests(unittest.TestCase):
    def test_extension_api_reexports_exact_process_control_helpers(self) -> None:
        self.assertIs(extension_api.owned_process_group_kwargs, process_control.owned_process_group_kwargs)
        self.assertIs(extension_api.terminate_owned_process_tree, process_control.terminate_owned_process_tree)
        self.assertEqual(
            set(extension_api.__all__),
            {"ExtensionError", "owned_process_group_kwargs", "terminate_owned_process_tree"},
        )

    def test_owned_process_group_kwargs_hide_windows_children_without_shell_helpers(self) -> None:
        with mock.patch.object(process_control.sys, "platform", "win32"), mock.patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True
        ), mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True):
            kwargs = process_control.owned_process_group_kwargs(hide_window=True)

        self.assertEqual(kwargs["creationflags"], 0x200 | 0x8000000)
        self.assertFalse(kwargs["start_new_session"])

    def test_owned_process_group_kwargs_use_posix_session(self) -> None:
        with mock.patch.object(process_control.sys, "platform", "linux"):
            kwargs = process_control.owned_process_group_kwargs()
        self.assertEqual(kwargs["creationflags"], 0)
        self.assertTrue(kwargs["start_new_session"])

    def test_windows_tree_termination_falls_back_when_taskkill_fails(self) -> None:
        class FakeProcess:
            pid = 4321

            def __init__(self) -> None:
                self.killed = False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

        process = FakeProcess()
        completed = mock.Mock(returncode=1)
        with mock.patch.object(process_control.sys, "platform", "win32"), mock.patch.dict(
            process_control.os.environ, {"SystemRoot": r"C:\Windows"}, clear=False
        ), mock.patch.object(process_control.subprocess, "run", return_value=completed) as run:
            process_control.terminate_owned_process_tree(process, force=True, hide_window=True)

        argv = run.call_args.args[0]
        self.assertIn("/T", argv)
        self.assertIn("/F", argv)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
