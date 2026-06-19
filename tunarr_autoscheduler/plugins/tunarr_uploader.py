from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import BlockType

logger = logging.getLogger(__name__)


class TunarrUploader(Plugin):
    name = "tunarr_uploader"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        client = getattr(context, "tunarr_client", None)
        if not client:
            logger.warning("No Tunarr client available, skipping upload")
            return timeline

        channel_id = context.channel_config.id
        try:
            await client.upload_timeline(
                channel_id,
                timeline,
                station_id_custom_show_id=(
                    context.channel_config.continuity.station_id_custom_show_id
                ),
                bumper_custom_show_id=(
                    context.channel_config.continuity.bumper_custom_show_id
                ),
            )
            logger.info("Schedule uploaded for channel %s", channel_id)
        except Exception as e:
            logger.error("Failed to upload schedule for channel %s: %s", channel_id, e)
            raise

        return timeline

    def _convert_timeline(self, timeline: Timeline) -> dict[str, Any]:
        blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
        if not blocks:
            return {
                "schedule": {
                    "type": "time",
                    "flexPreference": "distribute",
                    "latenessMs": 0,
                    "maxDays": 1,
                    "padMs": 0,
                    "period": "day",
                    "slots": [{"startTime": 0, "type": "flex"}],
                    "timeZoneOffset": 0,
                    "startTomorrow": False,
                },
            }

        day_start = _midnight(blocks[0].start_time)
        slots = [_slot_from_block(block, day_start) for block in blocks]
        days = max(
            1,
            int((blocks[-1].end_time - day_start) / timedelta(days=1)) + 1,
        )
        return {
            "schedule": {
                "type": "time",
                "flexPreference": "distribute",
                "latenessMs": 0,
                "maxDays": days,
                "padMs": 0,
                "period": "day",
                "slots": slots,
                "timeZoneOffset": 0,
                "startTomorrow": False,
            },
        }


def _midnight(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _slot_from_block(block: Any, day_start: datetime) -> dict[str, Any]:
    start_ms = int((block.start_time - day_start).total_seconds() * 1000)
    metadata = block.metadata or {}
    slot_id = _uuid_string(str(block.id))
    custom_show_id = _first_string(metadata.get("custom_show_list_ids", []))
    if custom_show_id:
        return {
            "id": slot_id,
            "type": "custom-show",
            "customShowId": custom_show_id,
            "order": "next",
            "startTime": start_ms,
        }
    if block.block_type == BlockType.MOVIE:
        return {
            "id": slot_id,
            "type": "movie",
            "order": "next",
            "startTime": start_ms,
        }
    if block.block_type == BlockType.EPISODE:
        show_id = getattr(block, "show_id", "") or metadata.get("show_id", "")
        if show_id:
            return {
                "id": slot_id,
                "type": "show",
                "showId": str(show_id),
                "order": "next",
                "startTime": start_ms,
            }
    if block.block_type in {BlockType.AD, BlockType.FILLER}:
        filler_list_id = metadata.get("filler_list_id", "")
        if filler_list_id:
            return {
                "id": slot_id,
                "type": "filler",
                "fillerListId": str(filler_list_id),
                "order": "shuffle_prefer_short",
                "durationWeighting": "linear",
                "decayFactor": 0.5,
                "recoveryFactor": 0.5,
                "startTime": start_ms,
            }
    return {"id": slot_id, "type": "flex", "startTime": start_ms}


def _first_string(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
    return ""


def _uuid_string(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))
