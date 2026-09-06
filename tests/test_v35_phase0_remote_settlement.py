from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _write_extension(root: Path, *, schema_version: int, effect: str = "external_effect") -> Path:
    extension = root / "settlement-fixture"
    extension.mkdir()
    (extension / "plugin.py").write_text(
        "def run(action, params, context):\n    return {'ok': True, 'executed': True}\n",
        encoding="utf-8",
    )
    action = {
        "read_only": True,
        "requires_workspace": False,
        "authorization": "none",
        "run_mode": "foreground",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    manifest = {
        "schema_version": schema_version,
        "id": "settlement-fixture",
        "name": "Settlement Fixture",
        "version": "1.0.0",
        "entrypoint": "plugin.py",
        "permissions": [],
        "workspace_adapter": {"mode": "none", "state": "none"},
        "actions": {"run": action},
    }
    if schema_version == 2:
        manifest["runtime_abi"] = 2
        action["effect_contract"] = {
            "effect_semantics": effect,
            "lifetime": "foreground",
        }
        if effect == "external_effect":
            action["recovery_contract"] = {
                "correlation": "host_operation_id",
                "recovery_mode_control": False,
            }
    (extension / "folderbridge-extension.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return extension


class V35RemoteSettlementCapabilityAcceptanceTests(unittest.TestCase):
    def test_transport_capability_requires_truthful_bounded_proof(self) -> None:
        from folderbridge_mcp.recovery_capability import TransportSettlementCapability

        none = TransportSettlementCapability.unproven()
        self.assertFalse(none.proven_bounded)

        ack = TransportSettlementCapability.structured_delivery_ack(
            correlation_proven=True,
            loss_reordering_proven=True,
        )
        self.assertTrue(ack.proven_bounded)

        identity = TransportSettlementCapability.retry_identity(
            identity_namespace="control-plane-request-v1",
            retry_horizon_seconds=300,
            retry_stable=True,
            non_reuse_proven=True,
        )
        self.assertTrue(identity.proven_bounded)

        with self.assertRaises(ValueError):
            TransportSettlementCapability.retry_identity(
                identity_namespace="control-plane-request-v1",
                retry_horizon_seconds=300,
                retry_stable=True,
                non_reuse_proven=False,
            )

        quarantine = TransportSettlementCapability.bounded_failure_quarantine(
            quarantine_horizon_seconds=30,
            failure_signal_proven=True,
        )
        self.assertTrue(quarantine.proven_bounded)

    def test_schema_v1_hot_loaded_action_is_blocked_before_worker_under_persistent_tunnel(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_extension(bundled, schema_version=1)

            runtime = ToolRuntime.from_roots(
                [workspace],
                recovery_context=RecoveryAdmissionContext.persistent_tunnel_unproven(),
            )
            runtime.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                with mock.patch.object(
                    runtime.extensions,
                    "execute_prepared",
                    side_effect=AssertionError("worker must not run"),
                ):
                    response = runtime.call(
                        "extension",
                        {
                            "action": "run",
                            "extension_id": "settlement-fixture",
                            "extension_action": "run",
                            "params": {},
                        },
                    )
                self.assertTrue(response["isError"])
                error = response["structuredContent"]["error"]
                self.assertEqual(error["code"], "RECOVERY_CONTRACT_UNAVAILABLE")
                self.assertEqual(
                    error["details"]["reason"],
                    "effect_contract_untrusted",
                )

                # Host-native discovery remains available while execution is blocked.
                listed = runtime.call("extension", {"action": "list"})
                self.assertFalse(listed["isError"])
            finally:
                runtime.close()

    def test_schema_v2_external_effect_needs_both_owner_recovery_and_transport_settlement(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.recovery_capability import (
            RecoveryAdmissionContext,
            TransportSettlementCapability,
        )
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_extension(bundled, schema_version=2)
            operation_registry = OperationRegistry(root / "state")

            blocked = ToolRuntime.from_roots(
                [workspace],
                recovery_context=RecoveryAdmissionContext(
                    transport_mode="persistent_tunnel",
                    settlement=TransportSettlementCapability.unproven(),
                ),
                operation_registry=operation_registry,
                boot_id="1" * 32,
            )
            blocked.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                response = blocked.call(
                    "extension",
                    {
                        "action": "run",
                        "extension_id": "settlement-fixture",
                        "extension_action": "run",
                        "params": {},
                    },
                )
                self.assertTrue(response["isError"])
                self.assertEqual(
                    response["structuredContent"]["error"]["code"],
                    "RECOVERY_CONTRACT_UNAVAILABLE",
                )
                self.assertEqual(
                    response["structuredContent"]["error"]["details"]["reason"],
                    "transport_settlement_unproven",
                )
            finally:
                blocked.close()

            allowed = ToolRuntime.from_roots(
                [workspace],
                recovery_context=RecoveryAdmissionContext(
                    transport_mode="persistent_tunnel",
                    settlement=TransportSettlementCapability.structured_delivery_ack(
                        correlation_proven=True,
                        loss_reordering_proven=True,
                    ),
                ),
                operation_registry=operation_registry,
                boot_id="2" * 32,
            )
            allowed.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                with mock.patch.object(
                    allowed.extensions,
                    "execute_prepared",
                    return_value={"executed": True},
                ) as execute:
                    response = allowed.call(
                        "extension",
                        {
                            "action": "run",
                            "extension_id": "settlement-fixture",
                            "extension_action": "run",
                            "params": {},
                        },
                    )
                self.assertFalse(response["isError"])
                execute.assert_called_once()
            finally:
                allowed.close()

    def test_settlement_proof_does_not_unlock_external_effect_without_durable_effect_boundary(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import (
            RecoveryAdmissionContext,
            TransportSettlementCapability,
        )
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_extension(bundled, schema_version=2)

            runtime = ToolRuntime.from_roots(
                [workspace],
                recovery_context=RecoveryAdmissionContext(
                    transport_mode="persistent_tunnel",
                    settlement=TransportSettlementCapability.structured_delivery_ack(
                        correlation_proven=True,
                        loss_reordering_proven=True,
                    ),
                    durable_effect_boundary_available=True,
                    journal_capacity_available=True,
                    pin_capacity_available=True,
                    key_capacity_available=True,
                ),
            )
            try:
                # Manual capability flags cannot self-attest a durable boundary;
                # only an injected authoritative OperationRegistry may do so.
                self.assertFalse(
                    runtime.recovery_admission.durable_effect_boundary_available
                )
                runtime.extensions = ExtensionRegistry(
                    user_root=user,
                    bundled_root=bundled,
                )
                response = runtime.call(
                    "extension",
                    {
                        "action": "run",
                        "extension_id": "settlement-fixture",
                        "extension_action": "run",
                        "params": {},
                    },
                )
                self.assertTrue(response["isError"])
                error = response["structuredContent"]["error"]
                self.assertEqual(error["code"], "RECOVERY_CONTRACT_UNAVAILABLE")
                self.assertEqual(
                    error["details"]["reason"],
                    "durable_effect_boundary_unavailable",
                )
            finally:
                runtime.close()

    def test_read_only_trusted_effect_does_not_require_remote_settlement(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_extension(bundled, schema_version=2, effect="read_only")

            runtime = ToolRuntime.from_roots(
                [workspace],
                recovery_context=RecoveryAdmissionContext.persistent_tunnel_unproven(),
            )
            runtime.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                with mock.patch.object(
                    runtime.extensions,
                    "execute_prepared",
                    return_value={"executed": True},
                ) as execute:
                    response = runtime.call(
                        "extension",
                        {
                            "action": "run",
                            "extension_id": "settlement-fixture",
                            "extension_action": "run",
                            "params": {},
                        },
                    )
                self.assertFalse(response["isError"])
                execute.assert_called_once()
            finally:
                runtime.close()

    def test_ordinary_task_and_capability_are_fail_closed_for_persistent_tunnel(self) -> None:
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext
        from folderbridge_mcp.tools import ToolRuntime

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime = ToolRuntime.from_roots(
                [workspace],
                allow_tasks=True,
                capabilities=("test",),
                recovery_context=RecoveryAdmissionContext.persistent_tunnel_unproven(),
            )
            try:
                with mock.patch.object(
                    runtime.task_jobs,
                    "run_or_promote",
                    side_effect=AssertionError("task worker must not run"),
                ):
                    task = runtime.call(
                        "run_task",
                        {"action": "run", "name": "anything"},
                    )
                # Config validation may fail first when no approved task exists; the
                # admission helper itself is locked separately below.
                decision = runtime.recovery_admission.assess_unclassified_owner(
                    owner="task:anything",
                    lifetime="job_owned",
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "effect_contract_untrusted")

                capability_decision = runtime.recovery_admission.assess_unclassified_owner(
                    owner="capability:test",
                    lifetime="job_owned",
                )
                self.assertFalse(capability_decision.allowed)
                self.assertEqual(
                    capability_decision.reason,
                    "effect_contract_untrusted",
                )
                self.assertTrue(task["isError"])
            finally:
                runtime.close()

    def test_direct_stdio_context_does_not_invent_remote_delivery_requirement(self) -> None:
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext

        context = RecoveryAdmissionContext.direct_stdio()
        decision = context.assess_unclassified_owner(
            owner="extension:legacy",
            lifetime="foreground",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "direct_stdio_no_remote_settlement_contract")


class V35RemoteSettlementMatrixAcceptanceTests(unittest.TestCase):
    def test_matrix_is_exact_owner_hash_action_inventory_and_unproven_cutover_fails_closed(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import (
            RecoveryAdmissionContext,
            build_extension_settlement_matrix,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "bundled"
            user = root / "user"
            bundled.mkdir()
            user.mkdir()
            extension_path = _write_extension(bundled, schema_version=1)

            registry = ExtensionRegistry(user_root=user, bundled_root=bundled)
            rows = build_extension_settlement_matrix(
                registry,
                RecoveryAdmissionContext.persistent_tunnel_unproven(),
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["owner"], "extension:settlement-fixture")
            self.assertEqual(row["action"], "run")
            self.assertEqual(row["owner_contract_digest"], registry.get("settlement-fixture").sha256)
            self.assertEqual(row["effect_semantics"], "unclassified_unsafe")
            self.assertFalse(row["effect_contract_trusted"])
            self.assertFalse(row["persistent_tunnel_allowed"])
            self.assertEqual(row["blocking_reason"], "effect_contract_untrusted")
            self.assertTrue(extension_path.is_dir())


if __name__ == "__main__":
    unittest.main()
