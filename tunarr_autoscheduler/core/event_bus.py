from __future__ import annotations

import inspect
from collections.abc import Callable
from enum import StrEnum
from typing import Any


class Event(StrEnum):
    SLOT_STARTED = "slot_started"
    EPISODE_SELECTED = "episode_selected"
    MOVIE_SELECTED = "movie_selected"
    MEDIA_MISSING = "media_missing"
    SHOW_REACTIVATED = "show_reactivated"
    TIMELINE_FINALIZED = "timeline_finalized"
    PIPELINE_STAGE_STARTED = "pipeline_stage_started"
    PIPELINE_STAGE_COMPLETED = "pipeline_stage_completed"
    PIPELINE_FAILED = "pipeline_failed"
    SCHEDULE_APPROVED = "schedule_approved"
    SCHEDULE_UPLOADED = "schedule_uploaded"


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[Event, list[Callable[..., None]]] = {}

    def subscribe(self, event: Event, handler: Callable[..., None]) -> None:
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(handler)

    def unsubscribe(self, event: Event, handler: Callable[..., None]) -> None:
        if event in self._subscribers:
            self._subscribers[event] = [h for h in self._subscribers[event] if h is not handler]

    async def emit(self, event: Event, **kwargs: Any) -> None:
        handlers = self._subscribers.get(event, [])
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                await handler(event=event, **kwargs)
            else:
                handler(event=event, **kwargs)
