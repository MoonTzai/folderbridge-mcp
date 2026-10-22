from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "Plugins" / "extensions" / "git-publisher" / "plugin.py"


def plugin_module():
    spec = importlib.util.spec_from_file_location("git_publisher_first_publication_tests", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *argv: str) -> str:
    proc = subprocess.run(
        [shutil.which("git.exe") or shutil.which("git") or "git", *argv],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, timeout=30,
    )
    return proc.stdout.decode("utf-8", errors="replace").strip()


@unittest.skipUnless(sys.platform == "win32" and shutil.which("git.exe"), "Native Git Publisher uses Git for Windows")
class GitPublisherFirstPublicationTests(unittest.TestCase):
    def setUp(self):
        self.plugin = plugin_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.repo = self.workspace / "Case-Trajectories"
        self.repo.mkdir()
        self.context = {"workspace_root": str(self.workspace), "workspace_read_only": False}
        self.public_name = "first-publication-test"
        self.remote = {
            "id": 993042, "owner": {"login": "MoonTzai"},
            "full_name": f"MoonTzai/{self.public_name}", "private": False,
        }
        self.remote_created = False
        self.create_count = 0

    def mock_gh(self, root, *args, token, timeout=300, check=True):
        self.assertEqual(root, self.repo)
        self.assertEqual(token, "local-gcm-credential")
        if args == ("api", "user"):
            return subprocess.CompletedProcess(["gh"], 0, b'{"login":"MoonTzai"}', b"")
        if args == ("api", f"repos/MoonTzai/{self.public_name}"):
            if self.remote_created:
                return subprocess.CompletedProcess(["gh"], 0, json.dumps(self.remote).encode(), b"")
            return subprocess.CompletedProcess(["gh"], 1, b"", b"gh: Not Found (HTTP 404)")
        if args[:4] == ("api", "-X", "POST", "user/repos"):
            self.assertFalse(self.remote_created)
            self.assertIn("-F", args)
            self.assertIn("private="+str(self.remote["private"]).lower(), args)
            self.assertIn("auto_init=false", args)
            self.remote_created = True
            self.create_count += 1
            return subprocess.CompletedProcess(["gh"], 0, json.dumps(self.remote).encode(), b"")
        raise AssertionError(f"unexpected GitHub request: {args!r}")

    def init(self):
        result = self.plugin.handle("init-local-repository", {"repo_path": "Case-Trajectories"}, self.context)
        git(self.repo, "config", "user.name", "FolderBridge Test")
        git(self.repo, "config", "user.email", "folderbridge@example.invalid")
        git(self.repo, "config", "core.autocrlf", "false")
        return result

    def create(self):
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="local-gcm-credential"), \
             mock.patch.object(self.plugin, "_run_gh", side_effect=self.mock_gh):
            return self.plugin.handle("create-github-repository", {
                "repo_path": "Case-Trajectories", "owner": "MoonTzai",
                "name": self.public_name, "visibility": "public", "description": "Case Trajectories",
            }, self.context)

    def test_end_to_end_local_initial_publication_and_retries(self):
        with mock.patch.object(self.plugin, "_gcm_status", return_value={"authenticated": True}):
            preflight = self.plugin.handle(
                "first-publication-preflight", {"repo_path": "Case-Trajectories"}, self.context,
            )
        self.assertEqual(preflight["repository_state"], "not_initialized")
        self.assertTrue(preflight["ready_for_init"])
        self.assertFalse((self.repo / ".git").exists())

        self.assertFalse(self.init()["already_initialized"])
        self.assertTrue(self.plugin.handle(
            "init-local-repository", {"repo_path": "Case-Trajectories"}, self.context,
        )["already_initialized"])
        initial = self.create()
        self.assertTrue(initial["created"])
        self.assertEqual(self.create_count, 1)
        self.assertEqual(git(self.repo, "remote", "get-url", "origin"),
                         "https://github.com/MoonTzai/first-publication-test.git")
        self.assertFalse(self.create()["created"])
        self.assertEqual(self.create_count, 1)

        (self.repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
        (self.repo / "index.html").write_bytes(b"hello\r\nworld\r\n")
        (self.repo / "README.md").write_text("Case Trajectories\n", encoding="utf-8")
        paths = [".gitattributes", "README.md", "index.html"]
        committed = self.plugin.handle(
            "initial-commit", {"repo_path": "Case-Trajectories", "paths": paths, "message": "Initial release"},
            self.context,
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), committed["commit"])
        self.assertEqual(git(self.repo, "show", "--pretty=", "--name-only", "HEAD").splitlines(),
                         [".gitattributes", "README.md", "index.html"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.create_count, 1)
        retry = self.plugin.handle(
            "initial-commit", {"repo_path": "Case-Trajectories", "paths": paths, "message": "Initial release"},
            self.context,
        )
        self.assertTrue(retry["already_committed"])
        self.assertEqual(retry["commit"], committed["commit"])
        with self.assertRaisesRegex(RuntimeError, "retry cannot change"):
            self.plugin.handle(
                "initial-commit", {"repo_path": "Case-Trajectories", "paths": ["README.md"], "message": "Initial release"},
                self.context,
            )
        with mock.patch.object(self.plugin, "_gcm_status", return_value={"authenticated": True}):
            final = self.plugin.handle(
                "first-publication-preflight", {"repo_path": "Case-Trajectories"}, self.context,
            )
        self.assertEqual(final["repository_state"], "initial_committed")


    def test_connect_can_authorize_before_origin_is_created(self):
        self.init()
        original_run_git = self.plugin._run_git
        calls = []

        def git_wrapper(root, *args, **kwargs):
            if args[:4] == ("credential-manager", "github", "login", "--url"):
                calls.append(args)
                return subprocess.CompletedProcess(["git"], 0, b"authenticated", b"")
            return original_run_git(root, *args, **kwargs)

        with mock.patch.object(self.plugin, "_run_git", side_effect=git_wrapper), \
             mock.patch.object(self.plugin, "_gcm_status",
                               return_value={"available": True, "authenticated": True}):
            result = self.plugin.handle(
                "connect", {"repo_path": "Case-Trajectories", "username": "MoonTzai"},
                self.context,
            )
        self.assertTrue(result["connected"])
        self.assertEqual(result["branch"], "main")
        self.assertIsNone(result["origin"])
        self.assertEqual(len(calls), 1)

    def test_first_publication_requires_explicit_repo_path(self):
        for action, params in (
            ("first-publication-preflight", {}),
            ("init-local-repository", {}),
            ("create-github-repository",
             {"owner": "MoonTzai", "name": "first-publication-test", "visibility": "public"}),
            ("initial-commit", {"paths": ["README.md"], "message": "no"}),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "explicitly selected repo_path"):
                self.plugin.handle(action, params, self.context)


    def test_explicit_private_repository_is_created_with_private_true(self):
        self.init()
        self.remote["private"] = True
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="local-gcm-credential"), \
             mock.patch.object(self.plugin, "_run_gh", side_effect=self.mock_gh):
            result = self.plugin.handle("create-github-repository", {
                "repo_path": "Case-Trajectories", "owner": "MoonTzai",
                "name": self.public_name, "visibility": "private", "description": "Private source",
            }, self.context)
        self.assertTrue(result["created"])
        self.assertEqual(result["visibility"], "private")
        self.assertEqual(self.create_count, 1)

    def test_push_checks_remote_sha_and_rejects_mismatch(self):
        self.init()
        self.create()
        (self.repo / "README.md").write_bytes(b"initial\n")
        self.plugin.handle("initial-commit", {
            "repo_path": "Case-Trajectories", "paths": ["README.md"], "message": "Initial release",
        }, self.context)
        head = git(self.repo, "rev-parse", "HEAD")
        original_git = self.plugin._run_git
        remote_commit = [head]

        def fake_git(root, *args, **kwargs):
            if "push" in args:
                self.assertNotIn("--force", args)
                return subprocess.CompletedProcess(["git"], 0, b"push ok", b"")
            if args and args[0] == "ls-remote":
                return subprocess.CompletedProcess(["git"], 0,
                                                   (remote_commit[0]+"\trefs/heads/main\n").encode(), b"")
            return original_git(root, *args, **kwargs)

        with mock.patch.object(self.plugin, "_gcm_status", return_value={"available": True}), \
             mock.patch.object(self.plugin, "_run_git", side_effect=fake_git):
            good = self.plugin.handle("push", {"repo_path": "Case-Trajectories"}, self.context)
            self.assertEqual(good["verified_remote_head"], head)
            remote_commit[0] = "0" * 40
            with self.assertRaisesRegex(RuntimeError, "differs from local HEAD"):
                self.plugin.handle("push", {"repo_path": "Case-Trajectories"}, self.context)

    def test_existing_remote_is_not_automatically_adopted(self):
        self.init()
        self.remote_created = True
        with self.assertRaisesRegex(RuntimeError, "already exists|ambiguous"):
            self.create()
        self.assertEqual(self.create_count, 0)
        self.assertNotIn("origin", git(self.repo, "remote"))

    def test_existing_non_publisher_repository_is_not_claimed(self):
        git(self.repo, "init", "-b", "main")
        with self.assertRaisesRegex(RuntimeError, "not initialized by Git Publisher"):
            self.plugin.handle("init-local-repository", {"repo_path": "Case-Trajectories"}, self.context)

    def test_failed_creation_auth_can_retry_without_second_login(self):
        self.init()
        def auth_denied(root, *args, token, timeout=300, check=True):
            if args == ("api", "user"):
                return subprocess.CompletedProcess(["gh"], 0, b'{"login":"MoonTzai"}', b"")
            if args == ("api", f"repos/MoonTzai/{self.public_name}"):
                return subprocess.CompletedProcess(["gh"], 1, b"", b"gh: Not Found (HTTP 404)")
            return subprocess.CompletedProcess(["gh"], 1, b"", b"gh: forbidden (HTTP 403)")
        with mock.patch.object(self.plugin, "_github_token_from_gcm", return_value="local-gcm-credential"), \
             mock.patch.object(self.plugin, "_run_gh", side_effect=auth_denied):
            with self.assertRaisesRegex(RuntimeError, "repository-creation access"):
                self.plugin.handle(
                    "create-github-repository",
                    {"repo_path": "Case-Trajectories", "owner": "MoonTzai", "name": self.public_name,
                     "visibility": "public", "description": "Case Trajectories"},
                    self.context,
                )
        self.assertEqual(self.plugin._read_first_publication_state(self.repo)["phase"], "remote_request_pending")
        self.assertTrue(self.create()["created"])

    def test_creation_success_but_local_ack_missing_is_fail_closed(self):
        self.init()
        self.remote_created = True
        state = self.plugin._read_first_publication_state(self.repo)
        self.plugin._write_first_publication_state(self.repo, {
            **state, "phase": "remote_request_pending", "owner": "MoonTzai",
            "name": self.public_name, "visibility": "public", "description": "Case Trajectories",
        })
        with self.assertRaisesRegex(RuntimeError, "ambiguous|already exists"):
            self.create()

    def test_visibility_owner_and_path_safety(self):
        self.init()
        for visibility in ("", "internal", None):
            with self.assertRaisesRegex(RuntimeError, "visibility"):
                self.plugin._create_github_repository(self.repo, "MoonTzai", self.public_name, visibility, "")
        for owner in ("other@domain", "../owner", ""):
            with self.assertRaisesRegex(RuntimeError, "owner"):
                self.plugin._create_github_repository(self.repo, owner, self.public_name, "public", "")
        for bad_path in ("../escape", "/outside", "Case-Trajectories\\evil"):
            with self.assertRaises(RuntimeError):
                self.plugin.handle("init-local-repository", {"repo_path": bad_path}, self.context)

    def test_initial_commit_refuses_secret_path_and_prior_staging(self):
        self.init()
        self.create()
        (self.repo / ".env").write_text("KEY=secret", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.plugin.handle(
                "initial-commit", {"repo_path": "Case-Trajectories", "paths": [".env"], "message": "No"},
                self.context,
            )
        (self.repo / "other.txt").write_text("existing stage", encoding="utf-8")
        git(self.repo, "add", "--", "other.txt")
        with self.assertRaisesRegex(RuntimeError, "unrelated staged"):
            self.plugin.handle(
                "initial-commit", {"repo_path": "Case-Trajectories", "paths": ["other.txt"], "message": "No"},
                self.context,
            )

    def test_initial_commit_rejects_crlf_normalization(self):
        self.init()
        self.create()
        git(self.repo, "config", "core.autocrlf", "true")
        (self.repo / "index.html").write_bytes(b"<h1>Case</h1>\r\n")
        with self.assertRaisesRegex(RuntimeError, "normalized or transformed bytes"):
            self.plugin.handle(
                "initial-commit", {"repo_path": "Case-Trajectories", "paths": ["index.html"], "message": "First"},
                self.context,
            )
        self.assertEqual(git(self.repo, "ls-files"), "")
        (self.repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
        result = self.plugin.handle(
            "initial-commit", {"repo_path": "Case-Trajectories", "paths": [".gitattributes", "index.html"],
                               "message": "First"},
            self.context,
        )
        self.assertEqual(len(result["commit"]), 40)
