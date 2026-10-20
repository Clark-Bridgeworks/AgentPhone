import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentphone.desktop import discovery
from agentphone.desktop.acp import ACP_ARGUMENTS
from agentphone.desktop.adapters import ACPAdapter, ClaudeAdapter
from agentphone.desktop.discovery import AgentInstallation
from agentphone.desktop.runtime import RuntimeBridge


def link(native=None):
    return dict(
        id="fixture-bridge",
        agent_a="original-a",
        agent_b="vendor",
        session_a="existing-a",
        session_b=native,
        binding_b="fixture-binding",
    )


def inbox(index=1):
    return dict(message_id=f"m{index}", content=f"Exact content {index}")


def acp_adapter(tmp_path, monkeypatch, vendor="gemini", mode="normal", native=None):
    monkeypatch.setenv("ACP_FIXTURE_MODE", mode)
    adapter = ACPAdapter(
        SimpleNamespace(url="http://127.0.0.1:9999"),
        tmp_path,
        AgentInstallation(vendor, vendor, "fixture-command"),
    )
    launch = adapter.bridge._launch
    fixture = Path(__file__).parent / "fixtures/acp_peer.py"
    commands = []

    def fixture_launch(command):
        commands.append(command)
        return launch([sys.executable, str(fixture)])

    monkeypatch.setattr(adapter.bridge, "_launch", fixture_launch)
    adapter.commands = commands
    try:
        adapter.start_session(link(native))
    except Exception:
        adapter.stop_session()
        raise
    return adapter


@pytest.mark.parametrize("vendor", ["gemini", "qwen", "kimi"])
def test_acp_actual_stdio_retains_session_and_never_forwards_thoughts(
    tmp_path, monkeypatch, vendor
):
    adapter = acp_adapter(tmp_path, monkeypatch, vendor)
    try:
        assert adapter.commands == [["fixture-command", *ACP_ARGUMENTS[vendor]]]
        for index in [1, 2]:
            result = adapter.receive_message(inbox(index))
            assert result["content"] == "Fixture reply"
            assert result["received_message_id"] == f"m{index}"
            assert result["received_content"] == f"Exact content {index}"
            assert "PRIVATE" not in json.dumps(result)
            assert adapter.native_session_id == "fixture-native-session"
    finally:
        adapter.stop_session()


def test_acp_restores_original_session_when_supported(tmp_path, monkeypatch):
    adapter = acp_adapter(tmp_path, monkeypatch, native="fixture-native-session")
    try:
        assert adapter.receive_message(inbox())["content"] == "Fixture reply"
    finally:
        adapter.stop_session()


@pytest.mark.parametrize("mode,error", [("version2", "版本不兼容"), ("no_load", "不支持恢复")])
def test_acp_rejects_incompatible_or_replacement_session(tmp_path, monkeypatch, mode, error):
    with pytest.raises(RuntimeError, match=error):
        acp_adapter(tmp_path, monkeypatch, mode=mode, native="fixture-native-session")


def test_acp_does_not_approve_native_permission_request(tmp_path, monkeypatch):
    adapter = acp_adapter(tmp_path, monkeypatch, mode="permission")
    try:
        with pytest.raises(RuntimeError, match="不能代替用户批准"):
            adapter.receive_message(inbox())
        assert adapter.status == "FAILED"
    finally:
        adapter.stop_session()


def test_claude_stream_json_resume_and_error_are_not_success(tmp_path, monkeypatch):
    adapter = ClaudeAdapter(
        SimpleNamespace(url="http://127.0.0.1:9999"),
        tmp_path,
        AgentInstallation("claude", "Claude", "fixture-claude"),
    )
    adapter.start_session(link())
    original_launch = adapter.bridge._launch
    commands = []

    def fixture_launch(command):
        commands.append(command)
        script = """import sys,json
p=sys.stdin.read()
m=json.loads(p.split('AgentPhone structured inbox:\\n')[-1])
print(json.dumps(dict(type='system',subtype='init',session_id='fixture-claude-native')))
print(json.dumps(dict(type='assistant',thinking='PRIVATE')))
action=dict(action='send_message',recipient_id='original-a',content='Claude fixture',
reply_to_message_id=m['message_id'],received_message_id=m['message_id'],
received_content=m['content'])
print(json.dumps(dict(type='result',session_id='fixture-claude-native',result=json.dumps(action))))
"""
        return original_launch([sys.executable, "-c", script])

    monkeypatch.setattr(adapter.bridge, "_launch", fixture_launch)
    try:
        assert adapter.receive_message(inbox())["content"] == "Claude fixture"
        assert adapter.receive_message(inbox(2))["content"] == "Claude fixture"
        assert "--resume" not in commands[0]
        assert commands[1][-2:] == ["--resume", "fixture-claude-native"]
        assert not any("permission" in flag for flag in commands[0])
    finally:
        adapter.stop_session()


def test_native_error_result_is_failure_even_if_exit_code_zero(tmp_path):
    runtime = RuntimeBridge(SimpleNamespace(url="unused"), tmp_path)
    command = [
        sys.executable,
        "-c",
        'import json;print(json.dumps(dict(type="result",is_error=True,errors=["failure"])))',
    ]
    with pytest.raises(RuntimeError, match="failure"):
        runtime._execute(AgentInstallation("claude", "Claude"), "", 5, command, strict=True)


def test_claude_detected_outside_path_and_missing_vendors_visible(tmp_path, monkeypatch):
    command = tmp_path / ".local/bin/claude.exe"
    command.parent.mkdir(parents=True)
    command.touch()
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    monkeypatch.setattr(discovery, "process_paths", lambda: [])
    monkeypatch.setattr(discovery, "registry_locations", lambda: [])
    found = {agent.id: agent for agent in discovery.discover_agents()}
    assert found["claude"].command == str(command.resolve()) and found["claude"].callable
    assert (
        not found["gemini"].callable and not found["qwen"].callable and not found["kimi"].callable
    )


@pytest.mark.parametrize(
    "vendor,relative",
    [
        ("gemini", "AppData/Roaming/npm/gemini.cmd"),
        ("qwen", "AppData/Roaming/npm/qwen.cmd"),
        ("kimi", "AppData/Roaming/Python/Python312/Scripts/kimi.exe"),
    ],
)
def test_other_vendor_cli_installation_paths(tmp_path, monkeypatch, vendor, relative):
    command = tmp_path / relative
    command.parent.mkdir(parents=True)
    command.touch()
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    monkeypatch.setattr(discovery, "process_paths", lambda: [])
    monkeypatch.setattr(discovery, "registry_locations", lambda: [])
    found = {agent.id: agent for agent in discovery.discover_agents()}
    assert found[vendor].command == str(command.resolve()) and found[vendor].callable


def test_kimi_desktop_chat_client_is_not_callable_cli(tmp_path, monkeypatch):
    application = tmp_path / "Apps/kimi-desktop/Kimi.exe"
    application.parent.mkdir(parents=True)
    application.touch()
    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    monkeypatch.setattr(discovery, "process_paths", lambda: [("kimi.exe", application)])
    monkeypatch.setattr(discovery, "registry_locations", lambda: [("kimi", application.parent)])
    found = {agent.id: agent for agent in discovery.discover_agents()}
    assert found["kimi"].application == str(application.resolve())
    assert not found["kimi"].command and not found["kimi"].callable
