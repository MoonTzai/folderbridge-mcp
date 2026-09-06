from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


class V35PersistentCorrelationKeyAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=16, max_bytes=256 * 1024)

    def test_active_key_is_persistent_versioned_and_recoverable_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._registry(root)
            version, secret = first.active_correlation_key()

            self.assertRegex(version, r"^ck1:[0-9a-f]{32}$")
            self.assertEqual(len(secret), 32)
            self.assertEqual(first.correlation_key(version), secret)

            reopened = self._registry(root)
            reopened_version, reopened_secret = reopened.active_correlation_key()
            self.assertEqual(reopened_version, version)
            self.assertEqual(reopened_secret, secret)
            self.assertEqual(reopened.correlation_key(version), secret)

    def test_key_store_never_persists_plain_secret_and_uses_platform_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, secret = registry.active_correlation_key()

            state_path = root / "operation-correlation-keys-v1.json"
            raw = state_path.read_bytes()
            self.assertNotIn(secret, raw)

            state = json.loads(raw)
            self.assertEqual(state["active_version"], version)
            record = state["keys"][version]
            if os.name == "nt":
                self.assertNotIn(base64.b64encode(secret), raw)
                self.assertEqual(record["protection"], "windows-dpapi-current-user")
            else:
                self.assertEqual(record["protection"], "posix-user-private")
                self.assertEqual(base64.b64decode(record["material"]), secret)
                mode = stat.S_IMODE(state_path.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_corrupt_key_store_fails_closed_without_minting_replacement(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, _ = registry.active_correlation_key()
            state_path = root / "operation-correlation-keys-v1.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["checksum"] = "0" * 64
            state_path.write_text(json.dumps(state), encoding="utf-8")

            reopened = self._registry(root)
            with self.assertRaises(CorrelationKeyUnavailable):
                reopened.active_correlation_key()
            with self.assertRaises(CorrelationKeyUnavailable):
                reopened.correlation_key(version)

            after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(after["active_version"], version)

    def test_unknown_key_version_fails_closed(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            registry.active_correlation_key()
            with self.assertRaises(CorrelationKeyUnavailable):
                registry.correlation_key("ck1:" + "f" * 32)

    def test_rotation_creates_new_active_key_and_preserves_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            old_version, old_secret = registry.active_correlation_key()

            new_version, new_secret = registry.rotate_correlation_key()
            self.assertNotEqual(new_version, old_version)
            self.assertNotEqual(new_secret, old_secret)
            self.assertEqual(registry.correlation_key(old_version), old_secret)
            self.assertEqual(registry.correlation_key(new_version), new_secret)

            reopened = self._registry(root)
            reopened_version, reopened_secret = reopened.active_correlation_key()
            self.assertEqual(reopened_version, new_version)
            self.assertEqual(reopened_secret, new_secret)
            self.assertEqual(reopened.correlation_key(old_version), old_secret)

    def test_rotation_fails_closed_when_existing_store_is_not_recoverable(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            old_version, _ = registry.active_correlation_key()
            state_path = root / "operation-correlation-keys-v1.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["checksum"] = "f" * 64
            state_path.write_text(json.dumps(state), encoding="utf-8")

            reopened = self._registry(root)
            with self.assertRaises(CorrelationKeyUnavailable):
                reopened.rotate_correlation_key()

            after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(after["active_version"], old_version)


if __name__ == "__main__":
    unittest.main()
