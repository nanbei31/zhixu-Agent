"""Ordered in-memory event stream used by the Web trace panel."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


class EventBus:
    def __init__(self, max_events: int = 2000):
        self.max_events = max_events
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self._sequence = 0

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._sequence += 1
        event = {
            "sequence": self._sequence,
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def after(self, sequence: int) -> list[dict[str, Any]]:
        return [event for event in self.events if event["sequence"] > sequence]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
