import asyncio
import logging
import math
from datetime import UTC, datetime
from uuid import uuid4

from .config import Settings
from .db import Database
from .errors import PhoneError
from .events import EventNotifier
from .models import Agent, Conversation, Event, Message
from .repository import Repository, encode

logger = logging.getLogger("agentphone.service")


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def identifier(value: str, field: str):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise PhoneError("invalid_input", f"{field} must contain 1–128 characters")


def page_limit(limit: int):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise PhoneError("invalid_input", "limit must be an integer between 1 and 1000")


class PhoneService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.notifier = EventNotifier()

    async def initialize(self):
        await asyncio.to_thread(self.database.initialize)

    async def _write(self, operation):
        def execute():
            emitted: list[Event] = []
            with self.database.transaction(write=True) as connection:
                result = operation(Repository(connection), emitted)
            return result, emitted

        result, emitted = await asyncio.to_thread(execute)
        if emitted:
            await self.notifier.notify()
            for event in emitted:
                logger.info(event.type, extra={"phone_event": event.model_dump()})
        return result

    async def _read(self, operation):
        def execute():
            with self.database.transaction() as connection:
                return operation(Repository(connection))

        return await asyncio.to_thread(execute)

    @staticmethod
    def _event(repo, emitted, kind, now, **fields):
        event = repo.insert_event(Event(id=0, type=kind, created_at=now, **fields))
        emitted.append(event)

    def _seen(self, repo, emitted, agent_id, now):
        repo.touch_agent(agent_id, now)
        self._event(
            repo, emitted, "agent.seen", now, actor_agent_id=agent_id, target_agent_id=agent_id
        )

    @staticmethod
    def _agent(repo, agent_id):
        agent = repo.get_agent(agent_id)
        if agent is None:
            raise PhoneError("agent_not_found", f"Agent '{agent_id}' is not registered", 404)
        return agent

    @staticmethod
    def _conversation(repo, conversation_id):
        conversation = repo.conversation(conversation_id)
        if conversation is None:
            raise PhoneError("conversation_not_found", "Conversation does not exist", 404)
        return conversation

    @staticmethod
    def _metadata(metadata):
        try:
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError()
            encode(metadata)
        except (TypeError, ValueError) as exc:
            raise PhoneError("invalid_input", "metadata must be a JSON object") from exc

    async def register_agent(self, agent_id: str, name: str, metadata=None) -> Agent:
        identifier(agent_id, "agent_id")
        identifier(name, "name")
        self._metadata(metadata)

        def operation(repo, emitted):
            now = timestamp()
            existing = repo.get_agent(agent_id)
            if existing is None:
                agent = Agent(
                    id=agent_id, name=name, created_at=now, last_seen_at=now, metadata=metadata
                )
                repo.insert_agent(agent)
                self._event(
                    repo,
                    emitted,
                    "agent.registered",
                    now,
                    actor_agent_id=agent_id,
                    target_agent_id=agent_id,
                )
            else:
                agent = existing.model_copy(
                    update={"name": name, "last_seen_at": now, "metadata": metadata}
                )
                repo.update_agent(agent)
                self._event(
                    repo,
                    emitted,
                    "agent.seen",
                    now,
                    actor_agent_id=agent_id,
                    target_agent_id=agent_id,
                )
            return agent

        return await self._write(operation)

    async def send_message(
        self,
        sender_agent_id: str,
        recipient_agent_id: str,
        content: str,
        conversation_id=None,
        reply_to_message_id=None,
        metadata=None,
        bridge_delivery=None,
    ) -> Message:
        identifier(sender_agent_id, "sender")
        identifier(recipient_agent_id, "recipient")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self.settings.max_message_length
        ):
            raise PhoneError(
                "invalid_input", "content is empty or exceeds the message length limit"
            )
        self._metadata(metadata)

        def operation(repo, emitted):
            self._agent(repo, sender_agent_id)
            self._agent(repo, recipient_agent_id)
            cid = conversation_id
            if reply_to_message_id is not None:
                parent = repo.message(reply_to_message_id)
                if parent is None:
                    raise PhoneError("message_not_found", "Reply message does not exist", 404)
                if cid is not None and cid != parent.conversation_id:
                    raise PhoneError(
                        "invalid_reply", "Reply must stay in the original conversation"
                    )
                if {sender_agent_id, recipient_agent_id} != {
                    parent.sender_agent_id,
                    parent.recipient_agent_id,
                }:
                    raise PhoneError("invalid_reply", "Reply must involve the original agents")
                cid = parent.conversation_id
            now = timestamp()
            if cid is None:
                cid = str(uuid4())
                repo.insert_conversation(Conversation(id=cid, created_at=now, updated_at=now))
            else:
                self._conversation(repo, cid)
            if repo.count_messages(cid) >= self.settings.max_messages_per_conversation:
                raise PhoneError("conversation_limit", "Conversation message limit reached", 409)
            message = Message(
                id=str(uuid4()),
                conversation_id=cid,
                sender_agent_id=sender_agent_id,
                recipient_agent_id=recipient_agent_id,
                content=content,
                created_at=now,
                status="stored",
                reply_to_message_id=reply_to_message_id,
                metadata=metadata,
            )
            repo.insert_message(message)
            if bridge_delivery is not None:
                repo.connection.execute(
                    "INSERT INTO bridge_messages(message_id, sender_session_id, "
                    "recipient_session_id, status, updated_at) VALUES (?, ?, ?, 'PENDING', ?)",
                    (
                        message.id,
                        bridge_delivery["sender_session_id"],
                        bridge_delivery["recipient_session_id"],
                        now,
                    ),
                )
            self._seen(repo, emitted, sender_agent_id, now)
            self._event(
                repo,
                emitted,
                "message.created",
                now,
                actor_agent_id=sender_agent_id,
                target_agent_id=recipient_agent_id,
                message_id=message.id,
                conversation_id=cid,
                payload={"sender": sender_agent_id, "recipient": recipient_agent_id},
            )
            return message

        return await self._write(operation)

    async def read_messages(
        self, agent_id: str, after_message_id=None, conversation_id=None, limit=50, mark_read=True
    ) -> list[Message]:
        page_limit(limit)

        def operation(repo, emitted):
            self._agent(repo, agent_id)
            if conversation_id is not None:
                self._conversation(repo, conversation_id)
            after = 0
            if after_message_id is not None:
                after, cursor = repo.message_cursor(after_message_id)
                if cursor.recipient_agent_id != agent_id or (
                    conversation_id is not None and cursor.conversation_id != conversation_id
                ):
                    raise PhoneError(
                        "invalid_cursor", "Cursor does not belong to this inbox/filter"
                    )
            messages = repo.inbox(agent_id, after, conversation_id, limit)
            now = timestamp()
            self._seen(repo, emitted, agent_id, now)
            for message in messages:
                if mark_read and message.status == "stored":
                    repo.mark_read(message.id)
                    message.status = "read"
                    self._event(
                        repo,
                        emitted,
                        "message.read",
                        now,
                        actor_agent_id=agent_id,
                        target_agent_id=message.sender_agent_id,
                        message_id=message.id,
                        conversation_id=message.conversation_id,
                    )
            return messages

        return await self._write(operation)

    async def get_conversation(self, conversation_id: str, limit=100, before_message_id=None):
        page_limit(limit)

        def operation(repo):
            conversation = self._conversation(repo, conversation_id)
            before = None
            if before_message_id is not None:
                before, cursor = repo.message_cursor(before_message_id)
                if cursor.conversation_id != conversation_id:
                    raise PhoneError("invalid_cursor", "Cursor belongs to a different conversation")
            return {
                "conversation": conversation,
                "messages": repo.history(conversation_id, before, limit),
            }

        return await self._read(operation)

    async def list_conversations(self, agent_a: str, agent_b: str, limit=20):
        page_limit(limit)

        def operation(repo):
            self._agent(repo, agent_a)
            self._agent(repo, agent_b)
            return repo.conversations_between(agent_a, agent_b, limit)

        return await self._read(operation)

    async def get_agents(self) -> list[Agent]:
        return await self._read(lambda repo: repo.agents())

    async def get_events(self, after_event_id=0, target_agent_id=None, limit=100) -> list[Event]:
        page_limit(limit)
        if (
            isinstance(after_event_id, bool)
            or not isinstance(after_event_id, int)
            or after_event_id < 0
        ):
            raise PhoneError("invalid_input", "after_event_id must be a nonnegative integer")

        def operation(repo):
            if target_agent_id is not None:
                self._agent(repo, target_agent_id)
            return repo.events(after_event_id, target_agent_id, limit)

        return await self._read(operation)

    async def wait_for_event(
        self, after_event_id=0, target_agent_id=None, timeout_seconds=30, limit=100
    ) -> list[Event]:
        if (
            not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 <= timeout_seconds <= 60
        ):
            raise PhoneError("invalid_input", "timeout_seconds must be between 0 and 60")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            version = self.notifier.version
            events = await self.get_events(after_event_id, target_agent_id, limit)
            if events:
                return events
            remaining = deadline - loop.time()
            if remaining <= 0:
                return []
            # A bounded fallback also observes commits made by another service instance.
            await self.notifier.wait(version, min(remaining, 1.0))
