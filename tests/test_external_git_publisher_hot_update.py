from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from folderbridge_mcp.extensions import (
    ExtensionRegistry,
    ExtensionTrustStore,
    install_external_extension_zip,
    load_extension,
)

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_SOURCE = ROOT / "Plugins" / "extensions" / "git-publisher"
LEGACY_BUNDLED_DIR = ROOT / "extensions" / "git-publisher"
PUBLISH_FILES = ("folderbridge-extension.json", "plugin.py", "README.md")


class GitPublisherExternalHotUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="git-publisher-hot-update-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.installed = self.root / "extensions"
        self.trust = ExtensionTrustStore(self.root / "approval.json")
        self.registry = ExtensionRegistry(
            user_root=self.installed,
            bundled_root=ROOT / "extensions",
            trust_store=self.trust,
        )

    def _make_zip(self, suffix: str = "") -> Path:
        zip_file = self.root / ("publisher-updated.zip" if suffix else "publisher-initial.zip")
        with zipfile.ZipFile(zip_file, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name in PUBLISH_FILES:
                b = (EXTERNAL_SOURCE / name).read_bytes()
                if name == "README.md" and suffix:
                    b += suffix.encode("utf-8")
                z.writestr(name, b)
        return zip_file

    def test_source_is_external_and_no_bundled_manifest_remains(self):
        self.assertTrue((EXTERNAL_SOURCE / "folderbridge-extension.json").is_file())
        self.assertFalse((LEGACY_BUNDLED_DIR / "folderbridge-extension.json").exists())
        record = load_extension(EXTERNAL_SOURCE, bundled=False)
        self.assertEqual(record.manifest.extension_id, "git-publisher")
        self.assertEqual(record.manifest.version, "1.6.0")
        self.assertTrue(all(action.authorization == "global" for action in record.manifest.actions.values()))
        self.assertEqual(self.registry.scan()[0].keys(), {"office", "skill-engine"})

    def test_install_approve_then_hot_update_invalidates_old_hash(self):
        initial = install_external_extension_zip(self._make_zip(), destination_root=self.installed)
        self.assertFalse(initial.bundled)
        item = self.registry.get("git-publisher")
        self.assertEqual(item.sha256, initial.sha256)
        before = next(x for x in self.registry.describe()["extensions"] if x["id"] == "git-publisher")
        self.assertFalse(before["loaded"])
        self.trust.approve(item, enabled=True)
        approved = next(x for x in self.registry.describe()["extensions"] if x["id"] == "git-publisher")
        self.assertTrue(approved["loaded"])
        self.assertFalse(approved["bundled"])

        updated = install_external_extension_zip(self._make_zip("\nHot update readme revision.\n"), destination_root=self.installed)
        self.assertNotEqual(initial.sha256, updated.sha256)
        stale = next(x for x in self.registry.describe()["extensions"] if x["id"] == "git-publisher")
        self.assertFalse(stale["loaded"])
        self.assertTrue(stale["approval_stale"])
        self.trust.approve(updated, enabled=True)
        refreshed = next(x for x in self.registry.describe()["extensions"] if x["id"] == "git-publisher")
        self.assertTrue(refreshed["loaded"])
        self.assertFalse(refreshed["approval_stale"])
        self.assertFalse(refreshed["bundled"])


if __name__ == "__main__":
    unittest.main()
