"""Original conversation metadata/setup only; never starts an inference turn."""

from pathlib import Path
from uuid import uuid4

from .adapters import CodexAdapter, session_adapter_available
from .dsh_desktop import DshDesktopClient
from .presentation import open_codex_conversation


class NativeSessions:
    def __init__(self, phone, directory, installations):
        self.phone, self.directory, self.installations = phone, Path(directory), installations

    def target(self, agent_id):
        target = self.installations().get(agent_id)
        if not target or not session_adapter_available(target):
            raise RuntimeError("该 Agent 缺少已验证的原桌面对话接口，暂不能选择。")
        return target

    def codex(self, target):
        client = CodexAdapter(self.phone, self.directory / "native-metadata", target)
        try:
            client.start_transport(enable_mcp=False, experimental=True)
        except Exception:
            client.stop_session()
            raise
        return client

    def dsh(self, target):
        directory = self.directory / "native-metadata"
        directory.mkdir(parents=True, exist_ok=True)
        client = DshDesktopClient(target.application, directory)
        client.connect()
        return client

    def workbuddy(self, target):
        from .workbuddy_desktop import WorkBuddyDesktopClient

        client = WorkBuddyDesktopClient(target.application)
        client.connect()
        return client

    @staticmethod
    def codex_info(thread):
        return {
            "native_session_id": thread["id"],
            "title": thread.get("name") or "未命名对话",
            "cwd": thread.get("cwd", ""),
            "updated_at": thread.get("updatedAt"),
            "running": thread.get("status", {}).get("type") == "active",
        }

    def list(self, agent_id, cursor=None):
        target = self.target(agent_id)
        if agent_id == "deepseek_harness":
            return {"sessions": self.dsh(target).sessions(), "next_cursor": None}
        if agent_id == "workbuddy":
            return self.workbuddy(target).sessions(cursor)
        if agent_id != "codex":
            raise RuntimeError("该 Agent 没有原生对话列表接口。")
        client = self.codex(target)
        try:
            result = client.rpc(
                "thread/list",
                {
                    "limit": 50,
                    "cursor": cursor,
                    "sortKey": "updated_at",
                    "sourceKinds": ["cli", "vscode", "appServer"],
                },
            )
            return {
                "sessions": [self.codex_info(t) for t in result["data"]],
                "next_cursor": result.get("nextCursor"),
            }
        finally:
            client.stop_session()

    def inspect(self, agent_id, session_id):
        target = self.target(agent_id)
        if agent_id == "deepseek_harness":
            return self.dsh(target).session_info(session_id)
        if agent_id == "workbuddy":
            return self.workbuddy(target).session_info(session_id)
        client = self.codex(target)
        try:
            result = client.rpc("thread/read", {"threadId": session_id, "includeTurns": False})
            if result["thread"]["id"] != session_id:
                raise RuntimeError("原生会话 ID 不匹配。")
            return self.codex_info(result["thread"])
        finally:
            client.stop_session()

    def create_a(self, agent_id, cwd):
        """Human-selected new original A window, without prompt/work/substitute initiator."""
        target = self.target(agent_id)
        path = Path(cwd).resolve()
        if not path.is_dir():
            raise RuntimeError("请为新对话选择存在的项目文件夹。")
        title = "AgentPhone 新对话 " + uuid4().hex[:8]
        if agent_id == "workbuddy":
            client = self.workbuddy(target)
            result = client.create(path, title)
            client.open_conversation(result["native_session_id"])
            return result
        if agent_id == "deepseek_harness":
            client = self.dsh(target)
            native = client.create(path)
            client.rename(native, title)
            client.select_window(title, session_id=native)
            return dict(native_session_id=native, cwd=str(path), title=title)
        client = self.codex(target)
        try:
            thread = client.rpc("thread/start", {"cwd": str(path), "ephemeral": False})["thread"]
            native = thread["id"]
            # A start-only thread has no rollout and cannot be reopened in the original app.
            # Persist a transport identity note via the native API, without starting a turn.
            client.rpc(
                "thread/inject_items",
                {
                    "threadId": native,
                    "items": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "这是用户在 AgentPhone 中选择的新原生对话。"
                                    + f"本对话真实 sender_session_id={native}，sender=codex。"
                                    + "用户在原软件提出需求；通信桥只传递双方显式消息。",
                                }
                            ],
                        }
                    ],
                },
            )
            client.rpc("thread/name/set", {"threadId": native, "name": title})
            open_codex_conversation(native)
            return dict(native_session_id=native, cwd=str(path), title=title)
        finally:
            client.stop_session()
