from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from folderbridge_mcp.managed_services import (
    ComfyUIServiceConfig,
    ComfyUIServiceController,
    ManagedServiceError,
    ManagedServiceManager,
    detect_comfyui_install,
    load_comfyui_service_config,
    save_comfyui_service_config,
)


class FakeProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None
        self.wait_calls: list[float | None] = []
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise AssertionError("process must be terminated before wait")
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class ManagedServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config_path = self.base / "state" / "launcher-service.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _portable(self) -> Path:
        root = self.base / "portable"
        (root / "python_embeded").mkdir(parents=True)
        (root / "ComfyUI").mkdir()
        (root / "python_embeded" / "python.exe").write_bytes(b"")
        (root / "ComfyUI" / "main.py").write_text("# comfy\n", encoding="utf-8")
        return root

    def _source(self, venv_name: str) -> Path:
        root = self.base / f"source-{venv_name.replace('.', 'dot')}"
        (root / venv_name / "Scripts").mkdir(parents=True)
        (root / "main.py").write_text("# comfy\n", encoding="utf-8")
        (root / venv_name / "Scripts" / "python.exe").write_bytes(b"")
        return root

    def test_detects_portable_install(self) -> None:
        root = self._portable()
        install = detect_comfyui_install(root)
        self.assertEqual(install.mode, "portable")
        self.assertEqual(install.working_directory, root / "ComfyUI")
        self.assertEqual(install.argv()[0], str(root / "python_embeded" / "python.exe"))
        self.assertIn("--windows-standalone-build", install.argv())
        self.assertIn("--disable-auto-launch", install.argv())
        self.assertEqual(install.argv()[-4:], ["--listen", "127.0.0.1", "--port", "8188"])

    def test_detects_source_dot_venv_and_venv(self) -> None:
        dot = detect_comfyui_install(self._source(".venv"))
        plain = detect_comfyui_install(self._source("venv"))
        self.assertEqual(dot.mode, "source-.venv")
        self.assertEqual(plain.mode, "source-venv")
        self.assertNotIn("--windows-standalone-build", dot.argv())
        self.assertNotIn("--windows-standalone-build", plain.argv())

    def test_invalid_directory_and_bat_only_are_rejected(self) -> None:
        invalid = self.base / "invalid"
        invalid.mkdir()
        (invalid / "run_nvidia_gpu.bat").write_text("echo no\n", encoding="utf-8")
        with self.assertRaises(ManagedServiceError):
            detect_comfyui_install(invalid)

    def test_config_round_trip_does_not_persist_pid_or_command(self) -> None:
        root = self._portable()
        save_comfyui_service_config(
            ComfyUIServiceConfig(install_root=str(root), auto_start=True),
            self.config_path,
        )
        loaded = load_comfyui_service_config(self.config_path)
        self.assertEqual(loaded.install_root, str(root))
        self.assertTrue(loaded.auto_start)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"version", "install_root", "auto_start"})
        self.assertNotIn("pid", raw)
        self.assertNotIn("command", raw)
        serialized = self.config_path.read_text(encoding="utf-8").lower()
        self.assertNotIn(".bat", serialized)
        self.assertNotIn(".cmd", serialized)

    def test_online_service_is_external_and_start_does_not_spawn(self) -> None:
        spawned = []
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": True, "endpoint": "http://127.0.0.1:8188"},
            popen_factory=lambda *args, **kwargs: spawned.append((args, kwargs)),
        )
        state = controller.start()
        self.assertTrue(state["online"])
        self.assertTrue(state["external"])
        self.assertFalse(state["owned"])
        self.assertEqual(state["reason"], "already-online")
        self.assertEqual(spawned, [])

    def test_external_service_stop_is_noop(self) -> None:
        terminated = []
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": True},
            terminate_process=lambda process: terminated.append(process),
        )
        state = controller.stop()
        self.assertFalse(state["stopped"])
        self.assertEqual(state["reason"], "not-owned")
        self.assertEqual(terminated, [])

    def test_owned_start_uses_explicit_python_main_and_shell_false(self) -> None:
        root = self._portable()
        save_comfyui_service_config(ComfyUIServiceConfig(str(root), True), self.config_path)
        probes = iter((
            {"online": False},
            {"online": True, "endpoint": "http://127.0.0.1:8188"},
        ))
        process = FakeProcess()
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: next(probes),
            popen_factory=popen,
        )
        state = controller.start(ready_timeout_seconds=1)
        self.assertTrue(state["started"])
        self.assertTrue(state["owned"])
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], str(root / "python_embeded" / "python.exe"))
        self.assertEqual(argv[2], str(root / "ComfyUI" / "main.py"))
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], __import__("subprocess").STDOUT)
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertIn("--disable-auto-launch", argv)
        self.assertFalse(any(str(value).lower().endswith((".bat", ".cmd")) for value in argv))

    def test_premature_exit_reports_persistent_startup_log(self) -> None:
        root = self._portable()
        save_comfyui_service_config(ComfyUIServiceConfig(str(root), True), self.config_path)
        process = FakeProcess()
        process.returncode = 7
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": False},
            popen_factory=lambda *args, **kwargs: process,
        )
        with self.assertRaisesRegex(ManagedServiceError, r"退出码 7.*launcher-comfyui\.log"):
            controller.start(ready_timeout_seconds=1)
        self.assertTrue((self.config_path.parent / "launcher-comfyui.log").is_file())

    def test_start_survives_process_reference_cleared_during_poll(self) -> None:
        root = self._portable()
        save_comfyui_service_config(ComfyUIServiceConfig(str(root), True), self.config_path)
        holder: dict[str, ComfyUIServiceController] = {}

        class RacingProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self._cleared = False

            def poll(self):
                if not self._cleared and "controller" in holder:
                    controller = holder["controller"]
                    if controller.process is self:
                        self._cleared = True
                        self.returncode = 0
                        controller._process = None
                return self.returncode

        process = RacingProcess()
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": False},
            popen_factory=lambda *args, **kwargs: process,
        )
        holder["controller"] = controller

        with self.assertRaises(ManagedServiceError) as raised:
            controller.start(ready_timeout_seconds=1)
        self.assertIn("退出码 0", str(raised.exception))
        self.assertIsNone(controller.process)

    def test_owned_stop_uses_saved_process_handle(self) -> None:
        process = FakeProcess()
        terminated = []
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": False},
            terminate_process=lambda owned: (terminated.append(owned), setattr(owned, "returncode", 0)),
        )
        controller._process = process
        state = controller.stop(wait_port_seconds=0)
        self.assertTrue(state["stopped"])
        self.assertEqual(terminated, [process])
        self.assertIsNone(controller.process)

    def test_port_still_online_after_owned_stop_only_warns(self) -> None:
        process = FakeProcess()
        clock = FakeClock()
        terminated = []
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": True},
            terminate_process=lambda owned: (terminated.append(owned), setattr(owned, "returncode", 0)),
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        )
        controller._process = process
        state = controller.stop(wait_port_seconds=0.5)
        self.assertTrue(state["stopped"])
        self.assertTrue(state["online"])
        self.assertTrue(state["external"])
        self.assertIn("不会终止", state["warning"])
        self.assertEqual(terminated, [process])

    def test_auto_start_without_path_requests_path_instead_of_spawning(self) -> None:
        spawned = []
        controller = ComfyUIServiceController(
            config_path=self.config_path,
            status_probe=lambda: {"online": False},
            popen_factory=lambda *args, **kwargs: spawned.append((args, kwargs)),
        )
        state = controller.ensure_auto_started()
        self.assertEqual(state["reason"], "path-required")
        self.assertEqual(spawned, [])

    def test_manager_shutdown_follows_loaded_extension_order(self) -> None:
        order: list[str] = []

        class Controller:
            def __init__(self, extension_id: str) -> None:
                self.extension_id = extension_id

            def stop(self):
                order.append(self.extension_id)
                return {"service_id": self.extension_id, "stopped": True}

        manager = ManagedServiceManager((Controller("alpha"), Controller("beta")))  # type: ignore[arg-type]
        results = manager.shutdown(("beta", "missing", "alpha"))
        self.assertEqual(order, ["beta", "alpha"])
        self.assertEqual([result["service_id"] for result in results], ["beta", "alpha"])

    def test_default_termination_never_discovers_pid_by_port(self) -> None:
        package = Path(__file__).resolve().parents[1] / "folderbridge_mcp"
        managed_text = (package / "managed_services.py").read_text(encoding="utf-8").lower()
        process_text = (package / "process_control.py").read_text(encoding="utf-8").lower()
        combined = managed_text + "\n" + process_text
        self.assertNotIn("netstat", combined)
        self.assertNotIn("get-nettcpconnection", combined)
        self.assertNotIn("localport", combined)
        self.assertIn("terminate_owned_process_tree", managed_text)
        self.assertIn("systemroot", process_text)
        self.assertIn("system32", process_text)
        self.assertIn("taskkill.exe", process_text)


if __name__ == "__main__":
    unittest.main()
