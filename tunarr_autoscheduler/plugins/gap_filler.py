from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import FillerBlock, FillerType, OfflineBlock, SlotBlock

logger = logging.getLogger(__name__)

GAP_THRESHOLD_SECONDS = 5
STANDBY_THRESHOLD_SECONDS = 600


class GapFiller(Plugin):
    name = "gap_filler"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        timeline = self._fill_remaining_slots(timeline, context)
        gaps = timeline.find_gaps()
        if not gaps:
            return timeline

        filled_count = 0
        for gap_start, gap_end in gaps:
            gap_seconds = (gap_end - gap_start).total_seconds()
            if gap_seconds <= GAP_THRESHOLD_SECONDS:
                continue

            timeline.insert(self._gap_block(
                context,
                gap_start,
                gap_end,
                reason="gap_fill",
                metadata={"gap_seconds": gap_seconds},
            ))
            filled_count += 1

        if filled_count:
            logger.info(
                "Filled %d gap(s) totaling %.0fs",
                filled_count,
                sum(
                    (e - s).total_seconds()
                    for s, e in gaps
                    if (e - s).total_seconds() > GAP_THRESHOLD_SECONDS
                ),
            )

        return timeline

    def _fill_remaining_slots(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        result = Timeline(metadata=dict(timeline.metadata))
        filled_count = 0
        for block in timeline.blocks:
            if not isinstance(block, SlotBlock):
                result.insert(block)
                continue

            gap_start = block.start_time
            gap_end = block.end_time
            if block.metadata.get("note") == "episode_does_not_fit_daypart":
                boundary = _daypart_boundary(block.metadata)
                if boundary is not None:
                    gap_end = min(gap_end, boundary)
            if gap_start >= gap_end:
                continue

            gap_seconds = (gap_end - gap_start).total_seconds()
            result.insert(self._gap_block(
                context,
                gap_start,
                gap_end,
                reason="unfilled_slot",
                metadata={**block.metadata, "gap_seconds": gap_seconds},
            ))
            filled_count += 1

        if filled_count:
            logger.info("Filled %d remaining slot(s)", filled_count)
        return result

    def _select_filler_type(self, gap_seconds: float) -> FillerType:
        if gap_seconds < 30:
            return FillerType.BUMPER
        elif gap_seconds < 120:
            return FillerType.TRAILER
        elif gap_seconds < 600:
            return FillerType.MINI_CONTENT
        else:
            return FillerType.FILLER_EPISODE

    def _gap_block(
        self,
        context: PipelineContext,
        gap_start: datetime,
        gap_end: datetime,
        *,
        reason: str,
        metadata: dict[str, Any],
    ) -> FillerBlock | OfflineBlock:
        gap_duration = gap_end - gap_start
        gap_seconds = gap_duration.total_seconds()
        standby_custom_show_id = context.channel_config.standby_custom_show_id
        if standby_custom_show_id and gap_seconds >= STANDBY_THRESHOLD_SECONDS:
            return OfflineBlock(
                start_time=gap_start,
                end_time=gap_end,
                duration=gap_duration,
                reason="Standby Loop",
                metadata={
                    **metadata,
                    "reason": "standby_loop",
                    "custom_show_list_ids": [standby_custom_show_id],
                    "title": "Standby Loop",
                },
            )

        filler_type = self._select_filler_type(gap_seconds)
        return FillerBlock(
            start_time=gap_start,
            end_time=gap_end,
            duration=gap_duration,
            filler_type=filler_type,
            metadata={
                **metadata,
                "reason": reason,
                "filler_type": filler_type.value,
            },
        )


def _daypart_boundary(metadata: dict[str, Any]) -> datetime | None:
    raw = metadata.get("daypart_boundary")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
