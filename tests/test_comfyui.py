from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from folderbridge_mcp.comfyui import comfyui_status, run_workflow
from folderbridge_mcp.security import ToolError, Workspace


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
        if parsed.path == "/history/prompt-test":
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
        if self.path != "/prompt":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
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
        self.workspace = Workspace(self.root)
        self.comfy_root = self.root / "ComfyUI"
        (self.comfy_root / "output").mkdir(parents=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeComfyHandler)
        self.server.system_stats = {"system": {"os": "test", "argv": [str(self.comfy_root / "main.py")]}}  # type: ignore[attr-defined]
        self.server.history_outputs = {  # type: ignore[attr-defined]
            "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
        }
        self.server.view_requests = 0  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_status_uses_loopback_comfyui(self) -> None:
        result = comfyui_status(port=self.port)
        self.assertTrue(result["online"])
        self.assertEqual(result["endpoint"], f"http://127.0.0.1:{self.port}")

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

    def test_ui_format_workflow_without_class_type_is_rejected(self) -> None:
        (self.root / "bad.json").write_text('{"nodes": []}', encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            run_workflow(self.workspace, "bad.json", timeout_seconds=1, port=self.port)
        self.assertEqual(raised.exception.code, "INVALID_COMFYUI_WORKFLOW")


if __name__ == "__main__":
    unittest.main()
