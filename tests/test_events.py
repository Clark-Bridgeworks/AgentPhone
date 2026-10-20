import asyncio

import pytest

from agentphone.errors import PhoneError
from agentphone.service import PhoneService


async def test_event_target_cursor_and_long_wait(service):
    cursor = (await service.get_events())[-1].id
    waiting = asyncio.create_task(service.wait_for_event(cursor, "agent_b", timeout_seconds=2))
    await asyncio.sleep(0.05)
    message = await service.send_message("agent_a", "agent_b", "wake hint")
    events = await waiting
    assert [e.type for e in events] == ["message.created"]
    assert events[0].message_id == message.id
    assert events[0].id > cursor
    assert await service.wait_for_event(events[-1].id, "agent_b", timeout_seconds=0.02) == []


async def test_long_wait_observes_separate_instance(service):
    cursor = (await service.get_events())[-1].id
    other = PhoneService(service.database, service.settings)
    waiting = asyncio.create_task(service.wait_for_event(cursor, "agent_b", timeout_seconds=2))
    await asyncio.sleep(0.05)
    await other.send_message("agent_a", "agent_b", "persistent event")
    assert (await waiting)[0].type == "message.created"


async def test_multiple_waiters_and_replay(service):
    cursor = (await service.get_events())[-1].id
    tasks = [asyncio.create_task(service.wait_for_event(cursor, "agent_b", 2)) for _ in range(4)]
    await service.send_message("agent_a", "agent_b", "all waiters")
    batches = await asyncio.gather(*tasks)
    assert all(batch == batches[0] for batch in batches)
    assert await service.get_events(cursor, "agent_b") == batches[0]


async def test_event_wait_cancellation(service):
    cursor = (await service.get_events())[-1].id
    waiting = asyncio.create_task(service.wait_for_event(cursor, "agent_b", 30))
    await asyncio.sleep(0.02)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await service.send_message("agent_a", "agent_b", "after cancellation")
    assert await service.wait_for_event(cursor, "agent_b", 0)


@pytest.mark.parametrize("timeout", [-1, 61, float("nan"), float("inf")])
async def test_invalid_timeout(service, timeout):
    with pytest.raises(PhoneError):
        await service.wait_for_event(timeout_seconds=timeout)
