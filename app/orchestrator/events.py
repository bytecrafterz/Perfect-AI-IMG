"""Per-session event bus, consumed by the browser over SSE.

Why this exists: previews appear ONE BY ONE as they finish rather than all at
once. The same twenty-five seconds feels roughly half as long, she can start
choosing before the set is complete, and the robot visibly looks like it is
working. It was the clearest single win of moving off a chat surface, where an
album can only arrive whole.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum


class EventKind(str, Enum):
    ANALYSING = "analysing"
    STYLES_READY = "styles_ready"
    PREVIEW_STARTED = "preview_started"
    PREVIEW_READY = "preview_ready"  # one tile has landed
    PREVIEW_REPLACED = "preview_replaced"  # a candidate failed, another is coming
    PREVIEWS_DONE = "previews_done"
    FINAL_STARTED = "final_started"
    FINAL_REPAIRING = "final_repairing"
    FINAL_READY = "final_ready"
    FINALS_DONE = "finals_done"
    COST = "cost"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


@dataclass
class Event:
    kind: EventKind
    data: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_sse(self) -> dict:
        return {
            "event": self.kind.value,
            "data": json.dumps({**self.data, "at": self.at}, ensure_ascii=False),
        }


class EventBus:
    """One queue per session.

    Bounded, and drops the oldest event rather than blocking the generator
    when nobody is listening: a browser that closed must never be able to
    stall the pipeline that is spending her money.
    """

    def __init__(self, *, maxsize: int = 256) -> None:
        self._queues: dict[str, asyncio.Queue[Event]] = {}
        self._maxsize = maxsize

    def channel(self, session_id: str) -> asyncio.Queue[Event]:
        queue = self._queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._maxsize)
            self._queues[session_id] = queue
        return queue

    def publish(self, session_id: str, kind: EventKind, **data) -> None:
        queue = self.channel(session_id)
        event = Event(kind=kind, data=data)
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # drop oldest
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 - never let telemetry break work
                pass

    async def subscribe(self, session_id: str, *, heartbeat_s: float = 15.0):
        """Yield events until the session closes.

        The heartbeat keeps proxies from severing an idle connection during a
        long generation, which would otherwise look to her like the app froze.
        """
        queue = self.channel(session_id)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except asyncio.TimeoutError:
                yield Event(kind=EventKind.HEARTBEAT)
                continue
            yield event
            if event.kind is EventKind.FINALS_DONE:
                return

    def close(self, session_id: str) -> None:
        self._queues.pop(session_id, None)


bus = EventBus()
