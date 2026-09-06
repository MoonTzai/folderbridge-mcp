from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class V35CrashRestartEffectBoundaryAcceptanceTests(unittest.TestCase):
    def _register(self, registry, *, boot_id: str = "boot-old"):
        return registry.register_prepared(
            boot_id=boot_id,
            owner="extension:test",
            public_workspace_id="public-a",
            workspace_recovery_key="wk-a",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="a" * 64,
            key_version="owner-key-v1",
        )

    def test_prepared_previous_boot_with_proven_quiescence_terminalizes_effect_not_started(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root)
            receipt = self._register(registry)

            reopened = OperationRegistry(root)
            changed = reopened.recover_for_boot(
                "boot-new",
                quiescent_boot_ids={"boot-old"},
            )

            self.assertEqual(changed, 1)
            recovered = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")
            self.assertEqual(recovered.state, "effect_not_started")
            self.assertEqual(recovered.terminal_reason, "effect_not_started")
            self.assertFalse(recovered.protected)

    def test_effect_attempted_never_downgrades_even_when_old_containment_is_quiescent(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = OperationRegistry(root)
            receipt = self._register(registry)
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            reopened = OperationRegistry(root)
            reopened.recover_for_boot(
                "boot-new",
                quiescent_boot_ids={"boot-old"},
            )
            recovered = reopened.get(receipt.operation_id, workspace_recovery_key="wk-a")

            self.assertEqual(recovered.state, "runtime_lost")
            self.assertEqual(recovered.terminal_reason, "runtime_lost")
            self.assertNotEqual(recovered.state, "effect_not_started")
            self.assertTrue(recovered.protected)

    def test_runtime_host_can_consume_exact_quiescent_boot_evidence_before_recovery_fence(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            registry = OperationRegistry(state)
            receipt = self._register(registry)

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="2" * 32,
                containment_identity="acceptance-runtime:new",
                quiescent_previous_boot_ids={"boot-old"},
            )
            try:
                recovered = host.registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                )
                self.assertEqual(recovered.state, "effect_not_started")
                self.assertFalse(host.recovery_required)
            finally:
                host.close()

    def test_effect_not_started_releases_recovery_resources_and_can_compact(self) -> None:
        from folderbridge_mcp.operation_registry import (
            OperationNotFound,
            OperationRegistry,
            ProvenRetryIdentity,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = ProvenRetryIdentity(
                authority_profile="crash-boundary-acceptance-v1",
                identity_namespace="accepted-request-v1",
                identity=b"pre-effect-request",
                retry_horizon_seconds=3600,
            )
            registry = OperationRegistry(root)
            receipt = registry.register_prepared(
                boot_id="boot-old",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="c" * 64,
                key_version="owner-key-v1",
                owner_snapshot=b"owner-snapshot",
                proven_retry_identity=identity,
            )

            registry.recover_for_boot(
                "boot-new",
                quiescent_boot_ids={"boot-old"},
            )
            self.assertEqual(registry.owner_snapshot_stats()["references"], 0)
            self.assertEqual(
                registry.get(receipt.operation_id, workspace_recovery_key="wk-a").state,
                "effect_not_started",
            )

            removed = registry.compact_reconciled_receipts(updated_before=10**12)
            self.assertEqual(removed, 1)
            with self.assertRaises(OperationNotFound):
                registry.get(receipt.operation_id, workspace_recovery_key="wk-a")

            second = registry.register_prepared(
                boot_id="boot-new",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="c" * 64,
                key_version="owner-key-v1",
                proven_retry_identity=identity,
            )
            self.assertNotEqual(second.operation_id, receipt.operation_id)


class V35RecoveryControlContractAcceptanceTests(unittest.TestCase):
    def test_recovery_control_requires_read_only_or_host_stable_token_guard(self) -> None:
        from folderbridge_mcp.runtime_transport import is_recovery_control_request

        self.assertTrue(
            is_recovery_control_request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "write_file",
                        "arguments": {"action": "status", "transaction_id": "tx-1"},
                    },
                }
            )
        )
        self.assertTrue(
            is_recovery_control_request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "write_file",
                        "arguments": {"action": "abort", "transaction_id": "tx-1"},
                    },
                }
            )
        )
        self.assertFalse(
            is_recovery_control_request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "write_file",
                        "arguments": {"action": "abort"},
                    },
                }
            )
        )
        self.assertTrue(
            is_recovery_control_request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "run_task",
                        "arguments": {"action": "cancel", "job_id": "job-1"},
                    },
                }
            )
        )
        self.assertTrue(
            is_recovery_control_request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "extension",
                        "arguments": {"action": "job_cancel", "job_id": "job-1"},
                    },
                }
            )
        )

        # Owner-specific actions do not become recovery-safe merely because
        # their names look like cancel/stop/abort/reconcile controls.
        for action_name in ("cancel", "stop", "abort", "reconcile"):
            with self.subTest(action_name=action_name):
                self.assertFalse(
                    is_recovery_control_request(
                        {
                            "method": "tools/call",
                            "params": {
                                "name": "extension",
                                "arguments": {
                                    "action": "run",
                                    "extension_id": "example",
                                    "extension_action": action_name,
                                    "params": {},
                                },
                            },
                        }
                    )
                )


class V35DirectStdioRecoveryParityAcceptanceTests(unittest.TestCase):
    def test_stdio_supervisor_uses_shared_registry_schema_liveness_and_recovery_gate(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import StdioSupervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            registry = OperationRegistry(state)
            receipt = registry.register_prepared(
                boot_id="boot-old",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="b" * 64,
                key_version="owner-key-v1",
            )
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            supervisor = StdioSupervisor(
                [workspace],
                state_root=state,
                boot_id="3" * 32,
                containment_identity="acceptance-stdio:one",
            )
            try:
                rows = supervisor.registry.schema_liveness_snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["process_role"], "stdio-supervisor")
                self.assertEqual(rows[0]["boot_id"], "3" * 32)
                self.assertTrue(supervisor.recovery_required)

                blocked_path = workspace / "blocked.txt"
                response = supervisor.mcp.dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "edit_file",
                            "arguments": {
                                "path": "blocked.txt",
                                "create_content": "must-not-run",
                            },
                        },
                    }
                )
                self.assertIn("error", response)
                self.assertEqual(response["error"]["code"], -32024)
                self.assertFalse(blocked_path.exists())

                status = supervisor.mcp.dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "server_info", "arguments": {}},
                    }
                )
                self.assertIn("result", status)
            finally:
                supervisor.close()

            self.assertEqual(OperationRegistry(state).schema_liveness_snapshot(), [])

    def test_cli_serve_keeps_candidate_stdio_supervisor_out_of_production_until_gate3(self) -> None:
        from folderbridge_mcp import cli

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            runtime = mock.MagicMock()
            server = mock.MagicMock()
            with mock.patch.object(
                cli.ToolRuntime,
                "from_roots",
                return_value=runtime,
            ) as runtime_constructor, mock.patch.object(
                cli,
                "McpServer",
                return_value=server,
            ) as server_constructor:
                result = cli.main(["serve", "--workspace", str(workspace)])

            self.assertEqual(result, 0)
            runtime_constructor.assert_called_once_with(
                (workspace.resolve(),),
                read_only=False,
                allow_tasks=False,
                capabilities=[],
            )
            server_constructor.assert_called_once_with(runtime)
            server.serve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
