from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _write_v4_registry(root: Path) -> tuple[bytes, str]:
    operation_id = "1" * 32
    receipt = {
        "operation_id": operation_id,
        "boot_id": "old-boot",
        "owner": "extension:test",
        "public_workspace_id": "public-a",
        "workspace_recovery_key": "wk-a",
        "effect_semantics": "external_effect",
        "lifetime": "job_owned",
        "owner_contract_digest": "a" * 64,
        "key_version": "owner-key-v1",
        "state": "prepared",
        "terminal_reason": None,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    unsigned = {
        "schema_version": 4,
        "generation": 7,
        "records": [receipt],
        "owner_snapshot_pins": {},
        "retry_fingerprints": {},
        "retry_authorities": {},
    }
    payload = dict(unsigned)
    payload["checksum"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    data = _canonical_json(payload) + b"\n"
    (root / "operation-registry-v1.json").write_bytes(data)
    return data, operation_id


def _write_v5_registry(root: Path) -> tuple[bytes, str]:
    operation_id = "2" * 32
    receipt = {
        "operation_id": operation_id,
        "boot_id": "v5-boot",
        "owner": "extension:test",
        "public_workspace_id": "public-a",
        "workspace_recovery_key": "wk-a",
        "effect_semantics": "external_effect",
        "lifetime": "job_owned",
        "owner_contract_digest": "b" * 64,
        "key_version": "owner-key-v1",
        "state": "prepared",
        "terminal_reason": None,
        "created_at": 200.0,
        "updated_at": 200.0,
    }
    unsigned = {
        "schema_version": 5,
        "generation": 11,
        "records": [receipt],
        "owner_snapshot_pins": {},
        "retry_fingerprints": {},
        "retry_authorities": {},
        "dedupe_tombstones": {},
    }
    payload = dict(unsigned)
    payload["checksum"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    data = _canonical_json(payload) + b"\n"
    (root / "operation-registry-v1.json").write_bytes(data)
    return data, operation_id


class V35RegistrySchemaMigrationAcceptanceTests(unittest.TestCase):
    def test_v4_to_v6_first_write_keeps_exact_pre_migration_backup(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_bytes, operation_id = _write_v4_registry(root)
            registry = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)

            registry.transition(
                operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            current = json.loads(
                (root / "operation-registry-v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current["schema_version"], 6)
            backup = root / "operation-registry-v1.pre-migration.json"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), old_bytes)

    def test_v5_to_v6_first_write_keeps_exact_pre_migration_backup(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_bytes, operation_id = _write_v5_registry(root)
            registry = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)

            registry.transition(
                operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            current = json.loads(
                (root / "operation-registry-v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current["schema_version"], 6)
            backup = root / "operation-registry-v1.pre-migration.json"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), old_bytes)

    def test_failed_v4_to_v6_publish_leaves_old_authority_and_recoverable_backup(self) -> None:
        import folderbridge_mcp.operation_registry as registry_module
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_bytes, operation_id = _write_v4_registry(root)
            registry = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)
            real_replace = registry_module.os.replace

            def fail_main_publish(source, target):
                if Path(target) == registry.path:
                    raise OSError("injected main publish failure")
                return real_replace(source, target)

            with mock.patch.object(
                registry_module.os,
                "replace",
                side_effect=fail_main_publish,
            ):
                with self.assertRaises(OSError):
                    registry.transition(
                        operation_id,
                        workspace_recovery_key="wk-a",
                        expected_state="prepared",
                        new_state="effect_attempted",
                    )

            self.assertEqual((root / "operation-registry-v1.json").read_bytes(), old_bytes)
            backup = root / "operation-registry-v1.pre-migration.json"
            self.assertEqual(backup.read_bytes(), old_bytes)

            reopened = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)
            current = reopened.get(operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(current.state, "prepared")


if __name__ == "__main__":
    unittest.main()
