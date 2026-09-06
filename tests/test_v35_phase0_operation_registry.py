from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path


class V35OperationRegistryScaffoldAcceptanceTests(unittest.TestCase):
    def _registry(self, root: Path, *, max_records: int = 16, max_bytes: int = 256 * 1024):
        from folderbridge_mcp.operation_registry import OperationRegistry

        return OperationRegistry(root, max_records=max_records, max_bytes=max_bytes)

    def _register(
        self,
        registry,
        *,
        workspace_key: str = "wk-a",
        boot_id: str = "boot-a",
        owner: str = "extension:test",
        owner_snapshot: bytes | None = None,
    ):
        kwargs = {
            "boot_id": boot_id,
            "owner": owner,
            "public_workspace_id": "public-a",
            "workspace_recovery_key": workspace_key,
            "effect_semantics": "external_effect",
            "lifetime": "job_owned",
            "owner_contract_digest": "a" * 64,
            "key_version": "k1",
        }
        if owner_snapshot is not None:
            kwargs["owner_snapshot"] = owner_snapshot
        return registry.register_prepared(**kwargs)

    def test_prepared_registration_is_durable_bounded_and_independent_from_job_cache(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            receipt = self._register(registry)

            self.assertEqual(len(receipt.operation_id), 32)
            self.assertEqual(receipt.state, "prepared")
            self.assertEqual(receipt.workspace_recovery_key, "wk-a")

            # Simulate ordinary in-memory Job cache pruning. The protected
            # receipt authority is the Registry file, not the ephemeral cache.
            ordinary_job_cache = {receipt.operation_id: {"status": "running"}}
            ordinary_job_cache.clear()

            reopened = OperationRegistry(root, max_records=16, max_bytes=256 * 1024)
            found = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(found.operation_id, receipt.operation_id)
            self.assertEqual(found.state, "prepared")

            raw = (root / "operation-registry-v1.json").read_text(encoding="utf-8")
            self.assertNotIn("password", raw.lower())
            self.assertNotIn("params", raw.lower())
            self.assertLessEqual(len(raw.encode("utf-8")), 256 * 1024)

    def test_workspace_visibility_is_scoped_without_cross_workspace_existence_leak(self) -> None:
        from folderbridge_mcp.operation_registry import OperationNotFound

        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            a = self._register(registry, workspace_key="wk-a")
            b = self._register(registry, workspace_key="wk-b", owner="extension:other")

            self.assertEqual(
                [item.operation_id for item in registry.list_for_workspace("wk-a")],
                [a.operation_id],
            )
            self.assertEqual(
                [item.operation_id for item in registry.list_for_workspace("wk-b")],
                [b.operation_id],
            )
            with self.assertRaises(OperationNotFound):
                registry.get(b.operation_id, workspace_recovery_key="wk-a")

    def test_capacity_refuses_new_protected_registration_without_evicting_unresolved(self) -> None:
        from folderbridge_mcp.operation_registry import RegistryCapacityExceeded

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root, max_records=2, max_bytes=64 * 1024)
            first = self._register(registry, workspace_key="wk-a")
            second = self._register(registry, workspace_key="wk-b")
            with self.assertRaises(RegistryCapacityExceeded):
                self._register(registry, workspace_key="wk-c")

            reopened = self._registry(root, max_records=2, max_bytes=64 * 1024)
            self.assertEqual(
                {item.operation_id for item in reopened.list_all_for_operator()},
                {first.operation_id, second.operation_id},
            )

    def test_state_transition_is_compare_and_set_and_previous_boot_nonterminal_becomes_runtime_lost(self) -> None:
        from folderbridge_mcp.operation_registry import InvalidRegistryTransition, OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            receipt = self._register(registry, boot_id="boot-old")
            attempted = registry.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )
            self.assertEqual(attempted.state, "effect_attempted")
            with self.assertRaises(InvalidRegistryTransition):
                registry.transition(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    expected_state="prepared",
                    new_state="complete",
                )

            reopened = OperationRegistry(root, max_records=16, max_bytes=256 * 1024)
            changed = reopened.recover_for_boot("boot-new")
            self.assertEqual(changed, 1)
            recovered = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(recovered.state, "runtime_lost")
            self.assertEqual(recovered.terminal_reason, "runtime_lost")

    def test_corrupt_committed_registry_forces_recovery_only_and_blocks_new_registration(self) -> None:
        from folderbridge_mcp.operation_registry import RegistryRecoveryOnly

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._register(registry)

            state_path = root / "operation-registry-v1.json"
            data = json.loads(state_path.read_text(encoding="utf-8"))
            data["records"][0]["owner"] = "tampered"
            state_path.write_text(json.dumps(data), encoding="utf-8")

            reopened = self._registry(root)
            self.assertTrue(reopened.recovery_only)
            with self.assertRaises(RegistryRecoveryOnly):
                self._register(reopened)

    def test_orphan_atomic_temp_is_never_promoted_over_last_committed_state(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            receipt = self._register(registry)

            (root / ".operation-registry-v1.json.crash.tmp").write_text(
                '{"schema_version":1,"generation":999,"records":[]}',
                encoding="utf-8",
            )
            reopened = OperationRegistry(root, max_records=16, max_bytes=256 * 1024)
            found = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(found.operation_id, receipt.operation_id)
            self.assertFalse(reopened.recovery_only)

    def test_owner_snapshot_is_durable_deduplicated_and_reference_counted(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = b"exact-owner-snapshot-a"
            digest = hashlib.sha256(snapshot).hexdigest()
            registry = OperationRegistry(
                root,
                max_records=16,
                max_bytes=256 * 1024,
                max_pinned_versions=2,
                max_pinned_bytes=1024,
            )
            first = self._register(registry, owner_snapshot=snapshot)
            second = self._register(registry, owner="extension:test-2", owner_snapshot=snapshot)

            stats = registry.owner_snapshot_stats()
            self.assertEqual(stats["distinct_versions"], 1)
            self.assertEqual(stats["references"], 2)
            self.assertEqual(stats["total_bytes"], len(snapshot))
            self.assertEqual(stats["reference_counts"], {digest: 2})

            reopened = OperationRegistry(
                root,
                max_records=16,
                max_bytes=256 * 1024,
                max_pinned_versions=2,
                max_pinned_bytes=1024,
            )
            self.assertEqual(
                reopened.read_owner_snapshot(first.operation_id, workspace_recovery_key="wk-a"),
                snapshot,
            )
            self.assertEqual(
                reopened.read_owner_snapshot(second.operation_id, workspace_recovery_key="wk-a"),
                snapshot,
            )

    def test_pin_version_quota_blocks_new_receipt_without_evicting_unresolved_snapshot(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry, RegistryPinCapacityExceeded

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(
                root,
                max_records=16,
                max_bytes=256 * 1024,
                max_pinned_versions=1,
                max_pinned_bytes=1024,
            )
            first_snapshot = b"exact-owner-snapshot-a"
            first = self._register(registry, owner_snapshot=first_snapshot)
            with self.assertRaises(RegistryPinCapacityExceeded):
                self._register(
                    registry,
                    owner="extension:new-version",
                    owner_snapshot=b"exact-owner-snapshot-b",
                )

            self.assertEqual(len(registry.list_all_for_operator()), 1)
            self.assertEqual(
                registry.read_owner_snapshot(first.operation_id, workspace_recovery_key="wk-a"),
                first_snapshot,
            )
            self.assertEqual(registry.owner_snapshot_stats()["distinct_versions"], 1)

    def test_pin_byte_quota_is_separate_from_registry_journal_quota(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry, RegistryPinCapacityExceeded

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_snapshot = b"a" * 32
            registry = OperationRegistry(
                root,
                max_records=16,
                max_bytes=256 * 1024,
                max_pinned_versions=4,
                max_pinned_bytes=len(first_snapshot),
            )
            first = self._register(registry, owner_snapshot=first_snapshot)
            with self.assertRaises(RegistryPinCapacityExceeded):
                self._register(registry, owner="extension:too-large", owner_snapshot=b"b")
            self.assertEqual(
                registry.read_owner_snapshot(first.operation_id, workspace_recovery_key="wk-a"),
                first_snapshot,
            )


if __name__ == "__main__":
    unittest.main()
