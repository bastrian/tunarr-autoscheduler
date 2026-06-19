from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tunarr_autoscheduler.integrations.metadata.audit import build_metadata_records
from tunarr_autoscheduler.integrations.metadata.cache import ExternalMetadataCacheRepository
from tunarr_autoscheduler.integrations.metadata.clients import (
    JellystatClient,
    OmdbClient,
    RateLimitExceededError,
    TmdbClient,
    TvdbClient,
)
from tunarr_autoscheduler.models.config import MetadataConfig
from tunarr_autoscheduler.models.schedule import MediaCacheEntry


@dataclass
class MetadataRefreshSummary:
    candidates: int = 0
    cached: int = 0
    missing: int = 0
    expired: int = 0
    fetched: int = 0
    skipped: int = 0
    rate_limited: int = 0
    rate_limited_providers: list[str] | None = None
    provider_statuses: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "cached": self.cached,
            "missing": self.missing,
            "expired": self.expired,
            "fetched": self.fetched,
            "skipped": self.skipped,
            "rate_limited": self.rate_limited,
            "rate_limited_providers": self.rate_limited_providers or [],
            "provider_statuses": self.provider_statuses or {},
        }

    def mark_rate_limited(self, provider: str) -> None:
        self.rate_limited += 1
        providers = self.rate_limited_providers or []
        if provider not in providers:
            providers.append(provider)
        self.rate_limited_providers = providers

    def set_provider_statuses(self, provider: str, statuses: dict[str, Any]) -> None:
        provider_statuses = self.provider_statuses or {}
        provider_statuses[provider] = statuses
        self.provider_statuses = provider_statuses


class MetadataEnrichmentService:
    def __init__(
        self,
        *,
        cache: ExternalMetadataCacheRepository,
        config: MetadataConfig,
    ):
        self._cache = cache
        self._config = config

    async def refresh(
        self,
        entries: list[MediaCacheEntry],
        *,
        dry_run: bool = True,
        limit: int | None = None,
    ) -> MetadataRefreshSummary:
        summary = MetadataRefreshSummary()
        blocked_providers: set[str] = set()
        for request in self._requests(entries):
            if limit is not None and summary.candidates >= limit:
                break
            summary.candidates += 1
            if request["provider"] in blocked_providers:
                summary.skipped += 1
                continue
            status = await self._cache.status(
                request["provider"],
                request["media_type"],
                request["provider_id"],
            )
            if status == "fresh":
                summary.cached += 1
                continue
            if status == "missing":
                summary.missing += 1
            else:
                summary.expired += 1
            if dry_run:
                summary.skipped += 1
                continue
            try:
                payload = await self._fetch(request)
            except RateLimitExceededError as e:
                summary.skipped += 1
                summary.mark_rate_limited(e.provider)
                blocked_providers.add(e.provider)
                _write_rate_limit_alert(e)
                continue
            if payload:
                await self._cache.set(
                    request["provider"],
                    request["media_type"],
                    request["provider_id"],
                    payload,
                    ttl_days=self._config.cache_ttl_days,
                )
                summary.fetched += 1
            else:
                summary.skipped += 1
        await self._refresh_jellystat(entries, dry_run=dry_run, summary=summary)
        return summary

    async def get_cached(
        self,
        provider: str,
        media_type: str,
        provider_id: str,
    ) -> dict[str, Any] | None:
        return await self._cache.get(provider, media_type, provider_id)

    def _requests(self, entries: list[MediaCacheEntry]) -> list[dict[str, str]]:
        requests: list[dict[str, str]] = []
        for record in build_metadata_records(entries):
            media_type = str(record["media_type"])
            provider_ids = record["provider_ids"]
            if self._config.tmdb_enabled and provider_ids.get("tmdb"):
                requests.append({
                    "provider": "tmdb",
                    "media_type": media_type,
                    "provider_id": str(provider_ids["tmdb"]),
                })
            if self._config.omdb_enabled and provider_ids.get("imdb"):
                requests.append({
                    "provider": "omdb",
                    "media_type": media_type,
                    "provider_id": str(provider_ids["imdb"]),
                })
            if self._config.tvdb_enabled and provider_ids.get("tvdb"):
                requests.append({
                    "provider": "tvdb",
                    "media_type": media_type,
                    "provider_id": str(provider_ids["tvdb"]),
                })
        return requests

    async def _fetch(self, request: dict[str, str]) -> dict[str, Any]:
        provider = request["provider"]
        if provider == "tmdb":
            return await TmdbClient(
                api_key=self._config.tmdb_api_key,
                language=self._config.tmdb_language,
                rate_limit_per_minute=self._config.tmdb_rate_limit_per_minute,
            ).fetch(request["media_type"], request["provider_id"])
        if provider == "omdb":
            return await OmdbClient(
                api_key=self._config.omdb_api_key,
                rate_limit_per_minute=self._config.omdb_rate_limit_per_minute,
            ).fetch(request["media_type"], request["provider_id"])
        if provider == "tvdb":
            return await TvdbClient(
                api_key=self._config.tvdb_api_key,
                rate_limit_per_minute=self._config.tvdb_rate_limit_per_minute,
            ).fetch(request["media_type"], request["provider_id"])
        return {}

    async def _refresh_jellystat(
        self,
        entries: list[MediaCacheEntry],
        *,
        dry_run: bool,
        summary: MetadataRefreshSummary,
    ) -> None:
        if not self._config.jellystat_enabled:
            return
        lookup = _jellystat_lookup(entries)
        if not lookup:
            return
        try:
            report = await JellystatClient(
                base_url=self._config.jellystat_url,
                api_token=self._config.jellystat_api_token,
                rate_limit_per_minute=self._config.jellystat_rate_limit_per_minute,
            ).fetch_stats_report(days=self._config.jellystat_days)
            rows = [
                row
                for row in report.get("rows", [])
                if isinstance(row, dict)
            ]
            statuses = report.get("statuses")
            if isinstance(statuses, dict):
                summary.set_provider_statuses("jellystat", statuses)
                _write_provider_statuses({"jellystat": statuses})
        except RateLimitExceededError as e:
            summary.mark_rate_limited(e.provider)
            _write_rate_limit_alert(e)
            return
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            match = lookup.get(str(row.get("provider_id", "")).lower())
            if match is None:
                continue
            key = (match["media_type"], match["id"])
            payload = grouped.setdefault(key, {
                "provider": "jellystat",
                "provider_id": match["id"],
                "signals": {},
            })
            signal = str(row.get("signal") or "viewed")
            signals = payload["signals"]
            if isinstance(signals, dict):
                signals[signal] = {
                    "score": row.get("score"),
                    "rank": row.get("rank"),
                    "plays": row.get("plays"),
                    "completion_rate": row.get("completion_rate"),
                    "last_played": row.get("last_played"),
                }
                _add_derived_jellystat_signals(signals)
        for (media_type, provider_id), payload in grouped.items():
            summary.candidates += 1
            status = await self._cache.status("jellystat", media_type, provider_id)
            if status == "fresh":
                summary.cached += 1
                continue
            if status == "missing":
                summary.missing += 1
            else:
                summary.expired += 1
            if dry_run:
                summary.skipped += 1
                continue
            await self._cache.set(
                "jellystat",
                media_type,
                provider_id,
                payload,
                ttl_days=self._config.cache_ttl_days,
            )
            summary.fetched += 1


def rate_limit_alert_path() -> Path:
    return Path("~/.tunarr/metadata_rate_limit_alert.json").expanduser()


def read_rate_limit_alert() -> dict[str, Any] | None:
    path = rate_limit_alert_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def provider_status_path() -> Path:
    return Path("~/.tunarr/metadata_provider_status.json").expanduser()


def read_provider_statuses() -> dict[str, Any] | None:
    path = provider_status_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_provider_statuses(statuses: dict[str, Any]) -> None:
    path = provider_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "providers": statuses,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_rate_limit_alert(error: RateLimitExceededError) -> None:
    path = rate_limit_alert_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider": error.provider,
        "attempts": error.attempts,
        "retry_after_seconds": error.retry_after_seconds,
        "created_at": datetime.now(UTC).isoformat(),
        "message": str(error),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _jellystat_lookup(entries: list[MediaCacheEntry]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for entry in entries:
        metadata = entry.metadata or {}
        if entry.item_type == "movie":
            target = {"media_type": "movie", "id": entry.id}
        elif entry.item_type == "episode" and metadata.get("series_id"):
            target = {"media_type": "series", "id": str(metadata["series_id"])}
        else:
            continue
        for value in _possible_jellystat_ids(entry):
            lookup.setdefault(value.lower(), target)
    return lookup


def _possible_jellystat_ids(entry: MediaCacheEntry) -> set[str]:
    values = {entry.id, entry.source_id}
    metadata = entry.metadata or {}
    if metadata.get("series_id"):
        values.add(str(metadata["series_id"]))
    provider_ids = metadata.get("provider_ids")
    if isinstance(provider_ids, dict):
        values.update(str(value) for value in provider_ids.values() if value)
    return {value for value in values if value}


def _add_derived_jellystat_signals(signals: dict[str, Any]) -> None:
    popular = signals.get("popular")
    viewed = signals.get("viewed")
    if isinstance(popular, dict) and isinstance(viewed, dict):
        popular_rank = _number_or_none(popular.get("rank"))
        viewed_rank = _number_or_none(viewed.get("rank"))
        if popular_rank is not None and viewed_rank is not None:
            improvement = popular_rank - viewed_rank
            if improvement > 0:
                signals["trend"] = {
                    "score": min(100, max(1, improvement * 10)),
                    "rank_delta": improvement,
                }
    completion_values: list[float] = []
    for signal in signals.values():
        if not isinstance(signal, dict):
            continue
        value = _number_or_none(signal.get("completion_rate"))
        if value is not None:
            completion_values.append(value)
    if completion_values:
        signals["completion"] = {"score": max(completion_values)}


def _number_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
