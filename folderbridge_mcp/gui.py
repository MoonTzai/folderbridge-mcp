from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .capabilities import CAPABILITY_LABELS, CAPABILITY_NAMES
from .config import canonical_workspaces, workspace_id
from .extensions import ExtensionRegistry, extension_root_path
from .extension_spec import EXTENSION_FORMAT_SUMMARY, EXTENSION_LLM_PROMPT
from .dpi import (
    enable_windows_dpi_awareness,
    fitted_window_size,
    font_pixel_size,
    scale_for_dpi,
    scaled_pixels,
    tk_scaling_for_dpi,
    window_dpi,
    window_work_area,
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
from .managed_services import ManagedServiceError, default_managed_service_manager
from .security import ToolError
from .skills import SkillEngine, skill_pack_root_path
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
PYTHON_WINDOWS_URL = "https://www.python.org/downloads/windows/"
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
MAX_LOG_CHARS = 180_000
MANAGED_SERVICE_STATUS_POLL_MS = 2_000
DPI_FONT_SPECS: dict[str, tuple[str, int, str]] = {
    "title": ("Segoe UI", 19, "bold"),
    "subtitle": ("Segoe UI", 10, "normal"),
    "card_title": ("Segoe UI", 11, "bold"),
    "body": ("Segoe UI", 9, "normal"),
    "field": ("Segoe UI", 9, "bold"),
    "status": ("Segoe UI", 10, "bold"),
    "muted": ("Segoe UI", 8, "normal"),
    "service": ("Segoe UI", 8, "bold"),
    "guide_title": ("Segoe UI", 14, "bold"),
    "guide_step": ("Segoe UI", 9, "normal"),
    "guide_warning": ("Segoe UI", 9, "bold"),
    "tab": ("Segoe UI", 9, "bold"),
    "button": ("Segoe UI", 9, "normal"),
    "compact_button": ("Segoe UI", 8, "normal"),
    "tree": ("Segoe UI", 9, "normal"),
    "tree_heading": ("Segoe UI", 9, "bold"),
    "log": ("Cascadia Mono", 8, "normal"),
    "primary_button": ("Segoe UI", 11, "bold"),
}


class FolderBridgeLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = LauncherSettingsStore()
        self.settings = self.store.load()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1_000)
        self.supervisor = TunnelSupervisor(self._queue_tunnel_output)
        self.extension_registry = ExtensionRegistry()
        self.skill_engine = SkillEngine()
        self.managed_services = default_managed_service_manager()
        self._managed_service_states: dict[str, dict[str, object]] = {}
        self._managed_service_busy: set[str] = set()
        self._managed_service_prompted: set[str] = set()
        self._managed_service_startup_logged: set[str] = set()
        self._sidebar_visible = False
        self._collapsible_sections: dict[str, dict[str, object]] = {}
        self.extension_vars: dict[str, tk.BooleanVar] = {}
        self.skill_vars: dict[str, tk.BooleanVar] = {}
        self.managed_service_auto_vars: dict[str, tk.BooleanVar] = {}
        self.managed_service_status_labels: dict[str, ttk.Label] = {}
        self._managed_service_status_pending: set[str] = set()
        self._extension_wrapped_labels: list[ttk.Label] = []
        self._fonts: dict[str, tkfont.Font] = {}
        self._guide_text_widgets: list[tk.Text] = []
        self._busy = False
        self._closing = False
        self._shutdown_in_progress = False
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
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self.root.after_idle(self._fit_window_to_content)
        self._refresh_status_cards()
        self._set_connection_state("stopped")
        self._log("启动器已就绪。默认只读，不会保存 Runtime API Key。")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._schedule_dpi_refresh, add="+")
        self.root.after(120, self._drain_events)
        self.root.after(300, lambda: self._initialize_managed_services(final_pass=False))
        self.root.after(1800, lambda: self._initialize_managed_services(final_pass=True))
        self.root.after(400, self._poll_dpi)
        self.root.after(500, self._poll_process)
        self.root.after(MANAGED_SERVICE_STATUS_POLL_MS, self._poll_managed_service_statuses)

    def _create_variables(self) -> None:
        self.workspace_paths = list(self.settings.workspaces)
        self.access_var = tk.StringVar(value=self.settings.access_mode)
        self.profile_var = tk.StringVar(value=self.settings.profile)
        self.tunnel_id_var = tk.StringVar(value=self.settings.tunnel_id)
        self.tunnel_client_var = tk.StringVar(value=self.settings.tunnel_client_path)
        self.allow_tasks_var = tk.BooleanVar(value=self.settings.allow_tasks)
        enabled_capabilities = set(self.settings.capabilities)
        self.capability_vars = {
            name: tk.BooleanVar(value=name in enabled_capabilities)
            for name in CAPABILITY_NAMES
        }
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
            *self.capability_vars.values(),
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
        work_left, work_top, work_right, work_bottom = window_work_area(self.root)
        work_width = max(1, work_right - work_left)
        work_height = max(1, work_bottom - work_top)
        width, height = fitted_window_size(self._dpi, work_width, work_height)
        x = work_left + max(0, (work_width - width) // 2)
        y = work_top + max(0, (work_height - height) // 2)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.minsize(
            min(self._px(820), width),
            min(self._px(700), height),
        )

    def _px(self, value: int | float) -> int:
        return scaled_pixels(value, self._ui_scale)

    def _font(self, name: str) -> tkfont.Font:
        return self._fonts[name]

    def _refresh_fonts(self) -> None:
        for name, (family, point_size, weight) in DPI_FONT_SPECS.items():
            pixel_size = font_pixel_size(point_size, self._dpi)
            managed = self._fonts.get(name)
            if managed is None:
                self._fonts[name] = tkfont.Font(
                    root=self.root,
                    family=family,
                    size=pixel_size,
                    weight=weight,
                )
            else:
                managed.configure(family=family, size=pixel_size, weight=weight)

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
        if current_dpi != self._dpi:
            self._apply_dpi(current_dpi)

    def _poll_dpi(self) -> None:
        if self._closing:
            return
        current_dpi = window_dpi(self.root)
        if current_dpi != self._dpi:
            self._apply_dpi(current_dpi)
        self.root.after(400, self._poll_dpi)

    def _apply_dpi(self, current_dpi: int) -> None:
        self._dpi = current_dpi
        self._ui_scale = scale_for_dpi(current_dpi)
        try:
            self.root.tk.call("tk", "scaling", tk_scaling_for_dpi(current_dpi))
        except tk.TclError:
            pass
        self._configure_styles()
        work_left, work_top, work_right, work_bottom = window_work_area(self.root)
        width, height = fitted_window_size(
            current_dpi,
            max(1, work_right - work_left),
            max(1, work_bottom - work_top),
        )
        self.root.minsize(
            min(self._px(820), width),
            min(self._px(700), height),
        )
        self._refresh_dpi_metrics()
        self.root.after_idle(self._fit_window_to_content)

    def _refresh_dpi_metrics(self) -> None:
        if hasattr(self, "page"):
            self.page.configure(padding=(self._px(24), self._px(20), self._px(24), self._px(20)))
        if hasattr(self, "page_canvas"):
            self.root.after_idle(lambda: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")))
        if hasattr(self, "extension_sidebar"):
            self.extension_sidebar.configure(width=self._px(320), padding=self._px(14))
        if hasattr(self, "extension_sidebar_hint"):
            self.extension_sidebar_hint.configure(wraplength=self._px(285))
        if hasattr(self, "extension_canvas"):
            self.extension_canvas.configure(width=self._px(285))
        for label in getattr(self, "_extension_wrapped_labels", []):
            try:
                label.configure(wraplength=self._px(270))
            except tk.TclError:
                pass
        if hasattr(self, "workspace_tree"):
            self.workspace_tree.column("workspace_id", width=self._px(115))
        if hasattr(self, "status_dot"):
            dot_size = self._px(18)
            inset = self._px(2)
            self.status_dot.configure(width=dot_size, height=dot_size)
            self.status_dot.coords(self.status_dot_id, inset, inset, dot_size - inset, dot_size - inset)
            self.status_dot.grid_configure(padx=(0, self._px(9)), pady=(self._px(2), 0))
        if hasattr(self, "log"):
            self.log.configure(padx=self._px(10), pady=self._px(9))
        if hasattr(self, "start_button"):
            self.start_button.configure(padx=self._px(24), pady=self._px(9))
        for guide_text in getattr(self, "_guide_text_widgets", []):
            try:
                guide_text.tag_configure("title", spacing3=self._px(12))
                guide_text.tag_configure("step", spacing3=self._px(9))
                guide_text.tag_configure(
                    "warning",
                    lmargin1=self._px(10),
                    lmargin2=self._px(10),
                    rmargin=self._px(10),
                    spacing1=self._px(4),
                    spacing3=self._px(8),
                )
            except tk.TclError:
                pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        self._refresh_fonts()
        style.configure("Page.TFrame", background="#f4f6fa")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#f4f6fa", foreground="#172033", font=self._font("title"))
        style.configure("Subtitle.TLabel", background="#f4f6fa", foreground="#62708a", font=self._font("subtitle"))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#172033", font=self._font("card_title"))
        style.configure("Body.TLabel", background="#ffffff", foreground="#4d5a72", font=self._font("body"))
        style.configure("Field.TLabel", background="#ffffff", foreground="#364157", font=self._font("field"))
        style.configure("Status.TLabel", background="#ffffff", foreground="#172033", font=self._font("status"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#7a869c", font=self._font("muted"))
        style.configure("ServiceOnline.TLabel", background="#ffffff", foreground="#16803c", font=self._font("service"))
        style.configure("ServiceOffline.TLabel", background="#ffffff", foreground="#c62828", font=self._font("service"))
        style.configure("ServicePending.TLabel", background="#ffffff", foreground="#7a869c", font=self._font("service"))
        style.configure("Guide.TFrame", background="#ffffff")
        style.configure("GuideTitle.TLabel", background="#ffffff", foreground="#172033", font=self._font("guide_title"))
        style.configure("GuideStep.TLabel", background="#ffffff", foreground="#364157", font=self._font("guide_step"))
        style.configure("GuideWarn.TLabel", background="#fff1f2", foreground="#b42318", font=self._font("guide_warning"))
        style.configure("TNotebook", background="#ffffff", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(self._px(12), self._px(8)), font=self._font("tab"))
        style.configure("TEntry", padding=self._px(6), font=self._font("body"))
        style.configure("TButton", padding=(self._px(10), self._px(7)), font=self._font("button"))
        style.configure("Compact.TButton", padding=(self._px(6), self._px(2)), font=self._font("compact_button"))
        style.configure("TRadiobutton", background="#ffffff", font=self._font("body"))
        style.configure("TCheckbutton", background="#ffffff", font=self._font("body"))
        style.configure("Workspace.Treeview", font=self._font("tree"), rowheight=self._px(25))
        style.configure("Workspace.Treeview.Heading", font=self._font("tree_heading"))

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="Page.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)

        main = ttk.Frame(shell, style="Page.TFrame")
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.page_canvas = tk.Canvas(
            main,
            bg="#f4f6fa",
            highlightthickness=0,
            borderwidth=0,
        )
        self.page_scrollbar = ttk.Scrollbar(main, orient="vertical", command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        self.page_canvas.grid(row=0, column=0, sticky="nsew")
        self.page_scrollbar.grid(row=0, column=1, sticky="ns")

        self.page = ttk.Frame(
            self.page_canvas,
            style="Page.TFrame",
            padding=(self._px(24), self._px(20), self._px(24), self._px(20)),
        )
        page = self.page
        self._page_window_id = self.page_canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", self._on_page_content_configure, add="+")
        self.page_canvas.bind("<Configure>", self._resize_page_canvas, add="+")
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
        self.sections_toggle_button = ttk.Button(
            header,
            text="全部折叠",
            command=self._toggle_all_sections,
        )
        self.sections_toggle_button.grid(row=0, column=2, rowspan=2, sticky="e", padx=(8, 0))
        self.extension_toggle_button = ttk.Button(header, text="扩展与 Skills ▸", command=self._toggle_extension_sidebar)
        self.extension_toggle_button.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 0))

        self._build_overview(page).grid(row=1, column=0, sticky="ew", pady=(0, 12))

        local_card = self._build_local_settings(page)
        local_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self._register_collapsible_section("local", local_card, button_column=3)

        tunnel_card = self._build_tunnel_settings(page)
        tunnel_card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self._register_collapsible_section("tunnel", tunnel_card, button_column=4)

        log_card = self._build_log(page)
        log_card.grid(row=4, column=0, sticky="nsew", pady=(0, 12))
        self._register_collapsible_section(
            "log",
            log_card,
            button_parent=self.log_header_bar,
            button_column=2,
        )
        self._build_actions(page).grid(row=5, column=0, sticky="ew")

        self.extension_sidebar = self._build_extension_sidebar(shell)
        self.extension_sidebar.grid(row=0, column=1, sticky="ns", padx=(0, 12), pady=12)
        self.extension_sidebar.grid_remove()

    def _register_collapsible_section(
        self,
        key: str,
        card: ttk.Frame,
        *,
        button_column: int,
        button_parent: ttk.Frame | None = None,
    ) -> None:
        content_widgets: list[tk.Misc] = []
        for child in card.winfo_children():
            try:
                row = int(child.grid_info().get("row", 0))
            except (tk.TclError, TypeError, ValueError):
                continue
            if row >= 1:
                content_widgets.append(child)

        parent = button_parent or card
        button = ttk.Button(
            parent,
            text="收起 ▴",
            command=lambda section_key=key: self._toggle_section(section_key),
        )
        button.grid(row=0, column=button_column, sticky="e", padx=(8, 0))
        self._collapsible_sections[key] = {
            "card": card,
            "widgets": content_widgets,
            "button": button,
            "collapsed": False,
        }
        self._sync_sections_toggle_button()

    def _toggle_section(self, key: str) -> None:
        section = self._collapsible_sections.get(key)
        if section is None:
            return
        self._set_section_collapsed(key, not bool(section.get("collapsed")))

    def _set_section_collapsed(self, key: str, collapsed: bool) -> None:
        section = self._collapsible_sections.get(key)
        if section is None:
            return
        widgets = section.get("widgets", [])
        if isinstance(widgets, list):
            for widget in widgets:
                if not isinstance(widget, tk.Misc):
                    continue
                try:
                    if collapsed:
                        widget.grid_remove()
                    else:
                        widget.grid()
                except tk.TclError:
                    continue
        section["collapsed"] = bool(collapsed)
        button = section.get("button")
        if isinstance(button, ttk.Button):
            button.configure(text="展开 ▾" if collapsed else "收起 ▴")
        if key == "log":
            card = section.get("card")
            if isinstance(card, ttk.Frame):
                card.rowconfigure(1, weight=0 if collapsed else 1)
        self._sync_sections_toggle_button()
        self.root.after_idle(self._fit_window_to_content)

    def _toggle_all_sections(self) -> None:
        if not self._collapsible_sections:
            return
        collapse = any(not bool(section.get("collapsed")) for section in self._collapsible_sections.values())
        for key in tuple(self._collapsible_sections):
            self._set_section_collapsed(key, collapse)
        self._sync_sections_toggle_button()
        self.root.after_idle(self._fit_window_to_content)

    def _sync_sections_toggle_button(self) -> None:
        if not hasattr(self, "sections_toggle_button") or not self._collapsible_sections:
            return
        all_collapsed = all(bool(section.get("collapsed")) for section in self._collapsible_sections.values())
        self.sections_toggle_button.configure(text="全部展开" if all_collapsed else "全部折叠")

    def _on_page_content_configure(self, _event: tk.Event[tk.Misc]) -> None:
        if not hasattr(self, "page_canvas"):
            return
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))
        self.root.after_idle(self._update_page_scrollbar)

    def _resize_page_canvas(self, event: tk.Event[tk.Misc]) -> None:
        if not hasattr(self, "_page_window_id"):
            return
        self.page_canvas.itemconfigure(self._page_window_id, width=max(1, event.width))
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))
        self.root.after_idle(self._update_page_scrollbar)

    @staticmethod
    def _is_descendant_of(widget: tk.Misc, ancestor: tk.Misc) -> bool:
        current: tk.Misc | None = widget
        while current is not None:
            if current is ancestor:
                return True
            current = current.master
        return False

    def _has_independent_wheel_scroll(self, widget: tk.Misc) -> bool:
        current: tk.Misc | None = widget
        native_scrollers = (tk.Text, tk.Listbox, tk.Spinbox, ttk.Treeview, ttk.Combobox, ttk.Scrollbar)
        while current is not None and current is not self.page_canvas:
            if isinstance(current, native_scrollers):
                return True
            if isinstance(current, tk.Canvas):
                try:
                    if str(current.cget("yscrollcommand")):
                        return True
                except tk.TclError:
                    return True
            current = current.master
        return False

    @staticmethod
    def _mousewheel_units(event: tk.Event[tk.Misc]) -> int:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta > 0:
            return -3
        if delta < 0:
            return 3
        button = getattr(event, "num", None)
        if button == 4:
            return -3
        if button == 5:
            return 3
        return 0

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str | None:
        if self._closing:
            return None
        units = self._mousewheel_units(event)
        if units == 0 or not isinstance(event.widget, tk.Misc):
            return None
        widget = event.widget
        if hasattr(self, "extension_canvas") and self._is_descendant_of(widget, self.extension_canvas):
            self.extension_canvas.yview_scroll(units, "units")
            return "break"
        if not hasattr(self, "page_canvas") or not self._is_descendant_of(widget, self.page_canvas):
            return None
        if self._has_independent_wheel_scroll(widget):
            return None
        self.page_canvas.yview_scroll(units, "units")
        return "break"

    def _update_page_scrollbar(self) -> None:
        if self._closing or not hasattr(self, "page_scrollbar") or not hasattr(self, "page"):
            return
        try:
            self.root.update_idletasks()
            content_height = self.page.winfo_reqheight()
            viewport_height = self.page_canvas.winfo_height()
            needed = content_height > viewport_height + self._px(2)
            if needed:
                self.page_scrollbar.grid()
            else:
                self.page_canvas.yview_moveto(0.0)
                self.page_scrollbar.grid_remove()
        except tk.TclError:
            return

    def _fit_window_to_content(self) -> None:
        """Fit requested content inside the current monitor work area; scroll when capped."""
        if self._closing or not hasattr(self, "page"):
            return
        try:
            self.root.update_idletasks()
            work_left, work_top, work_right, work_bottom = window_work_area(self.root)
            work_width = max(1, work_right - work_left)
            work_height = max(1, work_bottom - work_top)
            max_width = max(1, round(work_width * 0.94))
            max_height = max(1, round(work_height * 0.90))
            content_width = max(self._px(940), self.page.winfo_reqwidth())
            content_height = max(self._px(820), self.page.winfo_reqheight() + self._px(8))
            if self._sidebar_visible and hasattr(self, "extension_sidebar"):
                content_width += self._px(332)
            width = min(content_width, max_width)
            height = min(content_height, max_height)
            current_x = int(self.root.winfo_x())
            current_y = int(self.root.winfo_y())
            max_x = max(work_left, work_right - width)
            max_y = max(work_top, work_bottom - height)
            x = min(max(current_x, work_left), max_x)
            y = min(max(current_y, work_top), max_y)
            self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self.root.minsize(min(self._px(820), width), min(self._px(700), height))
            self.root.after_idle(self._update_page_scrollbar)
        except tk.TclError:
            return

    def _build_extension_sidebar(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=self._px(14), width=self._px(320))
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        header = ttk.Frame(frame, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Extensions & Skills", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="重新扫描", command=self._rescan_extensions).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(header, text="插件目录", command=self._open_extension_folder).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(header, text="Skill 目录", command=self._open_skill_folder).grid(row=0, column=3, padx=(6, 0))
        self.extension_sidebar_hint = ttk.Label(
            frame,
            text="默认折叠 · 热扫描 · Extensions 与 Skill Packs 分开授权；新增 Skill 不会新增 MCP tool。",
            style="Muted.TLabel",
            wraplength=self._px(285),
        )
        self.extension_sidebar_hint.grid(row=1, column=0, sticky="w", pady=(self._px(5), self._px(9)))

        container = ttk.Frame(frame, style="Card.TFrame")
        container.grid(row=2, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        canvas = tk.Canvas(container, bg="#ffffff", highlightthickness=0, width=self._px(285))
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        inner = ttk.Frame(canvas, style="Card.TFrame")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        self.extension_canvas = canvas
        self.extension_list_frame = inner
        self._refresh_extension_sidebar()
        return frame

    def _rescan_extensions(self) -> None:
        self._refresh_extension_sidebar()
        self._refresh_managed_service_statuses_async()

    def _toggle_extension_sidebar(self) -> None:
        self._sidebar_visible = not self._sidebar_visible
        if self._sidebar_visible:
            self._refresh_extension_sidebar()
            self._refresh_managed_service_statuses_async()
            self.extension_sidebar.grid()
            self.extension_toggle_button.configure(text="扩展与 Skills ◂")
        else:
            self.extension_sidebar.grid_remove()
            self.extension_toggle_button.configure(text="扩展与 Skills ▸")
        self.root.after_idle(self._fit_window_to_content)

    def _refresh_extension_sidebar(self) -> None:
        if not hasattr(self, "extension_list_frame"):
            return
        for child in self.extension_list_frame.winfo_children():
            child.destroy()
        self.extension_vars = {}
        self.skill_vars = {}
        self.managed_service_auto_vars = {}
        self.managed_service_status_labels = {}
        self._extension_wrapped_labels = []
        description = self.extension_registry.describe()
        extensions = description.get("extensions", [])
        if not extensions:
            empty_label = ttk.Label(
                self.extension_list_frame,
                text="未发现插件。把符合 ABI v1 的插件文件夹放入插件目录后点“重新扫描”。",
                style="Body.TLabel",
                wraplength=self._px(270),
            )
            empty_label.pack(fill="x", pady=(2, 8))
            self._extension_wrapped_labels.append(empty_label)
        for item in extensions:
            extension_id = str(item["id"])
            card = ttk.Frame(self.extension_list_frame, style="Card.TFrame", padding=(0, 4, 0, 8))
            card.pack(fill="x")
            var = tk.BooleanVar(value=bool(item.get("enabled")))
            self.extension_vars[extension_id] = var
            check = ttk.Checkbutton(
                card,
                text=f"{item['name']}  {item['version']}",
                variable=var,
                command=lambda eid=extension_id: self._toggle_extension_enabled(eid),
            )
            check.pack(anchor="w")
            if item.get("approval_stale"):
                status = "⚠ 文件/权限已变化 · 需重新批准"
            elif item.get("loaded"):
                status = "✓ 已批准 · 已加载"
            elif item.get("trusted"):
                status = "已批准 · 未加载"
            elif item.get("bundled"):
                status = "随 FolderBridge 提供 · 执行动作待批准"
            else:
                status = "未批准"
            status_label = ttk.Label(card, text=status, style="Muted.TLabel", wraplength=self._px(270))
            status_label.pack(anchor="w", padx=(22, 0))
            self._extension_wrapped_labels.append(status_label)
            permissions = item.get("permissions", [])
            if permissions:
                permissions_label = ttk.Label(
                    card,
                    text="权限：" + " · ".join(str(value) for value in permissions),
                    style="Muted.TLabel",
                    wraplength=self._px(270),
                )
                permissions_label.pack(anchor="w", padx=(22, 0), pady=(2, 0))
                self._extension_wrapped_labels.append(permissions_label)

            controller = self.managed_services.controller(extension_id)
            if controller is not None:
                cached = self._managed_service_states.get(extension_id)
                config = controller.config()
                service_state = cached or {
                    "online": False,
                    "owned": False,
                    "external": False,
                    "install_root": config.install_root,
                    "auto_start": config.auto_start,
                    "detail": "尚未检测",
                }
                online = bool(service_state.get("online"))
                owned = bool(service_state.get("owned"))
                busy = extension_id in self._managed_service_busy
                install_root = str(service_state.get("install_root") or config.install_root or "")
                service_text, service_style = self._managed_service_status_presentation(
                    extension_id,
                    service_state,
                    config,
                    cached=cached is not None,
                )
                service_label = ttk.Label(card, text=service_text, style=service_style, wraplength=self._px(270))
                self.managed_service_status_labels[extension_id] = service_label
                service_label.pack(anchor="w", padx=(22, 0), pady=(4, 0))
                self._extension_wrapped_labels.append(service_label)
                path_label = ttk.Label(
                    card,
                    text=f"ComfyUI：{install_root or '未选择（首次需配置）'}",
                    style="Muted.TLabel",
                    wraplength=self._px(270),
                )
                path_label.pack(anchor="w", padx=(22, 0), pady=(2, 0))
                self._extension_wrapped_labels.append(path_label)
                auto_var = tk.BooleanVar(value=bool(service_state.get("auto_start", config.auto_start)))
                self.managed_service_auto_vars[extension_id] = auto_var
                service_controls = ttk.Frame(card, style="Card.TFrame")
                service_controls.pack(fill="x", padx=(22, 0), pady=(4, 0))
                ttk.Checkbutton(
                    service_controls,
                    text="自动启动",
                    variable=auto_var,
                    command=lambda eid=extension_id: self._toggle_managed_service_auto_start(eid),
                ).pack(side="left")
                choose_button = ttk.Button(
                    service_controls,
                    text="选择目录…",
                    command=lambda eid=extension_id: self._select_managed_service_directory(eid),
                )
                choose_button.pack(side="left", padx=(6, 0))
                start_button = ttk.Button(
                    service_controls,
                    text="启动",
                    command=lambda eid=extension_id: self._start_managed_service(eid),
                )
                start_button.pack(side="left", padx=(6, 0))
                stop_button = ttk.Button(
                    service_controls,
                    text="停止",
                    command=lambda eid=extension_id: self._stop_managed_service(eid),
                )
                stop_button.pack(side="left", padx=(6, 0))
                if busy or online or owned or not config.install_root or not item.get("loaded"):
                    start_button.configure(state="disabled")
                if busy or not owned:
                    stop_button.configure(state="disabled")

            buttons = ttk.Frame(card, style="Card.TFrame")
            buttons.pack(anchor="e", pady=(3, 0))
            ttk.Button(
                buttons,
                text="详情",
                command=lambda eid=extension_id: self._show_extension_details(eid),
            ).pack(side="left")
            if item.get("trusted") or item.get("approval_stale"):
                ttk.Button(
                    buttons,
                    text="撤销批准",
                    command=lambda eid=extension_id: self._revoke_extension(eid),
                ).pack(side="left", padx=(6, 0))
            ttk.Separator(self.extension_list_frame, orient="horizontal").pack(fill="x", pady=(0, 5))
        for error in description.get("errors", []):
            error_label = ttk.Label(
                self.extension_list_frame,
                text=f"加载失败：{error.get('path', '')}\n{error.get('error', '')}",
                style="GuideWarn.TLabel",
                wraplength=self._px(270),
            )
            error_label.pack(fill="x", pady=(3, 6))
            self._extension_wrapped_labels.append(error_label)
        self._render_skill_pack_section()
        self.root.after_idle(lambda: self.extension_canvas.configure(scrollregion=self.extension_canvas.bbox("all")))

    def _render_skill_pack_section(self) -> None:
        ttk.Separator(self.extension_list_frame, orient="horizontal").pack(fill="x", pady=(8, 8))
        ttk.Label(self.extension_list_frame, text="Skill Packs", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 4))
        skill_hint = ttk.Label(
            self.extension_list_frame,
            text="Skill 是按需加载的方法文本，不执行本地代码；外部 Pack 需核对精确 hash 后批准。",
            style="Muted.TLabel",
            wraplength=self._px(270),
        )
        skill_hint.pack(fill="x", pady=(0, 6))
        self._extension_wrapped_labels.append(skill_hint)
        description = self.skill_engine.describe(include_untrusted=True)
        packs = description.get("packs", [])
        if not packs:
            empty = ttk.Label(
                self.extension_list_frame,
                text="未发现 Skill Pack。可把符合格式的 Pack 放入 Skill 目录后重新扫描。",
                style="Body.TLabel",
                wraplength=self._px(270),
            )
            empty.pack(fill="x", pady=(2, 8))
            self._extension_wrapped_labels.append(empty)
        for item in packs:
            pack_id = str(item["id"])
            card = ttk.Frame(self.extension_list_frame, style="Card.TFrame", padding=(0, 4, 0, 8))
            card.pack(fill="x")
            var = tk.BooleanVar(value=bool(item.get("enabled")))
            self.skill_vars[pack_id] = var
            ttk.Checkbutton(
                card,
                text=f"{item['name']}  {item['version']}",
                variable=var,
                command=lambda pid=pack_id, current=item: self._toggle_skill_enabled(pid, current),
            ).pack(anchor="w")
            if item.get("approval_stale"):
                status = "⚠ 内容已变化 · 需重新核对 hash"
            elif item.get("bundled") and item.get("enabled"):
                status = "✓ 内置 · 已启用"
            elif item.get("bundled"):
                status = "内置 · 已停用"
            elif item.get("trusted") and item.get("enabled"):
                status = "✓ 已批准 · 已启用"
            elif item.get("trusted"):
                status = "已批准 · 已停用"
            else:
                status = "未批准"
            status_label = ttk.Label(card, text=status, style="Muted.TLabel", wraplength=self._px(270))
            status_label.pack(anchor="w", padx=(22, 0))
            self._extension_wrapped_labels.append(status_label)
            meta_label = ttk.Label(
                card,
                text=f"{item.get('skill_count', 0)} Skills · SHA-256 {str(item['sha256'])[:12]}…",
                style="Muted.TLabel",
                wraplength=self._px(270),
            )
            meta_label.pack(anchor="w", padx=(22, 0), pady=(2, 0))
            self._extension_wrapped_labels.append(meta_label)
            buttons = ttk.Frame(card, style="Card.TFrame")
            buttons.pack(anchor="e", pady=(3, 0))
            ttk.Button(
                buttons,
                text="详情",
                command=lambda current=item: self._show_skill_pack_details(current),
            ).pack(side="left")
            if not item.get("bundled") and (item.get("trusted") or item.get("approval_stale")):
                ttk.Button(
                    buttons,
                    text="撤销批准",
                    command=lambda pid=pack_id: self._revoke_skill_pack(pid),
                ).pack(side="left", padx=(6, 0))
        for error in description.get("errors", []):
            error_label = ttk.Label(
                self.extension_list_frame,
                text=f"Skill Pack 加载失败：{error.get('path', '')}\n{error.get('error', '')}",
                style="GuideWarn.TLabel",
                wraplength=self._px(270),
            )
            error_label.pack(fill="x", pady=(3, 6))
            self._extension_wrapped_labels.append(error_label)

    def _toggle_skill_enabled(self, pack_id: str, item: dict[str, object]) -> None:
        var = self.skill_vars.get(pack_id)
        if var is None:
            return
        try:
            if var.get():
                if not bool(item.get("trusted")):
                    source = item.get("source") if isinstance(item.get("source"), dict) else {}
                    source = source if isinstance(source, dict) else {}
                    source_lines = [
                        value
                        for value in (
                            str(source.get("repository") or ""),
                            str(source.get("ref") or ""),
                            str(source.get("license") or ""),
                        )
                        if value
                    ]
                    source_summary = " · ".join(source_lines) or "未声明"
                    skills = item.get("skills", [])
                    skill_names = "\n".join(
                        f"• {entry.get('name', entry.get('id', ''))}"
                        for entry in skills
                        if isinstance(entry, dict)
                    ) or "• 未声明 Skill"
                    warning = (
                        f"批准并启用 Skill Pack：{item.get('name', pack_id)} {item.get('version', '')}\n\n"
                        f"SHA-256：{item['sha256']}\n"
                        f"来源：{source_summary}\n\n包含：\n{skill_names}\n\n"
                        "Skill 文本不会执行本地代码，但会影响模型的方法选择和行为。\n"
                        "任何 Pack 文件发生变化后，本批准会自动失效。"
                    )
                    if not messagebox.askyesno("批准 FolderBridge Skill Pack", warning, parent=self.root):
                        var.set(False)
                        return
                    self.skill_engine.approve_pack(pack_id, item["sha256"])
                self.skill_engine.set_enabled(pack_id, True)
                self._log(f"Skill Pack 已启用：{item.get('name', pack_id)}")
            else:
                self.skill_engine.set_enabled(pack_id, False)
                self._log(f"Skill Pack 已停用：{item.get('name', pack_id)}")
        except (ToolError, OSError, ValueError) as exc:
            var.set(bool(item.get("enabled")))
            self._show_error(f"无法更新 Skill Pack：{exc}")
        finally:
            self._refresh_extension_sidebar()

    def _revoke_skill_pack(self, pack_id: str) -> None:
        if not messagebox.askyesno(
            "撤销 Skill Pack 批准",
            "撤销该外部 Skill Pack 的本机批准记录并立即停用？之后再次启用时需要重新核对精确 hash。",
            parent=self.root,
        ):
            return
        try:
            self.skill_engine.revoke_pack(pack_id)
            self._log(f"已撤销 Skill Pack 批准：{pack_id}")
        except (ToolError, OSError, ValueError) as exc:
            self._show_error(f"无法撤销 Skill Pack 批准：{exc}")
        finally:
            self._refresh_extension_sidebar()

    def _show_skill_pack_details(self, item: dict[str, object]) -> None:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source = source if isinstance(source, dict) else {}
        skills = item.get("skills", [])
        skill_lines = "\n".join(
            f"• {entry.get('name', entry.get('id', ''))}"
            for entry in skills
            if isinstance(entry, dict)
        ) or "• 无"
        source_lines = [
            value
            for value in (
                str(source.get("repository") or ""),
                str(source.get("ref") or ""),
                str(source.get("license") or ""),
            )
            if value
        ]
        messagebox.showinfo(
            "Skill Pack 详情",
            f"{item.get('name', '')} {item.get('version', '')}\n"
            f"ID: {item.get('id', '')}\n"
            f"Bundled: {item.get('bundled', False)}\n"
            f"Trusted: {item.get('trusted', False)} · Enabled: {item.get('enabled', False)}\n"
            f"SHA-256: {item.get('sha256', '')}\n"
            f"来源：{' · '.join(source_lines) or '未声明'}\n\n"
            f"Skills：\n{skill_lines}\n\n{item.get('description', '')}",
            parent=self.root,
        )

    def _open_skill_folder(self) -> None:
        folder = skill_pack_root_path()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(folder.as_uri())
            self._log(f"已打开 Skill Pack 目录：{folder}")
        except OSError as exc:
            self._show_error(f"无法打开 Skill Pack 目录：{exc}")

    def _managed_service_status_presentation(
        self,
        extension_id: str,
        service_state: dict[str, object],
        config: object,
        *,
        cached: bool = True,
    ) -> tuple[str, str]:
        online = bool(service_state.get("online"))
        owned = bool(service_state.get("owned"))
        external = bool(service_state.get("external"))
        busy = extension_id in self._managed_service_busy
        install_root = str(service_state.get("install_root") or getattr(config, "install_root", "") or "")
        auto_start = bool(service_state.get("auto_start", getattr(config, "auto_start", True)))
        if busy and not online:
            return "服务：正在启动 / 检测…", "ServicePending.TLabel"
        if not cached:
            return "服务：检测中…", "ServicePending.TLabel"
        if online and owned:
            return "服务：在线 · FolderBridge 托管", "ServiceOnline.TLabel"
        if online and external:
            return "服务：在线 · 外部服务（不会被 FolderBridge 终止）", "ServiceOnline.TLabel"
        if not install_root:
            return "服务：等待配置安装目录 · 自动启动尚未执行", "ServiceOffline.TLabel"
        if not auto_start:
            return "服务：离线 · 自动启动已关闭", "ServiceOffline.TLabel"
        return "服务：离线", "ServiceOffline.TLabel"

    def _update_managed_service_status_label(self, extension_id: str) -> None:
        label = self.managed_service_status_labels.get(extension_id)
        controller = self.managed_services.controller(extension_id)
        state = self._managed_service_states.get(extension_id)
        if label is None or controller is None or state is None:
            return
        text, style = self._managed_service_status_presentation(extension_id, state, controller.config())
        try:
            label.configure(text=text, style=style)
        except tk.TclError:
            self.managed_service_status_labels.pop(extension_id, None)

    def _toggle_extension_enabled(self, extension_id: str) -> None:
        var = self.extension_vars.get(extension_id)
        if var is None:
            return
        try:
            record = self.extension_registry.get(extension_id)
            status = self.extension_registry.trust_store.status(record)
            if var.get():
                if not status["trusted"]:
                    permissions = "\n".join(f"• {permission}" for permission in record.manifest.permissions) or "• 无额外权限声明"
                    warning = (
                        f"批准并全局加载扩展：{record.manifest.name} {record.manifest.version}\n\n"
                        f"SHA-256：{record.sha256}\n\n请求权限：\n{permissions}\n\n"
                        "插件代码会在独立子进程运行，但这不是完整 OS 沙箱。来源不可信的插件应放入 VM/容器。\n"
                        "任一插件文件或权限发生变化后，本批准会自动失效。"
                    )
                    if not messagebox.askyesno("批准 FolderBridge Extension", warning, parent=self.root):
                        var.set(False)
                        return
                    self.extension_registry.trust_store.approve(record, enabled=True)
                else:
                    self.extension_registry.trust_store.set_enabled(record, True)
                self._log(f"扩展已全局加载：{record.manifest.name}")
                self._ensure_managed_service_async(extension_id)
            else:
                if self.managed_services.controller(extension_id) is not None:
                    var.set(True)
                    self._disable_or_revoke_extension_async(extension_id, revoke=False)
                    return
                self.extension_registry.trust_store.set_enabled(record, False)
                self._log(f"扩展已停用：{record.manifest.name}（批准记录保留）")
        except (OSError, ValueError) as exc:
            var.set(False)
            self._show_error(f"无法更新扩展授权：{exc}")
        finally:
            self._refresh_extension_sidebar()

    def _revoke_extension(self, extension_id: str) -> None:
        try:
            record = self.extension_registry.get(extension_id)
        except (OSError, ValueError) as exc:
            self._show_error(f"无法读取扩展：{exc}")
            return
        if not messagebox.askyesno(
            "撤销 Extension 批准",
            f"撤销 {record.manifest.name} 的本机批准记录并立即停用？\n\n之后再次启用时需要重新核对 hash 与权限。",
            parent=self.root,
        ):
            return
        if self.managed_services.controller(extension_id) is not None:
            self._disable_or_revoke_extension_async(extension_id, revoke=True)
            return
        try:
            self.extension_registry.trust_store.revoke(extension_id)
            self._log(f"已撤销扩展批准：{record.manifest.name}")
        except OSError as exc:
            self._show_error(f"无法撤销扩展批准：{exc}")
        finally:
            self._refresh_extension_sidebar()

    def _show_extension_details(self, extension_id: str) -> None:
        try:
            record = self.extension_registry.get(extension_id)
            status = self.extension_registry.trust_store.status(record)
        except (OSError, ValueError) as exc:
            self._show_error(f"无法读取扩展：{exc}")
            return
        actions = "\n".join(
            f"• {action.name} · auth={action.authorization} · workspace={action.requires_workspace}"
            for action in record.manifest.actions.values()
        )
        permissions = "\n".join(f"• {value}" for value in record.manifest.permissions) or "• 无"
        messagebox.showinfo(
            "Extension 详情",
            f"{record.manifest.name} {record.manifest.version}\n"
            f"ID: {record.manifest.extension_id}\n"
            f"Bundled: {record.bundled}\n"
            f"Trusted: {status['trusted']} · Enabled: {status['enabled']}\n"
            f"SHA-256: {record.sha256}\n\n权限：\n{permissions}\n\n动作：\n{actions}\n\n"
            f"{record.manifest.description}",
            parent=self.root,
        )

    def _open_extension_folder(self) -> None:
        folder = extension_root_path()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(folder.as_uri())
            self._log(f"已打开 Extension 目录：{folder}")
        except OSError as exc:
            self._show_error(f"无法打开 Extension 目录：{exc}")

    def _loaded_extension_ids(self) -> tuple[str, ...]:
        description = self.extension_registry.describe()
        return tuple(str(item["id"]) for item in description.get("extensions", []) if item.get("loaded"))

    def _initialize_managed_services(self, *, final_pass: bool = True) -> None:
        if self._closing:
            return
        loaded = set(self._loaded_extension_ids())
        description = self.extension_registry.describe()
        managed_items = [
            item
            for item in description.get("extensions", [])
            if self.managed_services.controller(str(item.get("id", ""))) is not None
        ]
        for item in managed_items:
            extension_id = str(item["id"])
            controller = self.managed_services.controller(extension_id)
            if controller is None:
                continue
            if extension_id not in loaded:
                if final_pass and extension_id not in self._managed_service_startup_logged:
                    self._managed_service_startup_logged.add(extension_id)
                    self._log(
                        f"托管服务自动启动未执行：{extension_id} Extension 当前未加载；"
                        "请确认已批准并启用该 Extension。"
                    )
                continue
            config = controller.config()
            if extension_id not in self._managed_service_startup_logged:
                self._managed_service_startup_logged.add(extension_id)
                if not config.install_root:
                    self._log(f"托管服务自动启动检查：{extension_id} 尚未配置安装目录。")
                elif not config.auto_start:
                    self._log(f"托管服务自动启动检查：{extension_id} 自动启动已关闭。")
                else:
                    self._log(
                        f"托管服务自动启动检查：{extension_id} · {config.install_root} · 正在检查/启动…"
                    )
            self._ensure_managed_service_async(extension_id)

    def _refresh_managed_service_statuses_async(self) -> None:
        if self._closing:
            return
        for extension_id in self._loaded_extension_ids():
            if self.managed_services.controller(extension_id) is not None:
                self._run_managed_service_action(extension_id, "status")

    def _poll_managed_service_statuses(self) -> None:
        if self._closing:
            return
        if self._sidebar_visible:
            self._refresh_managed_service_statuses_async()
        self.root.after(MANAGED_SERVICE_STATUS_POLL_MS, self._poll_managed_service_statuses)

    def _ensure_managed_service_async(self, extension_id: str) -> None:
        if self.managed_services.controller(extension_id) is not None:
            self._run_managed_service_action(extension_id, "ensure")

    def _start_managed_service(self, extension_id: str) -> None:
        self._run_managed_service_action(extension_id, "start")

    def _stop_managed_service(self, extension_id: str) -> None:
        self._run_managed_service_action(extension_id, "stop")

    def _run_managed_service_action(self, extension_id: str, action: str) -> None:
        controller = self.managed_services.controller(extension_id)
        if controller is None or self._closing:
            return
        if extension_id in self._managed_service_busy:
            return
        is_probe = action == "status"
        if is_probe:
            if extension_id in self._managed_service_status_pending:
                return
            self._managed_service_status_pending.add(extension_id)
        else:
            self._managed_service_busy.add(extension_id)
            self._refresh_extension_sidebar()

        def work() -> None:
            try:
                if action == "ensure":
                    state = controller.ensure_auto_started()
                elif action == "status":
                    state = controller.status()
                elif action == "start":
                    state = controller.start()
                elif action == "stop":
                    state = controller.stop()
                else:
                    raise ValueError(f"unknown managed service action: {action}")
                self._queue_event("managed-service-state", (extension_id, action, state))
                if action == "ensure" and state.get("reason") == "path-required":
                    self._queue_event("managed-service-path-required", extension_id)
            except (ManagedServiceError, OSError, ValueError) as exc:
                self._queue_event("managed-service-error", (extension_id, str(exc)))
            finally:
                self._queue_event("managed-service-probe-idle" if is_probe else "managed-service-idle", extension_id)

        threading.Thread(
            target=work,
            name=f"folderbridge-service-{extension_id}-{action}",
            daemon=True,
        ).start()

    def _select_managed_service_directory(self, extension_id: str) -> None:
        controller = self.managed_services.controller(extension_id)
        if controller is None:
            return
        current = controller.config().install_root
        selected = filedialog.askdirectory(
            title="选择 ComfyUI 安装目录",
            initialdir=current or str(Path.home()),
            parent=self.root,
        )
        if not selected:
            return
        try:
            install = controller.configure_install(selected, auto_start=True)
        except (ManagedServiceError, OSError, ValueError) as exc:
            self._show_error(str(exc))
            return
        self._managed_service_states[extension_id] = {
            **self._managed_service_states.get(extension_id, {}),
            "online": False,
            "owned": False,
            "external": False,
            "install_root": str(install.install_root),
            "auto_start": True,
        }
        self._log(f"已保存 ComfyUI 安装目录：{install.install_root}")
        self._refresh_extension_sidebar()
        self._start_managed_service(extension_id)

    def _prompt_managed_service_path(self, extension_id: str) -> None:
        if extension_id in self._managed_service_prompted or self._closing:
            return
        self._managed_service_prompted.add(extension_id)
        self._select_managed_service_directory(extension_id)

    def _toggle_managed_service_auto_start(self, extension_id: str) -> None:
        controller = self.managed_services.controller(extension_id)
        variable = self.managed_service_auto_vars.get(extension_id)
        if controller is None or variable is None:
            return
        try:
            config = controller.set_auto_start(variable.get())
        except OSError as exc:
            self._show_error(f"无法保存自动启动设置：{exc}")
            return
        state = dict(self._managed_service_states.get(extension_id, {}))
        state["install_root"] = config.install_root
        state["auto_start"] = config.auto_start
        self._managed_service_states[extension_id] = state
        self._log(f"ComfyUI 自动启动已{'开启' if config.auto_start else '关闭'}。")
        self._refresh_extension_sidebar()

    def _disable_or_revoke_extension_async(self, extension_id: str, *, revoke: bool) -> None:
        if extension_id in self._managed_service_busy:
            return
        controller = self.managed_services.controller(extension_id)
        if controller is None:
            return
        self._managed_service_busy.add(extension_id)
        self._log("正在先安全停止 FolderBridge 托管的插件服务…")
        self._refresh_extension_sidebar()

        def work() -> None:
            try:
                state = controller.stop()
                self._queue_event("managed-service-state", (extension_id, "stop", state))
                record = self.extension_registry.get(extension_id)
                if revoke:
                    self.extension_registry.trust_store.revoke(extension_id)
                    notice = f"已撤销扩展批准：{record.manifest.name}"
                else:
                    self.extension_registry.trust_store.set_enabled(record, False)
                    notice = f"扩展已停用：{record.manifest.name}（批准记录保留）"
                self._queue_event("log", notice)
                self._queue_event("extension-refresh", extension_id)
            except (ManagedServiceError, OSError, ValueError) as exc:
                self._queue_event("managed-service-error", (extension_id, str(exc)))
            finally:
                self._queue_event("managed-service-idle", extension_id)

        threading.Thread(
            target=work,
            name=f"folderbridge-extension-stop-{extension_id}",
            daemon=True,
        ).start()

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

        ttk.Label(card, text="全局预授权", style="Field.TLabel").grid(row=4, column=0, sticky="nw", pady=(13, 0))
        capability_frame = ttk.Frame(card, style="Card.TFrame")
        capability_frame.grid(row=4, column=1, columnspan=2, sticky="w", pady=(9, 0))
        capability_toolbar = ttk.Frame(capability_frame, style="Card.TFrame")
        capability_toolbar.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self.capability_select_all_button = ttk.Button(
            capability_toolbar,
            text="全选",
            style="Compact.TButton",
            command=lambda: self._set_all_capabilities(True),
        )
        self.capability_select_all_button.pack(side="left")
        self.capability_clear_button = ttk.Button(
            capability_toolbar,
            text="清空",
            style="Compact.TButton",
            command=lambda: self._set_all_capabilities(False),
        )
        self.capability_clear_button.pack(side="left", padx=(6, 0))
        self.capability_checks: list[ttk.Checkbutton] = []
        for index, name in enumerate(CAPABILITY_NAMES):
            check = ttk.Checkbutton(
                capability_frame,
                text=CAPABILITY_LABELS[name],
                variable=self.capability_vars[name],
            )
            check.grid(row=1 + index // 3, column=index % 3, sticky="w", padx=(0, 18), pady=(0, 4))
            self.capability_checks.append(check)
        ttk.Label(
            capability_frame,
            text="一次启用后适用于以后加入的所有工作区；构建/封装会执行项目代码，GitHub 推送仅允许 HTTPS origin 的当前分支且禁止 force。插件授权在右侧 Extensions 侧栏单独管理。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.tasks_check = ttk.Checkbutton(
            card,
            text="高级：允许每个工作区单独 hash 批准的自定义任务（默认关闭）",
            variable=self.allow_tasks_var,
        )
        self.tasks_check.grid(row=5, column=1, columnspan=2, sticky="w", pady=(9, 0))
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
        self.log_header_bar = bar
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
            font=self._font("log"),
            padx=self._px(10),
            pady=self._px(9),
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
        self.exit_button = ttk.Button(frame, text="退出", command=self._exit_application)
        self.exit_button.grid(row=0, column=5, sticky="e", padx=(0, 8))
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
            font=self._font("primary_button"),
            padx=self._px(24),
            pady=self._px(9),
        )
        self.start_button.grid(row=0, column=6, sticky="e")
        return frame

    def _set_all_capabilities(self, enabled: bool) -> None:
        for variable in self.capability_vars.values():
            variable.set(bool(enabled))
        self._refresh_status_cards()

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
            capabilities=[
                name for name in CAPABILITY_NAMES
                if self.capability_vars[name].get()
            ],
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
            command = mcp_command(workspaces, settings.access_mode, settings.allow_tasks, settings.capabilities)
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
            text="ChatGPT 网页端按 1 → 4 完成；其他支持本地 stdio 的客户端看第 5 页；Python/Node 与插件开发见附录。向导文字可拖选并用 Ctrl+C 复制。",
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
                "4. 按需勾选一次“全局预授权”（测试/构建/EXE/APK/GitHub），以后所有工作区继承；插件授权与本地 ComfyUI 在右侧 Extensions 单独管理。“高级：自定义任务”通常保持关闭。点击“启动连接”，等待顶部状态变成“运行中”。",
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
                "5. 客户端是否弹出工具确认属于客户端行为。FolderBridge 仍强制文件夹边界、只读开关、哈希防冲突、全局能力白名单和逐工作区自定义任务批准。",
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

        dependencies_tab = self._guide_tab(
            notebook,
            "可选开发 / 能力依赖：普通 EXE 用户无需安装",
            (
                "1. 只使用 FolderBridge.exe：无需另外安装 Python 或 Node.js。FolderBridge Windows 版是单文件应用并自带 Python runtime；连接 ChatGPT 网页版时只需另外准备 OpenAI 官方 tunnel-client.exe。",
                "2. 从源码运行、开发 FolderBridge 或重新封装 Windows EXE：推荐安装 Python 3.11 x64，并确保命令行可以执行 python --version。FolderBridge 当前 Windows Release 构建以 Python 3.11 为可复现基线。",
                "3. 重新封装 EXE 时使用独立虚拟环境：python -m venv .build-venv，然后用 .build-venv\\Scripts\\python.exe -m pip install -r requirements-build.txt 安装构建依赖；普通 EXE 用户不需要这些步骤。",
                "4. 只有当某个工作区本身是 Node/npm 项目，且你希望调用它的 test/build 能力时，才安装 Node.js LTS；安装后用 node --version 与 npm --version 验证。不要为单纯运行 FolderBridge 安装 Node。",
                "5. FolderBridge 的 test/build/package capability 是授权和受限入口，不是包管理器：勾选 capability 不会自动安装 Python、Node、Gradle、编译器或项目依赖。缺少工具链时应按目标项目自己的文档安装。",
            ),
            wrap,
            warnings_after_steps={
                2: "不要把“Python 3.11”写成 FolderBridge.exe 的运行前置条件；它只适用于源码/开发/重打包等场景。",
                5: "只给可信项目开启会执行项目代码的 test/build/package 能力。",
            },
        )
        notebook.add(dependencies_tab, text="附录  Python / Node")
        dependency_buttons = ttk.Frame(dependencies_tab, style="Guide.TFrame")
        dependency_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(
            dependency_buttons,
            text="Python 官方 Windows 下载",
            command=lambda: webbrowser.open(PYTHON_WINDOWS_URL),
        ).pack(side="left")
        ttk.Button(
            dependency_buttons,
            text="Node.js 官方 LTS 下载",
            command=lambda: webbrowser.open(NODE_DOWNLOAD_URL),
        ).pack(side="left", padx=(8, 0))

        extension_tab = self._guide_tab(
            notebook,
            "FolderBridge Extension ABI v1",
            (
                "1. 每个插件是一个独立文件夹，至少包含 folderbridge-extension.json 与 plugin.py；插件安装后仍统一通过 extension(list/info/run) 调用，不会新增 MCP tool。",
                "2. manifest 必须声明精确权限、动作 input_schema、execution 和 workspace_adapter。外部插件按完整目录 SHA-256 + permissions 批准；文件或权限变化会自动失效。",
                "3. 需要适配项目时使用 workspace_adapter.mode=dynamic + detect.any_of/all_of。FolderBridge 每次调用都会重新检测，禁止靠安装时向每个工作区注入 .folderbridge.json task。",
                "4. 插件代码在独立子进程中运行，环境清理、超时和输出都有边界；这能隔离崩溃，但不是完整 OS 沙箱。不可信插件请放 VM/容器。",
                "5. 右侧 Extensions 侧栏默认折叠；把插件放入用户插件目录后点“重新扫描”，勾选时会显示 hash 与权限并请求一次批准。之后可热加载/停用，无需重建 Connector。",
                "6. 下方“复制给 LLM 的插件开发指令”包含完整 ABI 速查，并要求 LLM 在资料不足时主动向用户索取/要求上传必要的 API 文档、脚本、workflow、示例文件或项目结构。",
            ),
            wrap,
            warnings_after_steps={
                4: "独立子进程不是安全容器。权限声明是 FolderBridge 的授权契约，不应被描述成能阻止恶意 Python 绕过操作系统权限。",
            },
        )
        notebook.add(extension_tab, text="附录  插件标准")
        extension_buttons = ttk.Frame(extension_tab, style="Guide.TFrame")
        extension_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(extension_buttons, text="打开插件目录", command=self._open_extension_folder).pack(side="left")
        ttk.Button(
            extension_buttons,
            text="复制标准格式",
            command=lambda: self._copy_text(EXTENSION_FORMAT_SUMMARY, "Extension ABI v1 标准格式已复制。"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            extension_buttons,
            text="复制给 LLM 的插件开发指令",
            command=lambda: self._copy_text(EXTENSION_LLM_PROMPT, "LLM 插件开发指令已复制。"),
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
            font=self._font("card_title"),
            spacing3=self._px(12),
        )
        guide_text.tag_configure(
            "step",
            foreground="#364157",
            font=self._font("guide_step"),
            spacing3=self._px(9),
        )
        guide_text.tag_configure(
            "warning",
            background="#fff1f2",
            foreground="#b42318",
            font=self._font("guide_warning"),
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
        self._guide_text_widgets.append(guide_text)
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
                settings.capabilities,
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
            elif kind == "managed-service-state":
                extension_id, service_action, raw_state = payload  # type: ignore[misc]
                extension_id = str(extension_id)
                previous = self._managed_service_states.get(extension_id)
                state = dict(raw_state)
                self._managed_service_states[extension_id] = state
                warning = str(state.get("warning") or "")
                if warning:
                    self._log(warning)
                if state.get("started"):
                    self._log(f"托管服务已启动：{extension_id}")
                elif state.get("stopped"):
                    self._log(f"托管服务已停止：{extension_id}")
                status_shape = ("online", "owned", "external", "install_root", "auto_start")
                state_changed = previous is None or any(previous.get(key) != state.get(key) for key in status_shape)
                if service_action == "status" and not state_changed:
                    self._update_managed_service_status_label(extension_id)
                else:
                    self._refresh_extension_sidebar()
            elif kind == "managed-service-idle":
                self._managed_service_busy.discard(str(payload))
                self._refresh_extension_sidebar()
            elif kind == "managed-service-probe-idle":
                self._managed_service_status_pending.discard(str(payload))
            elif kind == "managed-service-path-required":
                extension_id = str(payload)
                self._log("ComfyUI 尚未配置安装目录；自动启动会等待首次选择。请在右侧 Extensions 中选择 Portable 或源码安装根目录。")
                if not self._sidebar_visible:
                    self._toggle_extension_sidebar()
                self.root.after_idle(lambda eid=extension_id: self._prompt_managed_service_path(eid))
            elif kind == "managed-service-error":
                extension_id, message = payload  # type: ignore[misc]
                self._log(f"托管服务 {extension_id}：{message}（Tunnel 不受影响）")
                self._refresh_extension_sidebar()
            elif kind == "extension-refresh":
                self._refresh_extension_sidebar()
            elif kind == "shutdown-error":
                self._shutdown_in_progress = False
                self.exit_button.configure(state="normal")
                if not self.supervisor.running():
                    self.start_button.configure(state="normal")
                self._show_error(str(payload))
            elif kind == "shutdown-complete":
                self._finish_shutdown()
                return
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
            self.capability_select_all_button,
            self.capability_clear_button,
            *self.capability_checks,
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

    def _exit_application(self) -> None:
        self._shutdown_application()

    def _on_close(self) -> None:
        if not messagebox.askyesno(
            "退出 FolderBridge MCP",
            "将先停止 FolderBridge 托管的插件服务和 Tunnel，然后退出。外部启动的软件不会被终止。",
            parent=self.root,
        ):
            return
        self._shutdown_application()

    def _shutdown_application(self) -> None:
        if self._closing or self._shutdown_in_progress:
            return
        try:
            settings = self._settings_from_form()
            self._save_form(settings)
        except (OSError, tk.TclError):
            pass

        loaded_extension_ids = self._loaded_extension_ids()
        self._shutdown_in_progress = True
        self.exit_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self._log("正在按已加载 Extension 顺序停止 FolderBridge 托管服务…")

        def work() -> None:
            service_results = self.managed_services.shutdown(loaded_extension_ids)
            failures = [result for result in service_results if result.get("error")]
            for result in service_results:
                warning = str(result.get("warning") or "")
                if warning:
                    self._queue_event("log", warning)
            if failures:
                rendered = "; ".join(
                    f"{item.get('service_id')}: {item.get('error')}" for item in failures
                )
                self._queue_event(
                    "shutdown-error",
                    f"托管插件服务未能可靠停止，FolderBridge 将保持打开：{rendered}",
                )
                return
            try:
                if self.supervisor.running():
                    self._queue_event("log", "托管插件服务已处理完毕，正在关闭 Tunnel/MCP 进程树…")
                    self.supervisor.stop()
                if self.supervisor.running():
                    raise RuntimeError("Tunnel/MCP 进程树仍在运行")
            except Exception as exc:
                self._queue_event(
                    "shutdown-error",
                    f"连接进程未能可靠关闭，FolderBridge 将保持打开：{exc}",
                )
                return
            self._queue_event("shutdown-complete", None)

        threading.Thread(target=work, name="folderbridge-shutdown", daemon=True).start()

    def _finish_shutdown(self) -> None:
        self._log("FolderBridge 托管服务与 Tunnel/MCP 已安全关闭；外部启动的软件未被终止。")
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
