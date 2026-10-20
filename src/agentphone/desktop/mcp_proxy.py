"""A stdio connector to the running bridge, never a second bridge or model runtime."""

import os
import sys

from agentphone.mcp_server import create_mcp

from .client import call_tool


def restore_stdio():
    # A windowed Windows EXE has no Python stdio objects, even with inherited MCP pipes.
    if os.name == "nt":
        import ctypes
        import io
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        for name, handle_id, mode, flags in (
            ("stdin", -10, "rb", os.O_RDONLY),
            ("stdout", -11, "wb", os.O_WRONLY),
            ("stderr", -12, "wb", os.O_WRONLY),
        ):
            if getattr(sys, name) is None:
                handle = kernel.GetStdHandle(handle_id & 0xFFFFFFFF)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise RuntimeError("MCP stdio requires inherited pipes")
                fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
                setattr(sys, name, io.TextIOWrapper(os.fdopen(fd, mode), encoding="utf-8"))
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def run_proxy(directory):
    restore_stdio()

    async def forward(name, arguments):
        # locals() also contains closure cells; send only the public JSON schema fields.
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"forward", "bridge", "service", "invoke"}
        }
        return await call_tool(directory, name, arguments, infer_session=False)

    create_mcp(None, forward=forward).run(transport="stdio")
