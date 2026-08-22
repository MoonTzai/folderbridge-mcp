import unittest
from pathlib import Path

from folderbridge_mcp.setup_guide import (
    CHATGPT_INVOCATION_EXAMPLE,
    WINDOWS_X64_ASSET_GLOB,
    WINDOWS_X64_ASSET_PATTERN,
    looks_like_tunnel_id,
    recommended_client_directory,
)


class SetupGuideTests(unittest.TestCase):
    def test_windows_x64_hint_names_the_complete_amd64_archive(self) -> None:
        self.assertEqual(WINDOWS_X64_ASSET_PATTERN, "tunnel-client-v<版本>-windows-amd64.zip")
        self.assertEqual(WINDOWS_X64_ASSET_GLOB, "tunnel-client-v*-windows-amd64.zip")
        self.assertNotIn("runtime", WINDOWS_X64_ASSET_PATTERN)
        self.assertNotIn("arm64", WINDOWS_X64_ASSET_PATTERN)

    def test_recommended_directory_uses_local_app_data_without_admin_rights(self) -> None:
        path = recommended_client_directory({"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"})
        self.assertEqual(path, Path(r"C:\Users\Test\AppData\Local") / "FolderBridge" / "bin")

    def test_recommended_directory_has_a_portable_fallback(self) -> None:
        path = recommended_client_directory({}, home=Path("/home/test"))
        self.assertEqual(path, Path("/home/test/.folderbridge/bin"))

    def test_tunnel_id_validation_is_forward_compatible(self) -> None:
        self.assertTrue(looks_like_tunnel_id(" tunnel_0123456789abcdef "))
        self.assertFalse(looks_like_tunnel_id("https://example.test/v1/mcp/tunnel_123"))
        self.assertFalse(looks_like_tunnel_id("tunnel_"))

    def test_chatgpt_invocation_example_is_actionable(self) -> None:
        self.assertIn("FolderBridge", CHATGPT_INVOCATION_EXAMPLE)
        self.assertIn("列出", CHATGPT_INVOCATION_EXAMPLE)
        self.assertIn("访问权限", CHATGPT_INVOCATION_EXAMPLE)


if __name__ == "__main__":
    unittest.main()
