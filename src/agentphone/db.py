import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
    last_seen_at TEXT, metadata TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    sender_agent_id TEXT NOT NULL REFERENCES agents(id),
    recipient_agent_id TEXT NOT NULL REFERENCES agents(id),
    content TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('stored', 'read')),
    reply_to_message_id TEXT REFERENCES messages(id), metadata TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, created_at TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agents(id), target_agent_id TEXT REFERENCES agents(id),
    message_id TEXT REFERENCES messages(id),
    conversation_id TEXT REFERENCES conversations(id), payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_time
    ON messages(recipient_agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_target_id ON events(target_agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type_id ON events(type, id);
CREATE TABLE IF NOT EXISTS bridge_links (
    id TEXT PRIMARY KEY REFERENCES conversations(id),
    agent_a TEXT NOT NULL REFERENCES agents(id), session_a TEXT NOT NULL,
    agent_b TEXT NOT NULL REFERENCES agents(id), session_b TEXT, binding_b TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bridge_messages (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    sender_session_id TEXT NOT NULL, recipient_session_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('PENDING','DELIVERED','PROCESSED','FAILED')),
    receipt TEXT, error TEXT, updated_at TEXT NOT NULL
);
"""


class Database:
    """Short lived connections; mutations serialize through BEGIN IMMEDIATE."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            connection.executescript(SCHEMA)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        finally:
            connection.close()

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
