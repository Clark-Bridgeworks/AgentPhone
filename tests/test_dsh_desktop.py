import asyncio
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from agentphone.desktop.adapters import CodexAdapter, DeepSeekAdapter, session_adapter_available
from agentphone.desktop.discovery import AgentInstallation
from agentphone.desktop.dsh_desktop import (
    DshDesktopClient,
    DshReadUnavailable,
    browser_cookie,
    browser_secret,
)
from agentphone.desktop.runtime import RunStopped


@pytest.fixture
def native_adapter(tmp_path, monkeypatch):
    class NativeClient:
        def __init__(self, *_args):
            self.creations = []
            self.prompts = []
            self.selected = []
            self.cancelled = []
            self.rows = []
            self.fail_window = False
            self.plain_output = False

        def connect(self):
            pass

        def create(self, cwd, session_id):
            self.cwd = cwd
            self.creations.append((cwd, session_id))
            return session_id or "actual-desktop-session"

        def rename(self, session_id, title):
            self.title = title

        def session_info(self, session_id):
            return {"native_session_id": session_id, "cwd": self.cwd, "title": self.title}

        def events(self, session_id):
            assert session_id == "actual-desktop-session"
            return self.rows

        def prompt(self, session_id, text, request_id):
            self.prompts.append((session_id, text, request_id))
            inbox = json.loads((self.cwd / "agentphone-inbox.json").read_text(encoding="utf-8"))
            reply = dict(
                action="send_message",
                recipient_id="original",
                content="Desktop agent's explicit outgoing message",
                reply_to_message_id=request_id,
                received_message_id=request_id,
                received_content=inbox["content"],
            )
            final = "arbitrary final prose" if self.plain_output else json.dumps(reply)
            self.rows = self.rows + [
                dict(
                    seq=len(self.rows),
                    type="assistant/message",
                    data={
                        "message": {
                            "content": [
                                {"type": "reasoning", "text": "private reasoning, never forwarded"},
                                {"type": "text", "text": final},
                            ]
                        }
                    },
                ),
                dict(seq=len(self.rows) + 1, type="turn/end", data={}),
            ]

        def select_window(self, title, stop_event=None, session_id=None):
            if self.fail_window:
                raise RuntimeError("Native window unavailable")
            self.selected.append(title)

        def cancel(self, session_id):
            self.cancelled.append(session_id)

    monkeypatch.setattr("agentphone.desktop.dsh_desktop.DshDesktopClient", NativeClient)
    target = AgentInstallation("deepseek_harness", "DSH", application="installed.exe")
    adapter = DeepSeekAdapter(SimpleNamespace(url="http://127.0.0.1:8768"), tmp_path, target)
    link = dict(
        id="bridge-unique-id",
        session_b=None,
        binding_b="binding",
        agent_a="original",
        agent_b="deepseek_harness",
        session_a="original-native-session",
    )
    adapter.on_native_session = lambda native: link.update(session_b=native)
    adapter.start_session(link)
    return adapter


def incoming(message_id="first"):
    return dict(
        message_id=message_id,
        conversation_id="bridge-unique-id",
        sender_id="original",
        recipient_id="deepseek_harness",
        content="Actual incoming content",
        timestamp="now",
    )


def test_native_desktop_uses_one_session_and_opens_its_real_conversation(native_adapter):
    adapter = native_adapter
    first = adapter.receive_message(incoming())
    second = adapter.receive_message(incoming("second"))
    assert adapter.client.creations == [(adapter.bridge.directory, None)]
    assert len(adapter.client.selected) == 1
    assert all("本次通信绑定" not in row[1] for row in adapter.client.prompts)
    assert "本次通信绑定" in (adapter.bridge.directory / "AGENTS.md").read_text(encoding="utf-8")
    assert "Actual incoming content" in adapter.client.prompts[0][1]
    assert {row[0] for row in adapter.client.prompts} == {"actual-desktop-session"}
    assert first["received_message_id"] == "first" and second["received_message_id"] == "second"
    assert "private reasoning" not in json.dumps(first)


def test_native_window_failure_is_visible_without_cancelling_agent(native_adapter):
    native_adapter.client.fail_window = True
    with pytest.raises(RuntimeError, match="Native window unavailable"):
        native_adapter.receive_message(incoming())
    assert native_adapter.status == "FAILED"
    native_adapter.stop_session()  # The bridge error cleanup must not cancel it either.
    assert native_adapter.client.cancelled == []


def test_native_arbitrary_final_output_never_becomes_outgoing_message(native_adapter):
    native_adapter.client.plain_output = True
    with pytest.raises(RuntimeError, match="JSON"):
        native_adapter.receive_message(incoming())
    assert native_adapter.client.cancelled == []


def test_long_native_work_can_reply_after_three_minutes(native_adapter, monkeypatch):
    adapter = native_adapter
    clock = [0]
    original_events = adapter.client.events

    def events(session_id):
        # No turn/end for a simulated ten minutes. Never actually sleep in this test.
        return original_events(session_id) if clock[0] >= 600 else []

    monkeypatch.setattr(adapter.client, "events", events)
    monkeypatch.setattr("agentphone.desktop.adapters.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "agentphone.desktop.adapters.time.sleep", lambda _: clock.__setitem__(0, clock[0] + 60)
    )
    result = adapter.receive_message(incoming())
    assert clock[0] >= 600
    assert result["received_message_id"] == "first"
    assert result["action"] == "send_message"
    assert len(adapter.client.prompts) == 1
    assert adapter.client.cancelled == []


def test_temporary_read_timeout_retries_same_session_without_resending(native_adapter, monkeypatch):
    adapter = native_adapter
    calls = [0]
    original_events = adapter.client.events

    def events(session_id):
        calls[0] += 1
        if calls[0] in (2, 3):
            raise DshReadUnavailable("read timeout")
        return original_events(session_id)

    monkeypatch.setattr(adapter.client, "events", events)
    monkeypatch.setattr("agentphone.desktop.adapters.time.sleep", lambda _: None)
    result = adapter.receive_message(incoming())
    assert result["received_message_id"] == "first"
    assert calls[0] == 4
    assert len(adapter.client.prompts) == 1
    assert adapter.client.cancelled == []


def test_persistent_observation_loss_reports_error_without_cancelling(native_adapter, monkeypatch):
    adapter = native_adapter
    clock = [0]

    def events(_session_id):
        if adapter.client.prompts:
            raise DshReadUnavailable("read timeout")
        return []

    monkeypatch.setattr(adapter.client, "events", events)
    monkeypatch.setattr("agentphone.desktop.adapters.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "agentphone.desktop.adapters.time.sleep", lambda _: clock.__setitem__(0, clock[0] + 10)
    )
    with pytest.raises(RuntimeError, match="没有取消 DSH"):
        adapter.receive_message(incoming())
    adapter.stop_session()
    assert len(adapter.client.prompts) == 1
    assert adapter.client.cancelled == []


def test_mcp_receipt_releases_delivery_before_native_work_finishes(native_adapter, monkeypatch):
    adapter = native_adapter
    monkeypatch.setattr(adapter.client, "events", lambda _: [])  # Native turn still working.

    def read_receipt(message_id, native_id):
        assert native_id == "actual-desktop-session"
        assert adapter.client.prompts
        return {"received_message_id": message_id, "received_content": "Actual incoming content"}

    adapter.read_native_receipt = read_receipt
    first = adapter.receive_message(incoming())
    second = adapter.receive_message(incoming("second"))
    assert first["action"] == second["action"] == "wait"
    assert first["received_message_id"] == "first"
    assert second["received_message_id"] == "second"
    assert len(adapter.client.selected) == 1
    assert len(adapter.client.creations) == 1
    assert len(adapter.client.prompts) == 2
    assert adapter.client.cancelled == []


def test_bridge_stop_detaches_observer_and_preserves_running_dsh(native_adapter, monkeypatch):
    adapter = native_adapter
    monkeypatch.setattr(adapter.client, "events", lambda _: [])
    monkeypatch.setattr("agentphone.desktop.adapters.time.sleep", lambda _: adapter.stop_session())
    with pytest.raises(RunStopped):
        adapter.receive_message(incoming())
    assert adapter.bridge.stop_event.is_set()
    assert adapter.client.cancelled == []


@pytest.mark.parametrize("method", ["session/projections", "session/page", "session/prompt"])
def test_rpc_timeouts_distinguish_observation_from_uncertain_submission(method):
    client = object.__new__(DshDesktopClient)
    client.authority = "127.0.0.1:12345"
    client.secret = bytes(range(32))

    def fail(*_args, **_kwargs):
        raise TimeoutError("read timed out")

    client.opener = SimpleNamespace(open=fail)
    expected = RuntimeError if method == "session/prompt" else DshReadUnavailable
    with pytest.raises(expected) as error:
        client.rpc(method)
    if method == "session/prompt":
        assert not isinstance(error.value, DshReadUnavailable)
        assert "未自动重发" in str(error.value)


def test_codex_native_work_also_has_no_three_minute_cutoff(tmp_path, monkeypatch):
    adapter = CodexAdapter(SimpleNamespace(url="http://localhost"), tmp_path, None)
    adapter.native_session_id = "original-codex-thread"
    adapter.incoming = incoming()
    adapter.rpc = lambda *_: {"turn": {"id": "turn"}}
    adapter.present_conversation = lambda: None
    clock = [0]
    reply = dict(
        action="wait",
        recipient_id="",
        content="",
        reply_to_message_id=None,
        received_message_id="first",
        received_content="Actual incoming content",
    )
    rows = iter(
        [
            {"method": "progress", "params": {}},
            {"method": "progress", "params": {}},
            {
                "method": "item/completed",
                "params": {
                    "threadId": adapter.native_session_id,
                    "turnId": "turn",
                    "item": {"type": "agentMessage", "text": json.dumps(reply)},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": adapter.native_session_id,
                    "turn": {"id": "turn", "status": "completed"},
                },
            },
        ]
    )

    def get(**_kwargs):
        clock[0] += 100
        return next(rows)

    adapter.events = SimpleNamespace(get=get)
    monkeypatch.setattr("agentphone.desktop.adapters.time.monotonic", lambda: clock[0])
    assert adapter.send_message("real prompt")["received_message_id"] == "first"
    assert clock[0] == 400


async def test_live_turn_receipts_drain_bridge_queue_and_later_replies_still_route(
    native_adapter, service, tmp_path, monkeypatch
):
    from agentphone.bridge import CommunicationBridge

    executable = tmp_path / "installed.exe"
    executable.touch()
    target = AgentInstallation(
        "deepseek_harness", "DSH", command=str(executable), application=str(executable)
    )
    bridge = CommunicationBridge(
        service, tmp_path, lambda: {target.id: target}, lambda *_: native_adapter
    )
    bridge.url = "http://127.0.0.1:8768"
    await bridge.initialize()
    loop = asyncio.get_running_loop()
    native_class = type(native_adapter.client)

    def prompt(client, session_id, text, request_id):
        client.prompts.append((session_id, text, request_id))
        # The native Agent reads through the real router, but continues working:
        # there is deliberately no final output or turn/end in its event stream.
        asyncio.run_coroutine_threadsafe(
            bridge.read_messages(
                target.id,
                conversation_id=bridge.link["id"],
                sender_session_id=bridge.link["binding_b"],
            ),
            loop,
        ).result(timeout=3)

    monkeypatch.setattr(native_class, "prompt", prompt)
    try:
        first = await bridge.send_message(
            "original", target.id, "First task", sender_session_id="original-native-session"
        )
        second = await bridge.send_message(
            "original",
            target.id,
            "Follow-up",
            first["conversation_id"],
            sender_session_id="original-native-session",
        )
        await asyncio.wait_for(asyncio.shield(bridge.worker), timeout=5)
        for sent in (first, second):
            message = await bridge.message(sent["id"])
            assert message["status"] == "PROCESSED"
            assert message["receipt"]["native_session_id"] == "actual-desktop-session"
        assert bridge.link["status"] == "CONNECTED"
        assert native_adapter.client.rows == []
        assert len(native_adapter.client.selected) == 1
        await bridge.send_message(
            target.id,
            "original",
            "Explicit late reply",
            first["conversation_id"],
            reply_to_message_id=second["id"],
            sender_session_id=bridge.link["binding_b"],
        )
        inbox = await bridge.read_messages(
            "original",
            conversation_id=first["conversation_id"],
            sender_session_id="original-native-session",
        )
        assert [message["content"] for message in inbox["messages"]] == ["Explicit late reply"]
        assert native_adapter.client.cancelled == []
    finally:
        await bridge.close()
    assert native_adapter.client.cancelled == []


def test_browser_grant_read_does_not_change_credentials_or_select_other_grants(tmp_path):
    secret = bytes(range(32))
    encoded = base64.urlsafe_b64encode(secret).decode().rstrip("=")
    source = tmp_path / ".credentials.yaml"
    source.write_text(
        "version: 1\nrecords:\n  unrelated/provider:\n    kind: grant\n"
        "    payload:\n      secret: other-key\n"
        "  client-connection/browser-session:\n    kind: grant\n    payload:\n"
        f"      version: 1\n      secret: {encoded}\n"
        "  another/provider:\n    kind: grant\n    payload:\n      secret: other-key\n",
        encoding="utf-8",
    )
    original = source.read_bytes()
    assert browser_secret(tmp_path) == secret
    assert source.read_bytes() == original


def test_dsh_cli_detection_alone_cannot_claim_native_gui_support(tmp_path):
    command = tmp_path / "dsh.cmd"
    command.touch()
    target = AgentInstallation("deepseek_harness", "DSH", command=str(command))
    assert target.callable
    assert not session_adapter_available(target)
    application = tmp_path / "DSH.exe"
    application.touch()
    target.application = str(application)
    assert session_adapter_available(target)


def test_browser_cookie_remains_valid_when_node_clock_lags_python():
    secret = bytes(range(32))
    now = 1800000000000
    cookie = browser_cookie("127.0.0.1:12345", secret, now)
    value = cookie.split("=", 1)[1]
    version, body, signature = value.split(".")

    def decode(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    payload = json.loads(decode(body))
    assert version == "v1"
    assert hmac.compare_digest(
        decode(signature), hmac.new(secret, body.encode(), hashlib.sha256).digest()
    )
    assert payload["authority"] == "127.0.0.1:12345"
    for node_now in (now - 16, now, now + 16):
        assert payload["issuedAt"] <= node_now < payload["expiresAt"]
    assert payload["expiresAt"] - payload["issuedAt"] == 300000


def test_existing_dsh_preserves_project_title_and_native_id(native_adapter, monkeypatch, tmp_path):
    project = tmp_path / "original-project"
    project.mkdir()
    cls = type(native_adapter.client)
    monkeypatch.setattr(
        cls,
        "session_info",
        lambda self, native: {
            "native_session_id": native,
            "cwd": str(project),
            "title": "My original conversation",
        },
    )
    native_adapter.start_session(dict(native_adapter.link, session_b="actual-desktop-session"))
    assert native_adapter.client.creations == [(str(project), "actual-desktop-session")]
    assert native_adapter.title == "My original conversation"
    assert not hasattr(native_adapter.client, "title")


@pytest.mark.parametrize("reported", ["original-native-id", "wrong-replacement"])
def test_codex_resume_never_forks_or_changes_original_config(tmp_path, monkeypatch, reported):
    adapter = CodexAdapter(SimpleNamespace(url="http://localhost"), tmp_path, None)
    monkeypatch.setattr(adapter, "start_transport", lambda: None)
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return {"thread": {"id": reported}}

    adapter.rpc = rpc
    link = dict(
        id="cid",
        agent_a="original",
        agent_b="codex",
        binding_b="binding",
        session_a="a",
        session_b="original-native-id",
    )
    if reported == "wrong-replacement":
        with pytest.raises(RuntimeError, match="禁止新建替代"):
            adapter.start_session(link)
    else:
        assert adapter.start_session(link) == "original-native-id"
    assert calls == [("thread/resume", {"threadId": "original-native-id"})]


def test_native_session_metadata_does_not_expose_other_projection_data():
    client = object.__new__(DshDesktopClient)
    calls = []

    def rpc(method, request):
        calls.append((method, request))
        return {
            "items": [
                {
                    "sessionId": "session-real",
                    "cwd": "original-project",
                    "running": True,
                    "updatedAt": 123,
                    "projections": {
                        "values": {"title": "Original name", "private": "must not return"}
                    },
                }
            ]
        }

    client.rpc = rpc
    rows = client.sessions()
    assert rows == [
        {
            "native_session_id": "session-real",
            "title": "Original name",
            "cwd": "original-project",
            "running": True,
            "updated_at": 123,
        }
    ]
    assert calls == [("session/list", {})]


def test_dsh_window_rejects_ambiguous_title_before_native_helper(monkeypatch):
    client = object.__new__(DshDesktopClient)
    client.sessions = lambda: [
        {"native_session_id": "session-one", "title": "Same"},
        {"native_session_id": "session-two", "title": "Same"},
    ]
    with pytest.raises(RuntimeError, match="同名对话"):
        client.select_window("Same", session_id="session-one")
