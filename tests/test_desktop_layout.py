import os
import tkinter as tk
from types import SimpleNamespace

import pytest

from agentphone.desktop.app import DesktopApp
from agentphone.desktop.discovery import AgentInstallation


@pytest.fixture
def desktop_app(tmp_path, monkeypatch):
    if os.name != "nt" and not os.getenv("DISPLAY"):
        pytest.skip("Native Tk display unavailable")
    monkeypatch.setattr(DesktopApp, "detect", lambda self: None)
    root = tk.Tk()
    app = DesktopApp(root, SimpleNamespace(url="http://127.0.0.1:12345"), tmp_path)
    yield app
    root.destroy()


def test_footer_and_dual_panes_fit_minimum_window_with_all_detection_cards(desktop_app):
    app = desktop_app
    app.show_detection(
        [
            AgentInstallation(agent_id, name)
            for agent_id, name in [
                ("deepseek_harness", "DeepSeek Harness"),
                ("codex", "Codex"),
                ("workbuddy", "WorkBuddy"),
                ("ollama", "Ollama 本地模型"),
                ("claude", "Claude Code"),
                ("gemini", "Gemini CLI"),
                ("qwen", "Qwen Code"),
                ("kimi", "Kimi CLI"),
            ]
        ]
    )
    app.root.geometry("900x600")
    app.root.update_idletasks()
    footer = app.status_label.master
    assert footer.winfo_y() + footer.winfo_height() <= app.root.winfo_height()
    assert footer.winfo_height() > 20
    for pane in app.panes.values():
        assert pane["messages"].canvas.winfo_height() > 80
        assert pane["messages"].canvas.winfo_width() > 300


def test_wrapped_message_card_preserves_selectable_readonly_chinese_content(desktop_app):
    app = desktop_app
    content = "【UI 布局测试】保留完整消息。" * 25 + "\n第二行：青色木星43。"
    snapshot = {
        "bridge": dict(
            agent_a="a",
            agent_b="b",
            session_a="original-a",
            session_b="native-b",
            status="CONNECTED",
            error=None,
        ),
        "busy": False,
        "messages": [
            dict(
                id="message-1",
                sender_agent_id="a",
                recipient_agent_id="b",
                status="PROCESSED",
                content=content,
                error=None,
                receipt={"real": True},
            )
        ],
    }
    app.render(snapshot)
    app.root.update_idletasks()
    for pane in app.panes.values():
        card = pane["messages"].cards[0]
        assert card.text.get("1.0", "end-1c") == content
        assert card.text.cget("state") == "disabled"
        assert int(card.text.cget("height")) > 2
        assert card.text.winfo_height() >= card.text.winfo_reqheight()
    app.render({"bridge": None, "messages": []})
    assert all(not pane["messages"].cards for pane in app.panes.values())


def test_selector_offers_existing_or_new_on_both_sides_without_sending_tasks(
    desktop_app, monkeypatch
):
    from tkinter import ttk

    from agentphone.desktop import connection_dialog

    app = desktop_app
    app.installations = {
        key: AgentInstallation(key, name)
        for key, name in [("codex", "Codex"), ("deepseek_harness", "DeepSeek Harness")]
    }
    app.snapshot = {
        "bridge": None,
        "selection": {
            "a": {"agent_id": "codex", "native_session_id": "real-native-fixture"},
            "b": {"agent_id": "deepseek_harness", "native_session_id": "real-native-fixture"},
        },
    }
    monkeypatch.setattr(connection_dialog, "session_adapter_available", lambda _: True)
    calls = []

    def request(path, *args, **kwargs):
        calls.append(path)
        if path.startswith("/bridge/native-sessions"):
            return {
                "sessions": [
                    {
                        "native_session_id": "real-native-fixture",
                        "title": "Original",
                        "cwd": "original-project",
                    }
                ],
                "next_cursor": None,
            }
        return {"bridge": None, "messages": [], "busy": False}

    app.phone.request = request
    app.select_conversations()
    app.root.update()
    app.root.update_idletasks()
    popup = next(widget for widget in app.root.winfo_children() if isinstance(widget, tk.Toplevel))

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    modes = [
        widget
        for widget in descendants(popup)
        if isinstance(widget, ttk.Combobox)
        and tuple(widget.cget("values")) == ("已有对话", "新对话")
    ]
    assert len(modes) == 2
    for widget in modes:
        assert widget.get() == "已有对话"
        assert widget.winfo_width() > 180
        assert (
            widget.winfo_rooty() + widget.winfo_height()
            < popup.winfo_rooty() + popup.winfo_height()
        )
    assert not any(path == "/bridge/select" or "messages" in path for path in calls)
    popup.destroy()
