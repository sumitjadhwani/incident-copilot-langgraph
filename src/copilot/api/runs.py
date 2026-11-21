"""Background graph runs and their live event streams.

A run (start, resume after approval, or resume after a crash) executes as an asyncio task that is
independent of any HTTP request. Events go into a per-thread buffer that SSE clients read from; a
client can reconnect with `after=<seq>` and miss nothing that is still buffered. Durable state lives
in the checkpointer, so a restarted API rebuilds everything except the in-memory event buffer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from copilot.context import Actor, CopilotContext
from copilot.errors import Conflict
from copilot.runner import run_outcome, stream_events
from copilot.threads import ThreadRepository

log = logging.getLogger(__name__)
MAX_EVENTS_PER_THREAD = 5000


@dataclass
class _Channel:
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None


class RunManager:
    def __init__(
        self,
        graph: Any,
        threads: ThreadRepository,
        make_context: Callable[[str, Actor], CopilotContext],
        recursion_limit: int = 80,
    ) -> None:
        self.graph = graph
        self.threads = threads
        self.make_context = make_context
        self.recursion_limit = recursion_limit
        self._channels: dict[str, _Channel] = {}

    def _channel(self, thread_id: str) -> _Channel:
        return self._channels.setdefault(thread_id, _Channel())

    def config(self, thread_id: str, actor: Actor) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.recursion_limit,
            "metadata": {"tenant_id": actor.tenant_id, "user_id": actor.user_id},
            "tags": [f"tenant:{actor.tenant_id}"],
        }

    def is_running(self, thread_id: str) -> bool:
        task = self._channel(thread_id).task
        return task is not None and not task.done()

    async def publish(self, thread_id: str, event: dict[str, Any]) -> None:
        ch = self._channel(thread_id)
        async with ch.cond:
            ch.seq += 1
            ch.events.append({**event, "seq": ch.seq})
            if len(ch.events) > MAX_EVENTS_PER_THREAD:
                del ch.events[: len(ch.events) - MAX_EVENTS_PER_THREAD]
            ch.cond.notify_all()

    def start(self, thread_id: str, inputs: Any, actor: Actor, *, reason: str) -> None:
        if self.is_running(thread_id):
            raise Conflict("a run is already in progress for this incident")
        ch = self._channel(thread_id)
        ch.task = asyncio.create_task(self._run(thread_id, inputs, actor, reason), name=f"run-{thread_id}")

    async def _run(self, thread_id: str, inputs: Any, actor: Actor, reason: str) -> None:
        config = self.config(thread_id, actor)
        await self.threads.set_status(thread_id, "running")
        await self.publish(thread_id, {"type": "run_started", "reason": reason, "actor": actor.username})
        status = "failed"
        try:
            async for event in stream_events(self.graph, inputs, config, self.make_context(thread_id, actor)):
                await self.publish(thread_id, event)
            outcome = await run_outcome(self.graph, config)
            status = outcome["status"]
            await self.publish(thread_id, {"type": "run_finished", **outcome})
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI and the thread status
            log.exception("run failed for thread %s", thread_id)
            await self.publish(thread_id, {"type": "run_failed", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await self.threads.set_status(thread_id, status)

    async def subscribe(self, thread_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered then live events; finish once the run is over and everything was sent."""
        ch = self._channel(thread_id)
        cursor = after
        while True:
            async with ch.cond:
                pending = [e for e in ch.events if e["seq"] > cursor]
                if not pending:
                    if not self.is_running(thread_id):
                        return
                    try:
                        await asyncio.wait_for(ch.cond.wait(), timeout=15)
                    except TimeoutError:
                        yield {"type": "keepalive", "seq": cursor}
                    continue
            for event in pending:
                cursor = event["seq"]
                yield event

    async def shutdown(self) -> None:
        tasks = [ch.task for ch in self._channels.values() if ch.task and not ch.task.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
