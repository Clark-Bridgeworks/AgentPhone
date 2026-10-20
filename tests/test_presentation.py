from types import SimpleNamespace

import pytest

from agentphone.desktop.adapters import create_adapter, session_adapter_available
from agentphone.desktop.discovery import AgentInstallation
from agentphone.desktop.presentation import NativePresentation, open_codex_conversation


def test_once_is_per_connection_and_survives_adapter_restart(tmp_path):
    calls = []
    first = NativePresentation(tmp_path, "bridge-1")
    assert first.show_once("native-1", lambda: calls.append("opened"))
    assert not first.show_once("native-1", lambda: calls.append("reopened"))
    restored = NativePresentation(tmp_path, "bridge-1")
    assert not restored.show_once("native-1", lambda: calls.append("restored-reopened"))
    assert calls == ["opened"]
    with pytest.raises(RuntimeError, match="不一致"):
        restored.show_once("replacement-native", lambda: calls.append("wrong"))
    other_directory = tmp_path / "bridge-2"
    other_directory.mkdir()
    assert NativePresentation(other_directory, "bridge-2").show_once(
        "native-2", lambda: calls.append("new-connection")
    )
    assert calls == ["opened", "new-connection"]


def test_failed_native_launch_does_not_mark_shown(tmp_path):
    presenter = NativePresentation(tmp_path, "bridge")

    def failed():
        raise RuntimeError("native desktop refused")

    with pytest.raises(RuntimeError, match="refused"):
        presenter.show_once("native", failed)
    assert not presenter.record.exists()
    assert presenter.show_once("native", lambda: None)


@pytest.mark.parametrize("agent_id", ["claude", "gemini", "qwen", "kimi"])
def test_cli_only_targets_are_rejected_even_when_installed(tmp_path, agent_id):
    command = tmp_path / "installed.exe"
    command.touch()
    target = AgentInstallation(agent_id, agent_id, command=str(command))
    assert target.callable
    assert not session_adapter_available(target)
    with pytest.raises(RuntimeError, match="禁止连接"):
        create_adapter(agent_id, SimpleNamespace(), tmp_path, target)


def test_codex_uses_exact_existing_thread_deep_link(monkeypatch):
    from agentphone.desktop import presentation

    calls = []
    monkeypatch.setattr(presentation, "codex_desktop_registered", lambda: True)
    monkeypatch.setattr(presentation.os, "startfile", calls.append, raising=False)
    native = "01234567-89ab-cdef-0123-456789abcdef"
    open_codex_conversation(native)
    assert calls == ["codex://threads/" + native]
    with pytest.raises(RuntimeError, match="有效原生"):
        open_codex_conversation("threads/new?prompt=duplicate")
    monkeypatch.setattr(presentation, "codex_desktop_registered", lambda: False)
    with pytest.raises(RuntimeError, match="禁止隐藏"):
        open_codex_conversation(native)
    assert len(calls) == 1
