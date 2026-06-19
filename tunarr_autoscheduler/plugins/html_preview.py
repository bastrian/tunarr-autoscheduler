from __future__ import annotations

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.core.timezones import to_timezone
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    FillerBlock,
    MovieBlock,
    OfflineBlock,
    SlotBlock,
    StationIDBlock,
    TimelineBlock,
)


class HTMLPreview(Plugin):
    name = "html_preview"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        self._timeline = timeline
        self._context = context
        return timeline

    def render(
        self,
        timeline: Timeline | None = None,
        channel_name: str = "Channel",
        *,
        timezone: str = "UTC",
    ) -> str:
        tl = timeline or getattr(self, "_timeline", None)
        if tl is None:
            return "<html><body><p>No timeline to preview.</p></body></html>"

        blocks = sorted(tl.blocks, key=lambda b: b.start_time)
        rows_html = "\n".join(self._render_block(b, timezone) for b in blocks)
        validation_errors = tl.metadata.get("validation_errors", [])
        errors_html = ""
        if validation_errors:
            items = "".join(f"<li>{error}</li>" for error in validation_errors)
            errors_html = (
                '<section class="validation-errors">'
                "<h2>Validation Errors</h2>"
                f"<ul>{items}</ul>"
                "</section>"
            )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Schedule Preview - {channel_name}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 20px; background: #0a0a0a; color: #e0e0e0; }}
  h1 {{ color: #c9a24a; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }}
  th {{ background: #1a1a1a; color: #c9a24a; position: sticky; top: 0; }}
  tr:hover {{ background: #1a1a1a; }}
  .episode {{ border-left: 3px solid #4a9eff; }}
  .movie {{ border-left: 3px solid #ff6b4a; }}
  .ad {{ border-left: 3px solid #ffd700; }}
  .station_id {{ border-left: 3px solid #00cc88; }}
  .filler {{ border-left: 3px solid #888; }}
  .offline {{ border-left: 3px solid #ff4444; }}
  .badge {{
    display: inline-block;
    min-width: 72px;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    text-align: center;
  }}
  .badge-episode {{ background: #173a63; color: #9bd0ff; }}
  .badge-movie {{ background: #582318; color: #ffb39f; }}
  .badge-ad {{ background: #514510; color: #ffe985; }}
  .badge-station_id {{ background: #0f4f3d; color: #8ff0cc; }}
  .badge-filler {{ background: #343846; color: #d8deef; }}
  .badge-offline {{ background: #5c151f; color: #ff9aa8; }}
  .badge-slot {{ background: #3f2f5d; color: #d6c4ff; }}
  .badge-special_event {{ background: #51314d; color: #ffc8f5; }}
  .validation-errors {{
    border: 1px solid #8a3333;
    background: #2a1111;
    padding: 12px;
    margin: 16px 0;
  }}
  .validation-errors h2 {{ margin: 0 0 8px; color: #ff8a8a; font-size: 18px; }}
</style>
</head>
<body>
<h1>Schedule Preview - {channel_name}</h1>
<p>{len(blocks)} blocks | Total: {tl.total_duration()}</p>
{errors_html}
<table>
<thead><tr><th>Time</th><th>Type</th><th>Title</th><th>Duration</th><th>Details</th></tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
</body>
</html>"""

    def _render_block(self, block: TimelineBlock, timezone: str) -> str:
        css_class = block.block_type.value
        start_time = to_timezone(block.start_time, timezone) or block.start_time
        end_time = to_timezone(block.end_time, timezone) or block.end_time
        time_str = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
        duration_m = int(block.duration.total_seconds() / 60)
        type_badge = f'<span class="badge badge-{css_class}">{block.block_type.value}</span>'

        title = block.metadata.get("title", "")
        details = ""

        if isinstance(block, EpisodeBlock):
            title = block.metadata.get("title", f"S{block.season_number}E{block.episode_number}")
            show_name = block.metadata.get("show_name", "")
            details = f"{show_name} S{block.season_number}E{block.episode_number}"
        elif isinstance(block, MovieBlock):
            title = block.metadata.get("title", "Movie")
            details = f"{block.runtime_seconds // 60}m"
        elif isinstance(block, AdBlock):
            title = f"Ad Break ({block.ad_count} spots)"
            details = f"{block.total_duration_seconds // 60}m"
        elif isinstance(block, StationIDBlock):
            title = "Station ID"
        elif isinstance(block, FillerBlock):
            title = f"Filler ({block.filler_type.value})"
        elif isinstance(block, OfflineBlock):
            title = "Offline"
            details = block.reason
        elif isinstance(block, SlotBlock):
            title = "Unfilled Slot"
            details = block.metadata.get("daypart", "")

        return (
            f'<tr class="{css_class}">'
            f"<td>{time_str}</td><td>{type_badge}</td>"
            f"<td>{title}</td><td>{duration_m}m</td>"
            f"<td>{details}</td></tr>"
        )
