from __future__ import annotations

from typing import Any

from tunarr_autoscheduler.models.schedule import MediaCacheEntry

PROVIDERS = ("tmdb", "tvdb", "imdb")


def build_metadata_audit(entries: list[MediaCacheEntry]) -> dict[str, Any]:
    records = build_metadata_records(entries)
    by_type: dict[str, dict[str, Any]] = {}
    for media_type in ("movie", "series"):
        typed = [record for record in records if record["media_type"] == media_type]
        by_type[media_type] = _coverage(typed)
    return {
        "total": len(records),
        "by_type": by_type,
        "missing_examples": {
            provider: [
                {
                    "media_type": record["media_type"],
                    "id": record["id"],
                    "title": record["title"],
                }
                for record in records
                if not record["provider_ids"].get(provider)
            ][:20]
            for provider in PROVIDERS
        },
    }


def _coverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    providers = {}
    for provider in PROVIDERS:
        count = sum(1 for record in records if record["provider_ids"].get(provider))
        providers[provider] = {
            "count": count,
            "missing": total - count,
            "percent": round((count / total) * 100, 1) if total else 0.0,
        }
    return {"total": total, "providers": providers}


def build_metadata_records(entries: list[MediaCacheEntry]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    series: dict[str, dict[str, Any]] = {}
    for entry in entries:
        metadata = entry.metadata or {}
        if entry.item_type == "movie":
            records.append({
                "media_type": "movie",
                "id": entry.id,
                "title": entry.title,
                "provider_ids": _provider_ids(metadata.get("provider_ids")),
            })
            continue
        if entry.item_type != "episode" or not metadata.get("series_id"):
            continue
        series_id = str(metadata["series_id"])
        record = series.setdefault(series_id, {
            "media_type": "series",
            "id": series_id,
            "title": str(metadata.get("series_name") or entry.title),
            "provider_ids": {},
        })
        for provider, value in _provider_ids(metadata.get("provider_ids")).items():
            if value and not record["provider_ids"].get(provider):
                record["provider_ids"][provider] = value
    records.extend(series.values())
    return sorted(records, key=lambda item: (item["media_type"], item["title"].lower()))


def _provider_ids(raw: object) -> dict[str, str]:
    result = {provider: "" for provider in PROVIDERS}
    if not isinstance(raw, dict):
        return result
    aliases = {
        "tmdb": "tmdb",
        "themoviedb": "tmdb",
        "tvdb": "tvdb",
        "thetvdb": "tvdb",
        "imdb": "imdb",
    }
    for key, value in raw.items():
        provider = aliases.get(str(key).strip().lower())
        if provider and value:
            result[provider] = str(value)
    return result
