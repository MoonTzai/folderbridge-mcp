from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Callable, Iterable

from .extensions import ExtensionRegistry, extension_state_root
from .process_control import owned_process_group_kwargs, terminate_owned_process_tree


SERVICE_CONFIG_VERSION = 1
COMFYUI_SERVICE_ID = "comfyui"
COMFYUI_HOST = "127.0.0.1"
COMFYUI_PORT = 8188
COMFYUI_READY_TIMEOUT_SECONDS = 120
COMFYUI_STOP_TIMEOUT_SECONDS = 12
COMFYUI_STATUS_MAX_BYTES = 256 * 1024


def _comfyui_status_probe() -> dict[str, Any]:
    endpoint = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"
    connection = HTTPConnection(COMFYUI_HOST, COMFYUI_PORT, timeout=3)
    try:
        connection.request("GET", "/system_stats", headers={"Accept": "application/json"})
        response = connection.getresponse()
        data = response.read(COMFYUI_STATUS_MAX_BYTES + 1)
        if response.status != 200:
            return {"online": False, "endpoint": endpoint, "detail": f"ComfyUI returned HTTP {response.status}."}
        if len(data) > COMFYUI_STATUS_MAX_BYTES:
            return {"online": False, "endpoint": endpoint, "detail": "ComfyUI status response is too large."}
        try:
            stats = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"online": False, "endpoint": endpoint, "detail": "ComfyUI returned invalid JSON."}
        return {"online": True, "endpoint": endpoint, "system_stats": stats}
    except (OSError, TimeoutError) as exc:
        return {"online": False, "endpoint": endpoint, "detail": f"Cannot reach local ComfyUI: {exc}"}
    finally:
        connection.close()


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
        argv.append("--disable-auto-launch")
        argv.append("--fast-disk")
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
        self._status_probe = status_probe or _comfyui_status_probe
        self._popen_factory = popen_factory or subprocess.Popen
        self._terminate_process = terminate_process or (
            lambda process: terminate_owned_process_tree(process, force=os.name == "nt")
        )
        self._sleep = sleeper
        self._monotonic = monotonic
        self._process: Any | None = None

    @property
    def process(self) -> Any | None:
        return self._process

    def config(self) -> ComfyUIServiceConfig:
        return load_comfyui_service_config(self.config_path)

    def ui_spec(self) -> dict[str, Any]:
        return {
            "service_name": "ComfyUI",
            "requires_install_root": True,
            "select_directory": True,
            "show_auto_start": True,
            "supports_restart": False,
            "supports_open_status": False,
            "supports_open_login": False,
            "show_connection_fields": False,
            "show_parallel_control": False,
        }

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
        process = self._process
        process_alive = process is not None and process.poll() is None
        if process is not None and not process_alive and self._process is process:
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
        current_process = self._process
        if current_process is not None and current_process.poll() is None:
            return {**initial, "started": False, "reason": "already-starting"}

        config = self.config()
        if not config.install_root:
            raise ManagedServiceError("尚未配置 ComfyUI 安装目录。")
        install = detect_comfyui_install(config.install_root)
        log_path = self.config_path.parent / "launcher-comfyui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        kwargs: dict[str, Any] = {
            "cwd": str(install.working_directory),
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
            "env": environment,
        }
        kwargs.update(owned_process_group_kwargs())
        with log_path.open("ab", buffering=0) as log_handle:
            kwargs["stdout"] = log_handle
            process = self._popen_factory(install.argv(), **kwargs)
            self._process = process

        deadline = self._monotonic() + max(0.1, float(ready_timeout_seconds))
        while self._monotonic() < deadline:
            if process.poll() is not None:
                code = process.returncode
                if self._process is process:
                    self._process = None
                raise ManagedServiceError(
                    f"ComfyUI 启动进程提前退出（退出码 {code}）。启动日志：{log_path}"
                )
            state = self.status()
            if state["online"]:
                return {**state, "started": True, "reason": "ready"}
            self._sleep(0.25)

        self.stop(wait_port_seconds=2.0)
        raise ManagedServiceError(
            f"ComfyUI 在 {ready_timeout_seconds:g} 秒内未就绪。启动日志：{log_path}"
        )

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
        if self._process is process:
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


CHATGPT_WEB_LLM_SERVICE_ID = "chatgpt-web-llm-adapter"
CHATGPT_WEB_LLM_HOST = "127.0.0.1"
CHATGPT_WEB_LLM_API_PORT = 8769
CHATGPT_WEB_LLM_DEVTOOLS_PORT = 8770
CHATGPT_WEB_LLM_BASE_URL = f"http://{CHATGPT_WEB_LLM_HOST}:{CHATGPT_WEB_LLM_API_PORT}/v1"
CHATGPT_WEB_LLM_MODEL = "chatgpt-web-current"
CHATGPT_WEB_LLM_READY_TIMEOUT_SECONDS = 70
CHATGPT_WEB_LLM_STOP_TIMEOUT_SECONDS = 15
CHATGPT_WEB_LLM_STATUS_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class ChatGPTWebLLMServiceConfig:
    auto_start: bool = False
    max_parallel: int = 2
    response_timeout_seconds: int = 600
    version: int = SERVICE_CONFIG_VERSION


def chatgpt_web_llm_service_config_path() -> Path:
    return extension_state_root() / CHATGPT_WEB_LLM_SERVICE_ID / "launcher-service.json"


def chatgpt_web_llm_standalone_state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ChatGPT-Web-LLM-Test-Adapter" / "standalone-v1"
    return Path.home() / ".chatgpt-web-llm-test-adapter" / "standalone-v1"


def load_chatgpt_web_llm_service_config(path: Path | None = None) -> ChatGPTWebLLMServiceConfig:
    config_path = path or chatgpt_web_llm_service_config_path()
    try:
        parsed = json.loads(config_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ChatGPTWebLLMServiceConfig()
    if not isinstance(parsed, dict) or set(parsed).difference(
        {"version", "auto_start", "max_parallel", "response_timeout_seconds"}
    ):
        return ChatGPTWebLLMServiceConfig()
    version = parsed.get("version")
    auto_start = parsed.get("auto_start")
    max_parallel = parsed.get("max_parallel")
    response_timeout_seconds = parsed.get("response_timeout_seconds")
    if (
        version != SERVICE_CONFIG_VERSION
        or not isinstance(auto_start, bool)
        or not isinstance(max_parallel, int)
        or isinstance(max_parallel, bool)
        or not 1 <= max_parallel <= 4
        or not isinstance(response_timeout_seconds, int)
        or isinstance(response_timeout_seconds, bool)
        or not 30 <= response_timeout_seconds <= 1800
    ):
        return ChatGPTWebLLMServiceConfig()
    return ChatGPTWebLLMServiceConfig(
        auto_start=auto_start,
        max_parallel=max_parallel,
        response_timeout_seconds=response_timeout_seconds,
    )


def save_chatgpt_web_llm_service_config(config: ChatGPTWebLLMServiceConfig, path: Path | None = None) -> None:
    config_path = path or chatgpt_web_llm_service_config_path()
    payload = json.dumps(
        {
            "version": SERVICE_CONFIG_VERSION,
            "auto_start": config.auto_start,
            "max_parallel": config.max_parallel,
            "response_timeout_seconds": config.response_timeout_seconds,
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


class ChatGPTWebLLMServiceController:
    extension_id = CHATGPT_WEB_LLM_SERVICE_ID

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        config_path: Path | None = None,
        standalone_state_dir: Path | None = None,
        popen_factory: Callable[..., Any] | None = None,
        terminate_process: Callable[[Any], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.config_path = config_path or chatgpt_web_llm_service_config_path()
        self.state_dir = standalone_state_dir or chatgpt_web_llm_standalone_state_dir()
        self._popen_factory = popen_factory or subprocess.Popen
        self._terminate_process = terminate_process or (
            lambda process: terminate_owned_process_tree(process, force=os.name == "nt")
        )
        self._sleep = sleeper
        self._monotonic = monotonic
        self._process: Any | None = None
        self._last_error = ""

    @property
    def process(self) -> Any | None:
        return self._process

    def config(self) -> ChatGPTWebLLMServiceConfig:
        return load_chatgpt_web_llm_service_config(self.config_path)

    def ui_spec(self) -> dict[str, Any]:
        return {
            "service_name": "ChatGPT Web LLM Bridge",
            "requires_install_root": False,
            "select_directory": False,
            "show_auto_start": True,
            "supports_restart": True,
            "supports_open_status": True,
            "supports_open_login": True,
            "supports_probe": True,
            "show_connection_fields": True,
            "show_parallel_control": True,
        }

    def set_auto_start(self, enabled: bool) -> ChatGPTWebLLMServiceConfig:
        current = self.config()
        updated = ChatGPTWebLLMServiceConfig(
            auto_start=bool(enabled),
            max_parallel=current.max_parallel,
            response_timeout_seconds=current.response_timeout_seconds,
        )
        save_chatgpt_web_llm_service_config(updated, self.config_path)
        return updated

    def _installed_record(self, *, require_enabled: bool) -> tuple[Any, dict[str, Any]]:
        try:
            record = self.registry.get(self.extension_id)
        except Exception as exc:
            raise ManagedServiceError("ChatGPT Web LLM Adapter 尚未安装到当前用户 Extension 目录。") from exc
        if record.bundled:
            raise ManagedServiceError("ChatGPT Web LLM Adapter 必须来自外源热加载目录。")
        trust = self.registry.trust_store.status(record)
        if require_enabled and (not trust.get("trusted") or not trust.get("enabled")):
            if trust.get("approval_stale"):
                raise ManagedServiceError("Adapter 文件已变化；请先在 Extensions & Skills 重新核对 exact hash 并批准。")
            raise ManagedServiceError("Adapter 尚未按当前 exact hash 批准并启用。")
        script = record.path / "standalone.py"
        runtime = record.path / "browser_runtime.py"
        if not script.is_file() or script.is_symlink() or not runtime.is_file() or runtime.is_symlink():
            raise ManagedServiceError("已安装 Adapter 缺少受 hash 覆盖的 standalone.py / browser_runtime.py。")
        return record, trust

    def _installed_metadata(self) -> dict[str, Any]:
        try:
            record, trust = self._installed_record(require_enabled=False)
            return {
                "installed": True,
                "installed_version": record.manifest.version,
                "installed_sha256": record.sha256,
                "installed_path": str(record.path),
                "installed_trusted": bool(trust.get("trusted")),
                "installed_enabled": bool(trust.get("enabled")),
                "approval_stale": bool(trust.get("approval_stale")),
            }
        except ManagedServiceError as exc:
            return {
                "installed": False,
                "installed_version": "",
                "installed_sha256": "",
                "installed_path": "",
                "installed_trusted": False,
                "installed_enabled": False,
                "approval_stale": False,
                "install_error": str(exc),
            }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _http_json(
        method: str,
        path: str,
        *,
        token: str = "",
        body: dict[str, Any] | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        connection = HTTPConnection(CHATGPT_WEB_LLM_HOST, CHATGPT_WEB_LLM_API_PORT, timeout=timeout)
        payload = b""
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload if body is not None else None, headers=headers)
            response = connection.getresponse()
            raw = response.read(CHATGPT_WEB_LLM_STATUS_MAX_BYTES + 1)
            if len(raw) > CHATGPT_WEB_LLM_STATUS_MAX_BYTES:
                raise ManagedServiceError("Adapter 本地控制响应过大。")
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManagedServiceError(f"Adapter 本地控制返回无效 JSON（HTTP {response.status}）。") from exc
            if response.status < 200 or response.status >= 300:
                message = ""
                if isinstance(parsed, dict):
                    error = parsed.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "")
                raise ManagedServiceError(message or f"Adapter 本地控制失败（HTTP {response.status}）。")
            if not isinstance(parsed, dict):
                raise ManagedServiceError("Adapter 本地控制返回必须是 JSON object。")
            return parsed
        except (OSError, TimeoutError) as exc:
            raise ManagedServiceError(f"无法连接本地 Adapter：{exc}") from exc
        finally:
            connection.close()

    @classmethod
    def _health(cls) -> dict[str, Any] | None:
        try:
            value = cls._http_json("GET", "/health", timeout=2.0)
        except ManagedServiceError:
            return None
        if value.get("adapter") != CHATGPT_WEB_LLM_SERVICE_ID:
            return None
        return value

    @staticmethod
    def _port_available(port: int) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((CHATGPT_WEB_LLM_HOST, port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    @classmethod
    def _assert_start_ports_available(cls) -> None:
        busy = [port for port in (CHATGPT_WEB_LLM_API_PORT, CHATGPT_WEB_LLM_DEVTOOLS_PORT) if not cls._port_available(port)]
        if busy:
            joined = ", ".join(str(port) for port in busy)
            raise ManagedServiceError(
                f"本地端口 {joined} 已被未知/外部进程占用；FolderBridge 不会按端口强杀陌生 PID。请先处理占用后重试。"
            )

    @staticmethod
    def _python_prefix() -> list[str]:
        executable = Path(sys.executable)
        if not getattr(sys, "frozen", False) and executable.is_file() and executable.name.lower().startswith("python"):
            return [str(executable)]
        repo_python = Path(__file__).resolve().parents[1] / ".build-venv" / "Scripts" / "python.exe"
        if repo_python.is_file():
            return [str(repo_python)]
        py = shutil.which("py.exe") or shutil.which("py")
        if py:
            return [py, "-3"]
        python = shutil.which("python.exe") or shutil.which("python")
        if python:
            return [python]
        raise ManagedServiceError("未找到可用于启动已安装 Standalone Adapter 的 Python 3。")

    def _connection_record(self) -> dict[str, Any]:
        return self._read_json(self.state_dir / "connection.json")

    def _status_page(self) -> Path:
        return self.state_dir / "status.html"

    def status(self) -> dict[str, Any]:
        process = self._process
        process_alive = process is not None and process.poll() is None
        if process is not None and not process_alive and self._process is process:
            if process.returncode not in {None, 0} and not self._last_error:
                self._last_error = f"Standalone 进程已退出（退出码 {process.returncode}）。"
            self._process = None
        health = self._health()
        online = bool(health)
        metadata = self._installed_metadata()
        config = self.config()
        live_version = str((health or {}).get("adapter_version") or "")
        installed_version = str(metadata.get("installed_version") or "")
        version_match = bool(online and installed_version and live_version == installed_version)
        provider_state = str((health or {}).get("provider_state") or "")
        ready = bool(health and health.get("ready") and version_match)
        external = bool(online and not process_alive)
        connection = self._connection_record() if online else {}
        api_key = ""
        if version_match and connection.get("active") and isinstance(connection.get("api_key"), str):
            api_key = str(connection["api_key"])
        if online and not version_match:
            display_state = "ERROR"
            detail = (
                f"版本不一致：installed={installed_version or 'unknown'}，live={live_version or 'unknown'}。"
                "不会把旧进程标记为 READY。"
            )
        elif online and ready:
            display_state = "READY"
            detail = "ChatGPT 页面已就绪。"
        elif online:
            display_state = "WAITING_LOGIN"
            detail = f"ChatGPT 页面尚未就绪：{provider_state or 'waiting_login_or_challenge'}。"
        elif process_alive:
            display_state = "STARTING"
            detail = "Standalone 正在启动并等待本地端口就绪。"
        elif self._last_error:
            display_state = "ERROR"
            detail = self._last_error
        elif not metadata.get("installed"):
            display_state = "ERROR"
            detail = str(metadata.get("install_error") or "Adapter 未安装。")
        elif not metadata.get("installed_trusted") or not metadata.get("installed_enabled"):
            display_state = "ERROR"
            detail = "已安装 Adapter 尚未按当前 exact hash 批准并启用。"
        else:
            display_state = "OFFLINE"
            detail = "服务未运行。"
        return {
            "service_id": self.extension_id,
            "online": online,
            "ready": ready,
            "owned": bool(process_alive),
            "external": external,
            "process_running": bool(process_alive),
            "auto_start": config.auto_start,
            "max_parallel": int((health or {}).get("max_parallel") or config.max_parallel),
            "active_requests": int((health or {}).get("active_requests") or 0),
            "response_timeout_seconds": config.response_timeout_seconds,
            "base_url": CHATGPT_WEB_LLM_BASE_URL,
            "model": CHATGPT_WEB_LLM_MODEL,
            "api_key": api_key,
            "provider_state": provider_state,
            "display_state": display_state,
            "detail": detail,
            "live_version": live_version,
            "version_match": version_match,
            "status_page": str(self._status_page()),
            **metadata,
        }

    def start(self, *, ready_timeout_seconds: float = CHATGPT_WEB_LLM_READY_TIMEOUT_SECONDS) -> dict[str, Any]:
        initial = self.status()
        if initial["online"]:
            if not initial.get("version_match"):
                raise ManagedServiceError(str(initial.get("detail") or "已有不匹配的外部 Adapter 服务。"))
            return {**initial, "started": False, "reason": "already-online"}
        current_process = self._process
        if current_process is not None and current_process.poll() is None:
            return {**initial, "started": False, "reason": "already-starting"}
        record, _trust = self._installed_record(require_enabled=True)
        self._assert_start_ports_available()
        config = self.config()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        marker = self.state_dir / "stop.requested"
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        status_page = self._status_page()
        try:
            status_page.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ManagedServiceError(
                "无法清理上一代 status.html；为避免显示陈旧临时 API Key，已拒绝启动。"
            ) from exc
        log_path = self.config_path.parent / "launcher-chatgpt-web-llm.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = self._python_prefix() + [
            str(record.path / "standalone.py"),
            "--max-parallel",
            str(config.max_parallel),
            "--response-timeout-seconds",
            str(config.response_timeout_seconds),
            "--no-status-page",
        ]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        kwargs: dict[str, Any] = {
            "cwd": str(record.path),
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
            "env": environment,
        }
        kwargs.update(owned_process_group_kwargs(hide_window=True))
        self._last_error = ""
        with log_path.open("ab", buffering=0) as log_handle:
            kwargs["stdout"] = log_handle
            process = self._popen_factory(argv, **kwargs)
            self._process = process
        deadline = self._monotonic() + max(0.1, float(ready_timeout_seconds))
        while self._monotonic() < deadline:
            if process.poll() is not None:
                code = process.returncode
                if self._process is process:
                    self._process = None
                self._last_error = f"Standalone 启动进程提前退出（退出码 {code}）。日志：{log_path}"
                raise ManagedServiceError(self._last_error)
            state = self.status()
            if state["online"]:
                if not state.get("version_match"):
                    self.stop(wait_port_seconds=2.0)
                    raise ManagedServiceError(str(state.get("detail") or "Live Adapter 版本不匹配。"))
                self.open_status_page(wait_seconds=3.0)
                return {**state, "started": True, "reason": "ready" if state.get("ready") else "waiting-login"}
            self._sleep(0.25)
        self.stop(wait_port_seconds=2.0)
        self._last_error = f"Standalone 在 {ready_timeout_seconds:g} 秒内未建立本地服务。日志：{log_path}"
        raise ManagedServiceError(self._last_error)

    def ensure_auto_started(self) -> dict[str, Any]:
        state = self.status()
        if state["online"]:
            return {**state, "started": False, "reason": "already-online"}
        config = self.config()
        if not config.auto_start:
            return {**state, "started": False, "reason": "auto-start-disabled"}
        return self.start()

    def stop(self, *, wait_port_seconds: float = CHATGPT_WEB_LLM_STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            self._process = None
            state = self.status()
            return {**state, "stopped": False, "reason": "not-owned"}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        marker = self.state_dir / "stop.requested"
        try:
            marker.write_text("stop\n", encoding="utf-8")
        except OSError:
            pass
        deadline = self._monotonic() + 8.0
        while process.poll() is None and self._monotonic() < deadline:
            self._sleep(0.2)
        if process.poll() is None:
            self._terminate_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise ManagedServiceError("FolderBridge-owned Standalone 进程树未能确认退出。") from exc
        if self._process is process:
            self._process = None
        self._last_error = ""
        deadline = self._monotonic() + max(0.0, float(wait_port_seconds))
        state = self.status()
        while state["online"] and self._monotonic() < deadline:
            self._sleep(0.25)
            state = self.status()
        warnings: list[str] = []
        if state["online"]:
            warnings.append("8769 仍有 Adapter 响应，但它已不属于 FolderBridge 当前托管进程，因此不会终止。")
        for port in (CHATGPT_WEB_LLM_API_PORT, CHATGPT_WEB_LLM_DEVTOOLS_PORT):
            if not self._port_available(port):
                warnings.append(f"端口 {port} 仍被外部/未知进程占用；FolderBridge 不会按端口强杀陌生 PID。")
        return {
            **state,
            "stopped": True,
            "reason": "owned-process-stopped",
            "warning": " ".join(dict.fromkeys(warnings)),
        }

    def restart(self) -> dict[str, Any]:
        state = self.status()
        if state.get("online") and not state.get("owned"):
            raise ManagedServiceError("当前 8769 Adapter 为外部实例；FolderBridge 不会终止它。请先自行关闭外部实例。")
        if state.get("owned"):
            self.stop()
        self._assert_start_ports_available()
        return self.start()

    def open_status_page(self, *, wait_seconds: float = 0.0) -> dict[str, Any]:
        state = self.status()
        if not state.get("online") or not state.get("version_match"):
            raise ManagedServiceError("只有当前已安装版本的 live Adapter 才能打开本次状态页。")
        path = self._status_page()
        token = str(state.get("api_key") or "")
        deadline = self._monotonic() + max(0.0, wait_seconds)
        page_text = ""
        while True:
            if path.is_file():
                try:
                    page_text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    page_text = ""
                if page_text and (not token or token in page_text):
                    break
            if self._monotonic() >= deadline:
                break
            self._sleep(0.1)
        if not path.is_file():
            raise ManagedServiceError("本次 Standalone 尚未生成 status.html。")
        if not page_text or (token and token not in page_text):
            raise ManagedServiceError(
                "status.html 与当前 live Adapter 的临时 API Key 不一致；拒绝打开陈旧状态页，请重启托管服务重新生成。"
            )
        try:
            import webbrowser

            cache_buster = path.stat().st_mtime_ns
            opened = webbrowser.open(f"{path.resolve().as_uri()}?v={cache_buster}", new=2)
            if opened is False:
                raise OSError("default browser did not accept the status page URL")
        except OSError as exc:
            raise ManagedServiceError(f"无法打开 status.html：{exc}") from exc
        return {**state, "opened_status": True}

    def open_login_page(self) -> dict[str, Any]:
        state = self.status()
        token = str(state.get("api_key") or "")
        if not state.get("online") or not state.get("version_match") or not token:
            raise ManagedServiceError("当前 live Adapter 的临时 API Key 不可用，无法安全打开登录/验证页。")
        result = self._http_json("POST", "/control/open-login", token=token, timeout=10.0)
        return {**self.status(), "opened_login": bool(result.get("opened"))}

    def probe(self) -> dict[str, Any]:
        state = self.status()
        token = str(state.get("api_key") or "")
        if not state.get("ready") or not state.get("version_match") or not token:
            raise ManagedServiceError("Adapter 尚未 READY，无法执行真实 JSON completion 验证。")
        payload = {
            "model": CHATGPT_WEB_LLM_MODEL,
            "messages": [
                {"role": "system", "content": "Return exactly one JSON object and nothing else."},
                {"role": "user", "content": 'Return exactly {"ok":true,"adapter_probe":"pass"}.'},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        envelope = self._http_json(
            "POST",
            "/v1/chat/completions",
            token=token,
            body=payload,
            timeout=180.0,
        )
        try:
            content = envelope["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ManagedServiceError("真实 completion 返回了无效的 OpenAI-compatible JSON envelope。") from exc
        expected = {"ok": True, "adapter_probe": "pass"}
        if parsed != expected:
            raise ManagedServiceError("真实 completion 已返回，但固定 JSON probe 内容不匹配。")
        return {
            "service_id": self.extension_id,
            "probe_passed": True,
            "probe_response": expected,
        }

    def set_max_parallel(self, value: int) -> dict[str, Any]:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4:
            raise ManagedServiceError("并发上限必须是 1–4。")
        state = self.status()
        if state.get("online"):
            token = str(state.get("api_key") or "")
            if not state.get("version_match") or not token:
                raise ManagedServiceError("live Adapter 版本/临时 Key 不可用，拒绝调整并发。")
            self._http_json("POST", "/control/max-parallel", token=token, body={"max_parallel": value})
        current = self.config()
        updated = ChatGPTWebLLMServiceConfig(
            auto_start=current.auto_start,
            max_parallel=value,
            response_timeout_seconds=current.response_timeout_seconds,
        )
        save_chatgpt_web_llm_service_config(updated, self.config_path)
        return self.status()


class ManagedServiceManager:
    def __init__(self, controllers: Iterable[Any] = ()) -> None:
        self._controllers = {controller.extension_id: controller for controller in controllers}

    def controller(self, extension_id: str) -> Any | None:
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


def default_managed_service_manager(registry: ExtensionRegistry | None = None) -> ManagedServiceManager:
    controllers: list[Any] = [ComfyUIServiceController()]
    if registry is not None:
        controllers.append(ChatGPTWebLLMServiceController(registry))
    return ManagedServiceManager(controllers)

