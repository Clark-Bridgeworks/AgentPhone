import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/agentphone.db")
    max_messages_per_conversation: int = 50
    max_message_length: int = 16000
    min_agent_poll_interval: float = 1.0

    def __post_init__(self):
        if self.max_messages_per_conversation < 1 or self.max_message_length < 1:
            raise ValueError("Message limits must be positive")
        if self.min_agent_poll_interval < 0.1:
            raise ValueError("Minimum poll interval must be at least 0.1 seconds")

    @classmethod
    def from_env(cls):
        return cls(
            database_path=Path(os.getenv("AGENTPHONE_DATABASE", "data/agentphone.db")),
            max_messages_per_conversation=int(os.getenv("AGENTPHONE_MAX_MESSAGES", "50")),
            max_message_length=int(os.getenv("AGENTPHONE_MAX_MESSAGE_LENGTH", "16000")),
            min_agent_poll_interval=float(os.getenv("AGENTPHONE_MIN_POLL_INTERVAL", "1")),
        )
