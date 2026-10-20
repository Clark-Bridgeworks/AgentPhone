import json
import threading
from pathlib import Path
from uuid import uuid4


class DesktopStorage:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "desktop.json"
        self.lock = threading.RLock()
        self.data = {"paths": {}, "detected_paths": {}, "rooms": []}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value.get("rooms"), list) and isinstance(value.get("paths"), dict):
                    self.data = value
                    if not isinstance(self.data.get("detected_paths"), dict):
                        self.data["detected_paths"] = {}
            except (ValueError, OSError, AttributeError):
                # Preserve an unreadable file for recovery instead of silently overwriting it.
                self.path.replace(self.directory / f"desktop-recovery-{uuid4().hex}.json")

    def save(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.path)

    def remember_installations(self, installations):
        with self.lock:
            cached = self.data["detected_paths"]
            changed = False
            for agent in installations:
                if (
                    agent.command
                    and Path(agent.command).is_file()
                    and (cached.get(agent.id) != agent.command)
                ):
                    cached[agent.id] = agent.command
                    changed = True
            if changed:
                self.save()
