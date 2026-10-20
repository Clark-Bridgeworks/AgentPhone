import asyncio

import pytest

from agentphone.config import Settings
from agentphone.db import Database
from agentphone.errors import PhoneError
from agentphone.service import PhoneService


async def test_request_reply_and_restart(service):
    request = await service.send_message(
        "agent_a", "agent_b", "Review function X", metadata={"x": 1}
    )
    received = await service.read_messages("agent_b")
    assert [m.id for m in received] == [request.id]
    assert received[0].status == "read"
    reply = await service.send_message(
        "agent_b", "agent_a", "Reviewed", reply_to_message_id=request.id
    )
    assert reply.conversation_id == request.conversation_id
    assert [m.id for m in await service.read_messages("agent_a")] == [reply.id]
    restored = PhoneService(Database(service.database.path), service.settings)
    await restored.initialize()
    history = await restored.get_conversation(request.conversation_id)
    assert [m.id for m in history["messages"]] == [request.id, reply.id]
    assert history["messages"][0].metadata == {"x": 1}
    assert len([e for e in await restored.get_events() if e.type == "message.created"]) == 2


async def test_first_read_transition_only(service):
    message = await service.send_message("agent_a", "agent_b", "hello")
    assert (await service.read_messages("agent_b", mark_read=False))[0].status == "stored"
    await asyncio.gather(*(service.read_messages("agent_b") for _ in range(5)))
    read_events = [e for e in await service.get_events() if e.type == "message.read"]
    assert [e.message_id for e in read_events] == [message.id]


async def test_inbox_cursor_order_and_scope(service):
    messages = [await service.send_message("agent_a", "agent_b", str(n)) for n in range(5)]
    first = await service.read_messages("agent_b", limit=2)
    second = await service.read_messages("agent_b", after_message_id=first[-1].id, limit=3)
    assert [m.id for m in first + second] == [m.id for m in messages]
    assert await service.read_messages("agent_b", after_message_id=second[-1].id) == []
    with pytest.raises(PhoneError, match="inbox"):
        await service.read_messages("agent_a", after_message_id=first[0].id)
    with pytest.raises(PhoneError, match="does not exist"):
        await service.read_messages("agent_b", after_message_id="missing")
    with pytest.raises(PhoneError, match="filter"):
        await service.read_messages(
            "agent_b", after_message_id=first[0].id, conversation_id=messages[-1].conversation_id
        )


async def test_validation_rolls_back_all_state(service):
    before = await service.get_events()
    with pytest.raises(PhoneError, match="not registered"):
        await service.send_message("agent_a", "unknown", "hello")
    with pytest.raises(PhoneError, match="content"):
        await service.send_message("agent_a", "agent_b", " " * 2)
    with pytest.raises(PhoneError, match="content"):
        await service.send_message("agent_a", "agent_b", "x" * 16001)
    with pytest.raises(PhoneError, match="metadata"):
        await service.send_message("agent_a", "agent_b", "hello", metadata={"bad": float("nan")})
    assert await service.get_events() == before
    with service.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


async def test_atomic_conversation_limit(service):
    settings = Settings(database_path=service.database.path, max_messages_per_conversation=3)
    limited = PhoneService(service.database, settings)
    first = await limited.send_message("agent_a", "agent_b", "first")
    results = await asyncio.gather(
        *(
            limited.send_message(
                "agent_a", "agent_b", str(n), conversation_id=first.conversation_id
            )
            for n in range(10)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert all(isinstance(r, PhoneError) for r in results if isinstance(r, Exception))
    history = await limited.get_conversation(first.conversation_id)
    assert len(history["messages"]) == 3
    assert len([e for e in await limited.get_events() if e.type == "message.created"]) == 3


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
async def test_invalid_page_size(service, limit):
    with pytest.raises(PhoneError):
        await service.read_messages("agent_b", limit=limit)
