from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.core.timezones import to_timezone
from tunarr_autoscheduler.integrations.notifications import NotificationMessage
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

router = APIRouter(tags=["schedules"])
logger = logging.getLogger(__name__)


@router.get("/uploads", response_class=HTMLResponse)
async def upload_history(request: Request) -> HTMLResponse:
    core = request.app.state.core
    channel_id = request.query_params.get("channel_id") or None
    attempts = await core.state.list_upload_attempts(channel_id=channel_id, limit=100)
    channel_names = {
        channel.id: channel.name
        for channel in core.config_manager.config().channels
    }
    template = request.app.state.templates.get_template("upload_history.html")
    return HTMLResponse(template.render(
        request=request,
        attempts=attempts,
        channels=core.config_manager.config().channels,
        channel_names=channel_names,
        selected_channel_id=channel_id or "",
        timezone=core.config_manager.config().timezone,
    ))


@router.get("/schedules/{channel_id}", response_class=HTMLResponse)
async def schedule_list(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    versions = await core.state.list_versions(channel_id)
    template = request.app.state.templates.get_template("schedules.html")
    return HTMLResponse(template.render(
        request=request,
        channel=channel,
        versions=versions,
        timezone=core.config_manager.config().timezone,
    ))


@router.get("/schedules/{channel_id}/diff", response_class=HTMLResponse)
async def schedule_diff(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    from_version = _safe_int(request.query_params.get("from"))
    to_version = _safe_int(request.query_params.get("to"))
    versions = await core.state.list_versions(channel_id)
    if not from_version or not to_version:
        selected = [int(str(version["version"])) for version in versions[:2]]
        if len(selected) >= 2:
            to_version, from_version = selected[0], selected[1]
    if not from_version or not to_version:
        return HTMLResponse("Select two schedule versions to compare", status_code=400)

    from_meta = await core.state.get_schedule_version_meta(channel_id, from_version)
    to_meta = await core.state.get_schedule_version_meta(channel_id, to_version)
    if from_meta is None or to_meta is None:
        return HTMLResponse("Schedule version not found", status_code=404)

    from_timeline = Timeline.from_snapshot(json.loads(str(from_meta["timeline_json"])))
    to_timeline = Timeline.from_snapshot(json.loads(str(to_meta["timeline_json"])))
    timezone = core.config_manager.config().timezone
    template = request.app.state.templates.get_template("schedule_diff.html")
    return HTMLResponse(template.render(
        request=request,
        channel=channel,
        versions=versions,
        from_version=from_version,
        to_version=to_version,
        diff=_timeline_diff(from_timeline, to_timeline),
        timezone=timezone,
    ))


@router.get("/schedules/{channel_id}/preview/{version}", response_class=HTMLResponse)
async def schedule_preview(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    timeline_json = await core.state.get_schedule_version(channel_id, version)
    if timeline_json is None:
        return HTMLResponse("Schedule version not found", status_code=404)

    timeline = Timeline.from_snapshot(json.loads(timeline_json))
    timezone = core.config_manager.config().timezone
    rows = [
        _preview_row(block, timezone)
        for block in sorted(timeline.blocks, key=lambda block: block.start_time)
    ]
    validation_errors = timeline.metadata.get("validation_errors", [])
    template = request.app.state.templates.get_template("schedule_preview.html")
    return HTMLResponse(template.render(
        request=request,
        channel=channel,
        version=version,
        timeline=timeline,
        rows=rows,
        summary=_preview_summary(rows, timeline),
        validation_errors=validation_errors,
        ad_warnings=timeline.metadata.get("ad_warnings", []),
        timezone=timezone,
    ))


@router.post("/schedules/{channel_id}/{version}/approve", response_class=HTMLResponse)
async def approve_schedule(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    meta = await core.state.get_schedule_version_meta(channel_id, version)
    if meta is None:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    if meta["status"] == "invalid":
        return await _action_result(
            request,
            channel,
            "danger",
            "Invalid schedules cannot be approved.",
            status_code=409,
        )
    timeline = Timeline.from_snapshot(json.loads(str(meta["timeline_json"])))
    sanity_errors = _schedule_sanity_errors(timeline)
    if sanity_errors:
        return await _action_result(
            request,
            channel,
            "danger",
            "Schedule cannot be approved: " + "; ".join(sanity_errors[:3]),
            status_code=409,
        )

    await core.state.approve_version(channel_id, version)
    logger.info("Approved schedule version channel_id=%s version=%s", channel_id, version)
    return await _action_result(
        request, channel, "success", f"Approved schedule version {version}.",
    )


@router.post("/schedules/{channel_id}/{version}/reject", response_class=HTMLResponse)
async def reject_schedule(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    meta = await core.state.get_schedule_version_meta(channel_id, version)
    if meta is None:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    if meta["status"] == "uploaded":
        return await _action_result(
            request,
            channel,
            "danger",
            "Uploaded schedules cannot be rejected.",
            status_code=409,
        )

    await core.state.set_schedule_status(channel_id, version, "rejected")
    logger.info("Rejected schedule version channel_id=%s version=%s", channel_id, version)
    return await _action_result(
        request, channel, "success", f"Rejected schedule version {version}.",
    )


@router.post("/schedules/{channel_id}/{version}/rollback", response_class=HTMLResponse)
async def rollback_schedule(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    new_version = await core.state.rollback_to_version(channel_id, version)
    if new_version is None:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    logger.info(
        "Rolled back schedule version channel_id=%s source_version=%s new_version=%s",
        channel_id,
        version,
        new_version,
    )
    return await _action_result(
        request, channel, "success", f"Rolled back to draft version {new_version}.",
    )


@router.post("/schedules/{channel_id}/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_schedules(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    form = await request.form()
    versions = [_safe_int(value) for value in form.getlist("selected_versions")]
    versions = [version for version in versions if version > 0]
    if not versions:
        return await _action_result(
            request, channel, "warning", "Select at least one schedule version.",
        )
    result = await core.state.delete_schedule_versions(channel_id, versions)
    deleted = int(result.get("deleted", 0))
    skipped = result.get("skipped_uploaded", [])
    skipped_text = (
        f" Skipped uploaded versions: {', '.join(str(item) for item in skipped)}."
        if isinstance(skipped, list) and skipped
        else ""
    )
    logger.info(
        "Bulk deleted schedule versions channel_id=%s requested=%s deleted=%s skipped=%s",
        channel_id,
        versions,
        deleted,
        skipped,
    )
    return await _action_result(
        request,
        channel,
        "success" if deleted else "warning",
        f"Deleted {deleted} schedule version(s).{skipped_text}",
    )


@router.post("/schedules/{channel_id}/cleanup", response_class=HTMLResponse)
async def cleanup_schedules(request: Request, channel_id: str) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    form = await request.form()
    keep_latest = max(1, _safe_int(form.get("keep_latest"), 10))
    statuses = [
        str(status)
        for status in form.getlist("cleanup_statuses")
        if str(status) in {"draft", "approved", "invalid", "rejected"}
    ]
    if not statuses:
        statuses = ["draft", "approved", "invalid", "rejected"]
    result = await core.state.cleanup_schedule_versions(
        channel_id,
        keep_latest=keep_latest,
        statuses=statuses,
    )
    deleted = int(result.get("deleted", 0))
    logger.info(
        "Cleaned schedule versions channel_id=%s keep_latest=%s statuses=%s deleted=%s",
        channel_id,
        keep_latest,
        statuses,
        deleted,
    )
    return await _action_result(
        request,
        channel,
        "success" if deleted else "info",
        f"Cleanup deleted {deleted} old schedule version(s). Uploaded versions were kept.",
    )


@router.delete("/schedules/{channel_id}/{version}", response_class=HTMLResponse)
async def delete_schedule(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    meta = await core.state.get_schedule_version_meta(channel_id, version)
    if meta is None:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    if meta["status"] == "uploaded":
        return await _action_result(
            request,
            channel,
            "danger",
            "Uploaded schedules cannot be deleted.",
            status_code=409,
        )

    deleted = await core.state.delete_schedule_version(channel_id, version)
    if not deleted:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    logger.info("Deleted schedule version channel_id=%s version=%s", channel_id, version)
    return await _action_result(
        request, channel, "success", f"Deleted schedule version {version}.",
    )


@router.post("/schedules/{channel_id}/{version}/upload", response_class=HTMLResponse)
async def upload_schedule(request: Request, channel_id: str, version: int) -> HTMLResponse:
    core = request.app.state.core
    channel = _find_channel(core, channel_id)
    if not channel:
        return HTMLResponse("Channel not found", status_code=404)

    meta = await core.state.get_schedule_version_meta(channel_id, version)
    if meta is None:
        return await _action_result(
            request, channel, "danger", "Schedule version not found.", status_code=404,
        )
    if meta["status"] not in {"approved", "uploaded"}:
        return await _action_result(
            request,
            channel,
            "danger",
            "Only approved or previously uploaded schedules can be uploaded.",
            status_code=409,
        )
    if core.tunarr_client is None:
        return await _action_result(
            request, channel, "danger", "Tunarr client not available.", status_code=503,
        )

    timeline = Timeline.from_snapshot(json.loads(str(meta["timeline_json"])))
    sanity_errors = _schedule_sanity_errors(timeline)
    if sanity_errors:
        return await _action_result(
            request,
            channel,
            "danger",
            "Schedule cannot be uploaded: " + "; ".join(sanity_errors[:3]),
            status_code=409,
        )
    try:
        upload_result = await core.tunarr_client.upload_timeline(
            channel_id,
            timeline,
            station_id_custom_show_id=channel.continuity.station_id_custom_show_id,
            bumper_custom_show_id=channel.continuity.bumper_custom_show_id,
        )
    except httpx.HTTPStatusError as e:
        detail = e.response.text.strip() or e.response.reason_phrase
        await core.state.record_upload_attempt(
            channel_id,
            version,
            "failed",
            f"Tunarr rejected upload ({e.response.status_code}): {detail}",
            {"status_code": e.response.status_code},
        )
        core.metrics.record_upload(channel_id, "failed")
        if getattr(core, "notification_router", None) is not None:
            await core.notification_router.send(NotificationMessage(
                event_type="upload_failed",
                title=f"Upload failed for {channel.name or channel_id}",
                message=f"Tunarr rejected schedule version {version}: {detail}",
                severity="danger",
                channel_id=channel_id,
                details={"version": version, "status_code": e.response.status_code},
            ))
        logger.warning(
            "Tunarr rejected schedule upload channel_id=%s version=%s status=%s body=%s",
            channel_id,
            version,
            e.response.status_code,
            detail,
        )
        return await _action_result(
            request,
            channel,
            "danger",
            f"Tunarr rejected upload ({e.response.status_code}): {detail}",
        )
    except httpx.HTTPError as e:
        await core.state.record_upload_attempt(
            channel_id,
            version,
            "failed",
            f"Tunarr upload failed: {e}",
            {"error": str(e)},
        )
        core.metrics.record_upload(channel_id, "failed")
        if getattr(core, "notification_router", None) is not None:
            await core.notification_router.send(NotificationMessage(
                event_type="upload_failed",
                title=f"Upload failed for {channel.name or channel_id}",
                message=f"Tunarr upload failed for schedule version {version}: {e}",
                severity="danger",
                channel_id=channel_id,
                details={"version": version, "error": str(e)},
            ))
        logger.warning(
            "Tunarr upload failed channel_id=%s version=%s error=%s",
            channel_id,
            version,
            e,
        )
        return await _action_result(request, channel, "danger", f"Tunarr upload failed: {e}")
    await core.state.set_schedule_status(channel_id, version, "uploaded")
    upload_details = upload_result.get("_upload", {})
    await core.state.record_upload_attempt(
        channel_id,
        version,
        "success",
        "Uploaded schedule version.",
        upload_details if isinstance(upload_details, dict) else {},
    )
    core.metrics.record_upload(channel_id, "success")
    if getattr(core, "notification_router", None) is not None:
        await core.notification_router.send(NotificationMessage(
            event_type="upload_succeeded",
            title=f"Upload succeeded for {channel.name or channel_id}",
            message=f"Uploaded schedule version {version}.",
            severity="success",
            channel_id=channel_id,
            details={"version": version, "upload": upload_details},
        ))
    logger.info("Uploaded schedule version channel_id=%s version=%s", channel_id, version)
    return await _action_result(
        request,
        channel,
        "success",
        _upload_success_message(version, upload_result),
    )


def _find_channel(core: Any, channel_id: str) -> Any:
    for c in core.config_manager.config().channels:
        if c.id == channel_id:
            return c
    return None


def _notice(level: str, message: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="alert alert-{level} alert-dismissible fade show" role="alert">'
        f'{message}'
        '<button type="button" class="btn-close" data-bs-dismiss="alert" '
        'aria-label="Close"></button>'
        '</div>',
        status_code=status_code,
    )


def _upload_success_message(version: int, upload_result: dict[str, Any]) -> str:
    upload = upload_result.get("_upload", {})
    if not isinstance(upload, dict):
        return (
            '<div class="upload-result">'
            f"<strong>Uploaded schedule version {version}.</strong>"
            '<span class="text-secondary">Tunarr accepted the schedule.</span>'
            "</div>"
        )
    duration_minutes = int(float(upload.get("duration_ms", 0)) / 60000)
    fallback = "yes" if upload.get("fallback_used") else "no"
    return (
        '<div class="upload-result">'
        f"<strong>Uploaded schedule version {version}.</strong>"
        '<div class="upload-result-grid">'
        f'{_upload_metric("Upload mode", upload.get("mode", "manual"))}'
        f'{_upload_metric("Persistent time", _persistent_time_label(upload))}'
        f'{_upload_metric("Fallback used", fallback)}'
        f'{_upload_metric("Tunarr status", _tunarr_status_label(upload))}'
        f'{_upload_metric("Lineup entries", upload.get("lineup_items", 0))}'
        f'{_upload_metric("Content items", upload.get("content_items", 0))}'
        f'{_upload_metric("Duration", f"{duration_minutes} min")}'
        "</div>"
        '<span class="text-secondary">Schedule table refreshed.</span>'
        "</div>"
    )


def _upload_metric(label: str, value: object) -> str:
    return (
        '<span class="upload-result-metric">'
        f"<small>{label}</small>"
        f"<b>{value}</b>"
        "</span>"
    )


def _persistent_time_label(upload: dict[str, Any]) -> str:
    status = str(upload.get("persistent_time_status", "not_attempted"))
    if status == "succeeded":
        return "succeeded"
    if status == "failed_fallback":
        return "failed, fallback used"
    if status == "not_attempted":
        return "not attempted"
    return status


def _tunarr_status_label(upload: dict[str, Any]) -> str:
    final_status = upload.get("final_status") or upload.get("verification_status")
    programming_status = upload.get("programming_status")
    channel_status = upload.get("channel_update_status")
    parts = []
    if final_status:
        parts.append(f"final {final_status}")
    if programming_status:
        parts.append(f"programming {programming_status}")
    if channel_status:
        parts.append(f"channel {channel_status}")
    return ", ".join(parts) if parts else "accepted"


def _schedule_sanity_errors(timeline: Timeline) -> list[str]:
    errors: list[str] = []
    if not timeline.blocks:
        errors.append("schedule has no blocks")

    for error in timeline.metadata.get("validation_errors", []):
        if error:
            errors.append(str(error))

    errors.extend(timeline.validate())

    sorted_blocks = sorted(timeline.blocks, key=lambda b: b.start_time)
    previous_station_id: StationIDBlock | None = None
    for block in sorted_blocks:
        if block.duration.total_seconds() <= 0:
            errors.append(f"block {block.id} has non-positive duration")
        if isinstance(block, EpisodeBlock) and block.runtime_seconds <= 0:
            errors.append(f"episode {block.episode_id or block.id} has invalid runtime")
        if isinstance(block, MovieBlock) and block.runtime_seconds <= 0:
            errors.append(f"movie {block.movie_id or block.id} has invalid runtime")
        if isinstance(block, StationIDBlock):
            if previous_station_id and previous_station_id.clip_id == block.clip_id:
                errors.append(f"station ID {block.clip_id or block.id} repeats back-to-back")
            previous_station_id = block
        elif block.metadata.get("type") != "daypart_transition":
            previous_station_id = None

    return _unique_errors(errors)


def _preview_row(block: TimelineBlock, timezone: str) -> dict[str, object]:
    start_time = to_timezone(block.start_time, timezone) or block.start_time
    end_time = to_timezone(block.end_time, timezone) or block.end_time
    block_type = block.block_type.value
    title = str(block.metadata.get("title") or block_type.replace("_", " ").title())
    details = str(block.metadata.get("daypart") or "")
    source = ""
    status = "ok"
    if isinstance(block, EpisodeBlock):
        show = str(block.metadata.get("show_name") or "")
        season = _safe_int(block.season_number or block.metadata.get("parent_index_number"))
        episode = _safe_int(block.episode_number or block.metadata.get("index_number"))
        episode_title = str(block.metadata.get("title") or title)
        title = show or title
        details = f"S{season:02d}E{episode:02d} {episode_title}".strip()
        source = block.episode_id
    elif isinstance(block, MovieBlock):
        source = block.movie_id
        year = block.year or block.metadata.get("year")
        details = f"{year}" if year else f"{block.runtime_seconds // 60}m runtime"
    elif isinstance(block, AdBlock):
        title = f"Ad Break ({block.ad_count} spots)"
        details = f"{block.total_duration_seconds // 60}m"
        source = str(block.metadata.get("filler_list_id", ""))
        if block.metadata.get("fit_under_seconds") or block.metadata.get("generic_break"):
            status = "warning"
    elif isinstance(block, StationIDBlock):
        title = "Station ID"
        source = block.clip_id
    elif isinstance(block, FillerBlock):
        title = f"Filler ({block.filler_type.value})"
    elif isinstance(block, OfflineBlock):
        custom_show_ids = block.metadata.get("custom_show_list_ids", [])
        if isinstance(custom_show_ids, list):
            source = ", ".join(str(item) for item in custom_show_ids)
        title = "Off-Air Loop" if source else "Offline"
        details = "Custom show loop" if source else block.reason
    elif isinstance(block, SlotBlock):
        title = "Unfilled Slot"
        details = str(block.metadata.get("note") or block.metadata.get("daypart") or "")
        status = "error"
    if block.duration.total_seconds() <= 0:
        status = "error"
    return {
        "time": f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}",
        "date": start_time.strftime("%Y-%m-%d"),
        "day": start_time.strftime("%a %Y-%m-%d"),
        "hour": start_time.strftime("%H:00"),
        "daypart": str(block.metadata.get("daypart") or "none"),
        "type": block_type,
        "status": status,
        "title": title,
        "duration": int(block.duration.total_seconds() / 60),
        "details": details,
        "source": source,
    }


def _preview_summary(rows: list[dict[str, object]], timeline: Timeline) -> dict[str, object]:
    type_counts = Counter(str(row["type"]) for row in rows)
    type_minutes: Counter[str] = Counter()
    status_counts = Counter(str(row["status"]) for row in rows)
    dayparts: set[str] = set()
    hours: set[str] = set()
    days: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    for row in rows:
        row_type = str(row["type"])
        type_minutes[row_type] += _safe_int(row["duration"])
        daypart = str(row["daypart"])
        if daypart and daypart != "none":
            dayparts.add(daypart)
        hours.add(str(row["hour"]))
        date = str(row["date"])
        if date not in seen_dates:
            seen_dates.add(date)
            days.append({"date": date, "label": str(row["day"])})
    total_minutes = sum(_safe_int(row["duration"]) for row in rows)
    ad_minutes = type_minutes["ad"]
    target_seconds = _safe_int(timeline.metadata.get("ad_target_seconds"))
    inserted_seconds = _safe_int(timeline.metadata.get("ad_inserted_seconds"))
    ad_rotation = timeline.metadata.get("ad_rotation_summary", {})
    if not isinstance(ad_rotation, dict):
        ad_rotation = {}
    return {
        "total_blocks": len(rows),
        "total_minutes": total_minutes,
        "episode_count": type_counts["episode"],
        "movie_count": type_counts["movie"],
        "ad_count": type_counts["ad"],
        "offline_count": type_counts["offline"],
        "ad_minutes": ad_minutes,
        "ad_target_minutes": int(target_seconds / 60),
        "ad_inserted_minutes": int(inserted_seconds / 60),
        "ad_rotation": ad_rotation,
        "days": days,
        "dayparts": sorted(dayparts),
        "hours": sorted(hours),
        "statuses": sorted(status_counts),
        "types": sorted(type_counts),
        "type_minutes": dict(type_minutes),
    }


def _timeline_diff(from_timeline: Timeline, to_timeline: Timeline) -> dict[str, object]:
    from_rows = [_diff_signature(block) for block in sorted(
        from_timeline.blocks, key=lambda block: block.start_time,
    )]
    to_rows = [_diff_signature(block) for block in sorted(
        to_timeline.blocks, key=lambda block: block.start_time,
    )]
    from_counts = Counter(str(row["type"]) for row in from_rows)
    to_counts = Counter(str(row["type"]) for row in to_rows)
    from_ids = Counter(
        str(row["media_key"]) for row in from_rows if row["media_key"]
    )
    to_ids = Counter(
        str(row["media_key"]) for row in to_rows if row["media_key"]
    )
    added_media = sorted((to_ids - from_ids).elements())
    removed_media = sorted((from_ids - to_ids).elements())
    max_rows = max(len(from_rows), len(to_rows))
    row_changes: list[dict[str, object]] = []
    changed_rows = 0
    for index in range(max_rows):
        before = from_rows[index] if index < len(from_rows) else None
        after = to_rows[index] if index < len(to_rows) else None
        changed = before != after
        if changed:
            changed_rows += 1
        if index < 25 and changed:
            row_changes.append({
                "index": index + 1,
                "before": before,
                "after": after,
            })
    return {
        "from_summary": _diff_summary(from_rows),
        "to_summary": _diff_summary(to_rows),
        "type_changes": [
            {
                "type": block_type,
                "from": from_counts[block_type],
                "to": to_counts[block_type],
                "delta": to_counts[block_type] - from_counts[block_type],
            }
            for block_type in sorted(set(from_counts) | set(to_counts))
        ],
        "added_media": added_media[:30],
        "removed_media": removed_media[:30],
        "changed_rows": changed_rows,
        "row_changes": row_changes,
    }


def _diff_signature(block: TimelineBlock) -> dict[str, object]:
    media_key = ""
    if isinstance(block, EpisodeBlock):
        media_key = block.episode_id
    elif isinstance(block, MovieBlock):
        media_key = block.movie_id
    elif isinstance(block, StationIDBlock):
        media_key = block.clip_id
    elif isinstance(block, AdBlock):
        media_key = str(block.metadata.get("filler_list_id", ""))
    elif isinstance(block, OfflineBlock):
        custom_show_ids = block.metadata.get("custom_show_list_ids", [])
        if isinstance(custom_show_ids, list):
            media_key = ",".join(str(item) for item in custom_show_ids)
    return {
        "type": block.block_type.value,
        "title": str(block.metadata.get("title") or block.block_type.value),
        "daypart": str(block.metadata.get("daypart") or ""),
        "duration": int(block.duration.total_seconds() / 60),
        "media_key": media_key,
    }


def _diff_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    counts = Counter(str(row["type"]) for row in rows)
    minutes = sum(_safe_int(row["duration"]) for row in rows)
    return {
        "blocks": len(rows),
        "minutes": minutes,
        "episode_count": counts["episode"],
        "movie_count": counts["movie"],
        "ad_count": counts["ad"],
        "offline_count": counts["offline"],
    }


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _unique_errors(errors: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for error in errors:
        if error in seen:
            continue
        seen.add(error)
        result.append(error)
    return result


async def _action_result(
    request: Request,
    channel: Any,
    level: str,
    message: str,
    status_code: int = 200,
) -> HTMLResponse:
    core = request.app.state.core
    versions = await core.state.list_versions(channel.id)
    template = request.app.state.templates.get_template("schedule_table.html")
    notice = bytes(_notice(level, message).body).decode()
    rows = template.render(
        request=request,
        channel=channel,
        versions=versions,
        timezone=core.config_manager.config().timezone,
    )
    return HTMLResponse(notice + rows, status_code=status_code)
