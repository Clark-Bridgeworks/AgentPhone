import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentphone.desktop.adapters import WorkBuddyAdapter
from agentphone.desktop.awareness import install_awareness, session_context
from agentphone.desktop.discovery import AgentInstallation
from agentphone.desktop.native_sessions import NativeSessions
from agentphone.desktop.workbuddy_desktop import WorkBuddyDesktopClient


def test_workbuddy_install_preserves_native_model_permissions_hooks_and_other_mcp(tmp_path):
    home = tmp_path / "home"
    root = home / ".workbuddy"
    root.mkdir(parents=True)
    original = {
        "model": "original",
        "permissions": {"defaultMode": "default"},
        "hooks": {"SessionStart": [{"hooks": [{"command": "keep original"}]}]},
    }
    (root / "settings.json").write_text(json.dumps(original), encoding="utf-8")
    (root / "mcp.json").write_text('{"mcpServers":{"other":{"command":"keep"}}}')
    args = (home, tmp_path / "data", tmp_path / "AgentPhone.exe", ["workbuddy"])
    first = install_awareness(*args)
    assert not first["errors"]
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == original["model"]
    assert settings["permissions"] == original["permissions"]
    assert settings["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert len(settings["hooks"]["SessionStart"]) == 2
    assert (
        "--session-context workbuddy" in settings["hooks"]["SessionStart"][1]["hooks"][0]["command"]
    )
    assert json.loads((root / "mcp.json").read_text())["mcpServers"]["other"] == {"command": "keep"}
    assert (root / "extensions/agentphone-bridge/server/index.cjs").is_file()
    assert not install_awareness(*args)["changed"]
    context = session_context(
        {"session_id": "real-native", "hook_event_name": "SessionStart"}, "workbuddy"
    )
    assert "sender=workbuddy" in context["hookSpecificOutput"]["additionalContext"]
    assert "real-native" in context["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def wb_client(monkeypatch):
    from agentphone.desktop import workbuddy_desktop

    instances = []

    class Native:
        def __init__(self, application):
            self.calls = []
            instances.append(self)

        def connect(self):
            self.calls.append(("connect",))

        def session_info(self, native):
            self.calls.append(("get", native))
            return dict(
                native_session_id=native, cwd="original-project", title="原名称", running=False
            )

        def create(self, cwd, title):
            self.calls.append(("create", str(cwd), title))
            return dict(native_session_id="new-native", cwd=str(cwd), title=title)

        def prompt(self, native, content, request_id):
            self.calls.append(("prompt", native, content, request_id))

        def poll(self, native, request_id):
            return {"state": "done", "text": json.dumps(self.action, ensure_ascii=False)}

        def open_conversation(self, native):
            self.calls.append(("open", native))

    monkeypatch.setattr(workbuddy_desktop, "WorkBuddyDesktopClient", Native)
    return instances


def test_existing_native_same_id_once_presentation_and_detach_never_cancel(tmp_path, wb_client):
    phone = SimpleNamespace(url="http://127.0.0.1:1", directory=tmp_path)
    target = AgentInstallation("workbuddy", "WorkBuddy", application="original-app")
    link = dict(
        id=str(uuid4()),
        agent_a="codex",
        agent_b="workbuddy",
        session_a="real-a",
        session_b="real-b",
        binding_b="bound-b",
    )
    adapter = WorkBuddyAdapter(phone, tmp_path / "connection", target)
    adapter.start_session(link)
    assert '"sender_session_id": "real-b"' in adapter.context_text
    assert '"sender_session_id": "bound-b"' not in adapter.context_text
    message = dict(
        message_id=str(uuid4()),
        conversation_id=link["id"],
        sender_id="codex",
        recipient_id="workbuddy",
        content="中文消息",
    )
    action = dict(
        action="send_message",
        recipient_id="codex",
        content="中文回信",
        reply_to_message_id=message["message_id"],
        received_message_id=message["message_id"],
        received_content=message["content"],
    )
    wb_client[-1].action = action
    assert adapter.receive_message(message) == action
    assert adapter.receive_message(message) == action
    assert adapter.native_session_id == "real-b"
    calls = wb_client[-1].calls
    assert [c for c in calls if c[0] == "open"] == [("open", "real-b")]
    assert not any(c[0] in {"create", "rename", "cancel", "destroy"} for c in calls)
    adapter.stop_session()
    assert not any(c[0] in {"cancel", "destroy"} for c in calls)
    restarted = WorkBuddyAdapter(phone, tmp_path / "connection", target)
    restarted.start_session(link)
    wb_client[-1].action = action
    restarted.receive_message(message)
    assert not any(c[0] == "open" for c in wb_client[-1].calls)


def test_new_native_b_uses_selected_project_without_model_handshake(tmp_path, wb_client):
    phone = SimpleNamespace(
        url="http://127.0.0.1:1",
        directory=tmp_path,
        selection={"b": {"agent_id": "workbuddy", "cwd": str(tmp_path)}},
    )
    target = AgentInstallation("workbuddy", "WorkBuddy", application="original-app")
    adapter = WorkBuddyAdapter(phone, tmp_path / "connection", target)
    adapter.start_session(
        dict(
            id=str(uuid4()),
            agent_a="codex",
            agent_b="workbuddy",
            session_a="real-a",
            session_b=None,
            binding_b="bound-b",
        )
    )
    assert adapter.native_session_id == "new-native"
    assert '"sender_session_id": "new-native"' in adapter.context_text
    assert any(c[0] == "create" and c[1] == str(tmp_path) for c in wb_client[-1].calls)
    assert not any(c[0] in {"prompt", "open"} for c in wb_client[-1].calls)


def test_native_new_a_blank_and_exact_deep_link(tmp_path, wb_client, monkeypatch):
    from agentphone.desktop import native_sessions

    monkeypatch.setattr(native_sessions, "session_adapter_available", lambda target: True)
    target = AgentInstallation("workbuddy", "WorkBuddy", application="original-app")
    sessions = NativeSessions(SimpleNamespace(), tmp_path, lambda: {"workbuddy": target})
    assert sessions.create_a("workbuddy", tmp_path)["native_session_id"] == "new-native"
    assert any(c == ("open", "new-native") for c in wb_client[-1].calls)
    assert not any(c[0] == "prompt" for c in wb_client[-1].calls)


def test_open_rejects_path_or_query_injection_before_calling_native_app(tmp_path):
    client = WorkBuddyDesktopClient(tmp_path / "WorkBuddy.exe")
    for value in ("chat/new?prompt=evil", "../other", "native#other", ""):
        with pytest.raises(RuntimeError, match="有效原生"):
            client.open_conversation(value)
