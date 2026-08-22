from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .comfyui import COMFYUI_HOST, COMFYUI_PORT, comfyui_status
from .extensions import extension_state_root


SERVICE_CONFIG_VERSION = 1
COMFYUI_SERVICE_ID = "comfyui"
COMFYUI_READY_TIMEOUT_SECONDS = 30
COMFYUI_STOP_TIMEOUT_SECONDS = 12


class ManagedServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComfyUIInstall:
    install_root: Path
    python_executable: Path
    main_py: Path
    mode: str
    working_directory: Path

    def argv(self) -> list[str]:
        argv = [str(self.python_executable), "-s", str(self.main_py)]
        if self.mode == "portable":
            argv.append("--windows-standalone-build")
        argv.extend(("--listen", COMFYUI_HOST, "--port", str(COMFYUI_PORT)))
        return argv


@dataclass(frozen=True)
class ComfyUIServiceConfig:
    install_root: str = ""
    auto_start: bool = True
    version: int = SERVICE_CONFIG_VERSION


def comfyui_service_config_path() -> Path:
    return extension_state_root() / COMFYUI_SERVICE_ID / "launcher-service.json"


def detect_comfyui_install(raw_root: str | os.PathLike[str]) -> ComfyUIInstall:
    root = Path(raw_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ManagedServiceError("ComfyUI 安装路径必须是文件夹。")

    portable_python = root / "python_embeded" / "python.exe"
    portable_main = root / "ComfyUI" / "main.py"
    if portable_python.is_file() and portable_main.is_file():
        return ComfyUIInstall(root, portable_python, portable_main, "portable", portable_main.parent)

    source_main = root / "main.py"
    if source_main.is_file():
        for venv_name in (".venv", "venv"):
            python_executable = root / venv_name / "Scripts" / "python.exe"
            if python_executable.is_file():
                return ComfyUIInstall(root, python_executable, source_main, f"source-{venv_name}", root)

    raise ManagedServiceError(
        "未识别为受支持的 ComfyUI 安装目录。请选择包含 python_embeded\\python.exe + ComfyUI\\main.py 的 Portable 根目录，"
        "或包含 main.py + .venv/venv\\Scripts\\python.exe 的源码安装根目录。"
    )


def load_comfyui_service_config(path: Path | None = None) -> ComfyUIServiceConfig:
    config_path = path or comfyui_service_config_path()
    try:
        data = config_path.read_bytes()
        parsed = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ComfyUIServiceConfig()
    if not isinstance(parsed, dict) or set(parsed).difference({"version", "install_root", "auto_start"}):
        return ComfyUIServiceConfig()
    version = parsed.get("version")
    install_root = parsed.get("install_root")
    auto_start = parsed.get("auto_start")
    if version != SERVICE_CONFIG_VERSION or not isinstance(install_root, str) or not isinstance(auto_start, bool):
        return ComfyUIServiceConfig()
    return ComfyUIServiceConfig(install_root=install_root, auto_start=auto_start)


def save_comfyui_service_config(config: ComfyUIServiceConfig, path: Path | None = None) -> None:
    config_path = path or comfyui_service_config_path()
    payload = json.dumps(
        {
            "version": SERVICE_CONFIG_VERSION,
            "install_root": config.install_root,
            "auto_start": config.auto_start,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


class ComfyUIServiceController:
    extension_id = COMFYUI_SERVICE_ID

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        status_probe: Callable[[], dict[str, Any]] | None = None,
        popen_factory: Callable[..., Any] | None = None,
        terminate_process: Callable[[Any], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config_path = config_path or comfyui_service_config_path()
        self._status_probe = status_probe or (lambda: comfyui_status(port=COMFYUI_PORT))
        self._popen_factory = popen_factory or subprocess.Popen
        self._terminate_process = terminate_process or _terminate_owned_process_tree
        self._sleep = sleeper
        self._monotonic = monotonic
        self._process: Any | None = None

    @property
    def process(self) -> Any | None:
        return self._process

    def config(self) -> ComfyUIServiceConfig:
        return load_comfyui_service_config(self.config_path)

    def configure_install(self, raw_root: str | os.PathLike[str], *, auto_start: bool = True) -> ComfyUIInstall:
        install = detect_comfyui_install(raw_root)
        save_comfyui_service_config(
            ComfyUIServiceConfig(install_root=str(install.install_root), auto_start=bool(auto_start)),
            self.config_path,
        )
        return install

    def set_auto_start(self, enabled: bool) -> ComfyUIServiceConfig:
        current = self.config()
        updated = ComfyUIServiceConfig(install_root=current.install_root, auto_start=bool(enabled))
        save_comfyui_service_config(updated, self.config_path)
        return updated

    def status(self) -> dict[str, Any]:
        process_alive = self._process is not None and self._process.poll() is None
        if self._process is not None and not process_alive:
            self._process = None
        probe = self._status_probe()
        online = bool(probe.get("online"))
        config = self.config()
        return {
            "service_id": COMFYUI_SERVICE_ID,
            "online": online,
            "owned": bool(process_alive),
            "external": bool(online and not process_alive),
            "process_running": bool(process_alive),
            "install_root": config.install_root,
            "auto_start": config.auto_start,
            "endpoint": probe.get("endpoint", f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"),
            "detail": probe.get("detail", ""),
        }

    def start(self, *, ready_timeout_seconds: float = COMFYUI_READY_TIMEOUT_SECONDS) -> dict[str, Any]:
        initial = self.status()
        if initial["online"]:
            return {**initial, "started": False, "reason": "already-online"}
        if self._process is not None and self._process.poll() is None:
            return {**initial, "started": False, "reason": "already-starting"}

        config = self.config()
        if not config.install_root:
            raise ManagedServiceError("尚未配置 ComfyUI 安装目录。")
        install = detect_comfyui_install(config.install_root)
        kwargs: dict[str, Any] = {
            "cwd": str(install.working_directory),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        self._process = self._popen_factory(install.argv(), **kwargs)

        deadline = self._monotonic() + max(0.1, float(ready_timeout_seconds))
        while self._monotonic() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                self._process = None
                raise ManagedServiceError(f"ComfyUI 启动进程提前退出（退出码 {code}）。")
            state = self.status()
            if state["online"]:
                return {**state, "started": True, "reason": "ready"}
            self._sleep(0.25)

        self.stop(wait_port_seconds=2.0)
        raise ManagedServiceError(f"ComfyUI 在 {ready_timeout_seconds:g} 秒内未就绪。")

    def ensure_auto_started(self) -> dict[str, Any]:
        state = self.status()
        if state["online"]:
            return {**state, "started": False, "reason": "already-online"}
        config = self.config()
        if not config.install_root:
            return {**state, "started": False, "reason": "path-required"}
        if not config.auto_start:
            return {**state, "started": False, "reason": "auto-start-disabled"}
        return self.start()

    def stop(self, *, wait_port_seconds: float = COMFYUI_STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            self._process = None
            state = self.status()
            return {**state, "stopped": False, "reason": "not-owned"}

        self._terminate_process(process)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._process = None

        deadline = self._monotonic() + max(0.0, float(wait_port_seconds))
        state = self.status()
        while state["online"] and self._monotonic() < deadline:
            self._sleep(0.25)
            state = self.status()
        warning = ""
        if state["online"]:
            warning = "8188 仍由其他进程占用，不属于 FolderBridge 托管进程，因此不会终止。"
        return {**state, "stopped": True, "reason": "owned-process-stopped", "warning": warning}


class ManagedServiceManager:
    def __init__(self, controllers: Iterable[ComfyUIServiceController] = ()) -> None:
        self._controllers = {controller.extension_id: controller for controller in controllers}

    def controller(self, extension_id: str) -> ComfyUIServiceController | None:
        return self._controllers.get(extension_id)

    def shutdown(self, loaded_extension_ids: Iterable[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for extension_id in loaded_extension_ids:
            controller = self.controller(extension_id)
            if controller is None:
                continue
            try:
                results.append(controller.stop())
            except Exception as exc:
                results.append(
                    {
                        "service_id": extension_id,
                        "stopped": False,
                        "reason": "stop-failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return results


def default_managed_service_manager() -> ManagedServiceManager:
    return ManagedServiceManager((ComfyUIServiceController(),))


def _terminate_owned_process_tree(process: Any) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
        completed = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=8,
        )
        if completed.returncode == 0:
            return
        process.terminate()
        return
    try:
        os.killpg(os.getpgid(process.pid), 15)
    except (AttributeError, OSError, ProcessLookupError):
        process.terminate()
