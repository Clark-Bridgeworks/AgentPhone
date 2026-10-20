"""Protocol fixture; never claimed to be a vendor Agent/model."""

import json
import os
import sys

mode = os.getenv("ACP_FIXTURE_MODE", "normal")
native = "fixture-native-session"
permission_prompt = None


def emit(value):
    print(json.dumps(dict(jsonrpc="2.0", **value)), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if not method:
        if "result" in request:
            assert request["result"] == {"outcome": {"outcome": "cancelled"}}
            emit(dict(id=permission_prompt, result={"stopReason": "cancelled"}))
        continue
    params = request.get("params", {})
    if method == "initialize":
        result = dict(
            protocolVersion=2 if mode == "version2" else 1,
            agentCapabilities=dict(loadSession=mode != "no_load", mcpCapabilities=dict(http=True)),
        )
    elif method == "session/new":
        assert params["mcpServers"][0]["type"] == "http"
        result = dict(sessionId=native)
    elif method == "session/load":
        assert params["sessionId"] == native
        result = {}
    elif method == "session/prompt":
        assert params["sessionId"] == native
        if mode == "permission":
            permission_prompt = request["id"]
            emit(
                dict(
                    id=900,
                    method="session/request_permission",
                    params=dict(sessionId=native, options=[]),
                )
            )
            continue
        text = params["prompt"][0]["text"]
        inbox = json.loads(text.split("AgentPhone structured inbox:\n")[-1])
        action = dict(
            action="send_message",
            recipient_id="original-a",
            content="Fixture reply",
            reply_to_message_id=inbox["message_id"],
            received_message_id=inbox["message_id"],
            received_content=inbox["content"],
        )
        emit(
            dict(
                method="session/update",
                params=dict(
                    sessionId=native,
                    update=dict(
                        sessionUpdate="agent_thought_chunk",
                        content=dict(type="text", text="PRIVATE"),
                    ),
                ),
            )
        )
        for chunk in [json.dumps(action)[:35], json.dumps(action)[35:]]:
            emit(
                dict(
                    method="session/update",
                    params=dict(
                        sessionId=native,
                        update=dict(
                            sessionUpdate="agent_message_chunk",
                            content=dict(type="text", text=chunk),
                        ),
                    ),
                )
            )
        result = dict(stopReason="end_turn")
    else:
        raise AssertionError(method)
    emit(dict(id=request["id"], result=result))
