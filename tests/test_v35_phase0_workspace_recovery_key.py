from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


class V35WorkspaceRecoveryKeyAcceptanceTests(unittest.TestCase):
    def test_public_workspace_id_remains_compatibility_selector(self) -> None:
        from folderbridge_mcp.config import workspace_id

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            expected = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
            self.assertEqual(workspace_id(root), expected)

    def test_recovery_key_is_bounded_opaque_and_separate_from_public_selector(self) -> None:
        from folderbridge_mcp.config import workspace_id, workspace_recovery_key

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            key = workspace_recovery_key(root)
            self.assertRegex(key, r"^wrk1:[0-9a-f]{64}$")
            self.assertLessEqual(len(key), 80)
            self.assertNotEqual(key, workspace_id(root))
            self.assertNotIn(str(root), key)

    def test_same_real_root_through_symlink_alias_converges_when_supported(self) -> None:
        from folderbridge_mcp.config import workspace_recovery_key

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "real-workspace"
            root.mkdir()
            alias = base / "workspace-alias"
            try:
                alias.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is unavailable")
            self.assertEqual(workspace_recovery_key(root), workspace_recovery_key(alias))

    @unittest.skipUnless(sys.platform == "win32", "Windows alias/case acceptance")
    def test_windows_case_and_short_name_spellings_converge(self) -> None:
        from folderbridge_mcp.config import workspace_recovery_key

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "FolderBridge Recovery Workspace"
            root.mkdir()
            canonical_key = workspace_recovery_key(root)

            case_variant = Path(str(root).swapcase())
            self.assertTrue(case_variant.exists())
            self.assertEqual(canonical_key, workspace_recovery_key(case_variant))

            buffer = ctypes.create_unicode_buffer(32768)
            get_short_path = ctypes.windll.kernel32.GetShortPathNameW
            get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_short_path.restype = ctypes.c_uint32
            length = get_short_path(str(root), buffer, len(buffer))
            if 0 < length < len(buffer):
                short_path = Path(buffer.value)
                if os.path.normcase(str(short_path)) != os.path.normcase(str(root)):
                    self.assertTrue(short_path.exists())
                    self.assertEqual(canonical_key, workspace_recovery_key(short_path))

    def test_replaced_root_at_same_path_gets_new_recovery_binding(self) -> None:
        from folderbridge_mcp.config import workspace_recovery_key

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            before = workspace_recovery_key(root)
            root.rmdir()
            root.mkdir()
            after = workspace_recovery_key(root)
            self.assertNotEqual(before, after)

    def test_runtime_binding_captures_the_internal_recovery_key(self) -> None:
        from folderbridge_mcp.config import ProjectConfig, workspace_recovery_key
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            config = ProjectConfig(path=root / ".folderbridge.json", tasks={}, sha256="")
            target = ToolRuntime._make_target(root, config)
            self.assertEqual(target.workspace_recovery_key, workspace_recovery_key(root))
            self.assertNotEqual(target.workspace_recovery_key, target.workspace_id)


if __name__ == "__main__":
    unittest.main()
