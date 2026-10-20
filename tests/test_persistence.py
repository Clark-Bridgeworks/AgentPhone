import sqlite3

import pytest

from agentphone.repository import Repository


async def test_message_and_event_are_one_transaction(service, monkeypatch):
    original = Repository.insert_event
    before_agents = await service.get_agents()
    before_events = await service.get_events()

    def fail_creation(self, event):
        if event.type == "message.created":
            raise sqlite3.OperationalError("simulated event failure")
        return original(self, event)

    monkeypatch.setattr(Repository, "insert_event", fail_creation)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        await service.send_message("agent_a", "agent_b", "must roll back")
    assert await service.get_agents() == before_agents
    assert await service.get_events() == before_events
    with service.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


async def test_database_foreign_keys_and_rollback(service):
    with pytest.raises(sqlite3.IntegrityError):
        with service.database.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO conversations VALUES ('rollback', 'time', 'time', NULL)"
            )
            connection.execute(
                "INSERT INTO messages VALUES ('bad', 'rollback', 'missing', 'agent_b', "
                "'hello', 'time', 'stored', NULL, NULL)"
            )
    with service.database.transaction() as connection:
        assert (
            connection.execute("SELECT * FROM conversations WHERE id='rollback'").fetchone() is None
        )
