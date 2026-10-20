import asyncio
import json
import logging


class EventNotifier:
    """Notification is only a hint. SQLite remains the source of truth."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.version = 0

    async def notify(self):
        async with self.condition:
            self.version += 1
            self.condition.notify_all()

    async def wait(self, version: int, timeout: float):
        async with self.condition:
            if self.version != version:
                return
            try:
                await asyncio.wait_for(self.condition.wait(), timeout)
            except TimeoutError:
                pass


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "phone_event"):
            data.update(record.phone_event)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("agentphone")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
