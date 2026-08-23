from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.user_paths import INTERNAL_CONFIG_ROOT_ENV, user_config_root


class UserPathTests(unittest.TestCase):
    def test_windows_localappdata_is_canonical_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "custom-local"
            root = user_config_root(
                environ={"LOCALAPPDATA": str(local), "USERPROFILE": str(Path(directory) / "home")},
                platform="win32",
            )
            self.assertEqual(root, local / "folderbridge-mcp")

    def test_worker_internal_override_wins_over_clean_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "exact-profile"
            fallback_home = Path(directory) / "other-home"
            root = user_config_root(
                environ={
                    INTERNAL_CONFIG_ROOT_ENV: str(expected),
                    "USERPROFILE": str(fallback_home),
                },
                platform="win32",
            )
            self.assertEqual(root, expected)

    def test_windows_clean_environment_falls_back_to_userprofile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = user_config_root(environ={"USERPROFILE": str(home)}, platform="win32")
            self.assertEqual(root, home / "AppData" / "Local" / "folderbridge-mcp")

    def test_posix_xdg_config_home_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xdg = Path(directory) / "xdg"
            root = user_config_root(
                environ={"XDG_CONFIG_HOME": str(xdg)},
                platform="linux",
                home=Path(directory) / "home",
            )
            self.assertEqual(root, xdg / "folderbridge-mcp")


if __name__ == "__main__":
    unittest.main()
