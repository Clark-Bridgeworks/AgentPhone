"""Select original A/B conversations; contains no task or chat input."""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .adapters import session_adapter_available
from .mac_widgets import AccentButton, GhostButton, dark_titlebar
from .runtime import safe_error
from .ui_theme import (
    ACCENT_SOFT,
    BG,
    BORDER,
    CARD,
    DIM,
    MUTED,
    PANEL,
    TEXT,
    font,
)


def _style(window):
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(
        "Dark.TCombobox",
        fieldbackground=CARD,
        background=CARD,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=BORDER,
        lightcolor=CARD,
        darkcolor=CARD,
        insertcolor=TEXT,
        selectbackground=ACCENT_SOFT,
        selectforeground=TEXT,
        padding=5,
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", CARD), ("disabled", PANEL)],
        foreground=[("readonly", TEXT), ("disabled", DIM)],
        background=[("readonly", CARD), ("active", CARD)],
        arrowcolor=[("disabled", DIM)],
    )
    window.option_add("*TCombobox*Listbox.background", CARD)
    window.option_add("*TCombobox*Listbox.foreground", TEXT)
    window.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
    window.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    window.option_add("*TCombobox*Listbox.borderWidth", 0)


def _panel(window, title):
    outer = tk.Frame(window, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    tk.Label(
        outer, text=title, bg=CARD, fg=TEXT, font=font(10, "bold"),
        anchor="w", padx=16, pady=12,
    ).pack(fill="x")
    tk.Frame(outer, bg=BORDER, height=1).pack(fill="x")
    body = tk.Frame(outer, bg=CARD, padx=16, pady=14)
    body.pack(fill="both", expand=True)
    return outer, body


def _field(parent, label, note=None):
    tk.Label(parent, text=label, bg=CARD, fg=MUTED, font=font(8), anchor="w").pack(fill="x")
    if note:
        tk.Label(parent, text=note, bg=CARD, fg=DIM, font=font(8), anchor="w").pack(fill="x")


def open_selector(app):
    window = tk.Toplevel(app.root)
    window.title("AgentPhone · 选择两边原对话")
    window.geometry("920x600")
    window.minsize(760, 520)
    window.configure(bg=BG)
    window.transient(app.root)
    dark_titlebar(window)
    window.after(120, lambda: dark_titlebar(window))
    _style(window)
    window.columnconfigure(0, weight=1)
    window.columnconfigure(1, weight=1)
    window.rowconfigure(1, weight=1)

    targets = {a.name: a.id for a in app.installations.values() if session_adapter_available(a)}
    if len(targets) < 2:
        messagebox.showinfo("无法选择", "需要两款支持原桌面对话的 Agent。", parent=window)
        window.destroy()
        return

    selected = app.snapshot.get("selection") or {}
    link = app.snapshot.get("bridge")

    note = tk.StringVar(value="已有对话保留上下文；B 的新对话在首次收到消息时创建并打开。")
    tk.Label(
        window, textvariable=note, bg=BG, fg=MUTED, font=font(9),
        wraplength=860, justify="left", anchor="w", padx=20, pady=16,
    ).grid(row=0, columnspan=2, sticky="ew")

    panels = {}

    def background(operation, callback):
        def work():
            try:
                value, error = operation(), None
            except Exception as exc:
                value, error = None, safe_error(str(exc))
            app.events.put(("selector_result", (window, callback, value, error)))

        threading.Thread(target=work, daemon=True).start()

    for column, side in enumerate(("a", "b")):
        outer, frame = _panel(window, "A · 发起对话" if side == "a" else "B · 对方对话")
        outer.grid(row=1, column=column, sticky="nsew", padx=(20, 8) if side == "a" else (8, 20),
                   pady=(0, 10))
        old = selected.get(side, {})
        agent_id = old.get("agent_id") or (link.get("agent_" + side) if link else None)
        default = next(
            (name for name, key in targets.items() if key == agent_id), list(targets)[column]
        )
        agent = tk.StringVar(value=default)
        mode = tk.StringVar(
            value="已有对话" if side == "a" or old.get("native_session_id") or link else "新对话"
        )
        choice = tk.StringVar()
        identity = tk.StringVar(value="等待读取原软件的对话列表")
        cwd = tk.StringVar(value=old.get("cwd") or str(Path.home() / "Documents"))

        _field(frame, "Agent 软件")
        software = ttk.Combobox(
            frame, textvariable=agent, values=list(targets), state="readonly",
            style="Dark.TCombobox", font=font(9),
        )
        software.pack(fill="x", pady=(5, 14))

        _field(frame, "对话方式")
        modes = ttk.Combobox(
            frame, textvariable=mode, values=["已有对话", "新对话"], state="readonly",
            style="Dark.TCombobox", font=font(9),
        )
        modes.pack(fill="x", pady=(5, 14))

        _field(frame, "对话")
        conversations = ttk.Combobox(
            frame, textvariable=choice, state="readonly", height=12,
            style="Dark.TCombobox", font=font(9),
        )
        conversations.pack(fill="x", pady=(5, 10))

        tk.Label(
            frame, textvariable=identity, bg=CARD, fg=DIM, font=("Consolas", 8),
            wraplength=380, justify="left", anchor="w",
        ).pack(fill="x", pady=6)

        buttons = tk.Frame(frame, bg=CARD)
        buttons.pack(fill="x", pady=(4, 0))
        refresh = GhostButton(buttons, "刷新对话", lambda: None, bg=CARD)
        refresh.pack(side="left")
        more = GhostButton(buttons, "更早的对话", lambda: None, bg=CARD)
        more.pack(side="left", padx=6)
        more.set_enabled(False)

        project = tk.Frame(frame, bg=CARD)
        tk.Label(project, text="新对话的项目文件夹", bg=CARD, fg=MUTED,
                 font=font(8), anchor="w").pack(fill="x")
        folder_row = tk.Frame(project, bg=CARD)
        folder_row.pack(fill="x", pady=5)
        folder_entry = tk.Entry(
            folder_row, textvariable=cwd, bg=BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT_SOFT, font=font(9),
        )
        folder_entry.pack(side="left", fill="x", expand=True, ipady=3)
        GhostButton(
            folder_row, "选择文件夹…",
            lambda v=cwd: v.set(filedialog.askdirectory(parent=window) or v.get()), bg=CARD,
        ).pack(side="left", padx=(6, 0))
        project.pack_forget()

        panel = dict(
            agent=agent, mode=mode, choice=choice, identity=identity, rows={},
            conversations=conversations, refresh=refresh, more=more, cursor=None,
            project=project, cwd=cwd,
            preferred=old.get("native_session_id")
            or (link.get("session_" + side) if link else None),
        )
        panels[side] = panel

        def display_mode(_event=None, p=panel, which=side):
            existing = p["mode"].get() == "已有对话"
            p["conversations"].configure(state="readonly" if existing else "disabled")
            p["refresh"].set_enabled(existing)
            p["more"].set_enabled(bool(existing and p["cursor"]))
            if not existing and which == "a":
                p["project"].pack(fill="x", pady=12)
            else:
                p["project"].pack_forget()
            if not existing:
                p["identity"].set(
                    "打开原软件的新对话，不自动发送任务。"
                    if which == "a"
                    else "首次收到 A 的消息时打开原软件的新对话。"
                )

        def load(_event=None, p=panel, append=False):
            key = targets[p["agent"].get()]
            cursor = p["cursor"] if append else None
            p["refresh"].set_enabled(False)
            p["identity"].set("正在读取原对话…")

            def done(value, error):
                p["refresh"].set_enabled(True)
                if error:
                    p["identity"].set(error)
                    return
                if targets[p["agent"].get()] != key:
                    return
                if not append:
                    p["rows"] = {}
                for row in value["sessions"]:
                    label = (row["title"] or "未命名对话") + " · " + row["native_session_id"][-12:]
                    p["rows"][label] = row
                p["cursor"] = value.get("next_cursor")
                p["more"].set_enabled(bool(p["cursor"]))
                p["conversations"].configure(values=list(p["rows"]))
                preferred = next(
                    (label for label, r in p["rows"].items()
                     if r["native_session_id"] == p["preferred"]),
                    None,
                )
                if preferred:
                    p["choice"].set(preferred)
                elif p["choice"].get() not in p["rows"]:
                    p["choice"].set(next(iter(p["rows"]), ""))
                update_identity(p=p)

            url = "/bridge/native-sessions?agent_id=" + key
            if cursor:
                from urllib.parse import quote

                url += "&cursor=" + quote(cursor)
            background(lambda: app.phone.request(url, timeout=35), done)

        def update_identity(_event=None, p=panel):
            row = p["rows"].get(p["choice"].get())
            if row:
                p["identity"].set(row["native_session_id"] + "\n" + row["cwd"])

        refresh.command = load
        more.command = lambda action=load: action(append=True)
        software.bind("<<ComboboxSelected>>", load)
        conversations.bind("<<ComboboxSelected>>", update_identity)
        modes.bind("<<ComboboxSelected>>", display_mode)
        display_mode()
        if mode.get() == "已有对话":
            load()

    footer = tk.Frame(window, bg=BG, padx=20, pady=14)
    footer.grid(row=2, columnspan=2, sticky="ew")
    save = AccentButton(footer, "应用对话选择", lambda: None, bg=BG)
    save.pack(side="right")
    GhostButton(footer, "取消", window.destroy, bg=BG).pack(side="right", padx=8)

    def apply():
        request = {}
        for side, panel in panels.items():
            row = {
                "agent_id": targets[panel["agent"].get()],
                "mode": "existing" if panel["mode"].get() == "已有对话" else "new",
            }
            if row["mode"] == "existing":
                chosen = panel["rows"].get(panel["choice"].get())
                if not chosen:
                    messagebox.showinfo(
                        "请选择对话", "请先选择 " + side.upper() + " 的已有对话。", parent=window
                    )
                    return
                row["native_session_id"] = chosen["native_session_id"]
            elif side == "a":
                row["cwd"] = panel["cwd"].get()
            request[side] = row
        save.set_enabled(False)
        note.set("正在接入所选原对话…")

        def done(value, error):
            save.set_enabled(True)
            if error:
                note.set(error)
                return
            app.last_signature = None
            window.destroy()

        background(lambda: app.phone.request("/bridge/select", request, timeout=45), done)

    save.command = apply
