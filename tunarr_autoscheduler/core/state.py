from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.repositories.media_repo import normalize_media_type
from tunarr_autoscheduler.models.blocks import EpisodeBlock, MovieBlock
from tunarr_autoscheduler.models.schedule import (
    AirHistoryEntry,
    MediaCacheEntry,
    RotationState,
)


class StateManager:
    def __init__(self, db: Database):
        self._db = db

    async def mark_episode_used(
        self, channel_id: str, episode_id: str, duration_seconds: int,
        show_id: str | None = None, schedule_version: int | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO air_history "
            "(id, channel_id, item_id, item_type, aired_at, "
            "duration_seconds, show_id, schedule_version) "
            "VALUES (?, ?, ?, 'episode', ?, ?, ?, ?)",
            f"{channel_id}_{episode_id}_{datetime.now(tz=UTC).timestamp()}",
            channel_id, episode_id, datetime.now(tz=UTC).isoformat(),
            duration_seconds, show_id, schedule_version,
        )

    async def get_rotation_state(self, channel_id: str, rotation_name: str) -> RotationState | None:
        row = await self._db.fetch_one(
            "SELECT * FROM rotation_state WHERE channel_id = ? AND rotation_name = ?",
            channel_id, rotation_name,
        )
        if row is None:
            return None
        return RotationState(**row)

    async def save_rotation_state(self, state: RotationState) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO rotation_state "
            "(id, channel_id, rotation_name, current_index, "
            "current_show_id, episode_counter, last_rotation_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            f"{state.channel_id}_{state.rotation_name}",
            state.channel_id, state.rotation_name,
            state.current_index, state.current_show_id,
            state.episode_counter,
            state.last_rotation_time.isoformat() if state.last_rotation_time else None,
        )

    async def get_cooldown_remaining(self, item_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT cooldown_until FROM cooldowns WHERE item_id = ?",
            item_id,
        )
        if row is None:
            return 0
        until = datetime.fromisoformat(row["cooldown_until"])
        remaining = (until - datetime.now(tz=UTC)).total_seconds()
        return max(0, int(remaining))

    async def set_cooldown(
        self, item_id: str, item_type: str, channel_id: str, duration_minutes: int,
    ) -> None:
        until = datetime.now(tz=UTC)
        until = until.replace(second=0, microsecond=0)
        until += timedelta(minutes=duration_minutes)
        await self._db.execute(
            "INSERT OR REPLACE INTO cooldowns (item_id, item_type, cooldown_until, channel_id) "
            "VALUES (?, ?, ?, ?)",
            item_id, item_type, until.isoformat(), channel_id,
        )

    async def get_media_cache(self, item_id: str) -> MediaCacheEntry | None:
        row = await self._db.fetch_one(
            "SELECT * FROM media_cache WHERE id = ?", item_id,
        )
        if row is None:
            return None
        return MediaCacheEntry(**row)

    async def save_media_cache(self, entry: MediaCacheEntry) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO media_cache "
            "(id, item_type, source_type, source_id, title, "
            "duration_seconds, metadata_json, available, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            entry.id, normalize_media_type(entry.item_type), entry.source_type,
            entry.source_id, entry.title, entry.duration_seconds,
            json_dumps(entry.metadata) if entry.metadata else None,
            1 if entry.available else 0,
            datetime.now(tz=UTC).isoformat(),
        )

    async def get_air_history(
        self, channel_id: str, item_id: str, since: datetime,
    ) -> list[AirHistoryEntry]:
        rows = await self._db.fetch_all(
            "SELECT * FROM air_history WHERE channel_id = ? AND item_id = ? AND aired_at >= ?",
            channel_id, item_id, since.isoformat(),
        )
        return [AirHistoryEntry(**r) for r in rows]

    async def save_job(self, job: Any) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO generation_jobs "
            "(id, channel_id, status, current_stage, "
            "error_message, checkpoint_id, schedule_version_id, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            job.id, job.channel_id, job.status.value,
            job.current_stage, job.error_message,
            job.checkpoint_id, job.schedule_version_id, job.started_at.isoformat(),
            job.completed_at.isoformat() if job.completed_at else None,
        )

    # --- Schedule versioning ---

    async def save_schedule_version(
        self, channel_id: str, version: int, timeline_json: str,
        status: str = "draft", parent_version: int | None = None,
    ) -> str | None:
        import uuid
        vid = str(uuid.uuid4())
        try:
            await self._db.execute(
                "INSERT INTO schedule_versions "
                "(id, channel_id, version, status, timeline_json, created_at, parent_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                vid, channel_id, version, status, timeline_json,
                datetime.now(tz=UTC).isoformat(), parent_version,
            )
            return vid
        except Exception:
            return None

    async def get_latest_version(self, channel_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) as v FROM schedule_versions WHERE channel_id = ?",
            channel_id,
        )
        return row["v"] if row else 0

    async def get_schedule_version(self, channel_id: str, version: int) -> str | None:
        row = await self._db.fetch_one(
            "SELECT timeline_json FROM schedule_versions WHERE channel_id = ? AND version = ?",
            channel_id, version,
        )
        return row["timeline_json"] if row else None

    async def get_schedule_version_meta(
        self, channel_id: str, version: int,
    ) -> dict[str, object] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM schedule_versions WHERE channel_id = ? AND version = ?",
            channel_id, version,
        )
        return dict(row) if row else None

    async def list_versions(self, channel_id: str) -> list[dict[str, object]]:
        rows = await self._db.fetch_all(
            "SELECT id, version, status, timeline_json, created_at, parent_version "
            "FROM schedule_versions WHERE channel_id = ? "
            "ORDER BY version DESC LIMIT 50",
            channel_id,
        )
        versions: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item.update(_timeline_period(str(item.pop("timeline_json", ""))))
            versions.append(item)
        return versions

    async def get_follow_up_context(
        self, channel_id: str, parent_version: int | None = None,
    ) -> dict[str, Any] | None:
        rows = await self._db.fetch_all(
            "SELECT version, status, timeline_json, parent_version FROM schedule_versions "
            "WHERE channel_id = ? AND status IN ('draft', 'approved', 'uploaded') "
            "ORDER BY version DESC",
            channel_id,
        )
        if not rows:
            return None
        timelines: list[dict[str, Any]] = []
        for version_row in rows:
            try:
                timeline = Timeline.from_snapshot(json.loads(str(version_row["timeline_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not timeline.blocks:
                continue
            timelines.append({
                "version": int(version_row["version"]),
                "status": str(version_row["status"]),
                "parent_version": version_row["parent_version"],
                "timeline": timeline,
                "start_time": min(block.start_time for block in timeline.blocks),
                "end_time": max(block.end_time for block in timeline.blocks),
            })
        if not timelines:
            return None
        parent: dict[str, Any] | None
        if parent_version is None:
            parent = max(
                timelines,
                key=lambda item: (item["end_time"], item["version"]),
            )
        else:
            parent = None
            for item in timelines:
                if int(item["version"]) == parent_version:
                    parent = item
                    break
            if parent is None:
                return None
        parent_version = int(parent["version"])
        parent_timeline = cast(Timeline, parent["timeline"])
        parent_end = cast(datetime, parent["end_time"])
        chain = sorted(
            [
                item for item in timelines
                if cast(datetime, item["end_time"]) <= parent_end
            ],
            key=lambda item: (item["start_time"], item["end_time"], item["version"]),
        )
        gaps: list[dict[str, Any]] = []
        if chain:
            cursor = cast(datetime, chain[0]["end_time"])
            for item in chain[1:]:
                start_time = cast(datetime, item["start_time"])
                end_time = cast(datetime, item["end_time"])
                if start_time > cursor:
                    gaps.append({
                        "start": cursor,
                        "end": start_time,
                        "minutes": int((start_time - cursor).total_seconds() / 60),
                    })
                if end_time > cursor:
                    cursor = end_time
        episode_ids: set[str] = set()
        movie_ids: set[str] = set()
        for item in chain:
            version_timeline = cast(Timeline, item["timeline"])
            episode_ids.update(
                block.episode_id
                for block in version_timeline.blocks
                if isinstance(block, EpisodeBlock) and block.episode_id
            )
            movie_ids.update(
                block.movie_id
                for block in version_timeline.blocks
                if isinstance(block, MovieBlock) and block.movie_id
            )
        return {
            "version": parent_version,
            "status": parent["status"],
            "end_time": max(block.end_time for block in parent_timeline.blocks),
            "planned_start": chain[0]["start_time"],
            "planned_end": parent["end_time"],
            "chain_versions": [int(item["version"]) for item in chain],
            "gaps": gaps,
            "episode_ids": episode_ids,
            "movie_ids": movie_ids,
        }

    async def list_public_epg_versions(self) -> list[dict[str, object]]:
        rows = await self._db.fetch_all(
            """
            WITH ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel_id
                        ORDER BY
                            CASE status
                                WHEN 'uploaded' THEN 0
                                WHEN 'approved' THEN 1
                                WHEN 'draft' THEN 2
                                ELSE 3
                            END,
                            version DESC
                    ) AS rank
                FROM schedule_versions
                WHERE status IN ('uploaded', 'approved', 'draft')
            )
            SELECT channel_id, version, status, timeline_json, created_at
            FROM ranked
            WHERE rank = 1
            ORDER BY channel_id
            """,
        )
        return [dict(row) for row in rows]

    async def rollback_to_version(self, channel_id: str, target_version: int) -> int | None:
        timeline_json = await self.get_schedule_version(channel_id, target_version)
        if timeline_json is None:
            return None
        latest = await self.get_latest_version(channel_id)
        new_version = latest + 1
        version_id = await self.save_schedule_version(
            channel_id, new_version, timeline_json,
            status="draft", parent_version=target_version,
        )
        return new_version if version_id is not None else None

    async def approve_version(
        self, channel_id: str, version: int, approved_by: str = "system",
    ) -> None:
        await self._db.execute(
            "UPDATE schedule_versions SET status = 'approved' WHERE channel_id = ? AND version = ?",
            channel_id, version,
        )

    async def set_schedule_status(self, channel_id: str, version: int, status: str) -> None:
        await self._db.execute(
            "UPDATE schedule_versions SET status = ? WHERE channel_id = ? AND version = ?",
            status, channel_id, version,
        )

    async def delete_schedule_version(self, channel_id: str, version: int) -> bool:
        existing = await self.get_schedule_version_meta(channel_id, version)
        if existing is None:
            return False
        await self._db.execute(
            "DELETE FROM schedule_versions WHERE channel_id = ? AND version = ?",
            channel_id, version,
        )
        return True

    async def delete_schedule_versions(
        self,
        channel_id: str,
        versions: list[int],
        *,
        include_uploaded: bool = False,
    ) -> dict[str, int | list[int]]:
        unique_versions = sorted({version for version in versions if version > 0})
        deleted = 0
        skipped_uploaded: list[int] = []
        missing: list[int] = []
        for version in unique_versions:
            meta = await self.get_schedule_version_meta(channel_id, version)
            if meta is None:
                missing.append(version)
                continue
            if str(meta.get("status")) == "uploaded" and not include_uploaded:
                skipped_uploaded.append(version)
                continue
            await self._db.execute(
                "DELETE FROM schedule_versions WHERE channel_id = ? AND version = ?",
                channel_id, version,
            )
            deleted += 1
        return {
            "deleted": deleted,
            "skipped_uploaded": skipped_uploaded,
            "missing": missing,
        }

    async def cleanup_schedule_versions(
        self,
        channel_id: str,
        *,
        keep_latest: int,
        include_uploaded: bool = False,
        statuses: list[str] | None = None,
    ) -> dict[str, int | list[int]]:
        keep_latest = max(0, keep_latest)
        rows = await self._db.fetch_all(
            "SELECT version, status FROM schedule_versions WHERE channel_id = ? "
            "ORDER BY version DESC",
            channel_id,
        )
        protected_versions = {
            int(row["version"])
            for index, row in enumerate(rows)
            if index < keep_latest
        }
        allowed_statuses = set(statuses or [])
        candidates = [
            int(row["version"])
            for row in rows
            if int(row["version"]) not in protected_versions
            and (not allowed_statuses or str(row["status"]) in allowed_statuses)
        ]
        return await self.delete_schedule_versions(
            channel_id,
            candidates,
            include_uploaded=include_uploaded,
        )

    async def record_upload_attempt(
        self,
        channel_id: str,
        version: int,
        status: str,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO upload_attempts "
            "(id, channel_id, schedule_version, status, message, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            attempt_id,
            channel_id,
            version,
            status,
            message,
            json.dumps(details or {}, default=str),
            datetime.now(tz=UTC).isoformat(),
        )
        return attempt_id

    async def list_upload_attempts(
        self,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if channel_id:
            rows = await self._db.fetch_all(
                "SELECT * FROM upload_attempts WHERE channel_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                channel_id,
                limit,
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM upload_attempts ORDER BY created_at DESC LIMIT ?",
                limit,
            )
        attempts: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                details = json.loads(str(item.get("details_json") or "{}"))
            except json.JSONDecodeError:
                details = {}
            item["details"] = details
            attempts.append(item)
        return attempts

    async def list_recent_jobs(self, channel_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT generation_jobs.*, schedule_versions.version AS schedule_version "
            "FROM generation_jobs "
            "LEFT JOIN schedule_versions "
            "ON schedule_versions.id = generation_jobs.schedule_version_id "
            "WHERE generation_jobs.channel_id = ? "
            "ORDER BY generation_jobs.started_at DESC LIMIT ?",
            channel_id, limit,
        )
        return [dict(r) for r in rows]


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _timeline_period(timeline_json: str) -> dict[str, str | None]:
    try:
        timeline = Timeline.from_snapshot(json.loads(timeline_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"planned_start": None, "planned_end": None}
    if not timeline.blocks:
        return {"planned_start": None, "planned_end": None}
    return {
        "planned_start": min(block.start_time for block in timeline.blocks).isoformat(),
        "planned_end": max(block.end_time for block in timeline.blocks).isoformat(),
    }
