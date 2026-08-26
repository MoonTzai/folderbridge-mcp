import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import folderbridge_mcp.process_control  # type: ignore
except ModuleNotFoundError:
    package = types.ModuleType("folderbridge_mcp")
    process_control = types.ModuleType("folderbridge_mcp.process_control")
    process_control.owned_process_group_kwargs = lambda **kwargs: {}
    process_control.terminate_owned_process_tree = lambda process, **kwargs: process.kill()
    sys.modules["folderbridge_mcp"] = package
    sys.modules["folderbridge_mcp.process_control"] = process_control

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plugin  # noqa: E402


class FFmpegToolkitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "media").mkdir()
        (self.root / "media" / "in.mp4").write_bytes(b"x")
        (self.root / "media" / "sub.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_absolute_parent_and_url_arguments(self):
        cases = [
            ["C:/Windows/win.ini"],
            [r"movie=C\:/Windows/win.ini"],
            ["../secret.mp4"],
            ["https://example.com/a.mp4"],
            [r"https\://example.com/a.mp4"],
            ["pipe:1"],
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(RuntimeError):
                    plugin._validate_raw_args(args)

    def test_rejects_device_capture_and_plugin_loading_filters(self):
        with self.assertRaises(RuntimeError):
            plugin._validate_raw_args(["-f", "dshow", "-i", "video=Camera"])
        with self.assertRaises(RuntimeError):
            plugin._validate_raw_args(["-vf", "scale=1280:720,zmq"])

    def test_allows_complex_ffmpeg_options_and_placeholders(self):
        args = plugin._validate_raw_args([
            "-i", "{{src}}", "-filter_complex", "[0:v]scale=1280:-2[v];[0:a]loudnorm[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "h264_nvenc", "-cq", "20", "{{dst}}",
        ])
        values, normalized = plugin._resolve_path_specs(
            self.root,
            {
                "src": {"path": "media/in.mp4", "mode": "input"},
                "dst": {"path": "out/final.mp4", "mode": "output"},
            },
            create_parents=True,
            overwrite=False,
            read_only=False,
        )
        expanded, used = plugin._expand_args(args, values)
        self.assertEqual(used, {"src", "dst"})
        self.assertIn(str(self.root / "media" / "in.mp4"), expanded)
        self.assertIn(str(self.root / "out" / "final.mp4"), expanded)
        self.assertEqual(normalized["dst"]["mode"], "output")

    def test_filter_path_placeholder_is_escaped(self):
        values, _ = plugin._resolve_path_specs(
            self.root,
            {"subs": {"path": "media/sub.srt", "mode": "input"}},
            create_parents=True,
            overwrite=False,
            read_only=True,
        )
        expanded, used = plugin._expand_args(["subtitles={{filter:subs}}"], values)
        self.assertEqual(used, {"subs"})
        self.assertIn("subtitles=", expanded[0])
        if os.name == "nt":
            self.assertIn("\\:", expanded[0])

    def test_probe_cannot_declare_outputs(self):
        with self.assertRaises(RuntimeError):
            plugin._resolve_path_specs(
                self.root,
                {"dst": {"path": "out.mp4", "mode": "output"}},
                create_parents=True,
                overwrite=False,
                read_only=True,
            )

    def test_output_pattern_is_confined_and_enumerated(self):
        values, normalized = plugin._resolve_path_specs(
            self.root,
            {"frames": {"path": "frames/frame-%04d.png", "mode": "output_pattern"}},
            create_parents=True,
            overwrite=False,
            read_only=False,
        )
        self.assertTrue(values["frames"].endswith("frame-%04d.png"))
        (self.root / "frames" / "frame-0001.png").write_bytes(b"a")
        (self.root / "frames" / "frame-0002.png").write_bytes(b"b")
        artifacts, truncated = plugin._artifact_paths(self.root, normalized, 10)
        self.assertFalse(truncated)
        self.assertEqual(artifacts, ["frames/frame-0001.png", "frames/frame-0002.png"])

    def test_unused_declared_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "not used"):
            plugin._prepare_invocation(
                self.root,
                {
                    "args": ["-version"],
                    "paths": {"src": {"path": "media/in.mp4", "mode": "input"}},
                },
                read_only=True,
            )

    def test_sensitive_paths_are_rejected(self):
        for path in (".env", "secret.pem", ".git/config", "node_modules/a.mp4"):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    plugin._clean_relative(path)

    def test_ffprobe_argv_is_tool_specific_and_does_not_inject_nostdin(self):
        argv = plugin._build_ffprobe_argv(Path("ffprobe.exe"), ["-show_format", "input.wav"])
        self.assertEqual(argv[0], "ffprobe.exe")
        self.assertNotIn("-nostdin", argv)
        self.assertEqual(argv[1:4], ["-hide_banner", "-protocol_whitelist", "file,crypto,data"])
        self.assertEqual(argv[-2:], ["-show_format", "input.wav"])

    def test_ffmpeg_argv_keeps_nostdin_and_managed_overwrite(self):
        argv = plugin._build_ffmpeg_argv(Path("ffmpeg.exe"), ["-i", "input.wav", "output.wav"], overwrite=True)
        self.assertEqual(argv[0], "ffmpeg.exe")
        self.assertIn("-nostdin", argv)
        self.assertIn("-protocol_whitelist", argv)
        self.assertIn("-y", argv)
        self.assertNotIn("-n", argv)

    def test_probe_path_with_custom_args_auto_appends_source_placeholder(self):
        effective = plugin._prepare_probe_params({
            "path": "media/in.mp4",
            "args": ["-v", "error", "-show_format", "-of", "json"],
        })
        self.assertEqual(effective["paths"]["source"], {"path": "media/in.mp4", "mode": "input"})
        self.assertEqual(effective["args"][-1], "{{source}}")
        self.assertEqual(effective["args"].count("{{source}}"), 1)

    def test_probe_path_with_explicit_source_placeholder_preserves_order(self):
        original = ["-v", "error", "{{source}}", "-show_streams", "-of", "json"]
        effective = plugin._prepare_probe_params({"path": "media/in.mp4", "args": original})
        self.assertEqual(effective["args"], original)

    def test_probe_advanced_paths_mode_is_unchanged(self):
        effective = plugin._prepare_probe_params({
            "paths": {"input1": {"path": "media/in.mp4", "mode": "input"}},
            "args": ["-show_streams", "-of", "json", "{{input1}}"],
        })
        self.assertNotIn("source", effective["paths"])
        self.assertEqual(effective["args"][-1], "{{input1}}")


if __name__ == "__main__":
    unittest.main()
