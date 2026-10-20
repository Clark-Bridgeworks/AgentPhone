"""Expose the same minimal MCP tools to the caller's existing agent/session."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse


def default_directory():
    return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local")) / "AgentPhone/ABBridge"


async def call_tool(directory, tool, arguments, *, infer_session=True):
    from mcp.client import Client

    endpoint = json.loads((Path(directory) / "bridge-endpoint.json").read_text(encoding="utf-8"))
    url = endpoint["url"]
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("AgentPhone 通信桥只连接本机地址。")
    arguments = dict(arguments)
    if tool in {"send_message", "read_messages"} and not arguments.get("sender_session_id"):
        if not infer_session:
            raise RuntimeError("请提供当前原生对话的 sender_session_id；常驻 MCP 接口不猜测会话。")
        # The current process inherits this from the existing Codex chat. Never generate a new A.
        native_id = os.getenv("CODEX_THREAD_ID") or os.getenv("CODEX_SESSION_ID")
        if not native_id:
            raise RuntimeError("请由当前 Agent 提供其真实 sender_session_id；桥不会新建发起端。")
        arguments["sender_session_id"] = native_id
    bypass = os.getenv("NO_PROXY", os.getenv("no_proxy", ""))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass + ",127.0.0.1,localhost"
    async with Client(url + "/mcp", read_timeout_seconds=50) as client:
        result = await client.call_tool(tool, arguments, read_timeout_seconds=50)
    if result.is_error:
        raise RuntimeError("\n".join(c.text for c in result.content if c.type == "text"))
    return result.structured_content
