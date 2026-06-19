from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient
from tunarr_autoscheduler.models.blocks import EpisodeBlock


async def test_resolve_jellyfin_program_ids_batches_and_omits_missing() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append(body["externalIds"])
        item_id = body["externalIds"][0].rsplit("|", 1)[-1]
        return httpx.Response(200, json={
            f"uuid-{item_id}": {
                "uuid": f"uuid-{item_id}",
                "identifiers": [{"type": "jellyfin", "id": item_id}],
            },
        })

    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        resolved = await client.resolve_jellyfin_program_ids(
            ["episode-1", "episode-2", "episode-3"],
            media_source_id="source-1",
            batch_size=1,
        )
    finally:
        await client.close()

    assert resolved == {
        "episode-1": "uuid-episode-1",
        "episode-2": "uuid-episode-2",
        "episode-3": "uuid-episode-3",
    }
    assert len(requests) == 3


async def test_upload_timeline_persists_exact_program_and_channel_timing() -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []
    stored_lineup: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/media-sources":
            return httpx.Response(200, json=[{"id": "source-1", "type": "jellyfin"}])
        if request.url.path == "/api/programming/batch/lookup":
            return httpx.Response(200, json={
                "program-uuid": {
                    "uuid": "program-uuid",
                    "identifiers": [{
                        "type": "jellyfin",
                        "sourceId": "source-1",
                        "id": "episode-1",
                    }],
                },
            })
        if request.method == "GET" and request.url.path == "/api/channels/ch1":
            return httpx.Response(200, json={
                "disableFillerOverlay": False,
                "duration": 1,
                "groupTitle": "",
                "guideMinimumDuration": 30000,
                "icon": {"path": "", "width": 0, "duration": 0, "position": "bottom-right"},
                "id": "ch1",
                "name": "Channel",
                "number": 1,
                "offline": {"mode": "pic"},
                "startTime": 0,
                "stealth": False,
                "streamMode": "hls",
                "transcodeConfigId": "transcode-1",
                "subtitlesEnabled": False,
            })
        if request.method == "POST" and request.url.path.endswith("/programming"):
            stored_lineup[:] = body["lineup"]
            return httpx.Response(200, json={"ok": True})
        if request.method == "PUT" and request.url.path == "/api/channels/ch1":
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and request.url.path.endswith("/programming"):
            return httpx.Response(200, json={"lineup": stored_lineup})
        return httpx.Response(404)

    start = datetime(2026, 6, 11, 12, tzinfo=UTC)
    timeline = Timeline()
    timeline.insert(EpisodeBlock(
        start_time=start,
        end_time=start + timedelta(minutes=30),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
    ))
    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        result = await client.upload_timeline("ch1", timeline)
    finally:
        await client.close()

    assert result["lineup"] == [{
        "type": "content",
        "duration": 1_800_000,
        "id": "program-uuid",
    }]
    programming = next(
        body for method, path, body in requests
        if method == "POST" and path.endswith("/programming")
    )
    assert programming["type"] == "manual"
    channel_update = next(
        body for method, path, body in requests
        if method == "PUT" and path == "/api/channels/ch1"
    )
    assert channel_update["startTime"] == int(start.timestamp() * 1000)
    assert channel_update["duration"] == 1_800_000
    assert result["_upload"] == {
        "mode": "manual",
        "persistent_time_status": "not_attempted",
        "programming_status": 200,
        "channel_update_status": 200,
        "verification_status": 200,
        "final_status": 200,
        "duration_ms": 1_800_000,
        "lineup_items": 1,
        "content_items": 1,
        "fallback_used": False,
    }


async def test_upload_schedule_validates_and_persists_time_schedule() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append((request.url.path, body))
        if request.url.path.endswith("/schedule-time-slots"):
            return httpx.Response(
                200,
                json={
                    "startTime": 0,
                    "seed": [42],
                    "discardCount": 2,
                    "lineup": [
                        {
                            "type": "content",
                            "persisted": True,
                            "duration": 1800000,
                            "id": "program-1",
                        },
                        {
                            "type": "filler",
                            "persisted": True,
                            "duration": 30000,
                            "id": "filler-1",
                        },
                        {
                            "type": "flex",
                            "duration": None,
                        },
                    ],
                    "programs": {
                        "program-1": {
                            "type": "content",
                            "persisted": True,
                            "duration": 1800000,
                            "id": "program-1",
                        },
                        "filler-1": {
                            "type": "content",
                            "persisted": True,
                            "duration": 30000,
                            "id": "filler-1",
                        },
                    },
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.upload_schedule(
            "ch1",
            {
                "schedule": {
                    "type": "time",
                    "flexPreference": "distribute",
                    "latenessMs": 0,
                    "maxDays": 1,
                    "padMs": 0,
                    "period": "day",
                    "slots": [{
                        "id": "local-slot-id",
                        "type": "show",
                        "showId": "show-1",
                        "order": "next",
                        "startTime": 26 * 60 * 60 * 1000,
                    }, {
                        "id": "gap-slot-id",
                        "type": "flex",
                        "startTime": 1800000,
                    }],
                    "timeZoneOffset": 0,
                },
            },
        )
    finally:
        await client.close()

    assert [path for path, _ in requests] == [
        "/api/channels/ch1/schedule-time-slots",
        "/api/channels/ch1/programming",
    ]
    generated_schedule = requests[0][1]["schedule"]
    assert len(generated_schedule["slots"]) == 1
    assert generated_schedule["slots"][0]["id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "local-slot-id"),
    )
    assert generated_schedule["slots"][0]["startTime"] == 2 * 60 * 60 * 1000
    assert generated_schedule["slots"][0]["direction"] == "asc"
    persisted = requests[1][1]
    assert persisted["type"] == "time"
    assert persisted["programs"] == ["program-1", "filler-1"]
    assert persisted["seed"] == [42]
    assert persisted["discardCount"] == 2
    assert persisted["schedule"]["padStyle"] == "slot"
    assert persisted["schedule"]["randomDistribution"] == "none"
    assert persisted["schedule"]["lockWeights"] is False
    assert persisted["schedule"]["periodMs"] == 24 * 60 * 60 * 1000
    assert persisted["schedule"]["slots"][0]["id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "local-slot-id"),
    )
    assert persisted["schedule"]["slots"][0]["cooldownMs"] == 0
    assert persisted["schedule"]["slots"][0]["weight"] == 1
    assert persisted["schedule"]["slots"][0]["durationSpec"] == {
        "type": "dynamic",
        "programCount": 1,
    }


async def test_upload_schedule_falls_back_to_custom_manual_lineup_on_tunarr_duration_bug() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append((request.url.path, body))
        if request.url.path.endswith("/schedule-time-slots"):
            return httpx.Response(
                200,
                json={
                    "startTime": 0,
                    "seed": [42],
                    "discardCount": 0,
                    "lineup": [
                        {
                            "type": "custom",
                            "duration": 1800000,
                            "id": "custom-program",
                            "customShowId": "standby-list",
                            "index": 0,
                        },
                    ],
                    "programs": {"custom-program": {"id": "custom-program"}},
                },
            )
        if body.get("type") == "time":
            return httpx.Response(
                500,
                json={
                    "message": (
                        "Failed to serialize an error. Original error: "
                        "NOT NULL constraint failed: channel.duration"
                    ),
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.upload_schedule(
            "ch1",
            {
                "schedule": {
                    "type": "time",
                    "flexPreference": "distribute",
                    "maxDays": 1,
                    "padMs": 0,
                    "period": "day",
                    "slots": [{
                        "id": "custom-slot-id",
                        "type": "custom-show",
                        "customShowId": "standby-list",
                        "order": "next",
                        "startTime": 0,
                    }],
                },
            },
        )
    finally:
        await client.close()

    assert [body["type"] for path, body in requests if path.endswith("/programming")] == [
        "time",
        "manual",
    ]
    fallback = requests[-1][1]
    assert fallback["lineup"] == [{
        "type": "custom",
        "duration": 1800000,
        "id": "custom-program",
        "customShowId": "standby-list",
        "index": 0,
    }]


async def test_upload_schedule_falls_back_on_tunarr_time_schema_validation() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append((request.url.path, body))
        if request.url.path.endswith("/schedule-time-slots"):
            return httpx.Response(
                200,
                json={
                    "startTime": 0,
                    "lineup": [
                        {
                            "type": "content",
                            "duration": 1800000,
                            "id": "program-1",
                        },
                    ],
                    "programs": {"program-1": {"id": "program-1"}},
                },
            )
        if body.get("type") == "time":
            return httpx.Response(
                400,
                json={
                    "statusCode": 400,
                    "code": "FST_ERR_VALIDATION",
                    "message": "body/programs/0 Invalid input",
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        result = await client.upload_schedule(
            "ch1",
            {
                "schedule": {
                    "type": "time",
                    "flexPreference": "distribute",
                    "maxDays": 1,
                    "padMs": 0,
                    "period": "day",
                    "slots": [{
                        "type": "show",
                        "showId": "show-1",
                        "order": "next",
                        "startTime": 0,
                    }],
                },
            },
        )
    finally:
        await client.close()

    assert [body["type"] for path, body in requests if path.endswith("/programming")] == [
        "time",
        "manual",
    ]
    generated_schedule = requests[0][1]["schedule"]
    assert generated_schedule["slots"][0]["id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "show:0"),
    )
    assert result["_upload"]["persistent_time_status"] == "failed_fallback"
    assert result["_upload"]["fallback_used"] is True
    assert "FST_ERR_VALIDATION" in result["_upload"]["fallback_reason"]


async def test_persistent_time_fallback_refuses_empty_generated_lineup() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append((request.url.path, body))
        if body.get("type") == "time":
            return httpx.Response(
                400,
                json={
                    "statusCode": 400,
                    "code": "FST_ERR_VALIDATION",
                    "message": "body/programs Invalid input",
                },
            )
        return httpx.Response(200, json={"ok": True})

    client = TunarrClient("http://tunarr.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://tunarr.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        try:
            await client.persist_time_schedule_with_fallback(
                "ch1",
                {
                    "type": "time",
                    "period": "day",
                    "slots": [{
                        "id": "slot-1",
                        "type": "show",
                        "showId": "show-1",
                        "startTime": 0,
                    }],
                },
                {"lineup": [], "programs": {}},
            )
        except RuntimeError as e:
            assert "empty manual lineup" in str(e)
        else:
            raise AssertionError("Expected empty generated lineup to fail")
    finally:
        await client.close()

    assert [body["type"] for path, body in requests if path.endswith("/programming")] == [
        "time",
    ]
