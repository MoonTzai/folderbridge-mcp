from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path


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


class V35DualEndedTunnelHealthReplayAcceptanceTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        (workspace / "note.txt").write_text("health-replay", encoding="utf-8")
        return workspace

    def test_verified_repeated_502_with_healthy_runtime_retires_only_tunnel_generation(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost
        from folderbridge_mcp.runtime_transport import TunnelHealthClassifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = RuntimeHost(
                [self._workspace(root)],
                state_root=root / "state",
                boot_id="6" * 32,
                containment_identity="health-replay:healthy-runtime",
            )
            host.start()
            old_token = "a" * 64
            host.register_generation(old_token, generation=1)
            runtime_identity = id(host.runtime)
            boot_id = host.boot_id
            classifier = TunnelHealthClassifier(failure_threshold=3, window_seconds=10.0)
            observed = {
                "status_code": 502,
                "failure_source": "client_internal",
                "upstream_response_received": False,
            }
            try:
                self.assertEqual(
                    classifier.observe(
                        observed,
                        runtime_healthy=True,
                        diagnostic_contract_verified=True,
                        now=1.0,
                    ),
                    "transient",
                )
                self.assertEqual(
                    classifier.observe(
                        observed,
                        runtime_healthy=True,
                        diagnostic_contract_verified=True,
                        now=2.0,
                    ),
                    "transient",
                )
                self.assertEqual(
                    classifier.observe(
                        observed,
                        runtime_healthy=True,
                        diagnostic_contract_verified=True,
                        now=3.0,
                    ),
                    "upstream_path_dead",
                )

                retired = host.admission.retire_generation(old_token)
                self.assertEqual(retired["state"], "retired")
                new_token = "b" * 64
                host.register_generation(new_token, generation=2)

                status, payload = _post(
                    host.port,
                    old_token,
                    {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                )
                self.assertEqual(status, 401)
                status, payload = _post(
                    host.port,
                    new_token,
                    {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"], {})
                self.assertEqual(id(host.runtime), runtime_identity)
                self.assertEqual(host.boot_id, boot_id)
                rows = host.registry.schema_liveness_snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["boot_id"], boot_id)
            finally:
                host.close()

    def test_verified_502_with_unhealthy_runtime_enters_runtime_loss_recovery_on_replacement(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost
        from folderbridge_mcp.runtime_transport import TunnelHealthClassifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            workspace = self._workspace(root)
            first = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="7" * 32,
                containment_identity="health-replay:failed-runtime",
            )
            first.start()
            old_token = "c" * 64
            first.register_generation(old_token, generation=1)
            receipt = first.registry.register_prepared(
                boot_id=first.boot_id,
                owner="extension:test",
                public_workspace_id="public-a",
                workspace_recovery_key="wk-a",
                effect_semantics="external_effect",
                lifetime="job_owned",
                owner_contract_digest="c" * 64,
                key_version="owner-key-v1",
            )
            first.registry.transition(
                receipt.operation_id,
                workspace_recovery_key="wk-a",
                expected_state="prepared",
                new_state="effect_attempted",
            )
            classifier = TunnelHealthClassifier(failure_threshold=3, window_seconds=10.0)
            observed = {
                "status_code": 502,
                "failure_source": "client_internal",
                "upstream_response_received": False,
            }
            self.assertEqual(
                classifier.observe(
                    observed,
                    runtime_healthy=False,
                    diagnostic_contract_verified=True,
                    now=1.0,
                ),
                "runtime_loss",
            )
            first.admission.retire_generation(old_token)
            first.close()

            replacement = RuntimeHost(
                [workspace],
                state_root=state,
                boot_id="8" * 32,
                containment_identity="health-replay:replacement-runtime",
            )
            replacement.start()
            recovery_token = "d" * 64
            replacement.register_generation(recovery_token, generation=1)
            try:
                current = replacement.registry.get(
                    receipt.operation_id,
                    workspace_recovery_key="wk-a",
                )
                self.assertEqual(current.state, "runtime_lost")
                self.assertTrue(replacement.recovery_required)
                self.assertEqual(
                    replacement.admission.snapshot(recovery_token)["state"],
                    "control_only",
                )
                status, payload = _post(
                    replacement.port,
                    recovery_token,
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
                replacement.close()

    def test_invalid_api_key_is_auth_terminal_without_runtime_or_job_reset(self) -> None:
        from folderbridge_mcp.runtime_host import RuntimeHost
        from folderbridge_mcp.runtime_transport import TunnelHealthClassifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = RuntimeHost(
                [self._workspace(root)],
                state_root=root / "state",
                boot_id="9" * 32,
                containment_identity="health-replay:auth-terminal",
            )
            host.start()
            token = "e" * 64
            host.register_generation(token, generation=1)
            classifier = TunnelHealthClassifier()
            runtime_identity = id(host.runtime)
            try:
                self.assertEqual(
                    classifier.observe(
                        {"status_code": 401, "error_code": "invalid_api_key"},
                        runtime_healthy=True,
                        diagnostic_contract_verified=True,
                        credential_snapshot="credential-v1",
                        now=1.0,
                    ),
                    "auth_terminal",
                )
                self.assertTrue(classifier.auth_terminal("credential-v1"))
                self.assertFalse(classifier.auth_terminal("credential-v2"))
                self.assertEqual(id(host.runtime), runtime_identity)
                self.assertEqual(host.admission.snapshot(token)["state"], "active")

                status, payload = _post(
                    host.port,
                    token,
                    {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"], {})
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
