from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path


def _post(port: int, token: str, request: dict) -> tuple[int, dict | None]:
    body = json.dumps(request).encode("utf-8")
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


class V35RuntimeHostAcceptanceTests(unittest.TestCase):
    def _workspace(self, parent: Path) -> Path:
        workspace = parent / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("hello", encoding="utf-8")
        return workspace

    def test_runtime_host_registers_schema_liveness_and_releases_it_on_close(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="1" * 32,
                containment_identity="acceptance-runtime:one",
            )
            try:
                rows = host.registry.schema_liveness_snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["boot_id"], "1" * 32)
                self.assertEqual(rows[0]["process_role"], "runtime")
                self.assertEqual(rows[0]["containment_identity"], "acceptance-runtime:one")
                self.assertEqual(rows[0]["readable_schema_min"], 1)
                self.assertEqual(rows[0]["readable_schema_max"], 6)
                self.assertEqual(rows[0]["writable_schema_min"], 6)
                self.assertEqual(rows[0]["writable_schema_max"], 6)
                self.assertFalse(host.recovery_required)
            finally:
                host.close()

            from folderbridge_mcp.operation_registry import OperationRegistry

            reopened = OperationRegistry(state, max_records=4096)
            self.assertEqual(reopened.schema_liveness_snapshot(), [])

    def test_previous_boot_unresolved_receipt_forces_control_only_generation(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            registry = OperationRegistry(state)
            receipt = registry.register_prepared(
                boot_id="old-boot",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="a" * 64,
                key_version="owner-key-v1",
            )
            registry.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="2" * 32,
                containment_identity="acceptance-runtime:two",
            )
            host.start()
            token = "t" * 64
            host.register_generation(token, generation=1)
            try:
                current = host.registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                )
                self.assertEqual(current.state, "runtime_lost")
                self.assertTrue(host.recovery_required)
                self.assertEqual(host.admission.snapshot(token)["state"], "control_only")

                status, payload = _post(
                    host.port,
                    token,
                    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"], {})

                status, payload = _post(
                    host.port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "server_info", "arguments": {}},
                    },
                )
                self.assertEqual(status, 200)
                self.assertIn("result", payload)

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
                self.assertEqual(status, 503)
                self.assertIn("recovery-control", payload["error"])
            finally:
                host.close()

    def test_empty_registry_generation_can_activate_data_dispatch(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="3" * 32,
                containment_identity="acceptance-runtime:three",
            )
            host.start()
            token = "u" * 64
            host.register_generation(token, generation=1)
            try:
                self.assertEqual(host.admission.snapshot(token)["state"], "active")
                status, payload = _post(
                    host.port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
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

    def test_recovery_required_rejects_explicit_active_generation(self) -> None:
        from folderbridge_mcp.operation_registry import OperationRegistry
        from folderbridge_mcp.runtime_host import RuntimeHost, RuntimeRecoveryBlocked

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            registry = OperationRegistry(state)
            registry.register_prepared(
                boot_id="old-boot",
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="unclassified_unsafe",
                lifetime="foreground",
                owner_contract_digest="b" * 64,
                key_version="owner-key-v1",
            )

            host = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="4" * 32,
                containment_identity="acceptance-runtime:four",
            )
            try:
                with self.assertRaises(RuntimeRecoveryBlocked):
                    host.register_generation("v" * 64, generation=1, state="active")
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
