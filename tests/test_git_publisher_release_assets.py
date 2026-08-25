from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "extensions" / "git-publisher"


def load_plugin():
    spec = importlib.util.spec_from_file_location("folderbridge_test_git_publisher_release_assets", EXT_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [shutil.which("git.exe") or shutil.which("git") or "git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


@unittest.skipUnless(sys.platform == "win32" and shutil.which("git.exe"), "Git Publisher release tests require Git for Windows")
class GitPublisherReleaseAssetValidationTests(unittest.TestCase):
    def make_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "FolderBridge Test")
        git(root, "config", "user.email", "folderbridge-test@example.invalid")
        git(root, "remote", "add", "origin", "https://github.com/example/folderbridge-test.git")
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(root, "add", "--", "tracked.txt")
        git(root, "commit", "-m", "initial")
        return temp, root

    def test_generic_release_asset_allows_generated_file_and_friendly_name(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        dist = root / "dist"
        dist.mkdir()
        artifact = dist / "app.exe"
        artifact.write_bytes(b"artifact")

        assets = plugin._clean_release_assets(
            root,
            [{"path": "dist/app.exe", "name": "App-v1.2.3（Windows版）.exe"}],
        )

        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["path"], "dist/app.exe")
        self.assertEqual(assets[0]["name"], "App-v1.2.3（Windows版）.exe")
        self.assertEqual(assets[0]["size"], len(b"artifact"))
        self.assertEqual(len(assets[0]["sha256"]), 64)

    def test_generic_release_assets_reject_escape_sensitive_and_duplicate_names(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
        (root / ".env.production").write_text("SECRET=value\n", encoding="utf-8")
        (root / "one.bin").write_bytes(b"1")
        (root / "two.bin").write_bytes(b"2")

        with self.assertRaises(RuntimeError):
            plugin._clean_release_assets(root, [{"path": "../outside.bin"}])
        for sensitive in (".env", ".env.production"):
            with self.subTest(sensitive=sensitive):
                with self.assertRaises(RuntimeError):
                    plugin._clean_release_assets(root, [{"path": sensitive}])
        with self.assertRaisesRegex(RuntimeError, "duplicate Release asset name"):
            plugin._clean_release_assets(
                root,
                [
                    {"path": "one.bin", "name": "Same.bin"},
                    {"path": "two.bin", "name": "same.BIN"},
                ],
            )

    def test_generic_release_asset_name_is_a_filename_not_a_path(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        (root / "app.bin").write_bytes(b"x")

        for bad in ("../app.bin", "nested/app.bin", "nested\\app.bin", "bad?.bin", "asset#label.bin", "asset[1].bin", "CON"):
            with self.subTest(name=bad):
                with self.assertRaises(RuntimeError):
                    plugin._clean_release_assets(root, [{"path": "app.bin", "name": bad}])

    def test_release_uploads_use_verified_temp_snapshots_even_without_rename(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        artifact = root / "artifact.bin"
        artifact.write_bytes(b"stable")
        assets = plugin._clean_release_assets(root, [{"path": "artifact.bin"}])

        with tempfile.TemporaryDirectory() as temp_dir:
            uploads = plugin._prepare_release_upload_paths(root, assets, Path(temp_dir))
            self.assertEqual(len(uploads), 1)
            upload = Path(uploads[0])
            self.assertEqual(upload.parent, Path(temp_dir))
            self.assertNotEqual(upload.resolve(), artifact.resolve())
            self.assertEqual(upload.read_bytes(), b"stable")

    def test_generic_release_preflights_gh_auth_before_mutating_tag(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        artifact = root / "artifact.bin"
        artifact.write_bytes(b"stable")
        events: list[str] = []
        head = git(root, "rev-parse", "HEAD")
        original_snapshot = plugin._prepare_release_upload_paths

        def token_preflight(_root: Path) -> str:
            events.append("token")
            return "secret"

        def snapshot_assets(_root: Path, assets: list[dict[str, object]], temp_root: Path) -> list[str]:
            events.append("snapshot")
            return original_snapshot(_root, assets, temp_root)

        def ensure_tag(_root: Path, _repo: dict[str, str], _tag: str, _title: str) -> str:
            events.append("tag")
            return head

        gh_results = [
            subprocess.CompletedProcess([], 1, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b'{"tagName":"v1.2.3","url":"https://github.com/example/folderbridge-test/releases/tag/v1.2.3","assets":[{"name":"artifact.bin","size":6}]}',
                b"",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                b'{"latestRelease":{"tagName":"v1.2.3"}}',
                b"",
            ),
        ]

        with (
            mock.patch.object(plugin, "_github_token_from_gcm", side_effect=token_preflight),
            mock.patch.object(plugin, "_prepare_release_upload_paths", side_effect=snapshot_assets),
            mock.patch.object(plugin, "_ensure_generic_release_tag", side_effect=ensure_tag),
            mock.patch.object(plugin, "_run_gh", side_effect=gh_results),
            mock.patch.object(plugin, "_remote_tag_target", return_value=(head, [])),
        ):
            result = plugin._release_assets(
                root,
                "v1.2.3",
                "Demo v1.2.3",
                [{"path": "artifact.bin"}],
                latest=True,
            )

        self.assertEqual(events, ["token", "snapshot", "tag"])
        self.assertTrue(result["released"])

    def test_generic_release_latest_false_is_explicit(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        artifact = root / "artifact.bin"
        artifact.write_bytes(b"stable")
        head = git(root, "rev-parse", "HEAD")
        calls: list[tuple[str, ...]] = []

        def run_gh(_root: Path, *args: str, **_kwargs: object):
            calls.append(args)
            if args[:2] == ("release", "view") and "--json" not in args:
                return subprocess.CompletedProcess([], 1, b"", b"")
            if args[:2] == ("release", "view") and "--json" in args:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b'{"tagName":"v1.2.3","url":"https://github.com/example/folderbridge-test/releases/tag/v1.2.3","assets":[{"name":"artifact.bin","size":6}]}',
                    b"",
                )
            if args[:2] == ("repo", "view"):
                return subprocess.CompletedProcess(
                    [],
                    0,
                    b'{"latestRelease":{"tagName":"v9.9.9"}}',
                    b"",
                )
            return subprocess.CompletedProcess([], 0, b"", b"")

        with (
            mock.patch.object(plugin, "_github_token_from_gcm", return_value="secret"),
            mock.patch.object(plugin, "_ensure_generic_release_tag", return_value=head),
            mock.patch.object(plugin, "_run_gh", side_effect=run_gh),
            mock.patch.object(plugin, "_remote_tag_target", return_value=(head, [])),
        ):
            plugin._release_assets(
                root,
                "v1.2.3",
                "Demo v1.2.3",
                [{"path": "artifact.bin"}],
                latest=False,
            )

        create = next(args for args in calls if args[:2] == ("release", "create"))
        self.assertIn("--latest=false", create)
        self.assertNotIn("--latest", create)

    def test_latest_postcondition_rejects_mismatch(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)
        completed = subprocess.CompletedProcess(
            [],
            0,
            b'{"latestRelease":{"tagName":"v9.9.9"}}',
            b"",
        )
        with mock.patch.object(plugin, "_run_gh", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "latest-state mismatch"):
                plugin._verify_release_latest(
                    root,
                    "example/folderbridge-test",
                    "v1.2.3",
                    True,
                    "secret",
                )

    def test_generic_release_tag_and_title_are_bounded_and_safe(self) -> None:
        plugin = load_plugin()
        temp, root = self.make_repo()
        self.addCleanup(temp.cleanup)

        self.assertEqual(plugin._clean_release_tag(root, "v1.2.3"), "v1.2.3")
        self.assertEqual(plugin._clean_release_title(" Debate Coach v1.2.3 "), "Debate Coach v1.2.3")
        for bad in ("-latest", "bad tag", "refs/tags/v1", "v1..2"):
            with self.subTest(tag=bad):
                with self.assertRaises(RuntimeError):
                    plugin._clean_release_tag(root, bad)
        for bad in ("", "line1\nline2", "x" * 257):
            with self.subTest(title=bad[:20]):
                with self.assertRaises(RuntimeError):
                    plugin._clean_release_title(bad)


if __name__ == "__main__":
    unittest.main()
