from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .launcher_backend import (
    LauncherError,
    LauncherSettings,
    LauncherSettingsStore,
    TunnelSupervisor,
    build_doctor_argv,
    build_init_argv,
    build_run_argv,
    control_plane_environment,
    find_tunnel_client,
    mcp_command,
    redact_text,
    run_short_command,
)


DOCS_URL = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
TUNNEL_SETTINGS_URL = "https://platform.openai.com/settings/organization/tunnels"
TUNNEL_RELEASE_URL = "https://github.com/openai/tunnel-client/releases/latest"
CHATGPT_PLUGINS_URL = "https://chatgpt.com/plugins"
HELP_URL = "https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt"
MAX_LOG_CHARS = 180_000


class FolderBridgeLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = LauncherSettingsStore()
        self.settings = self.store.load()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1_000)
        self.supervisor = TunnelSupervisor(self._queue_tunnel_output)
        self._busy = False
        self._closing = False
        self._active_secret = ""
        self._last_exit_reported: int | None = None
        self._log_drop_reported = False

        self._create_variables()
        self._configure_window()
        self._configure_styles()
        self._build_ui()
        self._refresh_status_cards()
        self._set_connection_state("stopped")
        self._log("启动器已就绪。默认只读，不会保存 Runtime API Key。")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._drain_events)
        self.root.after(500, self._poll_process)

    def _create_variables(self) -> None:
        workspace = self.settings.workspace
        if not workspace:
            workspace = str(Path.cwd().resolve())
        self.workspace_var = tk.StringVar(value=workspace)
        self.access_var = tk.StringVar(value=self.settings.access_mode)
        self.profile_var = tk.StringVar(value=self.settings.profile)
        self.tunnel_id_var = tk.StringVar(value=self.settings.tunnel_id)
        self.tunnel_client_var = tk.StringVar(value=self.settings.tunnel_client_path)
        self.allow_tasks_var = tk.BooleanVar(value=self.settings.allow_tasks)
        self.api_key_var = tk.StringVar()
        self.show_key_var = tk.BooleanVar()

        self.connection_text = tk.StringVar(value="已停止")
        self.connection_detail = tk.StringVar(value="点击启动后建立出站连接")
        self.workspace_status = tk.StringVar()
        self.access_status = tk.StringVar()
        self.client_status = tk.StringVar()
        self.config_status = tk.StringVar()
        self.key_hint = tk.StringVar()

        for variable in (
            self.workspace_var,
            self.access_var,
            self.profile_var,
            self.tunnel_id_var,
            self.tunnel_client_var,
            self.allow_tasks_var,
        ):
            variable.trace_add("write", self._on_form_changed)

    def _configure_window(self) -> None:
        self.root.title("FolderBridge MCP · 本地工作区连接器")
        self.root.geometry("940x820")
        self.root.minsize(820, 700)
        self.root.configure(bg="#f4f6fa")
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("Page.TFrame", background="#f4f6fa")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#f4f6fa", foreground="#172033", font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", background="#f4f6fa", foreground="#62708a", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 11, "bold"))
        style.configure("Body.TLabel", background="#ffffff", foreground="#4d5a72", font=("Segoe UI", 9))
        style.configure("Field.TLabel", background="#ffffff", foreground="#364157", font=("Segoe UI", 9, "bold"))
        style.configure("Status.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#7a869c", font=("Segoe UI", 8))
        style.configure("TEntry", padding=6)
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 9))
        style.configure("TRadiobutton", background="#ffffff", font=("Segoe UI", 9))
        style.configure("TCheckbutton", background="#ffffff", font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        page = ttk.Frame(self.root, style="Page.TFrame", padding=(24, 20, 24, 20))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)

        header = ttk.Frame(page, style="Page.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="FolderBridge MCP", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="把一个明确的本地文件夹安全地接到 ChatGPT 网页端",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.guide_button = ttk.Button(header, text="网页端一键引导", command=self._open_web_setup)
        self.guide_button.grid(row=0, column=1, rowspan=2, sticky="e")

        self._build_overview(page).grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._build_local_settings(page).grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self._build_tunnel_settings(page).grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self._build_log(page).grid(row=4, column=0, sticky="nsew", pady=(0, 12))
        self._build_actions(page).grid(row=5, column=0, sticky="ew")

    def _build_overview(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        self.status_dot = tk.Canvas(card, width=18, height=18, bg="#ffffff", highlightthickness=0)
        self.status_dot.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 9), pady=(2, 0))
        self.status_dot_id = self.status_dot.create_oval(2, 2, 16, 16, fill="#98a2b3", outline="")
        ttk.Label(card, textvariable=self.connection_text, style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(card, textvariable=self.connection_detail, style="Muted.TLabel").grid(row=1, column=1, sticky="w")

        divider = ttk.Separator(card, orient="vertical")
        divider.grid(row=0, column=2, rowspan=2, sticky="ns", padx=18)
        ttk.Label(card, text="当前权限", style="Muted.TLabel").grid(row=0, column=3, sticky="w")
        ttk.Label(card, textvariable=self.access_status, style="Status.TLabel").grid(row=1, column=3, sticky="w")

        ttk.Label(card, text="工作区", style="Muted.TLabel").grid(row=0, column=4, sticky="w", padx=(24, 0))
        ttk.Label(card, textvariable=self.workspace_status, style="Status.TLabel").grid(row=1, column=4, sticky="w", padx=(24, 0))
        return card

    def _build_local_settings(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="1  选择本地工作区", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            card,
            text="MCP 进程只能访问这个文件夹；子目录中的链接、凭据和常见依赖目录会被拦截。",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 12))

        ttk.Label(card, text="文件夹", style="Field.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.workspace_entry = ttk.Entry(card, textvariable=self.workspace_var)
        self.workspace_entry.grid(row=2, column=1, sticky="ew")
        self.browse_workspace_button = ttk.Button(card, text="浏览…", command=self._browse_workspace)
        self.browse_workspace_button.grid(row=2, column=2, padx=(8, 0))

        ttk.Label(card, text="权限", style="Field.TLabel").grid(row=3, column=0, sticky="nw", pady=(13, 0))
        mode_frame = ttk.Frame(card, style="Card.TFrame")
        mode_frame.grid(row=3, column=1, columnspan=2, sticky="w", pady=(10, 0))
        self.read_only_radio = ttk.Radiobutton(
            mode_frame,
            text="只读（推荐）",
            value="read_only",
            variable=self.access_var,
        )
        self.read_only_radio.pack(side="left")
        self.read_write_radio = ttk.Radiobutton(
            mode_frame,
            text="读写（修改前仍需 ChatGPT 确认）",
            value="read_write",
            variable=self.access_var,
        )
        self.read_write_radio.pack(side="left", padx=(18, 0))

        self.tasks_check = ttk.Checkbutton(
            card,
            text="高级：允许已在本机单独批准的测试任务（默认关闭）",
            variable=self.allow_tasks_var,
        )
        self.tasks_check.grid(row=4, column=1, columnspan=2, sticky="w", pady=(9, 0))
        return card

    def _build_tunnel_settings(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="2  OpenAI Secure MCP Tunnel", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            card,
            text="只建立到 OpenAI 的出站 HTTPS 连接，本机不开放公网端口。API Key 只存在于本次内存。",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 12))

        ttk.Label(card, text="tunnel-client", style="Field.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.client_entry = ttk.Entry(card, textvariable=self.tunnel_client_var)
        self.client_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        self.browse_client_button = ttk.Button(card, text="选择…", command=self._browse_tunnel_client)
        self.browse_client_button.grid(row=2, column=3, padx=(8, 0))
        ttk.Label(card, textvariable=self.client_status, style="Muted.TLabel").grid(row=3, column=1, columnspan=3, sticky="w", pady=(3, 0))

        ttk.Label(card, text="Profile", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.profile_entry = ttk.Entry(card, textvariable=self.profile_var, width=22)
        self.profile_entry.grid(row=4, column=1, sticky="w", pady=(10, 0))
        ttk.Label(card, text="Tunnel ID", style="Field.TLabel").grid(row=4, column=2, sticky="e", padx=(16, 8), pady=(10, 0))
        self.tunnel_entry = ttk.Entry(card, textvariable=self.tunnel_id_var)
        self.tunnel_entry.grid(row=4, column=3, sticky="ew", pady=(10, 0))

        ttk.Label(card, text="Runtime API Key", style="Field.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 0))
        key_frame = ttk.Frame(card, style="Card.TFrame")
        key_frame.grid(row=5, column=1, columnspan=3, sticky="ew", pady=(10, 0))
        key_frame.columnconfigure(0, weight=1)
        self.key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, show="●")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_check = ttk.Checkbutton(
            key_frame,
            text="显示",
            variable=self.show_key_var,
            command=self._toggle_key_visibility,
        )
        self.show_key_check.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(card, textvariable=self.key_hint, style="Muted.TLabel").grid(row=6, column=1, columnspan=3, sticky="w", pady=(3, 0))

        ttk.Label(card, text="配置状态", style="Field.TLabel").grid(row=7, column=0, sticky="w", pady=(10, 0))
        ttk.Label(card, textvariable=self.config_status, style="Body.TLabel").grid(row=7, column=1, columnspan=3, sticky="w", pady=(10, 0))
        return card

    def _build_log(self, parent: ttk.Frame) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, text="运行日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(bar, text="清空", command=self._clear_log).grid(row=0, column=1, sticky="e")
        self.log = scrolledtext.ScrolledText(
            card,
            height=8,
            wrap="word",
            borderwidth=0,
            background="#101827",
            foreground="#d8e2f0",
            insertbackground="#ffffff",
            font=("Cascadia Mono", 8),
            padx=10,
            pady=9,
            state="disabled",
        )
        self.log.grid(row=1, column=0, sticky="nsew")
        return card

    def _build_actions(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Page.TFrame")
        frame.columnconfigure(4, weight=1)
        self.copy_button = ttk.Button(frame, text="复制本地 MCP 命令", command=self._copy_mcp_command)
        self.copy_button.grid(row=0, column=0, padx=(0, 8))
        self.apply_button = ttk.Button(frame, text="应用配置", command=self._apply_config)
        self.apply_button.grid(row=0, column=1, padx=(0, 8))
        self.doctor_button = ttk.Button(frame, text="诊断", command=self._diagnose)
        self.doctor_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(frame, text="官方文档", command=lambda: webbrowser.open(DOCS_URL)).grid(row=0, column=3)
        self.start_button = tk.Button(
            frame,
            text="启动连接",
            command=self._toggle_connection,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            disabledforeground="#dbe6ff",
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 11, "bold"),
            padx=24,
            pady=9,
        )
        self.start_button.grid(row=0, column=5, sticky="e")
        return frame

    def _on_form_changed(self, *_args: object) -> None:
        if hasattr(self, "workspace_status"):
            self._refresh_status_cards()

    def _refresh_status_cards(self) -> None:
        raw_workspace = self.workspace_var.get().strip()
        workspace = Path(raw_workspace).expanduser() if raw_workspace else None
        self.workspace_status.set(workspace.name if workspace and workspace.name else "未选择")
        mode = self.access_var.get()
        if mode == "read_only":
            self.access_status.set("只读 · 安全")
        elif mode == "read_write":
            self.access_status.set("读写 · 需确认")
        else:
            self.access_status.set("未选择")

        executable = find_tunnel_client(self.tunnel_client_var.get())
        if executable:
            self.client_status.set(f"已找到：{executable}")
        else:
            self.client_status.set("未找到。可点击“网页端一键引导”打开官方下载页。")

        try:
            current = self._settings_from_form().fingerprint()
        except (LauncherError, OSError):
            current = ""
        if current and current == self.settings.configured_fingerprint:
            self.config_status.set("已应用；配置未发生变化")
        else:
            self.config_status.set("尚未应用，首次启动时会自动配置")

        if self.api_key_var.get().strip():
            self.key_hint.set("仅保留在当前进程内存，关闭窗口后消失")
        elif os.environ.get("CONTROL_PLANE_API_KEY"):
            self.key_hint.set("已检测到 CONTROL_PLANE_API_KEY 环境变量")
        else:
            self.key_hint.set("不会写入配置、日志或命令行参数")

    def _settings_from_form(self) -> LauncherSettings:
        return LauncherSettings(
            workspace=self.workspace_var.get().strip(),
            access_mode=self.access_var.get(),
            profile=self.profile_var.get().strip(),
            tunnel_id=self.tunnel_id_var.get().strip(),
            tunnel_client_path=self.tunnel_client_var.get().strip(),
            allow_tasks=bool(self.allow_tasks_var.get()),
            configured_fingerprint=self.settings.configured_fingerprint,
        )

    def _save_form(self, settings: LauncherSettings) -> None:
        self.settings = settings
        self.store.save(settings)

    def _browse_workspace(self) -> None:
        selected = filedialog.askdirectory(title="选择允许 ChatGPT 访问的工作区", initialdir=self.workspace_var.get() or str(Path.cwd()))
        if selected:
            self.workspace_var.set(selected)

    def _browse_tunnel_client(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 OpenAI tunnel-client",
            filetypes=(("tunnel-client", "tunnel-client*"), ("可执行文件", "*.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.tunnel_client_var.set(selected)

    def _toggle_key_visibility(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "●")

    def _toggle_connection(self) -> None:
        if self.supervisor.running():
            self._stop_connection()
        else:
            self._start_connection()

    def _start_connection(self) -> None:
        try:
            settings = self._settings_from_form()
            workspace = settings.validate(require_tunnel_id=True)
            executable = find_tunnel_client(settings.tunnel_client_path)
            if not executable:
                raise LauncherError("没有找到官方 tunnel-client，请选择可执行文件或先下载")
            env = control_plane_environment(self.api_key_var.get())
            fingerprint = settings.fingerprint()
            self._save_form(settings)
        except (LauncherError, OSError) as exc:
            self._show_error(str(exc))
            return

        self._active_secret = env.get("CONTROL_PLANE_API_KEY", "")
        needs_init = fingerprint != settings.configured_fingerprint
        self._set_busy(True)
        self._set_connection_state("starting")
        self._log("开始连接：检查配置与官方 Tunnel…")

        def work() -> None:
            try:
                if needs_init:
                    self._queue_event("log", "检测到首次使用或配置变化，正在生成 Tunnel profile…")
                    result = run_short_command(build_init_argv(executable, settings, workspace), env=env, timeout_seconds=45)
                    self._queue_command_result("init", result)
                    if result.timed_out or result.exit_code != 0:
                        raise LauncherError("Tunnel profile 配置失败，请查看日志")
                    self._queue_event("configured", fingerprint)
                self._run_doctor(executable, settings.profile, env)
                pid = self.supervisor.start(build_run_argv(executable, settings.profile), env=env)
                self._queue_event("started", pid)
            except Exception as exc:  # Keep failures inside the worker boundary.
                self._queue_event("error", str(exc))
            finally:
                self._queue_event("busy", False)

        threading.Thread(target=work, name="folderbridge-start", daemon=True).start()

    def _stop_connection(self) -> None:
        self._set_busy(True, allow_stop=False)
        self.connection_detail.set("正在安全停止本地进程…")
        self._log("正在停止 Tunnel 连接…")

        def work() -> None:
            try:
                code = self.supervisor.stop()
                self._queue_event("stopped", code)
            except Exception as exc:
                self._queue_event("error", str(exc))
            finally:
                self._queue_event("busy", False)

        threading.Thread(target=work, name="folderbridge-stop", daemon=True).start()

    def _apply_config(self) -> None:
        try:
            settings = self._settings_from_form()
            workspace = settings.validate(require_tunnel_id=True)
            executable = find_tunnel_client(settings.tunnel_client_path)
            if not executable:
                raise LauncherError("没有找到官方 tunnel-client")
            env = control_plane_environment(self.api_key_var.get())
            fingerprint = settings.fingerprint()
            self._save_form(settings)
        except (LauncherError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._active_secret = env.get("CONTROL_PLANE_API_KEY", "")
        self._set_busy(True)
        self._log("正在应用 Tunnel profile…")

        def work() -> None:
            try:
                result = run_short_command(build_init_argv(executable, settings, workspace), env=env, timeout_seconds=45)
                self._queue_command_result("init", result)
                if result.timed_out or result.exit_code != 0:
                    raise LauncherError("配置失败，请查看日志")
                self._run_doctor(executable, settings.profile, env)
                self._queue_event("configured", fingerprint)
                self._queue_event("notice", "配置和诊断均已通过。")
            except Exception as exc:
                self._queue_event("error", str(exc))
            finally:
                self._queue_event("busy", False)

        threading.Thread(target=work, name="folderbridge-configure", daemon=True).start()

    def _diagnose(self) -> None:
        try:
            settings = self._settings_from_form()
            settings.validate(require_tunnel_id=False)
            executable = find_tunnel_client(settings.tunnel_client_path)
            if not executable:
                raise LauncherError("没有找到官方 tunnel-client")
            env = control_plane_environment(self.api_key_var.get())
            self._save_form(settings)
        except (LauncherError, OSError) as exc:
            self._show_error(str(exc))
            return
        self._active_secret = env.get("CONTROL_PLANE_API_KEY", "")
        self._set_busy(True)
        self._log("正在运行官方 doctor 诊断…")

        def work() -> None:
            try:
                self._run_doctor(executable, settings.profile, env)
                self._queue_event("notice", "诊断通过。")
            except Exception as exc:
                self._queue_event("error", str(exc))
            finally:
                self._queue_event("busy", False)

        threading.Thread(target=work, name="folderbridge-doctor", daemon=True).start()

    def _run_doctor(self, executable: Path, profile: str, env: dict[str, str]) -> None:
        result = run_short_command(build_doctor_argv(executable, profile), env=env, timeout_seconds=45)
        self._queue_command_result("doctor", result)
        if result.timed_out:
            raise LauncherError("官方 doctor 诊断超时")
        if result.exit_code != 0:
            raise LauncherError("官方 doctor 诊断未通过，请查看日志")

    def _queue_command_result(self, name: str, result: object) -> None:
        output = getattr(result, "output", "")
        code = getattr(result, "exit_code", "?")
        timed_out = getattr(result, "timed_out", False)
        truncated = getattr(result, "truncated", False)
        if output:
            self._queue_event("log", output)
        suffix = "；超时" if timed_out else ""
        if truncated:
            suffix += "；输出已截断"
        self._queue_event("log", f"[{name}] 退出码 {code}{suffix}")

    def _copy_mcp_command(self) -> None:
        try:
            settings = self._settings_from_form()
            workspace = settings.validate(require_tunnel_id=False)
            command = mcp_command(workspace, settings.access_mode, settings.allow_tasks)
        except LauncherError as exc:
            self._show_error(str(exc))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self._log("本地 MCP 命令已复制到剪贴板。")

    def _open_web_setup(self) -> None:
        tunnel_id = self.tunnel_id_var.get().strip()
        executable = find_tunnel_client(self.tunnel_client_var.get())
        if tunnel_id:
            self.root.clipboard_clear()
            self.root.clipboard_append(tunnel_id)

        dialog = tk.Toplevel(self.root)
        dialog.title("网页端连接引导")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg="#ffffff")
        body = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="网页端连接，只需确认三件事", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            body,
            text="账号级开关必须由你本人/管理员确认；启动器不会读取或操纵 ChatGPT 登录态。",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 16))

        checks = (
            ("1", "Platform Tunnel", "创建 Tunnel、取得 tunnel_id 和 Runtime API Key，并关联目标 ChatGPT workspace。"),
            ("2", "本地连接", "回到主界面填写信息并点击“启动连接”，保持状态为运行中。"),
            ("3", "ChatGPT 自定义 App", "打开 Plugins，点 +，Connection 选 Tunnel，再选择或粘贴 Tunnel ID。"),
        )
        for row, (number, title, detail) in enumerate(checks, start=2):
            badge = tk.Label(
                body,
                text=number,
                bg="#dbeafe",
                fg="#1d4ed8",
                width=2,
                font=("Segoe UI", 9, "bold"),
            )
            badge.grid(row=row, column=0, sticky="n", padx=(0, 10), pady=(4, 10))
            text_frame = ttk.Frame(body, style="Card.TFrame")
            text_frame.grid(row=row, column=1, sticky="w", pady=(0, 10))
            ttk.Label(text_frame, text=title, style="Status.TLabel").pack(anchor="w")
            ttk.Label(text_frame, text=detail, style="Body.TLabel", wraplength=620).pack(anchor="w", pady=(2, 0))

        status = "✓ 已找到 tunnel-client" if executable else "! 尚未找到 tunnel-client，可从官方 Release 下载"
        ttk.Label(body, text=status, style="Body.TLabel").grid(row=5, column=1, sticky="w", pady=(0, 12))
        if tunnel_id:
            ttk.Label(body, text="✓ Tunnel ID 已复制到剪贴板", style="Body.TLabel").grid(row=6, column=1, sticky="w", pady=(0, 12))

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(
            buttons,
            text="一键打开所需页面",
            command=lambda: self._open_required_pages(executable is not None),
        ).pack(side="left")
        ttk.Button(buttons, text="Tunnel 设置", command=lambda: webbrowser.open(TUNNEL_SETTINGS_URL)).pack(side="left", padx=(8, 0))
        if not executable:
            ttk.Button(buttons, text="下载官方客户端", command=lambda: webbrowser.open(TUNNEL_RELEASE_URL)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="ChatGPT Plugins", command=lambda: webbrowser.open(CHATGPT_PLUGINS_URL)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="权限说明", command=lambda: webbrowser.open(HELP_URL)).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="完成", command=dialog.destroy).pack(side="right", padx=(16, 0))
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 3)
        dialog.geometry(f"+{x}+{y}")

    def _open_required_pages(self, client_ready: bool) -> None:
        webbrowser.open(TUNNEL_SETTINGS_URL)
        if not client_ready:
            webbrowser.open(TUNNEL_RELEASE_URL)
        webbrowser.open(CHATGPT_PLUGINS_URL)
        self._log("已打开 OpenAI Tunnel 设置、官方客户端下载页（如需要）和 ChatGPT Plugins。")

    def _queue_tunnel_output(self, text: str) -> None:
        self._queue_event("log", text)

    def _queue_event(self, kind: str, payload: object) -> None:
        if kind == "log":
            payload = redact_text(str(payload), (self._active_secret,))
        try:
            self.events.put_nowait((kind, payload))
        except queue.Full:
            if not self._log_drop_reported:
                self._log_drop_reported = True
                try:
                    self.events.get_nowait()
                    self.events.put_nowait(("log", "日志过多，已丢弃部分旧输出。"))
                except (queue.Empty, queue.Full):
                    pass

    def _drain_events(self) -> None:
        if self._closing:
            return
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(payload))
            elif kind == "configured":
                self.settings.configured_fingerprint = str(payload)
                self.store.save(self.settings)
                self._refresh_status_cards()
            elif kind == "started":
                self._last_exit_reported = None
                self._set_connection_state("running", int(payload))
                self._log(f"Tunnel 已启动（PID {payload}）。保持本窗口打开即可使用。")
            elif kind == "stopped":
                self._set_connection_state("stopped")
                self._log(f"连接已停止（退出码 {payload}）。")
            elif kind == "error":
                self._set_connection_state("error")
                self._show_error(redact_text(str(payload), (self._active_secret,)))
            elif kind == "notice":
                self._log(str(payload))
                messagebox.showinfo("FolderBridge MCP", str(payload), parent=self.root)
            elif kind == "busy":
                self._set_busy(bool(payload))
        self.root.after(120, self._drain_events)

    def _poll_process(self) -> None:
        if self._closing:
            return
        process = self.supervisor.process
        if process is not None:
            code = process.poll()
            if code is not None and self._last_exit_reported is None and self.connection_text.get() == "运行中":
                self._last_exit_reported = code
                self._set_connection_state("error")
                self._log(f"Tunnel 进程意外退出（退出码 {code}）。请运行诊断。")
        self.root.after(500, self._poll_process)

    def _set_connection_state(self, state: str, pid: int | None = None) -> None:
        colors = {"stopped": "#98a2b3", "starting": "#f59e0b", "running": "#16a34a", "error": "#dc2626"}
        labels = {"stopped": "已停止", "starting": "启动中", "running": "运行中", "error": "异常"}
        details = {
            "stopped": "点击启动后建立出站连接",
            "starting": "正在配置并执行官方诊断…",
            "running": f"官方 doctor 已通过 · 进程 PID {pid}" if pid else "官方 doctor 已通过 · 进程正在运行",
            "error": "请查看日志并运行诊断",
        }
        self.status_dot.itemconfigure(self.status_dot_id, fill=colors[state])
        self.connection_text.set(labels[state])
        self.connection_detail.set(details[state])
        if state == "running":
            self.start_button.configure(text="停止连接", bg="#dc2626", activebackground="#b91c1c", state="normal")
            self._set_form_state(False)
        else:
            self.start_button.configure(text="启动连接", bg="#2563eb", activebackground="#1d4ed8")
            if not self._busy:
                self.start_button.configure(state="normal")
                self._set_form_state(True)

    def _set_busy(self, busy: bool, *, allow_stop: bool = True) -> None:
        self._busy = busy
        enabled = not busy and not self.supervisor.running()
        for widget in (self.apply_button, self.doctor_button, self.copy_button, self.guide_button):
            widget.configure(state="normal" if enabled else "disabled")
        if busy:
            can_stop = allow_stop and self.supervisor.running()
            self.start_button.configure(state="normal" if can_stop else "disabled")
            self._set_form_state(False)
        elif self.supervisor.running():
            self.start_button.configure(state="normal")
            self._set_form_state(False)
        else:
            self.start_button.configure(state="normal")
            self._set_form_state(True)

    def _set_form_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.workspace_entry,
            self.browse_workspace_button,
            self.client_entry,
            self.browse_client_button,
            self.profile_entry,
            self.tunnel_entry,
            self.key_entry,
            self.show_key_check,
            self.read_only_radio,
            self.read_write_radio,
            self.tasks_check,
        ):
            widget.configure(state=state)

    def _log(self, text: str) -> None:
        clean = redact_text(text.rstrip(), (self._active_secret,))
        if not clean:
            return
        timestamp = time.strftime("%H:%M:%S")
        lines = clean.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        rendered = "\n".join(f"[{timestamp}] {line}" for line in lines) + "\n"
        self.log.configure(state="normal")
        self.log.insert("end", rendered)
        current = int(self.log.index("end-1c").split(".")[0])
        if current > 2_000 or len(self.log.get("1.0", "end-1c")) > MAX_LOG_CHARS:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._log_drop_reported = False

    def _show_error(self, message: str) -> None:
        safe = redact_text(message, (self._active_secret,))
        self._log(f"错误：{safe}")
        messagebox.showerror("FolderBridge MCP", safe, parent=self.root)

    def _on_close(self) -> None:
        if self.supervisor.running():
            if not messagebox.askyesno("退出 FolderBridge MCP", "Tunnel 仍在运行。退出并停止连接吗？", parent=self.root):
                return
            try:
                self.supervisor.stop()
            except Exception:
                pass
        try:
            settings = self._settings_from_form()
            self._save_form(settings)
        except (OSError, tk.TclError):
            pass
        self._active_secret = ""
        self.api_key_var.set("")
        self._closing = True
        self.root.destroy()


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main() -> int:
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    FolderBridgeLauncher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
