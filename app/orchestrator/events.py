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
    """Fan-out per session, with a short replay for late arrivals.

    IT USED TO BE ONE QUEUE PER SESSION, SHARED BY EVERY LISTENER, and
    asyncio.Queue delivers each item to exactly ONE consumer. So a second
    listener did not observe the session - it STOLE from the first. With two
    tabs open on the same session, final_ready went to one and finals_done to
    the other, and the tab that got finals_done showed "Listas - 1 foto" above
    an empty grid. The photograph existed, was served correctly, and was
    simply never announced to the page that was looking.

    Two changes fix that, and both are needed:

      fan-out   every subscriber gets its own queue, so listeners cannot take
                events from one another
      replay    a bounded history is re-sent on subscribe, so a page that
                connects a moment late - or reloads - still learns about
                photographs that arrived while it was not listening

    Bounded throughout, and dropping the oldest rather than blocking: a
    browser that closed must never be able to stall the pipeline that is
    spending her money.
    """

    def __init__(self, *, maxsize: int = 256, history: int = 64) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}
        self._history: dict[str, list[Event]] = {}
        self._maxsize = maxsize
        self._history_limit = history

    def publish(self, session_id: str, kind: EventKind, **data) -> None:
        event = Event(kind=kind, data=data)

        past = self._history.setdefault(session_id, [])
        past.append(event)
        if len(past) > self._history_limit:
            del past[: len(past) - self._history_limit]

        for queue in list(self._subscribers.get(session_id, ())):
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

        Replays what has already happened first. A page that arrives after a
        photograph was announced must still be told about it, or it waits
        forever for something that already occurred.

        The heartbeat keeps proxies from severing an idle connection during a
        long generation, which would otherwise look to her like the app froze.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.setdefault(session_id, []).append(queue)
        try:
            for event in list(self._history.get(session_id, ())):
                yield event
                if event.kind is EventKind.FINALS_DONE:
                    return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                except asyncio.TimeoutError:
                    yield Event(kind=EventKind.HEARTBEAT)
                    continue
                yield event
                if event.kind is EventKind.FINALS_DONE:
                    return
        finally:
            listeners = self._subscribers.get(session_id)
            if listeners and queue in listeners:
                listeners.remove(queue)
            if listeners is not None and not listeners:
                self._subscribers.pop(session_id, None)

    def history(self, session_id: str) -> list[Event]:
        """What has happened on this session so far, without consuming it.

        Reading the stream must not remove events from it - that was the whole
        bug this class was rewritten to fix.
        """
        return list(self._history.get(session_id, ()))

    def close(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)
        self._history.pop(session_id, None)


bus = EventBus()
