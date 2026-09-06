from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _post(port: int, token: str, request: dict) -> tuple[int, dict | None]:
    body = json.dumps(request, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(
        "POST",
        "/mcp",
        body=body,
        headers={
            "Host": f"127.0.0.1:{port}",
            "Content-Type": "application/json",
            "X-FolderBridge-Generation-Token": token,
            "Connection": "close",
        },
    )
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    return status, json.loads(raw.decode("utf-8")) if raw else None


class V35FinalPhase0CrashCutoverAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _workspace(root: Path) -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("gate4", encoding="utf-8")
        return workspace

    @staticmethod
    def _register(registry, *, boot_id: str):
        return registry.register_prepared(
            boot_id=boot_id,
            owner="extension:gate4-fixture",
            public_workspace_id="public-a",
            workspace_recovery_key="wk-a",
            effect_semantics="external_effect",
            lifetime="job_owned",
            owner_contract_digest="f" * 64,
            key_version="owner-key-v1",
        )

    def test_crash_before_barrier_with_quiescence_recovers_active_data_plane(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            old = OperationRegistry(state)
            receipt = self._register(old, boot_id="old-quiescent")

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="a" * 32,
                containment_identity="gate4:before-barrier",
                quiescent_previous_boot_ids={"old-quiescent"},
            )
            host.start()
            token = "q" * 64
            host.register_generation(token, generation=1)
            try:
                recovered = host.registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                )
                self.assertEqual(recovered.state, "effect_not_started")
                self.assertFalse(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "active")

                status, payload = _post(
                    host.port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "workspace",
                            "arguments": {"action": "list", "path": "."},
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload["result"]["structuredContent"]["ok"])
            finally:
                host.close()

    def test_crash_after_barrier_reconcile_reactivates_same_control_only_generation(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            old = OperationRegistry(state)
            receipt = self._register(old, boot_id="old-attempted")
            old.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="b" * 32,
                containment_identity="gate4:after-barrier",
            )
            host.start()
            token = "r" * 64
            host.register_generation(token, generation=1)
            try:
                recovered = host.registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                )
                self.assertEqual(recovered.state, "runtime_lost")
                self.assertTrue(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "control_only")

                status, payload = _post(
                    host.port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "workspace",
                            "arguments": {"action": "list", "path": "."},
                        },
                    },
                )
                self.assertEqual(status, 503)
                self.assertIn("recovery-control", payload["error"])

                settled = host.registry.settle_effect_outcome(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                    outcome="effect_absent",
                )
                self.assertEqual(settled.state, "reconciled")

                host.activate_generation_after_recovery(token)
                self.assertFalse(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "active")

                status, payload = _post(
                    host.port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "workspace",
                            "arguments": {"action": "list", "path": "."},
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload["result"]["structuredContent"]["ok"])
            finally:
                host.close()

    def test_unresolved_receipt_cannot_be_promoted_out_of_control_only(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost, RuntimeRecoveryBlocked

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            old = OperationRegistry(state)
            receipt = self._register(old, boot_id="old-still-blocked")
            old.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="c" * 32,
                containment_identity="gate4:still-blocked",
            )
            token = "s" * 64
            host.register_generation(token, generation=1)
            try:
                self.assertEqual(host.admission.snapshot(token)["state"], "control_only")
                with self.assertRaises(RuntimeRecoveryBlocked):
                    host.activate_generation_after_recovery(token)
                self.assertTrue(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "control_only")
            finally:
                host.close()

    def test_activation_rechecks_and_rolls_back_if_blocker_appears(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost, RuntimeRecoveryBlocked

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = RuntimeHost(
                [self._workspace(root)],
                state_root=root / "state",
                boot_id="d" * 32,
                containment_identity="gate4:activation-race",
            )
            token = "t" * 64
            host.register_generation(token, generation=1, state="control_only")
            try:
                with mock.patch.object(
                    host,
                    "_compute_recovery_required",
                    side_effect=[False, True],
                ):
                    with self.assertRaises(RuntimeRecoveryBlocked):
                        host.activate_generation_after_recovery(token)

                self.assertTrue(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "control_only")
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
