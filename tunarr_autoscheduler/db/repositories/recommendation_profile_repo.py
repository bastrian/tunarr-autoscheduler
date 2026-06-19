from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.recommendations.profiles import RecommendationProfile


class RecommendationProfileRepository:
    def __init__(self, db: Database):
        self._db = db

    async def list_all(self) -> list[RecommendationProfile]:
        rows = await self._db.fetch_all(
            "SELECT * FROM recommendation_profiles ORDER BY lower(name), id",
        )
        return [_row_to_profile(row) for row in rows]

    async def get(self, profile_id: str) -> RecommendationProfile | None:
        row = await self._db.fetch_one(
            "SELECT * FROM recommendation_profiles WHERE id = ?",
            profile_id,
        )
        return _row_to_profile(row) if row else None

    async def save(self, profile: RecommendationProfile) -> RecommendationProfile:
        now = datetime.now(tz=UTC).isoformat()
        existing = await self.get(profile.id)
        payload = json.dumps(_profile_payload(profile), sort_keys=True)
        if existing is None:
            await self._db.execute(
                "INSERT INTO recommendation_profiles "
                "(id, name, description, profile_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                profile.id,
                profile.name,
                profile.description,
                payload,
                now,
                now,
            )
        else:
            await self._db.execute(
                "UPDATE recommendation_profiles "
                "SET name = ?, description = ?, profile_json = ?, updated_at = ? "
                "WHERE id = ?",
                profile.name,
                profile.description,
                payload,
                now,
                profile.id,
            )
        saved = await self.get(profile.id)
        if saved is None:
            raise RuntimeError("Recommendation profile was not saved")
        return saved

    async def delete(self, profile_id: str) -> bool:
        existing = await self.get(profile_id)
        if existing is None:
            return False
        await self._db.execute("DELETE FROM recommendation_profiles WHERE id = ?", profile_id)
        return True


def _row_to_profile(row: dict[str, Any]) -> RecommendationProfile:
    payload = json.loads(str(row["profile_json"]))
    return RecommendationProfile(
        id=str(row["id"]),
        name=str(row["name"]),
        media_types=tuple(_string_list(payload.get("media_types"))),
        preferred_genres=tuple(_string_list(payload.get("preferred_genres"))),
        preferred_tags=tuple(_string_list(payload.get("preferred_tags"))),
        required_terms=tuple(_string_list(payload.get("required_terms"))),
        excluded_genres=tuple(_string_list(payload.get("excluded_genres"))),
        min_runtime_minutes=_optional_int(payload.get("min_runtime_minutes")),
        max_runtime_minutes=_optional_int(payload.get("max_runtime_minutes")),
        min_items=max(1, _optional_int(payload.get("min_items")) or 1),
        language_rule=str(payload.get("language_rule") or "none"),
        description=str(row.get("description") or payload.get("description") or ""),
        weights={
            str(key): int(value)
            for key, value in dict(payload.get("weights") or {}).items()
        },
    )


def _profile_payload(profile: RecommendationProfile) -> dict[str, Any]:
    return {
        "media_types": list(profile.media_types),
        "preferred_genres": list(profile.preferred_genres),
        "preferred_tags": list(profile.preferred_tags),
        "required_terms": list(profile.required_terms),
        "excluded_genres": list(profile.excluded_genres),
        "min_runtime_minutes": profile.min_runtime_minutes,
        "max_runtime_minutes": profile.max_runtime_minutes,
        "min_items": profile.min_items,
        "language_rule": profile.language_rule,
        "description": profile.description,
        "weights": profile.weights,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
