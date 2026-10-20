from fastapi.testclient import TestClient

from agentphone.api import create_app
from agentphone.config import Settings


def test_http_flow_and_persistence(tmp_path):
    settings = Settings(database_path=tmp_path / "phone.db")
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.json()["purpose"] == "AB Communication Bridge"
        assert "AgentPhone" in page.text
        assert client.get("/health").json()["status"] == "ok"
        for agent in ["agent_a", "agent_b"]:
            assert client.post(
                "/agents/register", json={"agent_id": agent, "name": agent}
            ).is_success
        request = client.post(
            "/messages",
            json={
                "sender_agent_id": "agent_a",
                "recipient_agent_id": "agent_b",
                "content": "Review X",
            },
        )
        assert request.status_code == 201
        message = request.json()
        listed = client.get("/conversations", params={"agent_a": "agent_a", "agent_b": "agent_b"})
        assert listed.json()["conversations"][0]["id"] == message["conversation_id"]
        received = client.get("/messages", params={"agent_id": "agent_b"}).json()["messages"]
        assert received[0]["id"] == message["id"]
        reply = client.post(
            "/messages",
            json={
                "sender_agent_id": "agent_b",
                "recipient_agent_id": "agent_a",
                "content": "Reviewed",
                "conversation_id": message["conversation_id"],
                "reply_to_message_id": message["id"],
            },
        ).json()
        assert (
            client.get("/messages", params={"agent_id": "agent_a"}).json()["messages"][0]["id"]
            == reply["id"]
        )
        events = client.get("/events").json()["events"]
        assert len([e for e in events if e["type"] == "message.read"]) == 2
        assert client.get(
            "/events/wait", params={"after_event_id": events[-1]["id"], "timeout_seconds": 0}
        ).json() == {"events": []}
    with TestClient(create_app(settings)) as restarted:
        history = restarted.get(f"/conversations/{message['conversation_id']}").json()
        assert len(history["messages"]) == 2
        assert restarted.get("/events").json()["events"] == events


def test_http_errors(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path / "phone.db"))) as client:
        assert client.get("/messages", params={"agent_id": "missing"}).status_code == 404
        assert client.get("/events", params={"after_event_id": -1}).status_code == 422
        assert client.get("/events/wait", params={"timeout_seconds": 61}).status_code == 422
        assert (
            client.get("/messages", params={"agent_id": "missing", "limit": 0}).status_code == 422
        )
        assert (
            client.post("/agents/register", json={"agent_id": "a", "name": " "}).status_code == 400
        )


def test_conversation_list_filters_pair_and_orders_activity(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path / "phone.db"))) as client:
        for agent in ["a", "b", "c"]:
            client.post("/agents/register", json={"agent_id": agent, "name": agent})

        def send(sender, recipient, conversation_id=None):
            body = {"sender_agent_id": sender, "recipient_agent_id": recipient, "content": "Hi"}
            if conversation_id:
                body["conversation_id"] = conversation_id
            response = client.post("/messages", json=body)
            assert response.status_code == 201
            return response.json()["conversation_id"]

        first = send("a", "b")
        second = send("b", "a")
        send("a", "c")
        mixed = send("a", "b")
        send("c", "a", mixed)
        send("b", "a", first)
        params = {"agent_a": "a", "agent_b": "b"}
        result = client.get("/conversations", params=params).json()["conversations"]
        assert [conversation["id"] for conversation in result] == [first, second]
        result = client.get("/conversations", params={**params, "limit": 1}).json()
        assert [conversation["id"] for conversation in result["conversations"]] == [first]
