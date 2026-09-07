from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from folderbridge_mcp.launcher_backend import LauncherSettingsStore, mcp_command
from folderbridge_mcp.mcp import MAX_MESSAGE_BYTES


ROOT = Path(__file__).resolve().parents[1]
REGISTERED = [Path(value) for value in LauncherSettingsStore().load().workspaces]
TOOLS_CANDIDATES = REGISTERED + [ROOT.parent / "Tools", ROOT.parent.parent / "Tools"]
TOOLS = next((p for p in TOOLS_CANDIDATES if (p / "tunnel-client-v0.0.14-windows-amd64.zip").is_file()), TOOLS_CANDIDATES[0])
ZIP_PATH = TOOLS / "tunnel-client-v0.0.14-windows-amd64.zip"


def _decode(body: bytes) -> dict:
    events = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(b"data: "):
            events.append(stripped[6:].strip())
    payload = events[-1] if events else body.strip()
    return json.loads(payload.decode("utf-8"))


def _post(url: str, payload: dict, session_id: str | None = None, timeout: float = 15.0):
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Content-Type", "application/json")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, _decode(response.read()), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            decoded = _decode(body)
        except Exception:
            decoded = {"raw": body.decode("utf-8", errors="replace")}
        return exc.code, decoded, dict(exc.headers.items())


class OversizedSharedStdioRegressionTests(unittest.TestCase):
    def test_v0014_real_folderbridge_oversize_is_request_scoped_and_child_survives(self) -> None:
        self.assertTrue(ZIP_PATH.is_file(), ZIP_PATH)
        temp_path = Path(tempfile.mkdtemp(prefix="folderbridge-oversize-v0014-"))
        try:
            with zipfile.ZipFile(ZIP_PATH) as archive:
                member = next(
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                    and Path(item.filename.replace("\\", "/")).name.lower() == "tunnel-client.exe"
                )
                exe = temp_path / "tunnel-client.exe"
                exe.write_bytes(archive.read(member))

            info_file = temp_path / "proxy-info.json"
            health_file = temp_path / "health.url"
            log_file = temp_path / "dev-proxy.log"
            log_handle = log_file.open("w", encoding="utf-8")
            proc = None
            try:
                env = dict(os.environ)
                for name in list(env):
                    lower = name.lower()
                    if (
                        "control_plane_api_key" in lower
                        or "openai_api_key" in lower
                        or "tunnel_api_key" in lower
                        or lower.endswith("_token")
                        or lower in {"http_proxy", "https_proxy", "all_proxy"}
                    ):
                        env.pop(name, None)
                env["NO_PROXY"] = "127.0.0.1,localhost"
                env["no_proxy"] = "127.0.0.1,localhost"

                proc = subprocess.Popen(
                    [
                        str(exe),
                        "dev",
                        "proxy",
                        "--listen",
                        "127.0.0.1:0",
                        "--tunnel-id",
                        "tunnel_" + ("e" * 32),
                        "--mcp-command",
                        mcp_command(ROOT, "read_only", False, ()),
                        "--health-listen-addr",
                        "127.0.0.1:0",
                        "--health-url-file",
                        str(health_file),
                        "--url-file",
                        str(info_file),
                        "--backend",
                        "go",
                        "--engine-queue-backend",
                        "inmem",
                        "--readiness-timeout",
                        "30s",
                        "--response-timeout",
                        "15s",
                        "--client-last-seen-timeout",
                        "5s",
                    ],
                    cwd=temp_path,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                deadline = time.monotonic() + 32
                while time.monotonic() < deadline and not info_file.is_file():
                    if proc.poll() is not None:
                        log_handle.flush()
                        self.fail(log_file.read_text(encoding="utf-8", errors="replace")[-12000:])
                    time.sleep(0.05)
                self.assertTrue(info_file.is_file())
                url = json.loads(info_file.read_text(encoding="utf-8"))["mcp_url"]

                status, init, headers = _post(
                    url,
                    {
                        "jsonrpc": "2.0",
                        "id": 700,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "oversize-regression", "version": "1"},
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(init.get("id"), 700)
                session_id = headers.get("Mcp-Session-Id")

                # Prove the old 1 MiB ceiling is gone end-to-end. The child is
                # read-only, so edit_file returns a normal tool-level read-only
                # error after parsing the entire large frame; that is sufficient
                # to prove transport + JSON-RPC framing survived intact.
                for index, payload_mib in enumerate((2, 4, 8, 16), start=701):
                    large = {
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "tools/call",
                        "params": {
                            "name": "edit_file",
                            "arguments": {
                                "workspace_id": "126f215203a0",
                                "path": "transport-capacity-probe.txt",
                                "create_content": "x" * (payload_mib * 1024 * 1024),
                            },
                        },
                    }
                    encoded = json.dumps(large, separators=(",", ":")).encode("utf-8")
                    self.assertGreater(len(encoded), 1024 * 1024)
                    self.assertLess(len(encoded), MAX_MESSAGE_BYTES)
                    status, response, _ = _post(url, large, session_id, timeout=30.0)
                    self.assertEqual(status, 200, response)
                    self.assertEqual(response.get("id"), index, response)
                    self.assertIn("result", response)
                    self.assertTrue(response["result"].get("isError"), response)
                    self.assertIsNone(proc.poll(), f"tunnel-client died at {payload_mib} MiB")

                oversized = {
                    "jsonrpc": "2.0",
                    "id": 710,
                    "method": "tools/call",
                    "params": {
                        "name": "edit_file",
                        "arguments": {
                            "workspace_id": "126f215203a0",
                            "path": "transport-oversize-probe.txt",
                            "create_content": "x" * (MAX_MESSAGE_BYTES + 8192),
                        },
                    },
                }
                encoded = json.dumps(oversized, separators=(",", ":")).encode("utf-8")
                self.assertGreater(len(encoded), MAX_MESSAGE_BYTES)

                status, rejected, _ = _post(url, oversized, session_id, timeout=30.0)
                self.assertEqual(status, 200, rejected)
                self.assertEqual(rejected.get("id"), 710, rejected)
                self.assertEqual(
                    rejected.get("error", {}).get("message"),
                    f"Message exceeds {MAX_MESSAGE_BYTES // (1024 * 1024)} MiB",
                    rejected,
                )
                self.assertIsNone(proc.poll(), "tunnel-client must survive one oversized logical request")

                status, recovered, _ = _post(
                    url,
                    {
                        "jsonrpc": "2.0",
                        "id": 711,
                        "method": "tools/call",
                        "params": {"name": "server_info", "arguments": {}},
                    },
                    session_id,
                    timeout=30.0,
                )
                self.assertEqual(status, 200, recovered)
                self.assertEqual(recovered.get("id"), 711, recovered)
                self.assertIn("result", recovered)
                self.assertIsNone(proc.poll(), "shared runtime must remain alive after the rejected request")

                log_handle.flush()
                logs = log_file.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("received response without valid ID from MCP server", logs)
                self.assertNotIn("stdio MCP command failed; requesting tunnel-client shutdown", logs)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                if not log_handle.closed:
                    log_handle.close()
                # The real MCP child inherits the tunnel process stdout handle
                # and can take a short moment to observe stdin EOF after the
                # dev-proxy parent exits. Retry cleanup instead of treating that
                # normal Windows handle-release lag as a product failure.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        shutil.rmtree(temp_path)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    shutil.rmtree(temp_path, ignore_errors=True)
        finally:
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
