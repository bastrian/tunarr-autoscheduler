from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class BlockType(StrEnum):
    SLOT = "slot"
    EPISODE = "episode"
    MOVIE = "movie"
    AD = "ad"
    STATION_ID = "station_id"
    FILLER = "filler"
    OFFLINE = "offline"
    SPECIAL_EVENT = "special_event"


class FillerType(StrEnum):
    BUMPER = "bumper"
    TRAILER = "trailer"
    MINI_CONTENT = "mini_content"
    FILLER_EPISODE = "filler_episode"


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduleStatus(StrEnum):
    DRAFT = "draft"
    PREVIEWED = "previewed"
    APPROVED = "approved"
    UPLOADED = "uploaded"
    AIRED = "aired"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass
class TimelineBlock:
    start_time: datetime
    end_time: datetime
    duration: timedelta
    block_type: BlockType = BlockType.EPISODE
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class SlotBlock(TimelineBlock):
    def __post_init__(self) -> None:
        self.block_type = BlockType.SLOT


@dataclass
class EpisodeBlock(TimelineBlock):
    episode_id: str = ""
    show_id: str = ""
    season_number: int = 0
    episode_number: int = 0
    runtime_seconds: int = 0

    def __post_init__(self) -> None:
        self.block_type = BlockType.EPISODE


@dataclass
class MovieBlock(TimelineBlock):
    movie_id: str = ""
    runtime_seconds: int = 0
    year: int | None = None

    def __post_init__(self) -> None:
        self.block_type = BlockType.MOVIE


@dataclass
class AdBlock(TimelineBlock):
    ad_count: int = 0
    total_duration_seconds: int = 0

    def __post_init__(self) -> None:
        self.block_type = BlockType.AD


@dataclass
class StationIDBlock(TimelineBlock):
    clip_id: str = ""

    def __post_init__(self) -> None:
        self.block_type = BlockType.STATION_ID


@dataclass
class FillerBlock(TimelineBlock):
    filler_type: FillerType = FillerType.BUMPER

    def __post_init__(self) -> None:
        self.block_type = BlockType.FILLER


@dataclass
class OfflineBlock(TimelineBlock):
    reason: str = ""

    def __post_init__(self) -> None:
        self.block_type = BlockType.OFFLINE


@dataclass
class SpecialEventBlock(TimelineBlock):
    event_id: str = ""

    def __post_init__(self) -> None:
        self.block_type = BlockType.SPECIAL_EVENT


@dataclass
class GenerationJob:
    id: str
    channel_id: str
    status: JobStatus = JobStatus.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    current_stage: str = ""
    error_message: str | None = None
    checkpoint_id: str | None = None
    schedule_version_id: str | None = None
