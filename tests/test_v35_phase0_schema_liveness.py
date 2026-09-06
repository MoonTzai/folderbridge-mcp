from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path


def _hold_incompatible_schema_lease(root: str, ready, release) -> None:
    from folderbridge_mcp.operation_registry import OperationRegistry

    registry = OperationRegistry(Path(root), max_records=64, max_bytes=512 * 1024)
    lease = registry.register_schema_liveness(
        boot_id="old-runtime-boot",
        binary_version="0.8.22",
        process_role="runtime",
        containment_identity="runtime-job:old-generation",
        readable_schema_min=1,
        readable_schema_max=4,
        writable_schema_min=1,
        writable_schema_max=4,
    )
    ready.set()
    release.wait(30)
    lease.close()


class V35RegistrySchemaLivenessAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=64, max_bytes=512 * 1024)

    def _register_receipt(self, registry):
        return registry.register_prepared(
            boot_id="new-runtime-boot",
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key="wk-a",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="a" * 64,
            key_version="owner-key-v1",
        )

    def test_live_reader_incompatible_with_target_schema_blocks_migration(self) -> None:
        from folderbridge_mcp.operation_registry import RegistrySchemaMigrationBlocked

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            old = registry.register_schema_liveness(
                boot_id="v5-runtime-boot",
                binary_version="0.8.23-v5",
                process_role="runtime",
                containment_identity="runtime-job:v5-generation",
                readable_schema_min=1,
                readable_schema_max=5,
                writable_schema_min=1,
                writable_schema_max=5,
            )
            try:
                with self.assertRaises(RegistrySchemaMigrationBlocked):
                    self._register_receipt(registry)
                self.assertFalse((root / "operation-registry-v1.json").exists())
            finally:
                old.close()

    def test_compatible_live_reader_does_not_block_copy_on_write_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            compatible = registry.register_schema_liveness(
                boot_id="compatible-runtime-boot",
                binary_version="0.8.22-compatible",
                process_role="runtime",
                containment_identity="runtime-job:compatible-generation",
                readable_schema_min=1,
                readable_schema_max=6,
                writable_schema_min=1,
                writable_schema_max=4,
            )
            try:
                receipt = self._register_receipt(registry)
                self.assertEqual(receipt.state, "prepared")
                state = json.loads((root / "operation-registry-v1.json").read_text(encoding="utf-8"))
                self.assertEqual(state["schema_version"], 6)
            finally:
                compatible.close()

    def test_liveness_metadata_is_bounded_identity_not_pid_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            lease = registry.register_schema_liveness(
                boot_id="stdio-boot-a",
                binary_version="0.8.23",
                process_role="stdio-supervisor",
                containment_identity="stdio-job:opaque-generation-a",
                readable_schema_min=1,
                readable_schema_max=6,
                writable_schema_min=1,
                writable_schema_max=6,
            )
            try:
                rows = registry.schema_liveness_snapshot()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["boot_id"], "stdio-boot-a")
                self.assertEqual(row["binary_version"], "0.8.23")
                self.assertEqual(row["process_role"], "stdio-supervisor")
                self.assertEqual(row["containment_identity"], "stdio-job:opaque-generation-a")
                self.assertEqual(row["readable_schema_min"], 1)
                self.assertEqual(row["readable_schema_max"], 6)
                self.assertEqual(row["writable_schema_min"], 1)
                self.assertEqual(row["writable_schema_max"], 6)
                self.assertIsInstance(row["pid"], int)
                self.assertGreater(row["pid"], 0)
                self.assertRegex(row["instance_id"], r"^[0-9a-f]{32}$")
            finally:
                lease.close()
            self.assertEqual(registry.schema_liveness_snapshot(), [])

    def test_dead_process_lease_is_reaped_before_schema_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_incompatible_schema_lease,
                args=(str(root), ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(20))
            process.terminate()
            process.join(timeout=20)
            self.assertFalse(process.is_alive())

            registry = self._registry(root)
            receipt = self._register_receipt(registry)
            self.assertEqual(receipt.state, "prepared")
            self.assertEqual(registry.schema_liveness_snapshot(), [])

    def test_registration_fails_if_existing_registry_schema_is_unreadable(self) -> None:
        from folderbridge_mcp.operation_registry import RegistrySchemaMigrationBlocked

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._register_receipt(registry)
            with self.assertRaises(RegistrySchemaMigrationBlocked):
                registry.register_schema_liveness(
                    boot_id="old-runtime-boot",
                    binary_version="0.8.22",
                    process_role="runtime",
                    containment_identity="runtime-job:old-generation",
                    readable_schema_min=1,
                    readable_schema_max=4,
                    writable_schema_min=1,
                    writable_schema_max=4,
                )


if __name__ == "__main__":
    unittest.main()
