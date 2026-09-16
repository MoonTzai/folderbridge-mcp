from __future__ import annotations

import unittest
from unittest.mock import Mock

from folderbridge_mcp.gui import FolderBridgeLauncher
from folderbridge_mcp.update_check import LATEST_RELEASES_URL, UpdateCheckResult


class _Button:
    def __init__(self) -> None:
        self.states: list[dict[str, object]] = []

    def configure(self, **kwargs) -> None:
        self.states.append(kwargs)


class GuiUpdateCheckTests(unittest.TestCase):
    def launcher(self) -> FolderBridgeLauncher:
        launcher = FolderBridgeLauncher.__new__(FolderBridgeLauncher)
        launcher._update_check_inflight = True
        launcher._known_update_version = None
        launcher.update_button = _Button()
        launcher._set_widget_text = Mock()
        launcher._log = Mock()
        launcher._show_info = Mock()
        launcher._show_release_link_dialog = Mock()
        return launcher

    def test_newer_version_prompts_and_exposes_latest_release_link(self) -> None:
        launcher = self.launcher()
        result = UpdateCheckResult(
            "update_available",
            "0.8.34",
            "0.8.35",
            LATEST_RELEASES_URL,
        )
        launcher._handle_update_check_result(result, automatic=True)
        self.assertFalse(launcher._update_check_inflight)
        self.assertEqual(launcher._known_update_version, "0.8.35")
        launcher._set_widget_text.assert_called_with(launcher.update_button, "有更新 v0.8.35")
        launcher._show_release_link_dialog.assert_called_once()
        title, message, url = launcher._show_release_link_dialog.call_args.args
        self.assertEqual(title, "FolderBridge 有更新")
        self.assertIn("0.8.35", message)
        self.assertEqual(url, LATEST_RELEASES_URL)

    def test_update_available_exposes_interactive_latest_release_dialog(self) -> None:
        launcher = self.launcher()
        result = UpdateCheckResult(
            "update_available",
            "0.8.34",
            "0.8.35",
            LATEST_RELEASES_URL,
        )
        launcher._handle_update_check_result(result, automatic=False)
        launcher._show_release_link_dialog.assert_called_once_with(
            "FolderBridge 有更新",
            "发现新版本 0.8.35，当前版本 0.8.34。",
            LATEST_RELEASES_URL,
        )

    def test_automatic_network_failure_is_silent_but_manual_failure_explains_latest_link(self) -> None:
        result = UpdateCheckResult("unavailable", "0.8.34", None, LATEST_RELEASES_URL, "OSError")
        automatic = self.launcher()
        automatic._handle_update_check_result(result, automatic=True)
        automatic._show_release_link_dialog.assert_not_called()

        manual = self.launcher()
        manual._handle_update_check_result(result, automatic=False)
        manual._show_release_link_dialog.assert_called_once_with(
            "FolderBridge 更新检查",
            "暂时无法检查更新。你仍可直接查看 Latest Release。",
            LATEST_RELEASES_URL,
        )


if __name__ == "__main__":
    unittest.main()
