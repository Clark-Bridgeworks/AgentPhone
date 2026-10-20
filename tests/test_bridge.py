import asyncio
import json

import pytest
from mcp.client import Client

from agentphone.bridge import CommunicationBridge
from agentphone.desktop.adapters import decode_action
from agentphone.desktop.app import pane_entries
from agentphone.desktop.discovery import AgentInstallation
from agentphone.errors import PhoneError
from agentphone.mcp_server import create_mcp


@pytest.fixture
async def bridge(service, tmp_path):
    command = tmp_path / "target.cmd"
    command.touch()
    target = AgentInstallation("target_x", "Independent target", command=str(command))
    instances = []

    class Adapter:
        def __init__(self):
            self.native_session_id = None
            self.received = []
            self.bad_receipt = False
            self.new_native = False
            self.stopped = False

        def start_session(self, link):
            self.restored_from = link["session_b"]
            self.native_session_id = "real-runtime-id-in-transport-fixture"
            self.on_native_session(self.native_session_id)

        def receive_message(self, message):
            self.received.append(message)
            if self.new_native:
                self.on_native_session("replacement-session-must-be-rejected")
            return dict(
                action="wait",
                recipient_id="",
                content="not a bridge summary",
                reply_to_message_id=None,
                received_message_id="wrong" if self.bad_receipt else message["message_id"],
                received_content=message["content"],
            )

        def stop_session(self):
            self.stopped = True

    def factory(*args):
        adapter = Adapter()
        instances.append(adapter)
        return adapter

    result = CommunicationBridge(service, tmp_path, lambda: {target.id: target}, factory)
    result.runtime_keys = {"target_x"}
    result.instances = instances
    await result.initialize()
    yield result
    await result.close()


async def settle(bridge):
    await asyncio.wait_for(asyncio.shield(bridge.worker), timeout=5)


async def test_pause_resume_keeps_pair_and_unread_reply_then_switch_creates_new_pair(bridge):
    first = await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    old_adapter = bridge.adapter
    await bridge.send_message(
        "target_x",
        "external",
        "Unread",
        first["conversation_id"],
        sender_session_id=bridge.link["binding_b"],
    )
    previous = dict(bridge.link)
    paused = await bridge.disconnect(first["conversation_id"])
    assert paused["bridge"]["status"] == "DISCONNECTED" and not paused["busy"]
    assert bridge.adapter is None
    assert old_adapter.stopped and not bridge.closed
    resumed = await bridge.send_message(
        "external", "target_x", "Continue", first["conversation_id"], sender_session_id="native-a"
    )
    await settle(bridge)
    assert resumed["conversation_id"] == previous["id"]
    assert bridge.link["session_b"] == previous["session_b"]
    assert bridge.link["binding_b"] == previous["binding_b"]
    inbox = await bridge.read_messages("external", sender_session_id="native-a")
    assert inbox["messages"][0]["content"] == "Unread"
    second = await bridge.send_message(
        "external", "target_x", "New", sender_session_id="native-a", new_connection=True
    )
    await settle(bridge)
    assert second["conversation_id"] != first["conversation_id"]
    assert len(bridge.instances) == 3
    assert len((await bridge.snapshot())["messages"]) == 1


async def test_busy_disconnect_does_not_stop_delivery(bridge):
    import threading

    gate = threading.Event()
    original_factory = bridge.adapter_factory

    def factory(*args):
        adapter = original_factory(*args)
        receive = adapter.receive_message

        def blocked(message):
            gate.wait(timeout=5)
            return receive(message)

        adapter.receive_message = blocked
        return adapter

    bridge.adapter_factory = factory
    try:
        sent = await bridge.send_message(
            "external", "target_x", "Busy", sender_session_id="native-a"
        )
        assert (await bridge.snapshot())["busy"]
        with pytest.raises(PhoneError, match="消息正在收发"):
            await bridge.disconnect(sent["conversation_id"])
        assert bridge.link["id"] == sent["conversation_id"]
    finally:
        gate.set()
        await settle(bridge)


async def test_pause_interrupts_poll_and_can_resume_same_context_after_restart(bridge):
    sent = await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    waiting = asyncio.create_task(
        bridge.read_messages("external", sender_session_id="native-a", wait_seconds=2)
    )
    await asyncio.sleep(0.05)
    await bridge.disconnect(sent["conversation_id"])
    with pytest.raises(PhoneError, match="连接已切换"):
        await waiting
    await bridge.close()
    restored = CommunicationBridge(
        bridge.service, bridge.directory, bridge.installations, bridge.adapter_factory
    )
    restored.runtime_keys = bridge.runtime_keys
    await restored.initialize()
    try:
        continued = await restored.send_message(
            "external", "target_x", "Late", sent["conversation_id"], sender_session_id="native-a"
        )
        await settle(restored)
        assert continued["conversation_id"] == sent["conversation_id"]
        assert restored.link["session_b"] == bridge.link["session_b"]
    finally:
        await restored.close()


async def test_mcp_switch_releases_pair_and_invalid_target_preserves_it(bridge):
    sent = await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    async with Client(create_mcp(bridge.service, bridge)) as client:
        invalid = await client.call_tool(
            "send_message",
            dict(
                sender="external",
                recipient="missing",
                content="Switch",
                sender_session_id="native-a",
                new_connection=True,
            ),
        )
        assert invalid.is_error and bridge.link["id"] == sent["conversation_id"]
        switched = await client.call_tool(
            "send_message",
            dict(
                sender="external",
                recipient="target_x",
                content="Next pair",
                sender_session_id="native-a",
                new_connection=True,
            ),
        )
        assert not switched.is_error
        assert switched.structured_content["conversation_id"] != sent["conversation_id"]
    await settle(bridge)
    assert bridge.instances[0].stopped


async def test_first_call_binds_existing_generic_a_and_lazily_creates_b(bridge):
    assert bridge.adapter is None and bridge.link is None
    sent = await bridge.send_message(
        "claude-current",
        "target_x",
        "Original message",
        sender_session_id="existing-claude-native-session",
    )
    assert sent["sender_session_id"] == "existing-claude-native-session"
    await settle(bridge)
    snapshot = await bridge.snapshot()
    assert snapshot["bridge"]["session_a"] == "existing-claude-native-session"
    assert len(bridge.instances) == 1
    assert len(snapshot["messages"]) == 1  # no system summary, task or manufactured peer reply
    assert snapshot["messages"][0]["status"] == "PROCESSED"
    assert pane_entries(snapshot, "a")[0]["direction"] == "outgoing"
    assert pane_entries(snapshot, "b")[0]["direction"] == "incoming"
    assert (
        pane_entries(snapshot, "a")[0]["message_id"] == pane_entries(snapshot, "b")[0]["message_id"]
    )


async def test_continuity_and_active_reverse_message_do_not_invoke_a(bridge):
    first = await bridge.send_message(
        "some-agent", "target_x", "First", sender_session_id="native-a"
    )
    await settle(bridge)
    native_b = bridge.link["session_b"]
    await bridge.send_message(
        "some-agent", "target_x", "Second", first["conversation_id"], sender_session_id="native-a"
    )
    await settle(bridge)
    assert len(bridge.instances) == 1 and bridge.link["session_b"] == native_b
    adapter = bridge.instances[0]
    reverse = await bridge.send_message(
        "target_x",
        "some-agent",
        "New unsolicited communication",
        bridge.link["id"],
        None,
        bridge.link["binding_b"],
    )
    assert reverse["reply_to_message_id"] is None
    assert len(adapter.received) == 2  # no synthesized A turn or automatic alternation
    queued = await bridge.snapshot()
    assert reverse["id"] not in [e["message_id"] for e in pane_entries(queued, "a")]
    inbox = await bridge.read_messages("some-agent", sender_session_id="native-a")
    assert inbox["messages"][0]["content"] == "New unsolicited communication"
    assert inbox["messages"][0]["receipt"]["native_session_id"] == "native-a"
    delivered = await bridge.snapshot()
    assert reverse["id"] in [e["message_id"] for e in pane_entries(delivered, "a")]
    assert reverse["id"] in [e["message_id"] for e in pane_entries(delivered, "b")]


async def test_other_session_or_third_agent_cannot_enter_current_bridge(bridge):
    await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    before = await bridge.snapshot()
    with pytest.raises(PhoneError, match="different native session"):
        await bridge.send_message(
            "external", "target_x", "Different chat", sender_session_id="wrong"
        )
    with pytest.raises(PhoneError, match="exactly two"):
        await bridge.send_message(
            "external", "third", "Third endpoint", sender_session_id="native-a"
        )
    with pytest.raises(PhoneError, match="different native session"):
        await bridge.read_messages("external", sender_session_id="wrong")
    assert (await bridge.snapshot())["messages"] == before["messages"]


async def test_wrong_receipt_fails_without_showing_received_content(bridge):
    await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    bridge.instances[0].bad_receipt = True
    await bridge.send_message(
        "external", "target_x", "Must not claim delivery", sender_session_id="native-a"
    )
    await settle(bridge)
    snapshot = await bridge.snapshot()
    assert snapshot["bridge"]["status"] == "FAILED"
    failed = snapshot["messages"][-1]
    assert failed["status"] == "FAILED" and failed["receipt"] is None
    assert failed["id"] not in [e["message_id"] for e in pane_entries(snapshot, "b")]
    assert failed["id"] in [e["message_id"] for e in pane_entries(snapshot, "a")]


async def test_native_session_replacement_is_never_silent(bridge):
    await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    old_native = bridge.link["session_b"]
    bridge.instances[0].new_native = True
    await bridge.send_message("external", "target_x", "Second", sender_session_id="native-a")
    await settle(bridge)
    assert bridge.link["status"] == "FAILED" and bridge.link["session_b"] == old_native


async def test_invalid_first_message_does_not_start_a_session(bridge):
    with pytest.raises(PhoneError):
        await bridge.send_message("external", "target_x", "", sender_session_id="native-a")
    with pytest.raises(PhoneError):
        await bridge.send_message(
            "external",
            "target_x",
            "Hi",
            reply_to_message_id="foreign",
            sender_session_id="native-a",
        )
    assert bridge.link is None and bridge.instances == []


async def test_real_mcp_contract_reaches_same_bridge_and_binds_current_session(bridge):
    async with Client(create_mcp(bridge.service, bridge)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {"get_agents", "send_message", "read_messages", "get_conversation"}
        sent = await client.call_tool(
            "send_message",
            dict(
                sender="external",
                recipient="target_x",
                content="MCP transport",
                wait_seconds=0,
                sender_session_id="current-session",
            ),
        )
        assert not sent.is_error
        await settle(bridge)
        assert (await bridge.snapshot())["bridge"]["session_a"] == "current-session"
        agents = (await client.call_tool("get_agents", {})).structured_content["agents"]
        assert agents[0]["can_contact"]
        history = await client.call_tool("get_conversation", {"conversation_id": bridge.link["id"]})
        delivered = history.structured_content["messages"][0]
        assert delivered["status"] == "PROCESSED"
        assert delivered["receipt"]["received_content"] == "MCP transport"
        assert delivered["recipient_session_id"] == bridge.link["session_b"]


async def test_close_preserves_unread_reverse_inbox_and_does_not_judge_work_completed(bridge):
    await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    await bridge.send_message(
        "target_x", "external", "Unread reply", bridge.link["id"], None, bridge.link["binding_b"]
    )
    await bridge.close()
    assert bridge.link["status"] == "DISCONNECTED"
    assert (await bridge.snapshot())["messages"][-1]["status"] == "PENDING"


def test_wait_can_be_empty_and_never_requires_an_automatic_summary():
    import json

    action = dict(
        action="wait",
        recipient_id="",
        content="",
        reply_to_message_id=None,
        received_message_id="real-message",
        received_content="actual-inbox-content",
    )
    assert decode_action(json.dumps(action))["content"] == ""
    with pytest.raises(RuntimeError):
        decode_action(json.dumps(dict(action, action="send_message", recipient_id="peer")))


async def test_existing_external_session_restores_original_target_without_replacing_ids(bridge):
    first = await bridge.send_message("external", "target_x", "First", sender_session_id="native-a")
    await settle(bridge)
    reverse = await bridge.send_message(
        "target_x", "external", "Target question", bridge.link["id"], None, bridge.link["binding_b"]
    )
    await bridge.read_messages("external", sender_session_id="native-a")
    previous = dict(bridge.link)
    await bridge.close()
    restored = CommunicationBridge(
        bridge.service, bridge.directory, bridge.installations, bridge.adapter_factory
    )
    restored.runtime_keys = bridge.runtime_keys
    await restored.initialize()
    try:
        with pytest.raises(PhoneError):
            await restored.send_message(
                "external",
                "target_x",
                "Other chat",
                first["conversation_id"],
                sender_session_id="wrong-session",
            )
        assert restored.link["id"] == first["conversation_id"] and restored.adapter is None
        await restored.send_message(
            "external",
            "target_x",
            "Answer after restart",
            first["conversation_id"],
            reply_to_message_id=reverse["id"],
            sender_session_id="native-a",
        )
        await settle(restored)
        assert restored.link["session_a"] == previous["session_a"]
        assert restored.link["session_b"] == previous["session_b"]
        assert restored.link["binding_b"] == previous["binding_b"]
    finally:
        await restored.close()


async def test_send_tool_returns_real_reply_to_original_context_without_separate_read(bridge):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    adapter = bridge.adapter
    original = adapter.receive_message

    def reply(envelope):
        return dict(
            original(envelope),
            action="send_message",
            recipient_id="external",
            content="中文回信🌙",
            reply_to_message_id=envelope["message_id"],
        )

    adapter.receive_message = reply
    async with Client(create_mcp(bridge.service, bridge)) as client:
        sent = await client.call_tool(
            "send_message",
            dict(
                sender="external",
                recipient="target_x",
                content="中文请求☀️",
                conversation_id=first["conversation_id"],
                sender_session_id="native-a",
                wait_seconds=2,
            ),
        )
        result = sent.structured_content
        assert not sent.is_error and not result["awaiting_reply"]
        assert result["inbox"][0]["content"] == "中文回信🌙"
        assert result["inbox"][0]["receipt"]["native_session_id"] == "native-a"
    await settle(bridge)


async def test_unread_reply_survives_restart_and_requires_original_identity(bridge):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    reverse = await bridge.send_message(
        "target_x",
        "external",
        "保留回信",
        first["conversation_id"],
        sender_session_id=bridge.link["binding_b"],
    )
    await bridge.close()
    restored = CommunicationBridge(
        bridge.service, bridge.directory, bridge.installations, bridge.adapter_factory
    )
    await restored.initialize()
    try:
        with pytest.raises(PhoneError):
            await restored.read_messages(
                "external", conversation_id=first["conversation_id"], sender_session_id="wrong"
            )
        assert restored.link["id"] == first["conversation_id"] and restored.adapter is None
        inbox = await restored.read_messages(
            "external", conversation_id=first["conversation_id"], sender_session_id="native-a"
        )
        assert inbox["messages"][0]["id"] == reverse["id"]
        assert inbox["messages"][0]["status"] == "DELIVERED"
        assert restored.adapter is None
    finally:
        await restored.close()


async def test_invalid_wait_never_sends_or_creates_session(bridge):
    with pytest.raises(PhoneError):
        await bridge.send_and_receive(
            "external", "target_x", "No send", sender_session_id="native-a", wait_seconds=31
        )
    assert bridge.link is None and not bridge.instances


async def test_failed_link_recovers_same_ids_without_replaying_old_work(bridge):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    previous = dict(bridge.link)
    await bridge._delivery(first["id"], "FAILED", previous["session_b"], error="old timeout")
    await bridge._state("FAILED", "DSH 原生对话未在规定时间内返回通信消息。")
    sent = await bridge.send_message(
        "external", "target_x", "New message", previous["id"], sender_session_id="native-a"
    )
    await settle(bridge)
    assert bridge.link["status"] == "CONNECTED" and bridge.link["error"] is None
    assert bridge.link["id"] == previous["id"]
    assert bridge.adapter.restored_from == previous["session_b"]
    assert [r["content"] for r in bridge.adapter.received] == ["New message"]
    assert (await bridge.message(first["id"]))["status"] == "FAILED"
    assert (await bridge.message(sent["id"]))["status"] == "PROCESSED"


async def test_native_b_can_reply_after_restart_without_failure_lock(bridge):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    previous = dict(bridge.link)
    await bridge._state("FAILED", "old timeout")
    await bridge.close()
    restored = CommunicationBridge(
        bridge.service, bridge.directory, bridge.installations, bridge.adapter_factory
    )
    await restored.initialize()
    try:
        with pytest.raises(PhoneError):
            await restored.send_message(
                "target_x", "external", "Wrong", previous["id"], sender_session_id="wrong-b"
            )
        sent = await restored.send_message(
            "target_x", "external", "Late reply", previous["id"], first["id"], previous["binding_b"]
        )
        inbox = await restored.read_messages(
            "external", conversation_id=previous["id"], sender_session_id="native-a"
        )
        assert inbox["messages"][0]["id"] == sent["id"]
        assert restored.adapter is None and restored.link["session_b"] == previous["session_b"]
        assert restored.link["status"] == "CONNECTED"
    finally:
        await restored.close()


async def test_manual_resume_attaches_without_sending_work(bridge):
    await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    previous = dict(bridge.link)
    await bridge.disconnect(previous["id"])
    snapshot = await bridge.resume(previous["id"])
    assert snapshot["bridge"]["status"] == "CONNECTED"
    assert bridge.adapter.restored_from == previous["session_b"]
    assert bridge.adapter.received == [] and len(snapshot["messages"]) == 1


@pytest.fixture
def native_catalog_fixture(bridge, monkeypatch):
    class Catalog:
        created = []

        def target(self, agent_id):
            if agent_id not in {"external", "target_x"}:
                raise RuntimeError("Unsupported target")

        def inspect(self, agent_id, native):
            self.target(agent_id)
            return {"native_session_id": native, "cwd": "original-project", "title": "Original"}

        def create_a(self, agent_id, cwd):
            self.created.append((agent_id, cwd))
            return {"native_session_id": "new-original-a", "cwd": cwd, "title": "New original"}

    catalog = Catalog()
    monkeypatch.setattr(bridge, "native_catalog", lambda: catalog)
    return catalog


async def test_select_original_pair_preserves_connection_and_unread_reply(
    bridge, native_catalog_fixture
):
    await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    previous = dict(bridge.link)
    reverse = await bridge.send_message(
        "target_x", "external", "Unread", previous["id"], sender_session_id=previous["binding_b"]
    )
    await bridge._state("FAILED", "old timeout")
    selected = await bridge.select_endpoints(
        {
            "a": {"agent_id": "external", "mode": "existing", "native_session_id": "native-a"},
            "b": {
                "agent_id": "target_x",
                "mode": "existing",
                "native_session_id": previous["session_b"],
            },
        }
    )
    assert selected["bridge"]["id"] == previous["id"]
    assert selected["bridge"]["status"] == "CONNECTED" and bridge.adapter.received == []
    assert native_catalog_fixture.created == []
    assert (await bridge.message(reverse["id"]))["status"] == "PENDING"


async def test_selected_existing_b_enforces_true_a_identity(bridge, native_catalog_fixture):
    native = "real-runtime-id-in-transport-fixture"
    await bridge.select_endpoints(
        {
            "a": {"agent_id": "external", "mode": "existing", "native_session_id": "native-a"},
            "b": {"agent_id": "target_x", "mode": "existing", "native_session_id": native},
        }
    )
    with pytest.raises(PhoneError, match="选定的 A"):
        await bridge.send_message("external", "target_x", "Wrong", sender_session_id="wrong-a")
    assert bridge.link is None
    await bridge.send_message("external", "target_x", "Right", sender_session_id="native-a")
    await settle(bridge)
    assert bridge.adapter.restored_from == native
    assert native_catalog_fixture.created == []


async def test_new_a_is_empty_original_window_and_new_b_stays_lazy(
    bridge, native_catalog_fixture, tmp_path
):
    selected = await bridge.select_endpoints(
        {
            "a": {"agent_id": "external", "mode": "new", "cwd": str(tmp_path)},
            "b": {"agent_id": "target_x", "mode": "new"},
        }
    )
    assert native_catalog_fixture.created == [("external", str(tmp_path))]
    assert selected["selection"]["a"]["native_session_id"] == "new-original-a"
    assert "native_session_id" not in selected["selection"]["b"]
    assert bridge.link is None and bridge.adapter is None
    await bridge.send_message("external", "target_x", "First", sender_session_id="new-original-a")
    await settle(bridge)
    assert bridge.adapter.restored_from is None


async def test_invalid_b_never_opens_a_or_detaches_valid_pair(
    bridge, native_catalog_fixture, tmp_path
):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    adapter = bridge.adapter
    with pytest.raises(RuntimeError, match="Unsupported"):
        await bridge.select_endpoints(
            {
                "a": {"agent_id": "external", "mode": "new", "cwd": str(tmp_path)},
                "b": {"agent_id": "missing", "mode": "new"},
            }
        )
    assert bridge.link["id"] == first["conversation_id"] and bridge.adapter is adapter
    assert native_catalog_fixture.created == []


@pytest.mark.parametrize("operation", ["send", "read"])
async def test_old_request_cannot_take_over_selected_new_dialog(
    bridge, native_catalog_fixture, operation
):
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="old-a")
    await settle(bridge)
    old = dict(bridge.link)
    await bridge.select_endpoints(
        {
            "a": {"agent_id": "external", "mode": "existing", "native_session_id": "new-a"},
            "b": {"agent_id": "target_x", "mode": "new"},
        }
    )
    # An older saved failed link can outlive the current frontend selection.
    await bridge.service._write(
        lambda repo, _: repo.connection.execute(
            "UPDATE bridge_links SET status='FAILED' WHERE id=?", (old["id"],)
        )
    )
    with pytest.raises(PhoneError) as caught:
        if operation == "send":
            await bridge.send_message(
                "external", "target_x", "Late", old["id"], sender_session_id="old-a"
            )
        else:
            await bridge.read_messages(
                "external", conversation_id=old["id"], sender_session_id="old-a"
            )
    assert caught.value.code == "selection_mismatch"
    assert bridge.link is None and bridge.adapter is None
    assert bridge.selection["a"]["native_session_id"] == "new-a"
    assert len(bridge.instances) == 1
    assert len((await bridge.service.get_conversation(first["conversation_id"]))["messages"]) == 1


async def test_restart_preserves_pending_new_b_without_adopting_an_old_b(bridge):
    await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    await bridge.disconnect()
    selection = {
        "a": {"agent_id": "external", "mode": "existing", "native_session_id": "native-a"},
        "b": {"agent_id": "target_x", "mode": "new"},
    }
    bridge.selection_path.write_text(json.dumps(selection), encoding="utf-8")
    await bridge.close()
    restarted = CommunicationBridge(
        bridge.service, bridge.directory, bridge.installations, bridge.adapter_factory
    )
    await restarted.initialize()
    try:
        assert restarted.link is None and restarted.selection == selection
    finally:
        await restarted.close()


async def test_target_switch_against_selection_preserves_current_pair(
    bridge, native_catalog_fixture
):
    await bridge.select_endpoints(
        {
            "a": {"agent_id": "external", "mode": "existing", "native_session_id": "native-a"},
            "b": {"agent_id": "target_x", "mode": "new"},
        }
    )
    first = await bridge.send_message("external", "target_x", "Start", sender_session_id="native-a")
    await settle(bridge)
    adapter = bridge.adapter
    with pytest.raises(PhoneError) as caught:
        await bridge.send_message(
            "external", "other-target", "Switch", sender_session_id="native-a", new_connection=True
        )
    assert caught.value.code == "selection_mismatch"
    assert bridge.link["id"] == first["conversation_id"] and bridge.adapter is adapter
    assert bridge.link["status"] == "CONNECTED" and not adapter.stopped
