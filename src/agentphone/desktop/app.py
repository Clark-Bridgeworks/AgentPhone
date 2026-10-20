"""A/B views of bridge traffic. No task input, chat modes or history manager."""

import queue
import threading
import tkinter as tk
from importlib.resources import files
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import VERSION
from .adapters import ADAPTERS, session_adapter_available
from .discovery import CLI_AGENTS, discover_agents
from .mac_widgets import (
    BG,
    FONT,
    GREEN,
    HAIR,
    INK,
    MUTED,
    RED,
    SIDE,
    WHITE,
    AccentButton,
    AgentStatusCard,
    GhostButton,
    MessageList,
    Pill,
    WindowDots,
    dark_titlebar,
)
from .runtime import safe_error
from .storage import DesktopStorage
from .ui_theme import (
    ACCENT,
    BORDER,
    CARD,
    CARD_HOVER,
    DIM,
    MONO,
    PANEL,
    TEXT,
    font,
)


def pane_entries(snapshot, side):
    """Pure traffic projection: incoming content appears only after real delivery."""
    link = snapshot.get("bridge")
    if not link:
        return []
    own = link["agent_a" if side == "a" else "agent_b"]
    entries = []
    for message in snapshot["messages"]:
        outgoing = message["sender_agent_id"] == own
        received = message["recipient_agent_id"] == own and bool(message.get("receipt"))
        if outgoing or received:
            entries.append(
                {
                    "message_id": message["id"],
                    "direction": "outgoing" if outgoing else "incoming",
                    "sender": message["sender_agent_id"],
                    "recipient": message["recipient_agent_id"],
                    "content": message["content"],
                    "status": message["status"],
                    "error": message["error"],
                }
            )
    return entries


class DesktopApp:
    def __init__(self, root, phone, directory):
        self.root, self.phone = root, phone
        self.store = DesktopStorage(directory)
        self.installations = {}
        self.snapshot = {"bridge": None, "messages": []}
        self.events = queue.Queue()
        self.scanning = self.closing = False
        self.disconnecting = False
        self.last_signature = None
        root.title(f"AgentPhone · AB Communication Bridge {VERSION}")
        root.geometry("1180x780")
        root.minsize(960, 700)
        root.configure(bg=BG)
        icon = files("agentphone.desktop").joinpath("agentphone.ico")
        if icon.is_file():
            root.iconbitmap(str(icon))
        root.protocol("WM_DELETE_WINDOW", self.close)
        dark_titlebar(root)
        self._style()
        self._build()
        self.detect()
        root.after(100, self.poll)
        root.after(30000, self.periodic_scan)

    # ── chrome ──────────────────────────────────────────────────────────
    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#2a3242",
            troughcolor=SIDE,
            bordercolor=SIDE,
            darkcolor="#2a3242",
            lightcolor="#2a3242",
            borderwidth=0,
            arrowsize=0,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#3a4458")],
            bordercolor=[("active", SIDE)],
            arrowcolor=[("active", SIDE)],
        )

    def _build(self):
        self._build_header()
        self._build_agents()
        self._build_footer()
        self._build_panes()

    def _build_header(self):
        header = tk.Frame(self.root, bg=PANEL, padx=20, pady=13)
        header.pack(fill="x")
        left = tk.Frame(header, bg=PANEL)
        left.pack(side="left")
        WindowDots(left, bg=PANEL).pack(side="left", padx=14)
        icon_png = files("agentphone.desktop").joinpath("agentphone.png")
        self.brand_icon = tk.PhotoImage(file=str(icon_png))
        tk.Label(left, image=self.brand_icon, bg=PANEL).pack(side="left", padx=11)
        brand = tk.Frame(left, bg=PANEL)
        brand.pack(side="left")
        title_row = tk.Frame(brand, bg=PANEL)
        title_row.pack(anchor="w")
        tk.Label(title_row, text="AgentPhone", bg=PANEL, fg=INK,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        Pill(title_row, text=VERSION, tone="solid-blue", bg=PANEL, dot=False,
             height=20).pack(side="left", padx=9)
        tk.Label(brand, text="AB Communication Bridge", bg=PANEL, fg=MUTED,
                 font=font(9)).pack(anchor="w", pady=2)

        right = tk.Frame(header, bg=PANEL)
        right.pack(side="right")
        self.select_button = AccentButton(right, "选择两边对话", self.select_conversations,
                                          bg=PANEL)
        self.select_button.pack(side="right")
        self.resume_button = GhostButton(right, "续接原对话", self.resume, bg=PANEL)
        self.resume_button.pack(side="right", padx=6)
        self.disconnect_button = GhostButton(right, "断开连接", self.disconnect, bg=PANEL)
        self.disconnect_button.pack(side="right", padx=6)
        self.rescan_button = GhostButton(right, "重新检测", self.detect, bg=PANEL)
        self.rescan_button.pack(side="right", padx=6)
        self.paths_button = GhostButton(right, "路径设置", self.path_settings, bg=PANEL)
        self.paths_button.pack(side="right", padx=6)

        tk.Frame(self.root, bg=HAIR, height=1).pack(fill="x")

    # ── agent detection ─────────────────────────────────────────────────
    def _build_agents(self):
        section = tk.Frame(self.root, bg=BG)
        section.pack(fill="x", padx=20, pady=15)
        row = tk.Frame(section, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text="本机 Agent", bg=BG, fg=TEXT,
                 font=font(10, "bold")).pack(side="left")
        self.updated_label = tk.Label(row, text="", bg=BG, fg=DIM, font=font(8))
        self.updated_label.pack(side="right")
        self.detection_grid = tk.Frame(section, bg=BG)
        self.detection_grid.pack(fill="x", pady=10)
        for column in range(3):
            self.detection_grid.columnconfigure(column, weight=1, uniform="detection")
        self.scan_note = tk.StringVar(value="正在检测本机 Agent…")
        tk.Label(section, textvariable=self.scan_note, bg=BG, fg=DIM,
                 font=font(8), anchor="w").pack(fill="x", pady=8)

    # ── A/B panes ───────────────────────────────────────────────────────
    def _build_panes(self):
        self.panes = {}
        main = tk.Frame(self.root, bg=BG, padx=20, pady=10)
        main.pack(fill="both", expand=True, pady=14)
        main.columnconfigure(0, weight=1, uniform="endpoint")
        main.columnconfigure(1, weight=1, uniform="endpoint")
        main.rowconfigure(0, weight=1)
        for index, side in enumerate(["a", "b"]):
            frame = tk.Frame(main, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            frame.grid(row=0, column=index, sticky="nsew",
                       padx=6 if index == 0 else (6, 0))
            title = tk.StringVar(value="Agent " + side.upper())
            state = tk.StringVar(value="等待接入" if side == "a" else "等待联系")
            native = tk.StringVar(value="Session  尚未连接")

            pane_header = tk.Frame(frame, bg=PANEL, padx=14, pady=11)
            pane_header.pack(fill="x")
            tag = tk.Label(pane_header, text="AGENT " + side.upper(), bg="#1a2130", fg=MUTED,
                           font=font(8, "bold"), padx=8, pady=3)
            tag.pack(side="left")
            tk.Label(pane_header, textvariable=title, bg=PANEL, fg=TEXT,
                     font=font(11, "bold")).pack(side="left", padx=10)
            state_pill = Pill(pane_header, text=state.get(), tone="muted", bg=PANEL)
            state_pill.pack(side="left", padx=10)

            tk.Label(frame, textvariable=native, bg=PANEL, fg=DIM, padx=14,
                     font=("Consolas", 8), anchor="w").pack(fill="x", pady=10)
            tk.Frame(frame, bg=HAIR, height=1).pack(fill="x")
            messages = MessageList(frame, side)
            messages.pack(fill="both", expand=True)
            self.panes[side] = dict(
                title=title, state=state, native=native, messages=messages,
                state_pill=state_pill,
            )

    # ── footer ──────────────────────────────────────────────────────────
    def _build_footer(self):
        tk.Frame(self.root, bg=HAIR, height=1).pack(fill="x", side="bottom")
        footer = tk.Frame(self.root, bg=PANEL, padx=20, pady=12)
        footer.pack(fill="x", side="bottom")
        self.bridge_status = tk.StringVar(value="●  Bridge · 等待 Agent 首次发消息")
        self.status_label = tk.Label(
            footer, textvariable=self.bridge_status, bg=PANEL, fg=MUTED,
            font=font(9), anchor="w", justify="left",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_label.bind(
            "<Configure>", lambda event: self.status_label.configure(wraplength=event.width)
        )
        copy = GhostButton(footer, "复制", self.copy_mcp, bg=PANEL)
        copy.pack(side="right")
        tk.Label(footer, text=self.phone.url + "/mcp", bg=PANEL, fg=MUTED,
                 font=("Consolas", 8)).pack(side="right", padx=7)
        tk.Label(footer, text="MCP", bg=PANEL, fg=DIM,
                 font=font(8)).pack(side="right")

    # ── behaviour (unchanged) ───────────────────────────────────────────
    def detect(self):
        if self.scanning or self.closing:
            return
        self.scanning = True
        self.scan_note.set("正在检测本机 Agent 安装入口…")
        paths, cached = dict(self.store.data["paths"]), dict(self.store.data["detected_paths"])

        def scan():
            try:
                self.events.put(("detected", discover_agents(paths, cached)))
            except Exception as error:
                self.events.put(("scan_error", safe_error(str(error))))

        threading.Thread(target=scan, daemon=True).start()

    def periodic_scan(self):
        if not self.closing:
            self.detect()
            self.root.after(30000, self.periodic_scan)

    def render(self, snapshot):
        signature = repr(snapshot)
        if signature == self.last_signature:
            return
        self.last_signature, self.snapshot = signature, snapshot
        link = snapshot.get("bridge")
        idle = not snapshot.get("busy") and not self.disconnecting
        self.resume_button.set_enabled(
            bool(link and idle and link["status"] in {"FAILED", "DISCONNECTED"})
        )
        self.select_button.set_enabled(bool(idle))
        self.disconnect_button.set_enabled(
            bool(link and not snapshot.get("busy") and not self.disconnecting)
        )
        if not link:
            selection = snapshot.get("selection")
            self.bridge_status.set(
                "●  Bridge · 已选择两边对话，请在 A 原对话发起"
                if selection
                else "●  Bridge · 等待 Agent 首次发消息"
            )
            self.status_label.configure(fg=MUTED)
            for side, pane in self.panes.items():
                pane["title"].set("—")
                pane["state"].set("等待当前 Agent 接入" if side == "a" else "等待首次联系")
                pane["state_pill"].configure_pill(
                    text=pane["state"].get(), tone="muted"
                )
                pane["native"].set("Session：尚未连接")
                pane["messages"].set_entries([], {})
                if selection:
                    endpoint = selection[side]
                    pane["title"].set(endpoint["agent_id"])
                    pane["state"].set(
                        "新对话待首次送达"
                        if side == "b" and endpoint["mode"] == "new"
                        else "已选择原对话，等待接入"
                    )
                    pane["state_pill"].configure_pill(
                        text=pane["state"].get(),
                        tone="blue" if side == "b" and endpoint["mode"] == "new" else "amber",
                    )
                    pane["native"].set(
                        "Session：" + (endpoint.get("native_session_id") or "首次送达时创建")
                    )
            return
        names = {key: agent.name for key, agent in self.installations.items()}
        status = link["status"]
        self.status_label.configure(
            fg=GREEN if status == "CONNECTED" else RED if status == "FAILED" else MUTED
        )
        self.bridge_status.set(
            "●  Bridge："
            + status
            + (" · " + link["error"] if link["error"] else "")
            + (" · 消息收发中" if snapshot.get("busy") else " · 可选择对话或续接")
        )
        for side in ["a", "b"]:
            pane = self.panes[side]
            own = link["agent_a" if side == "a" else "agent_b"]
            native = link["session_a" if side == "a" else "session_b"]
            pane["title"].set(names.get(own, own))
            pane["native"].set("Session：" + (native or "正在建立"))
            label = (
                "已接入当前会话"
                if side == "a" and status not in {"FAILED", "DISCONNECTED"}
                else "已连接"
                if native and status == "CONNECTED"
                else "连接中"
                if status == "CONNECTING"
                else status
            )
            pane["state"].set(label)
            tone = (
                "green"
                if native and status == "CONNECTED"
                else "red"
                if status == "FAILED"
                else "amber"
            )
            pane["state_pill"].configure_pill(text=label, tone=tone)
            pane["messages"].set_entries(pane_entries(snapshot, side), names)

    def copy_mcp(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.phone.url + "/mcp")

    def show_detection(self, agents):
        for child in self.detection_grid.winfo_children():
            child.destroy()
        for index, agent in enumerate(agents):
            available = session_adapter_available(agent) and agent.id in ADAPTERS
            capability = (
                "已安装 · 可联系"
                if available
                else "已识别 · 未适配目标会话"
                if agent.command and agent.id not in ADAPTERS
                else "缺少原桌面对话接口 · 暂不可联系"
                if agent.callable
                else "检测到桌面版 · 缺少接入接口"
                if agent.application
                else "未安装"
            )
            tone = "green" if available else "amber" if agent.command or agent.application else "muted"
            card = AgentStatusCard(
                self.detection_grid, agent.name,
                capability + (" · 运行中" if agent.running else ""), tone, bg=BG,
            )
            card.grid(row=index // 3, column=index % 3, sticky="nsew",
                      padx=8, pady=4)

    def poll(self):
        if self.closing:
            return
        for _ in range(10):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "detected":
                self.scanning = False
                self.store.remember_installations(value)
                self.installations = {agent.id: agent for agent in value}
                self.phone.installations = dict(self.installations)
                self.show_detection(value)
                self.scan_note.set("已完成本机检测 · 仅可联系支持原桌面对话的目标")
                self.updated_label.configure(text="Updated " + _clock())
                self.last_signature = None
            elif kind == "scan_error":
                self.scanning = False
                self.scan_note.set("检测失败：" + value)
            elif kind in {"disconnected", "disconnect_error"}:
                self.disconnecting = False
                self.last_signature = None
                if kind == "disconnect_error":
                    messagebox.showinfo("无法断开", value, parent=self.root)
            elif kind == "selector_result":
                window, callback, result, error = value
                if window.winfo_exists():
                    callback(result, error)
        try:
            self.render(self.phone.request("/bridge"))
        except (OSError, RuntimeError) as error:
            self.bridge_status.set("Bridge：Disconnected · " + safe_error(str(error)))
        self.root.after(350, self.poll)

    def disconnect(self):
        link = self.snapshot.get("bridge")
        if self.disconnecting or not link:
            return
        self.disconnecting = True
        self.disconnect_button.set_enabled(False)

        def release():
            try:
                self.phone.request("/bridge/disconnect", {"conversation_id": link["id"]})
                self.events.put(("disconnected", None))
            except (OSError, RuntimeError) as error:
                self.events.put(("disconnect_error", safe_error(str(error))))

        threading.Thread(target=release, daemon=True).start()

    def select_conversations(self):
        from .connection_dialog import open_selector

        open_selector(self)

    def resume(self):
        link = self.snapshot.get("bridge")
        if not link or self.disconnecting:
            return
        self.disconnecting = True
        self.resume_button.set_enabled(False)

        def reconnect():
            try:
                self.phone.request("/bridge/resume", {"conversation_id": link["id"]}, timeout=45)
                self.events.put(("disconnected", None))
            except (OSError, RuntimeError) as error:
                self.events.put(("disconnect_error", safe_error(str(error))))

        threading.Thread(target=reconnect, daemon=True).start()

    def path_settings(self):
        link = self.snapshot.get("bridge")
        if link and link["status"] not in {"DISCONNECTED", "FAILED"}:
            messagebox.showinfo(
                "AgentPhone", "请先点击“断开连接”，再修改入口；无需重启软件。", parent=self.root
            )
            return
        window = tk.Toplevel(self.root)
        window.title("AgentPhone · Agent 路径设置")
        window.geometry("740x650")
        window.configure(bg=BG)
        window.transient(self.root)
        dark_titlebar(window)
        window.after(120, lambda: dark_titlebar(window))
        variables = {}
        tk.Label(window, text="仅在自动检测没找到时填写。无需填写密码或 API Key。",
                 bg=BG, fg=MUTED, font=font(9), padx=18, pady=16).pack(anchor="w")
        for identity, label in [
            ("deepseek_harness", "DeepSeek：选择 dsh.cmd"),
            ("codex", "Codex：选择 codex.exe / codex.cmd"),
            ("workbuddy", "WorkBuddy：选择腾讯原桌面 WorkBuddy.exe"),
            ("ollama", "Ollama：选择 ollama.exe（可选）"),
            *[(identity, name + "：选择官方 CLI 入口") for identity, name in CLI_AGENTS],
        ]:
            frame = tk.Frame(window, bg=BG, padx=18, pady=4)
            frame.pack(fill="x")
            tk.Label(frame, text=label, bg=BG, fg=MUTED, font=font(9)).pack(anchor="w")
            variable = tk.StringVar(value=self.store.data["paths"].get(identity, ""))
            variables[identity] = variable
            entry = tk.Entry(frame, textvariable=variable, width=66, bg=CARD, fg=TEXT,
                             insertbackground=TEXT, relief="flat", highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT)
            entry.pack(side="left", pady=6)

            def choose(target=variable):
                filename = filedialog.askopenfilename(
                    parent=window,
                    title="选择 Agent 的官方启动入口",
                    filetypes=[("启动程序", "*.exe *.cmd *.bat"), ("所有文件", "*.*")],
                )
                if filename:
                    target.set(filename)

            GhostButton(frame, "选择…", choose, bg=BG).pack(side="right")

        def save():
            for identity, variable in variables.items():
                path = variable.get().strip().strip('"')
                if path and not Path(path).is_file():
                    messagebox.showinfo("路径不存在", f"请重新选择：{identity}", parent=window)
                    return
                self.store.data["paths"][identity] = path
            self.store.save()
            window.destroy()
            self.detect()

        AccentButton(window, "保存并重新检测", save, bg=BG).pack(pady=10)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.phone.close()
        self.root.destroy()


def _clock():
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")
