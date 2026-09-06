from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class V35RetryFingerprintAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=32, max_bytes=256 * 1024)

    def _register(self, registry, *, version: str, material: bytes, workspace_key: str = "wk-a", digest: str = "a" * 64):
        return registry.register_prepared(
            boot_id="boot-a",
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key=workspace_key,
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest=digest,
            key_version=version,
            retry_fingerprint_material=material,
        )

    def test_keyed_fingerprint_survives_reopen_and_matches_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, _ = registry.active_correlation_key()
            material = b"policy-selected:operation=publish;target=artifact-a"
            receipt = self._register(registry, version=version, material=material)

            reopened = self._registry(root)
            candidates = reopened.retry_fingerprint_candidates(
                workspace_recovery_key="wk-a",
                owner_contract_digest="a" * 64,
                key_version=version,
                fingerprint_material=material,
            )
            self.assertEqual([item.operation_id for item in candidates], [receipt.operation_id])
            self.assertEqual(
                reopened.retry_fingerprint_candidates(
                    workspace_recovery_key="wk-a",
                    owner_contract_digest="a" * 64,
                    key_version=version,
                    fingerprint_material=b"policy-selected:different-intent",
                ),
                [],
            )

    def test_fingerprint_material_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, _ = registry.active_correlation_key()
            material = b"low-entropy-policy-field=yes"
            self._register(registry, version=version, material=material)

            state = (root / "operation-registry-v1.json").read_bytes()
            self.assertNotIn(material, state)
            self.assertNotIn(b"low-entropy-policy-field", state)

    def test_same_fingerprint_is_candidate_only_and_does_not_suppress_distinct_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, _ = registry.active_correlation_key()
            material = b"policy-selected:identical-looking-operation"
            first = self._register(registry, version=version, material=material)
            second = self._register(registry, version=version, material=material)

            self.assertNotEqual(first.operation_id, second.operation_id)
            candidates = registry.retry_fingerprint_candidates(
                workspace_recovery_key="wk-a",
                owner_contract_digest="a" * 64,
                key_version=version,
                fingerprint_material=material,
            )
            self.assertEqual(
                {item.operation_id for item in candidates},
                {first.operation_id, second.operation_id},
            )

    def test_candidate_lookup_is_workspace_and_owner_contract_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            version, _ = registry.active_correlation_key()
            material = b"policy-selected:scoped-operation"
            visible = self._register(registry, version=version, material=material)
            self._register(registry, version=version, material=material, workspace_key="wk-b")
            self._register(registry, version=version, material=material, digest="b" * 64)

            candidates = registry.retry_fingerprint_candidates(
                workspace_recovery_key="wk-a",
                owner_contract_digest="a" * 64,
                key_version=version,
                fingerprint_material=material,
            )
            self.assertEqual([item.operation_id for item in candidates], [visible.operation_id])

    def test_key_loss_fails_closed_instead_of_returning_no_candidate(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            version, _ = registry.active_correlation_key()
            material = b"policy-selected:recover-me"
            self._register(registry, version=version, material=material)

            key_path = root / "operation-correlation-keys-v1.json"
            key_path.write_text("{}", encoding="utf-8")
            reopened = self._registry(root)
            with self.assertRaises(CorrelationKeyUnavailable):
                reopened.retry_fingerprint_candidates(
                    workspace_recovery_key="wk-a",
                    owner_contract_digest="a" * 64,
                    key_version=version,
                    fingerprint_material=material,
                )

    def test_rotation_preserves_old_candidate_but_new_registration_requires_new_active_key(self) -> None:
        from folderbridge_mcp.operation_registry import CorrelationKeyUnavailable

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            old_version, _ = registry.active_correlation_key()
            old_material = b"policy-selected:old-protected-operation"
            old_receipt = self._register(registry, version=old_version, material=old_material)

            new_version, _ = registry.rotate_correlation_key()
            old_candidates = registry.retry_fingerprint_candidates(
                workspace_recovery_key="wk-a",
                owner_contract_digest="a" * 64,
                key_version=old_version,
                fingerprint_material=old_material,
            )
            self.assertEqual([item.operation_id for item in old_candidates], [old_receipt.operation_id])

            with self.assertRaises(CorrelationKeyUnavailable):
                self._register(
                    registry,
                    version=old_version,
                    material=b"policy-selected:new-operation-on-retired-active",
                )

            new_receipt = self._register(
                registry,
                version=new_version,
                material=b"policy-selected:new-operation",
            )
            self.assertNotEqual(new_receipt.operation_id, old_receipt.operation_id)


if __name__ == "__main__":
    unittest.main()
