from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _write_external_effect_extension(
    root: Path,
    *,
    schema_version: int = 2,
    runtime_abi: int = 2,
    recovery: bool = True,
) -> Path:
    extension = root / "effect-boundary-fixture"
    extension.mkdir()
    (extension / "plugin.py").write_text(
        "def run(action, params, context):\n"
        "    return {'ok': True, 'operation_id': context.get('operation_id')}\n",
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
        "id": "effect-boundary-fixture",
        "name": "Effect Boundary Fixture",
        "version": "1.0.0",
        "entrypoint": "plugin.py",
        "permissions": [],
        "workspace_adapter": {"mode": "none", "state": "none"},
        "actions": {"run": action},
    }
    if schema_version == 2:
        manifest["runtime_abi"] = runtime_abi
        action["effect_contract"] = {
            "effect_semantics": "external_effect",
            "lifetime": "foreground",
        }
        if recovery:
            action["recovery_contract"] = {
                "correlation": "host_operation_id",
                "recovery_mode_control": False,
            }
    (extension / "folderbridge-extension.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return extension


class V35ToolRuntimeEffectBoundaryAcceptanceTests(unittest.TestCase):
    def test_persistent_external_effect_crosses_durable_barrier_with_one_host_operation_identity(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import TransportSettlementCapability
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_external_effect_extension(bundled)

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="a" * 32,
                containment_identity="acceptance-runtime:effect-boundary",
                settlement_capability=TransportSettlementCapability.structured_delivery_ack(
                    correlation_proven=True,
                    loss_reordering_proven=True,
                ),
            )
            host.runtime.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                self.assertTrue(host.runtime.recovery_admission.durable_effect_boundary_available)
                self.assertTrue(host.runtime.recovery_admission.journal_capacity_available)
                self.assertTrue(host.runtime.recovery_admission.pin_capacity_available)
                self.assertTrue(host.runtime.recovery_admission.key_capacity_available)

                def execute(prepared, *, on_job_finish=None):
                    operation_id = prepared.operation_id
                    self.assertIsInstance(operation_id, str)
                    receipts = host.registry.list_all_for_operator()
                    self.assertEqual(len(receipts), 1)
                    receipt = receipts[0]
                    self.assertEqual(receipt.operation_id, operation_id)
                    self.assertEqual(receipt.state, "effect_attempted")
                    self.assertEqual(receipt.owner, "extension:effect-boundary-fixture/run")
                    self.assertEqual(receipt.owner_contract_digest, prepared.record.sha256)
                    capsule = host.registry.read_automatic_reconciliation_capsule(
                        operation_id,
                        workspace_recovery_key=receipt.workspace_recovery_key,
                        owner_contract_digest=prepared.record.sha256,
                    )
                    payload = capsule.as_payload()
                    self.assertEqual(
                        payload["pre_effect_correlation"]["host_operation_id"],
                        {"kind": "opaque_id", "value": operation_id},
                    )
                    return {"executed": True, "operation_id": operation_id}

                with mock.patch.object(
                    host.runtime.extensions,
                    "execute_prepared",
                    side_effect=execute,
                ) as execute_mock:
                    response = host.runtime.call(
                        "extension",
                        {
                            "action": "run",
                            "extension_id": "effect-boundary-fixture",
                            "extension_action": "run",
                            "params": {},
                        },
                    )
                self.assertFalse(response["isError"])
                execute_mock.assert_called_once()
                receipts = host.registry.list_all_for_operator()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].state, "effect_observed")
                self.assertEqual(
                    response["structuredContent"]["operation_id"],
                    receipts[0].operation_id,
                )
            finally:
                host.close()

    def test_barrier_failure_never_invokes_external_effect(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.recovery_capability import TransportSettlementCapability
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_external_effect_extension(bundled)

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="b" * 32,
                containment_identity="acceptance-runtime:barrier-failure",
                settlement_capability=TransportSettlementCapability.structured_delivery_ack(
                    correlation_proven=True,
                    loss_reordering_proven=True,
                ),
            )
            host.runtime.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                original_transition = host.registry.transition

                def transition(operation_id, **kwargs):
                    if kwargs.get("new_state") == "effect_attempted":
                        raise RuntimeError("injected durable barrier failure")
                    return original_transition(operation_id, **kwargs)

                with mock.patch.object(host.registry, "transition", side_effect=transition):
                    with mock.patch.object(
                        host.runtime.extensions,
                        "execute_prepared",
                        side_effect=AssertionError("effect owner must not run before durable barrier"),
                    ) as execute_mock:
                        response = host.runtime.call(
                            "extension",
                            {
                                "action": "run",
                                "extension_id": "effect-boundary-fixture",
                                "extension_action": "run",
                                "params": {},
                            },
                        )
                self.assertTrue(response["isError"])
                self.assertEqual(
                    response["structuredContent"]["error"]["code"],
                    "INTERNAL_ERROR",
                )
                execute_mock.assert_not_called()
                receipts = host.registry.list_all_for_operator()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].state, "effect_not_started")
            finally:
                host.close()

    def test_direct_stdio_unclassified_extension_also_crosses_registry_barrier(self) -> None:
        from folderbridge_mcp.extensions import ExtensionRegistry
        from folderbridge_mcp.runtime_host import StdioSupervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            bundled = root / "bundled"
            user = root / "user"
            workspace.mkdir()
            bundled.mkdir()
            user.mkdir()
            _write_external_effect_extension(
                bundled,
                schema_version=1,
                runtime_abi=1,
                recovery=False,
            )

            supervisor = StdioSupervisor(
                [workspace],
                state_root=state,
                boot_id="c" * 32,
                containment_identity="acceptance-stdio:effect-boundary",
            )
            supervisor.runtime.extensions = ExtensionRegistry(user_root=user, bundled_root=bundled)
            try:
                def execute(prepared, *, on_job_finish=None):
                    self.assertIsInstance(prepared.operation_id, str)
                    receipts = supervisor.registry.list_all_for_operator()
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0].operation_id, prepared.operation_id)
                    self.assertEqual(receipts[0].state, "effect_attempted")
                    self.assertEqual(
                        receipts[0].owner,
                        "extension:effect-boundary-fixture/run",
                    )
                    return {"executed": True}

                with mock.patch.object(
                    supervisor.runtime.extensions,
                    "execute_prepared",
                    side_effect=execute,
                ):
                    response = supervisor.runtime.call(
                        "extension",
                        {
                            "action": "run",
                            "extension_id": "effect-boundary-fixture",
                            "extension_action": "run",
                            "params": {},
                        },
                    )
                self.assertFalse(response["isError"])
                receipts = supervisor.registry.list_all_for_operator()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].state, "effect_observed")
            finally:
                supervisor.close()

    def test_direct_stdio_named_task_crosses_registry_barrier_before_spawn(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext
        from folderbridge_mcp.tools import ToolRuntime
        import folderbridge_mcp.tools as tools_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".folderbridge.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tasks": {
                            "probe": {
                                "argv": ["python", "scripts/probe.py", "--fixture-marker", "PRIVATE-ARGUMENT-FIXTURE"],
                                "timeout_seconds": 30,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry = OperationRegistry(root / "state")
            runtime = ToolRuntime.from_roots(
                [workspace],
                allow_tasks=True,
                recovery_context=RecoveryAdmissionContext.direct_stdio(),
                operation_registry=registry,
                boot_id="d" * 32,
            )
            try:
                def run_or_promote(_root, _task, **_kwargs):
                    receipts = registry.list_all_for_operator()
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0].owner, "task:probe")
                    self.assertEqual(receipts[0].state, "effect_attempted")
                    return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

                with mock.patch.object(tools_module, "config_is_trusted", return_value=True), mock.patch.object(
                    runtime.task_jobs,
                    "run_or_promote",
                    side_effect=run_or_promote,
                ):
                    response = runtime.call("run_task", {"action": "run", "name": "probe"})
                self.assertFalse(response["isError"])
                receipts = registry.list_all_for_operator()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].state, "effect_observed")
                owner_snapshot = registry.read_owner_snapshot(
                    receipts[0].operation_id,
                    workspace_recovery_key=receipts[0].workspace_recovery_key,
                )
                self.assertNotIn(b"PRIVATE-ARGUMENT-FIXTURE", owner_snapshot)
                self.assertNotIn(b"--fixture-marker", owner_snapshot)
                self.assertIn(b"task_contract_sha256", owner_snapshot)
            finally:
                runtime.close()

    def test_direct_stdio_capability_crosses_registry_barrier_before_provider(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.recovery_capability import RecoveryAdmissionContext
        from folderbridge_mcp.tools import ToolRuntime
        import folderbridge_mcp.tools as tools_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            registry = OperationRegistry(root / "state")
            runtime = ToolRuntime.from_roots(
                [workspace],
                capabilities=("test",),
                recovery_context=RecoveryAdmissionContext.direct_stdio(),
                operation_registry=registry,
                boot_id="e" * 32,
            )
            try:
                def provider(_root, name, *, task_runner=None):
                    self.assertEqual(name, "test")
                    receipts = registry.list_all_for_operator()
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0].owner, "capability:test")
                    self.assertEqual(receipts[0].state, "effect_attempted")
                    return {
                        "capability": "test",
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    }

                with mock.patch.object(tools_module, "run_capability", side_effect=provider):
                    response = runtime.call("run_capability", {"action": "run", "name": "test"})
                self.assertFalse(response["isError"])
                receipts = registry.list_all_for_operator()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].state, "effect_observed")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
