import pytest

from agentphone.config import Settings
from agentphone.db import Database
from agentphone.service import PhoneService


@pytest.fixture
async def service(tmp_path):
    settings = Settings(database_path=tmp_path / "phone.db")
    service = PhoneService(Database(settings.database_path), settings)
    await service.initialize()
    await service.register_agent("agent_a", "Agent A")
    await service.register_agent("agent_b", "Agent B")
    return service
