from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.extensions import load_extension


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "file-ops-toolkit"
SPEC = importlib.util.spec_from_file_location("published_file_ops_toolkit", PLUGIN_ROOT / "plugin.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class _Context(dict):
    def __init__(self, workspace_root: Path, *, read_only: bool = False):
        super().__init__(
            workspace_root=str(workspace_root),
            workspace_read_only=read_only,
        )


class FileOpsToolkitTests(unittest.TestCase):
    def test_current_host_parser_accepts_exact_external_manifest(self) -> None:
        record = load_extension(PLUGIN_ROOT, bundled=False)
        self.assertEqual(record.manifest.extension_id, "file-ops-toolkit")
        self.assertEqual(record.manifest.schema_version, 2)
        self.assertEqual(record.manifest.runtime_abi, 1)
        self.assertFalse(record.bundled)
        for name in ("copy-file", "move-file"):
            action = record.manifest.actions[name]
            self.assertEqual(action.mutation_scope.mode, "paths")
            self.assertEqual(
                [(claim.param, claim.kind) for claim in action.mutation_scope.claims],
                [("source_path", "exact"), ("destination_path", "exact")],
            )
            self.assertEqual(action.effect_contract.direct.effect_semantics, "guarded_replay_safe")

    def test_manifest_is_narrow_guarded_replay_safe_surface(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "file-ops-toolkit")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(set(manifest["permissions"]), {"workspace.read", "workspace.write"})
        self.assertEqual(set(manifest["actions"]), {"copy-file", "move-file"})
        for action in manifest["actions"].values():
            self.assertFalse(action["read_only"])
            self.assertTrue(action["requires_workspace"])
            self.assertEqual(action["authorization"], "global")
            self.assertEqual(action["run_mode"], "foreground")
            self.assertEqual(action["timeout_seconds"], 0)
            self.assertEqual(action["effect_contract"]["effect_semantics"], "guarded_replay_safe")
            claims = action["mutation_scope"]["claims"]
            self.assertEqual(
                [(item["param"], item["kind"]) for item in claims],
                [("source_path", "exact"), ("destination_path", "exact")],
            )

    def test_copy_binary_streams_and_returns_exact_hash(self) -> None:
        body = bytes(range(256)) * 8192
        expected = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src.bin").write_bytes(body)
            result = plugin.handle(
                "copy-file",
                {
                    "source_path": "src.bin",
                    "destination_path": "backup/dst.bin",
                    "expected_source_sha256": expected,
                    "create_parents": True,
                },
                _Context(root),
            )
            self.assertEqual((root / "backup" / "dst.bin").read_bytes(), body)
            self.assertEqual(result["sha256"], expected)
            self.assertEqual(result["size"], len(body))
            self.assertFalse(result["already_completed"])

    def test_copy_defaults_to_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(b"source")
            (root / "target.bin").write_bytes(b"target")
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {"source_path": "source.bin", "destination_path": "target.bin"},
                    _Context(root),
                )
            self.assertEqual((root / "target.bin").read_bytes(), b"target")

    def test_copy_overwrite_requires_exact_old_destination_hash(self) -> None:
        old = b"old-target"
        new = b"new-source"
        old_sha = hashlib.sha256(old).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(new)
            (root / "target.bin").write_bytes(old)

            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {
                        "source_path": "source.bin",
                        "destination_path": "target.bin",
                        "overwrite": True,
                    },
                    _Context(root),
                )
            self.assertEqual((root / "target.bin").read_bytes(), old)

            result = plugin.handle(
                "copy-file",
                {
                    "source_path": "source.bin",
                    "destination_path": "target.bin",
                    "overwrite": True,
                    "expected_destination_sha256": old_sha,
                },
                _Context(root),
            )
            self.assertEqual((root / "target.bin").read_bytes(), new)
            self.assertEqual(result["sha256"], hashlib.sha256(new).hexdigest())

    def test_copy_source_hash_mismatch_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(b"actual")
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {
                        "source_path": "source.bin",
                        "destination_path": "target.bin",
                        "expected_source_sha256": "0" * 64,
                    },
                    _Context(root),
                )
            self.assertFalse((root / "target.bin").exists())

    def test_copy_exact_hash_retry_can_report_already_completed(self) -> None:
        body = b"stable-copy"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(body)
            (root / "target.bin").write_bytes(body)
            result = plugin.handle(
                "copy-file",
                {
                    "source_path": "source.bin",
                    "destination_path": "target.bin",
                    "expected_source_sha256": digest,
                },
                _Context(root),
            )
            self.assertTrue(result["already_completed"])
            self.assertEqual(result["sha256"], digest)

    def test_move_no_clobber_moves_regular_file_and_preserves_hash(self) -> None:
        body = b"move-me" * 10000
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(body)
            result = plugin.handle(
                "move-file",
                {
                    "source_path": "source.bin",
                    "destination_path": "nested/target.bin",
                    "expected_source_sha256": digest,
                    "create_parents": True,
                },
                _Context(root),
            )
            self.assertFalse((root / "source.bin").exists())
            self.assertEqual((root / "nested" / "target.bin").read_bytes(), body)
            self.assertEqual(result["sha256"], digest)

    def test_move_overwrite_requires_exact_old_destination_hash(self) -> None:
        old = b"old"
        new = b"new"
        old_sha = hashlib.sha256(old).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(new)
            (root / "target.bin").write_bytes(old)
            result = plugin.handle(
                "move-file",
                {
                    "source_path": "source.bin",
                    "destination_path": "target.bin",
                    "overwrite": True,
                    "expected_destination_sha256": old_sha,
                },
                _Context(root),
            )
            self.assertFalse((root / "source.bin").exists())
            self.assertEqual((root / "target.bin").read_bytes(), new)
            self.assertEqual(result["sha256"], hashlib.sha256(new).hexdigest())

    def test_move_retry_with_expected_source_hash_recognizes_completed_state(self) -> None:
        body = b"already-moved"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target.bin").write_bytes(body)
            result = plugin.handle(
                "move-file",
                {
                    "source_path": "missing-source.bin",
                    "destination_path": "target.bin",
                    "expected_source_sha256": digest,
                },
                _Context(root),
            )
            self.assertTrue(result["already_completed"])
            self.assertEqual(result["sha256"], digest)

    def test_same_path_and_hardlink_alias_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(b"x")
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {"source_path": "source.bin", "destination_path": "source.bin"},
                    _Context(root),
                )
            alias = root / "alias.bin"
            try:
                os.link(source, alias)
            except OSError:
                return
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "move-file",
                    {"source_path": "source.bin", "destination_path": "alias.bin", "overwrite": True, "expected_destination_sha256": hashlib.sha256(b"x").hexdigest()},
                    _Context(root),
                )

    def test_parent_creation_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(b"x")
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {"source_path": "source.bin", "destination_path": "new/target.bin"},
                    _Context(root),
                )
            self.assertFalse((root / "new").exists())

    def test_max_bytes_is_enforced_before_publish_or_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = b"x" * 32
            (root / "source.bin").write_bytes(body)
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {"source_path": "source.bin", "destination_path": "copy.bin", "max_bytes": 8},
                    _Context(root),
                )
            self.assertFalse((root / "copy.bin").exists())
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "move-file",
                    {"source_path": "source.bin", "destination_path": "moved.bin", "max_bytes": 8},
                    _Context(root),
                )
            self.assertTrue((root / "source.bin").exists())
            self.assertFalse((root / "moved.bin").exists())

    def test_workspace_policy_rejects_traversal_sensitive_generated_ads_and_devices(self) -> None:
        for value in (
            "../x.bin",
            "/x.bin",
            r"x\y.bin",
            ".git/config",
            "node_modules/a.bin",
            ".env",
            "secret.pem",
            "file.bin:stream",
            "CON",
            "LPT9.txt",
            "name.",
        ):
            with self.subTest(value=value), self.assertRaises(plugin.ExtensionError):
                plugin._clean_relative(value, for_write=True)

    def test_read_only_context_rejects_both_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.bin").write_bytes(b"x")
            for action in ("copy-file", "move-file"):
                with self.subTest(action=action), self.assertRaises(plugin.ExtensionError):
                    plugin.handle(
                        action,
                        {"source_path": "source.bin", "destination_path": f"{action}.bin"},
                        _Context(root, read_only=True),
                    )

    def test_symlink_or_reparse_component_is_rejected_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "source.bin").write_bytes(b"x")
            link = root / "link"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(plugin.ExtensionError):
                plugin.handle(
                    "copy-file",
                    {"source_path": "source.bin", "destination_path": "link/out.bin"},
                    _Context(root),
                )

    def test_cross_device_move_failure_does_not_delete_source(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows atomic rename branch is tested on the production platform")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(b"x")
            with patch.object(plugin.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device")):
                with self.assertRaises(plugin.ExtensionError):
                    plugin.handle(
                        "move-file",
                        {"source_path": "source.bin", "destination_path": "target.bin"},
                        _Context(root),
                    )
            self.assertTrue(source.exists())
            self.assertFalse((root / "target.bin").exists())


if __name__ == "__main__":
    unittest.main()
