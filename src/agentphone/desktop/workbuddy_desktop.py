"""Authenticated connection to an extension loaded by the original WorkBuddy host."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from .runtime import spawn_external


def workbuddy_home():
    return Path(os.getenv("WORKBUDDY_CONFIG_DIR") or Path.home() / ".workbuddy")


def extension_source():
    return Path(__file__).parent / "native/workbuddy-bridge"


def install_extension(home, write):
    """Only our three owned files; user's other extensions/settings remain intact."""
    source = extension_source()
    target = Path(home) / ".workbuddy/extensions/agentphone-bridge"
    for relative in ("extension.json", "distribution.json", "server/index.cjs"):
        write(target / relative, (source / relative).read_text(encoding="utf-8"))


def desktop_registered(application):
    if os.name != "nt" or not application or not Path(application).is_file():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "workbuddy") as key:
            winreg.QueryValueEx(key, "URL Protocol")
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"workbuddy\shell\open\command") as key:
            command = str(winreg.QueryValueEx(key, "")[0])
        return command.lower().startswith('"' + str(Path(application).resolve()).lower() + '"')
    except OSError:
        return False


class WorkBuddyDesktopClient:
    def __init__(self, application):
        self.application = Path(application).resolve()
        self.home = workbuddy_home()
        self.endpoint_path = self.home / "extensions/agentphone-bridge/endpoint.json"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.endpoint = None

    def validate_endpoint(self, data):
        if data.get("protocol") != 1 or not isinstance(data.get("port"), int):
            raise RuntimeError("WorkBuddy 连接扩展协议不兼容。")
        if not 1 <= data["port"] <= 65535 or len(data.get("token", "")) != 64:
            raise RuntimeError("WorkBuddy 连接扩展认证格式无效。")
        process = psutil.Process(data["pid"])
        if process.ppid() != data["host_pid"]:
            raise RuntimeError("WorkBuddy 连接扩展宿主已变更。")
        if not any(Path(p.exe()).resolve() == self.application for p in process.parents()):
            raise RuntimeError("连接扩展没有运行在所选原生 WorkBuddy 内。")
        return data

    def connect(self):
        if not desktop_registered(self.application):
            raise RuntimeError("WorkBuddy 原桌面对话入口未注册或指向另一款软件，禁止隐藏投递。")
        deadline = time.monotonic() + 25
        launched = False
        while time.monotonic() < deadline:
            try:
                self.endpoint = self.validate_endpoint(
                    json.loads(self.endpoint_path.read_text(encoding="utf-8"))
                )
                result = self.rpc("health")
                if result.get("pid") == self.endpoint["pid"] and result.get("agent") == "workbuddy":
                    return
            except (OSError, ValueError, KeyError, psutil.Error, RuntimeError):
                self.endpoint = None
            if not launched:
                spawn_external([str(self.application)], creationflags=subprocess.CREATE_NO_WINDOW)
                launched = True
            time.sleep(0.3)
        raise RuntimeError(
            "WorkBuddy 原生连接扩展尚未就绪。请从原软件菜单正常退出后重新打开一次；"
            "不要中断正在运行的任务。"
        )

    def rpc(self, method, **arguments):
        if not self.endpoint:
            raise RuntimeError("尚未连接 WorkBuddy 原生宿主。")
        self.validate_endpoint(self.endpoint)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.endpoint['port']}/bridge",
            json.dumps({"method": method, **arguments}, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.endpoint["token"],
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                envelope = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get("error", "WorkBuddy 原生连接失败。")
            except (ValueError, OSError):
                message = "WorkBuddy 原生连接失败。"
            raise RuntimeError(message) from None
        if "result" not in envelope:
            raise RuntimeError("WorkBuddy 连接器没有返回真实结果。")
        return envelope["result"]

    def sessions(self, cursor=None):
        return self.rpc("list", page=int(cursor or 1))

    def session_info(self, native):
        return self.rpc("get", session_id=native)

    def create(self, cwd, title):
        return self.rpc("create", cwd=str(Path(cwd).resolve()), title=title)

    def prompt(self, native, content, request_id):
        return self.rpc("prompt", session_id=native, content=content, request_id=request_id)

    def poll(self, native, request_id):
        return self.rpc("poll", session_id=native, request_id=request_id)

    def open_conversation(self, native):
        import re

        if not isinstance(native, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", native
        ):
            raise RuntimeError("WorkBuddy 未提供有效原生会话 ID。")
        self.session_info(native)
        if not desktop_registered(self.application):
            raise RuntimeError("WorkBuddy 原桌面入口已改变。")
        os.startfile("workbuddy://chat/" + native)
