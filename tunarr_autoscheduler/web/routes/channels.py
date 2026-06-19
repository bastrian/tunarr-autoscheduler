from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from enum import Enum
from html import escape
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from tunarr_autoscheduler.core.schedule_health import build_schedule_health
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
from tunarr_autoscheduler.models.config import (
    AutomaticFollowUpConfig,
    ChannelConfig,
    DayOfWeek,
    DaypartTemplate,
)
from tunarr_autoscheduler.plugins.html_preview import HTMLPreview
from tunarr_autoscheduler.recommendations.profiles import BUILT_IN_PROFILES
from tunarr_autoscheduler.web.routes.recommendations import (
    _engine as _recommendation_engine,
)
from tunarr_autoscheduler.web.routes.recommendations import (
    _infer_context_defaults as _infer_recommendation_defaults,
)

router = APIRouter(tags=["channels"])


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_schedule(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    active_job = core.job_manager.get_active_job(channel_id)
    versions = await core.state.list_versions(channel_id)
    pending_version = next(
        (version for version in versions if version["status"] in {"draft", "approved"}),
        None,
    )
    active_version = next(
        (version for version in versions if version["status"] == "uploaded"),
        None,
    )
    follow_up_context = await core.state.get_follow_up_context(channel_id)
    schedule_health = await _schedule_health_cards(
        core,
        channel_id,
        [pending_version, active_version],
    )
    template = request.app.state.templates.get_template("channel.html")
    return HTMLResponse(template.render(
        request=request,
        channel=channel,
        active_job=active_job,
        pending_version=pending_version,
        active_version=active_version,
        versions=versions,
        follow_up_context=follow_up_context,
        schedule_health=schedule_health,
        timezone=core.config_manager.config().timezone,
    ))


@router.get("/channels/{channel_id}/config", response_class=HTMLResponse)
async def channel_config_form(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    template = request.app.state.templates.get_template("channel_config.html")
    filler_fit = await _load_filler_fit(
        core, channel.ads.filler_list_id,
    ) if channel.ads.filler_list_id else {}
    daypart_rows = _daypart_rows(channel)
    structural_warnings = _daypart_warnings(daypart_rows)
    content_warnings_by_daypart = await _recommendation_content_warning_map(request, channel)
    daypart_fix_suggestions = await _recommendation_daypart_fix_suggestions(request, channel)
    content_warnings = [
        warning
        for warnings in content_warnings_by_daypart.values()
        for warning in warnings
    ]
    return HTMLResponse(template.render(
        request=request,
        channel=channel,
        daypart_rows=daypart_rows,
        error=None,
        saved=request.query_params.get("saved") == "1",
        imported=request.query_params.get("imported") == "1",
        import_error=request.query_params.get("import_error", ""),
        return_to=_safe_return_to(request.query_params.get("return_to")),
        custom_show_options=await _load_custom_show_options(core),
        playlist_options=await _load_playlist_options(core, channel.id),
        filler_list_options=await _load_filler_list_options(core),
        channel_profiles=_channel_profile_options(),
        daypart_warnings=structural_warnings + content_warnings,
        daypart_content_warnings=content_warnings_by_daypart,
        daypart_fix_suggestions=daypart_fix_suggestions,
        ad_plan=_ad_plan_estimate(channel, filler_fit),
        day_options=[
            ("mon", "Mon"),
            ("tue", "Tue"),
            ("wed", "Wed"),
            ("thu", "Thu"),
            ("fri", "Fri"),
            ("sat", "Sat"),
            ("sun", "Sun"),
        ],
        dayparts_yaml=yaml.safe_dump(
            [d.model_dump(mode="json") for d in channel.dayparts],
            sort_keys=False,
        ),
        rotations_yaml=yaml.safe_dump(
            [r.model_dump(mode="json") for r in channel.rotations],
            sort_keys=False,
        ),
        ads_yaml=yaml.safe_dump(channel.ads.model_dump(mode="json"), sort_keys=False),
        pipeline_text="\n".join(channel.pipeline),
    ))


@router.get("/channels/{channel_id}/config/export")
async def channel_config_export(request: Request, channel_id: str) -> JSONResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    response = JSONResponse({
        "schema": "tunarr_autoscheduler.channel_config.v1",
        "channel": channel.model_dump(mode="json"),
    })
    filename = _safe_filename(channel.name or channel.id)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{filename}-channel-config.json"'
    )
    return response


@router.post("/channels/{channel_id}/config/import", response_class=HTMLResponse)
async def channel_config_import(request: Request, channel_id: str) -> Response:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)
    form = await request.form()
    raw = str(form.get("import_json", "")).strip()
    if not raw:
        return RedirectResponse("/?import_error=empty", status_code=303)
    try:
        payload = json.loads(raw)
        raw_channel = payload.get("channel") if isinstance(payload, dict) else payload
        if not isinstance(raw_channel, dict):
            raise ValueError("Import JSON must contain a channel object")
        updated = ChannelConfig.model_validate({
            **raw_channel,
            "id": channel_id,
        })
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        return RedirectResponse("/?import_error=invalid", status_code=303)

    channels = core.config_manager.config().channels
    for index, existing in enumerate(channels):
        if existing.id == channel_id:
            channels[index] = updated
            break
    core.config_manager.save()
    return RedirectResponse("/?imported=1", status_code=303)


@router.post("/channels/{channel_id}/config", response_class=HTMLResponse)
async def channel_config_save(request: Request, channel_id: str) -> Response:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    form = await request.form()
    try:
        if "ads_enabled" in form or "ads_filler_list_id" in form:
            ads = {
                "enabled": form.get("ads_enabled") == "on",
                "filler_list_id": str(form.get("ads_filler_list_id", "")).strip(),
                "ad_density": _parse_float(form.get("ads_ad_density"), channel.ads.ad_density),
                "break_after_programs": _parse_int(
                    form.get("ads_break_after_programs"), channel.ads.break_after_programs,
                ),
                "max_total_minutes": _parse_int(
                    form.get("ads_max_total_minutes"), channel.ads.max_total_minutes,
                ),
                "min_total_minutes": _parse_int(
                    form.get("ads_min_total_minutes"), channel.ads.min_total_minutes,
                ),
                "max_ad_break_duration_minutes": _parse_int(
                    form.get("ads_max_ad_break_duration_minutes"),
                    channel.ads.max_ad_break_duration_minutes,
                ),
                "min_ad_break_duration_minutes": _parse_int(
                    form.get("ads_min_ad_break_duration_minutes"),
                    channel.ads.min_ad_break_duration_minutes,
                ),
            }
        else:
            ads = _parse_yaml_dict(str(form.get("ads_yaml", "")))
        scheduling_value = str(form.get("scheduling_enabled", "false"))
        values = {
            "name": str(form.get("name", "")).strip(),
            "channel_profile": _channel_profile_value(form.get("channel_profile")),
            "scheduling_enabled": scheduling_value in {"true", "on"},
            "public_epg_enabled": (
                form.get("public_epg_enabled") == "on"
                if "public_epg_submitted" in form
                else channel.public_epg_enabled
            ),
            "public_epg_order": _parse_int(
                form.get("public_epg_order"), channel.public_epg_order,
            ),
            "public_epg_logo_url": str(
                form.get("public_epg_logo_url", channel.public_epg_logo_url),
            ).strip(),
            "schedule_horizon_days": _parse_int(form.get("schedule_horizon_days"), 1),
            "automatic_follow_up": AutomaticFollowUpConfig(
                enabled=(
                    form.get("auto_follow_up_enabled") == "on"
                    if "auto_follow_up_submitted" in form
                    else channel.automatic_follow_up.enabled
                ),
                auto_approve=(
                    form.get("auto_follow_up_auto_approve") == "on"
                    if "auto_follow_up_submitted" in form
                    else channel.automatic_follow_up.auto_approve
                ),
                auto_upload=(
                    form.get("auto_follow_up_auto_upload") == "on"
                    if "auto_follow_up_submitted" in form
                    else channel.automatic_follow_up.auto_upload
                ),
                warning_hours=max(
                    1,
                    _parse_int(
                        form.get("auto_follow_up_warning_hours"),
                        channel.automatic_follow_up.warning_hours,
                    ),
                ),
            ),
            "standby_custom_show_id": str(
                form.get("standby_custom_show_id", channel.standby_custom_show_id),
            ).strip(),
            "dayparts": _parse_dayparts_form(
                form,
                channel.dayparts,
                str(form.get("dayparts_yaml", "")),
            ),
            "rotations": _parse_yaml_list(str(form.get("rotations_yaml", ""))),
            "ads": ads,
            "continuity": {
                "enabled": form.get("continuity_enabled") == "on",
                "frequency": _parse_int(
                    form.get("continuity_frequency"), channel.continuity.frequency,
                ),
                "station_id_custom_show_id": str(
                    form.get(
                        "continuity_station_id_custom_show_id",
                        channel.continuity.station_id_custom_show_id,
                    ),
                ).strip(),
                "bumper_custom_show_id": str(
                    form.get(
                        "continuity_bumper_custom_show_id",
                        channel.continuity.bumper_custom_show_id,
                    ),
                ).strip(),
                "station_id_clip_ids": _parse_csv(
                    form.get("continuity_station_id_clip_ids"),
                ),
                "bumper_clip_ids": _parse_csv(form.get("continuity_bumper_clip_ids")),
            },
            "pipeline": [
                line.strip()
                for line in str(form.get("pipeline_text", "")).splitlines()
                if line.strip()
            ],
        }
        updated = type(channel).model_validate({
            **channel.model_dump(mode="python"),
            **values,
        })
    except (TypeError, ValueError, yaml.YAMLError, ValidationError):
        template = request.app.state.templates.get_template("channel_config.html")
        filler_fit = await _load_filler_fit(
            core, channel.ads.filler_list_id,
        ) if channel.ads.filler_list_id else {}
        daypart_rows = _daypart_rows(channel)
        structural_warnings = _daypart_warnings(daypart_rows)
        content_warnings_by_daypart = await _recommendation_content_warning_map(request, channel)
        daypart_fix_suggestions = await _recommendation_daypart_fix_suggestions(request, channel)
        content_warnings = [
            warning
            for warnings in content_warnings_by_daypart.values()
            for warning in warnings
        ]
        return HTMLResponse(template.render(
            request=request,
            channel=channel,
            daypart_rows=daypart_rows,
            error="Invalid channel config. Check the submitted fields and YAML sections.",
            saved=False,
            imported=False,
            import_error="",
            return_to=_safe_return_to(str(form.get("return_to", ""))),
            custom_show_options=await _load_custom_show_options(core),
            playlist_options=await _load_playlist_options(core, channel.id),
            filler_list_options=await _load_filler_list_options(core),
            channel_profiles=_channel_profile_options(),
            daypart_warnings=structural_warnings + content_warnings,
            daypart_content_warnings=content_warnings_by_daypart,
            daypart_fix_suggestions=daypart_fix_suggestions,
            ad_plan=_ad_plan_estimate(channel, filler_fit),
            day_options=[
                ("mon", "Mon"),
                ("tue", "Tue"),
                ("wed", "Wed"),
                ("thu", "Thu"),
                ("fri", "Fri"),
                ("sat", "Sat"),
                ("sun", "Sun"),
            ],
            dayparts_yaml=str(form.get("dayparts_yaml", "")),
            rotations_yaml=str(form.get("rotations_yaml", "")),
            ads_yaml=str(form.get("ads_yaml", "")),
            pipeline_text=str(form.get("pipeline_text", "")),
        ), status_code=400)

    channels = core.config_manager.config().channels
    for index, existing in enumerate(channels):
        if existing.id == channel_id:
            channels[index] = updated
            break
    core.config_manager.save()
    return RedirectResponse("/?saved=1", status_code=303)


@router.post("/channels/{channel_id}/toggle", response_class=HTMLResponse)
async def toggle_channel(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    channel.scheduling_enabled = not channel.scheduling_enabled
    core.config_manager.save()
    label = "Enabled" if channel.scheduling_enabled else "Disabled"
    badge_class = "badge-enabled" if channel.scheduling_enabled else "badge-disabled"
    button_label = "Disable" if channel.scheduling_enabled else "Enable"
    return HTMLResponse(
        _notice_html("success", f"{channel.name or channel.id} {label.lower()}.")
        + f"""
<span id="channel-status-{channel.id}" hx-swap-oob="outerHTML"
      class="badge {badge_class}">{label}</span>
<button id="channel-toggle-{channel.id}" type="button" class="btn btn-sm btn-outline-secondary"
   hx-post="/channels/{channel.id}/toggle" hx-target="#notifications" hx-swap="innerHTML">
  {button_label}
</button>
""",
    )


@router.post("/channels/{channel_id}/generate", response_class=HTMLResponse)
async def generate_schedule(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    if core.job_manager.is_running(channel_id):
        return HTMLResponse("Generation already in progress", status_code=409)

    generation_mode = request.query_params.get("mode", "fresh")
    if generation_mode not in {"fresh", "follow_up"}:
        return _notice("danger", "Unknown generation mode.", status_code=400)
    form = await request.form()
    parent_version = _parse_optional_int(
        request.query_params.get("parent_version") or form.get("parent_version"),
    )
    if generation_mode == "fresh":
        parent_version = None
    elif parent_version is not None:
        follow_up_context = await core.state.get_follow_up_context(
            channel_id, parent_version=parent_version,
        )
        if follow_up_context is None:
            return _notice(
                "danger",
                f"Schedule version {parent_version} is not available as a follow-up base.",
                status_code=409,
            )
    try:
        job = await core.job_manager.start_generation(
            channel, generation_mode, parent_version=parent_version,
        )
    except ValueError:
        if not channel.scheduling_enabled:
            return _notice("danger", "Channel scheduling disabled.", status_code=409)
        return _notice(
            "danger",
            "Generation could not be started with the selected options.",
            status_code=409,
        )
    mode_label = "Follow-up" if generation_mode == "follow_up" else "New"
    parent_label = f" after version {parent_version}" if parent_version else ""
    return HTMLResponse(
        _notice_html(
            "info",
            f"{mode_label} generation{parent_label} started: "
            f"{job.id} for {channel.name or channel.id}.",
        )
        + _job_status_html(channel, job, oob=True),
    )


@router.post("/channels/{channel_id}/cancel", response_class=HTMLResponse)
async def cancel_generation(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    if not _find_channel(core, channel_id):
        return HTMLResponse("Channel not found", status_code=404)

    cancelled = await core.job_manager.cancel_generation(channel_id)
    if not cancelled:
        return _notice("warning", "No generation in progress.", status_code=404)
    return _notice("info", "Generation cancellation requested.")


@router.get("/channels/{channel_id}/job-status", response_class=HTMLResponse)
async def channel_job_status(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    active_job = core.job_manager.get_active_job(channel_id)
    if active_job:
        return HTMLResponse(_job_status_html(channel, active_job))

    recent_jobs = await core.state.list_recent_jobs(channel_id, limit=1)
    latest_job = recent_jobs[0] if recent_jobs else None
    return HTMLResponse(_job_status_html(channel, latest_job))


@router.get("/channels/{channel_id}/preview/{generation_id}", response_class=HTMLResponse)
async def preview_schedule(request: Request, channel_id: str, generation_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    last_stage = core.checkpoint_manager.get_last_stage(channel_id, generation_id)
    if not last_stage:
        return HTMLResponse("No preview available", status_code=404)

    snapshot = core.checkpoint_manager.load(channel_id, generation_id, last_stage)
    if not snapshot:
        return HTMLResponse("No preview available", status_code=404)

    from tunarr_autoscheduler.core.timeline import Timeline
    timeline = Timeline.from_snapshot(snapshot)
    preview = HTMLPreview()
    html = preview.render(timeline, channel.name)
    return HTMLResponse(html)


def _find_channel(core: Any, channel_id: str) -> Any:
    for c in core.config_manager.config().channels:
        if c.id == channel_id:
            return c
    return None


def _channel_profile_options() -> list[dict[str, str]]:
    return [
        {
            "value": "advanced",
            "label": "Advanced",
            "description": "Show every scheduler option.",
        },
        {
            "value": "general_tv",
            "label": "General TV",
            "description": "Balanced series, movies, ads, and continuity.",
        },
        {
            "value": "movie_channel",
            "label": "Movie Channel",
            "description": "Movie-first scheduling with standby/off-air support.",
        },
        {
            "value": "series_marathon",
            "label": "Series Marathon",
            "description": "Episode-first scheduling with optional ad breaks.",
        },
    ]


def _channel_profile_value(value: object) -> str:
    selected = str(value or "advanced").strip()
    allowed = {option["value"] for option in _channel_profile_options()}
    return selected if selected in allowed else "advanced"


async def _schedule_health_cards(
    core: Any,
    channel_id: str,
    versions: list[dict[str, object] | None],
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    seen: set[int] = set()
    for version_meta in versions:
        if not version_meta:
            continue
        version = int(str(version_meta["version"]))
        if version in seen:
            continue
        seen.add(version)
        timeline_json = await core.state.get_schedule_version(channel_id, version)
        if not timeline_json:
            continue
        try:
            timeline = Timeline.from_snapshot(json.loads(timeline_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            cards.append({
                "version": version,
                "status": version_meta["status"],
                "level": "danger",
                "summary": "Schedule health could not be read.",
                "metrics": [],
                "issues": ["Timeline data is invalid."],
            })
            continue
        cards.append(build_schedule_health(version_meta, timeline))
    return cards


def _schedule_health_card(
    version_meta: dict[str, object],
    timeline: Timeline,
) -> dict[str, object]:
    total_seconds = sum(max(0.0, block.duration.total_seconds()) for block in timeline.blocks)
    type_seconds: defaultdict[str, float] = defaultdict(float)
    type_counts: Counter[str] = Counter()
    notes: Counter[str] = Counter()
    daypart_overruns = 0
    for block in timeline.blocks:
        public_type = _health_block_type(block)
        seconds = max(0.0, block.duration.total_seconds())
        type_seconds[public_type] += seconds
        type_counts[public_type] += 1
        note = str(block.metadata.get("note") or "").strip()
        if note:
            notes[note] += 1
        if _crosses_declared_daypart_boundary(block):
            daypart_overruns += 1

    content_seconds = type_seconds["episode"] + type_seconds["movie"]
    standby_seconds = type_seconds["offline"]
    filler_seconds = type_seconds["filler"] + type_seconds["slot"]
    ad_seconds = type_seconds["ad"]
    content_pct = _percent(content_seconds, total_seconds)
    standby_pct = _percent(standby_seconds, total_seconds)
    filler_pct = _percent(filler_seconds, total_seconds)
    ad_pct = _percent(ad_seconds, total_seconds)
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
        level = _max_level(level, "warning")
        issues.append(f"Unfilled/filler time is {filler_pct:.1f}%.")
    if notes.get("no_movie_fits_slot", 0):
        level = _max_level(level, "danger")
        issues.append(f"{notes['no_movie_fits_slot']} movie slot(s) could not fit a movie.")
    if notes.get("no_movies_available", 0):
        level = _max_level(level, "danger")
        issues.append(f"{notes['no_movies_available']} movie slot(s) had no movies available.")
    if notes.get("no_unused_episodes_available", 0):
        level = _max_level(level, "warning")
        issues.append(
            f"{notes['no_unused_episodes_available']} series slot(s) had no unused episode.",
        )
    if daypart_overruns:
        level = _max_level(level, "warning")
        issues.append(f"{daypart_overruns} block(s) run past their configured daypart boundary.")
    if not issues:
        issues.append("No obvious schedule health issues detected.")

    return {
        "version": int(str(version_meta["version"])),
        "status": version_meta["status"],
        "level": level,
        "summary": _health_summary(level),
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


def _health_block_type(block: TimelineBlock) -> str:
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


def _crosses_declared_daypart_boundary(block: TimelineBlock) -> bool:
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


def _percent(seconds: float, total_seconds: float) -> float:
    if total_seconds <= 0:
        return 0.0
    return seconds / total_seconds * 100


def _max_level(current: str, candidate: str) -> str:
    order = {"success": 0, "warning": 1, "danger": 2}
    return candidate if order[candidate] > order[current] else current


def _health_summary(level: str) -> str:
    return {
        "success": "Looks healthy",
        "warning": "Needs attention",
        "danger": "Likely broken",
    }.get(level, "Needs attention")


class _ChannelWithDayparts(Protocol):
    dayparts: list[DaypartTemplate]


def _daypart_rows(channel: _ChannelWithDayparts) -> list[DaypartTemplate]:
    if channel.dayparts:
        return channel.dayparts
    return [
        DaypartTemplate(
            name="new-daypart",
            days=list(DayOfWeek),
            start_time="06:00",
            end_time="12:00",
        ),
    ]


def _ad_plan_estimate(
    channel: ChannelConfig,
    filler_fit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    filler_fit = filler_fit or {}
    horizon_days = max(1, channel.schedule_horizon_days)
    rows: list[dict[str, Any]] = []
    eligible_minutes = 0.0
    raw_target_minutes = 0.0
    for daypart in _daypart_rows(channel):
        duration = _daypart_duration_minutes(daypart)
        weekly_occurrences = len(daypart.days)
        horizon_minutes = duration * weekly_occurrences / 7 * horizon_days
        target = 0.0 if daypart.off_air else horizon_minutes * max(0.0, daypart.ad_density)
        if not daypart.off_air:
            eligible_minutes += horizon_minutes
            raw_target_minutes += target
        rows.append({
            "name": daypart.name,
            "window": f"{daypart.start_time}-{daypart.end_time}",
            "days": len(daypart.days),
            "eligible": not daypart.off_air,
            "content_minutes": int(round(horizon_minutes)),
            "target_minutes": int(round(target)),
            "density": daypart.ad_density,
        })

    target_minutes = max(channel.ads.min_total_minutes, int(round(raw_target_minutes)))
    target_source = "density"
    if channel.ads.min_total_minutes > int(round(raw_target_minutes)):
        target_source = "minimum"
    if channel.ads.max_total_minutes > 0:
        if target_minutes > channel.ads.max_total_minutes:
            target_source = "maximum"
        target_minutes = min(target_minutes, channel.ads.max_total_minutes)
    warnings = []
    if not channel.ads.enabled:
        warnings.append("Ads are disabled for this channel.")
    if channel.ads.enabled and not channel.ads.filler_list_id:
        warnings.append(
            "No Tunarr filler list is selected; generated ad blocks use fallback timing.",
        )
    if (
        channel.ads.max_total_minutes
        and channel.ads.min_total_minutes > channel.ads.max_total_minutes
    ):
        warnings.append("Minimum total ad minutes is higher than maximum total ad minutes.")
    if channel.ads.min_ad_break_duration_minutes > channel.ads.max_ad_break_duration_minutes:
        warnings.append("Minimum break length is higher than maximum break length.")
    if channel.ads.enabled and eligible_minutes <= 0:
        warnings.append("No non-off-air daypart can receive ads.")
    if filler_fit.get("error"):
        warnings.append(str(filler_fit["error"]))
    elif channel.ads.enabled and channel.ads.filler_list_id:
        spot_count = int(filler_fit.get("spot_count", 0))
        shortest = int(filler_fit.get("shortest_seconds", 0))
        longest = int(filler_fit.get("longest_seconds", 0))
        min_break_seconds = channel.ads.min_ad_break_duration_minutes * 60
        max_break_seconds = channel.ads.max_ad_break_duration_minutes * 60
        if spot_count <= 0:
            warnings.append("Selected filler list has no usable spots with durations.")
        if shortest and shortest > max_break_seconds:
            warnings.append("All filler spots are longer than the configured maximum break length.")
        if longest and longest < min_break_seconds:
            warnings.append(
                "Filler spots are shorter than the configured minimum break length; "
                "multiple spots are required.",
            )
    return {
        "enabled": channel.ads.enabled,
        "horizon_days": horizon_days,
        "eligible_minutes": int(round(eligible_minutes)),
        "target_minutes": target_minutes,
        "target_source": target_source,
        "max_total_reached": bool(channel.ads.max_total_minutes and target_source == "maximum"),
        "raw_target_minutes": int(round(raw_target_minutes)),
        "min_total_minutes": channel.ads.min_total_minutes,
        "max_total_minutes": channel.ads.max_total_minutes,
        "break_after_programs": channel.ads.break_after_programs,
        "break_range": (
            channel.ads.min_ad_break_duration_minutes,
            channel.ads.max_ad_break_duration_minutes,
        ),
        "rows": rows,
        "warnings": warnings,
        "filler_fit": filler_fit,
    }


async def _recommendation_content_warnings(request: Request, channel: ChannelConfig) -> list[str]:
    warnings_by_daypart = await _recommendation_content_warning_map(request, channel)
    return [
        warning
        for warnings in warnings_by_daypart.values()
        for warning in warnings
    ]


async def _recommendation_content_warning_map(
    request: Request,
    channel: ChannelConfig,
) -> dict[str, list[str]]:
    warnings_by_daypart: dict[str, list[str]] = defaultdict(list)
    playlist_counts = await _playlist_content_counts(request.app.state.core)
    engine = None
    recommendation_cache: dict[tuple[str, str], int] = {}

    for daypart in channel.dayparts:
        if daypart.off_air:
            continue
        wanted_type = _daypart_recommendation_media_type(channel, daypart)
        minimum = _minimum_daypart_sources(channel, daypart, wanted_type)
        if minimum <= 0:
            continue

        if daypart.playlist_ids:
            count = sum(
                playlist_counts.get(str(playlist_id), {}).get(wanted_type, 0)
                for playlist_id in daypart.playlist_ids
            )
            source = "selected scheduler playlist sources"
        elif daypart.custom_show_list_ids:
            continue
        else:
            defaults = _infer_recommendation_defaults(request, channel.id, daypart.name)
            profile_id = defaults["profile"]
            media_type = (
                wanted_type
                if wanted_type in {"movie", "series"}
                else defaults["media_type"]
            )
            key = (profile_id, media_type)
            if key not in recommendation_cache:
                if engine is None:
                    engine = await _recommendation_engine(request)
                results = await engine.run(profile_id, limit=10_000)
                recommendation_cache[key] = len([
                    result for result in results
                    if media_type == "all" or result.candidate.media_type == media_type
                ])
            count = recommendation_cache[key]
            profile = BUILT_IN_PROFILES.get(profile_id)
            source = f"{profile.name if profile else profile_id} recommendations"

        if count < minimum:
            label = "movie" if wanted_type == "movie" else "series"
            warnings_by_daypart[daypart.name].append(
                f"{daypart.name} has only {count} eligible {label} source"
                f"{'' if count == 1 else 's'} from {source}; "
                f"recommended minimum is {minimum}.",
            )
    return dict(warnings_by_daypart)


async def _recommendation_daypart_fix_suggestions(
    request: Request,
    channel: ChannelConfig,
) -> dict[str, list[dict[str, str]]]:
    suggestions: dict[str, list[dict[str, str]]] = defaultdict(list)
    playlist_counts = await _playlist_content_counts(request.app.state.core)
    for daypart in channel.dayparts:
        if daypart.off_air:
            continue
        wanted_type = _daypart_recommendation_media_type(channel, daypart)
        minimum = _minimum_daypart_sources(channel, daypart, wanted_type)
        if minimum <= 0:
            continue
        count = 0
        if daypart.playlist_ids:
            count = sum(
                playlist_counts.get(str(playlist_id), {}).get(wanted_type, 0)
                for playlist_id in daypart.playlist_ids
            )
        elif daypart.custom_show_list_ids:
            continue
        if count >= minimum:
            continue

        defaults = _infer_recommendation_defaults(request, channel.id, daypart.name)
        profile = _fix_profile_for_daypart(channel, daypart, defaults["profile"], wanted_type)
        query = urlencode({
            "preview": "1",
            "mode": "daypart",
            "builder_mode": "improve",
            "channel_id": channel.id,
            "channel_name": channel.name or channel.id,
            "profile": profile,
            "themes": _daypart_theme_for_fix(daypart, wanted_type),
            "seed": _daypart_theme_for_fix(daypart, wanted_type),
            "per_theme_limit": str(max(minimum * 2, 12)),
            "balance_mode": "movie_friendly" if wanted_type == "movie" else "series_heavy",
            "max_movies_per_theme": str(max(minimum * 2, 12)) if wanted_type == "movie" else "1",
            "min_series_per_theme": str(max(minimum, 3)),
            "replace_dayparts": "1",
        })
        label = "movie" if wanted_type == "movie" else "series"
        suggestions[daypart.name].append({
            "title": f"Build a fresh {label} source playlist",
            "message": (
                f"{daypart.name} has {count} selected {label} sources; "
                f"target is {minimum}."
            ),
            "href": f"/recommendations/builder?{query}",
            "action": "Build Fix",
            "direct": "1",
            "profile": profile,
            "media_type": wanted_type,
            "limit": str(max(minimum * 2, 12)),
        })
        recommendations_query = urlencode({
            "channel_id": channel.id,
            "daypart": daypart.name,
            "assign_to_daypart": "1",
            "profile": profile,
            "media_type": wanted_type,
        })
        suggestions[daypart.name].append({
            "title": "Browse recommendations manually",
            "message": "Review candidates first, then create and assign a playlist.",
            "href": f"/recommendations?{recommendations_query}",
            "action": "Review",
        })
    return dict(suggestions)


def _fix_profile_for_daypart(
    channel: ChannelConfig,
    daypart: DaypartTemplate,
    default_profile: str,
    wanted_type: str,
) -> str:
    if wanted_type == "movie":
        if channel.channel_profile == "movie_channel":
            return "movie-channel-pool"
        return "prime-time-movies"
    if channel.channel_profile == "series_marathon":
        return "series-marathon"
    name = daypart.name.lower()
    if "morning" in name:
        return "morning-sitcoms"
    if "afternoon" in name or "daytime" in name:
        return "afternoon-family"
    return default_profile or "series-marathon"


def _daypart_theme_for_fix(daypart: DaypartTemplate, wanted_type: str) -> str:
    if wanted_type == "movie":
        return f"{daypart.name} Movies"
    return daypart.name


async def _playlist_content_counts(core: Any) -> dict[str, dict[str, int]]:
    repo = getattr(core, "playlist_repo", None)
    if repo is None or not hasattr(repo, "list_all"):
        return {}
    counts: dict[str, dict[str, int]] = {}
    for playlist in await repo.list_all():
        by_type: dict[str, int] = {"movie": 0, "series": 0}
        for item in getattr(playlist, "items", []):
            media_type = str(getattr(item, "media_type", ""))
            if media_type in by_type:
                by_type[media_type] += 1
        counts[str(getattr(playlist, "id", ""))] = by_type
    return counts


def _daypart_recommendation_media_type(channel: ChannelConfig, daypart: DaypartTemplate) -> str:
    if daypart.content_mode == "movies" or (
        daypart.allow_movies and channel.channel_profile == "movie_channel"
    ):
        return "movie"
    return "series"


def _minimum_daypart_sources(
    channel: ChannelConfig,
    daypart: DaypartTemplate,
    wanted_type: str,
) -> int:
    if wanted_type == "movie":
        return 8 if channel.channel_profile == "movie_channel" else 5
    if channel.channel_profile == "series_marathon":
        return 3
    duration = _daypart_duration_minutes(daypart)
    if duration >= 6 * 60:
        return 8
    if duration >= 3 * 60:
        return 5
    return 3


def _daypart_warnings(dayparts: list[DaypartTemplate]) -> list[str]:
    warnings: list[str] = []
    if not dayparts:
        return ["No dayparts are configured; the standby source will cover the schedule."]
    names = Counter(daypart.name.strip().lower() for daypart in dayparts if daypart.name.strip())
    for name, count in sorted(names.items()):
        if count > 1:
            warnings.append(f"Duplicate daypart name: {name}.")
    for daypart in dayparts:
        duration = _daypart_duration_minutes(daypart)
        if not daypart.days:
            warnings.append(f"{daypart.name} has no active days.")
        if duration == 0:
            warnings.append(f"{daypart.name} starts and ends at the same time.")
        if daypart.slot_duration_minutes <= 0:
            warnings.append(f"{daypart.name} has a non-positive slot duration.")
        if duration > 0 and daypart.slot_duration_minutes > duration:
            warnings.append(
                f"{daypart.name} slot duration is longer than the daypart window.",
            )
        if daypart.content_mode == "movies" and not daypart.allow_movies:
            warnings.append(f"{daypart.name} is movie-only but movies are disabled.")
        if not daypart.allow_movies and daypart.movie_slot_count > 0:
            warnings.append(f"{daypart.name} has movie slots but movies are disabled.")
        if not daypart.allow_movies and daypart.variable_movie_duration:
            warnings.append(
                f"{daypart.name} has variable movie timing enabled but movies are disabled.",
            )
        if duration > 0 and daypart.end_tolerance_minutes >= duration:
            warnings.append(
                f"{daypart.name} end tolerance is longer than the daypart window.",
            )
        if daypart.off_air and not daypart.custom_show_list_ids:
            warnings.append(
                f"{daypart.name} is off-air but has no custom show loop selected.",
            )

    day_labels = {
        DayOfWeek.MON: "Mon",
        DayOfWeek.TUE: "Tue",
        DayOfWeek.WED: "Wed",
        DayOfWeek.THU: "Thu",
        DayOfWeek.FRI: "Fri",
        DayOfWeek.SAT: "Sat",
        DayOfWeek.SUN: "Sun",
    }
    intervals_by_day: dict[DayOfWeek, list[tuple[int, int, str]]] = {
        day: [] for day in DayOfWeek
    }
    days = list(DayOfWeek)
    for daypart in dayparts:
        start = _time_to_minutes(daypart.start_time)
        end = _time_to_minutes(daypart.end_time)
        if start == end:
            continue
        for day in daypart.days:
            if start < end:
                intervals_by_day[day].append((start, end, daypart.name))
                continue
            intervals_by_day[day].append((start, 1440, daypart.name))
            next_day = days[(days.index(day) + 1) % len(days)]
            intervals_by_day[next_day].append((0, end, daypart.name))

    for day, intervals in intervals_by_day.items():
        if not intervals:
            warnings.append(f"{day_labels[day]} has no daypart coverage.")
            continue
        sorted_intervals = sorted(intervals)
        cursor = 0
        for start, end, name in sorted_intervals:
            if start > cursor:
                warnings.append(
                    f"{day_labels[day]} has a gap from {_format_minute(cursor)} "
                    f"to {_format_minute(start)}.",
                )
            if start < cursor:
                warnings.append(f"{day_labels[day]} has overlapping coverage near {name}.")
            cursor = max(cursor, end)
        if cursor < 1440:
            warnings.append(
                f"{day_labels[day]} has a gap from {_format_minute(cursor)} to 24:00.",
            )
    return warnings


def _daypart_duration_minutes(daypart: DaypartTemplate) -> int:
    start = _time_to_minutes(daypart.start_time)
    end = _time_to_minutes(daypart.end_time)
    if start == end:
        return 0
    if end > start:
        return end - start
    return 1440 - start + end


def _time_to_minutes(raw: str) -> int:
    try:
        hour, minute = raw.split(":", 1)
        return max(0, min(1439, int(hour) * 60 + int(minute)))
    except (ValueError, TypeError):
        return 0


def _format_minute(value: int) -> str:
    if value >= 1440:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


def _parse_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _parse_optional_int(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _parse_dayparts_form(
    form: Any,
    existing_dayparts: list[Any],
    dayparts_yaml: str,
) -> list[dict[str, Any]]:
    if "daypart_indices" not in form and not _daypart_indices(form) and dayparts_yaml.strip():
        return _parse_yaml_list(dayparts_yaml)

    dayparts: list[dict[str, Any]] = []
    indices = _daypart_indices(form)
    for index in indices:
        existing = existing_dayparts[index] if index < len(existing_dayparts) else None
        days = [str(day) for day in form.getlist(f"daypart_days_{index}")]
        dayparts.append({
            "name": str(form.get(
                f"daypart_name_{index}", existing.name if existing else "new-daypart",
            )).strip(),
            "days": days,
            "start_time": str(form.get(
                f"daypart_start_{index}", existing.start_time if existing else "06:00",
            )).strip(),
            "end_time": str(form.get(
                f"daypart_end_{index}", existing.end_time if existing else "12:00",
            )).strip(),
            "content_mode": str(form.get(
                f"daypart_content_mode_{index}",
                existing.content_mode if existing else "series",
            )).strip(),
            "rotation": str(form.get(
                f"daypart_rotation_{index}", existing.rotation if existing else "default",
            )).strip(),
            "custom_show_list_ids": [
                str(item).strip()
                for item in form.getlist(f"daypart_custom_show_list_ids_{index}")
                if str(item).strip()
            ],
            "playlist_ids": [
                str(item).strip()
                for item in form.getlist(f"daypart_playlist_ids_{index}")
                if str(item).strip()
            ],
            "slot_duration_minutes": _parse_int(
                form.get(f"daypart_slot_duration_{index}"),
                existing.slot_duration_minutes if existing else 30,
            ),
            "allow_movies": form.get(f"daypart_allow_movies_{index}") == "on",
            "variable_movie_duration": (
                form.get(f"daypart_variable_movie_duration_{index}") == "on"
            ),
            "movie_selection": str(form.get(
                f"daypart_movie_selection_{index}",
                existing.movie_selection if existing else "best_fit",
            )).strip(),
            "movie_slot_count": _parse_int(
                form.get(f"daypart_movie_slot_count_{index}"),
                existing.movie_slot_count if existing else 0,
            ),
            "end_tolerance_minutes": _parse_int(
                form.get(f"daypart_end_tolerance_minutes_{index}"),
                existing.end_tolerance_minutes if existing else 0,
            ),
            "ad_density": _parse_float(
                form.get(f"daypart_ad_density_{index}"),
                existing.ad_density if existing else 0.08,
            ),
            "continuity_frequency": _parse_int(
                form.get(f"daypart_continuity_frequency_{index}"),
                existing.continuity_frequency if existing else 4,
            ),
            "off_air": form.get(f"daypart_off_air_{index}") == "on",
        })
    return dayparts


def _daypart_indices(form: Any) -> list[int]:
    raw = str(form.get("daypart_indices", "")).strip()
    if raw:
        return [
            index
            for value in raw.split(",")
            if value.strip().isdigit()
            for index in [int(value.strip())]
        ]
    indices = {
        int(match.group(1))
        for key in form.keys()
        if (match := re.fullmatch(r"daypart_name_(\d+)", str(key)))
    }
    return sorted(indices)


def _parse_yaml_list(raw: str) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(raw) if raw.strip() else []
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise ValueError("Expected a YAML list")
    if not all(isinstance(item, dict) for item in loaded):
        raise ValueError("Each YAML list item must be an object")
    return cast(list[dict[str, Any]], loaded)


def _parse_yaml_dict(raw: str) -> dict[str, Any]:
    loaded = yaml.safe_load(raw) if raw.strip() else {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Expected a YAML object")
    return cast(dict[str, Any], loaded)


def _parse_csv(value: object) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


async def _load_custom_show_options(core: Any) -> list[dict[str, str]]:
    client = getattr(core, "tunarr_client", None)
    if client is None or not hasattr(client, "get_custom_shows"):
        return []
    try:
        shows = await client.get_custom_shows()
    except Exception:
        return []
    return [
        {
            "id": str(show.get("id", "")),
            "name": str(show.get("name") or show.get("id", "")),
        }
        for show in shows
        if show.get("id")
    ]


async def _load_filler_list_options(core: Any) -> list[dict[str, str]]:
    client = getattr(core, "tunarr_client", None)
    if client is None or not hasattr(client, "get_filler_lists"):
        return []
    try:
        lists = await client.get_filler_lists()
    except Exception:
        return []
    return [
        {
            "id": str(filler_list.get("id", "")),
            "name": str(filler_list.get("name") or filler_list.get("id", "")),
        }
        for filler_list in lists
        if filler_list.get("id")
    ]


async def _load_filler_fit(core: Any, filler_list_id: str) -> dict[str, Any]:
    client = getattr(core, "tunarr_client", None)
    if client is None or not hasattr(client, "get_filler_list_programs"):
        return {}
    try:
        programs = await client.get_filler_list_programs(filler_list_id)
    except Exception:
        return {"error": "Could not load the selected Tunarr filler list for fit checks."}
    durations = [
        duration
        for program in programs
        for duration in [_filler_program_duration_seconds(program)]
        if duration > 0
    ]
    if not durations:
        return {
            "filler_list_id": filler_list_id,
            "spot_count": 0,
            "shortest_seconds": 0,
            "longest_seconds": 0,
            "total_seconds": 0,
        }
    return {
        "filler_list_id": filler_list_id,
        "spot_count": len(durations),
        "shortest_seconds": min(durations),
        "longest_seconds": max(durations),
        "total_seconds": sum(durations),
    }


def _filler_program_duration_seconds(program: dict[str, Any]) -> int:
    source = program.get("program") if program.get("type") == "filler" else program
    if not isinstance(source, dict):
        source = program
    raw = source.get("duration") or program.get("duration")
    if not isinstance(raw, (str, int, float)):
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    if value > 60 * 60:
        value = value / 1000
    return int(value)


async def _load_playlist_options(core: Any, channel_id: str = "") -> list[dict[str, str]]:
    repo = getattr(core, "playlist_repo", None)
    if repo is None:
        return []
    playlists = await repo.list_all()
    channels = {
        str(getattr(channel, "id", "")): str(
            getattr(channel, "name", "") or getattr(channel, "id", ""),
        )
        for channel in getattr(core.config_manager.config(), "channels", [])
    }
    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "category": getattr(playlist, "category_name", "") or "No category",
            "category_id": getattr(playlist, "category_id", ""),
            "tags": ", ".join(getattr(playlist, "tags", [])),
            "channel_scope": getattr(playlist, "channel_scope", ""),
            "scope_label": (
                "Global" if not getattr(playlist, "channel_scope", "")
                else channels.get(
                    str(getattr(playlist, "channel_scope", "")),
                    str(getattr(playlist, "channel_scope", "")),
                )
            ),
        }
        for playlist in playlists
        if not getattr(playlist, "channel_scope", "")
        or getattr(playlist, "channel_scope", "") == channel_id
    ]


def _notice(level: str, message: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_notice_html(level, message), status_code=status_code)


def _notice_html(level: str, message: str) -> str:
    safe_level = escape(level, quote=True)
    safe_message = escape(message)
    return (
        f'<div class="alert alert-{safe_level} alert-dismissible fade show" role="alert">'
        f'{safe_message}'
        '<button type="button" class="btn-close" data-bs-dismiss="alert" '
        'aria-label="Close"></button>'
        '</div>'
    )


def _job_status_html(channel: Any, job: Any, oob: bool = False) -> str:
    channel_id = escape(str(channel.id))
    oob_attr = ' hx-swap-oob="outerHTML"' if oob else ""
    if job is None:
        return (
            f'<div id="job-status-{channel_id}"{oob_attr} class="small text-secondary" '
            f'hx-get="/channels/{channel_id}/job-status" hx-trigger="load, every 3s" '
            'hx-swap="outerHTML">No recent generation.</div>'
        )

    status = _job_value(job, "status")
    stage = _job_value(job, "current_stage") or "-"
    error = _job_value(job, "error_message")
    version = _job_value(job, "schedule_version")
    version_id = _job_value(job, "schedule_version_id")
    job_id = _job_value(job, "id")
    is_running = status == "running"
    badge_class = {
        "running": "bg-info text-dark",
        "completed": "bg-success",
        "failed": "bg-danger",
        "cancelled": "bg-secondary",
    }.get(status, "bg-secondary")
    schedule_link = ""
    if version:
        schedule_link = (
            f'<a class="ms-2" href="/schedules/{channel_id}/preview/{escape(version)}">'
            f'v{escape(version)}</a>'
        )
    elif version_id:
        schedule_link = f'<span class="ms-2 text-secondary">{escape(version_id)}</span>'

    cancel_button = ""
    if is_running:
        cancel_button = (
            f'<button type="button" class="btn btn-sm btn-outline-light ms-2" '
            f'hx-post="/channels/{channel_id}/cancel" hx-target="#notifications" '
            'hx-swap="innerHTML">Cancel</button>'
        )

    error_html = f'<div class="text-danger mt-1">{escape(error)}</div>' if error else ""
    return (
        f'<div id="job-status-{channel_id}"{oob_attr} class="small" '
        f'hx-get="/channels/{channel_id}/job-status" '
        f'hx-trigger="load, every 3s" hx-swap="outerHTML">'
        f'<span class="badge {badge_class}">{escape(status or "unknown")}</span>'
        f'<span class="ms-2">Stage: {escape(stage)}</span>'
        f'<span class="ms-2 text-secondary">Job: {escape(job_id[:8])}</span>'
        f'{schedule_link}{cancel_button}{error_html}</div>'
    )


def _job_value(job: Any, key: str) -> str:
    if isinstance(job, dict):
        value = job.get(key)
    else:
        value = getattr(job, key, None)
    if isinstance(value, Enum):
        value = value.value
    return "" if value is None else str(value)


def _safe_return_to(value: str | None) -> str:
    if value:
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            and not parsed.netloc
            and parsed.path.startswith("/")
            and not parsed.path.startswith("//")
        ):
            return value
    return "/"


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return filename or "channel"
