from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.integrations.metadata.cache import ExternalMetadataCacheRepository
from tunarr_autoscheduler.models.schedule import MediaCacheEntry


async def build_external_signals(
    entries: list[MediaCacheEntry],
    cache: ExternalMetadataCacheRepository,
) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    await _add_provider_metadata(entries, cache, signals)
    await _add_jellystat_metadata(cache, signals)
    _add_library_signals(entries, signals)
    _add_genre_trend_signals(entries, signals)
    return signals


async def _add_provider_metadata(
    entries: list[MediaCacheEntry],
    cache: ExternalMetadataCacheRepository,
    signals: dict[str, dict[str, Any]],
) -> None:
    provider_cache = {
        provider: {
            (str(row["media_type"]), str(row["provider_id"])): row["payload"]
            for row in await cache.list_fresh(provider)
        }
        for provider in ["tmdb", "omdb", "tvdb"]
    }
    for entry in entries:
        candidate_id = _candidate_id(entry)
        if not candidate_id:
            continue
        provider_ids = (entry.metadata or {}).get("provider_ids")
        if not isinstance(provider_ids, dict):
            continue
        for provider, provider_id in _provider_ids(provider_ids).items():
            payload = provider_cache.get(provider, {}).get(
                (_candidate_type(entry), provider_id),
            )
            if not payload:
                continue
            signal = signals.setdefault(candidate_id, {})
            source = signal.setdefault(provider, {})
            if isinstance(source, dict):
                source.update(payload)


async def _add_jellystat_metadata(
    cache: ExternalMetadataCacheRepository,
    signals: dict[str, dict[str, Any]],
) -> None:
    for row in await cache.list_fresh("jellystat"):
        candidate_id = str(row["provider_id"])
        payload = row["payload"]
        signal = signals.setdefault(candidate_id, {})
        signal["jellystat"] = payload


def _add_library_signals(
    entries: list[MediaCacheEntry],
    signals: dict[str, dict[str, Any]],
) -> None:
    for entry in entries:
        candidate_id = _candidate_id(entry)
        if not candidate_id:
            continue
        signal = signals.setdefault(candidate_id, {})
        library = signal.setdefault("library", {})
        if not isinstance(library, dict):
            continue
        if "jellystat" not in signal:
            library["underused"] = True
        added_at = _date_added(entry.metadata or {})
        if added_at is not None:
            age_days = (datetime.now(UTC) - added_at).days
            library["age_days"] = age_days
            if age_days >= 365:
                library["stale"] = True


def _add_genre_trend_signals(
    entries: list[MediaCacheEntry],
    signals: dict[str, dict[str, Any]],
) -> None:
    genres_by_candidate: dict[str, set[str]] = {}
    for entry in entries:
        candidate_id = _candidate_id(entry)
        if not candidate_id:
            continue
        genres = genres_by_candidate.setdefault(candidate_id, set())
        for genre in _string_list((entry.metadata or {}).get("genres")):
            genres.add(genre.lower())
    active_genres: set[str] = set()
    for candidate_id, signal in signals.items():
        if "jellystat" in signal:
            active_genres.update(genres_by_candidate.get(candidate_id, set()))
    if not active_genres:
        return
    for candidate_id, genres in genres_by_candidate.items():
        matched = sorted(genres & active_genres)
        if not matched:
            continue
        signal = signals.setdefault(candidate_id, {})
        library = signal.setdefault("library", {})
        if isinstance(library, dict):
            library["genre_trend_terms"] = matched[:5]


def _candidate_id(entry: MediaCacheEntry) -> str:
    if entry.item_type == "movie":
        return entry.id
    if entry.item_type == "episode":
        series_id = (entry.metadata or {}).get("series_id")
        return str(series_id or "")
    return ""


def _candidate_type(entry: MediaCacheEntry) -> str:
    return "movie" if entry.item_type == "movie" else "series"


def _provider_ids(raw: dict[object, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not value:
            continue
        provider = str(key).lower()
        if provider in {"tmdb", "omdb", "tvdb"}:
            normalized[provider] = str(value)
        elif provider == "imdb":
            normalized["omdb"] = str(value)
    return normalized


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _date_added(metadata: dict[str, Any]) -> datetime | None:
    for key in ["date_added", "DateCreated", "created_at", "added_at"]:
        value = metadata.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None
