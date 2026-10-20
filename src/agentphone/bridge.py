"""Two endpoints and a delivery queue. No work planning or model-created initiator."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from .errors import PhoneError
from .models import Conversation
from .repository import encode
from .service import identifier, timestamp


class CommunicationBridge:
    def __init__(self, service, directory, installations, adapter_factory=None):
        from .desktop.adapters import ADAPTERS, create_adapter

        self.service = service
        self.directory = Path(directory).resolve()
        self.installations = installations
        self.adapter_factory = adapter_factory or create_adapter
        self.runtime_keys = set(ADAPTERS)
        self.link = None
        self.adapter = None
        self.lock = asyncio.Lock()
        self.queue = asyncio.Queue()
        self.worker = None
        self.closed = False
        self.released_connections = set()
        self.selection_path = self.directory / "bridge-selection.json"
        self.selection = (
            json.loads(self.selection_path.read_text(encoding="utf-8"))
            if self.selection_path.is_file()
            else None
        )

    async def disconnect(self, conversation_id=None, preserve_context=True):
        """Release an idle pair, without closing the local server or deleting its records."""
        async with self.lock:
            if self.closed:
                raise PhoneError("bridge_closed", "Communication bridge has stopped", 409)
            if conversation_id and (not self.link or conversation_id != self.link["id"]):
                raise PhoneError("session_mismatch", "Connection has already changed", 409)
            await self._disconnect_locked(release=not preserve_context)
            return await self.snapshot()

    async def _disconnect_locked(self, release=True):
        if self.worker and not self.worker.done():
            raise PhoneError("bridge_busy", "消息正在收发，请等当前投递结束后再断开。", 409)
        if not self.link:
            return
        previous = self.link["id"]
        if self.adapter:
            await asyncio.to_thread(self.adapter.stop_session)
        await self._state("RELEASED" if release else "DISCONNECTED")
        self.adapter = self.worker = None
        self.queue = asyncio.Queue()
        if not release:
            self.link = dict(self.link)
            return

        def expire(repo, _):
            repo.connection.execute(
                "UPDATE bridge_messages SET status='FAILED',error=?,updated_at=? "
                "WHERE status='PENDING' AND message_id IN "
                "(SELECT id FROM messages WHERE conversation_id=?)",
                ("连接已主动断开，未确认送达。", timestamp(), previous),
            )

        await self.service._write(expire)
        self.released_connections.add(previous)
        self.link = self.adapter = self.worker = None
        self.queue = asyncio.Queue()

    async def get_agents(self):
        from .desktop.adapters import session_adapter_available

        detected = self.installations()
        agents = [
            dict(
                agent.to_dict(),
                can_contact=agent.id in self.runtime_keys and session_adapter_available(agent),
                contact_error=(
                    "缺少原桌面对话接口，暂时禁止连接。"
                    if agent.id in self.runtime_keys
                    and agent.callable
                    and not session_adapter_available(agent)
                    else ""
                ),
            )
            for agent in detected.values()
        ]
        if self.link:
            current = next((agent for agent in agents if agent["id"] == self.link["agent_a"]), None)
            if current is None:
                current = {"id": self.link["agent_a"], "name": self.link["agent_a"]}
                agents.append(current)
            current.update(can_contact=True, native_session_id=self.link["session_a"])
        return {"agents": agents, "selection": self.selection, "connection": self.link}

    async def initialize(self):
        # This process cannot claim ownership of the prior native processes.
        def recover(repo, _):
            now = timestamp()
            repo.connection.execute(
                "UPDATE bridge_links SET status='DISCONNECTED', updated_at=? "
                "WHERE status NOT IN ('FAILED','RELEASED')",
                (now,),
            )
            repo.connection.execute(
                "UPDATE bridge_messages SET status='FAILED',error=?,updated_at=? "
                "WHERE status='PENDING' AND message_id IN "
                "(SELECT m.id FROM messages m JOIN bridge_links l ON l.id=m.conversation_id "
                "WHERE m.recipient_agent_id=l.agent_b)",
                ("通信桥进程已结束，消息未确认送达。", now),
            )

        await self.service._write(recover)

        def last_connection(repo):
            sql, params = "SELECT * FROM bridge_links WHERE status!='RELEASED'", []
            if self.selection:
                a, b = self.selection["a"], self.selection["b"]
                # A selected new B has no native identity until its first delivery.
                # Never substitute a previous B while this selection is pending.
                if not b.get("native_session_id"):
                    return None
                sql += " AND agent_a=? AND session_a=? AND agent_b=?"
                params += [a["agent_id"], a["native_session_id"], b["agent_id"]]
                if b.get("native_session_id"):
                    sql += " AND session_b=?"
                    params.append(b["native_session_id"])
            row = repo.connection.execute(sql + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
            return dict(row) if row else None

        self.link = await self.service._read(last_connection)

    def _selection_matches(self, link):
        if not self.selection:
            return True
        a, b = self.selection["a"], self.selection["b"]
        return (
            (a["agent_id"], a["native_session_id"], b["agent_id"])
            == (link["agent_a"], link["session_a"], link["agent_b"])
            and bool(b.get("native_session_id"))
            and b["native_session_id"] == link["session_b"]
        )

    async def _reset_observer(self):
        if self.worker and not self.worker.done():
            raise PhoneError("bridge_busy", "消息正在收发，请稍后续接。", 409)
        if self.adapter:
            await asyncio.to_thread(self.adapter.stop_session)
        self.adapter = self.worker = None
        self.queue = asyncio.Queue()
        await self._state("CONNECTING")

    async def _ensure_adapter(self):
        if self.adapter is not None:
            return
        from .desktop.adapters import session_adapter_available

        target = self.installations().get(self.link["agent_b"])
        if not target or not session_adapter_available(target):
            raise PhoneError("adapter_unavailable", "目标缺少原桌面对话接口。", 409)
        adapter = self.adapter_factory(
            target.id, self, self.directory / "sessions" / self.link["id"], target
        )
        adapter.on_native_session = self._native_connected
        adapter.read_native_receipt = self.native_receipt
        self.adapter = adapter
        try:
            await asyncio.to_thread(adapter.start_session, self.link)
        except Exception:
            await asyncio.to_thread(adapter.stop_session)
            self.adapter = None
            raise

    async def resume(self, conversation_id):
        async with self.lock:
            if self.closed:
                raise PhoneError("bridge_closed", "通信桥已关闭", 409)
            if not self.link or self.link["id"] != conversation_id:
                raise PhoneError("session_mismatch", "请先选择原来的两边对话。", 409)
            await self._reset_observer()
            try:
                await self._ensure_adapter()
                await self._state("CONNECTED")
            except Exception as error:
                from .desktop.runtime import safe_error

                detail = safe_error(str(error))
                await self._state("FAILED", detail)
                raise PhoneError("resume_failed", detail, 409) from error
            return await self.snapshot()

    def native_catalog(self):
        from .desktop.native_sessions import NativeSessions

        return NativeSessions(self, self.directory, self.installations)

    async def native_sessions(self, agent_id, cursor=None):
        try:
            return await asyncio.to_thread(self.native_catalog().list, agent_id, cursor)
        except Exception as error:
            from .desktop.runtime import safe_error

            raise PhoneError("native_sessions_unavailable", safe_error(str(error)), 409) from error

    async def select_endpoints(self, selection):
        if not isinstance(selection, dict) or set(selection) != {"a", "b"}:
            raise PhoneError("invalid_input", "请选择 A、B 两边的对话。")
        async with self.lock:
            if self.worker and not self.worker.done():
                raise PhoneError("bridge_busy", "消息正在收发，暂不能更换对话。", 409)
            catalog = self.native_catalog()
            endpoints = {}
            for side in ("a", "b"):
                row = selection[side]
                if not isinstance(row, dict) or row.get("mode") not in {"existing", "new"}:
                    raise PhoneError("invalid_input", "请选择已有对话或新对话。")
                catalog.target(row.get("agent_id"))
                selected = {"agent_id": row["agent_id"], "mode": row["mode"]}
                if row["mode"] == "existing":
                    identifier(row.get("native_session_id"), "native_session_id")
                    selected.update(
                        await asyncio.to_thread(
                            catalog.inspect, row["agent_id"], row["native_session_id"]
                        )
                    )
                elif side == "a":
                    if not row.get("cwd") or not Path(row["cwd"]).is_dir():
                        raise PhoneError("invalid_input", "请为 A 新对话选择项目文件夹。")
                    selected["cwd"] = row["cwd"]
                endpoints[side] = selected
            a, b = endpoints["a"], endpoints["b"]
            if a["agent_id"] == b["agent_id"]:
                raise PhoneError("invalid_route", "本版 A、B 需选择不同 Agent。")
            if a["mode"] == "new":
                a.update(await asyncio.to_thread(catalog.create_a, a["agent_id"], a["cwd"]))

            def previous(repo):
                row = repo.connection.execute(
                    "SELECT * FROM bridge_links WHERE agent_a=? AND session_a=? AND agent_b=? "
                    "AND session_b=? ORDER BY rowid DESC LIMIT 1",
                    (
                        a["agent_id"],
                        a["native_session_id"],
                        b["agent_id"],
                        b.get("native_session_id"),
                    ),
                ).fetchone()
                return dict(row) if row else None

            old = await self.service._read(previous) if b["mode"] == "existing" else None
            if self.link:
                await self._disconnect_locked(release=not (old and old["id"] == self.link["id"]))
            self.selection = endpoints
            temporary = self.selection_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(endpoints, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.selection_path)
            if old:
                self.link = old
                await self._state("CONNECTING")
                try:
                    await self._ensure_adapter()
                    await self._state("CONNECTED")
                except Exception as error:
                    from .desktop.runtime import safe_error

                    detail = safe_error(str(error))
                    await self._state("FAILED", detail)
                    raise PhoneError("resume_failed", detail, 409) from error
            return {"selection": endpoints, "bridge": self.link, "busy": False}

    async def send_and_receive(self, *args, wait_seconds=25, **kwargs):
        """Return the peer's real inbox content in the original caller's tool result."""
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= 30
        ):
            raise PhoneError("invalid_input", "wait_seconds must be between 0 and 30")
        sender, recipient = args[:2]
        cursor = None
        if self.link and not kwargs.get("new_connection"):

            def last_inbox(repo):
                row = repo.connection.execute(
                    "SELECT id FROM messages WHERE conversation_id=? AND recipient_agent_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (self.link["id"], sender),
                ).fetchone()
                return row["id"] if row else None

            cursor = await self.service._read(last_inbox)
        sent = await self.send_message(*args, **kwargs)
        active = self.link
        if sender != active["agent_a"] or recipient != active["agent_b"]:
            # B sends immediately; blocking it would prevent its native turn from finishing.
            return sent
        inbox = await self.read_messages(
            sender, cursor, active["id"], 50, active["session_a"], wait_seconds
        )
        sent = await self.message(sent["id"])
        sent["inbox"] = inbox["messages"]
        sent["awaiting_reply"] = not bool(inbox["messages"]) and sent["status"] != "FAILED"
        sent["next_action"] = (
            "对方尚未回信。继续在本对话调用 read_messages，携带同一连接和真实会话 ID，"
            "wait_seconds=30；收到回信后由双方决定下一步。待接收不代表任务完成。"
            if sent["awaiting_reply"]
            else "消息收发结果见 status、receipt 和 inbox。"
        )
        return sent

    async def _link_for_send(self, sender, recipient, session_id, conversation_id):
        identifier(sender, "sender")
        identifier(recipient, "recipient")
        identifier(session_id, "sender_session_id")
        if sender == recipient:
            raise PhoneError("invalid_route", "A and B must be different agents")
        if self.link:
            link = self.link
            if conversation_id is not None and conversation_id != link["id"]:
                raise PhoneError("bridge_busy", "This bridge is already bound to two sessions", 409)
            if {sender, recipient} != {link["agent_a"], link["agent_b"]}:
                raise PhoneError("bridge_busy", "A bridge connects exactly two agents", 409)
            expected = link["session_a"] if sender == link["agent_a"] else link["binding_b"]
            native = link["session_a"] if sender == link["agent_a"] else link["session_b"]
            if session_id not in {expected, native}:
                raise PhoneError(
                    "session_mismatch", "Message belongs to a different native session"
                )
            if link["status"] in {"FAILED", "DISCONNECTED"}:
                await self._reset_observer()
                if sender == link["agent_b"]:
                    await self._state("CONNECTED")
            return link
        if conversation_id is not None:

            def previous(repo):
                row = repo.connection.execute(
                    "SELECT * FROM bridge_links WHERE id=?", (conversation_id,)
                ).fetchone()
                return dict(row) if row else None

            link = await self.service._read(previous)
            if link and not self._selection_matches(link):
                raise PhoneError(
                    "selection_mismatch", "旧连接不属于当前选择，请使用所选原对话。", 409
                )
            if (
                not link
                or {sender, recipient} != {link["agent_a"], link["agent_b"]}
                or session_id
                not in (
                    {link["session_a"]}
                    if sender == link["agent_a"]
                    else {link["binding_b"], link["session_b"]}
                )
            ):
                raise PhoneError(
                    "session_mismatch", "Only the original initiator can restore this bridge"
                )
            self.link = link
            await self._reset_observer()
            return link
        selected_b = None
        if self.selection:
            a, b = self.selection["a"], self.selection["b"]
            if (sender, session_id, recipient) != (
                a["agent_id"],
                a["native_session_id"],
                b["agent_id"],
            ):
                raise PhoneError(
                    "selection_mismatch", "请从桥中选定的 A 原对话发起，或重新选择两边对话。", 409
                )
            selected_b = b.get("native_session_id")
        detected = self.installations()
        target = detected.get(recipient)
        from .desktop.adapters import session_adapter_available

        if (
            recipient not in self.runtime_keys
            or target is None
            or not session_adapter_available(target)
        ):
            raise PhoneError(
                "adapter_unavailable",
                "目标 Agent 未安装、未适配，或缺少原桌面对话接口；暂时禁止连接。",
                409,
            )
        for agent_id in [sender, recipient]:
            await self.service.register_agent(
                agent_id, detected[agent_id].name if agent_id in detected else agent_id
            )
        now = timestamp()
        link = dict(
            id=str(uuid4()),
            agent_a=sender,
            session_a=session_id,
            agent_b=recipient,
            session_b=selected_b,
            binding_b=str(uuid4()),
            status="CONNECTING",
            error=None,
            created_at=now,
            updated_at=now,
        )

        def insert(repo, _):
            repo.insert_conversation(Conversation(id=link["id"], created_at=now, updated_at=now))
            repo.connection.execute(
                "INSERT INTO bridge_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(link.values()),
            )

        await self.service._write(insert)
        self.link = link
        return link

    async def send_message(
        self,
        sender,
        recipient,
        content,
        conversation_id=None,
        reply_to_message_id=None,
        sender_session_id=None,
        transport="mcp",
        new_connection=False,
    ):
        if self.closed:
            raise PhoneError("bridge_closed", "Communication bridge has stopped", 409)
        # Validate before creating a link or waking a runtime.
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self.service.settings.max_message_length
        ):
            raise PhoneError("invalid_input", "Message is empty or too long")
        if self.link is None and conversation_id is None and reply_to_message_id is not None:
            raise PhoneError("invalid_reply", "First contact cannot reply to another bridge")
        async with self.lock:
            if new_connection:
                if conversation_id is not None or reply_to_message_id is not None:
                    raise PhoneError("invalid_input", "切换连接时不能携带旧连接或回复 ID。")
                if self.selection:
                    a, b = self.selection["a"], self.selection["b"]
                    if (sender, sender_session_id, recipient) != (
                        a["agent_id"], a["native_session_id"], b["agent_id"]
                    ):
                        raise PhoneError(
                            "selection_mismatch", "更换目标前请修改桥中的对话选择。", 409
                        )
                if self.link and (sender, sender_session_id) != (
                    self.link["agent_a"],
                    self.link["session_a"],
                ):
                    raise PhoneError("session_mismatch", "只有原发起会话可以主动切换连接。")
                from .desktop.adapters import session_adapter_available

                target = self.installations().get(recipient)
                if (
                    recipient not in self.runtime_keys
                    or not target
                    or not session_adapter_available(target)
                ):
                    raise PhoneError(
                        "adapter_unavailable", "目标 Agent 未安装或缺少原桌面对话接口。", 409
                    )
                identifier(sender_session_id, "sender_session_id")
                if sender == recipient:
                    raise PhoneError("invalid_route", "A and B must be different agents")
                await self._disconnect_locked()
            link = await self._link_for_send(sender, recipient, sender_session_id, conversation_id)
            native_sender = link["session_a"] if sender == link["agent_a"] else link["session_b"]
            if not native_sender:
                raise PhoneError("session_not_ready", "Sender has no native session")
            destination = link["session_a"] if recipient == link["agent_a"] else link["session_b"]
            message = await self.service.send_message(
                sender,
                recipient,
                content,
                link["id"],
                reply_to_message_id,
                metadata={"transport": transport, "sender_session_id": native_sender},
                bridge_delivery={
                    "sender_session_id": native_sender,
                    "recipient_session_id": destination,
                },
            )
            if recipient == link["agent_b"]:
                self.queue.put_nowait(message.id)
                if self.worker is None or self.worker.done():
                    self.worker = asyncio.create_task(self._deliver())
            # A is the caller's existing session. Never start or prompt a replacement A.
            return await self.message(message.id)

    async def message(self, message_id):
        def read(repo):
            row = repo.connection.execute(
                "SELECT m.*,b.sender_session_id,b.recipient_session_id,b.status AS delivery_status,"
                "b.receipt,b.error,b.updated_at FROM messages m JOIN bridge_messages b "
                "ON m.id=b.message_id WHERE m.id=?",
                (message_id,),
            ).fetchone()
            return self._view(row)

        return await self.service._read(read)

    @staticmethod
    def _view(row):
        result = dict(row)
        result["message_id"] = result["id"]
        result["status"] = result.pop("delivery_status")
        for key in ["receipt", "metadata"]:
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def native_receipt(self, message_id, native_id):
        with self.service.database.transaction() as connection:
            row = connection.execute(
                "SELECT receipt FROM bridge_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            receipt = json.loads(row["receipt"]) if row and row["receipt"] else None
            return receipt if receipt and receipt["native_session_id"] == native_id else None

    async def _delivery(self, message_id, status, native_id, receipt=None, error=None):
        def update(repo, _):
            row = repo.connection.execute(
                "SELECT status FROM bridge_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row["status"] == "FAILED":
                raise PhoneError(
                    "delivery_failed", "Failed delivery cannot be reported as successful"
                )
            now = timestamp()
            repo.connection.execute(
                "UPDATE bridge_messages SET status=?,recipient_session_id=?,"
                "receipt=COALESCE(?,receipt),error=?,updated_at=? "
                "WHERE message_id=?",
                (status, native_id, encode(receipt), error, now, message_id),
            )

        await self.service._write(update)

    def _native_connected(self, native_id):
        if self.link["session_b"] and self.link["session_b"] != native_id:
            raise RuntimeError("目标 Agent 更换了原生会话，禁止冒充原会话。")
        self.link["session_b"] = native_id
        if self.selection:
            a, b = self.selection["a"], self.selection["b"]
            if (a["agent_id"], a["native_session_id"], b["agent_id"]) == (
                self.link["agent_a"],
                self.link["session_a"],
                self.link["agent_b"],
            ):
                b.update(mode="existing", native_session_id=native_id)
                temporary = self.selection_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(self.selection, ensure_ascii=False), encoding="utf-8"
                )
                temporary.replace(self.selection_path)
        with self.service.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE bridge_links SET session_b=?, updated_at=? WHERE id=?",
                (native_id, timestamp(), self.link["id"]),
            )

    async def _state(self, status, error=None):
        self.link.update(status=status, error=error, updated_at=timestamp())

        def write(repo, _):
            repo.connection.execute(
                "UPDATE bridge_links SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, self.link["updated_at"], self.link["id"]),
            )

        await self.service._write(write)

    async def _deliver(self):
        from .desktop.runtime import safe_error

        current_id = None
        try:
            while not self.queue.empty() and not self.closed:
                current_id = self.queue.get_nowait()
                incoming = await self.message(current_id)
                if self.adapter is None:
                    await self._ensure_adapter()
                envelope = dict(
                    message_id=incoming["id"],
                    conversation_id=self.link["id"],
                    sender_id=incoming["sender_agent_id"],
                    recipient_id=incoming["recipient_agent_id"],
                    content=incoming["content"],
                    timestamp=incoming["created_at"],
                )
                action = await asyncio.to_thread(self.adapter.receive_message, envelope)
                if (
                    action["received_message_id"] != current_id
                    or action["received_content"] != incoming["content"]
                ):
                    raise RuntimeError("目标会话没有返回匹配原消息的接收回执。")
                native_id = self.adapter.native_session_id
                if not native_id or native_id != self.link["session_b"]:
                    raise RuntimeError("目标原生会话未连接或已被替换。")
                receipt = dict(
                    native_session_id=native_id,
                    received_message_id=current_id,
                    received_content=incoming["content"],
                    source="native_adapter_receipt",
                )
                await self._delivery(current_id, "DELIVERED", native_id, receipt)
                if action["action"] == "send_message":
                    if action["recipient_id"] != self.link["agent_a"]:
                        raise RuntimeError("目标 Agent 返回了不属于当前 A/B 桥的收件人。")
                    await self.send_message(
                        self.link["agent_b"],
                        self.link["agent_a"],
                        action["content"],
                        self.link["id"],
                        action["reply_to_message_id"],
                        self.link["binding_b"],
                        "structured_adapter_action",
                    )
                # wait is a transport choice, not a fabricated reply or work-completion judgment.
                await self._delivery(current_id, "PROCESSED", native_id, receipt)
                await self._state("CONNECTED")
                current_id = None
        except Exception as exc:
            detail = safe_error(str(exc)) or "通信中断"
            if current_id:
                await self._delivery(current_id, "FAILED", self.link["session_b"], error=detail)
            while not self.queue.empty():
                await self._delivery(
                    self.queue.get_nowait(), "FAILED", self.link["session_b"], error=detail
                )
            await self._state("FAILED", detail)
            if self.adapter:
                await asyncio.to_thread(self.adapter.stop_session)

    async def read_messages(
        self,
        agent,
        after_message_id=None,
        conversation_id=None,
        limit=50,
        sender_session_id=None,
        wait_seconds=0,
    ):
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= 30
        ):
            raise PhoneError("invalid_input", "wait_seconds must be between 0 and 30")
        if not self.link:
            # Reading an already stored reply never creates a replacement native agent.
            identifier(sender_session_id, "sender_session_id")

            def saved_link(repo):
                sql = (
                    "SELECT * FROM bridge_links WHERE ((agent_a=? AND session_a=?) "
                    "OR (agent_b=? AND (session_b=? OR binding_b=?)))"
                )
                params = [agent, sender_session_id, agent, sender_session_id, sender_session_id]
                if conversation_id:
                    sql += " AND id=?"
                    params.append(conversation_id)
                sql += " AND status!='RELEASED' ORDER BY rowid DESC LIMIT 1"
                row = repo.connection.execute(sql, params).fetchone()
                return dict(row) if row else None

            async with self.lock:
                if not self.link:
                    restored = await self.service._read(saved_link)
                    if not restored:
                        raise PhoneError(
                            "bridge_not_connected", "No inbox for this native session", 409
                        )
                    if not self._selection_matches(restored):
                        raise PhoneError(
                            "selection_mismatch", "旧收件箱不属于当前选择，请使用所选原对话。", 409
                        )
                    self.link = restored
        active_link = self.link
        identifier(sender_session_id, "sender_session_id")
        if agent not in {self.link["agent_a"], self.link["agent_b"]}:
            raise PhoneError("invalid_route", "This agent is not an A/B endpoint")
        if conversation_id is not None and conversation_id != self.link["id"]:
            raise PhoneError("session_mismatch", "Inbox belongs to a different bridge")
        expected = (
            {self.link["session_a"]}
            if agent == self.link["agent_a"]
            else {self.link["binding_b"], self.link["session_b"]}
        )
        if sender_session_id not in expected:
            raise PhoneError("session_mismatch", "Inbox belongs to a different native session")

        async def inbox():
            if self.link is not active_link:
                raise PhoneError("bridge_released", "等待收信期间连接已切换，请重新联系。", 409)
            return await self.service.read_messages(
                agent, after_message_id, active_link["id"], limit
            )

        messages = await inbox()
        if not messages and wait_seconds:
            # A long poll only waits for actual messages; it never generates or schedules a reply.
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while not messages and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.15)
                messages = await inbox()
        result = []
        for message in messages:
            delivered = await self.message(message.id)
            if delivered["status"] == "FAILED":
                continue
            native_id = (
                active_link["session_a"]
                if agent == active_link["agent_a"]
                else active_link["session_b"]
            )
            if delivered["status"] == "PENDING":
                await self._delivery(
                    message.id,
                    "DELIVERED",
                    native_id,
                    dict(
                        native_session_id=native_id,
                        received_message_id=message.id,
                        received_content=message.content,
                        source="agent_read_messages",
                    ),
                )
            result.append(await self.message(message.id))
        return {"messages": result, "conversation_id": active_link["id"]}

    async def snapshot(self):
        if not self.link:
            return {"bridge": None, "messages": [], "busy": False, "selection": self.selection}
        current = dict(self.link)
        busy = bool(self.worker and not self.worker.done())

        def read(repo):
            rows = repo.connection.execute(
                "SELECT m.*,b.sender_session_id,b.recipient_session_id,b.status AS delivery_status,"
                "b.receipt,b.error,b.updated_at FROM messages m JOIN bridge_messages b "
                "ON m.id=b.message_id WHERE m.conversation_id=? ORDER BY m.rowid",
                (current["id"],),
            ).fetchall()
            return {
                "bridge": current,
                "messages": [self._view(row) for row in rows],
                "busy": busy,
                "selection": self.selection,
            }

        return await self.service._read(read)

    async def close(self):
        self.closed = True
        if self.adapter:
            await asyncio.to_thread(self.adapter.stop_session)
        if self.worker and not self.worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                self.worker.cancel()
        if self.link and self.link["status"] != "FAILED":
            await self._state("DISCONNECTED")

        def fail_unreceived(repo, _):
            repo.connection.execute(
                "UPDATE bridge_messages SET status='FAILED',error=?,updated_at=? "
                "WHERE status='PENDING' AND message_id IN "
                "(SELECT m.id FROM messages m JOIN bridge_links l ON l.id=m.conversation_id "
                "WHERE m.recipient_agent_id=l.agent_b)",
                ("通信桥已关闭，消息未确认送达。", timestamp()),
            )

        await self.service._write(fail_unreceived)
