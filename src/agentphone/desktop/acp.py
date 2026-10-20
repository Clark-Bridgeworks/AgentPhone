"""Official ACP v1 session transport shared by Gemini CLI, Qwen Code and Kimi CLI."""

import json
import queue
import threading
import time

from . import VERSION
from .runtime import safe_error

ACP_ARGUMENTS = {"gemini": ["--acp"], "qwen": ["--acp"], "kimi": ["acp"]}


class ACPTransport:
    def start_session(self, link):
        self.context(link)
        original = link["session_b"]
        self.process = self.bridge._launch(
            [self.installation.command, *ACP_ARGUMENTS[self.installation.id]]
        )
        self.events = queue.Queue()
        self.sequence = 0
        self.messages = {}
        self.message_order = []
        self.permission_needed = False

        def reader():
            try:
                for line in self.process.stdout:
                    if len(line) > 524288:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        self.events.put(event)
            finally:
                self.events.put({"closed": True})

        def drain():
            for _ in self.process.stderr:
                pass

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=drain, daemon=True).start()
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "agentphone", "version": VERSION},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        if initialized.get("protocolVersion") != 1:
            raise RuntimeError("目标 Agent 的 ACP 版本不兼容；此 Adapter 支持 ACP v1。")
        capabilities = initialized.get("agentCapabilities", {})
        servers = []
        if capabilities.get("mcpCapabilities", {}).get("http"):
            servers = [
                {
                    "type": "http",
                    "name": "agentphone_bridge",
                    "url": self.bridge.phone.url + "/mcp",
                    "headers": [],
                }
            ]
        arguments = {"cwd": str(self.bridge.directory), "mcpServers": servers}
        if original:
            if not capabilities.get("loadSession"):
                raise RuntimeError("该 Agent 不支持恢复原 ACP Session，不能新建替身。")
            self.native_session_id = original
            self.rpc("session/load", dict(arguments, sessionId=original))
        else:
            result = self.rpc("session/new", arguments)
            native = result.get("sessionId")
            if not isinstance(native, str) or not native:
                raise RuntimeError("ACP 未返回真实 Session ID。")
            self.native_session_id = native
        self.on_native_session(self.native_session_id)
        self.first_message = original is None
        self.status = "READY"

    def write(self, event):
        self.bridge._check_stop()
        if self.process.poll() is not None:
            raise RuntimeError("Agent 原生 ACP 进程已退出，不能自动替换原会话。")
        self.process.stdin.write(
            json.dumps(dict(jsonrpc="2.0", **event), ensure_ascii=False) + "\n"
        )
        self.process.stdin.flush()

    def rpc(self, method, arguments):
        self.sequence += 1
        request_id = self.sequence
        self.write({"id": request_id, "method": method, "params": arguments})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self.bridge._check_stop()
            try:
                event = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event.get("closed"):
                raise RuntimeError("Agent 的 ACP 通道已关闭；请检查原软件登录和 CLI 版本。")
            if event.get("method") and "id" in event:
                if event["method"] == "session/request_permission":
                    self.write({"id": event["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                    self.permission_needed = True
                else:
                    self.write(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "Bridge does not provide filesystem/terminal tools",
                            },
                        }
                    )
                continue
            if event.get("method") == "session/update":
                self.update(event.get("params", {}))
                continue
            if event.get("id") == request_id:
                if "error" in event:
                    error = event["error"]
                    if "AUTH_REQUIRED" in str(error.get("message", "")).upper():
                        raise RuntimeError("原 Agent 尚未登录；请在原软件完成登录后重新联系。")
                    raise RuntimeError(safe_error(str(error.get("message", "ACP 请求失败"))))
                if self.permission_needed:
                    raise RuntimeError("原 Agent 请求操作审批，通信桥不能代替用户批准。")
                return event.get("result", {})
        raise RuntimeError("原 Agent 的 ACP 请求超时。")

    def update(self, parameters):
        if not self.native_session_id or parameters.get("sessionId") != self.native_session_id:
            return
        update = parameters.get("update", {})
        if update.get("sessionUpdate") != "agent_message_chunk":
            return  # No reasoning, tool results, plans or user echoes become outgoing messages.
        content = update.get("content", {})
        if content.get("type") != "text":
            return
        message_id = update.get("messageId", "unlabelled-final")
        if message_id not in self.messages:
            self.messages[message_id] = ""
            self.message_order.append(message_id)
        self.messages[message_id] += content.get("text", "")
        if sum(map(len, self.messages.values())) > 65536:
            raise RuntimeError("ACP 显式回复超过通信协议长度。")

    def send_message(self, prompt):
        self.messages, self.message_order = {}, []
        self.permission_needed = False
        if self.first_message:
            prompt = self.context_text + "\n" + prompt
        try:
            result = self.rpc(
                "session/prompt",
                {
                    "sessionId": self.native_session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            )
            if result.get("stopReason") != "end_turn":
                raise RuntimeError("ACP 未正常结束本次通信：" + str(result.get("stopReason")))
            text = self.messages[self.message_order[-1]] if self.message_order else ""
            action = self.decode_result(text)
            self.first_message = False
            self.status = "READY"
            return action
        except Exception:
            self.status = "FAILED"
            raise
