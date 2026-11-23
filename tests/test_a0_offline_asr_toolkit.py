from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.extensions import load_extension

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "Plugins" / "extensions" / "offline-asr-toolkit"
SPEC = importlib.util.spec_from_file_location("offline_asr_toolkit_candidate", DIR / "plugin.py")
assert SPEC and SPEC.loader
asr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(asr)


class OfflineAsrToolkitTests(unittest.TestCase):
    def test_manifest_is_accepted_by_actual_folderbridge_host(self):
        record = load_extension(DIR, bundled=False)
        self.assertEqual(record.manifest.extension_id, "offline-asr-toolkit")
        self.assertEqual(record.manifest.version, "0.1.0")
        manifest = json.loads((DIR / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["actions"]), {"status", "list-profiles", "transcribe-one"})
        self.assertIn("process.execute:python.exe", manifest["permissions"])
        self.assertNotIn("network.outbound:https", manifest["permissions"])
        action = manifest["actions"]["transcribe-one"]
        self.assertEqual(action["run_mode"], "job")
        self.assertEqual(action["mutation_scope"]["claims"], [{"param": "output_dir", "kind": "tree"}])
        self.assertEqual(set(action["input_schema"]["required"]), {"input_path", "output_dir", "profile_id"})
        self.assertNotIn("pattern", json.dumps(manifest))
        self.assertFalse(set(action["input_schema"]["properties"]) & {"cmd", "script", "url", "executable", "args"})

    def test_plugin_source_has_no_course_or_specific_model_folder(self):
        code = (DIR / "plugin.py").read_text(encoding="utf-8")
        model = (DIR / "asr_worker.py").read_text(encoding="utf-8")
        manifest = (DIR / "folderbridge-extension.json").read_text(encoding="utf-8")
        for text in (code, model, manifest):
            self.assertNotIn("Debate-Universal-Grammar", text)
            self.assertNotIn("DUG", text)
            self.assertNotIn("MiniMax", text)
            self.assertNotIn("shell=True", text)
        self.assertFalse((DIR / "profiles.json").exists())

    def test_install_script_is_staged_and_requires_user_approval(self):
        installer = (DIR / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("LOCALAPPDATA", installer)
        self.assertIn("Move-Item -LiteralPath $stage -Destination $target", installer)
        self.assertIn("Move-Item -LiteralPath $target -Destination $backup", installer)
        self.assertIn("approve and enable", installer)
        self.assertNotIn("trust.approve", installer)
        self.assertIn('Copy-Item -LiteralPath $profileSource', installer)

    def test_model_language_identifier_is_not_course_or_zh_only(self):
        self.assertIsNotNone(asr.LANGUAGE_RE.fullmatch("pt"))
        self.assertIsNotNone(asr.LANGUAGE_RE.fullmatch("zh-hans"))
        self.assertIsNotNone(asr.LANGUAGE_RE.fullmatch("auto"))
        self.assertIsNone(asr.LANGUAGE_RE.fullmatch("../../bad"))

    def test_paths_reject_escape_links_and_sensitive_locations(self):
        for raw in ("../secret.wav", "/absolute/file.wav", r"C:\windows\win.ini", ".git/data",
                    "foo/../bar", ".env", "node_modules/dep.wav", "outputs/.venv/result.wav"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                asr._relative(raw)
        self.assertEqual(asr._relative("音频/课堂录音.flac").as_posix(), "音频/课堂录音.flac")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "audio.flac").write_bytes(b"A" * 128)
            source, sha = asr._input(root, "audio.flac", 4096)
            self.assertEqual(source.stat().st_size, 128)
            self.assertEqual(sha, hashlib.sha256(b"A" * 128).hexdigest())
            with self.assertRaises(ValueError):
                asr._input(root, "audio.flac", 100)
            dest, rel = asr._output(root, "results/safe")
            self.assertEqual(rel.as_posix(), "results/safe")
            self.assertTrue(dest.is_dir())

    def test_pinned_model_identity_mismatch_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "python.exe"
            py.write_bytes(b"python-binary")
            model = root / "model"
            model.mkdir()
            (model / "model.pt").write_bytes(b"trained-weights")
            expected = hashlib.sha256(b"trained-weights").hexdigest()
            profile = {
                "id": "local-profile", "engine": "funasr", "language": "zh",
                "python_exe": str(py), "python_sha256": hashlib.sha256(py.read_bytes()).hexdigest(),
                "model": {"directory": str(model), "files": {"model.pt": expected}},
            }
            asr._validate_profile(profile)
            result = asr._verify_profile(profile, verify_hashes=True)
            self.assertTrue(result["asset_hashes_verified"])
            (model / "model.pt").write_bytes(b"mutated")
            with self.assertRaises(ValueError):
                asr._verify_profile(profile, verify_hashes=True)

    def test_read_only_workspace_fails_before_model_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                asr.handle("transcribe-one", {
                    "input_path": "x.wav", "output_dir": "out", "profile_id": "demo",
                }, {"workspace_root": tmp, "workspace_read_only": True})

    def test_profile_registry_is_optional_and_not_project_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            with patch.object(asr, "PROFILE_FILE", profile_file):
                result = asr.handle("status", {}, {"workspace_root": tmp})
                self.assertEqual(result["profiles"], [])
                self.assertTrue(result["all_workspaces"])
                profile_file.write_text('{"schema":"offline-asr-profiles-v1","profiles":[]}', encoding="utf-8")
                result = asr.handle("list-profiles", {}, {"workspace_root": tmp})
                self.assertEqual(result["profiles"], [])
                self.assertEqual(len(result["profiles_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
