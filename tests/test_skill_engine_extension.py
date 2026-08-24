from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import ExtensionRegistry, ExtensionTrustStore, load_extension


ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "extensions" / "skill-engine"


class SkillEngineExtensionTests(unittest.TestCase):
    def make_registry(self) -> tuple[tempfile.TemporaryDirectory[str], ExtensionRegistry]:
        temp = tempfile.TemporaryDirectory()
        state = Path(temp.name)
        registry = ExtensionRegistry(
            user_root=state / "extensions",
            bundled_root=ROOT / "extensions",
            trust_store=ExtensionTrustStore(state / "extensions-trust.json"),
        )
        return temp, registry

    def test_manifest_is_bundled_read_only_and_permissionless(self) -> None:
        record = load_extension(EXT_DIR, bundled=True)
        self.assertEqual(record.manifest.extension_id, "skill-engine")
        self.assertEqual(record.manifest.permissions, ())
        self.assertEqual(set(record.manifest.actions), {"list", "match", "get"})
        for action in record.manifest.actions.values():
            self.assertTrue(action.read_only)
            self.assertFalse(action.requires_workspace)
            self.assertEqual(action.authorization, "none")

    def test_list_match_get_run_through_stable_extension_worker(self) -> None:
        temp, registry = self.make_registry()
        self.addCleanup(temp.cleanup)

        listed = registry.run("skill-engine", "list", {}, workspace=None, read_only=True)
        packs = listed["packs"]
        self.assertIn("folderbridge-engineering", {item["id"] for item in packs})

        matched = registry.run(
            "skill-engine",
            "match",
            {"task": "请用TDD测试先行修这个bug", "limit": 5},
            workspace=None,
            read_only=True,
        )
        refs = {item["skill_ref"] for item in matched["matches"]}
        self.assertIn("folderbridge-engineering/tdd", refs)
        self.assertIn("folderbridge-engineering/diagnosing-bugs", refs)

        selected = next(item for item in matched["matches"] if item["skill_ref"].endswith("/tdd"))
        loaded = registry.run(
            "skill-engine",
            "get",
            {"skill_ref": selected["skill_ref"], "expected_sha256": selected["sha256"]},
            workspace=None,
            read_only=True,
        )
        self.assertEqual(loaded["skill_ref"], selected["skill_ref"])
        self.assertNotIn("text", loaded)
        self.assertEqual(loaded["_content"][0]["type"], "text")
        self.assertIn("Test-Driven Development", loaded["_content"][0]["text"])

    def test_extensions_self_test_runs_comfyui_and_skill_engine_workers(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "folderbridge_launcher.py"), "extensions", "--self-test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["comfyui"]["extension_id"], "comfyui")
        self.assertIn("folderbridge-engineering", {item["id"] for item in payload["skill_engine"]["packs"]})

    def test_windows_packaging_includes_skill_data_and_smokes_skill_gateway(self) -> None:
        script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn('"skill_packs") + ";skill_packs"', script)
        self.assertIn("skills --json", script)
        self.assertIn('"id"\\s*:\\s*"folderbridge-engineering"', script)
        self.assertIn('foreach ($requiredExtension in @("comfyui", "office", "git-publisher", "skill-engine"))', script)
        self.assertIn('$gitPublisherExtension.version -ne "1.1.0"', script)
        self.assertIn('$officeExtension.version -ne "1.1.0"', script)
        for skill_id in (
            "codebase-design",
            "improve-codebase-architecture",
            "diagnosing-bugs",
            "tdd",
            "code-review",
            "implement",
        ):
            self.assertIn(f'"id"\\s*:\\s*"{skill_id}"', script)


if __name__ == "__main__":
    unittest.main()
