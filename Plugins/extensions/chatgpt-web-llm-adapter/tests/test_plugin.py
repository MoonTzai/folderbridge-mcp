from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import browser_runtime as runtime  # noqa: E402


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], bool]] = []
        self.login_calls = 0
        self.max_parallel = 2
        self.active_requests = 0

    def ready(self) -> bool:
        return True

    def provider_state(self) -> str:
        return "ready"

    def open_login_page(self) -> dict[str, object]:
        self.login_calls += 1
        return {"ok": True, "opened": True, "target_id": "fake-login-target"}

    def concurrency_state(self) -> dict[str, int]:
        return {"max_parallel": self.max_parallel, "active_requests": self.active_requests}

    def set_max_parallel(self, value: int) -> dict[str, int]:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= runtime.MAX_WEB_PARALLEL:
            raise ValueError(f"max_parallel must be 1–{runtime.MAX_WEB_PARALLEL}")
        self.max_parallel = value
        return self.concurrency_state()

    def complete(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        self.calls.append((messages, json_mode))
        return '{"ok":true,"source":"fake"}' if json_mode else "semantic response"


class BrowserRuntimeContractTests(unittest.TestCase):
    def test_wrapped_prompt_preserves_roles_and_declares_fresh_stateless_transport(self) -> None:
        messages = [
            {"role": "system", "content": "系统约束"},
            {"role": "user", "content": "问题 A"},
            {"role": "assistant", "content": "历史回答"},
            {"role": "user", "content": "问题 B"},
        ]
        prompt = runtime.build_wrapped_prompt(messages, json_mode=True)
        self.assertIn("stateless compatibility request", prompt)
        self.assertIn("fresh ChatGPT conversation", prompt)
        self.assertIn("Return exactly one JSON object", prompt)
        payload = json.loads(prompt.split("REQUEST_JSON:\n", 1)[1])
        self.assertEqual(payload["system_messages"], ["系统约束"])
        self.assertEqual(payload["conversation"], messages[1:])

    def test_json_mode_normalizes_only_nonsemantic_display_wrappers(self) -> None:
        self.assertEqual(runtime.normalize_json_object_response('{"b":2,"a":1}'), '{"b":2,"a":1}')
        wrapped = '```json\nCopy code\n{"ok":true,"items":[1,2]}\n```'
        self.assertEqual(runtime.normalize_json_object_response(wrapped), '{"ok":true,"items":[1,2]}')
        same_line_wrapper = '```json Copy code\n{"ok":true,"items":[1,2]}\n```'
        self.assertEqual(runtime.normalize_json_object_response(same_line_wrapper), '{"ok":true,"items":[1,2]}')
        joined_wrapper = 'json   Copy code {"ok":true} ```'
        self.assertEqual(runtime.normalize_json_object_response(joined_wrapper), '{"ok":true}')
        with self.assertRaises(ValueError):
            runtime.normalize_json_object_response('Here is the result:\n{"ok":true}')
        with self.assertRaises(ValueError):
            runtime.normalize_json_object_response('{"a":1}\n{"b":2}')
        with self.assertRaises(TypeError):
            runtime.normalize_json_object_response('[1,2,3]')

    def test_adjustable_gate_defaults_to_a_hard_bounded_limit_and_can_resize_live(self) -> None:
        gate = runtime.AdjustableConcurrencyGate(2)
        self.assertEqual(gate.state(), {"max_parallel": 2, "active_requests": 0})
        gate.set_limit(runtime.MAX_WEB_PARALLEL)
        self.assertEqual(gate.state()["max_parallel"], 4)
        with self.assertRaises(ValueError):
            gate.set_limit(5)

    def test_fresh_chat_safety_gate_spaces_starts_caps_window_and_exponentially_cools_down(self) -> None:
        now = [0.0]
        def clock() -> float:
            return now[0]
        def sleep(seconds: float) -> None:
            now[0] += seconds
        gate = runtime.FreshChatSafetyGate(
            min_interval_seconds=20,
            window_seconds=300,
            window_max=2,
            cooldown_base_seconds=120,
            cooldown_max_seconds=600,
            cooldown_reset_seconds=1800,
            clock=clock,
            sleeper=sleep,
        )
        gate.wait_for_slot(); self.assertEqual(now[0], 0.0)
        gate.wait_for_slot(); self.assertEqual(now[0], 20.0)
        gate.wait_for_slot(); self.assertEqual(now[0], 300.0, "rolling window must dominate the 20-second cadence")
        state = gate.note_history_rate_limit(); self.assertEqual(state["history_rate_limit_strikes"], 1); self.assertEqual(state["history_cooldown_remaining_seconds"], 120)
        gate.wait_for_slot(); self.assertEqual(now[0], 420.0)
        state = gate.note_history_rate_limit(); self.assertEqual(state["history_rate_limit_strikes"], 2); self.assertEqual(state["history_cooldown_remaining_seconds"], 240)
        now[0] += 1801
        self.assertEqual(gate.state()["history_rate_limit_strikes"], 0)

    def test_request_validation_is_generic_but_bounded(self) -> None:
        request = {
            "model": runtime.MODEL_ID,
            "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
            "stream": True,
            "response_format": {"type": "json_object"},
            "max_tokens": 8192,
            "reasoning_effort": "high",
            "temperature": 0.2,
            "stop": ["never-enforced-by-web"],
        }
        messages, json_mode, stream, model = runtime.validate_chat_request(request)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(json_mode)
        self.assertTrue(stream)
        self.assertEqual(model, runtime.MODEL_ID)
        with self.assertRaisesRegex(ValueError, "model must"):
            runtime.validate_chat_request({**request, "model": "gpt-pretend"})
        with self.assertRaisesRegex(ValueError, "unsupported request fields"):
            runtime.validate_chat_request({**request, "api_key": "must-not-be-part-of-body"})
        with self.assertRaisesRegex(ValueError, "only string"):
            runtime.validate_chat_request({**request, "messages": [{"role": "user", "content": [{"type": "text"}]}]})

    def test_loopback_http_surface_requires_bearer_and_supports_file_origin_sse_and_json(self) -> None:
        backend = FakeBackend()
        token = "unit-test-loopback-token"
        server = runtime.AdapterHttpServer((runtime.HOST, 0), token, backend)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/v1/models")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            self.assertNotIn(token, response.read().decode("utf-8"))
            conn.close()

            body = json.dumps({
                "model": runtime.MODEL_ID,
                "messages": [{"role": "system", "content": "strict json"}, {"role": "user", "content": "return object"}],
                "stream": True,
                "response_format": {"type": "json_object"},
            }).encode("utf-8")
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                    "Origin": "null",
                },
            )
            response = conn.getresponse()
            payload = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "null")
            self.assertEqual(response.getheader("X-ChatGPT-Web-Test-Adapter"), "prompt-wrapped-system; fresh-chat-per-request")
            self.assertIn("data: [DONE]", payload)
            self.assertIn('"system_fingerprint":"chatgpt-web-test-prompt-wrapped"', payload)
            self.assertNotIn(token, payload)
            self.assertEqual(len(backend.calls), 1)
            self.assertTrue(backend.calls[0][1])
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/health", headers={"Origin": "null"})
            response = conn.getresponse()
            health_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(health_payload["adapter_version"], runtime.ADAPTER_VERSION)
            self.assertEqual(runtime.ADAPTER_VERSION, "0.3.2")
            self.assertEqual(health_payload["max_parallel"], 2)
            self.assertEqual(health_payload["active_requests"], 0)
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/control/max-parallel",
                body=json.dumps({"max_parallel": 1}),
                headers={"Authorization": "Bearer " + token, "Origin": "null", "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            parallel_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(parallel_payload["max_parallel"], 1)
            self.assertEqual(backend.max_parallel, 1)
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/control/max-parallel",
                body=json.dumps({"max_parallel": 5}),
                headers={"Authorization": "Bearer " + token, "Origin": "null", "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/control/open-login",
                headers={"Authorization": "Bearer " + token, "Origin": "null"},
            )
            response = conn.getresponse()
            login_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertTrue(login_payload["opened"])
            self.assertEqual(backend.login_calls, 1)
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request("POST", "/control/open-login", headers={"Origin": "null"})
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.close()

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "OPTIONS",
                "/v1/chat/completions",
                headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_history_limit_is_exposed_as_429_instead_of_generic_502(self) -> None:
        backend = FakeBackend(); token = "rate-limit-token"
        backend.complete = mock.Mock(side_effect=runtime.ExtensionError("HISTORY_RATE_LIMITED", "history cooldown active", retryable=True))
        server = runtime.AdapterHttpServer((runtime.HOST, 0), token, backend)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True); thread.start()
        host, port = server.server_address
        try:
            body = json.dumps({"model":runtime.MODEL_ID,"messages":[{"role":"user","content":"x"}],"stream":False}).encode("utf-8")
            conn = HTTPConnection(host, port, timeout=5); conn.request("POST","/v1/chat/completions",body=body,headers={"Content-Type":"application/json","Authorization":"Bearer "+token,"Origin":"null"})
            response=conn.getresponse(); payload=json.loads(response.read().decode("utf-8")); conn.close()
            self.assertEqual(response.status,429); self.assertEqual(payload["error"]["code"],"history_rate_limited")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_provider_state_prioritizes_history_limit_over_another_ready_tab(self) -> None:
        targets=[
            {"id":"a","type":"page","url":"https://chatgpt.com/","webSocketDebuggerUrl":"ws://127.0.0.1/a"},
            {"id":"b","type":"page","url":"https://chatgpt.com/c/1","webSocketDebuggerUrl":"ws://127.0.0.1/b"},
        ]
        session=mock.Mock(); session.evaluate.return_value={"readyState":"complete","composer":True}
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(runtime,"locate_browser",return_value=Path(r"C:\\Fake\\chrome.exe")):
                controller=runtime.BrowserController(Path(temporary),max_parallel=1,response_timeout_seconds=30)
            controller.debug_port=runtime.DEVTOOLS_PORT
            with mock.patch.object(controller,"_devtools_online",return_value=True), mock.patch.object(runtime,"_http_json",return_value=targets), mock.patch.object(runtime,"CdpSession",return_value=session), mock.patch.object(controller,"_history_limited",side_effect=[False,True]):
                self.assertEqual(controller.provider_state(),"history_rate_limited")

    def test_fixed_devtools_http_endpoint_is_authority_without_active_port_file(self) -> None:
        fake_process = mock.Mock()
        fake_process.poll.return_value = None
        fake_process.stderr = None
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(runtime, "locate_browser", return_value=Path(r"C:\\Fake\\chrome.exe")):
                controller = runtime.BrowserController(Path(temporary), max_parallel=1, response_timeout_seconds=30)
            with mock.patch.object(runtime, "ensure_loopback_port_available"), mock.patch.object(
                runtime.subprocess, "Popen", return_value=fake_process
            ), mock.patch.object(runtime, "_http_json", return_value={"Browser": "Chrome/Fake"}) as probe:
                controller.start()
        self.assertEqual(controller.debug_port, runtime.DEVTOOLS_PORT)
        self.assertTrue(any(f":{runtime.DEVTOOLS_PORT}/json/version" in str(call.args[0]) for call in probe.call_args_list))

    def test_provider_state_uses_devtools_as_authority_after_launcher_pid_exits(self) -> None:
        fake_process = mock.Mock()
        fake_process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(runtime, "locate_browser", return_value=Path(r"C:\\Fake\\chrome.exe")):
                controller = runtime.BrowserController(Path(temporary), max_parallel=1, response_timeout_seconds=30)
            controller.process = fake_process
            controller.debug_port = runtime.DEVTOOLS_PORT
            def probe(url: str, **_kwargs):
                if url.endswith("/json/version"):
                    return {"Browser": "Chrome/Fake"}
                if url.endswith("/json/list"):
                    return []
                raise AssertionError(url)
            with mock.patch.object(runtime, "_http_json", side_effect=probe):
                self.assertEqual(controller.provider_state(), "chatgpt_page_missing")
                self.assertTrue(controller._devtools_online())

    def test_open_login_page_reuses_existing_chatgpt_page_instead_of_creating_history_load(self) -> None:
        fake_session = mock.Mock()
        fake_target = {"id": "target-1", "webSocketDebuggerUrl": "ws://127.0.0.1/fake"}
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(runtime, "locate_browser", return_value=Path(r"C:\\Fake\\chrome.exe")):
                controller = runtime.BrowserController(Path(temporary), max_parallel=1, response_timeout_seconds=30)
            controller.debug_port = runtime.DEVTOOLS_PORT
            with mock.patch.object(controller, "ensure_running", return_value=True) as ensure, mock.patch.object(
                runtime, "_existing_chatgpt_target", return_value=fake_target
            ), mock.patch.object(runtime, "_new_target") as create, mock.patch.object(runtime, "CdpSession", return_value=fake_session):
                result = controller.open_login_page()
        ensure.assert_called_once_with(); create.assert_not_called()
        fake_session.call.assert_called_with("Page.bringToFront")
        fake_session.close.assert_called_once_with()
        self.assertTrue(result["browser_restarted"]); self.assertTrue(result["reused_existing_chatgpt_page"])
        self.assertEqual(result["target_id"], "target-1")

    def test_open_login_page_without_existing_page_uses_fresh_chat_safety_gate(self) -> None:
        fake_session = mock.Mock(); fake_target = {"id": "target-2", "webSocketDebuggerUrl": "ws://127.0.0.1/fake2"}
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(runtime, "locate_browser", return_value=Path(r"C:\\Fake\\chrome.exe")):
                controller = runtime.BrowserController(Path(temporary), max_parallel=1, response_timeout_seconds=30)
            controller.debug_port = runtime.DEVTOOLS_PORT
            with mock.patch.object(controller, "ensure_running", return_value=False), mock.patch.object(
                runtime, "_existing_chatgpt_target", return_value=None
            ), mock.patch.object(controller.fresh_chat_safety, "wait_for_slot") as paced, mock.patch.object(
                runtime, "_new_target", return_value=fake_target
            ), mock.patch.object(runtime, "CdpSession", return_value=fake_session):
                result = controller.open_login_page()
        paced.assert_called_once_with(); self.assertFalse(result["reused_existing_chatgpt_page"])

    def test_plugin_entrypoint_is_generic_and_exposes_no_project_specific_action(self) -> None:
        spec = importlib.util.spec_from_file_location("chatgpt_web_llm_adapter_plugin", PLUGIN_ROOT / "plugin.py")
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.handle))
        source = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        self.assertNotIn("Cognitive", source)
        self.assertNotIn("Debate-Judge", source)
        self.assertNotIn("认知星图", source)
        self.assertNotIn("shell=True", source)
        connection_block = source.split('if action == "connection":', 1)[1].split('if action == "verification-plan":', 1)[0]
        self.assertIn("include_token=False", connection_block)
        self.assertNotIn("include_token=True", connection_block)


if __name__ == "__main__":
    unittest.main()
