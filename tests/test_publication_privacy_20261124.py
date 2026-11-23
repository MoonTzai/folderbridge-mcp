"""Strict allowlist and privacy gate for the 2026-11-24 public FolderBridge source update.
Judge integrations, local profiles, memory, private reports and test archives are excluded.
No credential contents are printed; failures show only path and rule identifiers.
"""
from __future__ import annotations
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISH = (
    ".github/workflows/release-windows.yml",
    "pyproject.toml",
    "folderbridge_mcp/__init__.py",
    "folderbridge_mcp/tools.py",
    "packaging/windows_dpi_manifest.xml",
    "packaging/windows_version_info.txt",
    "tests/test_version.py",
    "tests/test_v35_phase0_effect_catalog_gate.py",
    "tests/test_external_remotion_toolkit.py",
    ".gitignore",
    "README.md",
    "README.zh-CN.md",
    "Plugins/extensions/README.md",
    "Plugins/extensions/download-toolkit/README.md",
    "Plugins/extensions/download-toolkit/folderbridge-extension.json",
    "Plugins/extensions/download-toolkit/plugin.py",
    "Plugins/extensions/git-publisher/README.md",
    "Plugins/extensions/git-publisher/folderbridge-extension.json",
    "Plugins/extensions/git-publisher/plugin.py",
    "Plugins/extensions/exe-packager-toolkit/README.md",
    "Plugins/extensions/exe-packager-toolkit/folderbridge-extension.json",
    "Plugins/extensions/exe-packager-toolkit/install.ps1",
    "Plugins/extensions/exe-packager-toolkit/plugin.py",
    "Plugins/extensions/exe-packager-toolkit/tests/test_plugin.py",
    "Plugins/extensions/offline-asr-toolkit/README.md",
    "Plugins/extensions/offline-asr-toolkit/asr_worker.py",
    "Plugins/extensions/offline-asr-toolkit/folderbridge-extension.json",
    "Plugins/extensions/offline-asr-toolkit/install.ps1",
    "Plugins/extensions/offline-asr-toolkit/plugin.py",
    "Plugins/extensions/remotion-toolkit/README.md",
    "Plugins/extensions/remotion-toolkit/bridge.cjs",
    "Plugins/extensions/remotion-toolkit/folderbridge-extension.json",
    "Plugins/extensions/remotion-toolkit/install.ps1",
    "Plugins/extensions/remotion-toolkit/plugin.py",
    "Plugins/extensions/remotion-toolkit/tests/test_plugin.py",
    "Plugins/extensions/testonline-host/README.md",
    "Plugins/extensions/testonline-host/folderbridge-extension.json",
    "Plugins/extensions/testonline-host/install.ps1",
    "Plugins/extensions/testonline-host/plugin.py",
    "Plugins/extensions/testonline-host/server.py",
    "Plugins/extensions/testonline-host/tls_cert.py",
    "folderbridge_mcp/cli.py",
    "folderbridge_mcp/managed_services.py",
    "folderbridge_mcp/testonline_service.py",
    "scripts/build_windows.ps1",
    "scripts/audit_release_public_privacy.py",
    "scripts/verify_windows_bundle.py",
    "tests/test_external_download_toolkit.py",
    "tests/test_external_git_publisher_hot_update.py",
    "tests/test_git_publisher_extension.py",
    "tests/test_repository_hygiene.py",
    "tests/test_a0_offline_asr_toolkit.py",
    "tests/test_external_exe_packager_toolkit.py",
    "tests/test_external_testonline_host.py",
    "tests/test_git_publisher_remote.py",
    "tests/test_git_publisher_replace_release.py",
    "tests/test_publication_privacy_20261124.py",
)
RULES = {
    "SECRET_TOKEN": re.compile(r"(?:gh[pousr]_[a-zA-Z0-9_]{24,}|github_pat_[a-zA-Z0-9_]{24,}|sk-(?:proj-)?[a-zA-Z0-9_-]{24,}|AIza[a-zA-Z0-9_-]{30,})"),
    "PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "PRIVATE_USER_PATH": re.compile(r"[a-zA-Z]:[\\/]+Users[\\/]+[a-zA-Z0-9_.-]+[\\/]+(?:Desktop|Documents|AppData|Downloads|OneDrive|\\.ssh)", re.I),
    "PRIVATE_PROJECT_PATH": re.compile(r"[a-zA-Z]:[\\/]+(?:Claude[\\/]+Project|MiniMax[\\/]+|Tools[\\/]+AI-Recorder)", re.I),
    "BEARER": re.compile(r"Bearer\\s+[A-Za-z0-9._-]{30,}"),
}
PRIVATE_LAN = re.compile(r"\\b(?:192\\.168\\.\\d{1,3}\\.\\d{1,3}|172\\.(?:1[6-9]|2\\d|3[01])\\.\\d{1,3}\\.\\d{1,3}|10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})\\b")

class PublicationPrivacyTests(unittest.TestCase):
    def test_only_selected_public_sources(self):
        self.assertEqual(len(PUBLISH), len(set(PUBLISH)))
        for rel in PUBLISH:
            self.assertNotIn("judge", rel.casefold(), rel)
            self.assertFalse(any(piece in rel.casefold() for piece in ("local-private", "upload/", "memory/", "testonline/", ".exe", ".zip", "snapshot")), rel)
            path = ROOT / rel
            self.assertTrue(path.is_file() and not path.is_symlink(), rel)
            self.assertEqual(path.resolve().parent == ROOT or ROOT in path.resolve().parents, True, rel)

    def test_no_secrets_or_machine_specific_values(self):
        for rel in PUBLISH:
            content = (ROOT / rel).read_text(encoding="utf-8")
            for name, pattern in RULES.items():
                self.assertIsNone(pattern.search(content), rel + " violates " + name)
            for match in PRIVATE_LAN.finditer(content):
                address = match.group(0)
                if address not in {"192.168.0.0", "10.0.0.0", "172.16.0.0"}:
                    self.fail(rel + " contains a machine-specific LAN IP")

    def test_local_state_excluded_from_git(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for item in ("/local-private/", "/TestOnline/", "/FolderBridge.exe"):
            self.assertIn(item, ignore)

if __name__ == "__main__":
    unittest.main()
