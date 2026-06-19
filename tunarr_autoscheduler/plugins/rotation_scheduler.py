from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tunarr_autoscheduler.core.plugin_loader import PipelineContext, Plugin
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.tunarr.programs import (
    program_duration_seconds,
    program_ids,
    program_show_id,
    program_subtype,
    unwrap_program,
)
from tunarr_autoscheduler.models.blocks import EpisodeBlock, OfflineBlock, SlotBlock
from tunarr_autoscheduler.models.schedule import RotationState

logger = logging.getLogger(__name__)


class RotationScheduler(Plugin):
    name = "rotation_scheduler"

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        channel_config = context.channel_config

        rotations = {rotation.name: rotation for rotation in channel_config.rotations}

        result = Timeline(metadata=dict(timeline.metadata))
        scheduled_episode_ids = {
            block.episode_id
            for block in timeline.blocks
            if isinstance(block, EpisodeBlock) and block.episode_id
        }
        scheduled_episode_ids.update(context.reserved_episode_ids)

        cursor: datetime | None = None
        cursor_daypart = ""
        for block in sorted(timeline.blocks, key=lambda item: item.start_time):
            if not isinstance(block, SlotBlock):
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                cursor_daypart = str(block.metadata.get("daypart", ""))
                continue

            metadata = block.metadata or {}
            daypart = str(metadata.get("daypart", ""))
            content_mode = str(metadata.get("content_mode", "series"))
            if (
                cursor is not None
                and daypart
                and daypart == cursor_daypart
                and block.end_time <= cursor
                and not metadata.get("custom_show_loop")
            ):
                continue
            allow_movies = bool(metadata.get("allow_movies", False))
            rotation_name = str(metadata.get("rotation", "default"))
            rotation_config = rotations.get(rotation_name)
            custom_show_list_ids = _string_list(metadata.get("custom_show_list_ids", []))
            playlist_ids = _string_list(metadata.get("playlist_ids", []))
            fallback_show_ids = rotation_config.show_ids if rotation_config else []
            custom_show_loop = bool(metadata.get("custom_show_loop", False))

            if metadata.get("off_air"):
                start_time = _scheduled_start(block.start_time, cursor)
                end_time = block.end_time
                if start_time >= end_time:
                    cursor = _advance_cursor(cursor, end_time)
                    cursor_daypart = daypart
                    continue
                offline_block = OfflineBlock(
                    start_time=start_time,
                    end_time=end_time,
                    duration=end_time - start_time,
                    reason="Off-Air Loop" if custom_show_list_ids else "Off-Air",
                    metadata={
                        **metadata,
                        "title": "Off-Air Loop" if custom_show_list_ids else "Off-Air",
                    },
                )
                result.insert(offline_block)
                cursor = _advance_cursor(cursor, offline_block.end_time)
                cursor_daypart = daypart
                continue

            if content_mode == "movies":
                block = _shift_slot_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                cursor_daypart = daypart
                continue

            if allow_movies and not custom_show_list_ids and not playlist_ids:
                block = _shift_slot_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue
            if not custom_show_list_ids and not playlist_ids and not fallback_show_ids:
                block = _shift_slot_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            playlist_episodes = await self._get_playlist_episodes(context, playlist_ids)
            custom_episodes = (
                await self._get_custom_show_episodes(context, custom_show_list_ids)
                if not playlist_episodes else []
            )
            preferred_episodes = playlist_episodes or custom_episodes
            preferred_show_ids = _ordered_show_ids(preferred_episodes)
            if allow_movies and (custom_show_list_ids or playlist_ids) and not preferred_episodes:
                block = _shift_slot_after_cursor(block, cursor)
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            selected_rotation_name = (
                f"playlist:{','.join(playlist_ids)}"
                if playlist_episodes
                else (
                    f"custom:{','.join(custom_show_list_ids)}"
                    if custom_episodes else rotation_name
                )
            )
            selected_show_ids = preferred_show_ids or fallback_show_ids
            episodes = preferred_episodes
            if not episodes and fallback_show_ids:
                episodes = await self._get_available_episodes(context, fallback_show_ids)
                selected_show_ids = fallback_show_ids
                selected_rotation_name = rotation_name
            elif (
                not episodes
                and (custom_show_list_ids or playlist_ids)
                and not allow_movies
            ):
                episodes = await self._get_available_episodes(context, [])
                selected_show_ids = _ordered_show_ids(episodes)
                selected_rotation_name = "library"
            if not episodes or not selected_show_ids:
                block = _shift_slot_after_cursor(block, cursor)
                block.metadata["note"] = "no_episodes_available"
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            state = await self._get_or_create_state(
                context, channel_config.id, selected_rotation_name,
            )
            if custom_show_loop and preferred_episodes:
                ep, selected_show_index = self._select_loop_episode(
                    preferred_episodes,
                    state.current_index,
                )
            else:
                ep, selected_show_index = await self._select_episode(
                    context=context,
                    channel_id=channel_config.id,
                    show_ids=selected_show_ids,
                    episodes=episodes,
                    current_show_index=state.current_index,
                    scheduled_episode_ids=scheduled_episode_ids,
                )
            if ep is None and preferred_episodes and fallback_show_ids:
                fallback_episodes = await self._get_available_episodes(context, fallback_show_ids)
                state = await self._get_or_create_state(context, channel_config.id, rotation_name)
                ep, selected_show_index = await self._select_episode(
                    context=context,
                    channel_id=channel_config.id,
                    show_ids=fallback_show_ids,
                    episodes=fallback_episodes,
                    current_show_index=state.current_index,
                    scheduled_episode_ids=scheduled_episode_ids,
                )
                selected_show_ids = fallback_show_ids
            elif ep is None and preferred_episodes:
                fallback_episodes = await self._get_available_episodes(context, [])
                fallback_show_ids = _ordered_show_ids(fallback_episodes)
                if fallback_show_ids:
                    state = await self._get_or_create_state(context, channel_config.id, "library")
                    ep, selected_show_index = await self._select_episode(
                        context=context,
                        channel_id=channel_config.id,
                        show_ids=fallback_show_ids,
                        episodes=fallback_episodes,
                        current_show_index=state.current_index,
                        scheduled_episode_ids=scheduled_episode_ids,
                    )
                    selected_show_ids = fallback_show_ids
            if ep is None:
                block = _shift_slot_after_cursor(block, cursor)
                block.metadata["note"] = "no_unused_episodes_available"
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                continue

            duration_seconds = ep.get("duration_seconds", 1800)
            start_time = _scheduled_start(block.start_time, cursor)
            boundary = _daypart_boundary(metadata)
            effective_boundary = _daypart_boundary(metadata, include_tolerance=True)
            if (
                effective_boundary is not None
                and start_time + timedelta(seconds=duration_seconds) > effective_boundary
            ):
                block = _remaining_slot(block, start_time, boundary or effective_boundary)
                block.metadata["note"] = "episode_does_not_fit_daypart"
                result.insert(block)
                cursor = _advance_cursor(cursor, block.end_time)
                cursor_daypart = daypart
                continue

            episode_block = EpisodeBlock(
                start_time=start_time,
                end_time=start_time + timedelta(seconds=duration_seconds),
                duration=timedelta(seconds=duration_seconds),
                episode_id=ep.get("id", ""),
                show_id=ep.get("show_id", ""),
                season_number=ep.get("season_number", 1),
                episode_number=ep.get("episode_number", 1),
                runtime_seconds=duration_seconds,
                metadata={
                    **metadata,
                    "title": ep.get("title", ""),
                    "show_name": ep.get("show_name", ""),
                },
            )
            result.insert(episode_block)
            cursor = _advance_cursor(cursor, episode_block.end_time)
            cursor_daypart = daypart
            scheduled_episode_ids.add(episode_block.episode_id)

            if custom_show_loop and preferred_episodes:
                index_modulo = len(preferred_episodes)
            else:
                index_modulo = len(selected_show_ids)
            state.current_index = (selected_show_index + 1) % index_modulo
            state.current_show_id = ep.get("show_id", "")
            state.episode_counter = 1
            state.last_rotation_time = datetime.now(tz=UTC)

            state_mgr = getattr(context, "state", None)
            if state_mgr:
                await state_mgr.save_rotation_state(state)

        return result

    def _select_loop_episode(
        self, episodes: list[dict[str, Any]], current_index: int,
    ) -> tuple[dict[str, Any] | None, int]:
        if not episodes:
            return None, current_index
        sorted_episodes = sorted(episodes, key=self._episode_sort_key)
        selected_index = current_index % len(sorted_episodes)
        return sorted_episodes[selected_index], selected_index

    async def _select_episode(
        self,
        context: PipelineContext,
        channel_id: str,
        show_ids: list[str],
        episodes: list[dict[str, Any]],
        current_show_index: int,
        scheduled_episode_ids: set[str],
    ) -> tuple[dict[str, Any] | None, int]:
        episodes_by_show: dict[str, list[dict[str, Any]]] = {show_id: [] for show_id in show_ids}
        for episode in episodes:
            episodes_by_show.setdefault(str(episode.get("show_id", "")), []).append(episode)

        for show_episodes in episodes_by_show.values():
            show_episodes.sort(key=self._episode_sort_key)

        state_mgr = (
            None if context.generation_mode == "fresh"
            else getattr(context, "state", None)
        )
        for offset in range(len(show_ids)):
            show_index = (current_show_index + offset) % len(show_ids)
            show_id = show_ids[show_index]
            for episode in episodes_by_show.get(show_id, []):
                episode_id = str(episode.get("id", ""))
                if not episode_id or episode_id in scheduled_episode_ids:
                    continue
                if state_mgr and await self._was_recently_aired(
                    state_mgr, channel_id, episode_id,
                ):
                    continue
                return episode, show_index
        return None, current_show_index

    def _episode_sort_key(self, episode: dict[str, Any]) -> tuple[int, int, str]:
        return (
            int(episode.get("season_number") or 0),
            int(episode.get("episode_number") or 0),
            str(episode.get("id", "")),
        )

    async def _was_recently_aired(
        self, state_mgr: Any, channel_id: str, episode_id: str,
    ) -> bool:
        since = datetime.now(tz=UTC) - timedelta(days=365)
        history = await state_mgr.get_air_history(channel_id, episode_id, since)
        return bool(history)

    async def _get_or_create_state(
        self, context: PipelineContext, channel_id: str, rotation_name: str,
    ) -> RotationState:
        if context.generation_mode == "fresh":
            state = context.local_rotation_states.get(rotation_name)
            if state is None:
                state = RotationState(channel_id=channel_id, rotation_name=rotation_name)
                context.local_rotation_states[rotation_name] = state
            return cast(RotationState, state)
        state_mgr = getattr(context, "state", None)
        if state_mgr:
            existing = await state_mgr.get_rotation_state(channel_id, rotation_name)
            if existing:
                return cast(RotationState, existing)
        return RotationState(
            channel_id=channel_id,
            rotation_name=rotation_name,
        )

    async def _get_available_episodes(
        self, context: PipelineContext, show_ids: list[str],
    ) -> list[dict[str, Any]]:
        repo = getattr(context, "media_repo", None)
        if not repo:
            return [
                {"id": f"{sid}-ep-{i}", "show_id": sid, "title": f"Episode {i}",
                 "season_number": 1, "episode_number": i + 1, "duration_seconds": 1800,
                 "show_name": f"Show {sid[:8]}"}
                for sid in show_ids
                for i in range(10)
            ]

        all_media = await repo.get_all_available(item_type="episode")
        if show_ids:
            episodes = [
                m for m in all_media
                if m.metadata and m.metadata.get("series_id") in show_ids
            ]
        else:
            episodes = [m for m in all_media if m.metadata and m.metadata.get("series_id")]
        result = [
            {
                "id": e.id,
                "show_id": e.metadata.get("series_id", ""),
                "title": e.title,
                "season_number": e.metadata.get("parent_index_number", 1),
                "episode_number": e.metadata.get("index_number", 1),
                "duration_seconds": e.duration_seconds or 1800,
                "show_name": e.metadata.get("series_name", ""),
            }
            for e in episodes
        ]
        return await context.filter_tunarr_media(result)

    async def _get_custom_show_episodes(
        self, context: PipelineContext, custom_show_list_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not custom_show_list_ids:
            return []
        if context.tunarr_client is None:
            return []

        repo = getattr(context, "media_repo", None)
        episodes: list[dict[str, Any]] = []
        for custom_show_id in custom_show_list_ids:
            try:
                programs = await context.get_custom_show_programs(custom_show_id)
            except Exception as e:
                logger.warning("Failed to load Tunarr custom show %s: %s", custom_show_id, e)
                continue
            for program in programs:
                if program_subtype(program) != "episode":
                    continue
                source = unwrap_program(program)
                candidate_ids = program_ids(program)
                entry = await _find_media_entry(repo, candidate_ids) if repo else None
                if repo is not None and entry is None:
                    continue
                show_id = (
                    (entry.metadata or {}).get("series_id")
                    if entry and entry.metadata else program_show_id(program)
                )
                episodes.append({
                    "id": entry.id if entry else (candidate_ids[0] if candidate_ids else ""),
                    "show_id": str(show_id or ""),
                    "title": entry.title if entry else str(source.get("title", "")),
                    "season_number": (
                        (entry.metadata or {}).get("parent_index_number")
                        if entry else source.get("seasonNumber", 1)
                    ),
                    "episode_number": (
                        (entry.metadata or {}).get("index_number")
                        if entry else source.get("episodeNumber", 1)
                    ),
                    "duration_seconds": (
                        entry.duration_seconds
                        if entry and entry.duration_seconds
                        else program_duration_seconds(program, 1800)
                    ),
                    "show_name": (
                        (entry.metadata or {}).get("series_name")
                        if entry else str(source.get("showTitle") or source.get("title", ""))
                    ),
                })
        valid = [episode for episode in episodes if episode["id"] and episode["show_id"]]
        return await context.filter_tunarr_media(valid)

    async def _get_playlist_episodes(
        self, context: PipelineContext, playlist_ids: list[str],
    ) -> list[dict[str, Any]]:
        playlist_repo = getattr(context, "playlist_repo", None)
        if not playlist_ids or playlist_repo is None:
            return []
        items = await playlist_repo.get_items(playlist_ids)
        show_ids = [
            item.media_id
            for item in items
            if item.media_type == "series"
        ]
        if not show_ids:
            return []
        episodes = await self._get_available_episodes(context, show_ids)
        order = {show_id: index for index, show_id in enumerate(show_ids)}
        return sorted(
            episodes,
            key=lambda episode: (
                order.get(str(episode.get("show_id", "")), len(order)),
                self._episode_sort_key(episode),
            ),
        )


async def _find_media_entry(repo: Any, candidate_ids: list[str]) -> Any:
    for candidate_id in candidate_ids:
        entry = await repo.get(candidate_id)
        if entry and entry.available:
            return entry
        entry = await repo.get_by_source("jellyfin", candidate_id)
        if entry and entry.available:
            return entry
    return None


def _ordered_show_ids(episodes: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    show_ids: list[str] = []
    for episode in episodes:
        show_id = str(episode.get("show_id", ""))
        if show_id and show_id not in seen:
            show_ids.append(show_id)
            seen.add(show_id)
    return show_ids


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


def _remaining_slot(
    block: SlotBlock, start_time: datetime, boundary: datetime,
) -> SlotBlock:
    block.start_time = min(start_time, boundary)
    block.end_time = boundary
    block.duration = max(boundary - block.start_time, timedelta())
    return block
