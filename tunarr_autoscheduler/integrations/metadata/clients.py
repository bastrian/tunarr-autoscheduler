from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from tunarr_autoscheduler.integrations.metadata.rate_limit import AsyncRateLimiter


@dataclass
class RateLimitExceededError(Exception):
    provider: str
    attempts: int
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        retry_after = (
            f" retry_after={self.retry_after_seconds:.0f}s"
            if self.retry_after_seconds is not None
            else ""
        )
        return f"{self.provider} rate limit exceeded after {self.attempts} attempts{retry_after}"


class TmdbClient:
    def __init__(
        self,
        *,
        api_key: str,
        language: str,
        rate_limit_per_minute: int,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.language = language
        self._client = http_client
        self._limiter = AsyncRateLimiter(
            max_calls=rate_limit_per_minute,
            period_seconds=60.0,
        )

    async def fetch(self, media_type: str, provider_id: str) -> dict[str, Any]:
        if not self.api_key or not provider_id:
            return {}
        endpoint = "movie" if media_type == "movie" else "tv"
        await self._limiter.acquire()
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await _get_with_backoff(
                client,
                provider="tmdb",
                url=f"https://api.themoviedb.org/3/{endpoint}/{provider_id}",
                params={"api_key": self.api_key, "language": self.language},
            )
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return _tmdb_payload(response.json())
        except RateLimitExceededError:
            raise
        except httpx.HTTPError:
            return {}
        finally:
            if close_client:
                await client.aclose()


class OmdbClient:
    def __init__(
        self,
        *,
        api_key: str,
        rate_limit_per_minute: int,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self._client = http_client
        self._limiter = AsyncRateLimiter(
            max_calls=rate_limit_per_minute,
            period_seconds=60.0,
        )

    async def fetch(self, media_type: str, provider_id: str) -> dict[str, Any]:
        if not self.api_key or not provider_id:
            return {}
        await self._limiter.acquire()
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await _get_with_backoff(
                client,
                provider="omdb",
                url="https://www.omdbapi.com/",
                params={"apikey": self.api_key, "i": provider_id, "plot": "full"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("Response") == "False":
                return {}
            return _omdb_payload(payload)
        except RateLimitExceededError:
            raise
        except httpx.HTTPError:
            return {}
        finally:
            if close_client:
                await client.aclose()


class TvdbClient:
    def __init__(self, *, api_key: str, rate_limit_per_minute: int):
        self.api_key = api_key
        self._limiter = AsyncRateLimiter(
            max_calls=rate_limit_per_minute,
            period_seconds=60.0,
        )

    async def fetch(self, media_type: str, provider_id: str) -> dict[str, Any]:
        await self._limiter.acquire()
        return {
            "provider": "tvdb",
            "provider_id": provider_id,
            "media_type": media_type,
            "unsupported": True,
            "note": "TVDB client is rate-limited but not active until auth flow is configured.",
        }


class JellystatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        rate_limit_per_minute: int,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._client = http_client
        self._limiter = AsyncRateLimiter(
            max_calls=rate_limit_per_minute,
            period_seconds=60.0,
        )

    async def fetch_stats(self, *, days: int) -> list[dict[str, Any]]:
        report = await self.fetch_stats_report(days=days)
        return [
            row
            for row in report.get("rows", [])
            if isinstance(row, dict)
        ]

    async def fetch_stats_report(self, *, days: int) -> dict[str, Any]:
        if not self.base_url or not self.api_token:
            return {"rows": [], "statuses": {}}
        results: list[dict[str, Any]] = []
        statuses: dict[str, dict[str, Any]] = {}
        for endpoint, signal_name in [
            ("getMostPopularByType", "popular"),
            ("getMostViewedByType", "viewed"),
        ]:
            for media_type in ["Movie", "Series", "Episode"]:
                rows, status = await self._post_stats(endpoint, days=days, media_type=media_type)
                statuses[f"{signal_name}:{media_type.lower()}"] = status
                for index, row in enumerate(rows):
                    item_id = _jellystat_item_id(row)
                    if not item_id:
                        continue
                    results.append({
                        "provider": "jellystat",
                        "provider_id": item_id,
                        "media_type": media_type.lower(),
                        "signal": signal_name,
                        "rank": index + 1,
                        "score": _jellystat_score(row, index),
                        "plays": _first_number(row, [
                            "play_count",
                            "PlayCount",
                            "total_plays",
                            "TotalPlays",
                            "count",
                            "Count",
                            "views",
                            "Views",
                        ]),
                        "completion_rate": _first_number(row, [
                            "completion_rate",
                            "completionRate",
                            "CompletionRate",
                            "percent_complete",
                            "PercentComplete",
                            "played_percentage",
                            "PlayedPercentage",
                        ]),
                        "last_played": _first_string(row, [
                            "last_played",
                            "lastPlayed",
                            "LastPlayed",
                            "date_played",
                            "DatePlayed",
                            "played_at",
                            "PlayedAt",
                        ]),
                    })
        return {"rows": results, "statuses": statuses}

    async def check_connection(self, *, days: int = 1) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "message": "Jellystat URL is required."}
        if not self.api_token:
            return {"ok": False, "message": "Jellystat API token is required."}
        await self._limiter.acquire()
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await _post_with_backoff(
                client,
                provider="jellystat",
                url=f"{self.base_url}/stats/getMostPopularByType",
                headers={"x-api-token": self.api_token},
                json_body={"days": max(1, days), "type": "Movie"},
                max_attempts=1,
            )
            if response.status_code in {401, 403}:
                return {
                    "ok": False,
                    "message": "Jellystat rejected the API token.",
                    "status_code": response.status_code,
                }
            if response.status_code == 404:
                return {
                    "ok": False,
                    "message": "Jellystat stats endpoint was not found.",
                    "status_code": response.status_code,
                }
            response.raise_for_status()
            payload = response.json()
            item_count = len(payload) if isinstance(payload, list) else 0
            if isinstance(payload, dict):
                for key in ["items", "data", "results", "rows"]:
                    value = payload.get(key)
                    if isinstance(value, list):
                        item_count = len(value)
                        break
            return {
                "ok": True,
                "message": f"Jellystat connection OK. Stats endpoint returned {item_count} rows.",
                "status_code": response.status_code,
                "rows": item_count,
            }
        except RateLimitExceededError as exc:
            return {"ok": False, "message": str(exc), "rate_limited": True}
        except ValueError:
            return {
                "ok": False,
                "message": "Jellystat responded, but the response was not valid JSON.",
            }
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "message": f"Jellystat returned HTTP {exc.response.status_code}.",
                "status_code": exc.response.status_code,
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"Jellystat connection failed: {exc}"}
        finally:
            if close_client:
                await client.aclose()

    async def _post_stats(
        self,
        endpoint: str,
        *,
        days: int,
        media_type: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        await self._limiter.acquire()
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await _post_with_backoff(
                client,
                provider="jellystat",
                url=f"{self.base_url}/stats/{endpoint}",
                headers={"x-api-token": self.api_token},
                json_body={"days": max(1, days), "type": media_type},
            )
            status = {"ok": response.status_code < 400, "status_code": response.status_code}
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                rows = [item for item in payload if isinstance(item, dict)]
                return rows, {**status, "rows": len(rows)}
            if isinstance(payload, dict):
                for key in ["items", "data", "results", "rows"]:
                    value = payload.get(key)
                    if isinstance(value, list):
                        rows = [item for item in value if isinstance(item, dict)]
                        return rows, {**status, "rows": len(rows)}
            return [], {**status, "rows": 0}
        except RateLimitExceededError:
            raise
        except httpx.HTTPStatusError as exc:
            return [], {
                "ok": False,
                "status_code": exc.response.status_code,
                "message": f"HTTP {exc.response.status_code}",
            }
        except ValueError:
            return [], {"ok": False, "message": "Invalid JSON"}
        except httpx.HTTPError as exc:
            return [], {"ok": False, "message": str(exc)}
        finally:
            if close_client:
                await client.aclose()


async def _get_with_backoff(
    client: httpx.AsyncClient,
    *,
    provider: str,
    url: str,
    params: dict[str, str],
    max_attempts: int = 6,
    initial_backoff_seconds: float = 5.0,
    max_backoff_seconds: float = 300.0,
) -> httpx.Response:
    backoff_seconds = initial_backoff_seconds
    retry_after_seconds: float | None = None
    for attempt in range(1, max_attempts + 1):
        response = await client.get(url, params=params)
        if response.status_code != 429:
            return response
        retry_after_seconds = _retry_after_seconds(response)
        if attempt == max_attempts:
            raise RateLimitExceededError(
                provider=provider,
                attempts=attempt,
                retry_after_seconds=retry_after_seconds,
            )
        wait_seconds = retry_after_seconds if retry_after_seconds is not None else backoff_seconds
        await asyncio.sleep(min(max_backoff_seconds, max(0.0, wait_seconds)))
        backoff_seconds = min(max_backoff_seconds, backoff_seconds * 2)
    raise RateLimitExceededError(
        provider=provider,
        attempts=max_attempts,
        retry_after_seconds=retry_after_seconds,
    )


async def _post_with_backoff(
    client: httpx.AsyncClient,
    *,
    provider: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, object],
    max_attempts: int = 6,
    initial_backoff_seconds: float = 5.0,
    max_backoff_seconds: float = 300.0,
) -> httpx.Response:
    backoff_seconds = initial_backoff_seconds
    retry_after_seconds: float | None = None
    for attempt in range(1, max_attempts + 1):
        response = await client.post(url, headers=headers, json=json_body)
        if response.status_code != 429:
            return response
        retry_after_seconds = _retry_after_seconds(response)
        if attempt == max_attempts:
            raise RateLimitExceededError(
                provider=provider,
                attempts=attempt,
                retry_after_seconds=retry_after_seconds,
            )
        wait_seconds = retry_after_seconds if retry_after_seconds is not None else backoff_seconds
        await asyncio.sleep(min(max_backoff_seconds, max(0.0, wait_seconds)))
        backoff_seconds = min(max_backoff_seconds, backoff_seconds * 2)
    raise RateLimitExceededError(
        provider=provider,
        attempts=max_attempts,
        retry_after_seconds=retry_after_seconds,
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _tmdb_payload(raw: dict[str, Any]) -> dict[str, Any]:
    poster_path = str(raw.get("poster_path") or "")
    backdrop_path = str(raw.get("backdrop_path") or "")
    release_date = str(raw.get("release_date") or raw.get("first_air_date") or "")
    return {
        "provider": "tmdb",
        "provider_id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or raw.get("name") or ""),
        "overview": str(raw.get("overview") or ""),
        "year": release_date[:4] if release_date else "",
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "",
        "backdrop_url": (
            f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else ""
        ),
        "rating": raw.get("vote_average"),
        "genres": [
            str(item.get("name"))
            for item in raw.get("genres", [])
            if isinstance(item, dict) and item.get("name")
        ],
    }


def _omdb_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "omdb",
        "provider_id": str(raw.get("imdbID") or ""),
        "title": str(raw.get("Title") or ""),
        "overview": str(raw.get("Plot") or ""),
        "year": str(raw.get("Year") or "")[:4],
        "poster_url": "" if raw.get("Poster") == "N/A" else str(raw.get("Poster") or ""),
        "rating": _float_or_none(raw.get("imdbRating")),
        "genres": [
            item.strip()
            for item in str(raw.get("Genre") or "").split(",")
            if item.strip()
        ],
    }


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _jellystat_item_id(row: dict[str, Any]) -> str:
    for key in [
        "item_id",
        "ItemId",
        "itemId",
        "id",
        "Id",
        "jf_id",
        "JellyfinId",
        "series_id",
        "SeriesId",
    ]:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _jellystat_score(row: dict[str, Any], index: int) -> float:
    explicit = _first_number(row, [
        "score",
        "Score",
        "popularity",
        "Popularity",
        "rating",
        "Rating",
    ])
    if explicit is not None:
        return explicit
    plays = _first_number(row, [
        "play_count",
        "PlayCount",
        "total_plays",
        "TotalPlays",
        "count",
        "Count",
        "views",
        "Views",
    ])
    if plays is not None:
        return plays
    return max(1.0, 100.0 - index)


def _first_number(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(str(value))
        except (TypeError, ValueError):
            continue
    return None


def _first_string(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""
