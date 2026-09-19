from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "storyboard-chatgpt-web"


class StoryboardChatGPTWebPublicExtensionTests(unittest.TestCase):
    def test_manifest_and_runtime_surface_match_accepted_020(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["runtime_abi"], 1)
        self.assertEqual(manifest["id"], "storyboard-chatgpt-web")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertIn("workspace.write", manifest["permissions"])
        self.assertIn("network.loopback:127.0.0.1:8771", manifest["permissions"])
        for action in (
            "generator-bind",
            "generator-run",
            "judge-bind",
            "judge-run",
            "candidate-accept",
            "repair-preview",
            "auto-run",
        ):
            self.assertIn(action, manifest["actions"])
        self.assertEqual(manifest["actions"]["generator-run"]["run_mode"], "job")
        self.assertEqual(manifest["actions"]["judge-run"]["run_mode"], "job")

    def test_installer_runtime_allowlist_is_complete_and_excludes_audit_only_legacy_reference(self) -> None:
        required = {
            "folderbridge-extension.json",
            "plugin.py",
            "bridge.cjs",
            "live_judge.py",
            "live_generator.py",
            "live_state.py",
            "README.md",
            "install.ps1",
            "engine/bundle-manifest.json",
            "engine/compiler/storyboard-forge-core.js",
            "engine/runner/compiled-task-adapter.cjs",
            "engine/runner/execution-handoff.cjs",
            "engine/runner/judge-contract.cjs",
            "engine/runner/operation-identity.cjs",
            "engine/runner/reference-binder.cjs",
            "engine/runner/repair-execution.cjs",
            "engine/runner/state-machine.cjs",
        }
        for relative in required:
            self.assertTrue((PLUGIN_ROOT / relative).is_file(), relative)
        self.assertFalse((PLUGIN_ROOT / "legacy_reference").exists())

        installer = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8").replace("\\", "/")
        for relative in required:
            if relative == "install.ps1":
                self.assertIn('"install.ps1"', installer)
            else:
                self.assertIn(f'"{relative}"', installer)
        self.assertNotIn("legacy_reference", installer)

    def test_pragmatic_transport_and_generate_only_contract_remain_public(self) -> None:
        judge = (PLUGIN_ROOT / "live_judge.py").read_text(encoding="utf-8")
        generator = (PLUGIN_ROOT / "live_generator.py").read_text(encoding="utf-8")
        plugin = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        binder = (PLUGIN_ROOT / "engine" / "runner" / "reference-binder.cjs").read_text(encoding="utf-8")

        self.assertNotIn("JUDGE_REPLY_INCOMPLETE", judge)
        self.assertNotIn("response_incomplete", judge)
        self.assertIn("browser transcript is transport, not semantic authority", judge)
        self.assertIn("image_asset_pointer", generator)
        self.assertIn("chatgpt-web-exact-image-asset-pointer", generator)
        self.assertIn("GENERATE_ONLY", plugin)
        self.assertIn("generated_unreviewed", plugin)
        self.assertIn("allowUnreviewed", binder)
        self.assertIn("sequence_only", binder)

    def test_engine_bundle_manifest_matches_packaged_files(self) -> None:
        bundle = json.loads((PLUGIN_ROOT / "engine" / "bundle-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["schema_version"], "storyboard-chatgpt-web-engine-bundle-1.0")
        self.assertEqual(len(bundle["files"]), 8)
        for row in bundle["files"]:
            self.assertTrue((PLUGIN_ROOT / row["target"]).is_file(), row["target"])


if __name__ == "__main__":
    unittest.main()
