from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "download-toolkit"
SPEC = importlib.util.spec_from_file_location("published_download_toolkit", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class _Context(dict):
    def __init__(self, workspace_root: Path):
        super().__init__(
            workspace_root=str(workspace_root),
            workspace_read_only=False,
        )


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def getheader(self, name: str, default=None):
        return self._headers.get(name, default)

    def close(self) -> None:
        pass


class DownloadToolkitTests(unittest.TestCase):
    def test_manifest_is_isolated_download_surface(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "download-toolkit")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(set(manifest["actions"]), {"download", "github-snapshot"})
        self.assertEqual(set(manifest["permissions"]), {"workspace.write", "network.outbound:https"})
        for action in manifest["actions"].values():
            self.assertEqual(action["authorization"], "global")
            self.assertEqual(action["run_mode"], "job")
            self.assertEqual(action["timeout_seconds"], 0)
        self.assertEqual(manifest["actions"]["download"]["mutation_scope"]["claims"][0]["kind"], "exact")
        self.assertEqual(manifest["actions"]["github-snapshot"]["mutation_scope"]["claims"][0]["kind"], "tree")

    def test_url_validation_rejects_non_https_userinfo_and_non_public_targets(self) -> None:
        for url in (
            "http://example.com/a.zip",
            "https://user:pass@example.com/a.zip",
            "https://127.0.0.1/a.zip",
            "https://169.254.169.254/latest/meta-data",
            "https://10.1.2.3/a.zip",
            "https://[::1]/a.zip",
        ):
            with self.subTest(url=url), self.assertRaises(Exception):
                plugin._validated_target(url)

        with patch.object(plugin.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            target = plugin._validated_target("https://example.com/path?q=1")
        self.assertEqual(target["host"], "example.com")
        self.assertEqual(target["port"], 443)
        self.assertEqual(target["path"], "/path?q=1")

    def test_public_ip_classifier_rejects_private_and_translation_edge_cases(self) -> None:
        for value in (
            "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1",
            "169.254.1.2", "172.16.0.1", "192.168.1.1",
            "198.18.0.1", "224.0.0.1", "255.255.255.255",
            "::", "::1", "fc00::1", "fe80::1", "ff02::1",
            "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1",
        ):
            with self.subTest(value=value):
                self.assertFalse(plugin._is_public_ip(value))
        for value in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"):
            with self.subTest(value=value):
                self.assertTrue(plugin._is_public_ip(value))

    def test_clash_fake_ip_exception_is_exactly_scoped_to_codeload(self) -> None:
        fake = [(2, 1, 6, "", ("198.18.0.94", 443))]
        with patch.object(plugin.socket, "getaddrinfo", return_value=fake):
            with self.assertRaises(plugin.ExtensionError):
                plugin._validated_target("https://codeload.github.com/owner/repo/zip/main")
            with self.assertRaises(plugin.ExtensionError):
                plugin._validated_target("https://example.com/file", allow_clash_fake_ip=True)
            target = plugin._validated_target(
                "https://codeload.github.com/owner/repo/zip/main",
                allow_clash_fake_ip=True,
            )
        self.assertEqual(target["host"], "codeload.github.com")
        self.assertEqual(target["ip"], "198.18.0.94")

    def test_loopback_system_proxy_parser_supports_clash_formats_only(self) -> None:
        self.assertEqual(plugin._parse_loopback_proxy("127.0.0.1:7897"), ("127.0.0.1", 7897))
        self.assertEqual(
            plugin._parse_loopback_proxy("http=127.0.0.1:7897;https=127.0.0.1:7897;socks=127.0.0.1:7898"),
            ("127.0.0.1", 7897),
        )
        self.assertEqual(plugin._parse_loopback_proxy("https=http://localhost:7897"), ("127.0.0.1", 7897))
        self.assertEqual(plugin._parse_loopback_proxy("[::1]:7897"), ("::1", 7897))
        for value in (
            "10.0.0.1:7897",
            "192.168.1.2:7897",
            "proxy.example.com:7897",
            "https=https://user:pass@127.0.0.1:7897",
            "socks=127.0.0.1:7898",
            "127.0.0.1",
        ):
            with self.subTest(value=value):
                self.assertIsNone(plugin._parse_loopback_proxy(value))

    def test_windows_system_proxy_uses_only_loopback_ie_proxy(self) -> None:
        with patch.object(plugin, "_windows_ie_proxy_string", return_value="https=127.0.0.1:7897"):
            self.assertEqual(plugin._windows_system_proxy(), ("127.0.0.1", 7897))
        with patch.object(plugin, "_windows_ie_proxy_string", return_value="https=10.0.0.2:7897"):
            self.assertIsNone(plugin._windows_system_proxy())

    def test_loopback_proxy_connection_configures_tunnel_before_connect(self) -> None:
        connection = plugin._LoopbackProxyHTTPSConnection(
            "example.com",
            443,
            "127.0.0.1",
            7897,
        )
        self.assertEqual(connection._tunnel_host, "example.com")
        self.assertEqual(connection._tunnel_port, 443)
        self.assertIsNone(connection.sock)

    def test_request_prefers_approved_loopback_system_proxy_over_direct_socket(self) -> None:
        target = {
            "url": "https://example.com/file",
            "host": "example.com",
            "port": 443,
            "path": "/file",
            "ip": "93.184.216.34",
        }
        proxy_connection = object()
        response = _FakeResponse(200, b"ok")
        proxy_instance = unittest.mock.MagicMock()
        proxy_instance.getresponse.return_value = response
        with (
            patch.object(plugin, "_windows_system_proxy", return_value=("127.0.0.1", 7897)),
            patch.object(plugin, "_LoopbackProxyHTTPSConnection", return_value=proxy_instance) as proxied,
            patch.object(plugin, "_PinnedHTTPSConnection") as direct,
        ):
            actual = plugin._request_once(target)
        self.assertIsNotNone(actual)
        proxied.assert_called_once_with("example.com", 443, "127.0.0.1", 7897, timeout=60.0)
        direct.assert_not_called()
        proxy_instance.request.assert_called_once()

    def test_redirect_is_revalidated_before_second_request(self) -> None:
        calls = []
        def fake_validated(url, *, allow_clash_fake_ip=False):
            calls.append(url)
            if "private.invalid" in url:
                raise plugin.ExtensionError("DOWNLOAD_URL_BLOCKED", "blocked")
            return {"url": url, "host": "example.com", "port": 443, "path": "/x", "ip": "93.184.216.34"}

        first = _FakeResponse(302, headers={"Location": "https://private.invalid/secret"})
        with patch.object(plugin, "_validated_target", side_effect=fake_validated), patch.object(plugin, "_request_once", return_value=first):
            with self.assertRaises(plugin.ExtensionError):
                plugin._open_download("https://example.com/start", max_redirects=5)
        self.assertEqual(calls, ["https://example.com/start", "https://private.invalid/secret"])

    def test_download_streams_to_atomic_workspace_file_and_verifies_hash(self) -> None:
        body = (b"abc123" * 10000)
        import hashlib
        expected = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response = _FakeResponse(200, body, {"Content-Length": str(len(body))})
            with patch.object(plugin, "_open_download", return_value=("https://example.com/file.bin", response)):
                result = plugin.handle(
                    "download",
                    {
                        "url": "https://example.com/file.bin",
                        "local_path": "refs/file.bin",
                        "expected_sha256": expected,
                        "max_bytes": len(body) + 1,
                        "overwrite": False,
                    },
                    _Context(root),
                )
            target = root / "refs" / "file.bin"
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(result["sha256"], expected)
            self.assertEqual(result["size"], len(body))
            self.assertEqual(result["workspace_artifacts"][0]["path"], "refs/file.bin")

    def test_download_size_hash_and_no_clobber_fail_closed(self) -> None:
        body = b"x" * 4096
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for params in (
                {"max_bytes": 100, "expected_sha256": None},
                {"max_bytes": 8192, "expected_sha256": "0" * 64},
            ):
                response = _FakeResponse(200, body, {"Content-Length": str(len(body))})
                with patch.object(plugin, "_open_download", return_value=("https://example.com/a.bin", response)):
                    with self.assertRaises(plugin.ExtensionError):
                        plugin.handle(
                            "download",
                            {
                                "url": "https://example.com/a.bin",
                                "local_path": "a.bin",
                                "max_bytes": params["max_bytes"],
                                "expected_sha256": params["expected_sha256"],
                                "overwrite": False,
                            },
                            _Context(root),
                        )
                self.assertFalse((root / "a.bin").exists())

            (root / "a.bin").write_bytes(b"existing")
            response = _FakeResponse(200, body, {"Content-Length": str(len(body))})
            with patch.object(plugin, "_open_download", return_value=("https://example.com/a.bin", response)):
                with self.assertRaises(plugin.ExtensionError):
                    plugin.handle(
                        "download",
                        {"url": "https://example.com/a.bin", "local_path": "a.bin", "max_bytes": 8192, "overwrite": False},
                        _Context(root),
                    )
            self.assertEqual((root / "a.bin").read_bytes(), b"existing")

    def test_overwrite_requires_current_target_sha_and_rechecks_before_publish(self) -> None:
        import hashlib

        old = b"old"
        new = b"new"
        old_sha = hashlib.sha256(old).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a.bin"
            target.write_bytes(old)

            response = _FakeResponse(200, new, {"Content-Length": str(len(new))})
            with patch.object(plugin, "_open_download", return_value=("https://example.com/a.bin?sig=secret", response)):
                result = plugin.handle(
                    "download",
                    {
                        "url": "https://example.com/a.bin?sig=secret",
                        "local_path": "a.bin",
                        "max_bytes": 8192,
                        "overwrite": True,
                        "expected_target_sha256": old_sha,
                    },
                    _Context(root),
                )
            self.assertEqual(target.read_bytes(), new)
            self.assertEqual(result["source_url"], "https://example.com/a.bin")
            self.assertNotIn("secret", json.dumps(result))

            target.write_bytes(old)
            response = _FakeResponse(200, new, {"Content-Length": str(len(new))})
            with patch.object(plugin, "_open_download", return_value=("https://example.com/a.bin", response)):
                with self.assertRaises(plugin.ExtensionError):
                    plugin.handle(
                        "download",
                        {
                            "url": "https://example.com/a.bin",
                            "local_path": "a.bin",
                            "max_bytes": 8192,
                            "overwrite": True,
                        },
                        _Context(root),
                    )
            self.assertEqual(target.read_bytes(), old)

            target.write_bytes(b"changed")
            response = _FakeResponse(200, new, {"Content-Length": str(len(new))})
            with patch.object(plugin, "_open_download", return_value=("https://example.com/a.bin", response)):
                with self.assertRaises(plugin.ExtensionError):
                    plugin.handle(
                        "download",
                        {
                            "url": "https://example.com/a.bin",
                            "local_path": "a.bin",
                            "max_bytes": 8192,
                            "overwrite": True,
                            "expected_target_sha256": old_sha,
                        },
                        _Context(root),
                    )
            self.assertEqual(target.read_bytes(), b"changed")

    def test_github_snapshot_rejects_archive_traversal_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_zip = root / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.txt", "bad")
            with self.assertRaises(plugin.ExtensionError):
                plugin._extract_github_zip(bad_zip, root / "stage", max_expanded_bytes=1024 * 1024)

            symlink_zip = root / "symlink.zip"
            info = zipfile.ZipInfo("repo-main/link")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(symlink_zip, "w") as zf:
                zf.writestr(info, "target")
            with self.assertRaises(plugin.ExtensionError):
                plugin._extract_github_zip(symlink_zip, root / "stage2", max_expanded_bytes=1024 * 1024)

    def test_github_snapshot_extracts_single_root_without_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "repo.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("repo-main/README.md", "hello")
                zf.writestr("repo-main/src/a.py", "print('x')")
            staging = root / "stage"
            stats = plugin._extract_github_zip(archive, staging, max_expanded_bytes=1024 * 1024)
            self.assertEqual((staging / "README.md").read_text(encoding="utf-8"), "hello")
            self.assertTrue((staging / "src" / "a.py").is_file())
            self.assertFalse((staging / ".git").exists())
            self.assertEqual(stats["files"], 2)

    def test_windows_destination_segments_reject_ads_devices_trailing_and_protected_names(self) -> None:
        for value in (
            "safe.txt:stream", "CON", "nul.txt", "COM1.log", "LPT9",
            "folder.", "folder /x.txt", ".folderbridge.json", "secret.pem",
            "build/out.bin", ".git/config", "node_modules/pkg.js",
        ):
            with self.subTest(value=value), self.assertRaises(plugin.ExtensionError):
                plugin._clean_relative(value, allow_directory=True)

    def test_github_archive_preserves_public_source_files_but_rejects_vcs_and_folderbridge_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("repo-main/dist/bundle.js", "ok")
                zf.writestr("repo-main/build/output.txt", "ok")
                zf.writestr("repo-main/node_modules/example/index.js", "ok")
                zf.writestr("repo-main/vendor/lib.txt", "ok")
                zf.writestr("repo-main/.npmrc", "registry=https://registry.npmjs.org")
                zf.writestr("repo-main/.env.example", "TOKEN=example")
                zf.writestr("repo-main/test-fixtures/cert.pem", "fixture")
            staging = root / "stage-source"
            stats = plugin._extract_github_zip(
                archive,
                staging,
                max_expanded_bytes=1024 * 1024,
            )
            self.assertEqual(stats["files"], 7)
            self.assertTrue((staging / "dist" / "bundle.js").is_file())
            self.assertTrue((staging / "build" / "output.txt").is_file())
            self.assertTrue((staging / "node_modules" / "example" / "index.js").is_file())
            self.assertTrue((staging / "vendor" / "lib.txt").is_file())
            self.assertTrue((staging / ".npmrc").is_file())
            self.assertTrue((staging / ".env.example").is_file())
            self.assertTrue((staging / "test-fixtures" / "cert.pem").is_file())

            for index, member in enumerate(("repo-main/.git/config", "repo-main/.folderbridge.json")):
                bad = root / f"blocked-{index}.zip"
                with zipfile.ZipFile(bad, "w") as zf:
                    zf.writestr(member, "bad")
                with self.subTest(member=member), self.assertRaises(plugin.ExtensionError):
                    plugin._extract_github_zip(
                        bad,
                        root / f"stage-blocked-{index}",
                        max_expanded_bytes=1024 * 1024,
                    )

    def test_github_archive_rejects_windows_unsafe_member_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, member in enumerate(("repo-main/CON", "repo-main/a.txt:stream", "repo-main/name. ")):
                archive = root / f"bad-name-{index}.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr(member, "bad")
                with self.subTest(member=member), self.assertRaises(plugin.ExtensionError):
                    plugin._extract_github_zip(
                        archive,
                        root / f"stage-{index}",
                        max_expanded_bytes=1024 * 1024,
                    )

    def test_github_identity_is_strict(self) -> None:
        self.assertEqual(plugin._github_identity("openai", "tunnel-client", "main"), ("openai", "tunnel-client", "main"))
        for values in (
            ("../x", "repo", "main"),
            ("owner", "../repo", "main"),
            ("owner", "repo", "../main"),
            ("owner", "repo", "x y"),
        ):
            with self.subTest(values=values), self.assertRaises(plugin.ExtensionError):
                plugin._github_identity(*values)


if __name__ == "__main__":
    unittest.main()
