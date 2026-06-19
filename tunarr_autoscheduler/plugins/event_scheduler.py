from __future__ import annotations

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline


class EventScheduler(Plugin):
    name = "event_scheduler"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        return timeline
