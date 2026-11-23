from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "Plugins" / "extensions" / "git-publisher" / "plugin.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("folderbridge_test_git_publisher_remote", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gh_json(value):
    return subprocess.CompletedProcess([], 0, json.dumps(value).encode("utf-8"), b"")


class RemotePublishingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b"one self-contained HTML file\n"
        self.file = self.root / "timer.html"
        self.file.write_bytes(self.payload)
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.before = "a" * 40
        self.head = "b" * 40
        self.after = "c" * 40
        self.repo = {
            "id": 1308226967,
            "full_name": "MoonTzai/debate-timer",
            "owner": {"login": "MoonTzai"},
            "permissions": {"push": True},
            "archived": False,
        }

    def api_read(self, branch_head=None, file_sha=None):
        head = branch_head or self.head
        git_blob = file_sha or self.before

        def run(_root, *args, **kwargs):
            endpoint = str(args[1]) if len(args) > 1 else ""
            if endpoint == "repos/MoonTzai/debate-timer":
                return gh_json(self.repo)
            if endpoint == "user":
                return gh_json({"login": "MoonTzai"})
            if endpoint == "repos/MoonTzai/debate-timer/branches/master":
                return gh_json({"commit": {"sha": head}})
            if endpoint.startswith("repos/MoonTzai/debate-timer/contents/"):
                return gh_json({"type": "file", "sha": git_blob, "size": len(self.payload)})
            raise AssertionError(("unexpected gh request", args))
        return run

    def params(self):
        return {
            "owner": "MoonTzai", "repo": "debate-timer", "repo_id": 1308226967,
            "branch": "master", "expected_branch_sha": self.head,
            "source_path": "timer.html", "source_sha256": self.sha,
            "remote_path": "辩论赛计时器-Debate-Timer.html",
            "expected_blob_sha": self.before, "message": "Update vetted timer",
        }

    def test_remote_actions_pass_host_input_schema_validator(self):
        from folderbridge_mcp.extensions import load_extension, validate_json_schema
        record = load_extension(PLUGIN.parent, bundled=False)
        update = record.manifest.actions["remote-update-file"]
        release = record.manifest.actions["remote-release-assets"]
        update_params = self.params()
        release_params = {
            "owner": "MoonTzai", "repo": "debate-timer", "repo_id": 1308226967,
            "branch": "master", "expected_branch_sha": self.head,
            "tag": "v9.3.9", "title": "Debate Timer v9.3.9", "latest": True,
            "assets": [{"path": "timer.html", "name": "Debate-Timer-v9.3.9.html",
                        "sha256": self.sha}],
        }
        validate_json_schema(update_params, update.input_schema, path="params")
        validate_json_schema(release_params, release.input_schema, path="params")
        with self.assertRaisesRegex(RuntimeError, "expected blob SHA"):
            self.plugin._remote_expected_sha("x" * 40, name="expected blob SHA", size=40)

    def test_remote_update_rejects_stale_blob_before_mutation(self):
        calls = []
        def fake(_root, *args, **kwargs):
            calls.append(args)
            return self.api_read(file_sha="d"*40)(_root, *args, **kwargs)
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"), mock.patch.object(self.plugin, "_run_gh", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "blob SHA"):
                self.plugin._remote_update_file(self.root, self.params())
        self.assertFalse(any("-X" in call for call in calls))

    def test_remote_update_rejects_stale_branch_before_mutation(self):
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"), mock.patch.object(self.plugin, "_run_gh", side_effect=self.api_read(branch_head="d"*40)):
            with self.assertRaisesRegex(RuntimeError, "branch SHA"):
                self.plugin._remote_update_file(self.root, self.params())

    def test_remote_source_rejects_path_escape_and_tamper(self):
        with self.assertRaises(RuntimeError):
            self.plugin._remote_source(self.root, "../timer.html", self.sha)
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            self.plugin._remote_source(self.root, "timer.html", "0" * 64)

    def test_remote_update_requires_repository_identity(self):
        tampered = self.params()
        tampered["repo_id"] = 123
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"), mock.patch.object(self.plugin, "_run_gh", side_effect=self.api_read()):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.plugin._remote_update_file(self.root, tampered)

    def test_remote_update_noop_when_blob_matches_source(self):
        git_blob = self.plugin._git_blob_sha(self.payload)
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"), mock.patch.object(self.plugin, "_run_gh", side_effect=self.api_read(file_sha=git_blob)):
            result = self.plugin._remote_update_file(self.root, self.params())
        self.assertTrue(result["already_current"])
        self.assertFalse(result["updated"])

    def test_remote_update_writes_pinned_contents_and_verifies_blob(self):
        intended = self.plugin._git_blob_sha(self.payload)
        state = {"reads": 0, "write": None}

        def fake_run(_root, *args, **_kwargs):
            endpoint = args[1] if len(args) > 1 else ""
            if args[:3] == ("api", "-X", "PUT"):
                import base64
                request = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
                self.assertEqual(base64.b64decode(request["content"]), self.payload)
                self.assertEqual(request["sha"], self.before)
                self.assertEqual(request["branch"], "master")
                state["write"] = request
                return gh_json({"content": {"sha": intended}, "commit": {"sha": self.after}})
            if endpoint == "repos/MoonTzai/debate-timer":
                return gh_json(self.repo)
            if endpoint == "user":
                return gh_json({"login": "MoonTzai"})
            if endpoint == "repos/MoonTzai/debate-timer/branches/master":
                return gh_json({"commit": {"sha": self.head}})
            if endpoint.startswith("repos/MoonTzai/debate-timer/contents/"):
                state["reads"] += 1
                return gh_json({"type": "file", "sha": intended if state["reads"] > 1 else self.before})
            raise AssertionError(("unexpected call", args))

        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"), mock.patch.object(self.plugin, "_run_gh", side_effect=fake_run):
            result = self.plugin._remote_update_file(self.root, self.params())
        self.assertTrue(result["updated"])
        self.assertEqual(result["blob_sha"], intended)
        self.assertEqual(result["commit"], self.after)
        self.assertEqual(state["reads"], 2)
        self.assertIsNotNone(state["write"])

    def test_remote_release_pins_tag_and_uploads_only_verified_asset(self):
        params = {
            "owner": "MoonTzai", "repo": "debate-timer", "repo_id": 1308226967,
            "branch": "master", "expected_branch_sha": self.head,
            "tag": "v9.3.9", "title": "Debate Timer v9.3.9", "latest": True,
            "assets": [{"path": "timer.html", "name": "Debate-Timer-v9.3.9.html",
                        "label": "Bilingual standalone HTML", "sha256": self.sha}],
        }
        identity = {"owner": "MoonTzai", "repo": "debate-timer", "repo_id": 1308226967,
                    "full_name": "MoonTzai/debate-timer", "branch": "master", "head": self.head}
        records = {"posted_tag": False, "uploaded_file": False}
        def fake_run(_root, *args, **_kwargs):
            if args[:3] == ("api", "-X", "POST"):
                tag_request = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
                self.assertEqual(tag_request, {"ref": "refs/tags/v9.3.9", "sha": self.head})
                records["posted_tag"] = True
                return gh_json({"ref": "refs/tags/v9.3.9"})
            if args[:3] == ("release", "view", "v9.3.9") and "--json" not in args:
                return subprocess.CompletedProcess([], 1, b"", b"release not found")
            if args[:3] == ("release", "create", "v9.3.9"):
                self.assertTrue(records["posted_tag"])
                paths = [s.split("#", 1)[0] for s in args if s.endswith("#Bilingual standalone HTML")]
                self.assertEqual(len(paths), 1)
                self.assertEqual(Path(paths[0]).read_bytes(), self.payload)
                records["uploaded_file"] = True
                return gh_json({})
            if args[:3] == ("release", "view", "v9.3.9") and "--json" in args:
                return gh_json({"tagName": "v9.3.9",
                                "url": "https://github.com/MoonTzai/debate-timer/releases/tag/v9.3.9",
                                "assets": [{"name": "Debate-Timer-v9.3.9.html",
                                            "size": len(self.payload),
                                            "label": "Bilingual standalone HTML",
                                            "digest": "sha256:" + self.sha}]})
            raise AssertionError(("unexpected call", args))
        with (
            mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="test_secret"),
            mock.patch.object(self.plugin, "_remote_identity", return_value=identity),
            mock.patch.object(self.plugin, "_remote_tag_sha", side_effect=[None, self.head, self.head]),
            mock.patch.object(self.plugin, "_clean_release_tag", return_value="v9.3.9"),
            mock.patch.object(self.plugin, "_verify_release_latest"),
            mock.patch.object(self.plugin, "_run_gh", side_effect=fake_run),
        ):
            result = self.plugin._remote_release_assets(self.root, params)
        self.assertTrue(records["posted_tag"])
        self.assertTrue(records["uploaded_file"])
        self.assertTrue(result["released"])
        self.assertEqual(result["head"], self.head)

    def test_remote_release_requires_exact_asset_sha(self):
        params = {
            "owner": "MoonTzai", "repo": "debate-timer", "repo_id": 1308226967,
            "branch": "master", "expected_branch_sha": self.head,
            "tag": "v9.3.9", "title": "Debate Timer v9.3.9", "latest": True,
            "assets": [{"path": "timer.html", "name": "Debate-Timer-v9.3.9.html",
                        "sha256": "0" * 64}],
        }
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            self.plugin._remote_release_assets(self.root, params)


if __name__ == "__main__":
    unittest.main()
