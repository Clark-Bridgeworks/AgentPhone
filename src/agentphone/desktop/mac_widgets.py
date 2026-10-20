"""Canvas-drawn controls for the AgentPhone desktop UI.

Tk has no rounded boxes, pointer states, or badges of its own, so every
control here paints its own surface on a Canvas. The public surface stays
compatible with the previous widgets: `MessageList(parent, side)` exposes
`set_entries(entries, names)` and lives in a parent frame.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .ui_theme import (
    ACCENT,
    ACCENT_SOFT,
    AMBER,
    AMBER_SOFT,
    BG,
    BLUE_SOFT,
    BORDER,
    BORDER_STRONG,
    CARD,
    CARD_HOVER,
    CHROME,
    DIM,
    FONT,
    GREEN,
    GREEN_SOFT,
    HAIR,
    INK,
    MUTED,
    PANEL,
    RED,
    RED_SOFT,
    SIDE,
    TEXT,
    WHITE,
    font,
)

STATUS_TONES = {
    "PROCESSED": (GREEN, GREEN_SOFT),
    "DELIVERED": (GREEN, GREEN_SOFT),
    "FAILED": (RED, RED_SOFT),
}

# Keep the names app.py imports from this module.
__all__ = [
    "BG",
    "CHROME",
    "FONT",
    "GREEN",
    "HAIR",
    "INK",
    "MUTED",
    "RED",
    "SIDE",
    "WHITE",
    "rounded",
    "dark_titlebar",
    "Pill",
    "GhostButton",
    "AccentButton",
    "WindowDots",
    "AgentStatusCard",
    "EmptyState",
    "MessageList",
    "MessageCard",
]


def rounded(canvas, x, y, right, bottom, radius=10, **options):
    """Draw one smooth-cornered rectangle and return its canvas item id."""
    radius = max(0, min(radius, (right - x) // 2, (bottom - y) // 2))
    return canvas.create_polygon(
        x + radius, y,
        right - radius, y,
        right, y,
        right, y + radius,
        right, bottom - radius,
        right, bottom,
        right - radius, bottom,
        x + radius, bottom,
        x, bottom,
        x, bottom - radius,
        x, y + radius,
        x, y,
        smooth=True,
        splinesteps=24,
        **options,
    )


def dark_titlebar(window):
    """Ask DWM for a dark caption so system chrome matches the app (Windows)."""
    try:
        import ctypes
        window.update_idletasks()
        # Tk's winfo_id() addresses an inner window; its parent is the frame DWM owns.
        # This holds for the root window and for every Toplevel.
        hwnd = window.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        if parent:
            hwnd = parent
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, older builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                break
    except Exception:
        pass


class Pill(tk.Canvas):
    """Rounded status chip: optional dot plus a short label."""

    TONES = {
        "green": (GREEN, GREEN_SOFT),
        "blue": (ACCENT, ACCENT_SOFT),
        "amber": (AMBER, AMBER_SOFT),
        "red": (RED, RED_SOFT),
        "muted": (MUTED, "#171c27"),
        "solid-blue": ("#ffffff", ACCENT),
    }

    def __init__(self, parent, text="", tone="muted", bg=None, dot=True, height=22):
        self.background = bg or parent.cget("bg")
        super().__init__(parent, bg=self.background, highlightthickness=0, height=height)
        self.dot = dot
        self._tone = tone
        self._text = text
        self._height = height
        self._render()

    def configure_pill(self, text=None, tone=None):
        changed = False
        if text is not None and text != self._text:
            self._text, changed = text, True
        if tone is not None and tone != self._tone:
            self._tone, changed = tone, True
        if changed:
            self._render()

    def set_text(self, text):
        self.configure_pill(text=text)

    def set_tone(self, tone):
        self.configure_pill(tone=tone)

    def _colors(self):
        return self.TONES.get(self._tone, (MUTED, "#171c27"))

    def _render(self):
        self.delete("all")
        fg, fill = self._colors()
        pad_x = 9
        measure = tkfont.Font(family=FONT, size=8, weight="bold")
        text_width = measure.measure(self._text) if self._text else 0
        dot_space = 10 if self.dot else 0
        width = max(36, pad_x * 2 + dot_space + text_width + 2)
        self.configure(width=width, height=self._height)
        rounded(self, 0, 1, width - 1, self._height - 1, radius=(self._height - 2) // 2,
                fill=fill, outline="")
        if self.dot:
            cy = self._height // 2
            self.create_oval(pad_x - 1, cy - 3, pad_x + 5, cy + 3, fill=fg, outline="")
        if self._text:
            self.create_text(pad_x + dot_space, self._height // 2,
                             text=self._text, anchor="w", fill=fg, font=font(8, "bold"))


class WindowDots(tk.Canvas):
    """The three decorative traffic lights from the mockup."""

    def __init__(self, parent, bg=None):
        self.background = bg or parent.cget("bg")
        super().__init__(parent, bg=self.background, highlightthickness=0, width=54, height=16)
        for index, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            x = 5 + index * 15
            self.create_oval(x, 4, x + 10, 14, fill=color, outline="")


class GhostButton(tk.Label):
    """Flat text button that lifts to a card shade on hover."""

    def __init__(self, parent, text, command, tone="default", bg=None):
        self._bg = bg or parent.cget("bg")
        super().__init__(
            parent, text=text, bg=self._bg,
            fg=TEXT if tone == "default" else MUTED,
            font=font(9), padx=12, pady=7,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.command, self.tone, self._enabled = command, tone, True
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _enter(self, _event):
        if self._enabled:
            self.configure(bg=CARD_HOVER if self.tone != "danger" else RED_SOFT,
                           fg=TEXT if self.tone != "danger" else RED)

    def _leave(self, _event):
        self.configure(bg=self._bg,
                       fg=(TEXT if self.tone == "default" else MUTED) if self._enabled else DIM)

    def _click(self, _event):
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(fg=(TEXT if self.tone == "default" else MUTED) if enabled else DIM,
                       cursor="hand2" if enabled else "arrow")


class AccentButton(tk.Label):
    """Filled primary action — one per surface."""

    def __init__(self, parent, text, command, bg=None):
        self._bg = bg or parent.cget("bg")
        super().__init__(
            parent, text=text, bg=ACCENT, fg="#07101f",
            font=font(9, "bold"), padx=16, pady=7,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.command = command
        self._enabled = True
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _enter(self, _event):
        if self._enabled:
            self.configure(bg="#6cb0ff")

    def _leave(self, _event):
        self.configure(bg=ACCENT if self._enabled else "#1a2130")

    def _click(self, _event):
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(
            bg=ACCENT if enabled else "#1a2130",
            fg="#07101f" if enabled else DIM,
            cursor="hand2" if enabled else "arrow",
        )


class AgentStatusCard(tk.Canvas):
    """One detected agent: status dot, bold name, muted capability line."""

    def __init__(self, parent, name, detail, tone, bg=None):
        self._bg = bg or parent.cget("bg")
        super().__init__(parent, bg=self._bg, highlightthickness=0, height=58)
        self.name, self.detail, self.tone = name, detail, tone
        self._hover = False
        self.bind("<Configure>", self._paint)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _event):
        self._hover = True
        self._paint()

    def _leave(self, _event):
        self._hover = False
        self._paint()

    def _dot(self):
        return {"green": GREEN, "amber": AMBER, "muted": DIM}.get(self.tone, DIM)

    def _paint(self, _event=None):
        self.delete("all")
        width = max(self.winfo_width(), 80)
        fill = "#1a2130" if self._hover else CARD
        rounded(self, 0, 0, width, 57, radius=10, fill=fill,
                outline=BORDER_STRONG if self._hover else BORDER)
        self.create_oval(15, 18, 23, 26, fill=self._dot(), outline="")
        self.create_text(32, 22, text=self.name, anchor="w", fill=TEXT, font=font(10, "bold"))
        self.create_text(32, 40, text=self.detail, anchor="w", fill=MUTED, font=font(8),
                         width=max(50, width - 44))


class EmptyState(tk.Frame):
    """Centered line-art bridge mark plus a short explanation."""

    def __init__(self, parent, title, detail):
        super().__init__(parent, bg=SIDE)
        art = tk.Canvas(self, bg=SIDE, width=340, height=84, highlightthickness=0)
        art.pack()
        cy = 42
        art.create_line(34, cy, 148, cy, fill=BORDER_STRONG, width=2)
        art.create_line(192, cy, 306, cy, fill=BORDER_STRONG, width=2)
        for x in (34, 306):
            art.create_oval(x - 8, cy - 8, x + 8, cy + 8,
                            outline="#5a6377", width=2, fill=SIDE)
        art.create_oval(163, cy - 11, 185, cy + 11, fill="#1b3560", outline="")
        art.create_oval(166, cy - 8, 182, cy + 8, fill=ACCENT, outline="")
        art.create_oval(170, cy - 4, 178, cy + 4, fill="#9cc8ff", outline="")
        tk.Label(self, text=title, bg=SIDE, fg=TEXT, font=font(11, "bold")).pack(pady=2)
        tk.Label(self, text=detail, bg=SIDE, fg=MUTED, font=font(9),
                 wraplength=360, pady=8).pack()


class MessageList(tk.Frame):
    """Scrollable message well with an illustrated empty state."""

    def __init__(self, parent, side):
        super().__init__(parent, bg=SIDE)
        self.side = side
        self.canvas = tk.Canvas(self, bg=SIDE, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.configure(style="Dark.Vertical.TScrollbar")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=SIDE)
        self.body_id = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", self._region)
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<MouseWheel>", self._wheel)

        self.empty = EmptyState(
            self,
            "等待当前 Agent 接入" if side == "a" else "等待对方 Agent",
            "在原 Agent 中下达需求，首次发消息时自动建立桥。"
            if side == "a"
            else "收到消息后，这里显示两个 Agent 的真实收发记录。",
        )
        self.empty.place(relx=0.5, rely=0.46, anchor="center")
        self.cards = []

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.body_id, width=event.width)

    def set_entries(self, entries, names):
        view = self.canvas.yview()
        at_bottom = view[1] > 0.96
        for child in self.body.winfo_children():
            child.destroy()
        self.cards.clear()
        if not entries:
            self.scrollbar.pack_forget()
            self.empty.place(relx=0.5, rely=0.46, anchor="center")
        else:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
            self.empty.place_forget()
        for entry in entries:
            card = MessageCard(self.body, entry, names, self._wheel)
            card.pack(fill="x", padx=14, pady=10)
            self.cards.append(card)
        self.update_idletasks()
        self._region()
        self.canvas.yview_moveto(1 if at_bottom else view[0])


class MessageCard(tk.Canvas):
    """One bridge message: direction stripe, tag, route, body, status chip."""

    def __init__(self, parent, entry, names, wheel):
        super().__init__(parent, bg=SIDE, highlightthickness=0, height=110)
        self.entry = entry
        outgoing = entry["direction"] == "outgoing"
        self.accent = ACCENT if outgoing else GREEN
        self.accent_soft = BLUE_SOFT if outgoing else GREEN_SOFT
        self.fill = CARD
        self.border = rounded(self, 1, 1, 300, 109, fill=self.fill, outline=BORDER)
        self.stripe = rounded(self, 1, 1, 6, 108, radius=3, fill=self.accent, outline="")
        self.content = tk.Frame(self, bg=self.fill)
        self.window = self.create_window(18, 13, anchor="nw", window=self.content)

        top = tk.Frame(self.content, bg=self.fill)
        top.pack(fill="x")
        tag = "OUTGOING" if outgoing else "INCOMING"
        tk.Label(
            top, text=tag, bg=self.accent_soft, fg=self.accent,
            font=font(8, "bold"), padx=9, pady=3,
        ).pack(side="left")
        route = names.get(entry["sender"], entry["sender"]) + "  →  " + names.get(
            entry["recipient"], entry["recipient"]
        )
        self.heading = tk.Label(
            top, text="route:  " + route, anchor="w", bg=self.fill, fg=MUTED, font=font(8),
        )
        self.heading.pack(side="left", padx=10, fill="x", expand=True)

        self.text = tk.Text(
            self.content,
            bg=self.fill, fg=TEXT,
            relief="flat", borderwidth=0, highlightthickness=0,
            wrap="word", font=font(10),
            width=1, height=1, padx=0, pady=0,
            spacing1=3, spacing3=3,
            selectbackground=ACCENT_SOFT, selectforeground=TEXT,
            insertbackground=TEXT, exportselection=False,
        )
        self.text.insert("1.0", entry["content"])
        self.text.configure(state="disabled")
        self.text.pack(fill="x", pady=10)
        self.text.bind("<MouseWheel>", wheel)
        self.text.bind("<Control-a>", self._select_all)

        footer = tk.Frame(self.content, bg=self.fill)
        footer.pack(fill="x", pady=10)
        fg, bg = STATUS_TONES.get(entry["status"], (MUTED, "#171c27"))
        tk.Label(
            footer, text=entry["status"].lower(), bg=bg, fg=fg, font=font(8, "bold"),
            padx=8, pady=2,
        ).pack(side="left")
        tk.Label(
            footer, text=entry["message_id"], bg=self.fill, fg=DIM,
            font=("Consolas", 8), anchor="w",
        ).pack(side="left", padx=9, fill="x", expand=True)

        self.error = None
        if entry["error"]:
            self.error = tk.Label(
                self.content, text="未送达 · " + entry["error"], bg=self.fill, fg=RED,
                font=font(9), justify="left", anchor="w",
            )
            self.error.pack(fill="x", pady=8)

        self.bind("<Configure>", self._fit)
        self.bind("<MouseWheel>", wheel)
        self.content.bind("<MouseWheel>", wheel)
        self.text.bind("<Configure>", self._text_fit)

    def _select_all(self, _event):
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _text_fit(self, _event=None):
        count = self.text.count("1.0", "end", "displaylines")
        height = max(1, count[0] if count else 1)
        if int(self.text.cget("height")) != height:
            self.text.configure(height=height)
        self.after_idle(self._height)

    def _height(self):
        if self.winfo_exists():
            height = self.content.winfo_reqheight() + 28
            self.configure(height=height)
            self._shape(self.winfo_width(), height)

    def _shape(self, width, height):
        self.delete(self.border)
        self.delete(self.stripe)
        self.border = rounded(self, 1, 1, width - 1, height - 1, fill=self.fill, outline=BORDER)
        self.stripe = rounded(self, 1, 1, 6, height - 1, radius=3, fill=self.accent, outline="")
        self.tag_lower(self.border)
        self.tag_raise(self.stripe)

    def _fit(self, event):
        self.itemconfigure(self.window, width=max(1, event.width - 36))
        self.heading.configure(wraplength=max(1, event.width - 190))
        if self.error:
            self.error.configure(wraplength=max(1, event.width - 36))
        self._shape(event.width, event.height)
