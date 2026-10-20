import json
import sqlite3
from typing import Any

from .errors import PhoneError
from .models import Agent, Conversation, Event, Message


def encode(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False)


def decode(row: sqlite3.Row, model):
    values = dict(row)
    for field in ("metadata", "payload"):
        if field in values and values[field] is not None:
            values[field] = json.loads(values[field])
    return model(**values)


class Repository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self.connection.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return decode(row, Agent) if row else None

    def agents(self) -> list[Agent]:
        return [
            decode(r, Agent) for r in self.connection.execute("SELECT * FROM agents ORDER BY id")
        ]

    def insert_agent(self, agent: Agent):
        self.connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?, ?, ?)",
            (agent.id, agent.name, agent.created_at, agent.last_seen_at, encode(agent.metadata)),
        )

    def update_agent(self, agent: Agent):
        self.connection.execute(
            "UPDATE agents SET name=?, last_seen_at=?, metadata=? WHERE id=?",
            (agent.name, agent.last_seen_at, encode(agent.metadata), agent.id),
        )

    def touch_agent(self, agent_id: str, now: str):
        self.connection.execute("UPDATE agents SET last_seen_at=? WHERE id=?", (now, agent_id))

    def conversation(self, conversation_id: str) -> Conversation | None:
        row = self.connection.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        return decode(row, Conversation) if row else None

    def conversations_between(self, agent_a: str, agent_b: str, limit: int):
        rows = self.connection.execute(
            "SELECT c.* FROM conversations c JOIN messages m ON m.conversation_id=c.id "
            "WHERE ((m.sender_agent_id=? AND m.recipient_agent_id=?) "
            "OR (m.sender_agent_id=? AND m.recipient_agent_id=?)) "
            "AND NOT EXISTS (SELECT 1 FROM messages other WHERE other.conversation_id=c.id "
            "AND NOT ((other.sender_agent_id=? AND other.recipient_agent_id=?) "
            "OR (other.sender_agent_id=? AND other.recipient_agent_id=?))) "
            "GROUP BY c.id ORDER BY MAX(m.rowid) DESC LIMIT ?",
            (agent_a, agent_b, agent_b, agent_a, agent_a, agent_b, agent_b, agent_a, limit),
        )
        return [decode(row, Conversation) for row in rows]

    def insert_conversation(self, conversation: Conversation):
        self.connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                conversation.id,
                conversation.created_at,
                conversation.updated_at,
                encode(conversation.metadata),
            ),
        )

    def message(self, message_id: str) -> Message | None:
        row = self.connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return decode(row, Message) if row else None

    def message_cursor(self, message_id: str) -> tuple[int, Message]:
        row = self.connection.execute(
            "SELECT rowid AS seq, * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise PhoneError("invalid_cursor", "Message cursor does not exist")
        values = dict(row)
        seq = values.pop("seq")
        if values["metadata"] is not None:
            values["metadata"] = json.loads(values["metadata"])
        return seq, Message(**values)

    def count_messages(self, conversation_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0]

    def insert_message(self, message: Message):
        self.connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.conversation_id,
                message.sender_agent_id,
                message.recipient_agent_id,
                message.content,
                message.created_at,
                message.status,
                message.reply_to_message_id,
                encode(message.metadata),
            ),
        )
        self.connection.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (message.created_at, message.conversation_id),
        )

    def inbox(self, agent_id: str, after: int, conversation_id: str | None, limit: int):
        sql = "SELECT * FROM messages WHERE recipient_agent_id=? AND rowid>?"
        parameters: list[Any] = [agent_id, after]
        if conversation_id is not None:
            sql += " AND conversation_id=?"
            parameters.append(conversation_id)
        sql += " ORDER BY rowid ASC LIMIT ?"
        parameters.append(limit)
        return [decode(r, Message) for r in self.connection.execute(sql, parameters)]

    def history(self, conversation_id: str, before: int | None, limit: int):
        sql = "SELECT * FROM messages WHERE conversation_id=?"
        parameters: list[Any] = [conversation_id]
        if before is not None:
            sql += " AND rowid<?"
            parameters.append(before)
        sql += " ORDER BY rowid DESC LIMIT ?"
        parameters.append(limit)
        return list(
            reversed([decode(r, Message) for r in self.connection.execute(sql, parameters)])
        )

    def mark_read(self, message_id: str):
        self.connection.execute("UPDATE messages SET status='read' WHERE id=?", (message_id,))

    def insert_event(self, event: Event) -> Event:
        cursor = self.connection.execute(
            "INSERT INTO events (type, created_at, actor_agent_id, target_agent_id, "
            "message_id, conversation_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.type,
                event.created_at,
                event.actor_agent_id,
                event.target_agent_id,
                event.message_id,
                event.conversation_id,
                encode(event.payload),
            ),
        )
        return event.model_copy(update={"id": cursor.lastrowid})

    def events(self, after: int, target_agent_id: str | None, limit: int):
        sql = "SELECT * FROM events WHERE id>?"
        parameters: list[Any] = [after]
        if target_agent_id is not None:
            sql += " AND target_agent_id=?"
            parameters.append(target_agent_id)
        sql += " ORDER BY id ASC LIMIT ?"
        parameters.append(limit)
        return [decode(r, Event) for r in self.connection.execute(sql, parameters)]
