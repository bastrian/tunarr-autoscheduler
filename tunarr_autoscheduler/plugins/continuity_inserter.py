from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.tunarr.programs import program_ids
from tunarr_autoscheduler.models.blocks import (
    EpisodeBlock,
    MovieBlock,
    OfflineBlock,
    StationIDBlock,
    TimelineBlock,
)

logger = logging.getLogger(__name__)

DEFAULT_STATION_ID_DURATION = 15
DEFAULT_BUMPER_DURATION = 30


class ContinuityInserter(Plugin):
    name = "continuity_inserter"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        if not channel_config.continuity.enabled:
            return timeline

        frequency = channel_config.continuity.frequency
        station_id_clip_ids = await self._get_clip_ids(
            context,
            channel_config.continuity.station_id_custom_show_id,
            channel_config.continuity.station_id_clip_ids,
        )
        bumper_clip_ids = await self._get_clip_ids(
            context,
            channel_config.continuity.bumper_custom_show_id,
            channel_config.continuity.bumper_clip_ids,
        )
        blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
        result = Timeline(metadata=dict(timeline.metadata))
        block_count = 0
        last_daypart = None
        previous_source_block: TimelineBlock | None = None
        cumulative_offset = timedelta()

        for block in blocks:
            raw_daypart = block.metadata.get("daypart", "")
            is_daypart_boundary = (
                raw_daypart != last_daypart and last_daypart is not None
            )
            if is_daypart_boundary:
                cumulative_offset = timedelta()
            block.start_time += cumulative_offset
            block.end_time += cumulative_offset
            block_count += 1
            daypart = raw_daypart
            pre_block_shift = timedelta()
            inserted_station_id = False

            if _should_insert_daypart_transition(
                previous_source_block, block, is_daypart_boundary,
            ):
                transition_start = max(block.start_time, _timeline_end(result))
                station_id = StationIDBlock(
                    start_time=transition_start,
                    end_time=transition_start + timedelta(seconds=DEFAULT_STATION_ID_DURATION),
                    duration=timedelta(seconds=DEFAULT_STATION_ID_DURATION),
                    clip_id=self._select_clip_id(
                        station_id_clip_ids, block_count, "daypart_transition",
                    ),
                    metadata={"type": "daypart_transition", "daypart": daypart},
                )
                result.insert(station_id)
                block.start_time = max(block.start_time, station_id.end_time)
                block.duration = max(block.end_time - block.start_time, timedelta())
                inserted_station_id = True

            if isinstance(block, (EpisodeBlock, MovieBlock)) and not inserted_station_id:
                if block_count > 0 and block_count % frequency == 0:
                    if isinstance(block, MovieBlock):
                        bumper = StationIDBlock(
                            start_time=block.start_time,
                            end_time=block.start_time + timedelta(seconds=DEFAULT_BUMPER_DURATION),
                            duration=timedelta(seconds=DEFAULT_BUMPER_DURATION),
                            clip_id=self._select_clip_id(
                                bumper_clip_ids, block_count, "up_next_bumper",
                            ),
                            metadata={
                                "type": "up_next",
                                "title": block.metadata.get("title", ""),
                                "daypart": daypart,
                            },
                        )
                        result.insert(bumper)
                        pre_block_shift += timedelta(seconds=DEFAULT_BUMPER_DURATION)

                    elif isinstance(block, EpisodeBlock):
                        show_name = block.metadata.get("show_name", "")
                        station_id = StationIDBlock(
                            start_time=block.start_time,
                            end_time=(
                                block.start_time + timedelta(seconds=DEFAULT_STATION_ID_DURATION)
                            ),
                            duration=timedelta(seconds=DEFAULT_STATION_ID_DURATION),
                            clip_id=self._select_clip_id(
                                station_id_clip_ids, block_count, "station_id",
                            ),
                            metadata={
                                "type": "station_id",
                                "show_name": show_name,
                                "daypart": daypart,
                            },
                        )
                        result.insert(station_id)
                        pre_block_shift += timedelta(seconds=DEFAULT_STATION_ID_DURATION)

            if pre_block_shift:
                block.start_time += pre_block_shift
                block.end_time += pre_block_shift
                cumulative_offset += pre_block_shift
            if _crosses_daypart_boundary(block):
                last_daypart = daypart
                continue
            result.insert(block)
            previous_source_block = block
            last_daypart = daypart

        return result

    async def _get_clip_ids(
        self, context: PipelineContext, custom_show_id: str, configured_clip_ids: list[str],
    ) -> list[str]:
        clip_ids = [clip_id for clip_id in configured_clip_ids if clip_id]
        if not custom_show_id:
            return clip_ids

        if context.tunarr_client is None:
            return clip_ids
        try:
            programs = await context.get_custom_show_programs(custom_show_id)
        except Exception as e:
            logger.warning("Failed to load continuity custom show %s: %s", custom_show_id, e)
            return clip_ids

        for program in programs:
            ids = program_ids(program)
            if ids:
                clip_ids.append(ids[0])
        return _unique(clip_ids)

    def _select_clip_id(self, clip_ids: list[str], position: int, fallback: str) -> str:
        if not clip_ids:
            return fallback
        return clip_ids[position % len(clip_ids)]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value and value not in seen:
            unique_values.append(value)
            seen.add(value)
    return unique_values


def _timeline_end(timeline: Timeline) -> datetime:
    if not timeline.blocks:
        return datetime.min.replace(tzinfo=UTC)
    return max(block.end_time for block in timeline.blocks)


def _crosses_daypart_boundary(block: TimelineBlock) -> bool:
    if block.metadata.get("variable_movie_duration"):
        return False
    raw = block.metadata.get("daypart_boundary")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        boundary = datetime.fromisoformat(raw)
    except ValueError:
        return False
    boundary += timedelta(minutes=_int_metadata(block.metadata, "end_tolerance_minutes"))
    return block.end_time > boundary


def _should_insert_daypart_transition(
    previous_block: TimelineBlock | None,
    current_block: TimelineBlock,
    is_daypart_boundary: bool,
) -> bool:
    if not is_daypart_boundary:
        return False
    if isinstance(current_block, OfflineBlock):
        return False
    if isinstance(previous_block, OfflineBlock) and isinstance(current_block, OfflineBlock):
        return False
    return True


def _int_metadata(metadata: dict[str, object], key: str) -> int:
    try:
        return max(0, int(str(metadata.get(key, 0))))
    except (TypeError, ValueError):
        return 0
