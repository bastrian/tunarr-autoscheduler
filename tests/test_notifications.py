from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from tunarr_autoscheduler.core.schedule_monitor import check_schedule_expiry
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    list_notification_events,
)
from tunarr_autoscheduler.models.blocks import EpisodeBlock, JobStatus
from tunarr_autoscheduler.models.config import (
    AppConfig,
    AutomaticFollowUpConfig,
    ChannelConfig,
    NotificationRuleConfig,
    WebhookNotificationConfig,
)


async def test_notification_router_sends_webhook_and_throttles(tmp_path) -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            "url": str(request.url),
            "body": request.content.decode(),
        })
        return httpx.Response(200, json={"ok": True}, request=request)

    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        config = AppConfig()
        config.notifications.enabled = True
        config.notifications.webhook = WebhookNotificationConfig(
            enabled=True,
            url="https://example.test/webhook",
        )
        config.notifications.rules = [
            NotificationRuleConfig(
                event_type="upload_failed",
                providers=["webhook"],
                throttle_minutes=30,
            ),
        ]
        router = NotificationRouter(config=config, db=db)
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await router.send(NotificationMessage(
                event_type="upload_failed",
                title="Upload failed",
                message="Tunarr rejected it.",
                severity="danger",
                channel_id="channel-1",
            ))
            second = await router.send(NotificationMessage(
                event_type="upload_failed",
                title="Upload failed again",
                message="Still broken.",
                severity="danger",
                channel_id="channel-1",
            ))
        finally:
            await router.close()

        events = await list_notification_events(db)
    finally:
        await db.disconnect()

    assert len(requests) == 1
    assert first[0]["status"] == "sent"
    assert second[0]["status"] == "throttled"
    assert [event["status"] for event in events] == ["throttled", "sent"]


async def test_notification_router_records_provider_failure(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        config = AppConfig()
        config.notifications.enabled = True
        config.notifications.webhook = WebhookNotificationConfig(
            enabled=True,
            url="https://example.test/webhook",
        )
        config.notifications.rules = [
            NotificationRuleConfig(event_type="upload_failed", providers=["webhook"]),
        ]
        router = NotificationRouter(config=config, db=db)
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await router.send(NotificationMessage(
                event_type="upload_failed",
                title="Upload failed",
                message="Tunarr rejected it.",
            ))
        finally:
            await router.close()
    finally:
        await db.disconnect()

    assert result[0]["status"] == "failed"
    assert "error" in result[0]["details"]


async def test_list_notification_events_filters(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        await db.execute(
            "INSERT INTO notification_events "
            "(id, event_type, provider, status, title, message, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            "event-1",
            "upload_failed",
            "telegram",
            "sent",
            "Upload failed",
            "Nope",
            "{}",
            datetime.now(UTC).isoformat(),
        )
        await db.execute(
            "INSERT INTO notification_events "
            "(id, event_type, provider, status, title, message, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            "event-2",
            "backup_failed",
            "webhook",
            "failed",
            "Backup failed",
            "Nope",
            "{}",
            datetime.now(UTC).isoformat(),
        )

        events = await list_notification_events(
            db,
            event_type="upload_failed",
            provider="telegram",
            status="sent",
        )
    finally:
        await db.disconnect()

    assert len(events) == 1
    assert events[0]["id"] == "event-1"


async def test_check_schedule_expiry_sends_expiry_and_follow_up_notifications(tmp_path) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(200, json={"ok": True}, request=request)

    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        state = StateManager(db)
        config = AppConfig(channels=[ChannelConfig(id="channel-1", name="Test Channel")])
        config.notifications.enabled = True
        config.notifications.webhook = WebhookNotificationConfig(
            enabled=True,
            url="https://example.test/webhook",
        )
        config.notifications.rules = [
            NotificationRuleConfig(
                event_type="schedule_expiring_soon",
                providers=["webhook"],
                throttle_minutes=0,
            ),
            NotificationRuleConfig(
                event_type="follow_up_missing",
                providers=["webhook"],
                throttle_minutes=0,
            ),
        ]
        start = datetime.now(UTC) - timedelta(hours=1)
        timeline = Timeline()
        timeline.insert(EpisodeBlock(
            start_time=start,
            end_time=start + timedelta(hours=2),
            duration=timedelta(hours=2),
            episode_id="ep-1",
            show_id="show-1",
            season_number=1,
            episode_number=1,
            runtime_seconds=7200,
        ))
        await state.save_schedule_version(
            "channel-1",
            1,
            json.dumps(timeline.snapshot(), default=str),
            status="uploaded",
        )
        router = NotificationRouter(config=config, db=db)
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await check_schedule_expiry(
                config=config,
                state=state,
                notification_router=router,
                warning_hours=12,
            )
        finally:
            await router.close()
    finally:
        await db.disconnect()

    assert len(result["expiring"]) == 1
    assert len(result["missing_follow_up"]) == 1
    assert len(requests) == 2


async def test_automatic_follow_up_generates_approves_and_uploads(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        state = StateManager(db)
        channel = ChannelConfig(
            id="channel-1",
            name="Test Channel",
            scheduling_enabled=True,
            automatic_follow_up=AutomaticFollowUpConfig(
                enabled=True,
                auto_approve=True,
                auto_upload=True,
                warning_hours=12,
            ),
        )
        config = AppConfig(channels=[channel])
        start = datetime.now(UTC) - timedelta(hours=1)
        uploaded = Timeline()
        uploaded.insert(EpisodeBlock(
            start_time=start,
            end_time=start + timedelta(hours=2),
            duration=timedelta(hours=2),
            episode_id="ep-1",
            show_id="show-1",
            season_number=1,
            episode_number=1,
            runtime_seconds=7200,
        ))
        await state.save_schedule_version(
            "channel-1",
            1,
            json.dumps(uploaded.snapshot(), default=str),
            status="uploaded",
        )

        class FakeJobManager:
            def is_running(self, channel_id: str) -> bool:
                return False

            async def run_generation(
                self,
                channel_config: ChannelConfig,
                generation_mode: str,
                parent_version: int | None = None,
            ) -> object:
                follow_up = Timeline()
                follow_up.insert(EpisodeBlock(
                    start_time=start + timedelta(hours=2),
                    end_time=start + timedelta(hours=4),
                    duration=timedelta(hours=2),
                    episode_id="ep-2",
                    show_id="show-1",
                    season_number=1,
                    episode_number=2,
                    runtime_seconds=7200,
                ))
                version_id = await state.save_schedule_version(
                    "channel-1",
                    2,
                    json.dumps(follow_up.snapshot(), default=str),
                    status="draft",
                    parent_version=1,
                )
                return type("Job", (), {
                    "status": JobStatus.COMPLETED,
                    "id": "job-1",
                    "schedule_version_id": version_id,
                    "error_message": None,
                })()

        class FakeTunarrClient:
            async def upload_timeline(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"_upload": {"mode": "fake"}}

        result = await check_schedule_expiry(
            config=config,
            state=state,
            warning_hours=12,
            job_manager=FakeJobManager(),  # type: ignore[arg-type]
            tunarr_client=FakeTunarrClient(),  # type: ignore[arg-type]
            automatic=True,
        )
        meta = await state.get_schedule_version_meta("channel-1", 2)
        attempts = await state.list_upload_attempts("channel-1")
    finally:
        await db.disconnect()

    assert result["automatic_actions"][0]["status"] == "uploaded"
    assert meta is not None
    assert meta["status"] == "uploaded"
    assert attempts[0]["status"] == "success"
    assert attempts[0]["details"]["automatic"] is True


async def test_automatic_follow_up_skips_when_newer_draft_is_pending(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        state = StateManager(db)
        channel = ChannelConfig(
            id="channel-1",
            name="Test Channel",
            scheduling_enabled=True,
            automatic_follow_up=AutomaticFollowUpConfig(enabled=True),
        )
        config = AppConfig(channels=[channel])
        start = datetime.now(UTC) - timedelta(hours=1)
        uploaded = Timeline()
        uploaded.insert(EpisodeBlock(
            start_time=start,
            end_time=start + timedelta(hours=2),
            duration=timedelta(hours=2),
            episode_id="ep-1",
            show_id="show-1",
            season_number=1,
            episode_number=1,
            runtime_seconds=7200,
        ))
        draft = Timeline()
        draft.insert(EpisodeBlock(
            start_time=start - timedelta(hours=4),
            end_time=start - timedelta(hours=3),
            duration=timedelta(hours=2),
            episode_id="ep-2",
            show_id="show-1",
            season_number=1,
            episode_number=2,
            runtime_seconds=7200,
        ))
        await state.save_schedule_version(
            "channel-1",
            1,
            json.dumps(uploaded.snapshot(), default=str),
            status="uploaded",
        )
        await state.save_schedule_version(
            "channel-1",
            2,
            json.dumps(draft.snapshot(), default=str),
            status="draft",
            parent_version=1,
        )

        class FailingJobManager:
            def is_running(self, channel_id: str) -> bool:
                return False

            async def run_generation(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("generation should be skipped")

        result = await check_schedule_expiry(
            config=config,
            state=state,
            warning_hours=12,
            job_manager=FailingJobManager(),  # type: ignore[arg-type]
            automatic=True,
        )
    finally:
        await db.disconnect()

    assert result["automatic_actions"][0]["status"] == "skipped"
    assert result["automatic_actions"][0]["reason"] == (
        "newer draft or approved schedule is pending"
    )
