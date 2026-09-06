from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class V35AuthoritativeDedupeTombstoneAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=32, max_bytes=256 * 1024)

    def _identity(self, *, horizon_seconds: int = 3600):
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        return ProvenRetryIdentity(
            authority_profile="accepted-retry-proof-v1",
            identity_namespace="accepted-tunnel-request-v1",
            identity=b"request-identity-0001",
            retry_horizon_seconds=horizon_seconds,
        )

    def _register(self, registry, *, identity=None):
        return registry.register_prepared(
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

    def _attempt(self, registry, operation_id: str):
        return registry.transition(
            operation_id,
            workspace_recovery_key="wk-a",
            expected_state="prepared",
            new_state="effect_attempted",
        )

    def test_effect_present_transfers_reservation_to_durable_tombstone(self) -> None:
        from folderbridge_mcp.operation_registry import RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity()
            receipt = self._register(registry, identity=identity)
            self._attempt(registry, receipt.operation_id)

            settled = registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_present",
            )
            self.assertEqual(settled.state, "reconciled")
            self.assertEqual(settled.terminal_reason, "effect_present")

            reopened = self._registry(root)
            with self.assertRaises(RetryIdentityReserved) as caught:
                self._register(reopened, identity=identity)
            self.assertEqual(caught.exception.operation_id, receipt.operation_id)

            raw = (root / "operation-registry-v1.json").read_bytes()
            self.assertNotIn(b"request-identity-0001", raw)
            self.assertNotIn(b"accepted-tunnel-request-v1", raw)
            state = json.loads(raw)
            tombstone = state["dedupe_tombstones"][receipt.operation_id]
            self.assertEqual(tombstone["outcome"], "effect_present")
            self.assertEqual(tombstone["owner_contract_digest"], "a" * 64)
            self.assertEqual(tombstone["workspace_recovery_key"], "wk-a")

    def test_effect_absent_releases_reservation_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity()
            first = self._register(registry, identity=identity)
            self._attempt(registry, first.operation_id)

            settled = registry.settle_effect_outcome(
                first.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_absent",
            )
            self.assertEqual(settled.state, "reconciled")
            self.assertEqual(settled.terminal_reason, "effect_absent")

            second = self._register(registry, identity=identity)
            self.assertNotEqual(second.operation_id, first.operation_id)
            state = json.loads((root / "operation-registry-v1.json").read_text(encoding="utf-8"))
            self.assertNotIn(first.operation_id, state["retry_authorities"])
            self.assertNotIn(first.operation_id, state["dedupe_tombstones"])

    def test_indeterminate_keeps_receipt_and_reservation_blocked(self) -> None:
        from folderbridge_mcp.operation_registry import RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            identity = self._identity()
            receipt = self._register(registry, identity=identity)
            self._attempt(registry, receipt.operation_id)

            unresolved = registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                outcome="indeterminate_keep_blocked",
            )
            self.assertTrue(unresolved.protected)
            self.assertEqual(unresolved.terminal_reason, "indeterminate_keep_blocked")

            with self.assertRaises(RetryIdentityReserved):
                self._register(registry, identity=identity)

    def test_effect_present_without_proven_identity_does_not_invent_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            receipt = self._register(registry)
            self._attempt(registry, receipt.operation_id)

            registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                outcome="effect_present",
            )
            state = json.loads((root / "operation-registry-v1.json").read_text(encoding="utf-8"))
            self.assertNotIn(receipt.operation_id, state["dedupe_tombstones"])

            later = self._register(registry, identity=self._identity())
            self.assertNotEqual(later.operation_id, receipt.operation_id)

    def test_tombstone_survives_rotation_but_expired_horizon_is_not_authority(self) -> None:
        from folderbridge_mcp.operation_registry import RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity(horizon_seconds=10)

            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1000.0):
                receipt = self._register(registry, identity=identity)
                self._attempt(registry, receipt.operation_id)
                registry.settle_effect_outcome(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    outcome="effect_present",
                )

            registry.rotate_correlation_key()
            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1005.0):
                with self.assertRaises(RetryIdentityReserved):
                    self._register(registry, identity=identity)

            with mock.patch("folderbridge_mcp.operation_registry.time.time", return_value=1011.0):
                later = self._register(registry, identity=identity)
            self.assertNotEqual(later.operation_id, receipt.operation_id)


if __name__ == "__main__":
    unittest.main()
