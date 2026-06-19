from __future__ import annotations

import logging
from typing import Any

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import EpisodeBlock, MovieBlock, SlotBlock

logger = logging.getLogger(__name__)


class ValidationError:
    def __init__(self, error_type: str, message: str, block_id: str | None = None):
        self.error_type = error_type
        self.message = message
        self.block_id = block_id

    def __str__(self) -> str:
        prefix = f"[{self.block_id}] " if self.block_id else ""
        return f"{prefix}{self.error_type}: {self.message}"


class Validator(Plugin):
    name = "validator"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        errors: list[ValidationError] = []

        errors.extend(self._check_overlaps(timeline))
        errors.extend(self._check_gaps(timeline))
        errors.extend(self._check_runtimes(timeline))
        errors.extend(self._check_duplicates(timeline, context))
        errors.extend(self._check_daypart_compliance(timeline, channel_config))
        errors.extend(self._check_unfilled_slots(timeline))

        if errors:
            for err in errors:
                logger.warning("Validation: %s", err)
                metrics = getattr(context, "metrics", None)
                if metrics:
                    metrics.record_validation_error(
                        channel_config.id, err.error_type,
                    )
        else:
            total_hours = timeline.total_duration().total_seconds() / 3600
            logger.info(
                "Validation passed: %d blocks, %.1f hours",
                len(timeline.blocks), total_hours,
            )

        timeline.metadata = timeline.metadata or {}
        timeline.metadata["validation_errors"] = [str(e) for e in errors]
        timeline.metadata["validation_passed"] = len(errors) == 0

        return timeline

    def _check_overlaps(self, timeline: Timeline) -> list[ValidationError]:
        errors: list[ValidationError] = []
        sorted_blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
        for i in range(len(sorted_blocks) - 1):
            a = sorted_blocks[i]
            b = sorted_blocks[i + 1]
            if a.end_time > b.start_time:
                overlap = (a.end_time - b.start_time).total_seconds()
                errors.append(ValidationError(
                    "overlap",
                    f"{overlap:.0f}s overlap between blocks "
                    f"({a.start_time.isoformat()}-{a.end_time.isoformat()}) and "
                    f"({b.start_time.isoformat()}-{b.end_time.isoformat()})",
                    block_id=a.id,
                ))
        return errors

    def _check_unfilled_slots(self, timeline: Timeline) -> list[ValidationError]:
        errors: list[ValidationError] = []
        for block in timeline.blocks:
            if isinstance(block, SlotBlock):
                daypart = block.metadata.get("daypart", "unknown")
                errors.append(ValidationError(
                    "unfilled_slot",
                    f"Unfilled slot remains in daypart '{daypart}'",
                    block_id=block.id,
                ))
        return errors

    def _check_gaps(self, timeline: Timeline) -> list[ValidationError]:
        errors: list[ValidationError] = []
        gaps = timeline.find_gaps()
        for gap_start, gap_end in gaps:
            gap_seconds = (gap_end - gap_start).total_seconds()
            if gap_seconds > 5:
                errors.append(ValidationError(
                    "dead_air",
                    f"{gap_seconds:.0f}s gap between "
                    f"{gap_start.isoformat()} and {gap_end.isoformat()}",
                ))
        return errors

    def _check_runtimes(self, timeline: Timeline) -> list[ValidationError]:
        errors: list[ValidationError] = []
        for block in timeline.blocks:
            if isinstance(block, EpisodeBlock):
                if block.runtime_seconds <= 0:
                    errors.append(ValidationError(
                        "invalid_runtime",
                        f"Episode {block.episode_id} has invalid runtime: {block.runtime_seconds}s",
                        block_id=block.id,
                    ))
            elif isinstance(block, MovieBlock):
                if block.runtime_seconds <= 0:
                    errors.append(ValidationError(
                        "invalid_runtime",
                        f"Movie {block.movie_id} has invalid runtime: {block.runtime_seconds}s",
                        block_id=block.id,
                    ))
        return errors

    def _check_duplicates(
        self, timeline: Timeline, context: PipelineContext,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        seen_episodes: set[str] = set()
        seen_movies: set[str] = set()

        for block in timeline.blocks:
            metadata = block.metadata or {}
            if metadata.get("custom_show_loop"):
                continue
            if isinstance(block, EpisodeBlock):
                if block.episode_id in seen_episodes:
                    errors.append(ValidationError(
                        "duplicate_airing",
                        f"Episode {block.episode_id} appears multiple times",
                        block_id=block.id,
                    ))
                seen_episodes.add(block.episode_id)
            elif isinstance(block, MovieBlock):
                if block.movie_id in seen_movies:
                    errors.append(ValidationError(
                        "duplicate_airing",
                        f"Movie {block.movie_id} appears multiple times",
                        block_id=block.id,
                    ))
                seen_movies.add(block.movie_id)

        return errors

    def _check_daypart_compliance(
        self, timeline: Timeline, channel_config: Any,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        for block in timeline.blocks:
            metadata = block.metadata or {}
            allow_movies = metadata.get("allow_movies", True)
            if isinstance(block, MovieBlock) and not allow_movies:
                daypart = metadata.get("daypart", "unknown")
                errors.append(ValidationError(
                    "daypart_violation",
                    f"Movie scheduled in daypart '{daypart}' which does not allow movies",
                    block_id=block.id,
                ))
        return errors
