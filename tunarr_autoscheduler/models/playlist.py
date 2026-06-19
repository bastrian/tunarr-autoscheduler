from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlaylistItem(BaseModel):
    media_type: Literal["series", "movie"]
    media_id: str
    title: str
    position: int = 0


class PlaylistCategory(BaseModel):
    id: str
    name: str
    description: str = ""
    created_at: datetime
    updated_at: datetime


class Playlist(BaseModel):
    id: str
    name: str
    description: str = ""
    category_id: str = ""
    category_name: str = ""
    channel_scope: str = ""
    tags: list[str] = Field(default_factory=list)
    items: list[PlaylistItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
