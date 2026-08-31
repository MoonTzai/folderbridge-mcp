from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from folderbridge_mcp.config import ConfigError, MAX_WORKSPACES, canonical_workspaces


class WorkspaceLimitTests(unittest.TestCase):
    def test_sixteen_sibling_workspaces_are_allowed(self) -> None:
        self.assertEqual(MAX_WORKSPACES, 16)
        with TemporaryDirectory() as temporary_directory:
            roots = self._make_sibling_roots(Path(temporary_directory), MAX_WORKSPACES)
            self.assertEqual(canonical_workspaces(roots), tuple(path.resolve() for path in roots))

    def test_seventeenth_workspace_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            roots = self._make_sibling_roots(Path(temporary_directory), MAX_WORKSPACES + 1)
            with self.assertRaisesRegex(ConfigError, "At most 16 workspace directories are allowed"):
                canonical_workspaces(roots)

    @staticmethod
    def _make_sibling_roots(parent: Path, count: int) -> list[Path]:
        roots = [parent / f"workspace-{index:02d}" for index in range(count)]
        for root in roots:
            root.mkdir()
        return roots


if __name__ == "__main__":
    unittest.main()
