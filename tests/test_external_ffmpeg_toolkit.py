from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "Plugins" / "extensions" / "ffmpeg-toolkit"
SPEC = importlib.util.spec_from_file_location("published_ffmpeg_toolkit", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PublishedFFmpegToolkitTests(unittest.TestCase):
    def test_manifest_is_v011_external_extension(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["id"], "ffmpeg-toolkit")
        self.assertEqual(manifest["version"], "0.1.1")
        self.assertEqual(manifest["entrypoint"], "plugin.py")
        self.assertEqual(set(manifest["actions"]), {"status", "capabilities", "probe", "run"})
        self.assertIn("process.execute:ffmpeg.exe", manifest["permissions"])
        self.assertIn("process.execute:ffprobe.exe", manifest["permissions"])

    def test_ffprobe_and_ffmpeg_have_separate_managed_argv(self) -> None:
        probe = plugin._build_ffprobe_argv(Path("ffprobe.exe"), ["-show_format", "input.wav"])
        run = plugin._build_ffmpeg_argv(Path("ffmpeg.exe"), ["-i", "input.wav", "output.wav"], overwrite=True)
        self.assertNotIn("-nostdin", probe)
        self.assertIn("-protocol_whitelist", probe)
        self.assertIn("-nostdin", run)
        self.assertIn("-protocol_whitelist", run)
        self.assertIn("-y", run)

    def test_probe_path_custom_args_auto_append_source_once(self) -> None:
        effective = plugin._prepare_probe_params({
            "path": "media/input.wav",
            "args": ["-v", "error", "-show_format", "-of", "json"],
        })
        self.assertEqual(effective["args"][-1], "{{source}}")
        self.assertEqual(effective["args"].count("{{source}}"), 1)
        explicit = ["-v", "error", "{{source}}", "-show_streams", "-of", "json"]
        self.assertEqual(plugin._prepare_probe_params({"path": "media/input.wav", "args": explicit})["args"], explicit)

    def test_default_probe_contract_builds_local_json_probe_without_nostdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            media = root / "media"
            state.mkdir()
            media.mkdir()
            (media / "input.wav").write_bytes(b"RIFF")
            captured: dict[str, object] = {}

            def fake_execute(argv, context, *, timeout_seconds, prefix):
                captured["argv"] = list(argv)
                return 0, '{"format":{"format_name":"wav"},"streams":[{"codec_type":"audio"}]}', "", False, False, 0.01

            with patch.object(plugin, "_resolve_tools", return_value=(Path("ffmpeg.exe"), Path("ffprobe.exe"), "test")), patch.object(plugin, "_execute_long", side_effect=fake_execute):
                result = plugin.handle(
                    "probe",
                    {"path": "media/input.wav"},
                    {"workspace_root": str(root), "state_dir": str(state), "workspace_read_only": False},
                )

            argv = captured["argv"]
            assert isinstance(argv, list)
            self.assertNotIn("-nostdin", argv)
            self.assertEqual(argv[1:4], ["-hide_banner", "-protocol_whitelist", "file,crypto,data"])
            self.assertEqual(argv[-1], str((media / "input.wav").resolve()))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["json"]["format"]["format_name"], "wav")
            self.assertEqual(result["json"]["streams"][0]["codec_type"], "audio")

    def test_probe_advanced_paths_and_safety_contract_remain_intact(self) -> None:
        effective = plugin._prepare_probe_params({
            "paths": {"input1": {"path": "media/input.wav", "mode": "input"}},
            "args": ["-show_streams", "-of", "json", "{{input1}}"],
        })
        self.assertNotIn("source", effective["paths"])
        for raw_args in (["C:/Windows/win.ini"], ["../secret.wav"], ["https://example.com/a.wav"], ["-"]):
            with self.subTest(raw_args=raw_args):
                with self.assertRaises(RuntimeError):
                    plugin._validate_raw_args(raw_args)


if __name__ == "__main__":
    unittest.main()
