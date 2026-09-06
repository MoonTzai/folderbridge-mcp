from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("file_ops_toolkit_under_test", ROOT / "plugin.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class _Context(dict):
    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root=str(workspace_root), workspace_read_only=False)


class FileOpsToolkitSelfTests(unittest.TestCase):
    def test_manifest_actions_permissions_and_scopes(self) -> None:
        manifest = json.loads((ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["actions"]), {"copy-file", "move-file"})
        self.assertEqual(set(manifest["permissions"]), {"workspace.read", "workspace.write"})
        for action in manifest["actions"].values():
            self.assertEqual(action["effect_contract"]["effect_semantics"], "guarded_replay_safe")
            self.assertEqual(
                [(claim["param"], claim["kind"]) for claim in action["mutation_scope"]["claims"]],
                [("source_path", "exact"), ("destination_path", "exact")],
            )

    def test_copy_and_move_regular_binary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = b"\x00\x01binary\xff" * 1000
            digest = hashlib.sha256(body).hexdigest()
            (root / "source.bin").write_bytes(body)
            copied = plugin.handle(
                "copy-file",
                {
                    "source_path": "source.bin",
                    "destination_path": "copy.bin",
                    "expected_source_sha256": digest,
                },
                _Context(root),
            )
            self.assertEqual(copied["sha256"], digest)
            moved = plugin.handle(
                "move-file",
                {
                    "source_path": "copy.bin",
                    "destination_path": "moved.bin",
                    "expected_source_sha256": digest,
                },
                _Context(root),
            )
            self.assertEqual(moved["sha256"], digest)
            self.assertFalse((root / "copy.bin").exists())
            self.assertEqual((root / "moved.bin").read_bytes(), body)

    def test_paths_fail_closed(self) -> None:
        for value in ("../x", "/x", ".git/config", "node_modules/x", ".env", "x.pem", "x:ads", "CON"):
            with self.subTest(value=value), self.assertRaises(plugin.ExtensionError):
                plugin._clean_relative(value, for_write=True)


if __name__ == "__main__":
    unittest.main()
