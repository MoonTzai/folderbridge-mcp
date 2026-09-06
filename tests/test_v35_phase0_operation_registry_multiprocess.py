from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path


def _register_worker(root: str, role: str, count: int, start, results) -> None:
    try:
        from folderbridge_mcp.operation_registry import OperationRegistry

        registry = OperationRegistry(Path(root), max_records=256, max_bytes=2 * 1024 * 1024)
        start.wait(10)
        ids: list[str] = []
        for index in range(count):
            receipt = registry.register_prepared(
                boot_id=f"{role}-boot",
                owner=f"{role}:{index}",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="a" * 64,
                key_version="k1",
            )
            ids.append(receipt.operation_id)
        results.put(("ok", role, ids))
    except BaseException as exc:
        results.put(("error", role, f"{type(exc).__name__}: {exc}"))


def _retry_reservation_worker(root: str, role: str, start, results) -> None:
    try:
        from folderbridge_mcp.operation_registry import (
            OperationRegistry,
            ProvenRetryIdentity,
            RetryIdentityReserved,
        )

        registry = OperationRegistry(Path(root), max_records=256, max_bytes=2 * 1024 * 1024)
        identity = ProvenRetryIdentity(
            authority_profile="multiprocess-acceptance-v1",
            identity_namespace="accepted-tunnel-request-v1",
            identity=b"same-cross-process-request",
            retry_horizon_seconds=3600,
        )
        start.wait(10)
        try:
            receipt = registry.register_prepared(
                boot_id=f"{role}-boot",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="c" * 64,
                key_version="owner-key-v1",
                proven_retry_identity=identity,
            )
            results.put(("ok", role, receipt.operation_id))
        except RetryIdentityReserved as exc:
            results.put(("reserved", role, exc.operation_id))
    except BaseException as exc:
        results.put(("error", role, f"{type(exc).__name__}: {exc}"))


def _transition_worker(root: str, operation_ids: list[str], start, results) -> None:
    try:
        from folderbridge_mcp.operation_registry import OperationRegistry

        registry = OperationRegistry(Path(root), max_records=256, max_bytes=2 * 1024 * 1024)
        start.wait(10)
        for operation_id in operation_ids:
            registry.transition(
                operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )
        results.put(("ok", len(operation_ids)))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _mixed_registry_worker(root: str, role: str, start, results) -> None:
    lease = None
    try:
        from folderbridge_mcp.operation_registry import OperationRegistry

        registry = OperationRegistry(Path(root), max_records=256, max_bytes=4 * 1024 * 1024)
        lease = registry.register_schema_liveness(
            boot_id=f"{role}-boot",
            binary_version="0.8.23-test",
            process_role="stdio-supervisor" if role == "stdio-supervisor" else "runtime",
            containment_identity=f"acceptance-containment:{role}",
            readable_schema_min=1,
            readable_schema_max=6,
            writable_schema_min=1,
            writable_schema_max=6,
        )
        start.wait(10)
        for index in range(4):
            receipt = registry.register_prepared(
                boot_id=f"{role}-boot",
                owner=f"{role}:settled:{index}",
                public_workspace_id=f"public-{role}",
                workspace_recovery_key=f"wk-{role}",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest=(str(index + 1) * 64)[:64],
                key_version="owner-key-v1",
                owner_snapshot=f"snapshot:{role}:{index}".encode("utf-8"),
            )
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key=f"wk-{role}",
                expected_state="prepared",
                new_state="effect_attempted",
            )
            registry.settle_effect_outcome(
                receipt.operation_id,
                workspace_recovery_key=f"wk-{role}",
                outcome="effect_absent",
            )
            registry.compact_reconciled_receipts(updated_before=10**12)
            registry.garbage_collect_owner_snapshots()
            if role == "runtime-a":
                registry.rotate_correlation_key()
                registry.garbage_collect_correlation_keys()

        protected = registry.register_prepared(
            boot_id=f"{role}-boot",
            owner=f"{role}:protected",
            public_workspace_id=f"public-{role}",
            workspace_recovery_key=f"wk-{role}",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="f" * 64,
            key_version="owner-key-v1",
            owner_snapshot=f"protected-snapshot:{role}".encode("utf-8"),
        )
        results.put(("ok", role, protected.operation_id))
    except BaseException as exc:
        results.put(("error", role, f"{type(exc).__name__}: {exc}"))
    finally:
        if lease is not None:
            try:
                lease.close()
            except BaseException:
                pass


def _crash_writer_before_registry_replace(root: str, operation_id: str) -> None:
    import folderbridge_mcp.operation_registry as registry_module
    from folderbridge_mcp.operation_registry import OperationRegistry

    registry = OperationRegistry(Path(root), max_records=64, max_bytes=512 * 1024)
    real_replace = registry_module.os.replace

    def crash_before_publish(source, target):
        if Path(target) == registry.path:
            os._exit(73)
        return real_replace(source, target)

    registry_module.os.replace = crash_before_publish
    registry.transition(
        operation_id,
        workspace_recovery_key="wk-a",
        expected_state="prepared",
        new_state="effect_attempted",
    )
    os._exit(74)


class V35OperationRegistryMultiprocessAcceptanceTests(unittest.TestCase):
    def test_three_independent_processes_do_not_lose_concurrent_registrations(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            roles = ("runtime-a", "runtime-b", "stdio-supervisor")
            count = 12
            processes = [
                context.Process(
                    target=_register_worker,
                    args=(str(root), role, count, start, results),
                )
                for role in roles
            ]
            for process in processes:
                process.start()
            start.set()

            reports = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(report[0] == "ok" for report in reports), reports)

            expected_ids = {
                operation_id
                for report in reports
                for operation_id in report[2]
            }
            self.assertEqual(len(expected_ids), len(roles) * count)

            reopened = OperationRegistry(root, max_records=256, max_bytes=2 * 1024 * 1024)
            actual = reopened.list_all_for_operator()
            self.assertEqual({item.operation_id for item in actual}, expected_ids)
            self.assertEqual(len(actual), len(roles) * count)

    def test_same_proven_retry_identity_is_reserved_across_independent_processes(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            roles = ("runtime-a", "stdio-supervisor")
            processes = [
                context.Process(
                    target=_retry_reservation_worker,
                    args=(str(root), role, start, results),
                )
                for role in roles
            ]
            for process in processes:
                process.start()
            start.set()

            reports = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            self.assertEqual(
                sorted(report[0] for report in reports),
                ["ok", "reserved"],
                reports,
            )
            winner = next(report for report in reports if report[0] == "ok")
            reserved = next(report for report in reports if report[0] == "reserved")
            self.assertEqual(reserved[2], winner[2])

            reopened = OperationRegistry(root, max_records=256, max_bytes=2 * 1024 * 1024)
            actual = reopened.list_for_workspace("wk-a")
            self.assertEqual([item.operation_id for item in actual], [winner[2]])

    def test_independent_process_transitions_merge_instead_of_overwriting_each_other(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root, max_records=256, max_bytes=2 * 1024 * 1024)
            operation_ids = [
                registry.register_prepared(
                    boot_id="boot-a",
                    owner=f"owner:{index}",
                    public_workspace_id="public-a",
                    workspace_recovery_key="wk-a",
                    effect_semantics="external_effect",
                    lifetime="job_owned",
                    owner_contract_digest="b" * 64,
                    key_version="k1",
                ).operation_id
                for index in range(12)
            ]

            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            halves = (operation_ids[:6], operation_ids[6:])
            processes = [
                context.Process(
                    target=_transition_worker,
                    args=(str(root), ids, start, results),
                )
                for ids in halves
            ]
            for process in processes:
                process.start()
            start.set()

            reports = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(report[0] == "ok" for report in reports), reports)

            reopened = OperationRegistry(root, max_records=256, max_bytes=2 * 1024 * 1024)
            states = {
                item.operation_id: item.state
                for item in reopened.list_for_workspace("wk-a")
            }
            self.assertEqual(set(states), set(operation_ids))
            self.assertTrue(all(states[operation_id] == "effect_attempted" for operation_id in operation_ids))

    def test_three_roles_mix_registration_transition_compaction_pin_gc_and_key_rotation(self) -> None:
        import json
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root, max_records=256, max_bytes=4 * 1024 * 1024)
            registry.active_correlation_key()

            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            roles = ("runtime-a", "runtime-b", "stdio-supervisor")
            processes = [
                context.Process(
                    target=_mixed_registry_worker,
                    args=(str(root), role, start, results),
                )
                for role in roles
            ]
            for process in processes:
                process.start()
            start.set()

            reports = [results.get(timeout=45) for _ in processes]
            for process in processes:
                process.join(timeout=45)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(report[0] == "ok" for report in reports), reports)

            reopened = OperationRegistry(root, max_records=256, max_bytes=4 * 1024 * 1024)
            protected_ids = {report[2] for report in reports}
            actual = reopened.list_all_for_operator()
            self.assertEqual({item.operation_id for item in actual}, protected_ids)
            self.assertTrue(all(item.state == "prepared" for item in actual))
            pin_stats = reopened.owner_snapshot_stats()
            self.assertEqual(pin_stats["references"], len(roles))
            self.assertEqual(pin_stats["distinct_versions"], len(roles))
            self.assertEqual(reopened.schema_liveness_snapshot(), [])

            reopened.garbage_collect_correlation_keys()
            key_state = json.loads(
                (root / "operation-correlation-keys-v1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(key_state["keys"]), 1)
            self.assertIn(key_state["active_version"], key_state["keys"])

    def test_writer_death_before_atomic_replace_preserves_last_committed_receipt(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)
            receipt = registry.register_prepared(
                boot_id="boot-a",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="a" * 64,
                key_version="owner-key-v1",
            )

            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_crash_writer_before_registry_replace,
                args=(str(root), receipt.operation_id),
            )
            process.start()
            process.join(timeout=30)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 73)

            reopened = OperationRegistry(root, max_records=64, max_bytes=512 * 1024)
            current = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(current.state, "prepared")
            self.assertEqual(
                [item.operation_id for item in reopened.list_all_for_operator()],
                [receipt.operation_id],
            )

            updated = reopened.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )
            self.assertEqual(updated.state, "effect_attempted")


if __name__ == "__main__":
    unittest.main()
