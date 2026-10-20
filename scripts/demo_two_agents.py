"""Start three independent processes and verify communication and restart durability."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_health(url, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("AgentPhone exited; inspect server.log")
        try:
            response = httpx.get(f"{url}/health", timeout=1)
            if response.is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError("AgentPhone health check timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["mcp", "http"], default="mcp")
    parser.add_argument("--wait-mode", choices=["events", "polling"], default="events")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = (args.output_dir or ROOT / "demo-results" / str(uuid4())).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "phone.db").exists():
        raise ValueError("Choose a fresh output directory for an isolated demo")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "AGENTPHONE_DATABASE": str(output / "phone.db")}
    server_command = [sys.executable, "-m", "agentphone.main", "--port", str(port)]
    if args.transport == "http":
        server_command.append("--http-only")
    processes = []
    logs = []

    def start(command, name):
        log = (output / f"{name}.log").open("a", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
        processes.append(process)
        return process

    try:
        server = start(server_command, "server")
        wait_health(url, server)
        agent_command = [
            sys.executable,
            str(ROOT / "scripts" / "demo_agent.py"),
            "--url",
            url,
            "--transport",
            args.transport,
            "--wait-mode",
            args.wait_mode,
            "--exit-after-message",
        ]
        # Both processes run independently; the harness does not alternate their turns.
        a = start(
            agent_command
            + [
                "--agent",
                "agent_a",
                "--name",
                "Agent A",
                "--state",
                str(output / "agent_a.json"),
                "--initial-recipient",
                "agent_b",
            ],
            "agent_a",
        )
        b = start(
            agent_command
            + ["--agent", "agent_b", "--name", "Agent B", "--state", str(output / "agent_b.json")],
            "agent_b",
        )
        for process in [a, b]:
            if process.wait(timeout=40) != 0:
                raise RuntimeError("Demo agent failed; inspect agent logs")
        state_a = json.loads((output / "agent_a.json").read_text(encoding="utf-8"))
        state_b = json.loads((output / "agent_b.json").read_text(encoding="utf-8"))
        if state_a["handled"] != 1 or state_b["handled"] != 1:
            raise RuntimeError("Both independent agents must receive exactly one demo message")
        with httpx.Client(base_url=url) as client:
            history_response = client.get(f"/conversations/{state_a['conversation_id']}")
            history_response.raise_for_status()
            history = history_response.json()
            events_response = client.get("/events")
            events_response.raise_for_status()
            events = events_response.json()["events"]
        messages = history["messages"]
        assert len(messages) == 2
        assert messages[0]["sender_agent_id"] == "agent_a"
        assert messages[1]["sender_agent_id"] == "agent_b"
        assert messages[1]["reply_to_message_id"] == messages[0]["id"]
        assert all(message["status"] == "read" for message in messages)
        assert len([event for event in events if event["type"] == "message.created"]) == 2
        assert len([event for event in events if event["type"] == "message.read"]) == 2
        # Agents restart without losing their local cursor or sending another acknowledgement.
        for agent_id in ["agent_a", "agent_b"]:
            restarted_agent = start(
                agent_command
                + [
                    "--agent",
                    agent_id,
                    "--name",
                    agent_id,
                    "--state",
                    str(output / f"{agent_id}.json"),
                    "--run-seconds",
                    "1",
                ],
                f"{agent_id}-restart",
            )
            assert restarted_agent.wait(timeout=10) == 0
            new_state = json.loads((output / f"{agent_id}.json").read_text(encoding="utf-8"))
            assert new_state["message_cursor"] is not None and new_state["handled"] == 1
        # Core history also survives a full AgentPhone process restart.
        stop(server)
        restarted = start(server_command, "server-restart")
        wait_health(url, restarted)
        persisted = httpx.get(f"{url}/conversations/{state_a['conversation_id']}").json()
        persisted_events = httpx.get(f"{url}/events").json()["events"]
        assert persisted == history
        assert persisted_events[: len(events)] == events
        report = {
            "status": "passed",
            "transport": args.transport,
            "wait_mode": args.wait_mode,
            "processes": {"server": server.pid, "agent_a": a.pid, "agent_b": b.pid},
            "agent_restart_verified": True,
            "server_restart_verified": True,
            "conversation": persisted,
            "events": persisted_events,
            "demo_policy": "Deterministic transport fixture; no real LLM runtime invoked",
        }
        (output / "verification.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"PASS: independent {args.transport} agents; agent and server restarts verified")
        print(f"Evidence: {output / 'verification.json'}")
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
