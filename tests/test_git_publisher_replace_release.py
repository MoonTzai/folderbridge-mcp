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
SOURCE = ROOT / "Plugins" / "extensions" / "git-publisher" / "plugin.py"


def plugin_module():
    spec = importlib.util.spec_from_file_location("git_publisher_replace_tests", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(value):
    return subprocess.CompletedProcess([], 0, json.dumps(value).encode("utf-8"), b"")


class ReplaceExistingReleaseTests(unittest.TestCase):
    def setUp(self):
        self.plugin = plugin_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = b"new web HTML\n"
        (self.root / "web.html").write_bytes(self.content)
        self.new_sha = hashlib.sha256(self.content).hexdigest()
        self.old_sha = "c" * 64
        self.head = "b" * 40
        self.tag_commit = "a" * 40
        self.repo = {
            "owner": "MoonTzai", "repo": "debate-coach", "repo_id": 1292008996,
            "full_name": "MoonTzai/debate-coach", "branch": "main", "head": self.head,
        }
        self.params = {
            "owner": "MoonTzai", "repo": "debate-coach", "repo_id": 1292008996,
            "branch": "main", "expected_branch_sha": self.head,
            "tag": "v9.3.12", "expected_tag_commit_sha": self.tag_commit,
            "expected_release_id": 438807643,
            "assets": [{
                "path": "web.html", "name": "Debate-Coach-web-v9.3.12.html",
                "label": "Web version",
                "sha256": self.new_sha, "expected_asset_id": 726489373,
                "expected_asset_sha256": self.old_sha,
            }],
        }
        self.current = {
            "id": 438807643, "tag_name": "v9.3.12",
            "name": "Debate-Coach v9.3.12", "draft": False, "prerelease": False,
            "target_commitish": "main",
            "html_url": "https://github.com/MoonTzai/debate-coach/releases/tag/v9.3.12",
            "assets": [
                {"id": 726489373, "name": "Debate-Coach-web-v9.3.12.html",
                 "size": 21, "digest": "sha256:" + self.old_sha, "label": "Web version"},
                {"id": 919, "name": "untouched.txt", "size": 10,
                 "digest": "sha256:" + "d" * 64, "label": "Unchanged"},
            ],
        }
        self.uploads = []

    def fake_gh(self, _root, *args, **kwargs):
        if args[:2] == ("api", "repos/MoonTzai/debate-coach/releases/tags/v9.3.12"):
            return response(self.current)
        if args[:3] == ("release", "upload", "v9.3.12"):
            self.assertIn("--clobber", args)
            self.assertIn("--repo", args)
            self.assertNotIn("release", args[3:][1:])
            snapshots = [x.split("#", 1)[0] for x in args[3:] if x.endswith("#Web version")]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(Path(snapshots[0]).read_bytes(), self.content)
            self.uploads.append(tuple(args))
            entry = self.current["assets"][0]
            entry.update({"id": 726490000, "size": len(self.content),
                          "digest": "sha256:" + self.new_sha, "label": "Web version"})
            return response({})
        raise AssertionError(f"unexpected GitHub operation: {args!r}")

    def invoke_publish(self, *, tag=None, head=None):
        with (
            mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="mock_secret"),
            mock.patch.object(self.plugin, "_remote_identity", return_value={**self.repo, **({"head": head} if head else {})}),
            mock.patch.object(self.plugin, "_remote_tag_sha", return_value=tag or self.tag_commit),
            mock.patch.object(self.plugin, "_run_gh", side_effect=self.fake_gh),
        ):
            return self.plugin._replace_existing_release_assets(self.root, self.params)

    def test_manifest_schema_is_pinned_and_has_no_token(self):
        from folderbridge_mcp.extensions import load_extension, validate_json_schema
        record = load_extension(SOURCE.parent, bundled=False)
        self.assertEqual(record.manifest.version, "1.6.3")
        action = record.manifest.actions["replace-existing-release-assets"]
        self.assertEqual(action.run_mode, "job")
        self.assertEqual(action.timeout_seconds, 7200)
        self.assertEqual(action.authorization, "global")
        manifest = json.loads((SOURCE.parent / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["actions"]["replace-existing-release-assets"]["effect_contract"]["effect_semantics"], "external_effect")
        validate_json_schema(self.params, action.input_schema, path="params")
        keys = action.input_schema["properties"]
        self.assertNotIn("token", keys)
        self.assertNotIn("password", keys)
        for p in ("expected_release_id", "expected_tag_commit_sha", "expected_branch_sha", "repo_id"):
            self.assertIn(p, action.input_schema["required"])
        self.assertEqual(action.input_schema["properties"]["assets"]["items"]["required"],
                         ["path", "name", "sha256", "expected_asset_id", "expected_asset_sha256"])

    def test_same_version_old_tag_can_replace_only_named_asset(self):
        out = self.invoke_publish()
        self.assertEqual(out["tag"], "v9.3.12")
        self.assertEqual(out["release_id"], 438807643)
        self.assertEqual(out["replaced"], ["Debate-Coach-web-v9.3.12.html"])
        self.assertEqual(out["already_current"], [])
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.current["assets"][1]["id"], 919)
        self.assertFalse(out["safety"]["tag_rewrite"])
        self.assertFalse(out["safety"]["release_deletion"])

    def test_partial_success_retry_is_noop_without_deleting_asset_again(self):
        self.current["assets"][0].update(
            {"id": 726490000, "size": len(self.content), "digest": "sha256:" + self.new_sha}
        )
        out = self.invoke_publish()
        self.assertEqual(out["replaced"], [])
        self.assertEqual(out["already_current"], ["Debate-Coach-web-v9.3.12.html"])
        self.assertFalse(self.uploads)

    def test_old_tag_change_fails_before_upload(self):
        with self.assertRaisesRegex(RuntimeError, "tag commit SHA changed"):
            self.invoke_publish(tag="d" * 40)
        self.assertFalse(self.uploads)

    def test_current_branch_change_fails_before_upload(self):
        with self.assertRaisesRegex(RuntimeError, "branch HEAD changed"):
            self.invoke_publish(head="d" * 40)
        self.assertFalse(self.uploads)

    def test_release_identity_change_fails_before_upload(self):
        self.current["id"] = 123
        with self.assertRaisesRegex(RuntimeError, "Release identity"):
            self.invoke_publish()
        self.assertFalse(self.uploads)

    def test_existing_asset_id_change_fails_before_upload(self):
        self.current["assets"][0]["id"] = 726489374
        with self.assertRaisesRegex(RuntimeError, "asset ID/SHA"):
            self.invoke_publish()
        self.assertFalse(self.uploads)

    def test_existing_asset_digest_change_fails_before_upload(self):
        self.current["assets"][0]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(RuntimeError, "asset ID/SHA"):
            self.invoke_publish()
        self.assertFalse(self.uploads)

    def test_unlisted_asset_tampering_after_upload_is_detected(self):
        original = self.fake_gh

        def tamper(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[:3] == ("release", "upload", "v9.3.12"):
                self.current["assets"][1]["size"] = 11
            return result

        with (
            mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="mock_secret"),
            mock.patch.object(self.plugin, "_remote_identity", return_value=self.repo),
            mock.patch.object(self.plugin, "_remote_tag_sha", return_value=self.tag_commit),
            mock.patch.object(self.plugin, "_run_gh", side_effect=tamper),
        ):
            with self.assertRaisesRegex(RuntimeError, "unspecified existing Release asset"):
                self.plugin._replace_existing_release_assets(self.root, self.params)
        self.assertEqual(len(self.uploads), 1)

    def test_existing_release_mode_does_not_relax_new_release_action(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("remote Release already exists: refusing to overwrite or re-upload assets", source)
        self.assertIn("remote Release tag already targets a different commit", source)


if __name__ == "__main__":
    unittest.main()
