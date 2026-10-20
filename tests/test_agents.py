async def test_registration_idempotent_and_presence(service):
    original = (await service.get_agents())[0]
    updated = await service.register_agent("agent_a", "New name", {"runtime": "independent"})
    assert updated.created_at == original.created_at
    assert updated.last_seen_at >= original.last_seen_at
    assert updated.metadata == {"runtime": "independent"}
    assert len(await service.get_agents()) == 2
    events = await service.get_events()
    assert len([e for e in events if e.type == "agent.registered"]) == 2
    assert events[-1].type == "agent.seen"
