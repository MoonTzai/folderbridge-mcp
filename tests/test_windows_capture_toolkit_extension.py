from __future__ import annotations

import importlib.util
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "windows-capture-toolkit"
SPEC = importlib.util.spec_from_file_location("folderbridge_windows_capture_plugin", PLUGIN_ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class WindowsCaptureManifestTests(unittest.TestCase):
    def test_manifest_is_v2_on_runtime_abi1_with_minimal_permission(self):
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["runtime_abi"], 1)
        self.assertEqual(manifest["id"], "windows-capture-toolkit")
        self.assertEqual(manifest["version"], "0.1.3")
        self.assertEqual(manifest["permissions"], ["workspace.write", "process.execute:FolderBridge.exe"])
        self.assertNotIn('"pattern"', (PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest["actions"]),
            {
                "status", "list-windows", "list-monitors", "capture-window",
                "capture-active-window", "capture-monitor", "capture-region", "click-window", "launch-demo-folderbridge",
            },
        )
        for name in ("capture-window", "capture-active-window", "capture-monitor", "capture-region"):
            action = manifest["actions"][name]
            self.assertFalse(action["read_only"])
            self.assertTrue(action["requires_workspace"])
            self.assertEqual(
                action["mutation_scope"],
                {"mode": "paths", "claims": [{"param": "output_path", "kind": "exact"}]},
            )
            self.assertEqual(action["input_schema"]["properties"]["delay_ms"]["maximum"], 30000)
        for name in ("capture-window", "capture-active-window"):
            props = manifest["actions"][name]["input_schema"]["properties"]
            capture_mode = props["capture_mode"]
            self.assertEqual(capture_mode["enum"], ["window_surface", "visible_pixels"])
            self.assertEqual(capture_mode["default"], "window_surface")
            self.assertTrue(props["restore_minimized"]["default"])
        click = manifest["actions"]["click-window"]
        self.assertFalse(click["read_only"])
        self.assertFalse(click["requires_workspace"])
        self.assertEqual(click["mutation_scope"], {"mode": "none"})
        self.assertEqual(click["input_schema"]["required"], ["window_handle", "x", "y"])
        self.assertEqual(click["input_schema"]["properties"]["after_click_ms"]["maximum"], 5000)
        demo = manifest["actions"]["launch-demo-folderbridge"]
        self.assertFalse(demo["read_only"])
        self.assertFalse(demo["requires_workspace"])
        self.assertEqual(demo["run_mode"], "job")
        self.assertEqual(demo["timeout_seconds"], 0)
        self.assertEqual(demo["mutation_scope"], {"mode": "none"})
        joined = " ".join(manifest["permissions"])
        self.assertNotIn("network.", joined)
        self.assertIn("process.execute:FolderBridge.exe", manifest["permissions"])

    def test_read_only_actions_require_global_extension_approval_but_no_workspace(self):
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        for name in ("status", "list-windows", "list-monitors"):
            action = manifest["actions"][name]
            self.assertTrue(action["read_only"])
            self.assertFalse(action["requires_workspace"])
            self.assertEqual(action["authorization"], "global")

    def test_readme_states_visible_pixel_and_no_drm_bypass(self):
        text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8").casefold()
        self.assertIn("visible pixels", text)
        self.assertIn("window_surface", text)
        self.assertIn("printwindow", text)
        self.assertIn("drm/hdcp", text)
        self.assertIn("no continuous/background recording", text)
        self.assertIn("video", text)
        self.assertIn("streaming", text)
        self.assertIn("restore_minimized", text)
        self.assertIn("launch-demo-folderbridge", text)
        self.assertIn("isolated", text)
        self.assertIn("click-window", text)
        self.assertIn("left click", text)
        self.assertIn("no keyboard", text)


class WindowsCapturePureFunctionTests(unittest.TestCase):
    def test_handle_parser_is_exact_hex_only(self):
        self.assertEqual(plugin._parse_handle("0x10"), 16)
        for value in ("16", "0", "0x0", "0xGG", "", None):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    plugin._parse_handle(value)

    def test_capture_mode_defaults_to_exact_window_surface_and_rejects_unknown(self):
        self.assertEqual(plugin._capture_mode({}), "window_surface")
        self.assertEqual(plugin._capture_mode({"capture_mode": "visible_pixels"}), "visible_pixels")
        with self.assertRaises(Exception):
            plugin._capture_mode({"capture_mode": "mystery"})

    def test_source_contains_exact_window_surface_and_bounded_demo_launch(self):
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("PrintWindow", source)
        self.assertIn("PW_RENDERFULLCONTENT", source)
        self.assertIn("windows-window-surface-printwindow", source)
        self.assertIn("ShowWindowAsync", source)
        self.assertIn("LOCALAPPDATA", source)
        self.assertIn("APPDATA", source)
        self.assertIn("PYINSTALLER_RESET_ENVIRONMENT", source)
        self.assertIn("SW_SHOWMINNOACTIVE", source)
        self.assertIn("folderbridge-capture-demo-", source)
        self.assertIn("mouse_event", source)
        self.assertIn("CLICK_POINT_OUTSIDE_WINDOW", source)
        self.assertIn("absolute_desktop_click", source)
        self.assertNotIn("shell=True", source)

    def test_png_encoder_writes_valid_signature_and_dimensions(self):
        # Two pixels, BGRA: red then green.
        raw = bytes([0, 0, 255, 0, 0, 255, 0, 0])
        png = plugin._encode_png_from_bgra(raw, 2, 1)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(png[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">I", png[16:20])[0], 2)
        self.assertEqual(struct.unpack(">I", png[20:24])[0], 1)
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))

    def test_pixel_stats_flags_only_near_black_sample_as_warning(self):
        black = bytes([0, 0, 0, 0] * 100)
        bright = bytes([255, 255, 255, 0] * 100)
        black_stats = plugin._pixel_stats(black, 10, 10)
        bright_stats = plugin._pixel_stats(bright, 10, 10)
        self.assertTrue(black_stats["near_blank"])
        self.assertIsNotNone(black_stats["warning"])
        self.assertFalse(bright_stats["near_blank"])
        self.assertIsNone(bright_stats["warning"])

    def test_capture_rect_enforces_pixel_bound(self):
        self.assertEqual(plugin._validate_capture_rect(plugin.RECT(0, 0, 1920, 1080)), (0, 0, 1920, 1080))
        with self.assertRaises(Exception):
            plugin._validate_capture_rect(plugin.RECT(0, 0, 10000, 10000))
        with self.assertRaises(Exception):
            plugin._validate_capture_rect(plugin.RECT(10, 10, 10, 20))

    def test_output_path_is_png_workspace_relative_and_guarded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            context = {"workspace_root": str(root), "workspace_read_only": False}
            relative, target = plugin._resolve_output(context, "Output/capture/frame.png")
            self.assertEqual(relative.as_posix(), "Output/capture/frame.png")
            self.assertEqual(target, root / "Output" / "capture" / "frame.png")
            for bad in (
                "../frame.png", "/tmp/frame.png", r"C:\\frame.png", ".git/frame.png",
                "node_modules/frame.png", ".folderbridge.json", "Output/frame.jpg",
                "Output/CON.png", "Output /frame.png",
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(Exception):
                        plugin._resolve_output(context, bad)

    def test_atomic_publish_requires_hash_guard_for_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "frame.png"
            first = b"one"
            second = b"two"
            sha1 = plugin._publish_png(target, first, overwrite=False, expected_target_sha256=None)
            self.assertEqual(target.read_bytes(), first)
            with self.assertRaises(Exception):
                plugin._publish_png(target, second, overwrite=False, expected_target_sha256=None)
            with self.assertRaises(Exception):
                plugin._publish_png(target, second, overwrite=True, expected_target_sha256="0" * 64)
            sha2 = plugin._publish_png(target, second, overwrite=True, expected_target_sha256=sha1)
            self.assertEqual(target.read_bytes(), second)
            self.assertNotEqual(sha1, sha2)


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("GITHUB_ACTIONS") != "true",
    "live enumeration requires an interactive Windows desktop",
)
class WindowsCaptureLiveReadOnlyTests(unittest.TestCase):
    def test_status_and_monitor_enumeration_are_available(self):
        status = plugin._status()
        self.assertTrue(status["ready"])
        self.assertTrue(status["capabilities"]["exact_window_surface"])
        self.assertTrue(status["capabilities"]["visible_window_pixels"])
        self.assertTrue(status["capabilities"]["temporary_restore_minimized"])
        self.assertTrue(status["capabilities"]["isolated_folderbridge_demo_instance"])
        self.assertTrue(status["capabilities"]["input_control"])
        self.assertTrue(status["capabilities"]["bounded_left_click"])
        self.assertFalse(status["capabilities"]["keyboard_input"])
        self.assertFalse(status["capabilities"]["drag_input"])
        self.assertFalse(status["capabilities"]["absolute_desktop_click"])
        self.assertFalse(status["capabilities"]["continuous_recording"])
        self.assertFalse(status["capabilities"]["drm_hdcp_bypass"])
        monitors = plugin._list_monitors()
        self.assertGreaterEqual(len(monitors), 1)
        for index, monitor in enumerate(monitors):
            self.assertEqual(monitor["index"], index)
            self.assertTrue(monitor["monitor_handle"].startswith("0x"))
            self.assertGreater(monitor["rect"]["width"], 0)
            self.assertGreater(monitor["rect"]["height"], 0)

    def test_window_enumeration_is_bounded_and_has_exact_handles(self):
        windows = plugin._list_windows({"max_results": 20, "include_untitled": False})
        self.assertLessEqual(len(windows), 20)
        for item in windows:
            self.assertRegex(item["window_handle"], r"^0x[0-9A-F]+$")
            self.assertGreater(item["rect"]["width"], 0)
            self.assertGreater(item["rect"]["height"], 0)


if __name__ == "__main__":
    unittest.main()
