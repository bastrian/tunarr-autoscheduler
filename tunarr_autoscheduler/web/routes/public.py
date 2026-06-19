from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.metadata.rate_limit import AsyncRateLimiter
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    FillerBlock,
    MovieBlock,
    OfflineBlock,
    StationIDBlock,
    TimelineBlock,
)

router = APIRouter(tags=["public"])
_TMDB_RATE_LIMITER = AsyncRateLimiter(max_calls=120, period_seconds=60.0)


@router.get("/epg", response_class=HTMLResponse)
@router.get("/public/epg", response_class=HTMLResponse)
@router.get("/public/epg/export", response_class=HTMLResponse)
async def public_epg(request: Request) -> HTMLResponse:
    standalone_export = request.url.path.endswith("/export")
    payload = await _public_epg_payload(request, absolute_images=standalone_export)
    template = request.app.state.templates.get_template("public_epg.html")
    return HTMLResponse(template.render(
        request=request,
        channels=payload["channels"],
        channel_options=payload["channel_options"],
        selected_channel=payload["selected_channel"],
        period=payload["period"],
        period_options=_period_options(str(payload["view"])),
        now=payload["now"],
        now_position=payload["now_position"],
        time_marks=payload["time_marks"],
        window_start=payload["window_start"],
        window_end=payload["window_end"],
        view=payload["view"],
        selected_date=payload["selected_date"],
        previous_date=payload["previous_date"],
        next_date=payload["next_date"],
        timezone=payload["timezone"],
        standalone_export=standalone_export,
        epg_path="/public/epg/export" if standalone_export else "/epg",
        inline_css=_inline_public_css() if standalone_export else "",
    ))


@router.get("/public/epg.json", response_class=JSONResponse)
@router.get("/public/epg/export.json", response_class=JSONResponse)
async def public_epg_json(request: Request) -> JSONResponse:
    payload = await _public_epg_payload(request, absolute_images=True)
    return JSONResponse(
        _json_public_payload(payload),
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/public/epg.xml", response_class=Response)
@router.get("/public/epg/xmltv", response_class=Response)
@router.get("/public/epg/export.xml", response_class=Response)
async def public_epg_xmltv(request: Request) -> Response:
    payload = await _public_epg_payload(request, absolute_images=True)
    return Response(
        _xmltv_payload(payload),
        media_type="application/xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/public/epg/images/{item_id}")
async def public_epg_image(request: Request, item_id: str) -> Response:
    core = request.app.state.core
    jellyfin = core.config_manager.config().jellyfin
    url = f"{jellyfin.url.rstrip('/')}/Items/{item_id}/Images/Primary"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers={"X-Emby-Token": jellyfin.api_key})
    if response.status_code == 404:
        return Response(status_code=404)
    response.raise_for_status()
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _public_epg_payload(
    request: Request,
    *,
    absolute_images: bool = False,
) -> dict[str, Any]:
    core = request.app.state.core
    config = core.config_manager.config()
    timezone = ZoneInfo(config.timezone)
    rows = await core.state.list_public_epg_versions()
    channels_by_id = {channel.id: channel for channel in config.channels}
    now = datetime.now(tz=timezone)
    view = _view_mode(request.query_params.get("view"))
    period = _period_mode(request.query_params.get("period"))
    if view == "week":
        period = "day"
    selected_date = _selected_date(request.query_params.get("date"), now.date())
    window_start, window_end = _window(selected_date, view, period, timezone, now)
    previous_date = selected_date - timedelta(days=7 if view == "week" else 1)
    next_date = selected_date + timedelta(days=7 if view == "week" else 1)
    selected_channel = str(request.query_params.get("channel", "")).strip()
    public_base_url = str(request.base_url).rstrip("/") if absolute_images else ""
    metadata_cache: dict[str, dict[str, Any]] = {}
    rows_by_channel = {str(row["channel_id"]): row for row in rows}
    channels: list[dict[str, Any]] = []

    public_channels = sorted(
        (
            channel for channel in config.channels
            if channel.public_epg_enabled
            and (not selected_channel or selected_channel == channel.id)
        ),
        key=lambda channel: (
            channel.public_epg_order,
            (channel.name or channel.id).lower(),
            channel.id,
        ),
    )

    for channel in public_channels:
        channel_id = channel.id
        row = rows_by_channel.get(channel_id)
        blocks: list[dict[str, Any]] = []
        if row is not None:
            timeline = Timeline.from_snapshot(json.loads(str(row["timeline_json"])))
            for block in sorted(timeline.blocks, key=lambda item: item.start_time):
                if not _is_public_epg_block(block) or not _is_visible(
                    block, window_start, window_end,
                ):
                    continue
                item = _epg_item(
                    block,
                    timezone,
                    window_start,
                    window_end,
                    now,
                    public_base_url=public_base_url,
                )
                await _enrich_epg_item(item, block, config.metadata, metadata_cache)
                blocks.append(item)
            blocks = _merge_consecutive_epg_items(blocks, now)
        channels.append({
            "id": channel_id,
            "name": channel.name or channel_id,
            "logo_url": channel.public_epg_logo_url,
            "has_schedule": row is not None,
            "version": int(str(row["version"])) if row is not None else None,
            "status": str(row["status"]) if row is not None else "",
            "created_at": str(row["created_at"]) if row is not None else "",
            "programs": blocks,
            "current": next((item for item in blocks if item["is_current"]), None),
            "next": next((item for item in blocks if item["starts_after_now"]), None),
            "upcoming": [item for item in blocks if item["starts_after_now"]][:5],
        })

    channel_options = [
        {"id": channel.id, "name": channel.name or channel.id}
        for channel in config.channels
        if channel.public_epg_enabled
    ]
    channel_options.sort(key=lambda item: (
        channels_by_id[item["id"]].public_epg_order,
        item["name"].lower(),
        item["id"],
    ))
    return {
        "schema": "flixwolf.public_epg.v1",
        "generated_at": now,
        "timezone": config.timezone,
        "selected_channel": selected_channel,
        "period": period,
        "view": view,
        "selected_date": selected_date,
        "previous_date": previous_date,
        "next_date": next_date,
        "window_start": window_start,
        "window_end": window_end,
        "now": now,
        "now_position": _window_position(now, window_start, window_end),
        "time_marks": _time_marks(window_start, window_end, view),
        "channel_options": channel_options,
        "channels": channels,
    }


def _view_mode(raw: str | None) -> str:
    return raw if raw in {"day", "week"} else "day"


def _period_mode(raw: str | None) -> str:
    allowed = {"now", "day", "evening", "late", "overnight", "morning", "afternoon"}
    return raw if raw in allowed else "day"


def _selected_date(raw: str | None, fallback: date) -> date:
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback


def _window(
    selected_date: date,
    view: str,
    period: str,
    timezone: ZoneInfo,
    now: datetime,
) -> tuple[datetime, datetime]:
    if view == "week":
        selected_date = selected_date - timedelta(days=selected_date.weekday())
        start = datetime.combine(selected_date, time.min, tzinfo=timezone)
        return start, start + timedelta(days=7)
    if period == "now" and selected_date == now.date():
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=4)
    start_time, end_time = {
        "evening": (time(20, 0), time.min),
        "late": (time(18, 0), time.min),
        "overnight": (time.min, time(6, 0)),
        "morning": (time(6, 0), time(12, 0)),
        "afternoon": (time(12, 0), time(18, 0)),
    }.get(period, (time.min, time.min))
    start = datetime.combine(selected_date, start_time, tzinfo=timezone)
    end_date = selected_date + timedelta(days=1) if end_time <= start_time else selected_date
    end = datetime.combine(end_date, end_time, tzinfo=timezone)
    return start, end


def _period_options(view: str) -> list[dict[str, str]]:
    if view == "week":
        return [{"id": "day", "label": "Full week"}]
    return [
        {"id": "now", "label": "Now"},
        {"id": "day", "label": "Full day"},
        {"id": "evening", "label": "20-00"},
        {"id": "late", "label": "18-00"},
        {"id": "overnight", "label": "00-06"},
        {"id": "morning", "label": "06-12"},
        {"id": "afternoon", "label": "12-18"},
    ]


def _time_marks(
    window_start: datetime,
    window_end: datetime,
    view: str,
) -> list[dict[str, Any]]:
    marks: list[dict[str, Any]] = []
    if view == "week":
        cursor = window_start
        while cursor <= window_end:
            marks.append({
                "label": cursor.strftime("%a %d.%m."),
                "position": _window_position(cursor, window_start, window_end),
            })
            cursor += timedelta(days=1)
        return marks
    cursor = window_start
    while cursor <= window_end:
        marks.append({
            "label": cursor.strftime("%H:%M"),
            "position": _window_position(cursor, window_start, window_end),
        })
        cursor += timedelta(hours=3)
    return marks


def _is_public_epg_block(block: TimelineBlock) -> bool:
    return isinstance(block, (EpisodeBlock, MovieBlock, OfflineBlock)) and not isinstance(
        block, (AdBlock, FillerBlock, StationIDBlock),
    )


def _is_visible(block: TimelineBlock, window_start: datetime, window_end: datetime) -> bool:
    local_start = block.start_time.astimezone(window_start.tzinfo)
    local_end = block.end_time.astimezone(window_start.tzinfo)
    return local_end > window_start and local_start < window_end


def _epg_item(
    block: TimelineBlock,
    timezone: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    *,
    public_base_url: str = "",
) -> dict[str, Any]:
    start = block.start_time.astimezone(timezone)
    end = block.end_time.astimezone(timezone)
    title = _title(block)
    subtitle = _subtitle(block)
    overview = str(block.metadata.get("overview") or block.metadata.get("description") or "")
    series_name = str(block.metadata.get("show_name") or "") if isinstance(
        block, EpisodeBlock,
    ) else ""
    episode_title = str(block.metadata.get("title") or "") if isinstance(
        block, EpisodeBlock,
    ) else ""
    clipped_start = max(start, window_start)
    clipped_end = min(end, window_end)
    runtime_minutes = max(1, int(block.duration.total_seconds() / 60))
    return {
        "type": block.block_type.value,
        "kind": _kind(block),
        "title": title,
        "subtitle": subtitle,
        "overview": overview or subtitle,
        "start": start,
        "end": end,
        "time": f"{start:%H:%M} - {end:%H:%M}",
        "date_label": start.strftime("%a %d.%m."),
        "duration_minutes": runtime_minutes,
        "runtime_label": f"{runtime_minutes}m",
        "is_current": start <= now <= end,
        "starts_after_now": start > now,
        "progress_percent": _progress_percent(start, end, now),
        "ends_label": f"{end:%H:%M}",
        "timeline_left": _window_position(clipped_start, window_start, window_end),
        "timeline_width": max(
            0.8,
            _window_span_percent(clipped_start, clipped_end, window_start, window_end),
        ),
        "merge_key": _merge_key(block),
        "image_url": _image_url(block, public_base_url=public_base_url),
        "metadata_links": _metadata_links(block),
        "series_name": series_name,
        "episode_title": episode_title,
        "episode_code": _episode_code(block) if isinstance(block, EpisodeBlock) else "",
        "genres": ", ".join(str(item) for item in block.metadata.get("genres", [])[:3])
        if isinstance(block.metadata.get("genres"), list)
        else "",
        "year": _year(block),
    }


def _merge_consecutive_epg_items(
    items: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        if merged and _can_merge_epg_items(merged[-1], item):
            _extend_epg_item(merged[-1], item, now)
            continue
        merged.append(dict(item))
    return merged


def _can_merge_epg_items(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if previous.get("type") != current.get("type"):
        return False
    if previous.get("type") != "offline":
        return False
    gap_seconds = (current["start"] - previous["end"]).total_seconds()
    if gap_seconds < -1 or gap_seconds > 60:
        return False
    return previous.get("merge_key") == current.get("merge_key")


def _extend_epg_item(
    target: dict[str, Any],
    item: dict[str, Any],
    now: datetime,
) -> None:
    target["end"] = item["end"]
    duration_minutes = max(1, int((target["end"] - target["start"]).total_seconds() / 60))
    target["duration_minutes"] = duration_minutes
    target["runtime_label"] = f"{duration_minutes}m"
    target["time"] = f"{target['start']:%H:%M} - {target['end']:%H:%M}"
    target["is_current"] = target["start"] <= now <= target["end"]
    target["starts_after_now"] = target["start"] > now
    target["progress_percent"] = _progress_percent(target["start"], target["end"], now)
    target["ends_label"] = f"{target['end']:%H:%M}"


def _window_position(value: datetime, window_start: datetime, window_end: datetime) -> float:
    total = max(1.0, (window_end - window_start).total_seconds())
    offset = (value - window_start).total_seconds()
    return round(max(0.0, min(100.0, offset / total * 100)), 3)


def _window_span_percent(
    start: datetime,
    end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    total = max(1.0, (window_end - window_start).total_seconds())
    span = max(0.0, (min(end, window_end) - max(start, window_start)).total_seconds())
    return round(span / total * 100, 3)


def _progress_percent(start: datetime, end: datetime, now: datetime) -> int:
    if now <= start:
        return 0
    if now >= end:
        return 100
    total = max(1.0, (end - start).total_seconds())
    elapsed = (now - start).total_seconds()
    return int(max(0, min(100, round(elapsed / total * 100))))


def _kind(block: TimelineBlock) -> str:
    if isinstance(block, EpisodeBlock):
        return "Episode"
    if isinstance(block, MovieBlock):
        return "Movie"
    if isinstance(block, AdBlock):
        return "Ads"
    if isinstance(block, StationIDBlock):
        return "Station ID"
    if isinstance(block, OfflineBlock):
        return "Off-Air"
    return block.block_type.value.replace("_", " ").title()


def _merge_key(block: TimelineBlock) -> str:
    if isinstance(block, OfflineBlock) and _offline_is_public_off_air(block):
        return "offline:standby"
    return f"{block.block_type.value}:{_title(block)}:{_subtitle(block)}"


def _title(block: TimelineBlock) -> str:
    if isinstance(block, EpisodeBlock):
        return str(block.metadata.get("show_name") or block.metadata.get("title") or "Episode")
    if isinstance(block, OfflineBlock):
        if _offline_is_public_off_air(block):
            return "Off-Air"
        return str(block.metadata.get("title") or "Off-Air")
    return str(block.metadata.get("title") or block.metadata.get("show_name") or _kind(block))


def _subtitle(block: TimelineBlock) -> str:
    if isinstance(block, EpisodeBlock):
        parts = [_episode_code(block), str(block.metadata.get("title") or "")]
        return " ".join(part for part in parts if part)
    if isinstance(block, MovieBlock):
        year = block.year or block.metadata.get("year")
        return str(year or "")
    if isinstance(block, OfflineBlock):
        daypart = str(block.metadata.get("daypart") or "").replace("_", " ").title()
        if _offline_is_public_off_air(block):
            return daypart or "Standby"
        if block.metadata.get("custom_show_loop"):
            return daypart or "Standby"
        return daypart or str(block.metadata.get("reason") or "")
    return str(block.metadata.get("reason") or block.metadata.get("daypart") or "")


def _year(block: TimelineBlock) -> str:
    if isinstance(block, MovieBlock):
        return str(block.year or block.metadata.get("year") or "")
    return str(block.metadata.get("year") or block.metadata.get("production_year") or "")


def _episode_code(block: EpisodeBlock) -> str:
    season = block.season_number or _int_metadata(block.metadata.get("parent_index_number"))
    episode = block.episode_number or _int_metadata(block.metadata.get("index_number"))
    if season and episode:
        return f"S{season:02d}E{episode:02d}"
    if episode:
        return f"E{episode:02d}"
    return ""


def _int_metadata(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def _image_url(block: TimelineBlock, *, public_base_url: str = "") -> str:
    for key in (
        "image_url",
        "poster_url",
        "primary_image_url",
        "tmdb_poster_url",
        "tvdb_poster_url",
    ):
        value = str(block.metadata.get(key) or "").strip()
        if value:
            return value
    if isinstance(block, EpisodeBlock) and block.episode_id:
        return _public_url(f"/public/epg/images/{block.episode_id}", public_base_url)
    if isinstance(block, MovieBlock) and block.movie_id:
        return _public_url(f"/public/epg/images/{block.movie_id}", public_base_url)
    return ""


def _metadata_links(block: TimelineBlock) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    imdb_id = str(block.metadata.get("imdb_id") or block.metadata.get("imdb") or "").strip()
    tmdb_id = str(block.metadata.get("tmdb_id") or block.metadata.get("tmdb") or "").strip()
    tvdb_id = str(block.metadata.get("tvdb_id") or block.metadata.get("tvdb") or "").strip()
    if imdb_id:
        links.append({"label": "IMDb", "url": f"https://www.imdb.com/title/{imdb_id}/"})
    if tmdb_id:
        kind = "tv" if isinstance(block, EpisodeBlock) else "movie"
        links.append({"label": "TMDB", "url": f"https://www.themoviedb.org/{kind}/{tmdb_id}"})
    if tvdb_id:
        links.append({"label": "TVDB", "url": f"https://thetvdb.com/dereferrer/series/{tvdb_id}"})
    return links


def _offline_is_public_off_air(block: OfflineBlock) -> bool:
    values = [
        block.reason,
        block.metadata.get("title"),
        block.metadata.get("reason"),
        block.metadata.get("daypart"),
    ]
    normalized = " ".join(str(value or "").lower().replace("_", " ") for value in values)
    return (
        bool(block.metadata.get("off_air"))
        or "off air" in normalized
        or "standby" in normalized
    )


async def _enrich_epg_item(
    item: dict[str, Any],
    block: TimelineBlock,
    metadata_config: Any,
    metadata_cache: dict[str, dict[str, Any]],
) -> None:
    if not getattr(metadata_config, "tmdb_enabled", False):
        return
    api_key = str(getattr(metadata_config, "tmdb_api_key", "") or "").strip()
    if not api_key or item.get("type") == "offline":
        return
    title = item["title"]
    if isinstance(block, EpisodeBlock):
        title = str(block.metadata.get("show_name") or item["title"])
        media_type = "tv"
    elif isinstance(block, MovieBlock):
        media_type = "movie"
    else:
        return
    year = item.get("year") or ""
    cache_key = f"{media_type}:{title.lower()}:{year}"
    if cache_key not in metadata_cache:
        metadata_cache[cache_key] = await _fetch_tmdb_metadata(
            media_type=media_type,
            title=title,
            year=str(year),
            api_key=api_key,
            language=str(getattr(metadata_config, "tmdb_language", "de-DE") or "de-DE"),
            rate_limit_per_minute=int(
                getattr(metadata_config, "tmdb_rate_limit_per_minute", 120) or 120,
            ),
        )
    metadata = metadata_cache[cache_key]
    if not metadata:
        return
    current_image = str(item.get("image_url") or "")
    external_image = str(metadata.get("image_url") or "")
    if external_image and (not current_image or "/public/epg/images/" in current_image):
        item["image_url"] = external_image
    item["overview"] = item.get("overview") or metadata.get("overview", "")
    item["year"] = item.get("year") or metadata.get("year", "")
    links = list(item.get("metadata_links", []))
    tmdb_id = str(metadata.get("tmdb_id", ""))
    if tmdb_id and not any(link.get("label") == "TMDB" for link in links):
        links.append({
            "label": "TMDB",
            "url": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
        })
    item["metadata_links"] = links


async def _fetch_tmdb_metadata(
    *,
    media_type: str,
    title: str,
    year: str,
    api_key: str,
    language: str,
    rate_limit_per_minute: int = 120,
) -> dict[str, Any]:
    endpoint = "movie" if media_type == "movie" else "tv"
    params: dict[str, str] = {
        "api_key": api_key,
        "query": title,
        "language": language,
        "include_adult": "false",
        "page": "1",
    }
    if year:
        params["year" if endpoint == "movie" else "first_air_date_year"] = year
    try:
        _TMDB_RATE_LIMITER.max_calls = max(1, rate_limit_per_minute)
        await _TMDB_RATE_LIMITER.acquire()
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"https://api.themoviedb.org/3/search/{endpoint}",
                params=params,
            )
            response.raise_for_status()
    except httpx.HTTPError:
        return {}
    payload = response.json()
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return {}
    first = results[0] if isinstance(results[0], dict) else {}
    poster_path = str(first.get("poster_path") or "").strip()
    overview = str(first.get("overview") or "").strip()
    release_date = str(first.get("release_date") or first.get("first_air_date") or "")
    return {
        "tmdb_id": str(first.get("id") or ""),
        "image_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "",
        "overview": overview,
        "year": release_date[:4] if release_date else "",
    }


def _public_url(path: str, public_base_url: str) -> str:
    if not public_base_url or path.startswith(("http://", "https://")):
        return path
    return f"{public_base_url}{path}"


def _inline_public_css() -> str:
    css_path = Path(__file__).resolve().parents[1] / "static" / "app.css"
    try:
        return css_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _json_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    channels = []
    for channel in payload["channels"]:
        programs = [_json_program(program) for program in channel["programs"]]
        channels.append({
            "id": channel["id"],
            "name": channel["name"],
            "logo_url": channel["logo_url"],
            "has_schedule": channel["has_schedule"],
            "version": channel["version"],
            "status": channel["status"],
            "created_at": channel["created_at"],
            "current": _json_program(channel["current"]) if channel["current"] else None,
            "next": _json_program(channel["next"]) if channel["next"] else None,
            "upcoming": [_json_program(program) for program in channel["upcoming"]],
            "programs": programs,
        })
    return {
        "schema": payload["schema"],
        "generated_at": payload["generated_at"].isoformat(),
        "timezone": payload["timezone"],
        "view": payload["view"],
        "period": payload["period"],
        "selected_channel": payload["selected_channel"],
        "selected_date": payload["selected_date"].isoformat(),
        "window_start": payload["window_start"].isoformat(),
        "window_end": payload["window_end"].isoformat(),
        "channels": channels,
    }


def _json_program(program: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": program["type"],
        "kind": program["kind"],
        "title": program["title"],
        "subtitle": program["subtitle"],
        "overview": program["overview"],
        "start": program["start"].isoformat(),
        "end": program["end"].isoformat(),
        "duration_minutes": program["duration_minutes"],
        "is_current": program["is_current"],
        "starts_after_now": program["starts_after_now"],
        "progress_percent": program["progress_percent"],
        "image_url": program["image_url"],
        "series_name": program["series_name"],
        "episode_title": program["episode_title"],
        "episode_code": program["episode_code"],
        "genres": program["genres"],
        "year": program["year"],
        "metadata_links": program["metadata_links"],
    }


def _xmltv_payload(payload: dict[str, Any]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="FlixWolf Tunarr AutoScheduler">',
    ]
    for channel in payload["channels"]:
        channel_id = _xml_id(str(channel["id"]))
        lines.append(f'  <channel id="{escape(channel_id, quote=True)}">')
        lines.append(f'    <display-name>{escape(str(channel["name"]))}</display-name>')
        if channel.get("logo_url"):
            lines.append(f'    <icon src="{escape(str(channel["logo_url"]), quote=True)}" />')
        lines.append("  </channel>")
    for channel in payload["channels"]:
        channel_id = _xml_id(str(channel["id"]))
        for program in channel["programs"]:
            start = _xmltv_time(program["start"])
            stop = _xmltv_time(program["end"])
            lines.append(
                f'  <programme start="{start}" stop="{stop}" '
                f'channel="{escape(channel_id, quote=True)}">',
            )
            title = str(program.get("title") or "")
            subtitle = str(program.get("subtitle") or "")
            desc = str(program.get("overview") or "")
            lines.append(f"    <title>{escape(title)}</title>")
            if subtitle:
                lines.append(f"    <sub-title>{escape(subtitle)}</sub-title>")
            if desc:
                lines.append(f"    <desc>{escape(desc)}</desc>")
            if program.get("kind"):
                lines.append(f"    <category>{escape(str(program['kind']))}</category>")
            if program.get("year"):
                lines.append(f"    <date>{escape(str(program['year']))}</date>")
            if program.get("image_url"):
                lines.append(
                    f'    <icon src="{escape(str(program["image_url"]), quote=True)}" />',
                )
            lines.append("  </programme>")
    lines.append("</tv>")
    return "\n".join(lines) + "\n"


def _xmltv_time(value: datetime) -> str:
    offset = value.strftime("%z")
    return f"{value:%Y%m%d%H%M%S} {offset}"


def _xml_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value)
    return safe or "channel"
