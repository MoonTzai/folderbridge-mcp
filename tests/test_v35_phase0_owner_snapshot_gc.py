from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path


class V35OwnerSnapshotReleaseGcAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(
            root,
            max_records=32,
            max_bytes=512 * 1024,
            max_pinned_versions=4,
            max_pinned_bytes=4096,
        )

    def _identity(self, value: bytes):
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        return ProvenRetryIdentity(
            authority_profile="pin-gc-acceptance-v1",
            identity_namespace="accepted-tunnel-request-v1",
            identity=value,
            retry_horizon_seconds=3600,
        )

    def _register(self, registry, *, snapshot: bytes, identity: bytes):
        receipt = registry.register_prepared(
            boot_id="boot-a",
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key="wk-a",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="a" * 64,
            key_version="owner-key-v1",
            owner_snapshot=snapshot,
            proven_retry_identity=self._identity(identity),
        )
        registry.transition(
            receipt.operation_id,
            workspace_recovery_key="wk-a",
            expected_state="prepared",
            new_state="effect_attempted",
        )
        return receipt

    def test_effect_present_releases_pin_then_collects_last_unreferenced_blob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            snapshot = b"exact-owner-snapshot-a"
            digest = hashlib.sha256(snapshot).hexdigest()
            receipt = self._register(
                registry,
                snapshot=snapshot,
                identity=b"request-a",
            )
            blob = root / "operation-owner-snapshots-v1" / f"{digest}.blob"
            self.assertTrue(blob.is_file())

            registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_present",
            )

            stats = registry.owner_snapshot_stats()
            self.assertEqual(stats["references"], 0)
            self.assertEqual(stats["distinct_versions"], 0)
            self.assertFalse(blob.exists())

    def test_shared_snapshot_survives_until_last_reference_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            snapshot = b"shared-owner-snapshot"
            digest = hashlib.sha256(snapshot).hexdigest()
            first = self._register(registry, snapshot=snapshot, identity=b"request-a")
            second = self._register(registry, snapshot=snapshot, identity=b"request-b")
            blob = root / "operation-owner-snapshots-v1" / f"{digest}.blob"

            registry.settle_effect_outcome(
                first.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_absent",
            )
            stats = registry.owner_snapshot_stats()
            self.assertEqual(stats["references"], 1)
            self.assertEqual(stats["reference_counts"], {digest: 1})
            self.assertTrue(blob.is_file())
            self.assertEqual(
                registry.read_owner_snapshot(
                    second.operation_id,
                    workspace_recovery_key="wk-a",
                ),
                snapshot,
            )

            registry.settle_effect_outcome(
                second.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_absent",
            )
            self.assertEqual(registry.owner_snapshot_stats()["references"], 0)
            self.assertFalse(blob.exists())

    def test_indeterminate_outcome_keeps_snapshot_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            snapshot = b"still-required-owner-snapshot"
            receipt = self._register(
                registry,
                snapshot=snapshot,
                identity=b"request-a",
            )

            registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                outcome="indeterminate_keep_blocked",
            )

            self.assertEqual(registry.owner_snapshot_stats()["references"], 1)
            self.assertEqual(
                registry.read_owner_snapshot(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                ),
                snapshot,
            )

    def test_safe_gc_removes_orphans_but_never_referenced_blob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            snapshot = b"referenced-owner-snapshot"
            receipt = self._register(
                registry,
                snapshot=snapshot,
                identity=b"request-a",
            )
            directory_path = root / "operation-owner-snapshots-v1"
            orphan_data = b"crash-left-orphan"
            orphan_digest = hashlib.sha256(orphan_data).hexdigest()
            orphan = directory_path / f"{orphan_digest}.blob"
            orphan.write_bytes(orphan_data)
            temp_orphan = directory_path / ".crash-left.tmp"
            temp_orphan.write_bytes(b"partial")

            removed = registry.garbage_collect_owner_snapshots()
            self.assertGreaterEqual(removed, 2)
            self.assertFalse(orphan.exists())
            self.assertFalse(temp_orphan.exists())
            self.assertEqual(
                registry.read_owner_snapshot(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                ),
                snapshot,
            )


if __name__ == "__main__":
    unittest.main()
