from mcp.client import Client

from agentphone.mcp_server import create_mcp


async def test_mcp_discovery_contract_and_shared_service(service):
    async with Client(create_mcp(service)) as client:
        listed = await client.list_tools()
        tools = {t.name: t for t in listed.tools}
        assert set(tools) == {"send_message", "read_messages", "get_conversation", "get_agents"}
        assert tools["send_message"].input_schema["required"] == ["sender", "recipient", "content"]
        assert set(tools["send_message"].input_schema["properties"]) == {
            "sender",
            "recipient",
            "content",
            "conversation_id",
            "reply_to_message_id",
            "sender_session_id",
            "new_connection",
            "wait_seconds",
        }
        sent = await client.call_tool(
            "send_message", {"sender": "agent_a", "recipient": "agent_b", "content": "MCP request"}
        )
        assert not sent.is_error
        message = sent.structured_content
        assert set(message) == {"message_id", "conversation_id", "status", "created_at"}
        # HTTP and MCP share exactly the same service instance and persisted state.
        assert (await service.read_messages("agent_b"))[0].id == message["message_id"]
        reply = await service.send_message(
            "agent_b", "agent_a", "HTTP/service reply", reply_to_message_id=message["message_id"]
        )
        inbox = await client.call_tool("read_messages", {"agent": "agent_a"})
        assert inbox.structured_content["messages"][0]["id"] == reply.id
        history = await client.call_tool(
            "get_conversation", {"conversation_id": message["conversation_id"]}
        )
        assert len(history.structured_content["messages"]) == 2
        agents = await client.call_tool("get_agents", {})
        assert len(agents.structured_content["agents"]) == 2


async def test_mcp_error_is_tool_error_without_partial_write(service):
    async with Client(create_mcp(service)) as client:
        bad = await client.call_tool(
            "send_message", {"sender": "agent_a", "recipient": "missing", "content": "hello"}
        )
        assert bad.is_error
        assert "agent_not_found" in bad.content[0].text
        assert not [e for e in await service.get_events() if e.type == "message.created"]
