import pytest

from agentphone.errors import PhoneError


async def test_history_pages_newest_window_in_chronological_order(service):
    first = await service.send_message("agent_a", "agent_b", "0")
    messages = [first] + [
        await service.send_message(
            "agent_a", "agent_b", str(n), conversation_id=first.conversation_id
        )
        for n in range(1, 7)
    ]
    newest = await service.get_conversation(first.conversation_id, limit=3)
    older = await service.get_conversation(
        first.conversation_id, limit=3, before_message_id=newest["messages"][0].id
    )
    assert [m.id for m in newest["messages"]] == [m.id for m in messages[-3:]]
    assert [m.id for m in older["messages"]] == [m.id for m in messages[1:4]]
    assert all(m.status == "stored" for m in newest["messages"])


async def test_invalid_reply_and_history_cursor(service):
    first = await service.send_message("agent_a", "agent_b", "first")
    other = await service.send_message("agent_a", "agent_b", "other")
    with pytest.raises(PhoneError, match="original conversation"):
        await service.send_message(
            "agent_b",
            "agent_a",
            "reply",
            conversation_id=other.conversation_id,
            reply_to_message_id=first.id,
        )
    await service.register_agent("agent_c", "C")
    with pytest.raises(PhoneError, match="original agents"):
        await service.send_message("agent_c", "agent_a", "reply", reply_to_message_id=first.id)
    with pytest.raises(PhoneError, match="different conversation"):
        await service.get_conversation(first.conversation_id, before_message_id=other.id)
    with pytest.raises(PhoneError, match="does not exist"):
        await service.get_conversation("missing")
