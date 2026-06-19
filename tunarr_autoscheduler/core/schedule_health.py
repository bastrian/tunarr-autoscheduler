from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from tunarr_autoscheduler.core.timeline import Timeline
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


def build_schedule_health(
    version_meta: dict[str, object],
    timeline: Timeline,
) -> dict[str, object]:
    total_seconds = sum(max(0.0, block.duration.total_seconds()) for block in timeline.blocks)
    type_seconds: defaultdict[str, float] = defaultdict(float)
    type_counts: Counter[str] = Counter()
    notes: Counter[str] = Counter()
    daypart_overruns = 0
    for block in timeline.blocks:
        public_type = health_block_type(block)
        seconds = max(0.0, block.duration.total_seconds())
        type_seconds[public_type] += seconds
        type_counts[public_type] += 1
        note = str(block.metadata.get("note") or "").strip()
        if note:
            notes[note] += 1
        if crosses_declared_daypart_boundary(block):
            daypart_overruns += 1

    content_seconds = type_seconds["episode"] + type_seconds["movie"]
    standby_seconds = type_seconds["offline"]
    filler_seconds = type_seconds["filler"] + type_seconds["slot"]
    ad_seconds = type_seconds["ad"]
    content_pct = percent(content_seconds, total_seconds)
    standby_pct = percent(standby_seconds, total_seconds)
    filler_pct = percent(filler_seconds, total_seconds)
    ad_pct = percent(ad_seconds, total_seconds)
    issues: list[str] = []
    level = "success"
    if total_seconds <= 0:
        level = "danger"
        issues.append("Schedule has no duration.")
    if standby_pct >= 40:
        level = "danger"
        issues.append(f"Standby/off-air is high at {standby_pct:.1f}%.")
    elif standby_pct >= 20:
        level = "warning"
        issues.append(f"Standby/off-air is elevated at {standby_pct:.1f}%.")
    if filler_pct >= 10:
        level = max_level(level, "warning")
        issues.append(f"Unfilled/filler time is {filler_pct:.1f}%.")
    if notes.get("no_movie_fits_slot", 0):
        level = max_level(level, "danger")
        issues.append(f"{notes['no_movie_fits_slot']} movie slot(s) could not fit a movie.")
    if notes.get("no_movies_available", 0):
        level = max_level(level, "danger")
        issues.append(f"{notes['no_movies_available']} movie slot(s) had no movies available.")
    if notes.get("no_unused_episodes_available", 0):
        level = max_level(level, "warning")
        issues.append(
            f"{notes['no_unused_episodes_available']} series slot(s) had no unused episode.",
        )
    if daypart_overruns:
        level = max_level(level, "warning")
        issues.append(f"{daypart_overruns} block(s) run past their configured daypart boundary.")
    if not issues:
        issues.append("No obvious schedule health issues detected.")

    return {
        "version": int(str(version_meta["version"])),
        "status": version_meta["status"],
        "level": level,
        "summary": health_summary(level),
        "metrics": [
            {"label": "Content", "value": f"{content_pct:.1f}%"},
            {"label": "Standby", "value": f"{standby_pct:.1f}%"},
            {"label": "Ads", "value": f"{ad_pct:.1f}%"},
            {"label": "Filler", "value": f"{filler_pct:.1f}%"},
        ],
        "issues": issues,
        "counts": {
            "episodes": type_counts["episode"],
            "movies": type_counts["movie"],
            "offline": type_counts["offline"],
            "ads": type_counts["ad"],
        },
    }


def health_block_type(block: TimelineBlock) -> str:
    if isinstance(block, EpisodeBlock):
        return "episode"
    if isinstance(block, MovieBlock):
        return "movie"
    if isinstance(block, OfflineBlock):
        return "offline"
    if isinstance(block, AdBlock):
        return "ad"
    if isinstance(block, FillerBlock):
        return "filler"
    if isinstance(block, StationIDBlock):
        return "station_id"
    if isinstance(block, SlotBlock):
        return "slot"
    return "other"


def crosses_declared_daypart_boundary(block: TimelineBlock) -> bool:
    metadata = block.metadata or {}
    if metadata.get("variable_movie_duration"):
        return False
    raw = metadata.get("daypart_boundary")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        boundary = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return bool(block.end_time > boundary)


def percent(seconds: float, total_seconds: float) -> float:
    if total_seconds <= 0:
        return 0.0
    return seconds / total_seconds * 100


def max_level(current: str, candidate: str) -> str:
    order = {"success": 0, "warning": 1, "danger": 2}
    return candidate if order[candidate] > order[current] else current


def health_summary(level: str) -> str:
    return {
        "success": "Looks healthy",
        "warning": "Needs attention",
        "danger": "Likely broken",
    }.get(level, "Needs attention")


def metric_map(health: dict[str, Any]) -> dict[str, str]:
    return {
        str(metric["label"]): str(metric["value"])
        for metric in health.get("metrics", [])
        if isinstance(metric, dict)
    }
