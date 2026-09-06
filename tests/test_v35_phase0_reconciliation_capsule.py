from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


OWNER_DIGEST = "a" * 64
WORKSPACE_KEY = "workspace-recovery-key"
PUBLIC_WORKSPACE_ID = "workspace-public"


def _register(registry, capsule, *, boot_id: str = "b" * 32):
    return registry.register_prepared(
        boot_id=boot_id,
        owner="extension:fixture",
        public_workspace_id=PUBLIC_WORKSPACE_ID,
        workspace_recovery_key=WORKSPACE_KEY,
        effect_semantics="external_effect",
        lifetime="job_owned",
        owner_contract_digest=OWNER_DIGEST,
        key_version="local-test-key",
        owner_snapshot=b"owner-fixture-v1",
        recovery_capsule=capsule,
    )


class V35ReconciliationCapsuleAcceptanceTests(unittest.TestCase):
    def _capsule(self, *, pre_effect: bool = True):
        from folderbridge_mcp.operation_registry import ReconciliationCapsule

        return ReconciliationCapsule.create(
            capsule_type="remote-job-v1",
            pre_effect_correlation=(
                {
                    "request_identity": {
                        "kind": "opaque_id",
                        "value": "client-request-123",
                    }
                }
                if pre_effect
                else {}
            ),
            evidence=(
                {}
                if pre_effect
                else {
                    "remote_job": {
                        "kind": "opaque_id",
                        "value": "server-generated-after-send",
                    }
                }
            ),
        )

    def test_capsule_is_typed_bounded_and_rejects_secret_or_arbitrary_payload(self) -> None:
        from folderbridge_mcp.operation_registry import ReconciliationCapsule

        capsule = self._capsule()
        self.assertLessEqual(len(capsule.canonical_bytes()), 4096)
        self.assertTrue(capsule.has_pre_effect_correlation)

        with self.assertRaises(ValueError):
            ReconciliationCapsule.create(
                capsule_type="bad-secret-v1",
                pre_effect_correlation={
                    "token": {"kind": "secret", "value": "do-not-persist"}
                },
                evidence={},
            )
        with self.assertRaises(ValueError):
            ReconciliationCapsule.create(
                capsule_type="raw-payload-v1",
                pre_effect_correlation={
                    "payload": "arbitrary raw string is not a typed field"
                },
                evidence={},
            )
        with self.assertRaises(ValueError):
            ReconciliationCapsule.create(
                capsule_type="too-large-v1",
                pre_effect_correlation={
                    "request_identity": {
                        "kind": "opaque_id",
                        "value": "x" * 5000,
                    }
                },
                evidence={},
            )

    def test_capsule_survives_reopen_and_is_bound_to_exact_owner_contract(self) -> None:
        from folderbridge_mcp.operation_registry import (
            OperationRegistry,
            RecoveryCapsuleUnavailable,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = OperationRegistry(root)
            receipt = _register(first, self._capsule())
            first.transition(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                expected_state="prepared",
                new_state="effect_attempted",
            )

            reopened = OperationRegistry(root)
            loaded = reopened.read_reconciliation_capsule(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                owner_contract_digest=OWNER_DIGEST,
            )
            self.assertEqual(loaded, self._capsule())

            with self.assertRaises(RecoveryCapsuleUnavailable):
                reopened.read_reconciliation_capsule(
                    receipt.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                    owner_contract_digest="b" * 64,
                )

    def test_post_effect_only_locator_is_not_automatic_reconciliation_authority(self) -> None:
        from folderbridge_mcp.operation_registry import (
            OperationRegistry,
            RecoveryCapsuleUnavailable,
        )

        with tempfile.TemporaryDirectory() as directory:
            registry = OperationRegistry(Path(directory))
            receipt = _register(registry, self._capsule(pre_effect=False))
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                expected_state="prepared",
                new_state="effect_attempted",
            )
            diagnostic = registry.read_reconciliation_capsule(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                owner_contract_digest=OWNER_DIGEST,
            )
            self.assertFalse(diagnostic.has_pre_effect_correlation)
            with self.assertRaises(RecoveryCapsuleUnavailable):
                registry.read_automatic_reconciliation_capsule(
                    receipt.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                    owner_contract_digest=OWNER_DIGEST,
                )

    def test_automatic_reconciliation_requires_pre_effect_seed_and_exact_pinned_owner(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            registry = OperationRegistry(Path(directory))
            receipt = _register(registry, self._capsule())
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                expected_state="prepared",
                new_state="effect_attempted",
            )
            loaded = registry.read_automatic_reconciliation_capsule(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                owner_contract_digest=OWNER_DIGEST,
            )
            self.assertTrue(loaded.has_pre_effect_correlation)
            self.assertEqual(
                registry.read_owner_snapshot(
                    receipt.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                ),
                b"owner-fixture-v1",
            )

    def test_indeterminate_keeps_capsule_but_effect_absent_releases_it(self) -> None:
        from folderbridge_mcp.operation_registry import (
            OperationRegistry,
            RecoveryCapsuleUnavailable,
        )

        with tempfile.TemporaryDirectory() as directory:
            registry = OperationRegistry(Path(directory))
            receipt = _register(registry, self._capsule())
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                expected_state="prepared",
                new_state="effect_attempted",
            )
            blocked = registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                outcome="indeterminate_keep_blocked",
            )
            self.assertTrue(blocked.protected)
            registry.read_reconciliation_capsule(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                owner_contract_digest=OWNER_DIGEST,
            )

            terminal = registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                outcome="effect_absent",
            )
            self.assertFalse(terminal.protected)
            with self.assertRaises(RecoveryCapsuleUnavailable):
                registry.read_reconciliation_capsule(
                    receipt.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                    owner_contract_digest=OWNER_DIGEST,
                )

    def test_prepared_quiescent_recovery_releases_capsule_but_attempted_keeps_it(self) -> None:
        from folderbridge_mcp.operation_registry import (
            OperationRegistry,
            RecoveryCapsuleUnavailable,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root)
            never_started = _register(registry, self._capsule(), boot_id="1" * 32)
            attempted = _register(registry, self._capsule(), boot_id="2" * 32)
            registry.transition(
                attempted.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                expected_state="prepared",
                new_state="effect_attempted",
            )

            reopened = OperationRegistry(root)
            reopened.recover_for_boot(
                "3" * 32,
                quiescent_boot_ids=("1" * 32, "2" * 32),
            )
            self.assertEqual(
                reopened.get(
                    never_started.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                ).state,
                "effect_not_started",
            )
            with self.assertRaises(RecoveryCapsuleUnavailable):
                reopened.read_reconciliation_capsule(
                    never_started.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                    owner_contract_digest=OWNER_DIGEST,
                )

            self.assertEqual(
                reopened.get(
                    attempted.operation_id,
                    workspace_recovery_key=WORKSPACE_KEY,
                ).state,
                "runtime_lost",
            )
            reopened.read_automatic_reconciliation_capsule(
                attempted.operation_id,
                workspace_recovery_key=WORKSPACE_KEY,
                owner_contract_digest=OWNER_DIGEST,
            )

    def test_capsule_is_durable_before_receipt_commit_and_failed_registration_leaves_only_gc_orphan(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            registry = OperationRegistry(Path(directory))
            with mock.patch.object(
                registry,
                "_commit",
                side_effect=RuntimeError("fault after capsule publish"),
            ):
                with self.assertRaisesRegex(RuntimeError, "fault after capsule publish"):
                    _register(registry, self._capsule())

            capsule_dir = registry.reconciliation_capsule_dir
            self.assertTrue(capsule_dir.is_dir())
            self.assertEqual(len(list(capsule_dir.glob("*.json"))), 1)
            self.assertEqual(registry.list_all_for_operator(), [])
            self.assertEqual(registry.garbage_collect_reconciliation_capsules(), 1)
            self.assertEqual(list(capsule_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
