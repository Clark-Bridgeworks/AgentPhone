"""One native desktop presentation per bridge, independent of message/turn count."""

import os
from pathlib import Path


def codex_desktop_registered():
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "codex") as key:
            winreg.QueryValueEx(key, "URL Protocol")
        return True
    except OSError:
        return False


def desktop_conversation_available(installation):
    if installation.id == "deepseek_harness":
        return bool(installation.application and Path(installation.application).is_file())
    if installation.id == "codex":
        return codex_desktop_registered()
    if installation.id == "workbuddy":
        from .workbuddy_desktop import desktop_registered

        return desktop_registered(installation.application)
    return False


class NativePresentation:
    def __init__(self, directory, conversation_id):
        self.record = Path(directory) / "desktop-presentation.json"
        self.conversation_id = conversation_id

    def show_once(self, native_session_id, show):
        import json

        if not native_session_id:
            raise RuntimeError("尚未确认原生会话，不能打开其他对话代替。")
        expected = {"conversation_id": self.conversation_id, "native_session_id": native_session_id}
        if self.record.is_file():
            previous = json.loads(self.record.read_text(encoding="utf-8"))
            if previous != expected:
                raise RuntimeError("原桌面对话记录与当前原生会话不一致。")
            return False
        show()
        # Record only successful presentation. Failed launches remain observable/retryable.
        self.record.write_text(json.dumps(expected), encoding="utf-8")
        return True


def open_codex_conversation(native_session_id):
    from uuid import UUID

    try:
        native_session_id = str(UUID(native_session_id))
    except ValueError as error:
        raise RuntimeError("Codex 未提供有效原生会话 ID。") from error
    if not codex_desktop_registered():
        raise RuntimeError("未找到 Codex 原桌面对话入口，禁止隐藏投递。")
    os.startfile("codex://threads/" + native_session_id)
