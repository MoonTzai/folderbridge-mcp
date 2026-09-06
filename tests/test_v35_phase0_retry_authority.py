from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class V35ProvenRetryIdentityReservationAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=32, max_bytes=256 * 1024)

    def _identity(
        self,
        *,
        namespace: str = "accepted-tunnel-request-v1",
        identity: bytes = b"request-opaque-0001",
        profile: str = "acceptance-fixture-v1",
        horizon_seconds: int = 3600,
    ):
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        return ProvenRetryIdentity(
            authority_profile=profile,
            identity_namespace=namespace,
            identity=identity,
            retry_horizon_seconds=horizon_seconds,
        )

    def _register(
        self,
        registry,
        *,
        retry_identity=None,
        workspace_key: str = "wk-a",
        owner_digest: str = "a" * 64,
    ):
        return registry.register_prepared(
            boot_id="boot-a",
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key=workspace_key,
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest=owner_digest,
            key_version="owner-key-v1",
            proven_retry_identity=retry_identity,
        )

    def test_same_proven_identity_is_reserved_before_second_receipt_exists(self) -> None:
        from folderbridge_mcp.operation_registry import RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            identity = self._identity()
            first = self._register(registry, retry_identity=identity)

            with self.assertRaises(RetryIdentityReserved) as caught:
                self._register(registry, retry_identity=identity)

            self.assertEqual(caught.exception.operation_id, first.operation_id)
            self.assertEqual(
                [item.operation_id for item in registry.list_for_workspace("wk-a")],
                [first.operation_id],
            )

    def test_distinct_identity_namespace_and_unproven_calls_remain_new_intents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            first = self._register(registry, retry_identity=self._identity())
            distinct_identity = self._register(
                registry,
                retry_identity=self._identity(identity=b"request-opaque-0002"),
            )
            distinct_namespace = self._register(
                registry,
                retry_identity=self._identity(namespace="accepted-direct-client-v1"),
            )
            unproven_a = self._register(registry)
            unproven_b = self._register(registry)

            self.assertEqual(
                len(
                    {
                        first.operation_id,
                        distinct_identity.operation_id,
                        distinct_namespace.operation_id,
                        unproven_a.operation_id,
                        unproven_b.operation_id,
                    }
                ),
                5,
            )

    def test_retry_reservation_survives_reopen_and_key_rotation(self) -> None:
        from folderbridge_mcp.operation_registry import RetryIdentityReserved

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity()
            first = self._register(registry, retry_identity=identity)

            registry.rotate_correlation_key()
            reopened = self._registry(root)
            with self.assertRaises(RetryIdentityReserved) as caught:
                self._register(reopened, retry_identity=identity)
            self.assertEqual(caught.exception.operation_id, first.operation_id)

    def test_raw_retry_identity_and_namespace_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            identity = self._identity(
                namespace="private-ish-transport-namespace",
                identity=b"low-entropy-request-identity-42",
            )
            self._register(registry, retry_identity=identity)

            raw = (root / "operation-registry-v1.json").read_bytes()
            self.assertNotIn(b"private-ish-transport-namespace", raw)
            self.assertNotIn(b"low-entropy-request-identity-42", raw)
            self.assertIn(b"acceptance-fixture-v1", raw)

    def test_reservation_is_scoped_by_workspace_and_owner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            identity = self._identity()
            first = self._register(registry, retry_identity=identity)
            other_workspace = self._register(
                registry,
                retry_identity=identity,
                workspace_key="wk-b",
            )
            other_owner = self._register(
                registry,
                retry_identity=identity,
                owner_digest="b" * 64,
            )
            self.assertEqual(
                len({first.operation_id, other_workspace.operation_id, other_owner.operation_id}),
                3,
            )

    def test_retry_horizon_must_be_positive_and_finite(self) -> None:
        from folderbridge_mcp.operation_registry import ProvenRetryIdentity

        with self.assertRaises(ValueError):
            ProvenRetryIdentity(
                authority_profile="acceptance-fixture-v1",
                identity_namespace="accepted-tunnel-request-v1",
                identity=b"request-opaque-0001",
                retry_horizon_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
