"""Connect to the existing, authenticated DSH Desktop Host and select its own session."""

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import psutil

from .runtime import RunStopped, spawn_external


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def browser_secret(home):
    # Read this one local-browser grant only; never read a model key into our client.
    rows = []
    with (Path(home) / ".credentials.yaml").open(encoding="utf-8") as source:
        for line in source:
            if line.rstrip() == "  client-connection/browser-session:":
                for child in source:
                    if child.strip() and not child.startswith("    "):
                        break
                    rows.append(child)
                break
    record = "".join(rows)
    if not record or not re.search(r"^    kind: grant\s*$", record, re.M):
        raise RuntimeError("DSH 桌面端尚未建立本机连接认证；请先正常启动原软件。")
    found = re.search(r"^      secret: ([A-Za-z0-9_-]+)\s*$", record, re.M)
    if not found or not re.search(r"^      version: 1\s*$", record, re.M):
        raise RuntimeError("DSH 本机连接认证格式不受支持。")
    secret = base64.urlsafe_b64decode(found[1] + "=" * (-len(found[1]) % 4))
    if len(secret) != 32:
        raise RuntimeError("DSH 本机连接认证格式不受支持。")
    return secret


def browser_cookie(authority, secret, now=None):
    now = int(time.time() * 1000) if now is None else now
    # Windows Python and Node wall clocks have different millisecond resolutions.
    # DSH rejects cookies issued even a few milliseconds ahead of Date.now().
    issued = now - 1000
    payload = dict(version=1, authority=authority, issuedAt=issued, expiresAt=issued + 300000)
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    name = "dsh-auth-" + _b64(hashlib.sha256(authority.encode()).digest())
    signature = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return name + "=v1." + body + "." + signature


class DshReadUnavailable(RuntimeError):
    """A read-only observation failed; this says nothing about the Agent's work."""


class DshDesktopClient:
    def __init__(self, application, directory):
        if os.name != "nt":
            raise RuntimeError("DSH 原生窗口连接仅适用于 Windows。")
        self.application = Path(application).resolve()
        if not self.application.is_file():
            raise RuntimeError("没有找到 DeepSeek Harness 原生桌面程序。")
        self.directory = Path(directory)
        self.home = Path(os.getenv("DSH_HOME") or Path.home() / ".dsh")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.authority = None
        self.secret = None
        self.desktop_pid = None

    def connect(self):
        deadline = time.monotonic() + 35
        launched = False
        while time.monotonic() < deadline:
            for process in psutil.process_iter(["exe", "cmdline", "ppid"]):
                try:
                    info = process.info
                    if not info["exe"] or Path(info["exe"]).resolve() != self.application:
                        continue
                    if not any("dsh-desktop-host" in arg for arg in info["cmdline"] or []):
                        continue
                    parent = psutil.Process(info["ppid"])
                    if Path(parent.exe()).resolve() != self.application:
                        continue
                    self.home = Path(parent.environ().get("DSH_HOME") or Path.home() / ".dsh")
                    for connection in process.net_connections(kind="tcp"):
                        if connection.status != psutil.CONN_LISTEN:
                            continue
                        if connection.laddr.ip not in {"127.0.0.1", "::1"}:
                            continue
                        self.authority = "127.0.0.1:" + str(connection.laddr.port)
                        self.secret = browser_secret(self.home)
                        self.desktop_pid = parent.pid
                        self.rpc("session/modelCatalog")
                        return
                except (psutil.Error, OSError, ValueError):
                    continue
            if not launched:
                # The installed app owns its process. Never terminate it with the bridge.
                spawn_external([str(self.application)], creationflags=subprocess.CREATE_NO_WINDOW)
                launched = True
            time.sleep(0.3)
        raise RuntimeError("DSH 原生桌面服务未就绪，无法打开真实桥接对话。")

    def rpc(self, method, request=None):
        args = (
            {}
            if request is None
            else {"_request" if method == "session/list" else "request": request}
        )
        rpc_id = str(uuid4())
        envelope = dict(type="client-request", rpcId=rpc_id, method=method, payload={"args": args})
        query = urllib.request.Request(
            "http://" + self.authority + "/api/" + method,
            json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": browser_cookie(self.authority, self.secret),
            },
        )
        try:
            with self.opener.open(query, timeout=6) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise RuntimeError(
                    "DSH 本机桌面连接授权未通过；请重新打开原 DSH 后重连。"
                ) from None
            if method in {"session/projections", "session/page"} and error.code >= 500:
                raise DshReadUnavailable("暂时无法读取 DSH 原生对话状态。") from None
            raise RuntimeError(f"DSH 本机会话接口拒绝请求（HTTP {error.code}）。") from None
        except OSError:
            if method in {"session/projections", "session/page"}:
                raise DshReadUnavailable("暂时无法读取 DSH 原生对话状态。") from None
            # Never retry prompt submission: it may already have been accepted.
            raise RuntimeError("DSH 本机接口连接中断，请检查原 DSH；请求未自动重发。") from None
        if result.get("rpcId") != rpc_id or result.get("type") != "server-response":
            raise RuntimeError("DSH 返回了不匹配的本机会话响应。")
        if not result["result"]["ok"]:
            detail = result["result"].get("error", {})
            raise RuntimeError("DSH 会话接口：" + detail.get("message", "unknown error"))
        return result["result"].get("value")

    def create(self, cwd, session_id=None):
        request = {"cwd": str(cwd)}
        if session_id:
            request["sessionId"] = session_id
        result = self.rpc("session/create", request)
        native_id = result["sessionId"]
        if session_id and native_id != session_id:
            raise RuntimeError("DSH 原生会话被替换，不能冒充恢复。")
        return native_id

    def rename(self, session_id, title):
        self.rpc("session/rename", {"sessionId": session_id, "title": title})

    def sessions(self):
        rows = []
        for item in self.rpc("session/list", {})["items"]:
            if item.get("parentSessionId") or not item.get("cwd"):
                continue
            title = item.get("projections", {}).get("values", {}).get("title", {})
            if isinstance(title, dict):
                title = title.get("title") or title.get("name") or ""
            rows.append(
                {
                    "native_session_id": item["sessionId"],
                    "cwd": item["cwd"],
                    "title": title if isinstance(title, str) else "",
                    "updated_at": item["updatedAt"],
                    "running": item["running"],
                }
            )
        return rows

    def session_info(self, session_id):
        row = next((r for r in self.sessions() if r["native_session_id"] == session_id), None)
        if row is None:
            raise RuntimeError("找不到所选 DSH 原生对话；没有新建替代对话。")
        return row

    def prompt(self, session_id, text, request_id):
        self.rpc(
            "session/prompt",
            dict(
                sessionId=session_id,
                requestId=request_id,
                mode="queue",
                content=[{"type": "text", "text": text}],
            ),
        )

    def events(self, session_id):
        projections = self.rpc("session/projections", {"sessionId": session_id})
        if projections is None:
            return []
        page = self.rpc(
            "session/page",
            dict(
                address={"kind": "session", "sessionId": session_id},
                throughSeq=projections["asOfSeq"],
                maxMessages=8,
            ),
        )
        return [row["event"] for row in page["records"]]

    def select_window(self, title, stop_event=None, session_id=None):
        from importlib.resources import files

        if not session_id:
            raise RuntimeError("缺少真实 DSH 原生会话 ID，不能选择窗口。")
        sessions = self.sessions()
        matching = [s for s in sessions if s["native_session_id"] == session_id]
        if len(matching) != 1 or matching[0]["title"] != title:
            raise RuntimeError("DSH 原对话名称或身份发生变化，请刷新后重新选择。")
        if sum(title in s["title"] for s in sessions) != 1:
            raise RuntimeError("DSH 存在同名对话，无法唯一定位窗口；请在原软件给目标换个名称。")
        helper = files("agentphone.desktop").joinpath("native/dsh-window.exe")
        request_path = self.directory / "native-window-request.json"
        response_path = self.directory / "native-window-response.json"
        request_path.write_text(
            json.dumps(
                {"pid": self.desktop_pid, "title": title, "native_session_id": session_id},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        child = spawn_external(
            [str(helper), str(request_path), str(response_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 18
            while child.poll() is None:
                if stop_event and stop_event.is_set():
                    raise RunStopped()
                if time.monotonic() > deadline:
                    raise RuntimeError("DSH 原生窗口选择超时。")
                time.sleep(0.1)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        result = json.loads(response_path.read_text(encoding="utf-8-sig"))
        if (
            child.returncode
            or not result.get("selected")
            or result.get("pid") != self.desktop_pid
            or result.get("title") != title
        ):
            raise RuntimeError("DSH 新对话未能在原生窗口显示：" + result.get("error", "未知错误"))
