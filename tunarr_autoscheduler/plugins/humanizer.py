from __future__ import annotations

import logging
import random
from datetime import timedelta

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline

logger = logging.getLogger(__name__)


class Humanizer(Plugin):
    name = "humanizer"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        if not channel_config.humanizer.enabled:
            return timeline

        jitter = channel_config.humanizer.jitter_seconds
        tolerance = channel_config.humanizer.tolerance_seconds

        blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
        total_drift = 0.0

        original_times = [
            (block.start_time, block.end_time)
            for block in blocks
        ]
        for index, block in enumerate(blocks):
            original_start, original_end = original_times[index]
            offset = random.uniform(-jitter, jitter)
            offset = max(-tolerance, min(tolerance, offset))

            min_shift = -float(tolerance)
            max_shift = float(tolerance)
            touches_neighbor = False
            if index > 0:
                previous_end = original_times[index - 1][1]
                touches_neighbor = (
                    original_start - previous_end
                ).total_seconds() <= 5
                min_shift = max(
                    min_shift,
                    (previous_end - original_start).total_seconds(),
                )
            if index + 1 < len(blocks):
                next_start = original_times[index + 1][0]
                touches_neighbor = touches_neighbor or (
                    next_start - original_end
                ).total_seconds() <= 5
                max_shift = min(
                    max_shift,
                    (next_start - original_end).total_seconds(),
                )

            shift = 0.0 if touches_neighbor else max(min_shift, min(max_shift, offset))
            if min_shift > max_shift:
                shift = 0.0

            block.start_time += timedelta(seconds=shift)
            block.end_time += timedelta(seconds=shift)
            total_drift += shift

        logger.debug(
            "Humanized %d blocks with jitter=%ds, total drift=%.1fs",
            len(blocks), jitter, total_drift,
        )

        return timeline
