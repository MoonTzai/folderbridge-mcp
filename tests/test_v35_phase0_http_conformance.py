from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path


def _post_json(port: int, token: str, request: dict) -> tuple[int, dict | None]:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
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


def _raw_post(port: int, token: str, body: bytes) -> tuple[int, bytes]:
    request = (
        f"POST /mcp HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"X-FolderBridge-Generation-Token: {token}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    status = int(response.split(b"\r\n", 1)[0].split()[1])
    _, _, payload = response.partition(b"\r\n\r\n")
    return status, payload


class V35PrivateHttpMcpConformanceTests(unittest.TestCase):
    def _host(self):
        from folderbridge_mcp.runtime_host import RuntimeHost

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("hello", encoding="utf-8")
        host = RuntimeHost(
            [workspace],
            state_root=root / "state",
            boot_id="5" * 32,
            containment_identity="conformance-runtime:one",
        )
        host.start()
        token = "w" * 64
        host.register_generation(token, generation=1)
        self.addCleanup(host.close)
        return host, token

    def test_initialize_initialized_tools_list_and_call_match_core_dispatch(self) -> None:
        host, token = self._host()
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "phase0-conformance", "version": "1"},
            },
        }
        direct_initialize = host.mcp.dispatch(initialize)
        status, over_http = _post_json(host.port, token, initialize)
        self.assertEqual(status, 200)
        self.assertEqual(over_http, direct_initialize)
        self.assertEqual(over_http["result"]["protocolVersion"], "2025-06-18")

        status, initialized = _post_json(
            host.port,
            token,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        self.assertEqual(status, 204)
        self.assertIsNone(initialized)

        tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        direct_tools = host.mcp.dispatch(tools_list)
        status, over_http_tools = _post_json(host.port, token, tools_list)
        self.assertEqual(status, 200)
        self.assertEqual(over_http_tools, direct_tools)

        server_info = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "server_info", "arguments": {}},
        }
        direct_info = host.mcp.dispatch(server_info)
        status, over_http_info = _post_json(host.port, token, server_info)
        self.assertEqual(status, 200)
        self.assertEqual(over_http_info, direct_info)

    def test_error_and_supported_protocol_version_mapping_match_core_dispatch(self) -> None:
        host, token = self._host()

        unknown = {"jsonrpc": "2.0", "id": 10, "method": "unknown/method", "params": {}}
        direct_unknown = host.mcp.dispatch(unknown)
        status, over_http_unknown = _post_json(host.port, token, unknown)
        self.assertEqual(status, 200)
        self.assertEqual(over_http_unknown, direct_unknown)
        self.assertEqual(over_http_unknown["error"]["code"], -32601)

        modern_wrong_version = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        direct_wrong = host.mcp.dispatch(modern_wrong_version)
        status, over_http_wrong = _post_json(host.port, token, modern_wrong_version)
        self.assertEqual(status, 200)
        self.assertEqual(over_http_wrong, direct_wrong)
        self.assertEqual(over_http_wrong["error"]["code"], -32022)

    def test_exact_one_mib_json_body_is_accepted_and_one_byte_over_is_rejected(self) -> None:
        from folderbridge_mcp.runtime_transport import MAX_MCP_REQUEST_BYTES

        host, token = self._host()
        prefix = b'{"jsonrpc":"2.0","id":20,"method":"ping","params":{"pad":"'
        suffix = b'"}}'
        padding = MAX_MCP_REQUEST_BYTES - len(prefix) - len(suffix)
        self.assertGreater(padding, 0)
        exact = prefix + (b"x" * padding) + suffix
        self.assertEqual(len(exact), MAX_MCP_REQUEST_BYTES)

        status, payload = _raw_post(host.port, token, exact)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload.decode("utf-8"))["result"], {})

        too_large_length = MAX_MCP_REQUEST_BYTES + 1
        request = (
            f"POST /mcp HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{host.port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            f"Content-Length: {too_large_length}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", host.port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        self.assertEqual(int(response.split(b"\r\n", 1)[0].split()[1]), 413)
        _, _, payload = response.partition(b"\r\n\r\n")
        self.assertIn("1 MiB", json.loads(payload.decode("utf-8"))["error"])

    def test_concurrent_requests_and_token_retirement_are_request_scoped(self) -> None:
        host, token = self._host()
        barrier = threading.Barrier(9)
        results: list[tuple[int, dict | None]] = []
        lock = threading.Lock()

        def caller(index: int) -> None:
            barrier.wait()
            result = _post_json(
                host.port,
                token,
                {"jsonrpc": "2.0", "id": 100 + index, "method": "ping", "params": {}},
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=caller, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 8)
        self.assertTrue(all(status == 200 for status, _ in results), results)

        host.admission.retire_generation(token)
        status, payload = _post_json(
            host.port,
            token,
            {"jsonrpc": "2.0", "id": 200, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 401)
        self.assertIn("retired", payload["error"])

        new_token = "x" * 64
        host.register_generation(new_token, generation=2)
        status, payload = _post_json(
            host.port,
            new_token,
            {"jsonrpc": "2.0", "id": 201, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], {})

    def test_client_disconnect_does_not_end_runtimehost(self) -> None:
        host, token = self._host()
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 300, "method": "tools/list", "params": {}},
            separators=(",", ":"),
        ).encode("utf-8")
        request = (
            f"POST /mcp HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{host.port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        sock = socket.create_connection(("127.0.0.1", host.port), timeout=3)
        sock.sendall(request)
        sock.close()

        status, payload = _post_json(
            host.port,
            token,
            {"jsonrpc": "2.0", "id": 301, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], {})

    def test_transport_does_not_impose_lower_cap_on_large_bounded_response(self) -> None:
        from folderbridge_mcp.runtime_transport import GenerationAdmission, PrivateMcpHttpServer

        admission = GenerationAdmission(max_accepted=16)
        token = "y" * 64
        admission.register_generation(token, generation=1, state="active")
        large_text = "z" * (2 * 1024 * 1024)

        def dispatch(request):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"payload": large_text},
            }

        server = PrivateMcpHttpServer(dispatch, admission, max_open_connections=4)
        server.start()
        self.addCleanup(server.close)

        status, payload = _post_json(
            server.port,
            token,
            {"jsonrpc": "2.0", "id": 400, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]["payload"]), len(large_text))




class V35LoopbackExhaustionAcceptanceTests(unittest.TestCase):
    def _server(self, *, max_open: int = 2, timeout: float = 0.15):
        from folderbridge_mcp.runtime_transport import GenerationAdmission, PrivateMcpHttpServer

        admission = GenerationAdmission(max_accepted=32)
        token = "q" * 64
        admission.register_generation(token, generation=1, state="active")
        server = PrivateMcpHttpServer(
            lambda request: {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {},
            },
            admission,
            max_open_connections=max_open,
            read_timeout_seconds=timeout,
        )
        server.start()
        self.addCleanup(server.close)
        return server, token

    def test_slow_header_clients_are_bounded_and_capacity_recovers_after_timeout(self) -> None:
        import time

        server, token = self._server(max_open=2, timeout=0.15)
        slow: list[socket.socket] = []
        try:
            for _ in range(2):
                sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
                sock.settimeout(1)
                sock.sendall(b"POST /mcp HTTP/1.1\r\n")
                slow.append(sock)

            time.sleep(0.05)
            third = socket.create_connection(("127.0.0.1", server.port), timeout=2)
            third.settimeout(1)
            third.sendall(b"POST /mcp HTTP/1.1\r\n")
            try:
                rejected = third.recv(64)
                self.assertEqual(rejected, b"")
            except (ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                third.close()

            time.sleep(0.30)
            status, payload = _post_json(
                server.port,
                token,
                {"jsonrpc": "2.0", "id": 500, "method": "ping", "params": {}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["result"], {})
        finally:
            for sock in slow:
                try:
                    sock.close()
                except OSError:
                    pass

    def test_partial_authenticated_body_times_out_without_leaking_listener_slot(self) -> None:
        import time

        server, token = self._server(max_open=1, timeout=0.15)
        body_length = 100
        request = (
            f"POST /mcp HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-FolderBridge-Generation-Token: {token}\r\n"
            f"Content-Length: {body_length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + b"{"
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall(request)
            response = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            self.assertEqual(int(bytes(response).split(b"\r\n", 1)[0].split()[1]), 408)
        finally:
            sock.close()

        time.sleep(0.05)
        status, payload = _post_json(
            server.port,
            token,
            {"jsonrpc": "2.0", "id": 501, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], {})


if __name__ == "__main__":
    unittest.main()
