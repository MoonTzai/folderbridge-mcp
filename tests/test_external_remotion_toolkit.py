"""Public Remotion Extension source and release-boundary smoke tests.

This test never invokes a render, installs dependencies, or touches user data.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "Plugins" / "extensions" / "remotion-toolkit"


class RemotionPublicPackagingTests(unittest.TestCase):
    def test_manifest_version_and_explicit_bounded_actions(self):
        manifest = json.loads((PLUGIN / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "remotion-toolkit")
        self.assertEqual(manifest["version"], "0.1.2")
        self.assertEqual(manifest["runtime_abi"], 1)
        actions = manifest["actions"]
        for action in ("status", "project-inspect", "preflight", "compositions",
                       "bundle", "render-still", "render-video", "media-probe", "silence-scan"):
            self.assertIn(action, actions)
        for action in actions.values():
            self.assertIn("read_only", action)

    def test_public_sources_are_independent_of_private_developer_paths(self):
        for name in ("README.md", "plugin.py", "bridge.cjs", "folderbridge-extension.json", "install.ps1"):
            data = (PLUGIN / name).read_text(encoding="utf-8")
            self.assertNotRegex(data, re.compile(r"[A-Za-z]:[\\/]+Claude[\\/]+Project", re.I))
            self.assertNotRegex(data, re.compile(r"[A-Za-z]:[\\/]+MiniMax[\\/]"))
            self.assertNotIn("local-private\\profiles.json", data)
            self.assertNotRegex(data, re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"))


if __name__ == "__main__":
    unittest.main()
