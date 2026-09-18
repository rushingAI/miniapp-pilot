"""Shared ClaudeSDKClient lifecycle for long-lived parent Agent conversations."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import ResultMessage, SystemMessage


USER_STOP_GRACE_TIMEOUT_S = 5.0
RESULT_DRAIN_TIMEOUT_S = 20.0
TRANSPORT_DISCONNECT_TIMEOUT_S = 1.0


def sdk_session_id(message: Any) -> str:
    """Return a trusted SDK session id from any typed SDK message that carries one."""
    direct = str(getattr(message, "session_id", "") or "").strip()
    if direct:
        return direct
    if isinstance(message, SystemMessage):
        data = getattr(message, "data", None)
        if isinstance(data, dict):
            return str(data.get("session_id") or "").strip()
    return ""


def sdk_session_fingerprint(session_id: str) -> str:
    if not session_id:
        return ""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


class SDKConversationLifecycle:
    """Own transport generations and per-turn Result boundaries for one parent Agent."""

    def __init__(self, owner: Any, emit: Callable, options_factory: Callable,
                 client_factory: Callable):
        self.owner = owner
        self.emit = emit
        self.options_factory = options_factory
        self.client_factory = client_factory
        self.generation = 0
        self._bound_generation = -1
        self._transport_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._ensure_owner_state()

    def _ensure_owner_state(self) -> None:
        owner = self.owner
        if not hasattr(owner, "client"):
            owner.client = None
        if not hasattr(owner, "session_id"):
            owner.session_id = ""
        if not hasattr(owner, "_send_lock"):
            owner._send_lock = asyncio.Lock()
        if not hasattr(owner, "_submitted_queries"):
            owner._submitted_queries = 0
        if not hasattr(owner, "_received_results"):
            owner._received_results = 0
        if not hasattr(owner, "_turn_drained"):
            owner._turn_drained = asyncio.Event()
        if not hasattr(owner, "_transport_needs_reconnect"):
            owner._transport_needs_reconnect = False
        if not hasattr(owner, "_context_loss_pending"):
            owner._context_loss_pending = False

    async def _emit_transport(self, action: str, *, reason: str = "",
                              context_preserved: bool | None = None,
                              **details: Any) -> None:
        event = {
            "type": "sdk_transport",
            "action": action,
            "generation": self.generation,
            "reason": reason,
            "session_fingerprint": sdk_session_fingerprint(self.owner.session_id),
        }
        if context_preserved is not None:
            event["context_preserved"] = context_preserved
        event.update(details)
        await self.emit(event)

    def _drain_details(self, started_at: float) -> dict[str, int | float]:
        return {
            "drain_elapsed_ms": round(max(0.0, time.perf_counter() - started_at) * 1000, 3),
            "submitted_queries": int(self.owner._submitted_queries),
            "received_results": int(self.owner._received_results),
        }

    async def _notify_context_lost(self) -> None:
        self.owner._context_loss_pending = False
        await self._emit_transport(
            "context_lost", reason="missing_session_id", context_preserved=False,
        )
        await self.emit({
            "type": "chat_delta", "role": "system",
            "text": "⚠ 模型会话未能恢复，已使用新的模型上下文；父 Run、Suite 和设备现场仍保留。",
        })

    def begin_turn(self) -> None:
        self._ensure_owner_state()
        self.owner._submitted_queries = 0
        self.owner._received_results = 0
        self.owner._turn_drained = asyncio.Event()
        self.owner._turn_drained.set()

    def query_submitted(self) -> None:
        if self.owner._received_results >= self.owner._submitted_queries:
            self.owner._turn_drained.clear()
        self.owner._submitted_queries += 1

    def result_received(self) -> bool:
        self.owner._received_results += 1
        drained = self.owner._received_results >= self.owner._submitted_queries
        if drained:
            self.owner._turn_drained.set()
        return drained

    @property
    def turn_drained(self) -> bool:
        return bool(self.owner._turn_drained.is_set())

    async def bind_message(self, message: Any) -> str:
        session_id = sdk_session_id(message)
        if not session_id:
            return ""
        changed = session_id != self.owner.session_id
        self.owner.session_id = session_id
        if changed or self._bound_generation != self.generation:
            self._bound_generation = self.generation
            await self._emit_transport(
                "session_bound", context_preserved=True,
            )
        return session_id

    async def connect(self, *, resume: str | None = None, reason: str = "initial") -> None:
        async with self._transport_lock:
            options = self.options_factory(resume=resume)
            client = self.client_factory(options)
            await client.connect()
            self.owner.options = options
            self.owner.client = client
            self.owner._transport_needs_reconnect = False
            self.generation += 1
            self._bound_generation = -1
            await self._emit_transport(
                "resumed" if resume else "connected",
                reason=reason,
                context_preserved=bool(resume) or reason == "initial",
            )

    async def _disconnect_client(self, client: Any) -> None:
        task = asyncio.create_task(client.disconnect())
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=TRANSPORT_DISCONNECT_TIMEOUT_S,
            )
            if not done:
                task.cancel()
                return
            task.result()
        except (asyncio.CancelledError, Exception):
            task.cancel()

    def _schedule_disconnect(self, client: Any) -> None:
        task = asyncio.create_task(self._disconnect_client(client))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def disconnect(self, *, reason: str = "closed") -> None:
        async with self._transport_lock:
            client = self.owner.client
            self.owner.client = None
            if client:
                await self._disconnect_client(client)
            await self._emit_transport(
                "disconnected", reason=reason,
                context_preserved=bool(self.owner.session_id),
            )

    async def reconnect(self, *, reason: str, require_resume: bool = False) -> None:
        resume = str(self.owner.session_id or "") or None
        if require_resume and not resume:
            raise RuntimeError("当前 Claude SDK Session 尚未建立，不能保留上下文重连")
        context_lost = bool(self.owner._context_loss_pending and not resume)
        await self.disconnect(reason=reason)
        await self.connect(resume=resume, reason=reason)
        if context_lost:
            await self._notify_context_lost()

    async def ensure_ready(self) -> None:
        self._ensure_owner_state()
        if self.owner.client is not None and not self.owner._transport_needs_reconnect:
            return
        resume = str(self.owner.session_id or "") or None
        context_lost = bool(self.owner._context_loss_pending and not resume)
        await self.connect(resume=resume, reason="stop_transport_retired")
        if context_lost:
            await self._notify_context_lost()

    async def retire(self, client: Any, *, reason: str, started_at: float) -> bool:
        """Logically detach a transport before best-effort physical cleanup."""
        async with self._transport_lock:
            if self.owner.client is not client:
                return False
            self.owner.client = None
            self.owner._transport_needs_reconnect = True
            self.owner._context_loss_pending = not bool(self.owner.session_id)
            self.generation += 1
            self._bound_generation = -1
            await self._emit_transport(
                "transport_retired", reason=reason,
                context_preserved=bool(self.owner.session_id),
                **self._drain_details(started_at),
            )
        self._schedule_disconnect(client)
        return True

    def is_current(self, generation: int, client: Any) -> bool:
        return self.generation == generation and self.owner.client is client

    async def interrupt_for_stop(self, timeout_s: float = USER_STOP_GRACE_TIMEOUT_S) -> bool:
        """Interrupt one turn within a fixed user-visible grace window."""
        self._ensure_owner_state()
        started_at = time.perf_counter()
        client = self.owner.client
        if not client:
            return True
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout_s)
        interrupt_task = asyncio.create_task(client.interrupt())
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(asyncio.shield(interrupt_task), timeout=remaining)
        except (asyncio.TimeoutError, Exception):
            interrupt_task.cancel()
            await self.retire(client, reason="stop_grace_timeout", started_at=started_at)
            return False
        if self.turn_drained:
            await self._emit_transport(
                "stop_completed", reason="terminal_result_received",
                context_preserved=True,
                **self._drain_details(started_at),
            )
            return True
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(
                self.owner._turn_drained.wait(), timeout=remaining,
            )
            await self._emit_transport(
                "stop_completed", reason="terminal_result_received",
                context_preserved=True,
                **self._drain_details(started_at),
            )
            return True
        except asyncio.TimeoutError:
            await self.retire(client, reason="stop_grace_timeout", started_at=started_at)
            return False

    async def drain_remaining_results(self, messages=None,
                                      timeout_s: float = RESULT_DRAIN_TIMEOUT_S) -> bool:
        """Silently consume only bookkeeping after a stopped turn leaves its main loop."""
        if self.turn_drained:
            return True
        client = self.owner.client
        if not client:
            return False
        started_at = time.perf_counter()

        async def consume() -> None:
            stream = messages or (
                client.receive_messages()
                if hasattr(client, "receive_messages") else client.receive_response()
            )
            async for message in stream:
                await self.bind_message(message)
                if isinstance(message, ResultMessage) and self.result_received():
                    return

        try:
            await asyncio.wait_for(consume(), timeout=max(0.0, timeout_s))
            if self.turn_drained:
                return True
            await self.retire(client, reason="result_drain_timeout", started_at=started_at)
            return False
        except (asyncio.TimeoutError, StopAsyncIteration):
            await self.retire(client, reason="result_drain_timeout", started_at=started_at)
            return False
