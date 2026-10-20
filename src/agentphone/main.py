import argparse

import uvicorn

from .api import create_app
from .config import Settings
from .events import configure_logging


def main():
    parser = argparse.ArgumentParser(description="Run the independent AgentPhone service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--http-only", action="store_true", help="Disable the optional MCP adapter")
    args = parser.parse_args()
    configure_logging()
    uvicorn.run(
        create_app(Settings.from_env(), enable_mcp=not args.http_only),
        host=args.host,
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
