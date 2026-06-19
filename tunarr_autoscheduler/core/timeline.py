from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    BlockType,
    EpisodeBlock,
    FillerBlock,
    FillerType,
    MovieBlock,
    OfflineBlock,
    SlotBlock,
    SpecialEventBlock,
    StationIDBlock,
    TimelineBlock,
)


class Timeline:
    def __init__(
        self,
        blocks: list[TimelineBlock] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.blocks = blocks or []
        self.metadata: dict[str, Any] = metadata or {}

    def insert(self, block: TimelineBlock) -> None:
        self.blocks.append(block)
        self.blocks.sort(key=lambda b: b.start_time)

    def remove(self, block_id: str) -> None:
        self.blocks = [b for b in self.blocks if b.id != block_id]

    def query(self, start: datetime, end: datetime) -> list[TimelineBlock]:
        return [
            b
            for b in self.blocks
            if b.start_time < end and b.end_time > start
        ]

    def find_gaps(self) -> list[tuple[datetime, datetime]]:
        if not self.blocks:
            return []
        sorted_blocks = sorted(self.blocks, key=lambda b: b.start_time)
        gaps: list[tuple[datetime, datetime]] = []
        cursor = sorted_blocks[0].start_time
        for block in sorted_blocks:
            if block.start_time > cursor + timedelta(seconds=1):
                gaps.append((cursor, block.start_time))
            if block.end_time > cursor:
                cursor = block.end_time
        return gaps

    def time_shift(self, amount: timedelta) -> None:
        for block in self.blocks:
            block.start_time += amount
            block.end_time += amount

    def validate(self) -> list[str]:
        errors: list[str] = []
        sorted_blocks = sorted(self.blocks, key=lambda b: b.start_time)
        for i in range(len(sorted_blocks) - 1):
            a = sorted_blocks[i]
            b = sorted_blocks[i + 1]
            if a.end_time > b.start_time:
                errors.append(
                    f"Overlap: block {a.id} ({a.start_time}-{a.end_time}) "
                    f"overlaps with {b.id} ({b.start_time}-{b.end_time})"
                )
        gaps = self.find_gaps()
        for gap_start, gap_end in gaps:
            gap_duration = (gap_end - gap_start).total_seconds()
            if gap_duration > 5:
                errors.append(
                    f"Gap: {gap_duration}s gap between {gap_start} and {gap_end}"
                )
        return errors

    def snapshot(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "blocks": [_serialize_block(b) for b in self.blocks],
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> Timeline:
        return cls(
            blocks=[_deserialize_block(b) for b in data.get("blocks", [])],
            metadata=dict(data.get("metadata", {})),
        )

    def total_duration(self) -> timedelta:
        if not self.blocks:
            return timedelta()
        sorted_blocks = sorted(self.blocks, key=lambda b: b.start_time)
        return sorted_blocks[-1].end_time - sorted_blocks[0].start_time

    def copy(self) -> Timeline:
        return Timeline(
            blocks=[_clone_block(b) for b in self.blocks],
            metadata=dict(self.metadata),
        )


def _serialize_block(block: TimelineBlock) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": block.id,
        "block_type": block.block_type.value,
        "start_time": block.start_time.isoformat(),
        "end_time": block.end_time.isoformat(),
        "duration_seconds": block.duration.total_seconds(),
        "metadata": block.metadata,
    }
    if isinstance(block, EpisodeBlock):
        data.update(
            episode_id=block.episode_id,
            show_id=block.show_id,
            season_number=block.season_number,
            episode_number=block.episode_number,
            runtime_seconds=block.runtime_seconds,
        )
    elif isinstance(block, MovieBlock):
        data["movie_id"] = block.movie_id
        data["runtime_seconds"] = block.runtime_seconds
        data["year"] = block.year
    elif isinstance(block, AdBlock):
        data.update(
            ad_count=block.ad_count,
            total_duration_seconds=block.total_duration_seconds,
        )
    elif isinstance(block, StationIDBlock):
        data.update(clip_id=block.clip_id)
    elif isinstance(block, FillerBlock):
        data.update(filler_type=block.filler_type.value)
    elif isinstance(block, OfflineBlock):
        data.update(reason=block.reason)
    elif isinstance(block, SpecialEventBlock):
        data.update(event_id=block.event_id)
    return data


def _deserialize_block(data: dict[str, Any]) -> TimelineBlock:
    block_type = BlockType(data["block_type"])
    start = datetime.fromisoformat(data["start_time"])
    end = datetime.fromisoformat(data["end_time"])
    duration = timedelta(seconds=data["duration_seconds"])
    base = {
        "id": data["id"],
        "start_time": start,
        "end_time": end,
        "duration": duration,
        "metadata": data.get("metadata", {}),
    }
    if block_type == BlockType.EPISODE:
        return EpisodeBlock(
            episode_id=data.get("episode_id", ""),
            show_id=data.get("show_id", ""),
            season_number=data.get("season_number", 0),
            episode_number=data.get("episode_number", 0),
            runtime_seconds=data.get("runtime_seconds", 0),
            **base,
        )
    elif block_type == BlockType.SLOT:
        return SlotBlock(**base)
    elif block_type == BlockType.MOVIE:
        return MovieBlock(
            movie_id=data.get("movie_id", ""),
            runtime_seconds=data.get("runtime_seconds", 0),
            year=data.get("year"),
            **base,
        )
    elif block_type == BlockType.AD:
        return AdBlock(
            ad_count=data.get("ad_count", 0),
            total_duration_seconds=data.get("total_duration_seconds", 0),
            **base,
        )
    elif block_type == BlockType.STATION_ID:
        return StationIDBlock(clip_id=data.get("clip_id", ""), **base)
    elif block_type == BlockType.FILLER:
        return FillerBlock(
            filler_type=FillerType(data.get("filler_type", FillerType.BUMPER.value)),
            **base,
        )
    elif block_type == BlockType.OFFLINE:
        return OfflineBlock(reason=data.get("reason", ""), **base)
    elif block_type == BlockType.SPECIAL_EVENT:
        return SpecialEventBlock(event_id=data.get("event_id", ""), **base)
    return TimelineBlock(**base, block_type=block_type)


def _clone_block(block: TimelineBlock) -> TimelineBlock:
    data: dict[str, Any] = _serialize_block(block)
    return _deserialize_block(data)
