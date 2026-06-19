from __future__ import annotations

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline


class StatsExporter(Plugin):
    name = "statistics_exporter"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        return timeline
