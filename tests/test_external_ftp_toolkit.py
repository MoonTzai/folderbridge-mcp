from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "Plugins" / "extensions" / "ftp-toolkit"
SPEC = importlib.util.spec_from_file_location("published_ftp_toolkit", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PublishedFtpToolkitTests(unittest.TestCase):
    def test_manifest_is_generic_and_secret_free(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "ftp-toolkit")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(
            set(manifest["actions"]),
            {"status", "configure", "forget", "check", "list", "stat", "mkdir", "rename", "delete", "upload", "upload-tree", "download"},
        )
        self.assertIn("process.execute:curl.exe", manifest["permissions"])
        self.assertIn("process.execute:powershell.exe", manifest["permissions"])
        self.assertNotIn("extension.state", manifest["permissions"])
        text = json.dumps(manifest).casefold()
        for forbidden in ("password", "token", "secret", "url"):
            self.assertNotIn(f'"{forbidden}"', text)
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("infinityfree", source)
        self.assertNotIn("debate-coach", source)

    def test_profile_and_remote_paths_are_bounded(self) -> None:
        self.assertEqual(plugin._profile_name("site-a"), "site-a")
        for bad in ("", "../x", "a/b", "a b", "x" * 65):
            with self.subTest(bad=bad):
                if bad == "":
                    self.assertEqual(plugin._profile_name(bad), "default")
                else:
                    with self.assertRaises(RuntimeError):
                        plugin._profile_name(bad)
        profile = {"host": "example.org", "port": 21, "remote_root": "/htdocs"}
        self.assertEqual(plugin._remote_path(profile, "a/b.txt"), "/htdocs/a/b.txt")
        self.assertTrue(plugin._remote_url(profile, "a b.txt").startswith("ftp://example.org:21/htdocs/"))
        for bad in ("/absolute", "../escape", "a\\b", "x?y"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    plugin._clean_remote_relative(bad)

    def test_local_paths_reject_escape_sensitive_and_links(self) -> None:
        for bad in ("../x.txt", "/tmp/x", "a\\b.txt", ".env", ".git/config", "keys/site.pem", "node_modules/a.js"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    plugin._clean_local_relative(bad)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "safe").mkdir()
            (root / "safe" / "a.txt").write_text("a", encoding="utf-8")
            rel, path = plugin._resolve_local_input(root, "safe/a.txt", directory=False)
            self.assertEqual(rel.as_posix(), "safe/a.txt")
            self.assertEqual(path.read_text(encoding="utf-8"), "a")

    def test_tree_plan_is_generic_and_preserves_relative_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            base = root / "publish"
            (base / "sub").mkdir(parents=True)
            (base / "a.txt").write_bytes(b"a")
            (base / "sub" / "b.bin").write_bytes(b"bb")
            (base / ".env").write_text("SECRET=x", encoding="utf-8")
            _, remote_base, items = plugin._tree_plan(root, "publish", "release/v1", 10)
            self.assertEqual(remote_base, "release/v1")
            self.assertEqual([item["relative"] for item in items], ["a.txt", "sub/b.bin"])
            self.assertEqual([item["remote"] for item in items], ["release/v1/a.txt", "release/v1/sub/b.bin"])

    def test_rename_uses_only_ftp_rename_commands(self) -> None:
        profile = {"host": "example.org", "port": 21, "remote_root": "/root", "mode": "ftps-explicit", "insecure_tls": False}
        captured = []

        def fake_exec(profile_arg, credential_arg, args, **kwargs):
            captured.extend(args)
            return 0, b"", b""

        with patch.object(plugin, "_read_profile", return_value=(profile, ("user", "pass"))), patch.object(plugin, "_curl_exec", side_effect=fake_exec):
            result = plugin._rename({"profile": "p", "from_path": "a.tmp", "to_path": "a.json"})
        self.assertTrue(result["ok"])
        joined = " ".join(captured)
        self.assertIn("RNFR /root/a.tmp", joined)
        self.assertIn("RNTO /root/a.json", joined)
        self.assertNotIn("DELE", joined)

    def test_upload_argv_never_embeds_credentials_in_url(self) -> None:
        profile = {"host": "example.org", "port": 21, "remote_root": "/root", "mode": "ftps-explicit", "insecure_tls": False}
        url = plugin._remote_url(profile, "x.txt")
        self.assertEqual(url, "ftp://example.org:21/root/x.txt")
        self.assertNotIn("@", url)
        config = plugin._curl_config(profile, ("user", "pass")).decode("utf-8")
        self.assertIn('user = "user:pass"', config)
        self.assertNotIn("user:pass@", url)

    def test_http_connect_proxy_is_profile_bounded_and_uses_tunnel(self) -> None:
        profile = {
            "host": "example.org", "port": 21, "remote_root": "/root",
            "mode": "ftps-explicit", "insecure_tls": False,
            "proxy_mode": "http-connect", "proxy_host": "127.0.0.1", "proxy_port": 7897,
        }
        config = plugin._curl_config(profile, ("user", "pass")).decode("utf-8")
        self.assertIn('proxy = "http://127.0.0.1:7897"', config)
        self.assertIn("proxytunnel", config)
        self.assertNotIn("example.org", config)

    def test_missing_deep_parent_is_treated_as_not_existing_before_upload(self) -> None:
        profile = {"host": "example.org", "port": 21, "remote_root": "/root", "mode": "ftps-explicit", "insecure_tls": False}

        def fake_exec(profile_arg, credential_arg, args, **kwargs):
            self.assertEqual(kwargs.get("allow_codes"), {9, 78})
            return 9, b"", b""

        with patch.object(plugin, "_curl_exec", side_effect=fake_exec):
            result = plugin._stat_remote(profile, ("user", "pass"), "missing/deep/file.txt")
        self.assertFalse(result["exists"])

    def test_upload_precreates_parent_directories_level_by_level(self) -> None:
        self.assertEqual(plugin._remote_parent_dirs("a/b/c.txt"), ["a", "a/b"])
        profile = {"host": "example.org", "port": 21, "remote_root": "/root", "mode": "ftps-explicit", "insecure_tls": False}
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "c.txt"
            local.write_bytes(b"x")

            def fake_exec(profile_arg, credential_arg, args, **kwargs):
                captured.extend(args)
                return 0, b"", b""

            with patch.object(plugin, "_remote_matches", return_value=False), patch.object(plugin, "_verify_remote", return_value=None), patch.object(plugin, "_curl_exec", side_effect=fake_exec):
                changed = plugin._upload_one(profile, ("user", "pass"), local, "a/b/c.txt", "none")
        self.assertTrue(changed)
        joined = " ".join(captured)
        self.assertIn("*MKD /root/a", joined)
        self.assertIn("*MKD /root/a/b", joined)
        self.assertNotIn("--ftp-create-dirs", captured)

    def test_delete_is_exact_file_only(self) -> None:
        profile = {"host": "example.org", "port": 21, "remote_root": "/root", "mode": "ftps-explicit", "insecure_tls": False}
        captured = []

        def fake_exec(profile_arg, credential_arg, args, **kwargs):
            captured.extend(args)
            return 0, b"", b""

        with patch.object(plugin, "_read_profile", return_value=(profile, ("user", "pass"))), patch.object(plugin, "_curl_exec", side_effect=fake_exec):
            result = plugin._delete({"profile": "p", "remote_path": "old.txt"})
        self.assertEqual(result["deleted"], "old.txt")
        joined = " ".join(captured)
        self.assertIn("DELE /root/old.txt", joined)
        self.assertNotIn("*", result["deleted"])
        with patch.object(plugin, "_read_profile", return_value=(profile, ("user", "pass"))):
            with self.assertRaises(RuntimeError):
                plugin._delete({"profile": "p", "remote_path": "*.txt"})

    def test_configure_powershell_parses_without_execution(self) -> None:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("powershell.exe unavailable")
        script = PLUGIN_ROOT / "configure.ps1"
        escaped_script = str(script).replace("'", "''")
        command = (
            "$errors=$null; $tokens=$null; "
            + "[void][System.Management.Automation.Language.Parser]::ParseFile('"
            + escaped_script
            + "', [ref]$tokens, [ref]$errors); "
            + "if($errors.Count -gt 0){$errors | ForEach-Object {$_.Message}; exit 1}"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
