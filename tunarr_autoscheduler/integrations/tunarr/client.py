from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

import httpx

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    MovieBlock,
    OfflineBlock,
    StationIDBlock,
)

DAY_MS = 24 * 60 * 60 * 1000
logger = logging.getLogger(__name__)


class TunarrClient:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def check_connection(self) -> bool:
        try:
            await self.get_channels()
        except httpx.HTTPError:
            return False
        return True

    async def get_channels(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/channels")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        resp = await self._client.get(f"/api/channels/{channel_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_media_sources(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/media-sources")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_filler_lists(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/filler-lists")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/api/filler-lists/{filler_list_id}/programs")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_custom_shows(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/custom-shows")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_custom_show_programs(self, custom_show_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/api/custom-shows/{custom_show_id}/programs")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def upload_timeline(
        self,
        channel_id: str,
        timeline: Timeline,
        *,
        station_id_custom_show_id: str = "",
        bumper_custom_show_id: str = "",
    ) -> dict[str, Any]:
        blocks = sorted(timeline.blocks, key=lambda block: block.start_time)
        if not blocks:
            raise ValueError("Cannot upload an empty timeline")

        media_source_id = await self._jellyfin_media_source_id()
        program_ids = await self._resolve_program_ids(blocks, media_source_id)
        custom_cache: dict[str, list[dict[str, Any]]] = {}
        lineup: list[dict[str, Any]] = []
        timeline_start = blocks[0].start_time
        cursor = timeline_start

        for block in blocks:
            if block.start_time > cursor:
                _append_flex(lineup, _duration_ms(block.start_time - cursor))
            duration_ms = _duration_ms(block.duration)
            if isinstance(block, (EpisodeBlock, MovieBlock)):
                external_id = (
                    block.episode_id if isinstance(block, EpisodeBlock) else block.movie_id
                )
                lineup.append({
                    "type": "content",
                    "duration": duration_ms,
                    "id": program_ids[external_id],
                })
            elif isinstance(block, AdBlock):
                await self._append_ad_block(lineup, block)
            elif isinstance(block, StationIDBlock):
                custom_show_id = (
                    bumper_custom_show_id
                    if block.metadata.get("type") == "up_next"
                    else station_id_custom_show_id
                )
                await self._append_custom_block(
                    lineup,
                    custom_cache,
                    custom_show_id,
                    duration_ms,
                    preferred_id=block.clip_id,
                )
            elif isinstance(block, OfflineBlock):
                custom_show_ids = block.metadata.get("custom_show_list_ids", [])
                custom_show_id = (
                    str(custom_show_ids[0])
                    if isinstance(custom_show_ids, list) and custom_show_ids
                    else ""
                )
                await self._append_custom_block(
                    lineup,
                    custom_cache,
                    custom_show_id,
                    duration_ms,
                    repeat=True,
                )
            else:
                _append_flex(lineup, duration_ms)
            cursor = max(cursor, block.end_time)

        expected_duration = _duration_ms(blocks[-1].end_time - timeline_start)
        actual_duration = sum(float(item["duration"]) for item in lineup)
        if abs(actual_duration - expected_duration) > 1:
            raise RuntimeError(
                "Exact Tunarr lineup duration mismatch: "
                f"expected {expected_duration}ms, built {actual_duration}ms",
            )

        resp = await self._client.post(
            f"/api/channels/{channel_id}/programming",
            json={"type": "manual", "lineup": lineup, "append": False},
        )
        resp.raise_for_status()
        channel_status = await self._update_channel_timing(
            channel_id,
            start_time=timeline_start,
            duration_ms=expected_duration,
        )
        stored = await self._client.get(f"/api/channels/{channel_id}/programming")
        stored.raise_for_status()
        stored_data = stored.json()
        stored_lineup = stored_data.get("lineup", [])
        if not isinstance(stored_lineup, list) or not stored_lineup:
            raise RuntimeError("Tunarr stored an empty lineup")
        stored_duration = sum(
            float(item.get("duration", 0))
            for item in stored_lineup
            if isinstance(item, dict)
        )
        expected_content = sum(
            isinstance(block, (EpisodeBlock, MovieBlock)) for block in blocks
        )
        stored_content = sum(
            isinstance(item, dict) and item.get("type") == "content"
            for item in stored_lineup
        )
        if abs(stored_duration - expected_duration) > 1:
            raise RuntimeError(
                "Tunarr stored an incomplete lineup: "
                f"expected {expected_duration}ms, got {stored_duration}ms",
            )
        if stored_content != expected_content:
            raise RuntimeError(
                "Tunarr stored the wrong number of scheduled programs: "
                f"expected {expected_content}, got {stored_content}",
            )
        stored_data["_upload"] = {
            "mode": "manual",
            "persistent_time_status": "not_attempted",
            "programming_status": resp.status_code,
            "channel_update_status": channel_status,
            "verification_status": stored.status_code,
            "final_status": stored.status_code,
            "duration_ms": expected_duration,
            "lineup_items": len(stored_lineup),
            "content_items": stored_content,
            "fallback_used": False,
        }
        return stored_data  # type: ignore[no-any-return]

    async def _jellyfin_media_source_id(self) -> str:
        sources = await self.get_media_sources()
        source = next(
            (
                item for item in sources
                if item.get("type") == "jellyfin" and item.get("id")
            ),
            None,
        )
        if source is None:
            raise RuntimeError("Tunarr has no Jellyfin media source")
        return str(source["id"])

    async def _resolve_program_ids(
        self, blocks: list[Any], media_source_id: str,
    ) -> dict[str, str]:
        external_item_ids = [
            block.episode_id if isinstance(block, EpisodeBlock) else block.movie_id
            for block in blocks
            if isinstance(block, (EpisodeBlock, MovieBlock))
        ]
        unique_ids = list(dict.fromkeys(item_id for item_id in external_item_ids if item_id))
        if not unique_ids:
            return {}
        resolved = await self.resolve_jellyfin_program_ids(
            unique_ids, media_source_id=media_source_id,
        )
        missing = [item_id for item_id in unique_ids if item_id not in resolved]
        if missing:
            raise RuntimeError(
                f"Tunarr is missing {len(missing)} scheduled media item(s): "
                + ", ".join(missing[:5]),
            )
        return resolved

    async def resolve_jellyfin_program_ids(
        self,
        item_ids: list[str],
        *,
        media_source_id: str | None = None,
        batch_size: int = 500,
    ) -> dict[str, str]:
        source_id = media_source_id or await self._jellyfin_media_source_id()
        unique_ids = list(dict.fromkeys(item_id for item_id in item_ids if item_id))
        resolved: dict[str, str] = {}
        for offset in range(0, len(unique_ids), batch_size):
            batch = unique_ids[offset:offset + batch_size]
            resp = await self._client.post(
                "/api/programming/batch/lookup",
                json={
                    "externalIds": [
                        f"jellyfin|{source_id}|{item_id}" for item_id in batch
                    ],
                },
            )
            resp.raise_for_status()
            programs = resp.json()
            if not isinstance(programs, dict):
                continue
            for program in programs.values():
                if not isinstance(program, dict) or not program.get("uuid"):
                    continue
                for identifier in program.get("identifiers", []):
                    if (
                        isinstance(identifier, dict)
                        and identifier.get("type") == "jellyfin"
                        and identifier.get("id")
                    ):
                        resolved[str(identifier["id"])] = str(program["uuid"])
        return resolved

    async def _append_ad_block(
        self, lineup: list[dict[str, Any]], block: AdBlock,
    ) -> None:
        filler_list_id = str(block.metadata.get("filler_list_id", ""))
        spots = block.metadata.get("spots", [])
        remaining = _duration_ms(block.duration)
        if not filler_list_id or not isinstance(spots, list):
            _append_flex(lineup, remaining)
            return
        for spot in spots:
            if remaining <= 0 or not isinstance(spot, dict) or not spot.get("id"):
                continue
            spot_duration = min(
                remaining,
                max(1, int(float(spot.get("duration_seconds", 0)) * 1000)),
            )
            lineup.append({
                "type": "filler",
                "duration": spot_duration,
                "id": str(spot["id"]),
                "fillerListId": filler_list_id,
            })
            remaining -= spot_duration
        _append_flex(lineup, remaining)

    async def _append_custom_block(
        self,
        lineup: list[dict[str, Any]],
        cache: dict[str, list[dict[str, Any]]],
        custom_show_id: str,
        duration_ms: int,
        *,
        preferred_id: str = "",
        repeat: bool = False,
    ) -> None:
        if not custom_show_id:
            _append_flex(lineup, duration_ms)
            return
        programs = cache.get(custom_show_id)
        if programs is None:
            programs = await self.get_custom_show_programs(custom_show_id)
            cache[custom_show_id] = programs
        candidates = [
            item for item in programs
            if isinstance(item, dict)
            and item.get("id")
            and isinstance(item.get("duration"), (int, float))
            and float(item["duration"]) > 0
        ]
        if preferred_id:
            preferred = [item for item in candidates if str(item["id"]) == preferred_id]
            if preferred:
                candidates = preferred
        if not candidates:
            _append_flex(lineup, duration_ms)
            return

        remaining = duration_ms
        index = 0
        while remaining > 0 and (repeat or index == 0):
            item = candidates[index % len(candidates)]
            item_duration = min(remaining, max(1, int(float(item["duration"]))))
            lineup.append({
                "type": "custom",
                "duration": item_duration,
                "id": str(item["id"]),
                "customShowId": custom_show_id,
                "index": int(item.get("index", index % len(candidates))),
            })
            remaining -= item_duration
            index += 1
        _append_flex(lineup, remaining)

    async def _update_channel_timing(
        self, channel_id: str, start_time: datetime, duration_ms: int,
    ) -> int:
        channel = await self.get_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"Tunarr channel not found: {channel_id}")
        allowed = {
            "disableFillerOverlay", "duration", "fillerCollections",
            "fillerRepeatCooldown", "groupTitle", "guideFlexTitle",
            "guideMinimumDuration", "icon", "id", "name", "number", "offline",
            "startTime", "stealth", "watermark", "onDemand", "streamMode",
            "transcodeConfigId", "subtitlesEnabled", "subtitlePreferences",
        }
        payload = {key: value for key, value in channel.items() if key in allowed}
        payload["startTime"] = int(start_time.timestamp() * 1000)
        payload["duration"] = duration_ms
        resp = await self._client.put(f"/api/channels/{channel_id}", json=payload)
        resp.raise_for_status()
        return resp.status_code

    async def upload_schedule(
        self, channel_id: str, schedule_data: dict[str, Any],
    ) -> dict[str, Any]:
        schedule = _schedule_payload(schedule_data["schedule"])
        _generated_schedule, generated = await self.generate_schedule(
            channel_id, {"schedule": schedule},
        )
        return await self.persist_time_schedule_with_fallback(
            channel_id,
            schedule,
            generated,
        )

    async def persist_time_schedule_with_fallback(
        self,
        channel_id: str,
        schedule: dict[str, Any],
        generated: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self.persist_time_schedule(channel_id, schedule, generated)
        except httpx.HTTPStatusError as e:
            if not _is_tunarr_time_programming_rejection(e):
                raise
            logger.warning(
                "Tunarr rejected persistent time schedule for channel %s "
                "with status %s; falling back to generated manual lineup: %s",
                channel_id,
                e.response.status_code,
                e.response.text[:500],
            )
            return await self.persist_generated_schedule(
                channel_id,
                schedule,
                generated,
                persistent_error=e,
            )

    async def generate_schedule(
        self, channel_id: str, schedule_data: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        schedule = _schedule_payload(schedule_data["schedule"])
        generated_resp = await self._client.post(
            f"/api/channels/{channel_id}/schedule-time-slots",
            json={"schedule": schedule},
        )
        generated_resp.raise_for_status()
        generated = generated_resp.json()
        return schedule, generated

    async def persist_generated_schedule(
        self,
        channel_id: str,
        _schedule: dict[str, Any],
        generated: dict[str, Any],
        *,
        persistent_error: httpx.HTTPStatusError | None = None,
    ) -> dict[str, Any]:
        lineup = _manual_lineup(generated)
        if not lineup:
            raise RuntimeError(
                "Tunarr generated an empty manual lineup; refusing to persist upload",
            )
        resp = await self._client.post(
            f"/api/channels/{channel_id}/programming",
            json={
                "type": "manual",
                "lineup": lineup,
                "append": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data["_upload"] = {
                "mode": "generated_manual",
                "persistent_time_status": (
                    "failed_fallback" if persistent_error is not None else "not_attempted"
                ),
                "programming_status": resp.status_code,
                "final_status": resp.status_code,
                "fallback_used": persistent_error is not None,
                "fallback_reason": (
                    _status_error_text(persistent_error) if persistent_error else ""
                ),
                "lineup_items": len(lineup),
                "content_items": sum(
                    item.get("type") == "content"
                    for item in lineup
                    if isinstance(item, dict)
                ),
                "duration_ms": sum(
                    float(item.get("duration", 0))
                    for item in lineup
                    if isinstance(item, dict)
                ),
            }
        return data  # type: ignore[no-any-return]

    async def persist_time_schedule(
        self, channel_id: str, schedule: dict[str, Any], generated: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": "time",
            "programs": _generated_program_ids(generated),
            "schedule": _persistent_schedule_payload(schedule),
        }
        if "seed" in generated:
            body["seed"] = generated["seed"]
        if "discardCount" in generated:
            body["discardCount"] = generated["discardCount"]
        resp = await self._client.post(
            f"/api/channels/{channel_id}/programming",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data["_upload"] = {
                "mode": "time",
                "persistent_time_status": "accepted",
                "programming_status": resp.status_code,
                "final_status": resp.status_code,
                "fallback_used": False,
                "lineup_items": len(generated.get("lineup", []))
                if isinstance(generated.get("lineup"), list)
                else 0,
                "content_items": len(_generated_program_ids(generated)),
                "duration_ms": sum(
                    float(item.get("duration", 0))
                    for item in _manual_lineup(generated)
                ),
            }
        return data  # type: ignore[no-any-return]


def _schedule_payload(schedule: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(schedule)
    slots = [
        slot for slot in payload.get("slots", [])
        if not (isinstance(slot, dict) and slot.get("type") == "flex")
    ]
    if not slots:
        slots = [{"startTime": 0, "type": "flex"}]
    payload["slots"] = slots
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            continue
        if slot.get("type") != "flex":
            raw_id = str(slot.get("id") or f"{slot.get('type', 'slot')}:{index}")
            slot["id"] = _uuid_string(raw_id)
        if payload.get("period") == "day":
            slot["startTime"] = int(slot.get("startTime", 0)) % DAY_MS
        if slot.get("type") in {"movie", "show", "custom-show", "smart-collection"}:
            slot.setdefault("direction", "asc")
    slots.sort(key=lambda slot: int(slot.get("startTime", 0)) if isinstance(slot, dict) else 0)
    return payload


def _persistent_schedule_payload(schedule: dict[str, Any]) -> dict[str, Any]:
    payload = _schedule_payload(schedule)
    payload.setdefault("padStyle", "slot")
    payload.setdefault("randomDistribution", "none")
    payload.setdefault("lockWeights", False)
    if "periodMs" not in payload:
        payload["periodMs"] = 7 * DAY_MS if payload.get("period") == "week" else DAY_MS
    for slot in payload.get("slots", []):
        if not isinstance(slot, dict) or slot.get("type") == "flex":
            continue
        slot.setdefault("cooldownMs", 0)
        slot.setdefault("weight", 1)
        slot.setdefault("durationSpec", {"type": "dynamic", "programCount": 1})
    return payload


def _manual_lineup(generated: dict[str, Any]) -> list[dict[str, Any]]:
    lineup = generated.get("lineup")
    if isinstance(lineup, list):
        return [
            item
            for item in lineup
            if isinstance(item, dict) and _has_persistable_duration(item)
        ]
    return []


def _generated_program_ids(generated: dict[str, Any]) -> list[str]:
    programs = generated.get("programs")
    if isinstance(programs, dict):
        return [str(program_id) for program_id in programs if program_id]
    if isinstance(programs, list):
        return [str(program_id) for program_id in programs if isinstance(program_id, str)]
    return []


def _has_persistable_duration(item: dict[str, Any]) -> bool:
    duration = item.get("duration")
    return isinstance(duration, (int, float)) and duration > 0


def _is_tunarr_duration_bug(error: httpx.HTTPStatusError) -> bool:
    if error.response.status_code != 500:
        return False
    return "NOT NULL constraint failed: channel.duration" in error.response.text


def _is_tunarr_time_programming_rejection(error: httpx.HTTPStatusError) -> bool:
    if _is_tunarr_duration_bug(error):
        return True
    if error.response.status_code not in {400, 500}:
        return False
    text = error.response.text
    markers = (
        "FST_ERR_VALIDATION",
        "FST_ERR_FAILED_ERROR_SERIALIZATION",
        "Response doesn't match the schema",
        "body/programs",
        "body/schedule",
        "body/type",
    )
    return any(marker in text for marker in markers)


def _status_error_text(error: httpx.HTTPStatusError) -> str:
    return f"{error.response.status_code}: {error.response.text.strip()}"


def _uuid_string(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _duration_ms(value: Any) -> int:
    return max(0, int(round(value.total_seconds() * 1000)))


def _append_flex(lineup: list[dict[str, Any]], duration_ms: int) -> None:
    if duration_ms > 0:
        lineup.append({"type": "flex", "duration": duration_ms})
