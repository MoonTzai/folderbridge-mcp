from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class V35CorrelationKeyGcAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=32, max_bytes=512 * 1024)

    def _identity(self, value: bytes, *, horizon_seconds: int = 30):
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        return ProvenRetryIdentity(
            authority_profile="key-gc-acceptance-v1",
            identity_namespace="accepted-tunnel-request-v1",
            identity=value,
            retry_horizon_seconds=horizon_seconds,
        )

    def test_old_key_is_retained_while_authoritative_tombstone_references_it(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            first_version, _ = registry.active_correlation_key()
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1000.0):
                receipt = registry.register_prepared(
                    boot_id="boot-a",
                    owner="extension:test",
                    public_workspace_id="public-a",
                    workspace_recovery_key="wk-a",
                    effect_semantics="external_effect",
                    lifetime="job_owned",
                    owner_contract_digest="a" * 64,
                    key_version=first_version,
                    proven_retry_identity=self._identity(b"request-a"),
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
            registry.rotate_correlation_key()
            registry.compact_reconciled_receipts(updated_before=1000.0)

            self.assertEqual(registry.garbage_collect_correlation_keys(), 0)
            self.assertEqual(len(registry.correlation_key(first_version)), 32)

            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1031.0):
                self.assertEqual(registry.expire_dedupe_tombstones(), 1)
                self.assertEqual(registry.garbage_collect_correlation_keys(), 1)
            with self.assertRaises(CorrelationKeyUnavailable):
                registry.correlation_key(first_version)

    def test_old_key_is_retained_while_receipt_fingerprint_still_needs_candidate_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            first_version, _ = registry.active_correlation_key()
            receipt = registry.register_prepared(
                boot_id="boot-a",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="a" * 64,
                key_version=first_version,
                retry_fingerprint_material=b"candidate-only-material",
            )
            registry.rotate_correlation_key()

            self.assertEqual(registry.garbage_collect_correlation_keys(), 0)
            self.assertEqual(
                [item.operation_id for item in registry.retry_fingerprint_candidates(
                    workspace_recovery_key="wk-a",
                    owner_contract_digest="a" * 64,
                    key_version=first_version,
                    fingerprint_material=b"candidate-only-material",
                )],
                [receipt.operation_id],
            )

    def test_unreferenced_nonactive_key_can_be_retired_without_touching_active_key(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            first_version, _ = registry.active_correlation_key()
            active_version, active_secret = registry.rotate_correlation_key()

            self.assertEqual(registry.garbage_collect_correlation_keys(), 1)
            self.assertEqual(registry.correlation_key(active_version), active_secret)
            with self.assertRaises(CorrelationKeyUnavailable):
                registry.correlation_key(first_version)


if __name__ == "__main__":
    unittest.main()
