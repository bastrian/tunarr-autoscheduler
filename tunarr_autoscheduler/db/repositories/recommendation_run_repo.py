from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.db.database import Database


class RecommendationRunRepository:
    def __init__(self, db: Database):
        self._db = db

    async def create(
        self,
        *,
        run_type: str,
        title: str,
        request: dict[str, Any],
        result: dict[str, Any],
        status: str = "draft",
    ) -> dict[str, Any]:
        now = datetime.now(tz=UTC).isoformat()
        run_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO recommendation_runs "
            "(id, run_type, title, status, request_json, result_json, created_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            run_id,
            run_type,
            title,
            status,
            json.dumps(request, sort_keys=True),
            json.dumps(result, sort_keys=True),
            now,
            "",
        )
        run = await self.get(run_id)
        if run is None:
            raise RuntimeError("Recommendation run was not saved")
        return run

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM recommendation_runs ORDER BY created_at DESC LIMIT ?",
            limit,
        )
        return [_row_to_run(row) for row in rows]

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM recommendation_runs WHERE id = ?",
            run_id,
        )
        return _row_to_run(row) if row else None

    async def mark_applied(self, run_id: str) -> None:
        await self._db.execute(
            "UPDATE recommendation_runs SET status = ?, applied_at = ? WHERE id = ?",
            "applied",
            datetime.now(tz=UTC).isoformat(),
            run_id,
        )


def _row_to_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "run_type": str(row["run_type"]),
        "title": str(row["title"]),
        "status": str(row["status"]),
        "request": _json_dict(row.get("request_json")),
        "result": _json_dict(row.get("result_json")),
        "created_at": str(row["created_at"]),
        "applied_at": str(row.get("applied_at") or ""),
    }


def _json_dict(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
