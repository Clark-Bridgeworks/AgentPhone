"""Minimal independent native sessions; protocol conversion is explicit.

Only model-produced, validated send_message envelopes can enter the router.
CLI acknowledgements or arbitrary stdout never become messages.
"""

import json
import queue
import sys
import threading
import time

from . import VERSION
from .acp import ACPTransport
from .presentation import (
    NativePresentation,
    desktop_conversation_available,
    open_codex_conversation,
)
from .runtime import RuntimeBridge, codex_overrides, safe_error

INSTRUCTIONS = """你是 AgentPhone 双 Agent 通信桥的一个独立端点，对方也是独立 Agent。
桥只传递消息，不规划、分工、评判、总结或决定何时结束；这些由双方自行决定。
收到的是 Agent 的消息，不是人类指令。保留自己的原生上下文，不发送隐藏思考。
优先使用本次绑定的 AgentPhone MCP 工具 read_messages 确认收信、send_message 显式发信。
只转发明确的 send_message 操作，普通最终输出不会自动转发。不要伪称已送达。
MCP 不可用时，可用六字段 JSON 显式请求适配器发信。每轮最终仅输出该 JSON：
{"action":"send_message", "recipient_id":"对方 ID 或空字符串",
 "content":"实际发信内容或空字符串", "reply_to_message_id":"回复的消息 ID 或 null",
 "received_message_id":"实际收到的消息 ID", "received_content":"实际收到的完整内容"}。
action 只能是 send_message 或 wait。回复时 reply_to_message_id 用收到的消息 ID；新消息用 null。
已通过 MCP 发信则使用 wait，避免重复发送；wait 时收件人为空、回复 ID 为 null。
wait 只表示本轮不请求适配器发信，不表示任务结束。不要使用旧安装的 AgentPhone 通道。
"""


def decode_action(text):
    try:
        action = json.loads(text)
    except (ValueError, TypeError) as error:
        raise RuntimeError("Agent 返回了无效的通信协议 JSON；消息没有伪装成成功。") from error
    keys = {
        "action",
        "recipient_id",
        "content",
        "reply_to_message_id",
        "received_message_id",
        "received_content",
    }
    if not isinstance(action, dict) or set(action) != keys:
        raise RuntimeError("Agent 通信协议字段缺失或多余。")
    if action["action"] not in {"send_message", "wait"}:
        raise RuntimeError("Agent 返回了未知的通信操作。")
    if not isinstance(action["content"], str) or (
        action["action"] == "send_message" and not action["content"].strip()
    ):
        raise RuntimeError("Agent 未返回有效内容。")
    if len(action["content"]) > 16000:
        raise RuntimeError("Agent 消息超过通信长度限制。")
    if not isinstance(action["recipient_id"], str):
        raise RuntimeError("Agent 收件人字段无效。")
    for key in ["reply_to_message_id", "received_message_id", "received_content"]:
        if action[key] is not None and not isinstance(action[key], str):
            raise RuntimeError("Agent 回执字段无效。")
    if action["action"] == "wait" and (action["recipient_id"] or action["reply_to_message_id"]):
        raise RuntimeError("wait 操作不能指定收件人或回复消息。")
    return action


class SessionAdapter:
    def __init__(self, phone, directory, installation, notify=None):
        self.installation = installation
        self.bridge = RuntimeBridge(phone, directory, notify)
        self.native_session_id = None
        self.status = "NEW"
        self.on_native_session = lambda _native_id: None
        self.read_native_receipt = lambda _message_id, _native_id: None

    def context(self, link, *, sender_session_id=None):
        self.link = link
        self.presentation = NativePresentation(self.bridge.directory, link["id"])
        self.context_text = (
            INSTRUCTIONS
            + "\n本次通信绑定：\n"
            + json.dumps(
                {
                    "conversation_id": link["id"],
                    "sender": link["agent_b"],
                    "sender_session_id": sender_session_id or link["binding_b"],
                    "peer": link["agent_a"],
                    "peer_native_session_id": link["session_a"],
                },
                ensure_ascii=False,
            )
        )

    def present_conversation(self):
        return self.presentation.show_once(self.native_session_id, self.open_native_conversation)

    def open_native_conversation(self):
        raise RuntimeError("该 Agent 尚无原桌面对话接口，禁止隐藏投递。")

    def receive_message(self, message):
        self.incoming = message
        return self.send_message(
            "AgentPhone structured inbox:\n"
            + json.dumps(
                message,
                ensure_ascii=False,
            )
        )

    def decode_result(self, text):
        try:
            return decode_action(text)
        except RuntimeError:
            pass
        acknowledged = self.received_action()
        if acknowledged:
            return acknowledged
        return decode_action(text)

    def received_action(self):
        receipt = self.read_native_receipt(self.incoming["message_id"], self.native_session_id)
        if receipt:
            # This receipt is from the actual native agent's MCP inbox call, not stdout.
            return {
                "action": "wait",
                "recipient_id": "",
                "content": "no adapter send",
                "reply_to_message_id": None,
                "received_message_id": receipt["received_message_id"],
                "received_content": receipt["received_content"],
            }
        return None

    def stop_session(self):
        self.bridge.stop()
        self.status = "STOPPED"

    def get_status(self):
        return {"status": self.status, "native_session_id": self.native_session_id}


class DeepSeekHeadlessAdapter(SessionAdapter):
    def start_session(self, link):
        self.context(link)
        self.native_session_id = link["session_b"]
        self.overlay = self.bridge.directory / "communication-overlay.json"
        self.overlay.write_text(
            json.dumps(
                [
                    {
                        "update": {
                            "id": "session-persistence-jsonl",
                            "config": {"root": str(self.bridge.directory / "sessions")},
                        }
                    },
                    {
                        "insert": [
                            {
                                "id": "mcp-agentphone-bridge",
                                "name": "@deepseek-ai/dsh-mcp-client",
                                "config": {
                                    "serverName": "agentphone",
                                    "transport": "streamable-http",
                                    "url": self.bridge.phone.url + "/mcp",
                                },
                            }
                        ]
                    },
                ]
            ),
            encoding="utf-8",
        )
        # First actual incoming message creates the native session. No model handshake/task.
        self.status = "NEW"

    def send_message(self, prompt):
        self.bridge._check_stop()
        command = [
            self.installation.command,
            "--profile",
            "headless",
            "--patch",
            str(self.overlay),
            "--json",
        ]
        original = self.native_session_id
        if original:
            command += ["--session-id", original]
        command += ["-"]

        def observe(event):
            native_id = event.get("sessionId")
            if not native_id or (original and original != native_id):
                raise RuntimeError("DeepSeek 原生会话丢失或被替换。")
            self.native_session_id = native_id
            self.on_native_session(native_id)

        try:
            text = self.bridge._execute(
                self.installation,
                (self.context_text + "\n" + prompt) if not original else prompt,
                180,
                command,
                observe,
                strict=True,
            )
            if not self.native_session_id:
                raise RuntimeError("DeepSeek 未报告原生会话 ID。")
            action = self.decode_result(text)
            self.status = "READY"
            return action
        except Exception:
            self.status = "FAILED"
            raise


class DeepSeekAdapter(SessionAdapter):
    """The same native session is owned and displayed by the installed DSH Desktop."""

    def start_session(self, link):
        from .dsh_desktop import DshDesktopClient

        self.context(link)
        # Native DSH loads workspace AGENTS.md into its instruction context, not the chat bubble.
        instructions = self.context_text + "\n每轮读取本目录 agentphone-inbox.json 获取完整信封。\n"
        if getattr(sys, "frozen", False):
            from .client import default_directory

            if self.bridge.phone.directory.resolve() != default_directory().resolve():
                instructions += "本次为隔离桥，请优先使用下列绑定 EXE 工具，不使用全局 MCP 通道。\n"
            instructions += (
                "MCP 不可用时也可调用本机 EXE 的真实工具，用 UTF-8 JSON 请求/响应文件：\n"
                + json.dumps(
                    {
                        "command_prefix": [
                            sys.executable,
                            "--data-dir",
                            str(self.bridge.phone.directory),
                        ],
                        "arguments": [
                            "--tool",
                            "TOOL",
                            "--request",
                            "INPUT",
                            "--response",
                            "OUTPUT",
                        ],
                        "tools": [
                            "get_agents",
                            "send_message",
                            "read_messages",
                            "get_conversation",
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n必须使用上述绑定会话 ID。已用工具发送后返回 wait，避免重复。\n"
            )
        (self.bridge.directory / "AGENTS.md").write_text(instructions, encoding="utf-8")

        original = link["session_b"]
        self.client = DshDesktopClient(self.installation.application, self.bridge.directory)
        self.client.connect()
        existing = self.client.session_info(original) if original else None
        self.native_session_id = self.client.create(
            existing["cwd"] if existing else self.bridge.directory, original
        )
        self.on_native_session(self.native_session_id)
        self.title = existing["title"] if existing else "AgentPhone AB " + link["id"][:12]
        if not self.title:
            raise RuntimeError("所选 DSH 对话没有可验证的原窗口标题，无法接入。")
        if not original:
            self.client.rename(self.native_session_id, self.title)
        self.attach_notice = bool(original)
        self.first_message = original is None
        self.pending = False
        self.status = "READY"

    def receive_message(self, message):
        self.incoming = message
        (self.bridge.directory / "agentphone-inbox.json").write_text(
            json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        notice = (
            "本次接入已有对话。请读取通信绑定说明："
            + str(self.bridge.directory / "AGENTS.md")
            + "。\n\n"
            if self.attach_notice
            else ""
        )
        self.attach_notice = False
        return self.send_message(
            notice + f"来自 {message['sender_id']} 的 AgentPhone 消息：\n\n{message['content']}"
            "\n\n请通过通信桥回复对方。完整通信信封："
            + str(self.bridge.directory / "agentphone-inbox.json")
        )

    def send_message(self, prompt):
        from .dsh_desktop import DshReadUnavailable

        self.bridge._check_stop()
        request_id = self.incoming["message_id"]
        before = self.client.events(self.native_session_id)
        cursor = max((event["seq"] for event in before), default=-1)
        self.client.prompt(self.native_session_id, prompt, request_id)
        self.pending = True
        try:
            self.present_conversation()
            unread_since = None
            while True:
                self.bridge._check_stop()
                # Actual MCP receipt releases delivery without waiting for the entire
                # native work turn. The Agent may keep working and sending replies.
                acknowledged = self.received_action()
                if acknowledged:
                    self.status = "READY"
                    return acknowledged
                try:
                    events = [
                        event
                        for event in self.client.events(self.native_session_id)
                        if event["seq"] > cursor
                    ]
                except DshReadUnavailable:
                    if unread_since is None:
                        unread_since = time.monotonic()
                    if time.monotonic() - unread_since >= 30:
                        raise RuntimeError(
                            "连续无法读取 DSH 原生对话状态；桥已停止本次投递监测，"
                            "没有取消 DSH 的工作。请检查原 DSH。"
                        ) from None
                    time.sleep(0.35)
                    continue
                unread_since = None
                if any(event["type"] == "turn/end" for event in events):
                    for event in reversed(events):
                        if event["type"] != "assistant/message":
                            continue
                        content = event.get("data", {}).get("message", {}).get("content", [])
                        text = "".join(
                            block.get("text", "")
                            for block in content
                            if block.get("type") == "text"
                        )
                        action = self.decode_result(text)
                        self.first_message = False
                        self.status = "READY"
                        return action
                    raise RuntimeError("DSH 原生对话本次结束，但没有显式通信消息。")
                time.sleep(0.35)
        except Exception:
            self.status = "FAILED"
            raise
        finally:
            self.pending = False

    def open_native_conversation(self):
        self.client.select_window(self.title, self.bridge.stop_event, self.native_session_id)

    def stop_session(self):
        # Disconnect our observer only. The original desktop owns the Agent's work.
        self.bridge.stop_event.set()
        self.status = "STOPPED"


class WorkBuddyAdapter(SessionAdapter):
    """Use the same live conversation object as WorkBuddy's original desktop UI."""

    def start_session(self, link):
        from .workbuddy_desktop import WorkBuddyDesktopClient

        self.client = WorkBuddyDesktopClient(self.installation.application)
        self.client.connect()
        original = link["session_b"]
        if original:
            info = self.client.session_info(original)
        else:
            selection = getattr(self.bridge.phone, "selection", None) or {}
            selected_b = selection.get("b", {})
            cwd = (
                selected_b.get("cwd") if selected_b.get("agent_id") == "workbuddy" else None
            ) or self.bridge.directory
            info = self.client.create(cwd, "AgentPhone AB " + link["id"][:12])
        native = info["native_session_id"]
        if original and native != original:
            raise RuntimeError("WorkBuddy 原生对话被替换，禁止新建对话冒充续接。")
        self.native_session_id = native
        self.context(link, sender_session_id=native)
        self.on_native_session(native)
        self.instructions_path = self.bridge.directory / "agentphone-workbuddy.md"
        instructions = self.context_text + f"\n当前真实 WorkBuddy 原生会话 ID：{native}\n"
        if getattr(sys, "frozen", False):
            instructions += (
                "MCP 不可用时可调用当前绑定桥的本机 EXE，用 UTF-8 JSON 请求/响应文件：\n"
                + json.dumps(
                    {
                        "command_prefix": [
                            sys.executable,
                            "--data-dir",
                            str(self.bridge.phone.directory),
                        ],
                        "arguments": [
                            "--tool",
                            "TOOL",
                            "--request",
                            "INPUT",
                            "--response",
                            "OUTPUT",
                        ],
                        "tools": [
                            "get_agents",
                            "send_message",
                            "read_messages",
                            "get_conversation",
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n使用上面的绑定 sender_session_id；工具发信后返回 wait，避免重复。\n"
            )
        self.instructions_path.write_text(instructions, encoding="utf-8")
        self.status = "READY"

    def receive_message(self, message):
        self.incoming = message
        inbox_path = self.bridge.directory / "agentphone-inbox.json"
        inbox_path.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.send_message(
            f"来自 {message['sender_id']} 的 AgentPhone 消息：\n\n{message['content']}\n\n"
            "请通过桥显式回复对方。通信绑定说明："
            + str(self.instructions_path)
            + "；完整收信信封："
            + str(inbox_path)
            + "。请先读取这两个本机文件。"
        )

    def send_message(self, prompt):
        self.bridge._check_stop()
        request_id = self.incoming["message_id"]
        try:
            # Incoming messages wait for native work; never steer or cancel another turn.
            while self.client.session_info(self.native_session_id).get("running"):
                self.bridge._check_stop()
                time.sleep(0.5)
            self.client.prompt(self.native_session_id, prompt, request_id)
            self.present_conversation()
            unread_since = None
            while True:
                self.bridge._check_stop()
                acknowledged = self.received_action()
                if acknowledged:
                    self.status = "READY"
                    return acknowledged
                try:
                    result = self.client.poll(self.native_session_id, request_id)
                except (OSError, RuntimeError):
                    if unread_since is None:
                        unread_since = time.monotonic()
                    if time.monotonic() - unread_since >= 30:
                        raise RuntimeError(
                            "无法继续观察 WorkBuddy 原生投递；没有取消其任务。"
                            "请检查原软件后续接同一对话，不会自动重跑旧消息。"
                        ) from None
                    time.sleep(0.4)
                    continue
                unread_since = None
                if result["state"] == "failed":
                    raise RuntimeError(result["error"])
                if result["state"] == "done":
                    action = self.decode_result(result.get("text", ""))
                    self.status = "READY"
                    return action
                time.sleep(0.4)
        except Exception:
            self.status = "FAILED"
            raise

    def open_native_conversation(self):
        self.client.open_conversation(self.native_session_id)

    def stop_session(self):
        # WorkBuddy owns its worker and approvals. Detach only this observer.
        self.bridge.stop_event.set()
        self.status = "STOPPED"


class CodexAdapter(SessionAdapter):
    """Official CLI app-server keeps one native thread alive across all deliveries.

    Persistent native threads allow the original desktop to open the exact B conversation.
    Closing the program ends this session; this is reported rather than recreated.
    """

    def start_session(self, link):
        self.context(link)
        self.start_transport()
        original = link["session_b"]
        result = self.rpc(
            "thread/resume" if original else "thread/start",
            {"threadId": original}
            if original
            else {
                "cwd": str(self.bridge.directory),
                "ephemeral": False,
                "developerInstructions": self.context_text,
            },
        )
        native = result["thread"]["id"]
        if original and native != original:
            raise RuntimeError("Codex 未恢复原生对话，禁止新建替代对话。")
        self.native_session_id = native
        self.on_native_session(native)
        self.attach_notice = bool(original)
        self.status = "READY"
        return native

    def start_transport(self, enable_mcp=True, experimental=False):
        directory = self.bridge.directory
        command = [
            self.installation.command,
            "app-server",
            "--stdio",
            *codex_overrides(self.bridge.phone.url, enable_mcp=enable_mcp),
            "-c",
            'log_dir="' + str(directory).replace("\\", "/") + '"',
        ]
        self.process = self.bridge._launch(command)
        self.events = queue.Queue()
        self.sequence = 0

        def reader():
            try:
                for line in self.process.stdout:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    self.events.put(value)
            finally:
                self.events.put({"closed": True})

        def stderr():
            # Drain diagnostics without storing credentials or private reasoning.
            for _ in self.process.stderr:
                pass

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=stderr, daemon=True).start()
        self.rpc(
            "initialize",
            {
                "clientInfo": {"name": "agentphone", "version": VERSION},
                "capabilities": {"experimentalApi": experimental},
            },
        )
        self.write({"method": "initialized"})

    def write(self, value):
        self.bridge._check_stop()
        if self.process.poll() is not None:
            raise RuntimeError("Codex 原生会话进程已退出；不能自动替换原会话。")
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next_event(self, deadline=None):
        while deadline is None or time.monotonic() < deadline:
            self.bridge._check_stop()
            try:
                event = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event.get("closed"):
                raise RuntimeError("Codex 原生会话连接已断开。")
            if "id" in event and "method" in event:
                # Never approve tool requests or masquerade them as a model reply.
                self.write(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "Approval required; the bridge cannot approve it",
                        },
                    }
                )
                raise RuntimeError("Codex 请求外部审批；通信桥不能代替用户批准，连接已停止。")
            return event
        raise RuntimeError("Codex 原生会话超时。")

    def rpc(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 60
        while True:
            event = self.next_event(deadline)
            if event.get("id") == request_id:
                if "error" in event:
                    raise RuntimeError(safe_error(str(event["error"])))
                return event["result"]

    def send_message(self, prompt):
        try:
            if getattr(self, "attach_notice", False):
                prompt = self.context_text + "\n" + prompt
                self.attach_notice = False
            result = self.rpc(
                "turn/start",
                {
                    "threadId": self.native_session_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            )
            turn_id = result["turn"]["id"]
            self.present_conversation()
            final = ""
            while True:
                event = self.next_event()
                params = event.get("params", {})
                if params.get("threadId") != self.native_session_id:
                    continue
                if event.get("method") == "item/completed" and params.get("turnId") == turn_id:
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        final = item.get("text", "")
                if (
                    event.get("method") == "turn/completed"
                    and params.get("turn", {}).get("id") == turn_id
                ):
                    if params["turn"].get("status") != "completed":
                        raise RuntimeError(safe_error(str(params["turn"].get("error"))))
                    return self.decode_result(final)
        except Exception:
            self.status = "FAILED"
            raise

    def open_native_conversation(self):
        open_codex_conversation(self.native_session_id)


class ClaudeAdapter(SessionAdapter):
    """Official Claude CLI print transport, retaining the reported native session."""

    def start_session(self, link):
        self.context(link)
        self.native_session_id = link["session_b"]
        self.mcp_config = self.bridge.directory / "bridge-mcp.json"
        self.mcp_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "agentphone_bridge": {
                            "type": "http",
                            "url": self.bridge.phone.url + "/mcp",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.status = "NEW"

    def send_message(self, prompt):
        original = self.native_session_id
        command = [
            self.installation.command,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--mcp-config",
            str(self.mcp_config),
            "--strict-mcp-config",
        ]
        if original:
            command += ["--resume", original]

        def observe(event):
            native = event.get("session_id")
            if not native or (self.native_session_id and native != self.native_session_id):
                raise RuntimeError("Claude Code 未报告原生会话，或更换了会话。")
            self.native_session_id = native
            self.on_native_session(native)

        try:
            text = self.bridge._execute(
                self.installation,
                self.context_text + "\n" + prompt if not original else prompt,
                180,
                command,
                observe,
                strict=True,
            )
            if not self.native_session_id:
                raise RuntimeError("Claude Code 未建立真实原生会话。")
            action = self.decode_result(text)
            self.status = "READY"
            return action
        except Exception:
            self.status = "FAILED"
            raise


class ACPAdapter(ACPTransport, SessionAdapter):
    """Native ACP transport with the same verified bridge receipt contract."""


ADAPTERS = {
    "codex": CodexAdapter,
    "deepseek_harness": DeepSeekAdapter,
    "workbuddy": WorkBuddyAdapter,
    "claude": ClaudeAdapter,
    "gemini": ACPAdapter,
    "qwen": ACPAdapter,
    "kimi": ACPAdapter,
}


def session_adapter_available(installation):
    return installation.callable and (
        desktop_conversation_available(installation)
        if installation.id in ADAPTERS
        else True  # Explicit test/custom adapter factory only.
    )


def create_adapter(runtime_key, phone, directory, installation, notify=None):
    adapter = ADAPTERS.get(runtime_key)
    if adapter is None:
        raise RuntimeError("该 Agent 尚无通信桥适配器，不能声明已连接。")
    if not desktop_conversation_available(installation):
        raise RuntimeError("该 Agent 缺少原桌面对话接口，按设置暂时禁止连接。")
    return adapter(phone, directory, installation, notify)
