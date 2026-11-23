"""Launcher-owned service for the external TestOnline LAN host.

The external Plugin implements all publishing behavior. FolderBridge only
checks explicit approval, starts its own child runtime with approved plugin code,
provides status, and stops only its own child process at Launcher shutdown.
No extension status/list call starts a network service.
"""
from __future__ import annotations

import importlib.util
import ipaddress

# No machine-specific LAN address is stored in published source.
# An explicitly configured private IPv4 address takes priority; otherwise use
# an available local RFC1918 interface, and fall back safely to localhost.
def _testonline_bind_ip() -> str:
    configured = os.environ.get("FOLDERBRIDGE_TESTONLINE_BIND_IP", "").strip()
    candidates: list[str] = []
    if configured:
        candidates = [configured]
    else:
        try:
            candidates = [entry[4][0] for entry in socket.getaddrinfo(
                socket.gethostname(), None, family=socket.AF_INET)]
        except (OSError, socket.gaierror):
            pass
    private_networks = [ipaddress.IPv4Network(net) for net in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    for candidate in candidates:
        try:
            address = ipaddress.IPv4Address(candidate)
        except (ipaddress.AddressValueError, ValueError):
            continue
        if any(address in net for net in private_networks):
            return str(address)
    if configured:
        raise ValueError("FOLDERBRIDGE_TESTONLINE_BIND_IP must be a private LAN IPv4 address")
    return "127.0.0.1"
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Callable

from .extensions import ExtensionRegistry, extension_state_root, load_extension
from .managed_services import ManagedServiceError
from .process_control import owned_process_group_kwargs, terminate_owned_process_tree
from .security import clean_environment
from .user_paths import internal_child_config_root_environment, user_config_root

PLUGIN_ID = "testonline-host"
BIND_IP = _testonline_bind_ip()
DEFAULT_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1]) / "TestOnline"
CONTROL_PORT = 18889
BOOTSTRAP_PORT = 18888
PLUGIN_SERVER = "server.py"


class TestOnlineServiceError(ManagedServiceError):
    pass


@dataclass(frozen=True)
class TestOnlineServiceConfig:
    auto_start: bool = True


class TestOnlineServiceController:
    extension_id = PLUGIN_ID

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        config_path: Path | None = None,
        publish_root: Path = DEFAULT_ROOT,
        bind_ip: str = BIND_IP,
        popen_factory: Callable[..., Any] | None = None,
        terminate_process: Callable[[Any], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.config_path = config_path or extension_state_root() / PLUGIN_ID / "launcher-service.json"
        self.publish_root = publish_root
        self.bind_ip = bind_ip
        self._popen_factory = popen_factory or subprocess.Popen
        self._terminate_process = terminate_process or (
            lambda process: terminate_owned_process_tree(process, force=os.name == "nt")
        )
        self._sleep = sleeper
        self._monotonic = monotonic
        self._process: Any | None = None
        self._last_error = ""

    def config(self) -> TestOnlineServiceConfig:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if (
                isinstance(value, dict)
                and set(value) == {"version", "auto_start"}
                and value["version"] == 1
                and isinstance(value["auto_start"], bool)
            ):
                return TestOnlineServiceConfig(value["auto_start"])
        except (OSError, UnicodeError, ValueError, KeyError):
            pass
        return TestOnlineServiceConfig()

    def set_auto_start(self, enabled: bool) -> TestOnlineServiceConfig:
        config = TestOnlineServiceConfig(bool(enabled))
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "auto_start": config.auto_start}) + "\n"
        temp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", prefix=".testonline-",
                suffix=".tmp", dir=self.config_path.parent, delete=False,
            ) as handle:
                temp = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.config_path)
            temp = None
        finally:
            if temp is not None:
                try:
                    Path(temp).unlink()
                except FileNotFoundError:
                    pass
        return config

    def ui_spec(self) -> dict[str, Any]:
        return {
            "service_name": "TestOnline 局域网发布",
            "requires_install_root": False,
            "select_directory": False,
            "show_auto_start": True,
            "supports_restart": True,
            "supports_open_status": True,
            "supports_open_login": False,
            "supports_probe": False,
            "show_connection_fields": False,
            "show_parallel_control": False,
        }

    def _installed_record(self, *, require_enabled: bool) -> Any:
        try:
            record = self.registry.get(PLUGIN_ID)
        except Exception as exc:
            raise TestOnlineServiceError("TestOnline 外源插件尚未安装。") from exc
        if record.bundled:
            raise TestOnlineServiceError("TestOnline 必须使用已安装的外源插件。")
        trust = self.registry.trust_store.status(record)
        if require_enabled and not (trust.get("trusted") and trust.get("enabled")):
            raise TestOnlineServiceError("请先在 Extensions & Skills 批准并启用 TestOnline。")
        server = record.path / PLUGIN_SERVER
        if not server.is_file() or server.is_symlink():
            raise TestOnlineServiceError("TestOnline 外源插件缺少服务源码：" + PLUGIN_SERVER)
        if getattr(server.lstat(), "st_file_attributes", 0) & 0x400:
            raise TestOnlineServiceError("TestOnline 服务源码不得是 Windows reparse-point。")
        return record

    @staticmethod
    def _health() -> dict[str, Any] | None:
        connection = HTTPConnection("127.0.0.1", CONTROL_PORT, timeout=1.5)
        try:
            connection.request("GET", "/status", headers={"Accept": "application/json"})
            reply = connection.getresponse()
            raw = reply.read(131_073)
            if reply.status != 200 or len(raw) > 131_072:
                return None
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) and data.get("service_id") == PLUGIN_ID else None
        except (OSError, TimeoutError, UnicodeError, ValueError):
            return None
        finally:
            connection.close()

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
                return True
            except OSError:
                return False

    def status(self) -> dict[str, Any]:
        p = self._process
        owned = p is not None and p.poll() is None
        if p is not None and not owned:
            if p.returncode not in {None, 0}:
                self._last_error = "TestOnline 进程异常退出（码 " + str(p.returncode) + "）"
            self._process = None
        live = self._health()
        online = live is not None
        installed_version = ""
        installed_sha = ""
        trusted = False
        try:
            record = self._installed_record(require_enabled=False)
            installed_version = record.manifest.version
            installed_sha = record.sha256
            trust = self.registry.trust_store.status(record)
            trusted = bool(trust.get("trusted") and trust.get("enabled"))
        except TestOnlineServiceError:
            pass
        version_match = bool(online and live.get("version") == installed_version)
        site_errors = (live or {}).get("errors") or []
        ready = bool(online and version_match and not site_errors)
        return {
            "service_id": PLUGIN_ID,
            "online": online, "ready": ready,
            "owned": owned, "external": online and not owned,
            "auto_start": self.config().auto_start,
            "display_state": ("READY" if ready
                              else "ERROR" if online
                              else "STARTING" if owned
                              else "ERROR" if self._last_error
                              else "OFFLINE"),
            "detail": (
                ("站点错误：" + "; ".join(str(row.get("name", "")) + ": " + str(row.get("error", ""))
                                            for row in site_errors)[:300]) if site_errors
                else "正在托管 " + str(len(live.get("sites", []))) + " 个站点"
            ) if live else self._last_error or "等待 FolderBridge 启动 TestOnline",
            "installed_version": installed_version,
            "installed_sha256": installed_sha,
            "trusted": trusted,
            "live_version": str((live or {}).get("version") or ""),
            "version_match": version_match,
            "base_url": f"http://{self.bind_ip}:{BOOTSTRAP_PORT}/",
            "status_page": f"http://{self.bind_ip}:{BOOTSTRAP_PORT}/",
            "sites": (live or {}).get("sites", []),
            "errors": (live or {}).get("errors", []),
        }

    def start(self, *, ready_timeout_seconds: float = 12.0) -> dict[str, Any]:
        state = self.status()
        if state["online"]:
            if state.get("external"):
                raise TestOnlineServiceError("已有非本次 FolderBridge 托管的 TestOnline 实例；不会接管或停止它。")
            if not state.get("version_match"):
                raise TestOnlineServiceError("TestOnline 已运行版本与安装插件版本不一致。")
            return {**state, "started": False, "reason": "already-online"}
        if self._process is not None and self._process.poll() is None:
            return {**state, "started": False, "reason": "already-starting"}
        record = self._installed_record(require_enabled=True)
        if not self._port_open("127.0.0.1", CONTROL_PORT) or not self._port_open(self.bind_ip, BOOTSTRAP_PORT):
            raise TestOnlineServiceError("TestOnline 控制/入口端口已被占用；不会强行终止其它进程。")
        if self.publish_root.is_symlink() or (
            self.publish_root.exists() and
            (not self.publish_root.is_dir() or bool(getattr(self.publish_root.lstat(), "st_file_attributes", 0) & 0x400))
        ):
            raise TestOnlineServiceError("TestOnline 目录必须是真实文件夹，不能是链接或 Junction。")
        self.publish_root.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.config_path.parent / "launcher-testonline.log"
        child = [str(Path(sys.executable).resolve())]
        if not getattr(sys, "frozen", False):
            child.append(str(Path(__file__).resolve().parents[1] / "folderbridge_launcher.py"))
        argv = [
            *child, "testonline-service",
            "--extension-path", str(record.path),
            "--expected-sha256", record.sha256,
            "--root", str(self.publish_root),
            "--bind-ip", self.bind_ip,
            "--state-dir", str(self.config_path.parent),
        ]
        env = clean_environment(record.path)
        env.update(internal_child_config_root_environment(user_config_root()))
        env["PYTHONUTF8"] = "1"
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        kwargs: dict[str, Any] = {
            "cwd": str(record.path),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "close_fds": True,
        }
        kwargs.update(owned_process_group_kwargs(hide_window=True))
        self._last_error = ""
        with log_path.open("ab", buffering=0) as output:
            kwargs["stdout"] = output
            process = self._popen_factory(argv, **kwargs)
        self._process = process
        deadline = self._monotonic() + max(0.1, float(ready_timeout_seconds))
        while self._monotonic() < deadline:
            if process.poll() is not None:
                self._process = None
                self._last_error = "TestOnline 托管子进程启动失败（码 " + str(process.returncode) + "），请检查 " + str(log_path)
                raise TestOnlineServiceError(self._last_error)
            current = self.status()
            if current["online"]:
                if not current["version_match"]:
                    self.stop()
                    raise TestOnlineServiceError("TestOnline 独立进程版本不一致。")
                return {**current, "started": True, "reason": "ready" if current["ready"] else "online-with-errors"}
            self._sleep(0.25)
        self.stop()
        raise TestOnlineServiceError("TestOnline 启动超时；日志：" + str(log_path))

    def ensure_auto_started(self) -> dict[str, Any]:
        if not self.config().auto_start:
            return {**self.status(), "started": False, "reason": "auto-start-disabled"}
        return self.start()

    def stop(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            return {**self.status(), "stopped": False, "reason": "not-owned"}
        self._terminate_process(process)
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, AttributeError):
            raise TestOnlineServiceError("TestOnline 独立进程未能确认终止。")
        self._process = None
        self._last_error = ""
        return {**self.status(), "stopped": True, "reason": "owned-process-stopped"}

    def restart(self) -> dict[str, Any]:
        if self.status().get("external"):
            raise TestOnlineServiceError("已有外部 TestOnline 实例，拒绝终止其进程。")
        self.stop()
        return self.start()

    def open_status_page(self) -> dict[str, Any]:
        state = self.status()
        if not state["online"]:
            raise TestOnlineServiceError("TestOnline 尚未运行。")
        if not webbrowser.open(state["status_page"], new=2):
            raise TestOnlineServiceError("无法打开 TestOnline 局域网主页。")
        return {**state, "opened_status": True}


def run_testonline_service(extension_path: str, expected_sha256: str, root: str,
                           bind_ip: str, state_dir: str, *, check_import: bool = False) -> int:
    """Run approved external plugin source in an owned FolderBridge child process.

    The host owns the process lifetime; the plugin owns HTTP, TLS and site logic.
    No separate TestOnline executable or plugin code in the GUI process is needed.
    """
    try:
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise TestOnlineServiceError("TestOnline 插件 SHA 参数无效。")
        record = load_extension(Path(extension_path), bundled=False)
        if record.manifest.extension_id != PLUGIN_ID or record.sha256 != expected_sha256:
            raise TestOnlineServiceError("TestOnline 插件版本/哈希已变化；拒绝启动旧批准的代码。")
        entrypoint = record.path / PLUGIN_SERVER
        if not entrypoint.is_file() or entrypoint.is_symlink():
            raise TestOnlineServiceError("TestOnline 插件缺少服务入口。")
        spec = importlib.util.spec_from_file_location("folderbridge_testonline_host", entrypoint)
        if spec is None or spec.loader is None:
            raise TestOnlineServiceError("无法加载 TestOnline 服务源码。")
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(record.path))
        try:
            spec.loader.exec_module(module)
            serve = getattr(module, "serve", None)
            if not callable(serve):
                raise TestOnlineServiceError("TestOnline 外源插件必须提供 serve(root, bind_ip, state_dir)。")
            if not check_import:
                serve(Path(root).absolute(), bind_ip, Path(state_dir).absolute())
        finally:
            sys.path.remove(str(record.path))
        return 0
    except Exception as exc:
        print(f"TestOnline service failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
