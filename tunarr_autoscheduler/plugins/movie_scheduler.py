from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.tunarr.programs import (
    program_duration_seconds,
    program_ids,
    program_subtype,
    unwrap_program,
)
from tunarr_autoscheduler.models.blocks import MovieBlock, OfflineBlock, SlotBlock

logger = logging.getLogger(__name__)


class MovieScheduler(Plugin):
    name = "movie_scheduler"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config
        movies = await self._get_available_movies(context)
        result = Timeline(metadata=dict(timeline.metadata))
        scheduled_movie_ids = {
            block.movie_id
            for block in timeline.blocks
            if isinstance(block, MovieBlock) and block.movie_id
        }
        scheduled_movie_ids.update(context.reserved_movie_ids)

        cursor: datetime | None = None
        cursor_daypart = ""
        for block in sorted(timeline.blocks, key=lambda item: item.start_time):
            if not isinstance(block, SlotBlock):
                if isinstance(block, OfflineBlock):
                    clipped = _clip_offline_block_after_cursor(block, cursor)
                    if clipped is None:
                        cursor = _advance_cursor(cursor, block.end_time)
                        cursor_daypart = str(block.metadata.get("daypart", ""))
                        continue
                    block = clipped
                else:
                    block = _shift_block_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                cursor_daypart = str(block.metadata.get("daypart", ""))
                continue

            metadata = block.metadata or {}
            daypart = str(metadata.get("daypart", ""))
            content_mode = str(metadata.get("content_mode", "mixed"))
            if (
                cursor is not None
                and daypart
                and daypart == cursor_daypart
                and block.end_time <= cursor
            ):
                continue
            allow_movies = bool(metadata.get("allow_movies", True)) or content_mode == "movies"
            variable_movie_duration = bool(metadata.get("variable_movie_duration", False))
            movie_selection = str(metadata.get("movie_selection", "best_fit"))
            slot_minutes = metadata.get("slot_duration_minutes", 60)

            if not allow_movies:
                block = _shift_slot_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            custom_movies = await self._get_custom_show_movies(
                context, _string_list(metadata.get("custom_show_list_ids", [])),
            )
            playlist_movies = await self._get_playlist_movies(
                context, _string_list(metadata.get("playlist_ids", [])),
            )
            candidate_movies = playlist_movies or custom_movies or movies
            if not candidate_movies:
                block = _shift_slot_after_cursor(block, cursor)
                block.metadata["note"] = "no_movies_available"
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            best_movie = None
            available_movies = await self._filter_available_movies(
                context, channel_config.id, candidate_movies, scheduled_movie_ids,
            )
            if not available_movies and (playlist_movies or custom_movies):
                available_movies = await self._filter_available_movies(
                    context, channel_config.id, movies, scheduled_movie_ids,
                )
            ad_density = metadata.get("ad_density", 0.12)
            start_time = _scheduled_start(block.start_time, cursor)
            effective_boundary = _daypart_boundary(metadata, include_tolerance=True)
            available_window_seconds = _available_movie_window_seconds(
                content_mode=content_mode,
                start_time=start_time,
                slot_minutes=slot_minutes,
                effective_boundary=effective_boundary,
            )
            overhead = available_window_seconds * ad_density
            available_seconds = (
                available_window_seconds
                + (
                    0
                    if content_mode == "movies" and effective_boundary is not None
                    else _int_metadata(metadata, "end_tolerance_minutes") * 60
                )
                - overhead
            )
            fitting_movies = [
                movie
                for movie in available_movies
                if int(movie.get("duration_seconds", 0)) <= available_seconds
            ]
            selectable_movies = available_movies if variable_movie_duration else fitting_movies
            if movie_selection == "library_random" and selectable_movies:
                best_movie = _select_stable_random_movie(
                    selectable_movies,
                    context.channel_config.id,
                    daypart,
                    block.start_time,
                )
            elif variable_movie_duration and available_movies:
                best_movie = available_movies[0]
            else:
                for movie in fitting_movies:
                    runtime = movie.get("duration_seconds", 0)
                    if best_movie is None or runtime > best_movie.get("duration_seconds", 0):
                        best_movie = movie

            if best_movie:
                runtime_seconds = best_movie.get("duration_seconds", 5400)
                boundary = _daypart_boundary(metadata)
                if (
                    not variable_movie_duration
                    and effective_boundary is not None
                    and start_time + timedelta(seconds=runtime_seconds) > effective_boundary
                ):
                    block = _remaining_slot(block, start_time, boundary or effective_boundary)
                    block.metadata["note"] = "movie_does_not_fit_daypart"
                    result.insert(block)
                    cursor = _advance_cursor(cursor, block.end_time)
                    cursor_daypart = daypart
                    continue
                movie_block = MovieBlock(
                    start_time=start_time,
                    end_time=start_time + timedelta(seconds=runtime_seconds),
                    duration=timedelta(seconds=runtime_seconds),
                    movie_id=best_movie.get("id", ""),
                    runtime_seconds=runtime_seconds,
                    year=best_movie.get("year"),
                    metadata={**metadata, "title": best_movie.get("title", "")},
                )
                result.insert(movie_block)
                cursor = _advance_cursor(cursor, movie_block.end_time)
                cursor_daypart = daypart
                scheduled_movie_ids.add(movie_block.movie_id)

                from tunarr_autoscheduler.core.state import StateManager
                state: StateManager | None = getattr(context, "state", None)
                if state:
                    await state.set_cooldown(
                        best_movie["id"], "movie", channel_config.id, 7 * 24 * 60,
                    )
            else:
                block = _shift_slot_after_cursor(block, cursor)
                block.metadata["note"] = "no_movie_fits_slot"
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)

        return result

    async def _filter_available_movies(
        self,
        context: PipelineContext,
        channel_id: str,
        movies: list[dict[str, Any]],
        scheduled_movie_ids: set[str],
    ) -> list[dict[str, Any]]:
        state = getattr(context, "state", None)
        available: list[dict[str, Any]] = []
        for movie in movies:
            movie_id = str(movie.get("id", ""))
            if not movie_id or movie_id in scheduled_movie_ids:
                continue
            if state and await state.get_cooldown_remaining(movie_id) > 0:
                continue
            available.append(movie)
        return available

    async def _get_available_movies(self, context: PipelineContext) -> list[dict[str, Any]]:
        from tunarr_autoscheduler.db.repositories.media_repo import MediaRepository
        repo: MediaRepository | None = getattr(context, "media_repo", None)
        if not repo:
            return [
                {"id": "movie-1", "title": "Sample Movie", "duration_seconds": 5400, "year": 2020},
                {"id": "movie-2", "title": "Another Movie", "duration_seconds": 6300, "year": 2019},
            ]
        all_movies = await repo.get_all_available(item_type="movie")
        result = [
            {
                "id": m.id,
                "title": m.title,
                "duration_seconds": m.duration_seconds or 5400,
                "year": (m.metadata or {}).get("year"),
            }
            for m in all_movies
        ]
        return await context.filter_tunarr_media(result)

    async def _get_custom_show_movies(
        self, context: PipelineContext, custom_show_list_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not custom_show_list_ids:
            return []
        if context.tunarr_client is None:
            return []

        repo = getattr(context, "media_repo", None)
        movies: list[dict[str, Any]] = []
        for custom_show_id in custom_show_list_ids:
            try:
                programs = await context.get_custom_show_programs(custom_show_id)
            except Exception as e:
                logger.warning("Failed to load Tunarr custom show %s: %s", custom_show_id, e)
                continue
            for program in programs:
                if program_subtype(program) != "movie":
                    continue
                source = unwrap_program(program)
                candidate_ids = program_ids(program)
                entry = await _find_media_entry(repo, candidate_ids) if repo else None
                movies.append({
                    "id": entry.id if entry else (candidate_ids[0] if candidate_ids else ""),
                    "title": entry.title if entry else str(source.get("title", "")),
                    "duration_seconds": (
                        entry.duration_seconds
                        if entry and entry.duration_seconds
                        else program_duration_seconds(program, 5400)
                    ),
                    "year": (entry.metadata or {}).get("year") if entry else source.get("year"),
                })
        valid = [movie for movie in movies if movie["id"]]
        return await context.filter_tunarr_media(valid)

    async def _get_playlist_movies(
        self, context: PipelineContext, playlist_ids: list[str],
    ) -> list[dict[str, Any]]:
        playlist_repo = getattr(context, "playlist_repo", None)
        if not playlist_ids or playlist_repo is None:
            return []
        items = await playlist_repo.get_items(playlist_ids)
        movie_ids = [
            item.media_id
            for item in items
            if item.media_type == "movie"
        ]
        if not movie_ids:
            return []
        available = {
            movie["id"]: movie
            for movie in await self._get_available_movies(context)
        }
        return [
            available[movie_id]
            for movie_id in movie_ids
            if movie_id in available
        ]


async def _find_media_entry(repo: Any, candidate_ids: list[str]) -> Any:
    for candidate_id in candidate_ids:
        entry = await repo.get(candidate_id)
        if entry and entry.available:
            return entry
        entry = await repo.get_by_source("jellyfin", candidate_id)
        if entry and entry.available:
            return entry
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _scheduled_start(start_time: datetime, cursor: datetime | None) -> datetime:
    if cursor is None or start_time >= cursor:
        return start_time
    return cursor


def _advance_cursor(cursor: datetime | None, end_time: datetime) -> datetime:
    if cursor is None or end_time > cursor:
        return end_time
    return cursor


def _shift_slot_after_cursor(block: SlotBlock, cursor: datetime | None) -> SlotBlock:
    start_time = _scheduled_start(block.start_time, cursor)
    if start_time == block.start_time:
        return block
    block.start_time = start_time
    block.end_time = start_time + block.duration
    return block


def _shift_block_after_cursor(block: Any, cursor: datetime | None) -> Any:
    start_time = _scheduled_start(block.start_time, cursor)
    if start_time == block.start_time:
        return block
    block.start_time = start_time
    block.end_time = start_time + block.duration
    return block


def _available_movie_window_seconds(
    *,
    content_mode: str,
    start_time: datetime,
    slot_minutes: Any,
    effective_boundary: datetime | None,
) -> float:
    slot_seconds = timedelta(minutes=_int_value(slot_minutes, default=60)).total_seconds()
    if content_mode != "movies" or effective_boundary is None:
        return slot_seconds
    return max(0.0, min(
        (effective_boundary - start_time).total_seconds(),
        timedelta(days=1).total_seconds(),
    ))


def _clip_offline_block_after_cursor(
    block: OfflineBlock, cursor: datetime | None,
) -> OfflineBlock | None:
    start_time = _scheduled_start(block.start_time, cursor)
    if start_time >= block.end_time:
        return None
    if start_time == block.start_time:
        return block
    block.start_time = start_time
    block.duration = block.end_time - block.start_time
    return block


def _select_stable_random_movie(
    movies: list[dict[str, Any]],
    channel_id: str,
    daypart: str,
    slot_start: datetime,
) -> dict[str, Any]:
    if len(movies) == 1:
        return movies[0]
    seed = f"{channel_id}:{daypart}:{slot_start.date().isoformat()}:{slot_start.hour}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    start_index = int(digest[:8], 16) % len(movies)
    ordered = sorted(movies, key=lambda movie: str(movie.get("id", "")))
    return ordered[start_index]


def _daypart_boundary(
    metadata: dict[str, Any], *, include_tolerance: bool = False,
) -> datetime | None:
    raw = metadata.get("daypart_boundary")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        boundary = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if include_tolerance:
        boundary += timedelta(minutes=_int_metadata(metadata, "end_tolerance_minutes"))
    return boundary


def _int_metadata(metadata: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(metadata.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def _int_value(value: Any, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _remaining_slot(
    block: SlotBlock, start_time: datetime, boundary: datetime,
) -> SlotBlock:
    block.start_time = min(start_time, boundary)
    block.end_time = boundary
    block.duration = max(boundary - block.start_time, timedelta())
    return block
