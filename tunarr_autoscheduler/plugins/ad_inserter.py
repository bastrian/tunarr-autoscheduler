from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    MovieBlock,
    OfflineBlock,
    StationIDBlock,
    TimelineBlock,
)
from tunarr_autoscheduler.models.schedule import RotationState

logger = logging.getLogger(__name__)

MIN_AD_BREAK_SECONDS = 60
MAX_AD_BREAK_SECONDS = 300
DEFAULT_AD_SPOT_DURATION = 30


@dataclass(frozen=True)
class AdSpot:
    id: str
    title: str
    duration_seconds: int


class AdInserter(Plugin):
    name = "ad_inserter"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        if not channel_config.ads.enabled:
            return timeline

        ad_density = channel_config.ads.ad_density
        max_break = min(channel_config.ads.max_ad_break_duration_minutes * 60, MAX_AD_BREAK_SECONDS)
        min_break = channel_config.ads.min_ad_break_duration_minutes * 60
        break_after_programs = max(1, channel_config.ads.break_after_programs)
        min_total_seconds = channel_config.ads.min_total_minutes * 60
        max_total_seconds = channel_config.ads.max_total_minutes * 60
        remaining_total_seconds = max_total_seconds if max_total_seconds > 0 else None
        spots = await self._load_ad_spots(context)
        rotation_index = await self._load_rotation_index(context)
        rotation_start_index = rotation_index

        blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
        target_total_seconds = _target_ad_seconds(
            blocks,
            ad_density,
            min_total_seconds,
            max_total_seconds,
        )
        remaining_break_slots = _count_break_slots(blocks, break_after_programs)
        result = Timeline(metadata=dict(timeline.metadata))
        program_counter = 0
        last_daypart = None
        cumulative_offset = timedelta()
        previous_raw_end: datetime | None = None
        inserted_total_seconds = 0

        ad_warnings: list[str] = []
        used_spot_ids: list[str] = []
        actual_break_count = 0
        generic_break_count = 0
        poor_fit_count = 0
        if target_total_seconds > 0 and not spots:
            ad_warnings.append(
                "No filler spots were loaded; ad breaks use generic placeholder timing.",
            )
        for index, block in enumerate(blocks):
            next_block = blocks[index + 1] if index + 1 < len(blocks) else None
            raw_start = block.start_time
            raw_end = block.end_time
            raw_daypart = block.metadata.get("daypart", "")
            if last_daypart is not None and raw_daypart != last_daypart and cumulative_offset:
                if previous_raw_end is not None:
                    raw_gap = raw_start - previous_raw_end
                    if raw_gap > timedelta():
                        cumulative_offset = max(timedelta(), cumulative_offset - raw_gap)
            block.start_time += cumulative_offset
            block.end_time += cumulative_offset
            daypart = raw_daypart
            slot_density = block.metadata.get("ad_density", ad_density)
            boundary = _daypart_boundary(block, include_tolerance=True)
            raw_boundary = _daypart_boundary(block)
            if (
                boundary is not None
                and block.end_time > boundary
                and not block.metadata.get("variable_movie_duration")
            ):
                last_daypart = daypart
                previous_raw_end = raw_end
                continue

            if not isinstance(block, (EpisodeBlock, MovieBlock)):
                result.insert(block)
                last_daypart = daypart
                previous_raw_end = raw_end
                continue

            program_counter += 1
            is_daypart_boundary = daypart != last_daypart and last_daypart is not None
            frequency_break = program_counter % break_after_programs == 0

            natural_breaks = self._find_natural_breaks(
                block,
                is_daypart_boundary,
                frequency_break,
            )
            if _block_leads_to_offline(next_block, blocks):
                natural_breaks = [
                    break_point
                    for break_point in natural_breaks
                    if abs((break_point - block.end_time).total_seconds()) > 1
                ]
            if _float_metadata({"density": slot_density}, "density") <= 0:
                natural_breaks = []

            break_offset = timedelta()
            if natural_breaks:
                for break_point in sorted(natural_breaks):
                    remaining_break_slots = max(0, remaining_break_slots - 1)
                    remaining_target_seconds = target_total_seconds - inserted_total_seconds
                    if remaining_target_seconds < min_break:
                        continue
                    if remaining_total_seconds is not None and remaining_total_seconds < min_break:
                        continue
                    ad_duration = _balanced_ad_duration(
                        block,
                        slot_density,
                        max_break,
                        min_break,
                        remaining_target_seconds,
                        remaining_break_slots + 1,
                    )
                    if remaining_total_seconds is not None:
                        ad_duration = min(ad_duration, remaining_total_seconds)
                    if ad_duration <= 0:
                        continue
                    available_seconds = _available_break_seconds(
                        break_point + break_offset,
                        block,
                        next_block,
                        blocks,
                        raw_boundary,
                    )
                    if available_seconds < min_break:
                        continue
                    ad_duration = min(ad_duration, available_seconds)

                    selected_spots, rotation_index = self._select_spots(
                        spots,
                        rotation_index,
                        target_seconds=ad_duration,
                        min_seconds=min_break,
                    )
                    if spots and not selected_spots:
                        ad_warnings.append(
                            "No filler spots fit an available ad break window.",
                        )
                        continue
                    actual_duration = sum(s.duration_seconds for s in selected_spots)
                    if not selected_spots:
                        ad_count = max(1, ad_duration // DEFAULT_AD_SPOT_DURATION)
                        actual_duration = ad_count * DEFAULT_AD_SPOT_DURATION
                        generic_break_count += 1
                    else:
                        ad_count = len(selected_spots)
                        used_spot_ids.extend(s.id for s in selected_spots)
                        if actual_duration < max(min_break, int(ad_duration * 0.75)):
                            poor_fit_count += 1
                            ad_warnings.append(
                                "The filler list cannot closely fit some ad break targets.",
                            )
                    if remaining_total_seconds is not None:
                        remaining_total_seconds -= actual_duration
                    inserted_total_seconds += actual_duration
                    actual_break_count += 1
                    actual_delta = timedelta(seconds=actual_duration)
                    ad_start = break_point + break_offset
                    ad_end = ad_start + timedelta(seconds=actual_duration)
                    if raw_boundary is not None and ad_end > raw_boundary:
                        continue

                    ad_block = AdBlock(
                        start_time=ad_start,
                        end_time=ad_end,
                        duration=timedelta(seconds=actual_duration),
                        ad_count=ad_count,
                        total_duration_seconds=actual_duration,
                        metadata={
                            "daypart": daypart,
                            "density": slot_density,
                            "filler_list_id": channel_config.ads.filler_list_id,
                            "target_seconds": ad_duration,
                            "fit_under_seconds": max(0, ad_duration - actual_duration),
                            "spot_ids": [s.id for s in selected_spots],
                            "spots": [s.__dict__ for s in selected_spots],
                        },
                    )
                    result.insert(ad_block)
                    break_offset += actual_delta

            if break_offset:
                cumulative_offset += break_offset
            result.insert(block)
            last_daypart = daypart
            previous_raw_end = raw_end

        if spots and inserted_total_seconds < target_total_seconds:
            inserted_total_seconds, rotation_index = self._fill_existing_gaps(
                result,
                spots,
                rotation_index,
                inserted_total_seconds,
                target_total_seconds,
                min_break,
                max_break,
                channel_config.ads.filler_list_id,
                used_spot_ids,
                ad_warnings,
            )

        await self._save_rotation_index(context, rotation_index)
        _resolve_transition_overlaps(result)
        actual_break_count = len([
            block for block in result.blocks if isinstance(block, AdBlock)
        ])
        result.metadata["ad_inserted_seconds"] = inserted_total_seconds
        result.metadata["ad_target_seconds"] = target_total_seconds
        result.metadata["ad_rotation_summary"] = {
            "filler_list_id": channel_config.ads.filler_list_id,
            "spot_count": len(spots),
            "start_index": rotation_start_index,
            "next_index": rotation_index,
            "break_count": actual_break_count,
            "generic_break_count": generic_break_count,
            "poor_fit_count": poor_fit_count,
            "unique_spots_used": len(set(used_spot_ids)),
            "spots_used": len(used_spot_ids),
        }
        if inserted_total_seconds < min_total_seconds:
            ad_warnings.append(
                "Minimum ad minutes could not be reached with the available breaks.",
            )
        if inserted_total_seconds < target_total_seconds:
            ad_warnings.append(
                "Ad target could not be fully reached without violating timing constraints.",
            )
        if ad_warnings:
            result.metadata["ad_warnings"] = list(dict.fromkeys(ad_warnings))
        return result

    def _fill_existing_gaps(
        self,
        timeline: Timeline,
        spots: list[AdSpot],
        rotation_index: int,
        inserted_total_seconds: int,
        target_total_seconds: int,
        min_break: int,
        max_break: int,
        filler_list_id: str,
        used_spot_ids: list[str],
        ad_warnings: list[str],
    ) -> tuple[int, int]:
        for gap_start, gap_end in timeline.find_gaps():
            remaining_target = target_total_seconds - inserted_total_seconds
            if remaining_target < min_break:
                break
            if _gap_touches_ad(timeline, gap_start, gap_end):
                continue
            if _gap_touches_offline(timeline, gap_start, gap_end):
                continue
            gap_seconds = int((gap_end - gap_start).total_seconds())
            target_seconds = min(gap_seconds, max_break, remaining_target)
            if target_seconds < min_break:
                continue
            selected, next_index = self._select_spots(
                spots,
                rotation_index,
                target_seconds=target_seconds,
                min_seconds=min_break,
            )
            duration = sum(spot.duration_seconds for spot in selected)
            if duration < min_break:
                continue
            if duration < max(min_break, int(target_seconds * 0.75)):
                ad_warnings.append(
                    "The filler list cannot closely fit some ad break targets.",
                )
            timeline.insert(AdBlock(
                start_time=gap_start,
                end_time=gap_start + timedelta(seconds=duration),
                duration=timedelta(seconds=duration),
                ad_count=len(selected),
                total_duration_seconds=duration,
                metadata={
                    "reason": "gap_fill",
                    "filler_list_id": filler_list_id,
                    "target_seconds": target_seconds,
                    "fit_under_seconds": max(0, target_seconds - duration),
                    "spot_ids": [spot.id for spot in selected],
                    "spots": [spot.__dict__ for spot in selected],
                },
            ))
            inserted_total_seconds += duration
            used_spot_ids.extend(spot.id for spot in selected)
            rotation_index = next_index
        return inserted_total_seconds, rotation_index

    def _find_natural_breaks(
        self, block: TimelineBlock, is_daypart_boundary: bool, frequency_break: bool,
    ) -> list[datetime]:
        breaks: list[datetime] = []

        if isinstance(block, EpisodeBlock):
            if frequency_break:
                breaks.append(block.end_time)

        elif isinstance(block, MovieBlock):
            if frequency_break:
                breaks.append(block.end_time)

        return breaks

    async def _load_ad_spots(self, context: PipelineContext) -> list[AdSpot]:
        filler_list_id = context.channel_config.ads.filler_list_id
        if not filler_list_id or context.tunarr_client is None:
            return []
        try:
            raw_programs = await context.get_filler_list_programs(filler_list_id)
        except Exception as e:
            logger.warning("Failed to load Tunarr filler list %s: %s", filler_list_id, e)
            return []
        spots = [self._spot_from_program(p) for p in raw_programs]
        return [s for s in spots if s is not None and s.duration_seconds > 0]

    def _spot_from_program(self, program: dict[str, object]) -> AdSpot | None:
        source = program.get("program") if program.get("type") == "filler" else program
        if not isinstance(source, dict):
            return None
        duration = self._normalize_duration(source.get("duration") or program.get("duration"))
        spot_id = str(source.get("id") or program.get("id") or "")
        if not spot_id:
            return None
        return AdSpot(
            id=spot_id,
            title=str(source.get("title") or program.get("title") or "Ad"),
            duration_seconds=duration,
        )

    def _normalize_duration(self, raw: object) -> int:
        if not isinstance(raw, (str, int, float)):
            return 0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0
        if value > 60 * 60:
            value = value / 1000
        return int(value)

    def _select_spots(
        self,
        spots: list[AdSpot],
        start_index: int,
        target_seconds: int,
        min_seconds: int,
    ) -> tuple[list[AdSpot], int]:
        if not spots:
            return [], start_index

        selected: list[AdSpot] = []
        total = 0
        index = start_index % len(spots)
        checked_without_add = 0
        while checked_without_add < len(spots):
            spot = spots[index]
            if total + spot.duration_seconds <= target_seconds:
                selected.append(spot)
                total += spot.duration_seconds
                index = (index + 1) % len(spots)
                checked_without_add = 0
            else:
                index = (index + 1) % len(spots)
                checked_without_add += 1

        if total < min_seconds:
            return [], start_index
        return selected, index

    async def _load_rotation_index(self, context: PipelineContext) -> int:
        state = getattr(context, "state", None)
        filler_list_id = context.channel_config.ads.filler_list_id
        if state is None or not filler_list_id:
            return 0
        rotation = await state.get_rotation_state(
            context.channel_config.id,
            f"ads:{filler_list_id}",
        )
        return rotation.current_index if rotation else 0

    async def _save_rotation_index(self, context: PipelineContext, index: int) -> None:
        state = getattr(context, "state", None)
        filler_list_id = context.channel_config.ads.filler_list_id
        if state is None or not filler_list_id:
            return
        await state.save_rotation_state(RotationState(
            channel_id=context.channel_config.id,
            rotation_name=f"ads:{filler_list_id}",
            current_index=index,
            last_rotation_time=datetime.now(tz=UTC),
        ))


def _gap_touches_ad(timeline: Timeline, gap_start: datetime, gap_end: datetime) -> bool:
    for block in timeline.blocks:
        if not isinstance(block, AdBlock):
            continue
        if abs((block.end_time - gap_start).total_seconds()) <= 1:
            return True
        if abs((gap_end - block.start_time).total_seconds()) <= 1:
            return True
    return False


def _gap_touches_offline(timeline: Timeline, gap_start: datetime, gap_end: datetime) -> bool:
    for block in timeline.blocks:
        if not isinstance(block, OfflineBlock):
            continue
        if abs((block.end_time - gap_start).total_seconds()) <= 1:
            return True
        if abs((gap_end - block.start_time).total_seconds()) <= 1:
            return True
    return False


def _block_leads_to_offline(
    block: TimelineBlock | None, blocks: list[TimelineBlock],
) -> bool:
    if block is None:
        return False
    if isinstance(block, OfflineBlock):
        return True
    if isinstance(block, StationIDBlock):
        sorted_blocks = sorted(blocks, key=lambda item: item.start_time)
        try:
            index = sorted_blocks.index(block)
        except ValueError:
            return False
        next_block = sorted_blocks[index + 1] if index + 1 < len(sorted_blocks) else None
        return isinstance(next_block, OfflineBlock)
    return False


def _target_ad_seconds(
    blocks: list[TimelineBlock],
    density: float,
    min_total_seconds: int,
    max_total_seconds: int,
) -> int:
    eligible_seconds = sum(
        int(block.duration.total_seconds())
        for block in blocks
        if isinstance(block, (EpisodeBlock, MovieBlock))
    )
    target = max(min_total_seconds, int(eligible_seconds * density))
    if max_total_seconds > 0:
        target = min(target, max_total_seconds)
    return max(0, target)


def _count_break_slots(blocks: list[TimelineBlock], break_after_programs: int) -> int:
    program_counter = 0
    count = 0
    for block in blocks:
        if not isinstance(block, (EpisodeBlock, MovieBlock)):
            continue
        program_counter += 1
        breaks = []
        if program_counter % break_after_programs == 0:
            breaks.append(block.end_time)
        count += len(set(breaks))
    return count


def _balanced_ad_duration(
    block: TimelineBlock,
    density: object,
    max_break: int,
    min_break: int,
    remaining_target_seconds: int,
    remaining_break_slots: int,
) -> int:
    block_density = _float_metadata({"density": density}, "density")
    block_target = int(block.duration.total_seconds() * block_density)
    distributed_target = (
        remaining_target_seconds
        if remaining_break_slots <= 1
        else int((remaining_target_seconds + remaining_break_slots - 1) / remaining_break_slots)
    )
    target = min(max_break, remaining_target_seconds, max(block_target, distributed_target))
    if target < min_break:
        return 0
    return target


def _available_break_seconds(
    break_point: datetime,
    block: TimelineBlock,
    next_block: TimelineBlock | None,
    all_blocks: list[TimelineBlock],
    raw_boundary: datetime | None,
) -> int:
    candidates = [raw_boundary] if raw_boundary is not None else []
    if break_point < block.end_time:
        candidates.append(block.end_time)
    if next_block is not None and _block_leads_to_offline(next_block, all_blocks):
        candidates.append(next_block.start_time)
    future = [
        candidate
        for candidate in candidates
        if candidate is not None and candidate > break_point
    ]
    if not future:
        return MAX_AD_BREAK_SECONDS
    return max(0, int((min(future) - break_point).total_seconds()))


def _resolve_transition_overlaps(timeline: Timeline) -> None:
    blocks = sorted(timeline.blocks, key=lambda item: item.start_time)
    cursor: datetime | None = None
    for block in blocks:
        if cursor is None or block.start_time >= cursor:
            cursor = block.end_time
            continue
        if isinstance(block, StationIDBlock) and block.metadata.get("type") == "daypart_transition":
            duration = block.duration
            block.start_time = cursor
            block.end_time = block.start_time + duration
            cursor = block.end_time
            continue
        if isinstance(block, OfflineBlock):
            block.start_time = cursor
            block.duration = max(block.end_time - block.start_time, timedelta())
            cursor = max(cursor, block.end_time)
            continue
        cursor = max(cursor, block.end_time)


def _daypart_boundary(
    block: TimelineBlock, *, include_tolerance: bool = False,
) -> datetime | None:
    raw = block.metadata.get("daypart_boundary")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        boundary = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if include_tolerance:
        boundary += timedelta(minutes=_int_metadata(block.metadata, "end_tolerance_minutes"))
    return boundary


def _int_metadata(metadata: dict[str, object], key: str) -> int:
    try:
        return max(0, int(str(metadata.get(key, 0))))
    except (TypeError, ValueError):
        return 0


def _float_metadata(metadata: dict[str, object], key: str) -> float:
    try:
        return max(0.0, float(str(metadata.get(key, 0))))
    except (TypeError, ValueError):
        return 0.0
