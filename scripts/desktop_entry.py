"""PyInstaller entry point; normal use requires no Python installation."""

from agentphone.desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
