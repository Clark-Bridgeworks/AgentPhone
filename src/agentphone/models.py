from typing import Any

from pydantic import BaseModel


class Agent(BaseModel):
    id: str
    name: str
    created_at: str
    last_seen_at: str | None = None
    metadata: dict[str, Any] | None = None


class Conversation(BaseModel):
    id: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] | None = None


class Message(BaseModel):
    id: str
    conversation_id: str
    sender_agent_id: str
    recipient_agent_id: str
    content: str
    created_at: str
    status: str
    reply_to_message_id: str | None = None
    metadata: dict[str, Any] | None = None


class Event(BaseModel):
    id: int
    type: str
    created_at: str
    actor_agent_id: str | None = None
    target_agent_id: str | None = None
    message_id: str | None = None
    conversation_id: str | None = None
    payload: dict[str, Any] | None = None
