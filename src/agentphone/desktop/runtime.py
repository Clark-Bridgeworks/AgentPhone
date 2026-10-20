"""External runtime adapters. No inference or scheduling enters PhoneService."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

import psutil


def spawn_external(command, **options):
    """Do not leak the frozen Python DLL directory into installed Agent/native helpers."""
    kernel = None
    if os.name == "nt" and getattr(sys, "frozen", False):
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = (ctypes.c_wchar_p,)
        kernel.SetDllDirectoryW.restype = ctypes.c_int
        kernel.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(command, **options)
    finally:
        if kernel:
            kernel.SetDllDirectoryW(sys._MEIPASS)


class RunStopped(Exception):
    pass


def safe_error(text):
    text = re.sub(r"(?i)(bearer\s+|api[_-]?key[=: ]+|token[=: ]+)[^\s,;]+", r"\1[已隐藏]", text)
    if any(word in text.lower() for word in ["401", "unauthorized", "no credential", "not logged"]):
        return "Agent 尚未登录或账户认证已失效。请先打开原 Agent 完成登录，再重新发送。"
    if any(word in text.lower() for word in ["429", "quota", "insufficient", "rate limit"]):
        return "模型账户额度不足或请求过于频繁。请检查原 Agent 的账户，再稍后重试。"
    return text[-1600:]


def local_models():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:11434/api/tags", timeout=0.6) as response:
            models = json.load(response).get("models", [])
            models.sort(key=lambda item: item.get("size", float("inf")))
            return [item["name"] for item in models if item.get("name")]
    except (OSError, ValueError, KeyError):
        return []


def codex_overrides(url, enable_mcp=True):
    flags = [
        "-c",
        f'mcp_servers.agentphone_bound.url="{url}/mcp"',
        "-c",
        "mcp_servers.agentphone_bound.enabled=" + ("true" if enable_mcp else "false"),
    ]
    # Existing provider/auth/model configuration stays intact. Unrelated MCP servers need not boot.
    config = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        names = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {}).keys()
        for name in names:
            if name != "agentphone_bound" and re.fullmatch(r"[A-Za-z0-9_-]+", name):
                flags += ["-c", f"mcp_servers.{name}.enabled=false"]
    except (ValueError, OSError):
        pass
    return flags


class RuntimeBridge:
    def __init__(self, phone, directory, notify=None):
        self.phone = phone
        self.directory = Path(directory) / "runtime"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.notify = notify or (lambda *_args, **_kwargs: None)
        self.stop_event = threading.Event()
        self.process = None
        self.process_lock = threading.Lock()

    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            process = self.process
            if process is None or process.poll() is not None:
                return
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                parent.terminate()
                _, alive = psutil.wait_procs([*children, parent], timeout=1)
                for child in alive:
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _check_stop(self):
        if self.stop_event.is_set():
            raise RunStopped()

    def _launch(self, command):
        environment = dict(os.environ)
        bypass = environment.get("NO_PROXY", environment.get("no_proxy", ""))
        environment["NO_PROXY"] = environment["no_proxy"] = bypass + ",127.0.0.1,localhost"
        environment["PYTHONUTF8"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = spawn_external(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.directory,
            env=environment,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        with self.process_lock:
            self.process = process
        return process

    def _execute(self, agent, prompt, timeout, command=None, observe=None, strict=False):
        if command is None:
            raise RuntimeError("Agent Adapter 必须提供官方启动命令。")
        process = self._launch(command)
        events = queue.Queue()

        def reader(stream, channel):
            try:
                for line in stream:
                    if len(line) <= 524288:
                        events.put((channel, line))
            finally:
                stream.close()
                events.put((channel, None))

        for stream, channel in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            threading.Thread(target=reader, args=(stream, channel), daemon=True).start()
        try:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except BrokenPipeError:
                pass
            deadline = time.monotonic() + timeout
            final = ""
            native_error = False
            errors = []
            closed = set()
            while len(closed) < 2:
                self._check_stop()
                if time.monotonic() > deadline:
                    self.stop()
                    raise RuntimeError(
                        "Agent 等待超过 3 分钟。请检查原 Agent 登录、模型网络后重试。"
                    )
                try:
                    channel, line = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    closed.add(channel)
                    continue
                if channel == "stderr":
                    if any(word in line.lower() for word in ["error", "failed", "401", "429"]):
                        errors.append(safe_error(line))
                        errors = errors[-5:]
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if observe and (
                    kind in {"session", "thread.started", "result"}
                    or (kind == "system" and event.get("subtype") == "init")
                ):
                    observe(event)
                if kind == "final":
                    final = str(event.get("text", ""))
                elif kind == "result":
                    if event.get("is_error"):
                        native_error = True
                        errors.append(safe_error(str(event.get("errors", "Native Agent failed"))))
                    else:
                        final = str(event.get("result", ""))
                elif kind in ("error", "turn.failed"):
                    errors.append(safe_error(str(event.get("message", event.get("error", "")))))
                elif kind in ("item.completed", "item.updated"):
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        final = str(item.get("text", ""))
                    elif item.get("type") == "mcp_tool_call":
                        self.notify("progress", f"{agent.name} 正在通过消息通道处理回复…")
                elif kind == "tool_call":
                    self.notify("progress", f"{agent.name} 正在读取或发送消息…")
            return_code = process.wait(timeout=5)
            self._check_stop()
            if native_error or (return_code and (strict or not final)):
                raise RuntimeError(
                    safe_error("\n".join(errors))
                    or f"{agent.name} 启动失败（代码 {return_code}）。请先打开原 Agent 试聊。"
                )
            return final.strip()
        finally:
            if process.poll() is None:
                self.stop()
            with self.process_lock:
                self.process = None
