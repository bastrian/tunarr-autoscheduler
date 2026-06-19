from __future__ import annotations

import json
import logging

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline

logger = logging.getLogger(__name__)


class SchedulePersister(Plugin):
    name = "schedule_persister"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        state = getattr(context, "state", None)
        if state is None:
            logger.warning("No state manager available, skipping schedule persistence")
            return timeline

        channel_id = context.channel_config.id
        latest_version = await state.get_latest_version(channel_id)
        version = latest_version + 1
        status = "draft" if timeline.metadata.get("validation_passed", True) else "invalid"
        timeline.metadata["schedule_version"] = version
        timeline.metadata["schedule_status"] = status
        timeline_json = json.dumps(timeline.snapshot(), default=str)

        version_id = await state.save_schedule_version(
            channel_id=channel_id,
            version=version,
            timeline_json=timeline_json,
            status=status,
            parent_version=context.parent_version,
        )
        if version_id is None:
            raise RuntimeError(f"Failed to persist schedule version {version} for {channel_id}")

        timeline.metadata["schedule_version_id"] = version_id
        logger.info(
            "Persisted schedule version %s for channel %s as %s",
            version,
            channel_id,
            status,
        )
        return timeline
