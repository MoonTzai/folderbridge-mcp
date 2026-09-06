from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("download_toolkit_under_test", ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class DownloadToolkitSelfTests(unittest.TestCase):
    def test_manifest_actions_and_permissions_are_narrow(self) -> None:
        manifest = json.loads((ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["actions"]), {"download", "github-snapshot"})
        self.assertEqual(set(manifest["permissions"]), {"workspace.write", "network.outbound:https"})

    def test_private_resolution_is_rejected(self) -> None:
        with patch.object(plugin.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(plugin.ExtensionError):
                plugin._validated_target("https://example.invalid/file")

    def test_public_ip_classifier_rejects_private_targets(self) -> None:
        self.assertTrue(plugin._is_public_ip("8.8.8.8"))
        self.assertFalse(plugin._is_public_ip("127.0.0.1"))
        self.assertFalse(plugin._is_public_ip("::ffff:127.0.0.1"))

    def test_workspace_path_rejects_traversal(self) -> None:
        for value in ("../x", "/x", ".git/config", ".env", "node_modules/a", "x.txt:ads", "CON", ".folderbridge.json", "secret.pem"):
            with self.subTest(value=value), self.assertRaises(plugin.ExtensionError):
                plugin._clean_relative(value)


if __name__ == "__main__":
    unittest.main()
