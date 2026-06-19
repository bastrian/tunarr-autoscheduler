from __future__ import annotations

import httpx
import pytest

from tunarr_autoscheduler.integrations.metadata import clients
from tunarr_autoscheduler.integrations.metadata.clients import (
    JellystatClient,
    RateLimitExceededError,
    _get_with_backoff,
)


async def test_get_with_backoff_retries_429_then_returns(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(clients.asyncio, "sleep", fake_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _get_with_backoff(
            client,
            provider="tmdb",
            url="https://example.test/metadata",
            params={},
        )

    assert response.status_code == 200
    assert sleeps == [5.0, 10.0]


async def test_get_with_backoff_uses_retry_after_header(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(clients.asyncio, "sleep", fake_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _get_with_backoff(
            client,
            provider="omdb",
            url="https://example.test/metadata",
            params={},
        )

    assert sleeps == [7.0]


async def test_get_with_backoff_raises_after_attempt_ceiling(monkeypatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    monkeypatch.setattr(clients.asyncio, "sleep", fake_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RateLimitExceededError) as exc:
            await _get_with_backoff(
                client,
                provider="tmdb",
                url="https://example.test/metadata",
                params={},
                max_attempts=3,
            )

    assert exc.value.provider == "tmdb"
    assert exc.value.attempts == 3


async def test_jellystat_report_keeps_endpoint_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b'"type":"Episode"' in request.content or b'"type": "Episode"' in request.content:
            return httpx.Response(503, json={"error": "temporarily unavailable"}, request=request)
        return httpx.Response(
            200,
            json=[{
                "item_id": "item-1",
                "score": 95,
                "completion_rate": 0.8,
                "last_played": "2026-06-15T12:00:00Z",
            }],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await JellystatClient(
            base_url="http://jellystat.test",
            api_token="token",
            rate_limit_per_minute=100,
            http_client=client,
        ).fetch_stats_report(days=7)

    statuses = report["statuses"]
    assert statuses["popular:movie"]["ok"] is True
    assert statuses["popular:episode"]["status_code"] == 503
    assert len(report["rows"]) == 4
    assert report["rows"][0]["completion_rate"] == 0.8
