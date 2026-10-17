from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.extensions import load_extension


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "Plugins" / "extensions" / "gpt-sovits-local"
SPEC = importlib.util.spec_from_file_location("published_gpt_sovits_local", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PublishedGptSoVitsTests(unittest.TestCase):
    def test_current_host_accepts_manifest_and_dedicated_surface(self) -> None:
        record = load_extension(PLUGIN_ROOT, bundled=False)
        self.assertEqual(record.manifest.extension_id, "gpt-sovits-local")
        self.assertEqual(record.manifest.version, "0.2.2")
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        expected = {
            "verification-plan", "status", "list-models", "dataset-status", "service-status",
            "probe-runtime", "bootstrap", "analyze-audio", "analyze-audio-batch",
            "prepare-reference", "stitch-audio", "slice-audio",
            "asr", "prepare-dataset", "train", "infer", "infer-batch",
            "launch-webui", "stop-webui", "launch-api", "stop-api", "run",
        }
        self.assertEqual(set(manifest["actions"]), expected)
        self.assertEqual(set(plugin.ALLOWED_OPERATIONS), {
            "probe", "bootstrap", "analyze-audio", "prepare-reference", "stitch-audio",
            "slice-audio", "prepare-dataset",
            "asr", "train", "infer", "launch-webui", "stop", "launch-api", "stop-api",
        })
        self.assertEqual(manifest["actions"]["run"]["run_mode"], "job")
        self.assertEqual(manifest["actions"]["run"]["timeout_seconds"], 0)
        self.assertIn("process.execute:powershell.exe", manifest["permissions"])
        self.assertIn("extension.state", manifest["permissions"])
        self.assertIn("network.loopback:127.0.0.1:9874", manifest["permissions"])
        self.assertIn("network.loopback:127.0.0.1:9880", manifest["permissions"])
        launch_api = manifest["actions"]["launch-api"]["input_schema"]["properties"]
        self.assertIn("gpt_weight", launch_api)
        self.assertIn("sovits_weight", launch_api)
        self.assertIn("force_cpu", launch_api)
        self.assertIn("is_half", launch_api)
        self.assertNotIn('"pattern"', json.dumps(manifest))
        supported_schema_keys = {
            "type", "properties", "required", "additionalProperties", "items", "enum",
            "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
            "description", "default",
        }

        def assert_supported_schema(schema: dict, path: str) -> None:
            unknown = set(schema).difference(supported_schema_keys)
            self.assertFalse(unknown, f"unsupported schema keys at {path}: {sorted(unknown)}")
            for name, child in schema.get("properties", {}).items():
                assert_supported_schema(child, f"{path}.properties.{name}")
            items = schema.get("items")
            if isinstance(items, dict):
                assert_supported_schema(items, f"{path}.items")

        for action_name, action in manifest["actions"].items():
            assert_supported_schema(action["input_schema"], f"actions.{action_name}.input_schema")

    def test_noisy_stdout_recovers_final_bridge_json(self) -> None:
        result = plugin._parse_bridge_stdout(
            "torch warning\nprogress\n{\"ok\":true,\"operation\":\"infer\",\"count\":2}",
            "infer",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "infer")
        self.assertEqual(result["count"], 2)
        self.assertIn("torch warning", result["stdout_tail"])

    def test_public_process_control_abi_is_primary_with_old_host_fallback(self) -> None:
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree", source)
        self.assertIn("FolderBridge 0.8.21 compatibility before the public process-helper re-export", source)
        self.assertEqual(source.count("from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree"), 1)
        self.assertNotIn("shell=True", source)

    def test_workspace_webui_wrapper_is_loopback_only_by_contract(self) -> None:
        bridge_readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:9874", bridge_readme)
        self.assertIn("127.0.0.1:9880", bridge_readme)

    def test_status_is_workspace_confined_and_side_effect_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bridge = root / "GPT-SoVITS-Bridge"
            (bridge / "ops").mkdir(parents=True)
            (bridge / "runner.ps1").write_text("# test\n", encoding="utf-8")
            (bridge / "ops" / "bridge_ops.py").write_text("# test\n", encoding="utf-8")
            result = plugin.handle("status", {}, {"workspace_root": str(root)})
            self.assertEqual(result["workspace_root"], str(root))
            self.assertTrue(result["runner_ready"])
            self.assertTrue(result["bridge_driver_ready"])
            self.assertFalse(result["runtime_ready"])
            self.assertEqual(result["verified_training_family"], "v2Pro")

    def test_dedicated_actions_map_to_fixed_runner_operations(self) -> None:
        context = {"workspace_root": tempfile.gettempdir(), "workspace_read_only": False}
        with patch.object(plugin, "_run_operation", return_value={"ok": True}) as call:
            plugin.handle("prepare-reference", {"input_audio": "a.wav", "output_path": "ref.wav"}, context)
            self.assertEqual(call.call_args.args[0], "prepare-reference")
            plugin.handle("stitch-audio", {"audio_paths": ["a.wav", "b.wav"], "output_path": "joined.wav"}, context)
            self.assertEqual(call.call_args.args[0], "stitch-audio")
            plugin.handle("slice-audio", {"source": "a.wav", "output_dir": "out"}, context)
            self.assertEqual(call.call_args.args[0], "slice-audio")
            plugin.handle(
                "infer-batch",
                {
                    "prompt_text": "ref",
                    "ref_audio": "ref.wav",
                    "items": [{"text": "hello", "output_name": "one.wav"}],
                },
                context,
            )
            self.assertEqual(call.call_args.args[0], "infer")
            self.assertEqual(call.call_args.args[1]["text"], "hello")
            self.assertEqual(call.call_args.args[1]["items"][0]["output_name"], "one.wav")

    def test_run_rejects_unknown_operation_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(RuntimeError):
                plugin.handle(
                    "run",
                    {"operation": "arbitrary-command"},
                    {"workspace_root": str(root), "workspace_read_only": False},
                )

    def test_dataset_and_model_inspection_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pretrained = root / "GPT-SoVITS" / "GPT_SoVITS" / "pretrained_models"
            pretrained.mkdir(parents=True)
            (pretrained / "s1.ckpt").write_bytes(b"x")
            trained = root / "GPT-SoVITS-Work" / "checkpoints" / "demo" / "sovits"
            trained.mkdir(parents=True)
            (trained / "voice.pth").write_bytes(b"y")
            exp = root / "GPT-SoVITS-Work" / "experiments" / "demo"
            (exp / "3-bert").mkdir(parents=True)
            (exp / "4-cnhubert").mkdir()
            (exp / "5-wav32k").mkdir()
            (exp / "2-name2text.txt").write_text("x", encoding="utf-8")
            (exp / "6-name2semantic.tsv").write_text("x", encoding="utf-8")
            context = {"workspace_root": str(root)}
            models = plugin.handle("list-models", {}, context)
            self.assertEqual(models["count"], 2)
            status = plugin.handle("dataset-status", {"exp_name": "demo"}, context)
            self.assertTrue(status["prepared"])
            self.assertEqual(status["sovits_checkpoint_count"], 1)


if __name__ == "__main__":
    unittest.main()
