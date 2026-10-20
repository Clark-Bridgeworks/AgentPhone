import logging
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .errors import PhoneError
from .service import PhoneService

logger = logging.getLogger("agentphone.mcp")


def create_mcp(service: PhoneService | None, bridge=None, forward=None) -> MCPServer:
    """Only a transport adapter; all domain operations belong to PhoneService."""
    mcp = MCPServer(
        "AgentPhone",
        version="0.1.0",
        instructions=(
            "AgentPhone 是本机双 Agent 通信桥。用户要求与其他 Agent 协作或沟通时，优先通过这里"
            "联系真实 Agent，保持当前原生对话。send_message 的 inbox 是对方的真实回信；"
            "awaiting_reply=true 时继续 read_messages(wait_seconds=30)，不能只发送就结束。"
            "必须使用真实 sender_session_id，不得编造；本工具不规划、分工、评判或总结任务。"
            "get_agents.selection 是用户选择的原对话；原生身份必须匹配。断联后可保留同一"
            "conversation_id 续接，不新建对话冒充恢复，不重发旧失败任务。"
        ),
    )

    async def invoke(operation):
        try:
            return await operation
        except PhoneError as exc:
            logger.warning(
                "MCP tool error", extra={"phone_event": {"code": exc.code, "detail": exc.detail}}
            )
            raise ToolError(f"{exc.code}: {exc.detail}") from exc
        except Exception as exc:
            logger.exception("Unexpected MCP tool error")
            raise ToolError("internal_error: AgentPhone operation failed") from exc

    @mcp.tool(structured_output=True)
    async def send_message(
        sender: str,
        recipient: str,
        content: str,
        conversation_id: str | None = None,
        reply_to_message_id: str | None = None,
        sender_session_id: str | None = None,
        new_connection: bool = False,
        wait_seconds: float = 25,
    ) -> dict[str, Any]:
        """Forward a message. new_connection=True switches an idle pair; omit old IDs then."""
        if forward is not None:
            return await forward("send_message", locals())
        if bridge is not None:
            return await invoke(
                bridge.send_and_receive(
                    sender,
                    recipient,
                    content,
                    conversation_id,
                    reply_to_message_id,
                    sender_session_id,
                    new_connection=new_connection,
                    wait_seconds=wait_seconds,
                )
            )
        message = await invoke(
            service.send_message(
                sender,
                recipient,
                content,
                conversation_id,
                reply_to_message_id,
            )
        )
        return {
            "message_id": message.id,
            "conversation_id": message.conversation_id,
            "status": message.status,
            "created_at": message.created_at,
        }

    @mcp.tool(structured_output=True)
    async def read_messages(
        agent: str,
        after_message_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 50,
        sender_session_id: str | None = None,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Read the next inbox page, oldest first, and mark its messages read."""
        if forward is not None:
            return await forward("read_messages", locals())
        if bridge is not None:
            return await invoke(
                bridge.read_messages(
                    agent, after_message_id, conversation_id, limit, sender_session_id, wait_seconds
                )
            )
        messages = await invoke(
            service.read_messages(agent, after_message_id, conversation_id, limit)
        )
        return {"messages": [message.model_dump() for message in messages]}

    @mcp.tool(structured_output=True)
    async def get_conversation(conversation_id: str, limit: int = 100) -> dict[str, Any]:
        """Return the latest history window ordered oldest to newest, without marking read."""
        if forward is not None:
            return await forward("get_conversation", locals())
        if bridge is not None and (not bridge.link or conversation_id != bridge.link["id"]):
            raise ToolError("bridge_not_connected: Conversation is not the active A/B bridge")
        if bridge is not None:
            from .service import page_limit

            page_limit(limit)
            snapshot = await invoke(bridge.snapshot())
            return {
                "conversation": {"id": conversation_id},
                "messages": snapshot["messages"][-limit:],
            }
        history = await invoke(service.get_conversation(conversation_id, limit))
        return {
            "conversation": history["conversation"].model_dump(),
            "messages": [message.model_dump() for message in history["messages"]],
        }

    @mcp.tool(structured_output=True)
    async def get_agents() -> dict[str, Any]:
        """List registered agents and their last-seen timestamps."""
        if forward is not None:
            return await forward("get_agents", {})
        if bridge is not None:
            return await invoke(bridge.get_agents())
        agents = await invoke(service.get_agents())
        return {"agents": [agent.model_dump() for agent in agents]}

    return mcp
