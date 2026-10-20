import json
import tomllib

import pytest
from mcp.client import Client

from agentphone.desktop.awareness import install_awareness, managed_block, session_context
from agentphone.mcp_server import create_mcp


def test_native_setup_preserves_other_settings_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".dsh/profiles/desktop").mkdir(parents=True)
    config = home / ".codex/config.toml"
    config.write_text(
        '[mcp_servers.agentphone]\nurl="http://127.0.0.1:8765/mcp"\n[other]\nvalue="保留"\n',
        encoding="utf-8",
    )
    claude = home / ".claude.json"
    original = {
        "projects": {"original": {"mcpServers": {}}},
        "mcpServers": {"other": {"command": "keep"}},
    }
    claude.write_text(json.dumps(original), encoding="utf-8")
    dsh = home / ".dsh/profiles/desktop/cordis.patch.yml"
    dsh.write_text("- id: original\n  config: {}\n", encoding="utf-8")
    directory = tmp_path / "local/ABBridge"
    exe = tmp_path / "软件/AgentPhone.exe"
    first = install_awareness(home, directory, exe, ["codex", "claude", "deepseek_harness"])
    assert not first["errors"]
    assert tomllib.loads(config.read_text(encoding="utf-8"))["other"] == {"value": "保留"}
    new_claude = json.loads(claude.read_text(encoding="utf-8"))
    assert new_claude["projects"] == original["projects"]
    assert new_claude["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert dsh.read_text(encoding="utf-8").startswith("- id: original\n")
    assert "--mcp-stdio" in config.read_text(encoding="utf-8")
    second = install_awareness(home, directory, exe, ["codex", "claude", "deepseek_harness"])
    assert second["changed"] == [] and not second["errors"]
    assert list((directory.parent / "ConfigBackups").rglob("*.backup"))


def test_incomplete_managed_block_is_not_overwritten():
    with pytest.raises(ValueError):
        managed_block("<!-- AgentPhone managed start -->\nexisting", "new")


def test_claude_native_context_uses_only_actual_session_start_event():
    event = {"session_id": "actual-native-id", "hook_event_name": "SessionStart"}
    result = session_context(event)
    assert "actual-native-id" in result["hookSpecificOutput"]["additionalContext"]
    assert session_context({"session_id": "another", "hook_event_name": "Stop"}) == {}


async def test_stdio_proxy_uses_same_four_schemas_and_json_arguments_only():
    calls = []

    async def forward(name, arguments):
        public = {
            k: v
            for k, v in arguments.items()
            if k not in {"forward", "bridge", "service", "invoke"}
        }
        json.dumps(public)
        calls.append((name, public))
        return {"ok": True}

    async with Client(create_mcp(None, forward=forward)) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == {"get_agents", "send_message", "read_messages", "get_conversation"}
        result = await client.call_tool(
            "send_message", {"sender": "a", "recipient": "b", "content": "中文"}
        )
        assert not result.is_error and calls[0][1]["wait_seconds"] == 25
