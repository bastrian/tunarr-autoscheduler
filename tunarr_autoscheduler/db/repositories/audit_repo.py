from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.db.database import Database

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "auth_new_password",
    "auth_confirm_password",
    "bot_token",
    "current_password",
    "jellyfin_api_key",
    "metadata_jellystat_api_token",
    "metadata_omdb_api_key",
    "metadata_tmdb_api_key",
    "metadata_tvdb_api_key",
    "password",
    "secret",
    "session_secret",
    "smtp_password",
    "token",
}


class AuditLogRepository:
    def __init__(self, db: Database):
        self._db = db

    async def record(
        self,
        action: str,
        *,
        actor: str = "admin",
        source: str = "web",
        status: str = "success",
        channel_id: str = "",
        schedule_version: int | None = None,
        target_type: str = "",
        target_id: str = "",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO audit_log (
                id, action, actor, source, status, channel_id, schedule_version,
                target_type, target_id, message, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            str(uuid.uuid4()),
            action,
            actor,
            source,
            status,
            channel_id,
            schedule_version,
            target_type,
            target_id,
            message,
            json.dumps(_redact(details or {}), sort_keys=True),
            datetime.now(tz=UTC).isoformat(),
        )

    async def list_events(
        self,
        *,
        channel_id: str = "",
        action: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetch_all(
            f"""
            SELECT *
            FROM audit_log
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            *params,
            max(1, min(limit, 500)),
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(str(item.get("details_json") or "{}"))
            except json.JSONDecodeError:
                item["details"] = {}
            events.append(item)
        return events


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith("_token")
