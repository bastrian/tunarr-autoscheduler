from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RotationState(BaseModel):
    channel_id: str
    rotation_name: str
    current_index: int = 0
    current_show_id: str | None = None
    episode_counter: int = 0
    last_rotation_time: datetime | None = None


class AirHistoryEntry(BaseModel):
    channel_id: str
    item_id: str
    item_type: str
    aired_at: datetime
    duration_seconds: int
    show_id: str | None = None
    schedule_version: int | None = None


class CooldownEntry(BaseModel):
    item_id: str
    item_type: str
    channel_id: str
    cooldown_until: datetime


class MediaCacheEntry(BaseModel):
    id: str
    item_type: str
    source_type: str
    source_id: str
    title: str
    duration_seconds: int | None = None
    metadata: dict[str, Any] | None = None
    available: bool = True
