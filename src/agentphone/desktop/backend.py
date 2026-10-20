import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from agentphone.api import create_app
from agentphone.config import Settings


class LocalPhone:
    def __init__(
        self, directory: Path, preferred_port=8768, bridge_mode=True, adapter_factory=None
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind(("127.0.0.1", preferred_port))
        except OSError:
            self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.installations = {}
        self.bridge = None

        def detected():
            if not self.installations:
                from .discovery import discover_agents
                from .storage import DesktopStorage

                storage = DesktopStorage(self.directory)
                self.installations = {
                    a.id: a
                    for a in discover_agents(storage.data["paths"], storage.data["detected_paths"])
                }
            return dict(self.installations)

        def bridge_factory(service):
            from agentphone.bridge import CommunicationBridge

            self.bridge = CommunicationBridge(service, self.directory, detected, adapter_factory)
            self.bridge.url = self.url
            return self.bridge

        app = create_app(
            Settings(
                database_path=self.directory / "phone.db", max_messages_per_conversation=1000000
            ),
            enable_mcp=True,
            bridge_factory=bridge_factory if bridge_mode else None,
        )
        self.app = app
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, log_config=None, access_log=False, lifespan="on", log_level="warning"
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.sock]},
            daemon=True,
            name="AgentPhone-local-service",
        )
        self.thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if self.request("/health")["status"] == "ok":
                    break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            self.close()
            raise RuntimeError("本地消息服务启动失败，请查看使用说明中的故障处理。")
        if bridge_mode:
            from . import VERSION

            (self.directory / "bridge-endpoint.json").write_text(
                json.dumps({"url": self.url, "pid": os.getpid(), "version": VERSION}, indent=2),
                encoding="utf-8",
            )

    def request(self, path, body=None, timeout=5):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.url + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("error", {}).get("detail", str(error))
            except ValueError:
                detail = str(error)
            raise RuntimeError(detail) from error

    def send(self, sender, recipient, content, conversation_id=None, reply_to=None, metadata=None):
        body = {"sender_agent_id": sender, "recipient_agent_id": recipient, "content": content}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if reply_to:
            body["reply_to_message_id"] = reply_to
        if metadata:
            body["metadata"] = metadata
        return self.request("/messages", body)

    def history(self, conversation_id):
        if not conversation_id:
            return []
        return self.request(f"/conversations/{conversation_id}?limit=1000")["messages"]

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=6)
        try:
            self.sock.close()
        except OSError:
            pass
