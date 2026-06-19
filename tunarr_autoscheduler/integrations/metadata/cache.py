from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from tunarr_autoscheduler.db.database import Database


class ExternalMetadataCacheRepository:
    def __init__(self, db: Database):
        self._db = db

    async def get(
        self,
        provider: str,
        media_type: str,
        provider_id: str,
        *,
        include_expired: bool = False,
    ) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM external_metadata_cache "
            "WHERE provider = ? AND media_type = ? AND provider_id = ?",
            provider,
            media_type,
            provider_id,
        )
        if row is None:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if not include_expired and expires_at <= datetime.now(tz=UTC):
            return None
        payload = json.loads(str(row["payload_json"]))
        return payload if isinstance(payload, dict) else {}

    async def set(
        self,
        provider: str,
        media_type: str,
        provider_id: str,
        payload: dict[str, Any],
        *,
        ttl_days: int,
    ) -> None:
        now = datetime.now(tz=UTC)
        await self._db.execute(
            "INSERT OR REPLACE INTO external_metadata_cache "
            "(provider, media_type, provider_id, payload_json, fetched_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            provider,
            media_type,
            provider_id,
            json.dumps(payload, sort_keys=True),
            now.isoformat(),
            (now + timedelta(days=max(1, ttl_days))).isoformat(),
        )

    async def status(
        self,
        provider: str,
        media_type: str,
        provider_id: str,
    ) -> str:
        row = await self._db.fetch_one(
            "SELECT expires_at FROM external_metadata_cache "
            "WHERE provider = ? AND media_type = ? AND provider_id = ?",
            provider,
            media_type,
            provider_id,
        )
        if row is None:
            return "missing"
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        return "expired" if expires_at <= datetime.now(tz=UTC) else "fresh"

    async def stats(self) -> dict[str, Any]:
        rows = await self._db.fetch_all("SELECT provider, expires_at FROM external_metadata_cache")
        now = datetime.now(tz=UTC)
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            provider = str(row["provider"])
            provider_stats = stats.setdefault(provider, {"fresh": 0, "expired": 0})
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            provider_stats["expired" if expires_at <= now else "fresh"] += 1
        return stats

    async def list_fresh(self, provider: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT media_type, provider_id, payload_json, expires_at "
            "FROM external_metadata_cache WHERE provider = ?",
            provider,
        )
        now = datetime.now(tz=UTC)
        results: list[dict[str, Any]] = []
        for row in rows:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if expires_at <= now:
                continue
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                payload = {}
            results.append({
                "media_type": str(row["media_type"]),
                "provider_id": str(row["provider_id"]),
                "payload": payload,
            })
        return results
