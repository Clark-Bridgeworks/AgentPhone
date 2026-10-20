from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RegisterAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    metadata: dict[str, Any] | None = None


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender_agent_id: str
    recipient_agent_id: str
    content: str
    conversation_id: str | None = None
    reply_to_message_id: str | None = None
    sender_session_id: str | None = None
    metadata: dict[str, Any] | None = None
