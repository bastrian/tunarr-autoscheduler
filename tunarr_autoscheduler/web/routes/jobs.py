from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from tunarr_autoscheduler.core.diagnostics import create_diagnostic_bundle
from tunarr_autoscheduler.core.schedule_health import build_schedule_health
from tunarr_autoscheduler.core.schedule_monitor import channel_schedule_statuses
from tunarr_autoscheduler.core.timeline import Timeline

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_class=HTMLResponse)
async def job_history(request: Request) -> HTMLResponse:
    core = request.app.state.core
    channels = core.config_manager.config().channels
    all_jobs = []
    for channel in channels:
        jobs = await core.state.list_recent_jobs(channel.id, limit=10)
        for j in jobs:
            j["channel_name"] = channel.name
            j["channel_id"] = channel.id
        all_jobs.extend(jobs)
    all_jobs.sort(key=lambda j: j.get("started_at", ""), reverse=True)
    all_jobs = all_jobs[:50]

    template = request.app.state.templates.get_template("jobs.html")
    return HTMLResponse(template.render(
        request=request,
        jobs=all_jobs,
    ))


@router.get("/channel-health", response_class=HTMLResponse)
async def channel_health_dashboard(request: Request) -> HTMLResponse:
    core = request.app.state.core
    config = core.config_manager.config()
    statuses = await channel_schedule_statuses(config=config, state=core.state)
    connectivity = {
        "database": await _check_database(core),
        "jellyfin": await _check_client(getattr(core, "jellyfin_client", None)),
        "tunarr": await _check_client(getattr(core, "tunarr_client", None)),
    }
    rows = []
    for channel in config.channels:
        status = statuses.get(channel.id, {})
        health = await _latest_uploaded_health(core, channel.id, status)
        rows.append({
            "channel": channel,
            "status": status,
            "health": health,
            "active_job": core.job_manager.get_active_job(channel.id),
        })
    template = request.app.state.templates.get_template("channel_health.html")
    return HTMLResponse(template.render(
        request=request,
        rows=rows,
        connectivity=connectivity,
        timezone=config.timezone,
    ))


@router.post("/diagnostics/bundle")
async def diagnostic_bundle_download(request: Request) -> FileResponse:
    core = request.app.state.core
    archive_path = await create_diagnostic_bundle(
        config_manager=core.config_manager,
        state=core.state,
    )
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=archive_path.name,
    )


async def _latest_uploaded_health(
    core: Any,
    channel_id: str,
    status: dict[str, Any],
) -> dict[str, object] | None:
    version = status.get("uploaded_version")
    if not version:
        return None
    meta = await core.state.get_schedule_version_meta(channel_id, int(str(version)))
    if meta is None:
        return None
    timeline_json = await core.state.get_schedule_version(channel_id, int(str(version)))
    if not timeline_json:
        return None
    try:
        timeline = Timeline.from_snapshot(json.loads(timeline_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "level": "danger",
            "summary": "Schedule health could not be read",
            "issues": ["Timeline data is invalid."],
            "metrics": [],
        }
    return build_schedule_health(meta, timeline)


async def _check_database(core: object) -> bool:
    db = getattr(core, "db", None)
    if db is None:
        return False
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except Exception:
        return False
    return True


async def _check_client(client: object | None) -> bool:
    if client is None or not hasattr(client, "check_connection"):
        return False
    typed_client: Any = client
    try:
        return bool(await typed_client.check_connection())
    except Exception:
        return False
