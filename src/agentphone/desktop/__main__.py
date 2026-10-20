import argparse
import asyncio
import ctypes
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from . import VERSION
from .backend import LocalPhone
from .discovery import discover_agents


def self_test(output):
    import tkinter
    from importlib.resources import files

    from mcp.client import Client

    result = {
        "version": VERSION,
        "status": "failed",
        "scope": "packaging/storage/MCP/detection; no model calls",
    }
    with tempfile.TemporaryDirectory(prefix="agentphone-check-") as directory:
        phone = None
        try:
            result["tcl"] = tkinter.Tcl().eval("info patchlevel")
            window = tkinter.Tk()
            window.withdraw()
            window.update_idletasks()
            window.destroy()
            result["native_tk_window"] = True
            phone = LocalPhone(Path(directory), preferred_port=0, bridge_mode=False)
            result["health"] = phone.request("/health")
            for agent in ["storage-check-a", "storage-check-b"]:
                phone.request("/agents/register", {"agent_id": agent, "name": agent})
            sent = phone.send("storage-check-a", "storage-check-b", "local storage check")
            assert phone.history(sent["conversation_id"])[0]["content"] == "local storage check"

            async def check_mcp():
                async with Client(phone.url + "/mcp") as client:
                    listed = await client.list_tools()
                    return sorted(tool.name for tool in listed.tools)

            result["mcp_tools"] = asyncio.run(check_mcp())
            assert result["mcp_tools"] == [
                "get_agents",
                "get_conversation",
                "read_messages",
                "send_message",
            ]
            result["installed_agents"] = [agent.to_dict() for agent in discover_agents()]
            result["embedded_manual"] = "通信桥" in files("agentphone.desktop").joinpath(
                "guide.html"
            ).read_text(encoding="utf-8")
            assert result["embedded_manual"]
            result["native_dsh_window_helper"] = (
                files("agentphone.desktop").joinpath("native/dsh-window.exe").is_file()
            )
            assert result["native_dsh_window_helper"]
            result["native_workbuddy_extension"] = all(
                files("agentphone.desktop").joinpath("native/workbuddy-bridge/" + name).is_file()
                for name in ("extension.json", "distribution.json", "server/index.cjs")
            )
            assert result["native_workbuddy_extension"]
            result["mac_ui_assets"] = all(
                files("agentphone.desktop").joinpath(name).is_file()
                for name in ("agentphone.png", "agentphone.ico", "agentphone.svg")
            )
            assert result["mac_ui_assets"]
            result["status"] = "passed"
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            if phone:
                phone.close()
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description="AgentPhone native desktop")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--self-test", metavar="REPORT_JSON")
    parser.add_argument("--mcp-stdio", action="store_true", help="现有 Agent 的本机 MCP 接口")
    parser.add_argument(
        "--session-context", choices=["claude", "workbuddy"], help="原生会话启动上下文接口"
    )
    parser.add_argument(
        "--tool", choices=["get_agents", "send_message", "read_messages", "get_conversation"]
    )
    parser.add_argument("--request", type=Path, help="现有 Agent 的工具参数 JSON")
    parser.add_argument("--response", type=Path, help="工具响应 JSON（窗口版 EXE 没有终端输出）")
    args = parser.parse_args()
    logging.basicConfig(handlers=[logging.NullHandler()], level=logging.WARNING)
    if args.session_context:
        from .awareness import session_context
        from .mcp_proxy import restore_stdio

        restore_stdio()
        print(
            json.dumps(
                session_context(json.load(sys.stdin), args.session_context), ensure_ascii=False
            ),
            flush=True,
        )
        return 0
    if args.mcp_stdio:
        from .client import default_directory
        from .mcp_proxy import run_proxy

        run_proxy(args.data_dir or default_directory())
        return 0
    if args.self_test:
        return self_test(args.self_test)
    if args.tool:
        from .client import call_tool, default_directory

        try:
            arguments = (
                json.loads(args.request.read_text(encoding="utf-8-sig")) if args.request else {}
            )
            result = asyncio.run(
                call_tool(args.data_dir or default_directory(), args.tool, arguments)
            )
            result = {"status": "ok", "result": result}
            exit_code = 0
        except Exception as error:
            result = {"status": "failed", "error": str(error)}
            exit_code = 1
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        if args.response:
            args.response.write_text(serialized, encoding="utf-8")
        elif sys.stdout:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(serialized)
        return exit_code
    import tkinter as tk
    from tkinter import messagebox

    from .app import DesktopApp
    from .client import default_directory

    directory = args.data_dir or default_directory()
    mutex = None
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        name = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
        mutex = kernel.CreateMutexW(None, False, "Local\\AgentPhone-" + name)
        if ctypes.get_last_error() == 183:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                "AgentPhone 已打开", "软件已经在运行。请在任务栏找到 AgentPhone 窗口。"
            )
            root.destroy()
            kernel.CloseHandle(mutex)
            return 0
    phone = None
    try:
        phone = LocalPhone(directory)
        if getattr(sys, "frozen", False) and directory.resolve() == default_directory().resolve():
            from .awareness import install_awareness

            installed = [agent.id for agent in discover_agents() if agent.callable]
            install_awareness(Path.home(), directory, sys.executable, installed)
        root = tk.Tk()
        DesktopApp(root, phone, directory)
        root.mainloop()
    except Exception as error:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "startup-error.txt").write_text(
            f"{type(error).__name__}: {error}", encoding="utf-8"
        )
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("AgentPhone 启动失败", f"{error}\n\n错误记录保存在：{directory}")
        root.destroy()
        return 1
    finally:
        if phone and not phone.server.should_exit:
            phone.close()
        if mutex:
            kernel.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
