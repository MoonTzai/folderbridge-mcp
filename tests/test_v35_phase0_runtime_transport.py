from __future__ import annotations

import http.client
import json
import socket
import threading
import unittest


class V35GenerationAdmissionAcceptanceTests(unittest.TestCase):
    def test_retirement_is_atomic_with_ledger_admission_and_terminalizes_queued_no_effect(self) -> None:
        from folderbridge_mcp.runtime_transport import GenerationAdmission, GenerationRetired

        admission = GenerationAdmission(max_accepted=4096)
        token = "a" * 64
        admission.register_generation(token, generation=7, state="active")
        gate = threading.Barrier(17)
        accepted: list[str] = []
        accepted_lock = threading.Lock()

        def racer(worker: int) -> None:
            gate.wait()
            for index in range(128):
                request_id = f"{worker}-{index}"
                try:
                    admission.admit(token, request_id)
                except GenerationRetired:
                    continue
                with accepted_lock:
                    accepted.append(request_id)

        threads = [threading.Thread(target=racer, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        gate.wait()
        retired = admission.retire_generation(token)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        snapshot = admission.snapshot(token)
        self.assertEqual(snapshot["state"], "retired")
        self.assertEqual(snapshot["accepted"], len(accepted))
        self.assertEqual(set(snapshot["terminalized_no_effect"]), set(accepted))
        self.assertEqual(set(retired["terminalized_no_effect"]), set(accepted))
        with self.assertRaises(GenerationRetired):
            admission.admit(token, "after-retirement")
        for request_id in accepted:
            with self.assertRaises(GenerationRetired):
                admission.mark_started(request_id)

    def test_control_only_generation_rejects_data_but_allows_explicit_control(self) -> None:
        from folderbridge_mcp.runtime_transport import GenerationAdmission, GenerationControlOnly

        admission = GenerationAdmission(max_accepted=8)
        token = "b" * 64
        admission.register_generation(token, generation=8, state="control_only")
        with self.assertRaises(GenerationControlOnly):
            admission.admit(token, "data")
        ticket = admission.admit(token, "control", control=True)
        self.assertEqual(ticket.generation, 8)
        admission.complete("control")


class V35PrivateHttpAcceptanceTests(unittest.TestCase):
    def _server(self):
        from folderbridge_mcp.runtime_transport import GenerationAdmission, PrivateMcpHttpServer

        admission = GenerationAdmission(max_accepted=32)
        token = "c" * 64
        admission.register_generation(token, generation=3, state="active")

        def dispatch(request):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"method": request.get("method")},
            }

        server = PrivateMcpHttpServer(dispatch, admission, max_open_connections=8)
        server.start()
        self.addCleanup(server.close)
        return server, admission, token

    def test_authenticated_loopback_post_dispatches_one_mcp_request_and_closes_connection(self) -> None:
        server, _admission, token = self._server()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
        connection.request(
            "POST",
            "/mcp",
            body=body,
            headers={
                "Host": f"127.0.0.1:{server.port}",
                "Content-Type": "application/json",
                "X-FolderBridge-Generation-Token": token,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertEqual(payload["result"]["method"], "ping")
        connection.close()

    def test_private_http_rejects_bad_host_origin_transfer_encoding_and_retired_token(self) -> None:
        server, admission, token = self._server()

        def raw(request: bytes) -> int:
            with socket.create_connection(("127.0.0.1", server.port), timeout=3) as sock:
                sock.sendall(request)
                data = sock.recv(4096)
            status_line = data.split(b"\r\n", 1)[0]
            return int(status_line.split()[1])

        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        common = (
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
        )
        self.assertEqual(
            raw((f"POST /mcp HTTP/1.1\r\nHost: attacker.invalid\r\n{common}\r\n").encode() + body),
            400,
        )
        self.assertEqual(
            raw((f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nOrigin: https://example.invalid\r\n{common}\r\n").encode() + body),
            403,
        )
        duplicate = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            f"Content-Length: {len(body)}\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        self.assertEqual(raw(duplicate), 400)
        transfer = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        ).encode()
        self.assertEqual(raw(transfer), 400)

        admission.retire_generation(token)
        retired = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        self.assertEqual(raw(retired), 401)

    def test_private_http_has_64k_header_and_1m_body_ceiling(self) -> None:
        from folderbridge_mcp.runtime_transport import MAX_HTTP_HEADER_BYTES, MAX_MCP_REQUEST_BYTES

        self.assertEqual(MAX_HTTP_HEADER_BYTES, 64 * 1024)
        self.assertEqual(MAX_MCP_REQUEST_BYTES, 1024 * 1024)


class V35TunnelHealthAcceptanceTests(unittest.TestCase):
    def test_verified_repeated_502_with_healthy_runtime_is_tunnel_only_restart_fixture(self) -> None:
        from folderbridge_mcp.runtime_transport import TunnelHealthClassifier

        classifier = TunnelHealthClassifier(failure_threshold=3, window_seconds=10.0)
        event = {
            "status_code": 502,
            "failure_source": "client_internal",
            "upstream_response_received": False,
        }
        self.assertEqual(classifier.observe(event, runtime_healthy=True, diagnostic_contract_verified=True, now=1.0), "transient")
        self.assertEqual(classifier.observe(event, runtime_healthy=True, diagnostic_contract_verified=True, now=2.0), "transient")
        self.assertEqual(classifier.observe(event, runtime_healthy=True, diagnostic_contract_verified=True, now=3.0), "upstream_path_dead")

        classifier = TunnelHealthClassifier(failure_threshold=3, window_seconds=10.0)
        self.assertEqual(classifier.observe(event, runtime_healthy=False, diagnostic_contract_verified=True, now=1.0), "runtime_loss")
        self.assertEqual(classifier.observe(event, runtime_healthy=True, diagnostic_contract_verified=False, now=2.0), "unknown")

    def test_401_invalid_api_key_is_terminal_for_same_snapshot_until_new_credential(self) -> None:
        from folderbridge_mcp.runtime_transport import TunnelHealthClassifier

        classifier = TunnelHealthClassifier(failure_threshold=3, window_seconds=10.0)
        invalid = {"status_code": 401, "error_code": "invalid_api_key"}
        self.assertEqual(
            classifier.observe(
                invalid,
                runtime_healthy=True,
                diagnostic_contract_verified=True,
                credential_snapshot="cred-v1",
                now=1.0,
            ),
            "auth_terminal",
        )
        self.assertTrue(classifier.auth_terminal("cred-v1"))
        self.assertEqual(
            classifier.observe(
                {"status_code": 502, "failure_source": "client_internal", "upstream_response_received": False},
                runtime_healthy=True,
                diagnostic_contract_verified=True,
                credential_snapshot="cred-v1",
                now=2.0,
            ),
            "auth_terminal",
        )
        self.assertFalse(classifier.auth_terminal("cred-v2"))


if __name__ == "__main__":
    unittest.main()
