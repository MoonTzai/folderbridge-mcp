from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from folderbridge_mcp.extension_api import ExtensionError as ToolError


THIS_FILE = Path(__file__).resolve()
PLUGIN_ROOT = THIS_FILE.parent.parent
if not (PLUGIN_ROOT / "comfyui_runtime.py").is_file():
    PLUGIN_ROOT = THIS_FILE.parents[1] / "Plugins" / "extensions" / "comfyui"
PLUGIN_RUNTIME = PLUGIN_ROOT / "comfyui_runtime.py"
_SPEC = importlib.util.spec_from_file_location("folderbridge_external_comfyui_runtime", PLUGIN_RUNTIME)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Could not load external ComfyUI runtime for tests")
_RUNTIME = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNTIME)
comfyui_status = _RUNTIME.comfyui_status
release_comfyui_memory = _RUNTIME.release_comfyui_memory
list_jobs = _RUNTIME.list_jobs
get_job_status = _RUNTIME.get_job_status
get_health_check = _RUNTIME.get_health_check
get_progress = _RUNTIME.get_progress
cancel_prompt = _RUNTIME.cancel_prompt
get_node_info = _RUNTIME.get_node_info
list_models = _RUNTIME.list_models
get_features = _RUNTIME.get_features
preflight_workflow = _RUNTIME.preflight_workflow
run_workflow = _RUNTIME.run_workflow

JOB_ID = "11111111-1111-4111-8111-111111111111"


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _FakeComfyHandler(BaseHTTPRequestHandler):
    server_version = "FakeComfy/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/system_stats":
            self._json(200, self.server.system_stats)  # type: ignore[attr-defined]
            return
        if parsed.path == "/features":
            self._json(200, self.server.features)  # type: ignore[attr-defined]
            return
        if parsed.path == "/models":
            self._json(200, self.server.model_types)  # type: ignore[attr-defined]
            return
        if parsed.path.startswith("/models/"):
            folder = unquote(parsed.path.removeprefix("/models/"))
            values = self.server.models.get(folder)  # type: ignore[attr-defined]
            if values is None:
                self.send_error(404)
            else:
                self._json(200, values)
            return
        if parsed.path == "/queue":
            self.server.queue_requests += 1  # type: ignore[attr-defined]
            self._json(200, {"queue_running": self.server.queue_running, "queue_pending": self.server.queue_pending})  # type: ignore[attr-defined]
            return
        if parsed.path == "/api/jobs":
            self.server.jobs_requests += 1  # type: ignore[attr-defined]
            self.server.last_jobs_query = parsed.query  # type: ignore[attr-defined]
            self._json(200, self.server.jobs_response)  # type: ignore[attr-defined]
            return
        if parsed.path.startswith("/api/jobs/"):
            prompt_id = unquote(parsed.path.removeprefix("/api/jobs/"))
            self.server.job_status_requests += 1  # type: ignore[attr-defined]
            if prompt_id != JOB_ID:
                self.send_error(404)
            else:
                self._json(200, self.server.job_details)  # type: ignore[attr-defined]
            return
        if parsed.path.startswith("/object_info/"):
            class_name = unquote(parsed.path.removeprefix("/object_info/"))
            info = self.server.object_info.get(class_name)  # type: ignore[attr-defined]
            if info is None:
                self.send_error(404)
            else:
                self._json(200, {class_name: info})
            return
        if parsed.path == "/history/prompt-test":
            self.server.history_requests += 1  # type: ignore[attr-defined]
            if self.server.history_failures_remaining > 0:  # type: ignore[attr-defined]
                self.server.history_failures_remaining -= 1  # type: ignore[attr-defined]
                self._json(503, {"error": "temporarily busy"})
                return
            self.server.history_entered.set()  # type: ignore[attr-defined]
            gate = self.server.history_gate  # type: ignore[attr-defined]
            if gate is not None:
                gate.wait(timeout=5)
            if not self.server.history_complete:  # type: ignore[attr-defined]
                self._json(200, {})
                return
            self._json(
                200,
                {
                    "prompt-test": {
                        "status": {"status_str": "success"},
                        "outputs": self.server.history_outputs,  # type: ignore[attr-defined]
                    }
                },
            )
            return
        if parsed.path == "/view":
            self.server.view_requests += 1  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_1X1)))
            self.end_headers()
            self.wfile.write(PNG_1X1)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/free":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            self.server.free_requests += 1  # type: ignore[attr-defined]
            self.server.last_free_body = body  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
            prompt_id = unquote(self.path[len("/api/jobs/"):-len("/cancel")])
            self.server.targeted_cancel_requests += 1  # type: ignore[attr-defined]
            self.server.last_cancel_prompt_id = prompt_id  # type: ignore[attr-defined]
            if not self.server.targeted_cancel_available:  # type: ignore[attr-defined]
                self.send_error(404)
                return
            self._json(200, {"cancelled": True})
            return
        if self.path == "/interrupt":
            self.server.global_interrupt_requests += 1  # type: ignore[attr-defined]
            self._json(200, {})
            return
        if self.path != "/prompt":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.server.prompt_requests += 1  # type: ignore[attr-defined]
        self.server.last_prompt = body  # type: ignore[attr-defined]
        self._json(200, {"prompt_id": "prompt-test", "node_errors": {}})

    def _json(self, status: int, value: object) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ComfyUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.workspace = self.root
        self.comfy_root = self.root / "ComfyUI"
        (self.comfy_root / "output").mkdir(parents=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeComfyHandler)
        self.server.system_stats = {  # type: ignore[attr-defined]
            "system": {
                "os": "test",
                "comfyui_version": "test-version",
                "ram_total": 1000,
                "ram_free": 250,
                "argv": [str(self.comfy_root / "main.py"), "--fast-disk"],
            },
            "devices": [
                {
                    "name": "test-gpu",
                    "type": "cuda",
                    "index": 0,
                    "vram_total": 1000,
                    "vram_free": 150,
                    "torch_vram_total": 900,
                    "torch_vram_free": 100,
                }
            ],
        }
        self.server.history_outputs = {  # type: ignore[attr-defined]
            "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
        }
        self.server.object_info = {  # type: ignore[attr-defined]
            "MiniMaxH3Director": {
                "input": {
                    "optional": {
                        "guard_long_v2v_segments": ["BOOLEAN", {"default": True}],
                    }
                }
            }
        }
        self.server.features = {"jobs_api": True, "preview_metadata": True}  # type: ignore[attr-defined]
        self.server.model_types = ["checkpoints", "vae", "diffusion_models"]  # type: ignore[attr-defined]
        self.server.models = {  # type: ignore[attr-defined]
            "checkpoints": ["zeta.safetensors", "alpha.safetensors", "alpha-refiner.safetensors"],
            "vae": ["video_vae.safetensors"],
        }
        self.server.jobs_response = {  # type: ignore[attr-defined]
            "jobs": [{"id": JOB_ID, "status": "in_progress", "create_time": 1234, "outputs_count": 0}],
            "pagination": {"offset": 0, "limit": 20, "total": 1, "has_more": False},
        }
        self.server.queue_running = [  # type: ignore[attr-defined]
            [
                4,
                JOB_ID,
                {"5": {"class_type": "MiniMaxH3Director", "inputs": {}}, "7": {"class_type": "SaveVideo", "inputs": {}}},
                {"client_id": "folderbridge-progress-test"},
                ["7"],
            ]
        ]
        self.server.queue_pending = []  # type: ignore[attr-defined]
        self.server.job_details = {  # type: ignore[attr-defined]
            "id": JOB_ID,
            "status": "completed",
            "priority": 7,
            "create_time": 1234,
            "execution_start_time": 1240,
            "execution_end_time": 1300,
            "outputs_count": 1,
            "preview_output": {"filename": "result.mp4", "subfolder": "video", "type": "output"},
            "outputs": {"9": {"images": [{"filename": "result.mp4", "subfolder": "video", "type": "output"}]}},
            "workflow": {"prompt": {"secret": "must-not-leak"}, "extra_data": {"client_id": "hidden"}},
        }
        self.server.history_complete = True  # type: ignore[attr-defined]
        self.server.history_failures_remaining = 0  # type: ignore[attr-defined]
        self.server.history_requests = 0  # type: ignore[attr-defined]
        self.server.history_entered = threading.Event()  # type: ignore[attr-defined]
        self.server.history_gate = None  # type: ignore[attr-defined]
        self.server.prompt_requests = 0  # type: ignore[attr-defined]
        self.server.free_requests = 0  # type: ignore[attr-defined]
        self.server.last_free_body = None  # type: ignore[attr-defined]
        self.server.jobs_requests = 0  # type: ignore[attr-defined]
        self.server.last_jobs_query = ""  # type: ignore[attr-defined]
        self.server.job_status_requests = 0  # type: ignore[attr-defined]
        self.server.queue_requests = 0  # type: ignore[attr-defined]
        self.server.targeted_cancel_requests = 0  # type: ignore[attr-defined]
        self.server.last_cancel_prompt_id = None  # type: ignore[attr-defined]
        self.server.targeted_cancel_available = True  # type: ignore[attr-defined]
        self.server.global_interrupt_requests = 0  # type: ignore[attr-defined]
        self.server.view_requests = 0  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _safe_minimax_profile_workflow(
        self,
        *,
        frames: int = 124,
        chunks: int = 2,
        head_chunks: int = 4,
        guard: bool | None = None,
    ) -> dict[str, object]:
        timeline = {
            "version": 4,
            "segments": [
                {"id": "s0", "start": 0, "length": frames, "frameCount": frames, "taskType": ""}
            ],
        }
        director_inputs: dict[str, object] = {
            "model": ["9", 0],
            "task_type": "v2v — Video to Video",
            "cfg": 1,
            "frame_rate": 24,
            "width": 1344,
            "height": 768,
            "ref_max_size": 1344,
            "total_frames": frames,
            "timeline_data": json.dumps(timeline),
            "steps": 8,
            "sampler": "dpmpp_2m",
            "scheduler": "simple",
            "shift_video": 12,
            "shift_audio": 3,
            "clear_vram_between_segments": True,
        }
        if guard is not None:
            director_inputs["guard_long_v2v_segments"] = guard
        return {
            "1": {"class_type": "UNETLoader", "inputs": {}},
            "8": {
                "class_type": "MiniMaxChunkFeedForward",
                "inputs": {"model": ["1", 0], "chunks": chunks, "seq_threshold": 4096},
            },
            "9": {
                "class_type": "MiniMaxLowVRAMAttention",
                "inputs": {"model": ["8", 0], "head_chunks": head_chunks},
            },
            "5": {"class_type": "MiniMaxH3Director", "inputs": director_inputs},
            "7": {"class_type": "SaveVideo", "inputs": {}},
        }

    def test_status_uses_loopback_comfyui(self) -> None:
        result = comfyui_status(port=self.port)
        self.assertTrue(result["online"])
        self.assertEqual(result["endpoint"], f"http://127.0.0.1:{self.port}")

    def test_free_uses_only_fixed_official_endpoint_and_flags(self) -> None:
        result = release_comfyui_memory(unload_models=True, free_memory=True, settle_seconds=0, port=self.port)
        self.assertTrue(result["acknowledged"])
        self.assertEqual(result["requested"], {"unload_models": True, "free_memory": True})
        self.assertEqual(self.server.free_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.last_free_body, {"unload_models": True, "free_memory": True})  # type: ignore[attr-defined]
        self.assertTrue(result["after"]["online"])

    def test_free_rejects_noop_request(self) -> None:
        with self.assertRaises(ToolError) as raised:
            release_comfyui_memory(unload_models=False, free_memory=False, settle_seconds=0, port=self.port)
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.server.free_requests, 0)  # type: ignore[attr-defined]

    def test_jobs_uses_bounded_job_api(self) -> None:
        result = list_jobs(statuses=["in_progress", "pending"], limit=20, offset=0, port=self.port)
        self.assertEqual(result["jobs"][0]["id"], JOB_ID)
        self.assertEqual(result["jobs"][0]["status"], "in_progress")
        self.assertEqual(self.server.jobs_requests, 1)  # type: ignore[attr-defined]
        self.assertIn("status=in_progress%2Cpending", self.server.last_jobs_query)  # type: ignore[attr-defined]
        self.assertIn("limit=20", self.server.last_jobs_query)  # type: ignore[attr-defined]

    def test_job_status_strips_workflow_and_returns_bounded_artifacts(self) -> None:
        result = get_job_status(JOB_ID, port=self.port)
        self.assertEqual(result["id"], JOB_ID)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("workflow", result)
        self.assertNotIn("outputs", result)
        self.assertEqual(result["artifacts"][0]["filename"], "result.mp4")
        self.assertEqual(self.server.job_status_requests, 1)  # type: ignore[attr-defined]

    def test_health_check_aggregates_generic_runtime_job_and_queue_without_websocket_or_mutation(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["health_schema_version"], 1)
        self.assertEqual(result["job"]["status"], "in_progress")
        self.assertTrue(result["runtime"]["online"])
        self.assertTrue(result["runtime"]["fast_disk"])
        self.assertEqual(result["runtime"]["ram"]["free_ratio"], 0.25)
        self.assertNotIn("class", result["runtime"]["ram"])
        self.assertEqual(result["runtime"]["devices"][0]["vram"]["free_ratio"], 0.15)
        self.assertNotIn("class", result["runtime"]["devices"][0]["vram"])
        self.assertEqual(result["queue"]["exact_prompt_state"], "running")
        self.assertEqual(result["queue"]["progress_capability"]["provider"], "minimax_h3_director")
        self.assertTrue(result["queue"]["progress_capability"]["supported"])
        self.assertEqual(result["queue"]["progress_capability"]["node_ids"], ["5"])
        self.assertEqual(result["assessment"]["state"], "running_exact_queue_bound")
        self.assertFalse(result["assessment"]["stall_suspected"])
        self.assertTrue(result["consistency"]["final_refresh_ok"])
        self.assertFalse(result["consistency"]["state_changed_during_snapshot"])
        self.assertEqual(result["single_snapshot_stall_rule"], "never_infer_stall_without_longitudinal_evidence")
        observer.assert_not_called()
        self.assertEqual(self.server.job_status_requests, 2)  # type: ignore[attr-defined]
        self.assertEqual(self.server.queue_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.targeted_cancel_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.free_requests, 0)  # type: ignore[attr-defined]

    def test_health_check_generic_non_director_workflow_remains_healthy_without_progress_provider(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        self.server.queue_running = [  # type: ignore[attr-defined]
            [4, JOB_ID, {"7": {"class_type": "SaveVideo", "inputs": {}}}, {"client_id": "folderbridge-generic-test"}, ["7"]]
        ]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["assessment"]["state"], "running_exact_queue_bound")
        self.assertEqual(result["queue"]["exact_prompt_state"], "running")
        capability = result["queue"]["progress_capability"]
        self.assertFalse(capability["supported"])
        self.assertIsNone(capability["provider"])
        self.assertEqual(capability["reason"], "unsupported_workflow")
        observer.assert_not_called()

    def test_health_check_terminal_prompt_skips_queue_and_websocket_observation(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "completed", "outputs_count": 1, "outputs": {}}  # type: ignore[attr-defined]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["assessment"]["state"], "terminal_completed")
        self.assertIsNone(result["queue"])
        self.assertEqual(self.server.queue_requests, 0)  # type: ignore[attr-defined]
        observer.assert_not_called()

    def test_health_check_failed_and_cancelled_are_terminal_without_queue_observation(self) -> None:
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.server.job_details = {"id": JOB_ID, "status": status, "outputs_count": 0}  # type: ignore[attr-defined]
                before_queue_requests = self.server.queue_requests  # type: ignore[attr-defined]
                result = get_health_check(JOB_ID, port=self.port)
                self.assertEqual(result["assessment"]["state"], f"terminal_{status}")
                self.assertIsNone(result["queue"])
                self.assertEqual(self.server.queue_requests, before_queue_requests)  # type: ignore[attr-defined]

    def test_health_check_pending_prompt_uses_queue_and_final_refresh(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "pending", "outputs_count": 0}  # type: ignore[attr-defined]
        self.server.queue_running = []  # type: ignore[attr-defined]
        self.server.queue_pending = [  # type: ignore[attr-defined]
            [4, JOB_ID, {"7": {"class_type": "SaveVideo", "inputs": {}}}, {"client_id": "folderbridge-pending-test"}, ["7"]]
        ]
        result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["job"]["status"], "pending")
        self.assertEqual(result["queue"]["exact_prompt_state"], "pending")
        self.assertEqual(result["assessment"]["state"], "pending")
        self.assertTrue(result["consistency"]["queue_snapshot_attempted"])
        self.assertTrue(result["consistency"]["queue_snapshot_ok"])
        self.assertTrue(result["consistency"]["final_refresh_attempted"])
        self.assertTrue(result["consistency"]["final_refresh_ok"])

    def test_health_check_final_job_refresh_wins_when_prompt_completes_during_snapshot(self) -> None:
        initial = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0, "artifacts_found": 0, "artifacts": []}
        final = {
            "id": JOB_ID,
            "status": "completed",
            "outputs_count": 1,
            "artifacts_found": 1,
            "artifacts": [{"filename": "done.mp4", "subfolder": "", "type": "output", "kind": "video", "node_id": "7", "output_key": "video"}],
        }
        queue = {"exact_prompt_state": "running", "running_count": 1, "pending_count": 0, "progress_capability": {"provider": "minimax_h3_director", "supported": True, "node_ids": ["5"], "reason": None}}
        with mock.patch.object(_RUNTIME, "get_job_status", side_effect=[initial, final]), mock.patch.object(
            _RUNTIME, "_queue_health_snapshot", return_value=queue
        ), mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["job"]["status"], "completed")
        self.assertEqual(result["job"]["outputs_count"], 1)
        self.assertEqual(result["assessment"]["state"], "terminal_completed")
        self.assertTrue(result["consistency"]["state_changed_during_snapshot"])
        self.assertEqual(result["consistency"]["initial_status"], "in_progress")
        self.assertEqual(result["consistency"]["final_status"], "completed")
        observer.assert_not_called()

    def test_health_check_queue_failure_does_not_block_final_terminal_authority(self) -> None:
        initial = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0, "artifacts_found": 0, "artifacts": []}
        final = {"id": JOB_ID, "status": "completed", "outputs_count": 1, "artifacts_found": 1, "artifacts": []}
        with mock.patch.object(_RUNTIME, "get_job_status", side_effect=[initial, final]), mock.patch.object(
            _RUNTIME, "_queue_health_snapshot", side_effect=ToolError("COMFYUI_HTTP_ERROR", "queue temporarily unavailable", status=503)
        ):
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["job"]["status"], "completed")
        self.assertEqual(result["assessment"]["state"], "terminal_completed")
        self.assertTrue(result["consistency"]["state_changed_during_snapshot"])
        self.assertFalse(result["consistency"]["queue_snapshot_ok"])
        self.assertTrue(result["consistency"]["final_refresh_ok"])
        self.assertEqual(result["consistency"]["queue_error"]["code"], "COMFYUI_HTTP_ERROR")

    def test_health_check_post_queue_refresh_failure_is_explicitly_incomplete(self) -> None:
        initial = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0, "artifacts_found": 0, "artifacts": []}
        queue = {"exact_prompt_state": "running", "running_count": 1, "pending_count": 0, "progress_capability": {"provider": None, "supported": False, "node_ids": [], "reason": "unsupported_workflow"}}
        with mock.patch.object(
            _RUNTIME, "get_job_status", side_effect=[initial, ToolError("COMFYUI_OFFLINE", "lost during refresh")]
        ), mock.patch.object(_RUNTIME, "_queue_health_snapshot", return_value=queue):
            result = get_health_check(JOB_ID, port=self.port)
        self.assertIsNone(result["job"])
        self.assertEqual(result["assessment"]["state"], "snapshot_incomplete")
        self.assertFalse(result["consistency"]["final_refresh_ok"])
        self.assertIsNone(result["consistency"]["state_changed_during_snapshot"])
        self.assertEqual(result["consistency"]["refresh_error"]["code"], "COMFYUI_OFFLINE")
        self.assertFalse(result["assessment"]["stall_suspected"])

    def test_health_check_unknown_prompt_is_explicit_and_websocket_free(self) -> None:
        unknown = "22222222-2222-4222-8222-222222222222"
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_health_check(unknown, port=self.port)
        self.assertEqual(result["assessment"]["state"], "unknown_prompt")
        self.assertIsNone(result["job"])
        self.assertIsNone(result["queue"])
        self.assertIsNone(result["consistency"]["state_changed_during_snapshot"])
        observer.assert_not_called()

    def test_health_check_runtime_offline_is_explicit_and_schema_stable(self) -> None:
        offline = {"online": False, "endpoint": "http://127.0.0.1:8188", "detail": "offline"}
        with mock.patch.object(_RUNTIME, "comfyui_status", return_value=offline), mock.patch.object(
            _RUNTIME, "_observe_progress_events"
        ) as observer:
            result = get_health_check(JOB_ID, port=self.port)
        self.assertEqual(result["health_schema_version"], 1)
        self.assertEqual(result["assessment"]["state"], "runtime_offline")
        self.assertIsNone(result["job"])
        self.assertIsNone(result["queue"])
        self.assertIsNone(result["consistency"]["state_changed_during_snapshot"])
        self.assertFalse(result["consistency"]["queue_snapshot_attempted"])
        self.assertIsNone(result["consistency"]["queue_snapshot_ok"])
        self.assertFalse(result["consistency"]["final_refresh_attempted"])
        self.assertIsNone(result["consistency"]["final_refresh_ok"])
        observer.assert_not_called()

    def test_progress_unknown_prompt_is_explicitly_unavailable(self) -> None:
        unknown = "22222222-2222-4222-8222-222222222222"
        result = get_progress(unknown, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "unknown_prompt")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.server.queue_requests, 0)  # type: ignore[attr-defined]

    def test_progress_completed_prompt_does_not_observe_or_mutate(self) -> None:
        result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "prompt_not_in_progress")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.server.queue_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.targeted_cancel_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.free_requests, 0)  # type: ignore[attr-defined]

    def test_progress_binds_exact_running_prompt_and_director_node(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        now = _RUNTIME.time.time()
        observed = {
            "director_progress": [
                {"observed_at": now, "data": {"node_id": "5", "phase": "sample", "phase_label": "采样", "phase_value": 0.0, "phase_max": 1, "overall_value": 2.0, "overall_max": 6, "segment": 1, "segment_total": 1, "timeline_segment": 1, "timeline_segment_total": 1, "frames_label": "192f", "task_key": "v2v"}},
                {"observed_at": now + 0.01, "data": {"node_id": "5", "phase": "sample", "phase_label": "采样", "phase_value": 0.25, "phase_max": 1, "overall_value": 2.25, "overall_max": 6, "segment": 1, "segment_total": 1, "timeline_segment": 1, "timeline_segment_total": 1, "frames_label": "192f", "task_key": "v2v"}},
            ],
            "director_preview": [],
            "node_progress": [
                {"observed_at": now + 0.01, "data": {"state": "running", "value": 2.25, "max": 6}},
            ],
            "executing": [{"observed_at": now, "data": {"node": "5"}}],
        }
        with mock.patch.object(_RUNTIME, "_observe_progress_events", return_value=observed) as observer:
            result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertTrue(result["available"])
        self.assertEqual(result["node_id"], "5")
        self.assertEqual(result["phase"], "sample")
        self.assertEqual(result["phase_value"], 0.25)
        self.assertTrue(result["progress_changed_during_observation"])
        self.assertEqual(result["binding"], "exact_running_prompt_client_and_director_node")
        self.assertEqual(result["executing_node"], "5")
        observer.assert_called_once()
        self.assertEqual(observer.call_args.kwargs["prompt_id"], JOB_ID)
        self.assertEqual(observer.call_args.kwargs["node_id"], "5")
        self.assertEqual(self.server.queue_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.targeted_cancel_requests, 0)  # type: ignore[attr-defined]

    def test_progress_unavailable_when_no_phase_event_is_observed(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        empty = {"director_progress": [], "director_preview": [], "node_progress": [], "executing": [{"observed_at": _RUNTIME.time.time(), "data": {"node": "5"}}]}
        with mock.patch.object(_RUNTIME, "_observe_progress_events", return_value=empty):
            result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "director_phase_event_not_observed")
        self.assertIsNone(result["last_update"])
        self.assertIsNone(result["age_seconds"])
        self.assertEqual(result["executing_node"], "5")

    def test_progress_non_folderbridge_client_fails_closed_without_socket_observation(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        self.server.queue_running[0][3]["client_id"] = "webui-client"  # type: ignore[attr-defined]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "non_folderbridge_client_session_not_observed")
        observer.assert_not_called()

    def test_progress_rejects_node_not_bound_to_current_prompt(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_progress(JOB_ID, node_id="99", observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "node_not_bound_to_prompt_director")
        observer.assert_not_called()

    def test_progress_multiple_directors_require_explicit_node_id(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        self.server.queue_running = [  # type: ignore[attr-defined]
            [
                4,
                JOB_ID,
                {
                    "5": {"class_type": "MiniMaxH3Director", "inputs": {}},
                    "6": {"class_type": "MiniMaxH3Director", "inputs": {}},
                    "7": {"class_type": "SaveVideo", "inputs": {}},
                },
                {"client_id": "folderbridge-progress-test"},
                ["7"],
            ]
        ]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "multiple_director_nodes_require_node_id")
        self.assertEqual(result["director_node_count"], 2)
        observer.assert_not_called()

    def test_progress_stale_queue_entry_for_other_prompt_never_binds(self) -> None:
        self.server.job_details = {"id": JOB_ID, "status": "in_progress", "outputs_count": 0}  # type: ignore[attr-defined]
        self.server.queue_running = [  # type: ignore[attr-defined]
            [4, "33333333-3333-4333-8333-333333333333", {"5": {"class_type": "MiniMaxH3Director"}}, {"client_id": "stale"}, ["5"]]
        ]
        with mock.patch.object(_RUNTIME, "_observe_progress_events") as observer:
            result = get_progress(JOB_ID, observe_seconds=0.1, port=self.port)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "running_prompt_binding_unavailable")
        observer.assert_not_called()

    def test_cancel_prompt_is_targeted_and_rejects_non_uuid(self) -> None:
        result = cancel_prompt(JOB_ID, port=self.port)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["prompt_id"], JOB_ID)
        self.assertEqual(self.server.last_cancel_prompt_id, JOB_ID)  # type: ignore[attr-defined]
        with self.assertRaises(ToolError) as raised:
            cancel_prompt("not-a-uuid", port=self.port)
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.server.global_interrupt_requests, 0)  # type: ignore[attr-defined]

    def test_node_info_models_and_features_are_bounded_read_only_helpers(self) -> None:
        self.server.object_info = {  # type: ignore[attr-defined]
            "SaveVideo": {"name": "SaveVideo", "input": {"required": {"codec": [["auto", "h264"], {}]}}}
        }
        node = get_node_info("SaveVideo", port=self.port)
        self.assertEqual(node["class_name"], "SaveVideo")
        self.assertEqual(node["info"]["name"], "SaveVideo")
        folders = list_models(port=self.port)
        self.assertEqual(folders["folders"], ["checkpoints", "vae", "diffusion_models"])
        models = list_models(folder="checkpoints", contains="alpha", max_items=1, port=self.port)
        self.assertEqual(models["models"], ["alpha.safetensors"])
        self.assertTrue(models["truncated"])
        features = get_features(port=self.port)
        self.assertTrue(features["features"]["jobs_api"])

    def test_production_profile_preflight_auto_accepts_formal_141f_path_without_submitting(self) -> None:
        workflow = self._safe_minimax_profile_workflow(frames=141)
        (self.root / "profile-141.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = preflight_workflow(self.workspace, "profile-141.json", port=self.port)

        self.assertTrue(result["ready"])
        profile = result["production_profile"]
        self.assertEqual(profile["requested"], "auto")
        self.assertEqual(profile["resolved"], "minimax_h3_v2v_16gb_1344x768")
        self.assertTrue(profile["auto_applied"])
        self.assertEqual(profile["conservative_baseline_frames"], 124)
        self.assertEqual(profile["recommended_max_frames"], 158)
        self.assertEqual(profile["segments"][0]["frames"], 141)
        self.assertEqual(profile["segments"][0]["packed_video_rows"], 84672)
        self.assertEqual(profile["model_chain"]["chunk_feed_forward"]["chunks"], 2)
        self.assertEqual(profile["model_chain"]["low_vram_attention"]["head_chunks"], 4)
        self.assertTrue(profile["guard_long_v2v_segments"]["live_default"])
        self.assertTrue(profile["runtime"]["fast_disk"])
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_production_profile_auto_rejects_260f_before_prompt_submission(self) -> None:
        workflow = self._safe_minimax_profile_workflow(frames=260)
        (self.root / "profile-260.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            run_workflow(
                self.workspace,
                "profile-260.json",
                timeout_seconds=5,
                port=self.port,
                include_image_data=False,
            )

        self.assertEqual(raised.exception.code, "COMFYUI_PRODUCTION_PROFILE_REJECTED")
        self.assertEqual(raised.exception.details["frames"], 260)
        self.assertEqual(raised.exception.details["packed_video_rows"], 155232)
        self.assertEqual(raised.exception.details["recommended_max_frames"], 158)
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_production_profile_rejects_wrong_2x4_chain_before_submission(self) -> None:
        workflow = self._safe_minimax_profile_workflow(frames=124, chunks=4)
        (self.root / "profile-wrong-chain.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            preflight_workflow(self.workspace, "profile-wrong-chain.json", port=self.port)

        self.assertEqual(raised.exception.code, "COMFYUI_PRODUCTION_PROFILE_REJECTED")
        self.assertIn("chunks=2", raised.exception.message)
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_production_profile_requires_fast_disk_before_submission(self) -> None:
        self.server.system_stats["system"]["argv"] = [str(self.comfy_root / "main.py")]  # type: ignore[index]
        workflow = self._safe_minimax_profile_workflow(frames=124)
        (self.root / "profile-no-fast-disk.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            preflight_workflow(self.workspace, "profile-no-fast-disk.json", port=self.port)

        self.assertEqual(raised.exception.code, "COMFYUI_PRODUCTION_PROFILE_REJECTED")
        self.assertFalse(raised.exception.details["fast_disk"])
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_production_profile_requires_live_director_guard_default_true(self) -> None:
        self.server.object_info["MiniMaxH3Director"]["input"]["optional"]["guard_long_v2v_segments"][1]["default"] = False  # type: ignore[index]
        workflow = self._safe_minimax_profile_workflow(frames=124)
        (self.root / "profile-guard-false.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            preflight_workflow(self.workspace, "profile-guard-false.json", port=self.port)

        self.assertEqual(raised.exception.code, "COMFYUI_PRODUCTION_PROFILE_REJECTED")
        self.assertIn("default must be true", raised.exception.message)
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_production_profile_none_is_explicit_expert_opt_out(self) -> None:
        workflow = self._safe_minimax_profile_workflow(frames=260, chunks=8, head_chunks=16, guard=False)
        (self.root / "profile-opt-out.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(
            self.workspace,
            "profile-opt-out.json",
            production_profile="none",
            timeout_seconds=5,
            port=self.port,
            include_image_data=False,
        )

        self.assertEqual(result["prompt_id"], "prompt-test")
        self.assertEqual(result["production_profile"]["requested"], "none")
        self.assertIsNone(result["production_profile"]["resolved"])
        self.assertEqual(result["production_profile"]["reason"], "explicitly_disabled")
        self.assertEqual(self.server.prompt_requests, 1)  # type: ignore[attr-defined]

    def test_run_workflow_retries_transient_history_transport_failure_without_resubmitting(self) -> None:
        self.server.history_failures_remaining = 1  # type: ignore[attr-defined]
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "history-retry.json").write_text(json.dumps(workflow), encoding="utf-8")
        result = run_workflow(self.workspace, "history-retry.json", timeout_seconds=5, port=self.port, include_image_data=False)
        self.assertEqual(result["prompt_id"], "prompt-test")
        self.assertEqual(result["history_transport_failures"], 1)
        self.assertEqual(self.server.prompt_requests, 1)  # type: ignore[attr-defined]
        self.assertGreaterEqual(self.server.history_requests, 2)  # type: ignore[attr-defined]

    def test_run_workflow_applies_overrides_returns_and_saves_image(self) -> None:
        workflow = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old prompt"}},
            "9": {"class_type": "SaveImage", "inputs": {}},
        }
        (self.root / "workflow.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(
            self.workspace,
            "workflow.json",
            overrides={"1": {"text": "new prompt"}},
            save_directory="generated",
            timeout_seconds=5,
            port=self.port,
        )

        self.assertEqual(result["prompt_id"], "prompt-test")
        self.assertEqual(result["images_returned"], 1)
        self.assertEqual(result["images"][0]["mime_type"], "image/png")
        saved = self.root / result["images"][0]["saved_path"]
        self.assertEqual(saved.read_bytes(), PNG_1X1)
        content = result["_content"]
        self.assertEqual(content[1]["type"], "image")
        submitted = self.server.last_prompt  # type: ignore[attr-defined]
        self.assertEqual(submitted["prompt"]["1"]["inputs"]["text"], "new prompt")

    def test_path_only_mode_does_not_fetch_image_binary(self) -> None:
        image_path = self.comfy_root / "output" / "result.png"
        image_path.write_bytes(PNG_1X1)
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "path-only.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(
            self.workspace,
            "path-only.json",
            timeout_seconds=5,
            port=self.port,
            include_image_data=False,
        )

        self.assertEqual(result["images_found"], 1)
        self.assertEqual(result["images_returned"], 0)
        self.assertEqual(self.server.view_requests, 0)  # type: ignore[attr-defined]
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["kind"], "image")
        self.assertEqual(artifact["path"], str(image_path))
        self.assertEqual(artifact["workspace_path"], "ComfyUI/output/result.png")
        self.assertEqual(len(result["_content"]), 1)

    def test_path_only_mode_preserves_explicit_image_save_directory_without_inline_data(self) -> None:
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "path-only-save.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(
            self.workspace,
            "path-only-save.json",
            save_directory="generated",
            timeout_seconds=5,
            port=self.port,
            include_image_data=False,
        )

        self.assertEqual(self.server.view_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(result["images_returned"], 1)
        saved_path = self.root / result["images"][0]["saved_path"]
        self.assertEqual(saved_path.read_bytes(), PNG_1X1)
        self.assertEqual(len(result["_content"]), 1)

    def test_video_output_returns_local_path_without_fetching_binary(self) -> None:
        video_dir = self.comfy_root / "output" / "video"
        video_dir.mkdir(parents=True)
        video_path = video_dir / "result.mp4"
        video_path.write_bytes(b"fake-mp4")
        self.server.history_outputs = {  # type: ignore[attr-defined]
            "9": {"images": [{"filename": "result.mp4", "subfolder": "video", "type": "output"}]}
        }
        workflow = {"9": {"class_type": "SaveVideo", "inputs": {}}}
        (self.root / "video.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(self.workspace, "video.json", timeout_seconds=5, port=self.port)

        self.assertEqual(result["images_returned"], 0)
        self.assertEqual(result["artifacts_found"], 1)
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["kind"], "video")
        self.assertEqual(artifact["path"], str(video_path))
        self.assertEqual(artifact["workspace_path"], "ComfyUI/output/video/result.mp4")
        self.assertEqual(artifact["size"], len(b"fake-mp4"))
        self.assertEqual(self.server.view_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(result["_content"][0]["type"], "text")
        self.assertEqual(len(result["_content"]), 1)

    def test_relative_comfyui_main_path_does_not_fabricate_absolute_artifact_path(self) -> None:
        self.server.system_stats = {"system": {"os": "test", "argv": ["main.py"]}}  # type: ignore[attr-defined]
        self.server.history_outputs = {  # type: ignore[attr-defined]
            "9": {"images": [{"filename": "result.mp4", "subfolder": "video", "type": "output"}]}
        }
        workflow = {"9": {"class_type": "SaveVideo", "inputs": {}}}
        (self.root / "relative-main.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(self.workspace, "relative-main.json", timeout_seconds=5, port=self.port)

        artifact = result["artifacts"][0]
        self.assertEqual(artifact["comfyui_reference"], "output/video/result.mp4")
        self.assertIsNone(artifact["path"])
        self.assertIsNone(artifact["workspace_path"])
        self.assertIsNone(artifact["size"])
        self.assertEqual(self.server.view_requests, 0)  # type: ignore[attr-defined]

    def test_artifact_metadata_is_bounded_for_large_output_sets(self) -> None:
        self.server.history_outputs = {  # type: ignore[attr-defined]
            "9": {
                "images": [
                    {"filename": f"result-{index:03d}.mp4", "subfolder": "video", "type": "output"}
                    for index in range(80)
                ]
            }
        }
        workflow = {"9": {"class_type": "SaveVideo", "inputs": {}}}
        (self.root / "many-videos.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(self.workspace, "many-videos.json", timeout_seconds=5, port=self.port)

        self.assertEqual(result["artifacts_found"], 80)
        self.assertEqual(result["artifacts_returned"], 64)
        self.assertTrue(result["artifacts_truncated"])
        self.assertEqual(len(result["artifacts"]), 64)
        self.assertEqual(self.server.view_requests, 0)  # type: ignore[attr-defined]

    def test_dynamic_combo_string_option_is_accepted_and_submitted(self) -> None:
        self.server.object_info = {  # type: ignore[attr-defined]
            "SaveVideo": {
                "input": {
                    "required": {
                        "video": ["VIDEO", {}],
                        "codec": [
                            "COMFY_DYNAMICCOMBO_V3",
                            {"options": [{"key": "auto", "inputs": {"required": {}}}, {"key": "h264", "inputs": {"required": {}}}]},
                        ],
                    }
                }
            }
        }
        workflow = {
            "16": {
                "class_type": "SaveVideo",
                "inputs": {"video": ["15", 0], "filename_prefix": "video/test", "format": "mp4", "codec": "auto"},
            }
        }
        (self.root / "good-dynamic.json").write_text(json.dumps(workflow), encoding="utf-8")

        result = run_workflow(self.workspace, "good-dynamic.json", timeout_seconds=5, port=self.port, include_image_data=False)

        self.assertEqual(result["prompt_id"], "prompt-test")
        self.assertEqual(self.server.prompt_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.last_prompt["prompt"]["16"]["inputs"]["codec"], "auto")  # type: ignore[attr-defined]

    def test_dynamic_combo_object_shape_is_rejected_before_prompt_submission(self) -> None:
        self.server.object_info = {  # type: ignore[attr-defined]
            "SaveVideo": {
                "input": {
                    "required": {
                        "video": ["VIDEO", {}],
                        "codec": [
                            "COMFY_DYNAMICCOMBO_V3",
                            {"options": [{"key": "auto", "inputs": {"required": {}}}, {"key": "h264", "inputs": {"required": {}}}]},
                        ],
                    }
                }
            }
        }
        workflow = {
            "16": {
                "class_type": "SaveVideo",
                "inputs": {"video": ["15", 0], "filename_prefix": "video/test", "format": "mp4", "codec": {"codec": "auto"}},
            }
        }
        (self.root / "bad-dynamic.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            run_workflow(self.workspace, "bad-dynamic.json", timeout_seconds=5, port=self.port)

        self.assertEqual(raised.exception.code, "INVALID_COMFYUI_WORKFLOW")
        self.assertEqual(raised.exception.details["node_id"], "16")
        self.assertEqual(raised.exception.details["input_name"], "codec")
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]

    def test_preexisting_cancel_token_prevents_prompt_submission(self) -> None:
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "pre-cancel.json").write_text(json.dumps(workflow), encoding="utf-8")
        cancel_path = self.root / "pre-cancel.flag"
        cancel_path.write_text("cancel\n", encoding="ascii")

        with self.assertRaises(ToolError) as raised:
            run_workflow(
                self.workspace,
                "pre-cancel.json",
                timeout_seconds=30,
                port=self.port,
                cancel_token_path=str(cancel_path),
            )

        self.assertEqual(raised.exception.code, "COMFYUI_CANCELLED")
        self.assertEqual(self.server.prompt_requests, 0)  # type: ignore[attr-defined]
        self.assertEqual(self.server.targeted_cancel_requests, 0)  # type: ignore[attr-defined]

    def test_cancel_token_targets_only_the_submitted_comfyui_prompt(self) -> None:
        self.server.history_complete = False  # type: ignore[attr-defined]
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "cancel.json").write_text(json.dumps(workflow), encoding="utf-8")
        cancel_path = self.root / "cancel.flag"
        errors: list[ToolError] = []

        def run() -> None:
            try:
                run_workflow(
                    self.workspace,
                    "cancel.json",
                    timeout_seconds=30,
                    port=self.port,
                    cancel_token_path=str(cancel_path),
                )
            except ToolError as exc:
                errors.append(exc)

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        for _ in range(100):
            if self.server.prompt_requests:  # type: ignore[attr-defined]
                break
            threading.Event().wait(0.01)
        cancel_path.write_text("cancel\n", encoding="ascii")
        runner.join(timeout=3)

        self.assertFalse(runner.is_alive())
        self.assertEqual([error.code for error in errors], ["COMFYUI_CANCELLED"])
        self.assertEqual(self.server.targeted_cancel_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.global_interrupt_requests, 0)  # type: ignore[attr-defined]

    def test_cancel_watcher_dispatches_while_history_request_is_blocked(self) -> None:
        self.server.history_complete = False  # type: ignore[attr-defined]
        self.server.history_gate = threading.Event()  # type: ignore[attr-defined]
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "blocked-history-cancel.json").write_text(json.dumps(workflow), encoding="utf-8")
        cancel_path = self.root / "blocked-cancel.flag"
        errors: list[ToolError] = []

        def run() -> None:
            try:
                run_workflow(
                    self.workspace,
                    "blocked-history-cancel.json",
                    timeout_seconds=30,
                    port=self.port,
                    cancel_token_path=str(cancel_path),
                )
            except ToolError as exc:
                errors.append(exc)

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        self.assertTrue(self.server.history_entered.wait(timeout=2))  # type: ignore[attr-defined]
        cancel_path.write_text("cancel\n", encoding="ascii")
        for _ in range(100):
            if self.server.targeted_cancel_requests:  # type: ignore[attr-defined]
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.server.targeted_cancel_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.global_interrupt_requests, 0)  # type: ignore[attr-defined]
        self.server.history_gate.set()  # type: ignore[attr-defined]
        runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        self.assertEqual([error.code for error in errors], ["COMFYUI_CANCELLED"])

    def test_missing_targeted_cancel_endpoint_never_falls_back_to_global_interrupt(self) -> None:
        self.server.history_complete = False  # type: ignore[attr-defined]
        self.server.targeted_cancel_available = False  # type: ignore[attr-defined]
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "cancel-no-endpoint.json").write_text(json.dumps(workflow), encoding="utf-8")
        cancel_path = self.root / "cancel-no-endpoint.flag"
        errors: list[ToolError] = []

        def run() -> None:
            try:
                run_workflow(
                    self.workspace,
                    "cancel-no-endpoint.json",
                    timeout_seconds=30,
                    port=self.port,
                    cancel_token_path=str(cancel_path),
                )
            except ToolError as exc:
                errors.append(exc)

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        for _ in range(100):
            if self.server.prompt_requests:  # type: ignore[attr-defined]
                break
            threading.Event().wait(0.01)
        cancel_path.write_text("cancel\n", encoding="ascii")
        runner.join(timeout=3)

        self.assertFalse(runner.is_alive())
        self.assertEqual([error.code for error in errors], ["COMFYUI_CANCELLED"])
        self.assertFalse(errors[0].details["cancel_dispatched"])
        self.assertEqual(self.server.targeted_cancel_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.global_interrupt_requests, 0)  # type: ignore[attr-defined]

    def test_workflow_timeout_targets_only_the_submitted_comfyui_prompt(self) -> None:
        self.server.history_complete = False  # type: ignore[attr-defined]
        workflow = {"9": {"class_type": "SaveImage", "inputs": {}}}
        (self.root / "timeout.json").write_text(json.dumps(workflow), encoding="utf-8")

        with self.assertRaises(ToolError) as raised:
            run_workflow(self.workspace, "timeout.json", timeout_seconds=1, port=self.port)

        self.assertEqual(raised.exception.code, "COMFYUI_TIMEOUT")
        self.assertEqual(self.server.targeted_cancel_requests, 1)  # type: ignore[attr-defined]
        self.assertEqual(self.server.global_interrupt_requests, 0)  # type: ignore[attr-defined]

    def test_ui_format_workflow_without_class_type_is_rejected(self) -> None:
        (self.root / "bad.json").write_text('{"nodes": []}', encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            run_workflow(self.workspace, "bad.json", timeout_seconds=1, port=self.port)
        self.assertEqual(raised.exception.code, "INVALID_COMFYUI_WORKFLOW")


class ComfyUiExternalContractTests(unittest.TestCase):
    def test_manifest_is_external_hot_load_contract(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "comfyui")
        self.assertEqual(manifest["version"], "1.6.0")
        self.assertEqual(manifest["actions"]["status"]["authorization"], "global")
        self.assertEqual(
            set(manifest["actions"]),
            {"status", "free", "jobs", "job-status", "progress", "health-check", "cancel-prompt", "node-info", "models", "features", "preflight", "run"},
        )
        self.assertLessEqual(len(manifest["actions"]), 64)
        self.assertTrue(manifest["actions"]["progress"]["read_only"])
        self.assertEqual(manifest["actions"]["progress"]["effect_contract"]["effect_semantics"], "read_only")
        health = manifest["actions"]["health-check"]
        self.assertTrue(health["read_only"])
        self.assertEqual(health["effect_contract"]["effect_semantics"], "read_only")
        self.assertEqual(health["timeout_seconds"], 25)
        self.assertEqual(set(health["input_schema"]["properties"]), {"prompt_id"})
        self.assertEqual(health["input_schema"]["required"], ["prompt_id"])
        preflight = manifest["actions"]["preflight"]
        self.assertTrue(preflight["read_only"])
        self.assertTrue(preflight["requires_workspace"])
        self.assertEqual(preflight["mutation_scope"], {"mode": "none"})
        self.assertEqual(preflight["input_schema"]["required"], ["workflow_path"])
        self.assertEqual(
            preflight["input_schema"]["properties"]["production_profile"]["enum"],
            ["auto", "none", "minimax_h3_v2v_16gb_1344x768"],
        )
        self.assertEqual(preflight["input_schema"]["properties"]["production_profile"]["default"], "auto")
        for action_name in ("free", "jobs", "job-status", "progress", "health-check", "cancel-prompt", "node-info", "models", "features", "preflight"):
            self.assertEqual(manifest["actions"][action_name]["authorization"], "global")
            self.assertEqual(manifest["actions"][action_name]["mutation_scope"], {"mode": "none"})
        run = manifest["actions"]["run"]
        self.assertEqual(run["run_mode"], "job")
        self.assertEqual(run["timeout_seconds"], 0)
        self.assertEqual(run["input_schema"]["properties"]["production_profile"]["default"], "auto")
        self.assertEqual(
            run["mutation_scope"],
            {"mode": "paths", "claims": [{"param": "save_directory", "kind": "tree", "optional": True}]},
        )

    def test_runtime_uses_only_public_folderbridge_error_api(self) -> None:
        source = PLUGIN_RUNTIME.read_text(encoding="utf-8")
        self.assertIn("from folderbridge_mcp.extension_api import ExtensionError", source)
        self.assertNotIn("folderbridge_mcp.comfyui", source)
        self.assertNotIn("folderbridge_mcp.security", source)
        self.assertNotIn('\"/interrupt\"', source)

    def test_workspace_view_rejects_traversal_sensitive_and_protected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _RUNTIME.WorkspaceView(root)
            for raw, for_write, code in (
                ("../escape.json", False, "PATH_OUTSIDE_WORKSPACE"),
                (".env", False, "SENSITIVE_PATH"),
                (".folderbridge.json", True, "PROTECTED_CONFIG"),
            ):
                with self.subTest(raw=raw), self.assertRaises(ToolError) as raised:
                    workspace.resolve(raw, for_write=for_write)
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
