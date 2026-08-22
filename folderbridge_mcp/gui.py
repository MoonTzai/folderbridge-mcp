from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import canonical_workspaces, workspace_id
from .dpi import (
    enable_windows_dpi_awareness,
    fitted_window_size,
    scale_for_dpi,
    scaled_pixels,
    tk_scaling_for_dpi,
    window_dpi,
)
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
    render_client_config,
    run_short_command,
)
from .setup_guide import (
    CHATGPT_INVOCATION_EXAMPLE,
    WINDOWS_X64_ASSET_PATTERN,
    looks_like_tunnel_id,
    recommended_client_directory,
)


DOCS_URL = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
TUNNEL_SETTINGS_URL = "https://platform.openai.com/settings/organization/tunnels"
TUNNEL_RELEASE_URL = "https://github.com/openai/tunnel-client/releases/latest"
RUNTIME_KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
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
        self._dpi = 96
        self._ui_scale = 1.0
        self._dpi_refresh_id: str | None = None

        self._create_variables()
        self._configure_window()
        self._configure_styles()
        self._build_ui()
        self._refresh_status_cards()
        self._set_connection_state("stopped")
        self._log("启动器已就绪。默认只读，不会保存 Runtime API Key。")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._schedule_dpi_refresh, add="+")
        self.root.after(120, self._drain_events)
        self.root.after(500, self._poll_process)

    def _create_variables(self) -> None:
        self.workspace_paths = list(self.settings.workspaces)
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
            self.access_var,
            self.profile_var,
            self.tunnel_id_var,
            self.tunnel_client_var,
            self.allow_tasks_var,
        ):
            variable.trace_add("write", self._on_form_changed)

    def _configure_window(self) -> None:
        self.root.title("FolderBridge MCP · 本地工作区连接器")
        self.root.configure(bg="#f4f6fa")
        self._dpi = window_dpi(self.root)
        self._ui_scale = scale_for_dpi(self._dpi)
        try:
            self.root.tk.call("tk", "scaling", tk_scaling_for_dpi(self._dpi))
        except tk.TclError:
            pass
        width, height = fitted_window_size(
            self._dpi,
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(
            min(self._px(820), width),
            min(self._px(700), height),
        )

    def _px(self, value: int | float) -> int:
        return scaled_pixels(value, self._ui_scale)

    def _schedule_dpi_refresh(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self.root or self._closing:
            return
        if self._dpi_refresh_id is not None:
            self.root.after_cancel(self._dpi_refresh_id)
        self._dpi_refresh_id = self.root.after(150, self._refresh_dpi)

    def _refresh_dpi(self) -> None:
        self._dpi_refresh_id = None
        if self._closing:
            return
        current_dpi = window_dpi(self.root)
        if current_dpi == self._dpi:
            return
        self._dpi = current_dpi
        self._ui_scale = scale_for_dpi(current_dpi)
        try:
            self.root.tk.call("tk", "scaling", tk_scaling_for_dpi(current_dpi))
        except tk.TclError:
            pass
        self._configure_styles()
        width, height = fitted_window_size(
            current_dpi,
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )
        self.root.minsize(
            min(self._px(820), width),
            min(self._px(700), height),
        )

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
        style.configure("Guide.TFrame", background="#ffffff")
        style.configure("GuideTitle.TLabel", background="#ffffff", foreground="#172033", font=("Segoe UI", 14, "bold"))
        style.configure("GuideStep.TLabel", background="#ffffff", foreground="#364157", font=("Segoe UI", 9))
        style.configure("GuideWarn.TLabel", background="#fff1f2", foreground="#b42318", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook", background="#ffffff", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(self._px(12), self._px(8)), font=("Segoe UI", 9, "bold"))
        style.configure("TEntry", padding=self._px(6))
        style.configure("TButton", padding=(self._px(10), self._px(7)), font=("Segoe UI", 9))
        style.configure("TRadiobutton", background="#ffffff", font=("Segoe UI", 9))
        style.configure("TCheckbutton", background="#ffffff", font=("Segoe UI", 9))
        style.configure("Workspace.Treeview", font=("Segoe UI", 9), rowheight=self._px(25))
        style.configure("Workspace.Treeview.Heading", font=("Segoe UI", 9, "bold"))

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
            text="把一个或多个明确的本地文件夹安全地接到支持 MCP 的 AI 客户端",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.guide_button = ttk.Button(header, text="连接设置向导", command=self._open_web_setup)
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

        dot_size = self._px(18)
        self.status_dot = tk.Canvas(card, width=dot_size, height=dot_size, bg="#ffffff", highlightthickness=0)
        self.status_dot.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 9), pady=(2, 0))
        inset = self._px(2)
        self.status_dot_id = self.status_dot.create_oval(
            inset,
            inset,
            dot_size - inset,
            dot_size - inset,
            fill="#98a2b3",
            outline="",
        )
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
            text="每个目录保持独立安全边界；链接、凭据和常见依赖目录会被拦截，重复或父子重叠目录不能同时添加。",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 12))

        ttk.Label(card, text="文件夹列表", style="Field.TLabel").grid(row=2, column=0, sticky="nw", padx=(0, 10))
        workspace_list = ttk.Frame(card, style="Card.TFrame")
        workspace_list.grid(row=2, column=1, sticky="ew")
        workspace_list.columnconfigure(0, weight=1)
        self.workspace_tree = ttk.Treeview(
            workspace_list,
            columns=("path", "workspace_id"),
            show="headings",
            height=3,
            selectmode="extended",
            style="Workspace.Treeview",
        )
        self.workspace_tree.heading("path", text="已允许的本地目录（最多 8 个）", anchor="w")
        self.workspace_tree.heading("workspace_id", text="Workspace ID", anchor="w")
        self.workspace_tree.column("path", anchor="w", stretch=True)
        self.workspace_tree.column("workspace_id", anchor="w", stretch=False, width=self._px(115))
        self.workspace_tree.grid(row=0, column=0, sticky="ew")
        workspace_scroll = ttk.Scrollbar(workspace_list, orient="vertical", command=self.workspace_tree.yview)
        workspace_scroll.grid(row=0, column=1, sticky="ns")
        self.workspace_tree.configure(yscrollcommand=workspace_scroll.set)
        workspace_buttons = ttk.Frame(card, style="Card.TFrame")
        workspace_buttons.grid(row=2, column=2, sticky="ns", padx=(8, 0))
        self.add_workspace_button = ttk.Button(workspace_buttons, text="添加文件夹…", command=self._add_workspace)
        self.add_workspace_button.pack(fill="x")
        self.remove_workspace_button = ttk.Button(workspace_buttons, text="移除选中", command=self._remove_workspaces)
        self.remove_workspace_button.pack(fill="x", pady=(8, 0))
        self._render_workspace_tree()

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
            text="读写（作用于列表全部目录；修改前仍需 ChatGPT 确认）",
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
        if not self.workspace_paths:
            self.workspace_status.set("未选择")
        elif len(self.workspace_paths) == 1:
            self.workspace_status.set(Path(self.workspace_paths[0]).name or "1 个目录")
        else:
            self.workspace_status.set(f"{len(self.workspace_paths)} 个独立目录")
        mode = self.access_var.get()
        if mode == "read_only":
            self.access_status.set("只读 · 安全")
        elif mode == "read_write":
            self.access_status.set("读写 · 需确认")
        else:
            self.access_status.set("未选择")

        raw_client = self.tunnel_client_var.get()
        executable = find_tunnel_client(raw_client)
        if self._is_runtime_client_path(raw_client):
            self.client_status.set("选错了 Runtime 内部组件；请下载完整 Windows amd64 包并选择 tunnel-client.exe。")
        elif executable:
            self.client_status.set(f"已找到：{executable}")
        else:
            self.client_status.set("未找到。ChatGPT 网页端可在“连接设置向导”中下载；本地 stdio 客户端不需要它。")

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
            workspaces=list(self.workspace_paths),
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

    def _render_workspace_tree(self) -> None:
        if not hasattr(self, "workspace_tree"):
            return
        self.workspace_tree.delete(*self.workspace_tree.get_children())
        for index, path in enumerate(self.workspace_paths):
            identifier = workspace_id(Path(path).expanduser().resolve(strict=False))
            self.workspace_tree.insert("", "end", iid=str(index), values=(path, identifier))

    def _add_workspace(self) -> None:
        initial = self.workspace_paths[-1] if self.workspace_paths else str(Path.cwd())
        selected = filedialog.askdirectory(title="添加允许 AI 访问的工作区", initialdir=initial)
        if not selected:
            return
        try:
            roots = canonical_workspaces([*self.workspace_paths, selected])
        except (OSError, ValueError) as exc:
            self._show_error(str(exc))
            return
        self.workspace_paths = [str(root) for root in roots]
        self._render_workspace_tree()
        self._refresh_status_cards()

    def _remove_workspaces(self) -> None:
        selected = {int(item) for item in self.workspace_tree.selection()}
        if not selected:
            return
        self.workspace_paths = [path for index, path in enumerate(self.workspace_paths) if index not in selected]
        self._render_workspace_tree()
        self._refresh_status_cards()

    def _browse_tunnel_client(self) -> None:
        selected = filedialog.askopenfilename(
            title="只选择完整包中的 tunnel-client.exe（不要选 Runtime）",
            filetypes=(("正确的 tunnel-client.exe", "tunnel-client.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.tunnel_client_var.set(selected)
            if self._is_runtime_client_path(selected):
                self._show_error(
                    "选错了 Runtime 内部组件。不要下载或选择 tunnel-client-runtime-*；"
                    "请下载完整的 tunnel-client-v<版本>-windows-amd64.zip，并选择其中准确名为 tunnel-client.exe 的文件。"
                )

    @staticmethod
    def _is_runtime_client_path(value: str) -> bool:
        name = Path(value.strip().strip('"')).name.lower()
        return name.startswith("tunnel-client-runtime-")

    def _require_tunnel_client(self, value: str) -> Path:
        if self._is_runtime_client_path(value):
            raise LauncherError(
                "选错了 Runtime 内部组件。请选择完整 Windows amd64 包中的 tunnel-client.exe；"
                "tunnel-client-runtime-* 不支持配置所需的 init 命令。"
            )
        executable = find_tunnel_client(value)
        if not executable:
            raise LauncherError(
                "没有找到官方 tunnel-client.exe。请下载完整的 tunnel-client-v<版本>-windows-amd64.zip；"
                "不要下载 tunnel-client-runtime-*。"
            )
        return executable

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
            workspaces = settings.validate(require_tunnel_id=True)
            executable = self._require_tunnel_client(settings.tunnel_client_path)
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
                    result = run_short_command(build_init_argv(executable, settings, workspaces), env=env, timeout_seconds=45)
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
            workspaces = settings.validate(require_tunnel_id=True)
            executable = self._require_tunnel_client(settings.tunnel_client_path)
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
                result = run_short_command(build_init_argv(executable, settings, workspaces), env=env, timeout_seconds=45)
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
            executable = self._require_tunnel_client(settings.tunnel_client_path)
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
            workspaces = settings.validate(require_tunnel_id=False)
            command = mcp_command(workspaces, settings.access_mode, settings.allow_tasks)
        except LauncherError as exc:
            self._show_error(str(exc))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self._log("本地 MCP 命令已复制到剪贴板。")

    def _open_web_setup(self) -> None:
        tunnel_id = self.tunnel_id_var.get().strip()
        executable = find_tunnel_client(self.tunnel_client_var.get())

        dialog = tk.Toplevel(self.root)
        dialog.title("FolderBridge · MCP 连接向导")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(True, True)
        dialog.configure(bg="#ffffff")
        width, height = fitted_window_size(
            self._dpi,
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
            base_width=860,
            base_height=670,
        )
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(min(self._px(720), width), min(self._px(560), height))

        body = ttk.Frame(dialog, style="Guide.TFrame", padding=(22, 18, 22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        header = ttk.Frame(body, style="Guide.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="MCP 连接向导", style="GuideTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(
            header,
            text="一键打开配置页面",
            command=lambda: self._open_required_pages(
                find_tunnel_client(self.tunnel_client_var.get()) is not None
            ),
        ).grid(row=0, column=1, padx=(12, 0))
        ttk.Button(header, text="关闭", command=dialog.destroy).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            body,
            text="ChatGPT 网页端按 1 → 4 完成；其他支持本地 stdio 的客户端直接看第 5 页。向导文字可拖选并用 Ctrl+C 复制。",
            style="Body.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 14))

        notebook = ttk.Notebook(body)
        notebook.grid(row=2, column=0, sticky="nsew")
        wrap = self._px(720)

        raw_client = self.tunnel_client_var.get()
        if self._is_runtime_client_path(raw_client):
            initial_client_status = "✗ 选错了 Runtime 内部组件；请重新下载完整包。"
        else:
            initial_client_status = f"✓ 已找到：{executable}" if executable else "尚未找到 tunnel-client.exe"
        client_status = tk.StringVar(value=initial_client_status)
        client_tab = self._guide_tab(
            notebook,
            "下载并解压官方客户端",
            (
                f"1. 点击“打开官方 Release”，展开 Assets，下载 {WINDOWS_X64_ASSET_PATTERN}。",
                "2. x64 在 Release 文件名中写作 amd64；绝大多数 Intel/AMD Windows 电脑都选它。",
                "3. 可同时下载 SHA256SUMS.txt 校验；把 ZIP 全部解压到固定目录，不要在压缩包里直接运行。",
                "4. 点击“选择已解压的 EXE”，只选准确名为 tunnel-client.exe 的主程序。",
            ),
            wrap,
            warnings_after_steps={
                1: "不要下载或选择 tunnel-client-runtime-*：它只是内部组件，不支持 init，必然配置失败。只下载完整的 tunnel-client-v<版本>-windows-amd64.zip。",
            },
        )
        notebook.add(client_tab, text="1  Windows x64 客户端")
        ttk.Label(client_tab, textvariable=client_status, style="Body.TLabel", wraplength=wrap).pack(anchor="w", pady=(8, 6))
        client_buttons = ttk.Frame(client_tab, style="Guide.TFrame")
        client_buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(client_buttons, text="打开官方 Release", command=lambda: webbrowser.open(TUNNEL_RELEASE_URL)).pack(side="left")
        ttk.Button(client_buttons, text="打开推荐解压目录", command=self._open_recommended_client_dir).pack(side="left", padx=(8, 0))
        ttk.Button(
            client_buttons,
            text="选择已解压的 EXE",
            command=lambda: self._choose_client_from_guide(client_status),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            client_buttons,
            text="复制应下载的文件名",
            command=lambda: self._copy_text(WINDOWS_X64_ASSET_PATTERN, "Windows x64 文件名已复制。"),
        ).pack(side="left", padx=(8, 0))

        platform_tab = self._guide_tab(
            notebook,
            "Platform Tunnel 各项这样填",
            (
                "1. 点击 Create tunnel。Name：FolderBridge；Description：Local FolderBridge MCP for private workspace access。",
                "2. Organizations：个人账号保持 Personal；团队账号选实际管理此 Tunnel 的 Platform organization。",
                "3. ChatGPT workspaces：必须选将要创建 App 的目标工作区；个人账号通常选择列表中的唯一 workspace ID。",
                "4. 创建后复制 tunnel_ 开头的 Tunnel ID。运行账号至少需 Tunnels Read + Use；创建/修改还需 Manage。",
                "5. 打开 Runtime API Keys，在同一 Platform organization 创建 Key；创建者需 Tunnels Read + Use。它只填在 FolderBridge。",
            ),
            wrap,
            warnings_after_steps={
                3: "只关联需要访问此本地工作区的 Organization / ChatGPT workspace，不要无范围地多选。",
            },
        )
        notebook.add(platform_tab, text="2  Platform Tunnel")
        platform_buttons = ttk.Frame(platform_tab, style="Guide.TFrame")
        platform_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(platform_buttons, text="打开 Tunnel 设置", command=lambda: webbrowser.open(TUNNEL_SETTINGS_URL)).pack(side="left")
        ttk.Button(platform_buttons, text="创建 Runtime API Key", command=lambda: webbrowser.open(RUNTIME_KEYS_URL)).pack(side="left", padx=(8, 0))
        ttk.Button(platform_buttons, text="打开官方说明", command=lambda: webbrowser.open(DOCS_URL)).pack(side="left", padx=(8, 0))

        local_tab = self._guide_tab(
            notebook,
            "FolderBridge 主界面这样填",
            (
                "1. 文件夹列表：逐个添加明确的工作区，最多 8 个；重复或父子重叠目录会被拒绝。全局权限首次使用保持“只读（推荐）”。",
                "2. tunnel-client：只选择完整包中准确名为 tunnel-client.exe 的主程序；不要选 tunnel-client-runtime-*。Profile 保持 folderbridge 即可。",
                "3. Tunnel ID：粘贴 Platform 中 tunnel_ 开头的 ID；Runtime API Key：粘贴控制面 Key（仅留内存，不保存）。",
                "4. “高级：允许任务”保持关闭。点击“启动连接”，等待顶部状态变成“运行中”。",
                "5. 使用期间必须保持 FolderBridge 运行；若失败，点“诊断”并查看脱敏日志。",
            ),
            wrap,
        )
        notebook.add(local_tab, text="3  启动 FolderBridge")
        local_buttons = ttk.Frame(local_tab, style="Guide.TFrame")
        local_buttons.pack(fill="x", pady=(10, 0))
        if looks_like_tunnel_id(tunnel_id):
            ttk.Button(
                local_buttons,
                text="复制当前 Tunnel ID",
                command=lambda: self._copy_text(tunnel_id, "Tunnel ID 已复制。"),
            ).pack(side="left")
        ttk.Label(
            local_buttons,
            text="当前已填写 Tunnel ID" if looks_like_tunnel_id(tunnel_id) else "当前还没有有效的 tunnel_ ID",
            style="Body.TLabel",
        ).pack(side="left", padx=(8, 0))

        chatgpt_tab = self._guide_tab(
            notebook,
            "ChatGPT 开发态 App：创建并在 Chat 中调用",
            (
                "1. 先在 ChatGPT 设置中启用开发者模式；团队工作区可能需要管理员授权。",
                "2. 打开 Plugins，点击 + 创建 App。Connection（连接方式）选择“隧道 / Tunnel”。",
                "3. Available Tunnel 选择同一个 Tunnel；若列表没有，粘贴 tunnel_ 开头的 ID，并回查 workspace 关联。",
                "4. Authentication（身份验证）选择“无身份验证 / No authentication”。确认风险提示后再创建。",
                "5. 创建后新建一个 Chat；点击输入框旁的 +，进入“更多 / More”，选择刚创建的 App。",
                f"6. 发送任务请求，例如：“{CHATGPT_INVOCATION_EXAMPLE}”。如 ChatGPT 显示工具确认，核对后再确认。FolderBridge 顶部必须保持“运行中”。",
            ),
            wrap,
            warnings_after_steps={
                4: "不要保留默认 OAuth，也不要把 https://tunnel-service... 地址当“服务器 URL”填写，否则会报 does not implement OAuth。",
            },
        )
        notebook.add(chatgpt_tab, text="4  ChatGPT 创建与调用")
        chatgpt_buttons = ttk.Frame(chatgpt_tab, style="Guide.TFrame")
        chatgpt_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(chatgpt_buttons, text="打开 ChatGPT Plugins", command=lambda: webbrowser.open(CHATGPT_PLUGINS_URL)).pack(side="left")
        ttk.Button(chatgpt_buttons, text="开发者模式说明", command=lambda: webbrowser.open(HELP_URL)).pack(side="left", padx=(8, 0))
        ttk.Button(
            chatgpt_buttons,
            text="复制 Chat 调用示例",
            command=lambda: self._copy_text(CHATGPT_INVOCATION_EXAMPLE, "ChatGPT 调用示例已复制。"),
        ).pack(side="left", padx=(8, 0))

        other_tab = self._guide_tab(
            notebook,
            "其他 MCP 客户端：优先直接连接 stdio",
            (
                "1. 兼容条件：客户端能启动本地程序，分别配置 command 与 args，并通过 stdin/stdout 使用 MCP 工具。",
                "2. 接入命令：FolderBridge.exe serve --workspace <路径1> --workspace <路径2> --read-only；每增加一个目录就重复一次 --workspace。通常由客户端自动启停，无需打开 GUI。",
                "3. 客户端使用 mcpServers 风格时复制 JSON；使用 mcp_servers 风格时复制 TOML；字段名仍以该客户端文档为准。",
                "4. 只支持远程 HTTP/SSE、网页或移动端且不能启动本地进程时，不能直接连接；需要该厂商的官方网关、Tunnel 或显式代理。",
                "5. 客户端是否弹出工具确认属于客户端行为。FolderBridge 仍强制文件夹边界、只读开关、哈希防冲突和任务白名单。",
            ),
            wrap,
            warnings_after_steps={
                2: "首次接入保持只读。",
                5: "不要因为客户端显示了确认弹窗，就把它当成 FolderBridge 的唯一安全边界。",
            },
        )
        notebook.add(other_tab, text="5  其他 MCP 客户端")
        other_buttons = ttk.Frame(other_tab, style="Guide.TFrame")
        other_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(
            other_buttons,
            text="复制 JSON 配置",
            command=lambda: self._copy_client_config("json"),
        ).pack(side="left")
        ttk.Button(
            other_buttons,
            text="复制 TOML 配置",
            command=lambda: self._copy_client_config("toml"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            other_buttons,
            text="复制 stdio 命令",
            command=lambda: self._copy_client_config("tunnel"),
        ).pack(side="left", padx=(8, 0))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"{dialog.winfo_width()}x{dialog.winfo_height()}+{x}+{y}")

    def _guide_tab(
        self,
        notebook: ttk.Notebook,
        title: str,
        steps: tuple[str, ...],
        wrap: int,
        *,
        warnings_after_steps: dict[int, str] | None = None,
    ) -> ttk.Frame:
        warnings = warnings_after_steps or {}
        invalid_steps = sorted(step_number for step_number in warnings if not 1 <= step_number <= len(steps))
        if invalid_steps:
            raise ValueError(f"警告对应的步骤号不存在：{invalid_steps}")

        tab = ttk.Frame(notebook, style="Guide.TFrame", padding=(20, 18, 20, 18))
        guide_text = scrolledtext.ScrolledText(
            tab,
            wrap="word",
            width=max(40, wrap // max(1, self._px(8))),
            height=14,
            borderwidth=0,
            relief="flat",
            highlightthickness=0,
            background="#ffffff",
            foreground="#364157",
            insertbackground="#172033",
            selectbackground="#c7d7fe",
            selectforeground="#172033",
            cursor="xterm",
            exportselection=True,
            padx=0,
            pady=0,
        )
        guide_text.tag_configure(
            "title",
            foreground="#172033",
            font=("Segoe UI", 11, "bold"),
            spacing3=self._px(12),
        )
        guide_text.tag_configure(
            "step",
            foreground="#364157",
            font=("Segoe UI", 9),
            spacing3=self._px(9),
        )
        guide_text.tag_configure(
            "warning",
            background="#fff1f2",
            foreground="#b42318",
            font=("Segoe UI", 9, "bold"),
            lmargin1=self._px(10),
            lmargin2=self._px(10),
            rmargin=self._px(10),
            spacing1=self._px(4),
            spacing3=self._px(8),
        )
        guide_text.insert("end", f"{title}\n", "title")
        for step_number, step in enumerate(steps, start=1):
            guide_text.insert("end", f"{step}\n", "step")
            warning = warnings.get(step_number, "").strip()
            if warning:
                guide_text.insert("end", f"注意：{warning}\n", "warning")
        guide_text.configure(state="disabled")
        guide_text.bind("<Control-a>", self._select_all_guide_text)
        guide_text.bind("<Control-A>", self._select_all_guide_text)
        guide_text.pack(fill="both", expand=True)
        tab.guide_text = guide_text  # type: ignore[attr-defined]
        return tab

    @staticmethod
    def _select_all_guide_text(event: tk.Event[tk.Misc]) -> str:
        widget = event.widget
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("insert")
        return "break"

    def _choose_client_from_guide(self, status: tk.StringVar) -> None:
        self._browse_tunnel_client()
        raw_client = self.tunnel_client_var.get()
        executable = find_tunnel_client(raw_client)
        if self._is_runtime_client_path(raw_client):
            status.set("✗ 选错了 Runtime 内部组件；请选择完整包中的 tunnel-client.exe。")
        else:
            status.set(f"✓ 已找到：{executable}" if executable else "尚未找到 tunnel-client.exe")

    def _open_recommended_client_dir(self) -> None:
        folder = recommended_client_directory()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(folder.as_uri())
            self._log(f"已打开推荐的官方客户端解压目录：{folder}")
        except OSError as exc:
            self._show_error(f"无法打开推荐目录：{exc}")

    def _copy_text(self, value: str, notice: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._log(notice)

    def _copy_client_config(self, output_format: str) -> None:
        try:
            settings = self._settings_from_form()
            workspaces = settings.validate(require_tunnel_id=False)
            rendered = render_client_config(
                workspaces,
                settings.access_mode,
                settings.allow_tasks,
                output_format,
            )
        except LauncherError as exc:
            self._show_error(str(exc))
            return
        self._copy_text(rendered, f"已复制 {output_format.upper()} 本地 stdio 客户端配置。")

    def _open_required_pages(self, client_ready: bool) -> None:
        webbrowser.open(TUNNEL_SETTINGS_URL)
        if not client_ready:
            webbrowser.open(TUNNEL_RELEASE_URL)
        webbrowser.open(RUNTIME_KEYS_URL)
        webbrowser.open(CHATGPT_PLUGINS_URL)
        self._log("已打开 OpenAI Tunnel 设置、Runtime API Keys、官方客户端下载页（如需要）和 ChatGPT Plugins。")

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
        for widget in (self.apply_button, self.doctor_button, self.copy_button):
            widget.configure(state="normal" if enabled else "disabled")
        self.guide_button.configure(state="normal")
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
            self.add_workspace_button,
            self.remove_workspace_button,
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
        self.workspace_tree.state(("!disabled",) if enabled else ("disabled",))

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


def main() -> int:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    FolderBridgeLauncher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
