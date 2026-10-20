"""An independent demo runtime. Policies live here, never inside AgentPhone Core."""

import argparse
import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from agentphone.config import Settings

SYSTEM_INSTRUCTION = (
    "You have access to AgentPhone, a shared communication device used by independent agents. "
    "You may send messages to other registered agents when their input would help your work. "
    "Check for new messages periodically. Do not send unnecessary acknowledgements. "
    "Do not continue conversations unless useful to the task."
)


class PhoneTools:
    def __init__(self, http, mcp=None):
        self.http = http
        self.mcp = mcp

    async def call(self, tool, arguments):
        if self.mcp is not None:
            result = await self.mcp.call_tool(tool, arguments)
            if result.is_error:
                raise RuntimeError(result.content)
            return result.structured_content
        if tool == "get_agents":
            response = await self.http.get("/agents")
        elif tool == "read_messages":
            parameters = {
                ("agent_id" if k == "agent" else k): v
                for k, v in arguments.items()
                if v is not None
            }
            response = await self.http.get("/messages", params=parameters)
        elif tool == "send_message":
            body = {
                "sender_agent_id": arguments["sender"],
                "recipient_agent_id": arguments["recipient"],
                **{k: v for k, v in arguments.items() if k not in ("sender", "recipient")},
            }
            response = await self.http.post("/messages", json=body)
            response.raise_for_status()
            message = response.json()
            return {
                "message_id": message["id"],
                "conversation_id": message["conversation_id"],
                "status": message["status"],
                "created_at": message["created_at"],
            }
        else:
            raise ValueError(f"Unknown demo tool: {tool}")
        response.raise_for_status()
        return response.json()


def choose_reply(message):
    """Deterministic transport fixture, replaceable by the agent runtime's own policy."""
    if message["content"].startswith("Please review "):
        return (
            "Demo review response: I received the request. "
            "A substantive review requires the function's source code."
        )
    return None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


async def run(args):
    settings = Settings.from_env()
    interval = max(args.poll_interval, settings.min_agent_poll_interval)
    state_path = Path(args.state)
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {
            "agent_id": args.agent,
            "message_cursor": None,
            "event_cursor": 0,
            "initial_sent": False,
            "handled": 0,
            "conversation_id": None,
        }
    )
    if state["agent_id"] != args.agent:
        raise ValueError("State belongs to a different agent")
    state["pid"] = os.getpid()
    deadline = time.monotonic() + args.run_seconds
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(base_url=args.url, timeout=35))
        registration = await http.post(
            "/agents/register",
            json={
                "agent_id": args.agent,
                "name": args.name,
                "metadata": {"runtime": "deterministic-demo", "pid": os.getpid()},
            },
        )
        registration.raise_for_status()
        mcp = None
        if args.transport == "mcp":
            from mcp.client import Client

            mcp = await stack.enter_async_context(Client(f"{args.url.rstrip('/')}/mcp"))
        phone = PhoneTools(http, mcp)
        print(
            json.dumps({"agent": args.agent, "pid": os.getpid(), "event": "registered"}), flush=True
        )
        while time.monotonic() < deadline:
            cycle_start = time.monotonic()
            if args.initial_recipient and not state["initial_sent"]:
                agents = (await phone.call("get_agents", {}))["agents"]
                if any(agent["id"] == args.initial_recipient for agent in agents):
                    result = await phone.call(
                        "send_message",
                        {
                            "sender": args.agent,
                            "recipient": args.initial_recipient,
                            "content": args.initial_content,
                        },
                    )
                    state.update(initial_sent=True, conversation_id=result["conversation_id"])
                    save_state(state_path, state)
            batch = await phone.call(
                "read_messages",
                {"agent": args.agent, "after_message_id": state["message_cursor"], "limit": 50},
            )
            for message in batch["messages"]:
                response = choose_reply(message)
                if response is not None:
                    await phone.call(
                        "send_message",
                        {
                            "sender": args.agent,
                            "recipient": message["sender_agent_id"],
                            "content": response,
                            "conversation_id": message["conversation_id"],
                            "reply_to_message_id": message["id"],
                        },
                    )
                state.update(
                    message_cursor=message["id"],
                    handled=state["handled"] + 1,
                    conversation_id=message["conversation_id"],
                )
                save_state(state_path, state)
                print(
                    json.dumps(
                        {
                            "agent": args.agent,
                            "event": "processed",
                            "message_id": message["id"],
                            "replied": response is not None,
                        }
                    ),
                    flush=True,
                )
            if args.exit_after_message and batch["messages"]:
                break
            # Minimum interval prevents busy polling, even if presence events arrive immediately.
            await asyncio.sleep(max(0, interval - (time.monotonic() - cycle_start)))
            if args.wait_mode == "events" and (not args.initial_recipient or state["initial_sent"]):
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    events = await http.get(
                        "/events/wait",
                        params={
                            "target_agent_id": args.agent,
                            "after_event_id": state["event_cursor"],
                            "timeout_seconds": min(remaining, 10),
                        },
                    )
                    events.raise_for_status()
                    batch_events = events.json()["events"]
                    if batch_events:
                        state["event_cursor"] = batch_events[-1]["id"]
                        save_state(state_path, state)
                        if any(event["type"] == "message.created" for event in batch_events):
                            break
                    else:
                        # Retry inbox after a timeout, including recovery after missed hints.
                        break
        save_state(state_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--state", required=True)
    parser.add_argument("--transport", choices=["mcp", "http"], default="mcp")
    parser.add_argument("--wait-mode", choices=["events", "polling"], default="events")
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--run-seconds", type=float, default=30)
    parser.add_argument("--initial-recipient")
    parser.add_argument("--initial-content", default="Please review function X.")
    parser.add_argument("--exit-after-message", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
