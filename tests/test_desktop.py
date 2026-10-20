import concurrent.futures
import sys
import time

import pytest

from agentphone.desktop import discovery
from agentphone.desktop.backend import LocalPhone
from agentphone.desktop.discovery import AgentInstallation
from agentphone.desktop.runtime import RunStopped, RuntimeBridge, codex_overrides, safe_error
from agentphone.desktop.storage import DesktopStorage


@pytest.fixture
def phone(tmp_path):
    server = LocalPhone(tmp_path / "phone", preferred_port=0, bridge_mode=False)
    yield server
    server.close()


def test_custom_installation_discovery_and_gui_is_not_cli(tmp_path, monkeypatch):
    application = tmp_path / "自定义目录/DeepSeek Harness.exe"
    command = application.parent / "resources/runtime/cli/bin/dsh.cmd"
    command.parent.mkdir(parents=True)
    command.write_text("test fixture")
    application.touch()
    gui_codex = tmp_path / "Store/Codex.exe"
    gui_codex.parent.mkdir()
    gui_codex.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(
        discovery,
        "process_paths",
        lambda: [("deepseek harness.exe", application), ("codex.exe", gui_codex)],
    )
    monkeypatch.setattr(discovery, "registry_locations", lambda: [])
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    found = {item.id: item for item in discovery.discover_agents()}
    assert found["deepseek_harness"].callable
    assert found["deepseek_harness"].command == str(command.resolve())
    assert found["codex"].running
    assert not found["codex"].callable


def test_codex_uses_existing_provider_but_disables_unrelated_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'model_provider="existing"\n[mcp_servers.other]\ncommand="test"\n', encoding="utf-8"
    )
    flags = codex_overrides("http://127.0.0.1:9876")
    assert 'mcp_servers.agentphone_bound.url="http://127.0.0.1:9876/mcp"' in flags
    assert "mcp_servers.other.enabled=false" in flags
    assert not any("provider" in argument for argument in flags)


def test_discovery_remembers_portable_agent_after_original_app_closes(tmp_path, monkeypatch):
    application = tmp_path / "便携软件/DeepSeek Harness.exe"
    command = application.parent / "resources/runtime/cli/bin/dsh.cmd"
    command.parent.mkdir(parents=True)
    command.touch()
    application.touch()
    store = DesktopStorage(tmp_path / "settings")
    store.remember_installations(
        [AgentInstallation("deepseek_harness", "DeepSeek", str(command.resolve()))]
    )
    cache = DesktopStorage(store.directory).data["detected_paths"]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(discovery, "process_paths", lambda: [])
    monkeypatch.setattr(discovery, "registry_locations", lambda: [])
    monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
    found = {agent.id: agent for agent in discovery.discover_agents(cached=cache)}
    assert found["deepseek_harness"].callable
    assert found["deepseek_harness"].application == str(application.resolve())
    assert not found["deepseek_harness"].running


def test_stop_terminates_only_owned_runtime_process(phone, tmp_path, monkeypatch):
    bridge = RuntimeBridge(phone, tmp_path)
    command = [sys.executable, "-c", "import time;time.sleep(30)"]
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(
            bridge._execute, AgentInstallation("codex", "test"), "test", 15, command
        )
        deadline = time.monotonic() + 5
        while bridge.process is None and time.monotonic() < deadline:
            time.sleep(0.02)
        process = bridge.process
        assert process is not None
        bridge.stop()
        with pytest.raises(RunStopped):
            future.result(timeout=5)
        assert process.poll() is not None


def test_error_messages_redact_tokens():
    assert "private-secret" not in safe_error("error Bearer private-secret")
    assert "private-secret" not in safe_error("API_KEY=private-secret failed")
