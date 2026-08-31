from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "Plugins" / "extensions" / "gpt-sovits-local"
SPEC = importlib.util.spec_from_file_location("published_gpt_sovits_local", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PublishedGptSoVitsTests(unittest.TestCase):
    def test_manifest_and_operation_contract(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "gpt-sovits-local")
        self.assertEqual(manifest["version"], "0.1.2")
        self.assertEqual(set(plugin.ALLOWED_OPERATIONS), {
            "probe", "bootstrap", "prepare-dataset", "asr", "train", "infer", "launch-webui", "stop"
        })
        self.assertEqual(manifest["actions"]["run"]["run_mode"], "job")
        self.assertEqual(manifest["actions"]["run"]["timeout_seconds"], 0)
        self.assertIn("process.execute:powershell.exe", manifest["permissions"])
        self.assertIn("extension.state", manifest["permissions"])

    def test_public_process_control_abi_is_primary_with_old_host_fallback(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree", source)
        self.assertIn("FolderBridge 0.8.21 compatibility before the public process-helper re-export", source)
        self.assertEqual(source.count("from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree"), 1)

    def test_status_is_workspace_confined_and_side_effect_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bridge = root / "GPT-SoVITS-Bridge"
            bridge.mkdir()
            (bridge / "runner.ps1").write_text("# test\n", encoding="utf-8")
            result = plugin.handle("status", {}, {"workspace_root": str(root)})
            self.assertEqual(result["workspace_root"], str(root))
            self.assertTrue(result["runner_ready"])
            self.assertFalse(result["runtime_ready"])
            self.assertIn("ready", result)

    def test_run_rejects_unknown_operation_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(RuntimeError):
                plugin.handle(
                    "run",
                    {"operation": "arbitrary-command"},
                    {"workspace_root": str(root), "workspace_read_only": False},
                )


if __name__ == "__main__":
    unittest.main()
