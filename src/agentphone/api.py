import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .errors import PhoneError
from .models import Agent
from .schemas import RegisterAgent, SendMessage
from .service import PhoneService

logger = logging.getLogger("agentphone.api")


def create_app(
    settings: Settings | None = None, enable_mcp: bool = False, bridge_factory=None
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = PhoneService(Database(settings.database_path), settings)
    bridge = bridge_factory(service) if bridge_factory else None
    mcp = None
    if enable_mcp:
        from .mcp_server import create_mcp

        mcp = create_mcp(service, bridge)
        mcp_app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    @asynccontextmanager
    async def lifespan(app):
        await service.initialize()
        if bridge is not None:
            await bridge.initialize()
        try:
            if mcp is not None:
                async with mcp.session_manager.run():
                    yield
            else:
                yield
        finally:
            if bridge is not None:
                await bridge.close()

    app = FastAPI(title="AgentPhone", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.mcp = mcp
    app.state.bridge = bridge

    @app.get("/", include_in_schema=False)
    async def root():
        return {"name": "AgentPhone", "purpose": "AB Communication Bridge"}

    @app.get("/bridge")
    async def bridge_view():
        return await bridge.snapshot() if bridge else {"bridge": None, "messages": []}

    @app.post("/bridge/disconnect")
    async def disconnect_bridge(request: Request):
        if bridge is None:
            raise PhoneError("bridge_not_connected", "No A/B bridge available", 409)
        body = await request.json()
        if not isinstance(body, dict):
            raise PhoneError("invalid_input", "Disconnect request must be an object")
        return await bridge.disconnect(
            body.get("conversation_id"), body.get("preserve_context", True)
        )

    @app.post("/bridge/resume")
    async def resume_bridge(request: Request):
        if bridge is None:
            raise PhoneError("bridge_not_connected", "No A/B bridge available", 409)
        body = await request.json()
        return await bridge.resume(body.get("conversation_id"))

    @app.get("/bridge/native-sessions")
    async def native_sessions(agent_id: str, cursor: str | None = None):
        if bridge is None:
            raise PhoneError("bridge_not_connected", "No A/B bridge available", 409)
        return await bridge.native_sessions(agent_id, cursor)

    @app.post("/bridge/select")
    async def select_bridge(request: Request):
        if bridge is None:
            raise PhoneError("bridge_not_connected", "No A/B bridge available", 409)
        try:
            return await bridge.select_endpoints(await request.json())
        except RuntimeError as error:
            from .desktop.runtime import safe_error

            raise PhoneError("selection_failed", safe_error(str(error)), 409) from error

    @app.exception_handler(PhoneError)
    async def phone_error(request: Request, exc: PhoneError):
        logger.warning(
            "API error",
            extra={
                "phone_event": {"path": request.url.path, "code": exc.code, "detail": exc.detail}
            },
        )
        return JSONResponse(
            status_code=exc.status_code, content={"error": {"code": exc.code, "detail": exc.detail}}
        )

    @app.get("/health")
    async def health():
        await service.get_agents()
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/agents/register", response_model=Agent)
    async def register(body: RegisterAgent):
        return await service.register_agent(**body.model_dump())

    @app.get("/agents")
    async def agents():
        if bridge is not None:
            return await bridge.get_agents()
        return {"agents": await service.get_agents()}

    @app.post("/messages", status_code=201)
    async def send(body: SendMessage):
        if bridge is not None:
            return await bridge.send_message(
                body.sender_agent_id,
                body.recipient_agent_id,
                body.content,
                body.conversation_id,
                body.reply_to_message_id,
                body.sender_session_id,
                "http",
            )
        return await service.send_message(**body.model_dump(exclude={"sender_session_id"}))

    @app.get("/messages")
    async def read(
        agent_id: str,
        after_message_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = Query(50, ge=1, le=1000),
        mark_read: bool = True,
        sender_session_id: str | None = None,
        wait_seconds: float = Query(0, ge=0, le=30),
    ):
        if bridge is not None:
            return await bridge.read_messages(
                agent_id, after_message_id, conversation_id, limit, sender_session_id, wait_seconds
            )
        return {
            "messages": await service.read_messages(
                agent_id, after_message_id, conversation_id, limit, mark_read
            )
        }

    @app.get("/conversations")
    async def conversations(agent_a: str, agent_b: str, limit: int = Query(20, ge=1, le=1000)):
        return {"conversations": await service.list_conversations(agent_a, agent_b, limit)}

    @app.get("/conversations/{conversation_id}")
    async def conversation(
        conversation_id: str,
        limit: int = Query(100, ge=1, le=1000),
        before_message_id: str | None = None,
    ):
        return await service.get_conversation(conversation_id, limit, before_message_id)

    @app.get("/events")
    async def events(
        after_event_id: int = Query(0, ge=0),
        target_agent_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ):
        return {"events": await service.get_events(after_event_id, target_agent_id, limit)}

    @app.get("/events/wait")
    async def wait(
        after_event_id: int = Query(0, ge=0),
        target_agent_id: str | None = None,
        timeout_seconds: float = Query(30, ge=0, le=60),
        limit: int = Query(100, ge=1, le=1000),
    ):
        return {
            "events": await service.wait_for_event(
                after_event_id, target_agent_id, timeout_seconds, limit
            )
        }

    if mcp is not None:
        # Root mount is last so HTTP routes take precedence; MCP is at /mcp.
        app.mount("/", mcp_app)
    return app
