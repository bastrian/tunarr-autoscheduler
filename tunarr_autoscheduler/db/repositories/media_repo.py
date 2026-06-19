from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.models.schedule import MediaCacheEntry


class MediaRepository:
    def __init__(self, db: Database):
        self._db = db

    async def get(self, item_id: str) -> MediaCacheEntry | None:
        row = await self._db.fetch_one(
            "SELECT * FROM media_cache WHERE id = ?", item_id,
        )
        if row is None:
            return None
        return self._row_to_entry(row)

    async def save(self, entry: MediaCacheEntry) -> None:
        import json
        entry.item_type = normalize_media_type(entry.item_type)
        await self._db.execute(
            "INSERT OR REPLACE INTO media_cache "
            "(id, item_type, source_type, source_id, title, duration_seconds, "
            "metadata_json, last_synced_at, available) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            entry.id, entry.item_type, entry.source_type,
            entry.source_id, entry.title, entry.duration_seconds,
            json.dumps(entry.metadata) if entry.metadata else None,
            datetime.now(tz=UTC).isoformat(),
            1 if entry.available else 0,
        )

    async def save_many(self, entries: list[MediaCacheEntry]) -> None:
        import json
        if not entries:
            return
        synced_at = datetime.now(tz=UTC).isoformat()
        await self._db.execute_many(
            "INSERT OR REPLACE INTO media_cache "
            "(id, item_type, source_type, source_id, title, duration_seconds, "
            "metadata_json, last_synced_at, available) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    entry.id,
                    normalize_media_type(entry.item_type),
                    entry.source_type,
                    entry.source_id,
                    entry.title,
                    entry.duration_seconds,
                    json.dumps(entry.metadata) if entry.metadata else None,
                    synced_at,
                    1 if entry.available else 0,
                )
                for entry in entries
            ],
        )

    async def get_known_ids(self, source_type: str) -> set[str]:
        rows = await self._db.fetch_all(
            "SELECT id FROM media_cache WHERE source_type = ?", source_type,
        )
        return {str(row["id"]) for row in rows}

    async def get_by_source(self, source_type: str, source_id: str) -> MediaCacheEntry | None:
        row = await self._db.fetch_one(
            "SELECT * FROM media_cache WHERE source_type = ? AND source_id = ?",
            source_type, source_id,
        )
        if row is None:
            return None
        return self._row_to_entry(row)

    async def get_all_available(self, item_type: str | None = None) -> list[MediaCacheEntry]:
        if item_type:
            rows = await self._db.fetch_all(
                "SELECT * FROM media_cache WHERE available = 1 AND lower(item_type) = ?",
                normalize_media_type(item_type),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM media_cache WHERE available = 1",
            )
        return [self._row_to_entry(r) for r in rows]

    async def get_playlist_options(self) -> list[dict[str, str]]:
        rows = await self._db.fetch_all(
            """
            SELECT
                'series' AS media_type,
                json_extract(metadata_json, '$.series_id') AS media_id,
                COALESCE(
                    MAX(json_extract(metadata_json, '$.series_name')),
                    json_extract(metadata_json, '$.series_id')
                ) AS title,
                CAST(COUNT(*) AS TEXT) || ' episodes' AS details
            FROM media_cache
            WHERE available = 1
              AND lower(item_type) = 'episode'
              AND json_extract(metadata_json, '$.series_id') IS NOT NULL
            GROUP BY json_extract(metadata_json, '$.series_id')
            UNION ALL
            SELECT
                'movie' AS media_type,
                id AS media_id,
                title,
                COALESCE(CAST(json_extract(metadata_json, '$.year') AS TEXT), '') AS details
            FROM media_cache
            WHERE available = 1 AND lower(item_type) = 'movie'
            ORDER BY media_type DESC, title COLLATE NOCASE, media_id
            """,
        )
        return [
            {
                "key": f"{row['media_type']}:{row['media_id']}",
                "media_type": str(row["media_type"]),
                "media_id": str(row["media_id"]),
                "title": str(row["title"]),
                "details": str(row.get("details") or ""),
            }
            for row in rows
        ]

    async def mark_unavailable(self, item_id: str) -> None:
        await self._db.execute(
            "UPDATE media_cache SET available = 0, last_synced_at = ? WHERE id = ?",
            datetime.now(tz=UTC).isoformat(), item_id,
        )

    async def get_unavailable(self) -> list[MediaCacheEntry]:
        rows = await self._db.fetch_all(
            "SELECT * FROM media_cache WHERE available = 0",
        )
        return [self._row_to_entry(r) for r in rows]

    def _row_to_entry(self, row: dict[str, Any]) -> MediaCacheEntry:
        import json
        metadata = None
        if row.get("metadata_json"):
            try:
                metadata = json.loads(row["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        return MediaCacheEntry(
            id=row["id"],
            item_type=normalize_media_type(row["item_type"]),
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=row["title"],
            duration_seconds=row.get("duration_seconds"),
            metadata=metadata,
            available=bool(row.get("available", 1)),
        )


def normalize_media_type(item_type: str) -> str:
    return item_type.strip().lower()


class ChannelRepository:
    def __init__(self, db: Database):
        self._db = db


class ScheduleRepository:
    def __init__(self, db: Database):
        self._db = db


class JobRepository:
    def __init__(self, db: Database):
        self._db = db

    async def save(self, job: Any) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO generation_jobs "
            "(id, channel_id, status, current_stage, error_message, "
            "checkpoint_id, schedule_version_id, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            job.id, job.channel_id, job.status.value,
            job.current_stage, job.error_message,
            job.checkpoint_id, job.schedule_version_id, job.started_at.isoformat(),
            job.completed_at.isoformat() if job.completed_at else None,
        )

    async def get_recent(self, channel_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM generation_jobs WHERE channel_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            channel_id, limit,
        )
