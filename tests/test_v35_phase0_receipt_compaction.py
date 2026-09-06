from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class V35ReceiptCompactionAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path, *, max_records: int = 32):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=max_records, max_bytes=512 * 1024)

    def _identity(self, value: bytes, *, horizon_seconds: int = 3600):
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        return ProvenRetryIdentity(
            authority_profile="compaction-acceptance-v1",
            identity_namespace="accepted-tunnel-request-v1",
            identity=value,
            retry_horizon_seconds=horizon_seconds,
        )

    def _register(self, registry, *, identity=None):
        receipt = registry.register_prepared(
            boot_id="boot-a",
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key="wk-a",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="a" * 64,
            key_version="owner-key-v1",
            proven_retry_identity=identity,
        )
        registry.transition(
            receipt.operation_id,
            workspace_recovery_key="wk-a",
            expected_state="prepared",
            new_state="effect_attempted",
        )
        return receipt

    def test_reconciled_effect_present_receipt_compacts_but_tombstone_still_blocks_retry(self) -> None:
        from folderbridge_mcp.operation_registry import OperationNotFound, RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity(b"request-a")
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1000.0):
                receipt = self._register(registry, identity=identity)
                registry.settle_effect_outcome(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    outcome="effect_present",
                )

            removed = registry.compact_reconciled_receipts(updated_before=1000.0)
            self.assertEqual(removed, 1)
            with self.assertRaises(OperationNotFound):
                registry.get(receipt.operation_id, workspace_recovery_key="wk-a")

            reopened = self._registry(root)
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1001.0):
                with self.assertRaises(RetryIdentityReserved) as caught:
                    reopened.register_prepared(
                        boot_id="boot-b",
                        owner="extension:test",
                        public_workspace_id="public-a",
                        workspace_recovery_key="wk-a",
                        effect_semantics="external_effect",
                        lifetime="job_owned",
                        owner_contract_digest="a" * 64,
                        key_version="owner-key-v1",
                        proven_retry_identity=identity,
                    )
            self.assertEqual(caught.exception.operation_id, receipt.operation_id)

    def test_effect_absent_receipt_can_compact_after_cutoff_and_identity_becomes_new_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity(b"request-a")
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1000.0):
                first = self._register(registry, identity=identity)
                registry.settle_effect_outcome(
                    first.operation_id,
                    workspace_recovery_key="wk-a",
                    outcome="effect_absent",
                )

            self.assertEqual(registry.compact_reconciled_receipts(updated_before=999.0), 0)
            self.assertEqual(registry.compact_reconciled_receipts(updated_before=1000.0), 1)

            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1001.0):
                second = registry.register_prepared(
                    boot_id="boot-b",
                    owner="extension:test",
                    public_workspace_id="public-a",
                    workspace_recovery_key="wk-a",
                    effect_semantics="external_effect",
                    lifetime="job_owned",
                    owner_contract_digest="a" * 64,
                    key_version="owner-key-v1",
                    proven_retry_identity=identity,
                )
            self.assertNotEqual(second.operation_id, first.operation_id)

    def test_compaction_never_evicts_unresolved_receipt_even_with_far_future_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            identity = self._identity(b"request-a")
            receipt = self._register(registry, identity=identity)

            removed = registry.compact_reconciled_receipts(updated_before=10**12)
            self.assertEqual(removed, 0)
            self.assertEqual(
                registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                ).state,
                "effect_attempted",
            )

    def test_compaction_removes_receipt_bound_fingerprint_but_not_tombstone(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            fingerprint_version, _ = registry.active_correlation_key()
            identity = self._identity(b"request-a")
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1000.0):
                receipt = registry.register_prepared(
                    boot_id="boot-a",
                    owner="extension:test",
                    public_workspace_id="public-a",
                    workspace_recovery_key="wk-a",
                    effect_semantics="external_effect",
                    lifetime="job_owned",
                    owner_contract_digest="a" * 64,
                    key_version=fingerprint_version,
                    retry_fingerprint_material=b"candidate-only-material",
                    proven_retry_identity=identity,
                )
                registry.transition(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    expected_state="prepared",
                    new_state="effect_attempted",
                )
                registry.settle_effect_outcome(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    outcome="effect_present",
                )

            registry.compact_reconciled_receipts(updated_before=1000.0)
            state = json.loads((root / "operation-registry-v1.json").read_text(encoding="utf-8"))
            self.assertNotIn(receipt.operation_id, state["retry_fingerprints"])
            self.assertIn(receipt.operation_id, state["dedupe_tombstones"])


if __name__ == "__main__":
    unittest.main()
