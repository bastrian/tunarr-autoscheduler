from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tunarr_autoscheduler.core.job_manager import JobManager
from tunarr_autoscheduler.core.schedule_health import build_schedule_health
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    send_notification,
)
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient
from tunarr_autoscheduler.models.config import AppConfig

logger = logging.getLogger(__name__)


class ScheduleMonitorEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        state: StateManager,
        job_manager: JobManager,
        notification_router: NotificationRouter | None = None,
        tunarr_client: TunarrClient | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._job_manager = job_manager
        self._notification_router = notification_router
        self._tunarr_client = tunarr_client
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if not self._config.schedule_monitor.enabled:
            logger.info("Schedule monitor is disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run_loop(self) -> None:
        interval = max(1, self._config.schedule_monitor.interval_minutes) * 60
        while not self._stop_event.is_set():
            try:
                await check_schedule_expiry(
                    config=self._config,
                    state=self._state,
                    notification_router=self._notification_router,
                    warning_hours=self._config.schedule_monitor.warning_hours,
                    job_manager=self._job_manager,
                    tunarr_client=self._tunarr_client,
                    automatic=True,
                )
            except Exception:
                logger.exception("Schedule monitor check failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue


async def check_schedule_expiry(
    *,
    config: AppConfig,
    state: StateManager,
    notification_router: NotificationRouter | None = None,
    warning_hours: int = 12,
    job_manager: JobManager | None = None,
    tunarr_client: TunarrClient | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    expiring: list[dict[str, Any]] = []
    missing_follow_up: list[dict[str, Any]] = []
    automatic_actions: list[dict[str, Any]] = []
    for channel in config.channels:
        channel_warning_hours = (
            max(1, channel.automatic_follow_up.warning_hours)
            if channel.automatic_follow_up.enabled
            else max(1, warning_hours)
        )
        channel_warning_at = now + timedelta(hours=channel_warning_hours)
        versions = await state.list_versions(channel.id)
        uploaded = next(
            (version for version in versions if version.get("status") == "uploaded"),
            None,
        )
        if uploaded is None:
            continue
        planned_end = _parse_datetime(uploaded.get("planned_end"))
        if planned_end is None:
            continue
        if planned_end <= channel_warning_at:
            item = {
                "channel_id": channel.id,
                "channel_name": channel.name,
                "version": uploaded.get("version"),
                "planned_end": planned_end.isoformat(),
                "warning_hours": channel_warning_hours,
            }
            expiring.append(item)
            await send_notification(
                notification_router,
                NotificationMessage(
                    event_type="schedule_expiring_soon",
                    title=f"Schedule expiring soon for {channel.name or channel.id}",
                    message=f"Uploaded schedule ends at {planned_end.isoformat()}.",
                    severity="warning",
                    channel_id=channel.id,
                    details=item,
                ),
            )
            follow_up = _has_follow_up_after(versions, planned_end)
            if not follow_up:
                missing_follow_up.append(item)
                await send_notification(
                    notification_router,
                    NotificationMessage(
                        event_type="follow_up_missing",
                        title=f"Follow-up missing for {channel.name or channel.id}",
                        message="No draft, approved, or uploaded follow-up extends beyond "
                        f"{planned_end.isoformat()}.",
                        severity="warning",
                        channel_id=channel.id,
                        details=item,
                    ),
                )
                if automatic:
                    automatic_actions.append(
                        await _maybe_create_automatic_follow_up(
                            state=state,
                            channel=channel,
                            versions=versions,
                            uploaded_version=uploaded,
                            planned_end=planned_end,
                            job_manager=job_manager,
                            notification_router=notification_router,
                            tunarr_client=tunarr_client,
                        ),
                    )
    return {
        "warning_hours": warning_hours,
        "expiring": expiring,
        "missing_follow_up": missing_follow_up,
        "automatic_actions": automatic_actions,
    }


async def channel_schedule_statuses(
    *,
    config: AppConfig,
    state: StateManager,
    warning_hours: int | None = None,
) -> dict[str, dict[str, Any]]:
    now = datetime.now(UTC)
    warning_hours = max(1, warning_hours or config.schedule_monitor.warning_hours)
    statuses: dict[str, dict[str, Any]] = {}
    for channel in config.channels:
        versions = await state.list_versions(channel.id)
        uploaded = next(
            (version for version in versions if version.get("status") == "uploaded"),
            None,
        )
        planned_end = _parse_datetime(uploaded.get("planned_end")) if uploaded else None
        channel_warning_hours = (
            max(1, channel.automatic_follow_up.warning_hours)
            if channel.automatic_follow_up.enabled
            else warning_hours
        )
        warning_at = now + timedelta(hours=channel_warning_hours)
        has_follow_up = (
            _has_follow_up_after(versions, planned_end)
            if planned_end is not None
            else False
        )
        upload_attempts = await state.list_upload_attempts(channel.id, limit=1)
        invalid_count = sum(1 for version in versions if version.get("status") == "invalid")
        statuses[channel.id] = {
            "uploaded_version": uploaded.get("version") if uploaded else None,
            "planned_end": planned_end.isoformat() if planned_end else None,
            "expiring": bool(planned_end and planned_end <= warning_at),
            "has_follow_up": has_follow_up,
            "follow_up_ready": bool(channel.automatic_follow_up.enabled and not has_follow_up),
            "auto_follow_up": channel.automatic_follow_up.model_dump(mode="json"),
            "invalid_count": invalid_count,
            "latest_upload": upload_attempts[0] if upload_attempts else None,
        }
    return statuses


async def _maybe_create_automatic_follow_up(
    *,
    state: StateManager,
    channel: Any,
    versions: list[dict[str, object]],
    uploaded_version: dict[str, object],
    planned_end: datetime,
    job_manager: JobManager | None,
    notification_router: NotificationRouter | None,
    tunarr_client: TunarrClient | None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "channel_id": channel.id,
        "channel_name": channel.name,
        "status": "skipped",
        "reason": "",
    }
    if not channel.automatic_follow_up.enabled:
        action["reason"] = "automatic follow-up is disabled"
        return action
    if not channel.scheduling_enabled:
        action["reason"] = "scheduling is disabled"
        return action
    if job_manager is None:
        action["reason"] = "job manager is unavailable"
        return action
    if job_manager.is_running(channel.id):
        action["reason"] = "generation is already running"
        return action
    newer_pending = [
        version for version in versions
        if version.get("status") in {"draft", "approved"}
        and int(str(version.get("version", 0))) > int(str(uploaded_version.get("version", 0)))
    ]
    if newer_pending:
        action["reason"] = "newer draft or approved schedule is pending"
        action["pending_versions"] = [version.get("version") for version in newer_pending]
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "auto_follow_up_skipped",
            "Automatic follow-up skipped",
            "A newer draft or approved schedule is already pending.",
            "info",
            action,
        )
        return action

    try:
        job = await job_manager.run_generation(channel, generation_mode="follow_up")
    except Exception as e:
        action["status"] = "failed"
        action["reason"] = str(e)
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "auto_follow_up_failed",
            "Automatic follow-up generation failed",
            str(e),
            "danger",
            action,
        )
        return action

    action.update({"status": job.status.value, "job_id": job.id})
    if job.error_message:
        action["reason"] = job.error_message
        return action

    generated_meta = await _find_generated_version(state, channel.id, job.schedule_version_id)
    if generated_meta is None:
        action["status"] = "failed"
        action["reason"] = "generation did not create a schedule version"
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "auto_follow_up_failed",
            "Automatic follow-up generation failed",
            action["reason"],
            "danger",
            action,
        )
        return action

    action["version"] = generated_meta["version"]
    health = await _version_health(state, channel.id, generated_meta)
    action["health"] = health
    if health.get("level") == "danger" or generated_meta.get("status") == "invalid":
        action["status"] = "blocked"
        action["reason"] = "generated follow-up failed critical health checks"
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "auto_follow_up_blocked",
            "Automatic follow-up blocked",
            action["reason"],
            "warning",
            action,
        )
        return action

    await _notify_auto_follow_up(
        notification_router,
        channel,
        "auto_follow_up_generated",
        "Automatic follow-up generated",
        f"Generated follow-up schedule version {generated_meta['version']}.",
        "success",
        action,
    )

    version = int(str(generated_meta["version"]))
    if channel.automatic_follow_up.auto_approve:
        await state.approve_version(channel.id, version, approved_by="auto_follow_up")
        action["approved"] = True
        generated_meta = (
            await state.get_schedule_version_meta(channel.id, version) or generated_meta
        )
    if channel.automatic_follow_up.auto_upload:
        if tunarr_client is None:
            action["status"] = "blocked"
            action["reason"] = "Tunarr client is unavailable for automatic upload"
            return action
        if (
            not channel.automatic_follow_up.auto_approve
            and generated_meta.get("status") != "approved"
        ):
            action["status"] = "blocked"
            action["reason"] = "automatic upload requires auto-approval or approved status"
            return action
        upload_result = await _upload_follow_up(
            state=state,
            channel=channel,
            version=version,
            tunarr_client=tunarr_client,
            notification_router=notification_router,
        )
        action["upload"] = upload_result
        if upload_result.get("status") == "success":
            action["status"] = "uploaded"
        else:
            action["status"] = "failed"
            action["reason"] = upload_result.get("message", "automatic upload failed")
    return action


async def _find_generated_version(
    state: StateManager,
    channel_id: str,
    schedule_version_id: str | None,
) -> dict[str, object] | None:
    versions = await state.list_versions(channel_id)
    if schedule_version_id:
        for version in versions:
            if version.get("id") == schedule_version_id:
                return version
    return versions[0] if versions else None


async def _version_health(
    state: StateManager,
    channel_id: str,
    version_meta: dict[str, object],
) -> dict[str, object]:
    timeline_json = await state.get_schedule_version(
        channel_id,
        int(str(version_meta["version"])),
    )
    if not timeline_json:
        return {"level": "danger", "issues": ["Schedule version has no timeline data."]}
    try:
        timeline = Timeline.from_snapshot(json.loads(timeline_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"level": "danger", "issues": ["Schedule timeline could not be parsed."]}
    return build_schedule_health(version_meta, timeline)


async def _upload_follow_up(
    *,
    state: StateManager,
    channel: Any,
    version: int,
    tunarr_client: TunarrClient,
    notification_router: NotificationRouter | None,
) -> dict[str, Any]:
    meta = await state.get_schedule_version_meta(channel.id, version)
    if meta is None:
        return {"status": "failed", "message": "schedule version not found"}
    timeline = Timeline.from_snapshot(json.loads(str(meta["timeline_json"])))
    try:
        upload_result = await tunarr_client.upload_timeline(
            channel.id,
            timeline,
            station_id_custom_show_id=channel.continuity.station_id_custom_show_id,
            bumper_custom_show_id=channel.continuity.bumper_custom_show_id,
        )
    except httpx.HTTPStatusError as e:
        detail = e.response.text.strip() or e.response.reason_phrase
        message = f"Tunarr rejected automatic upload ({e.response.status_code}): {detail}"
        await state.record_upload_attempt(
            channel.id,
            version,
            "failed",
            message,
            {"automatic": True, "status_code": e.response.status_code},
        )
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "upload_failed",
            "Automatic follow-up upload failed",
            message,
            "danger",
            {"version": version, "status_code": e.response.status_code},
        )
        return {"status": "failed", "message": message}
    except httpx.HTTPError as e:
        message = f"Tunarr automatic upload failed: {e}"
        await state.record_upload_attempt(
            channel.id,
            version,
            "failed",
            message,
            {"automatic": True, "error": str(e)},
        )
        await _notify_auto_follow_up(
            notification_router,
            channel,
            "upload_failed",
            "Automatic follow-up upload failed",
            message,
            "danger",
            {"version": version, "error": str(e)},
        )
        return {"status": "failed", "message": message}
    await state.set_schedule_status(channel.id, version, "uploaded")
    details = upload_result.get("_upload", {})
    await state.record_upload_attempt(
        channel.id,
        version,
        "success",
        "Automatically uploaded follow-up schedule version.",
        {
            "automatic": True,
            **(details if isinstance(details, dict) else {}),
        },
    )
    await _notify_auto_follow_up(
        notification_router,
        channel,
        "upload_succeeded",
        "Automatic follow-up upload succeeded",
        f"Uploaded follow-up schedule version {version}.",
        "success",
        {"version": version, "upload": details},
    )
    return {"status": "success", "message": "uploaded", "details": details}


async def _notify_auto_follow_up(
    notification_router: NotificationRouter | None,
    channel: Any,
    event_type: str,
    title: str,
    message: str,
    severity: str,
    details: dict[str, Any],
) -> None:
    await send_notification(
        notification_router,
        NotificationMessage(
            event_type=event_type,
            title=f"{title} for {channel.name or channel.id}",
            message=message,
            severity=severity,
            channel_id=channel.id,
            details=details,
        ),
    )


def _has_follow_up_after(versions: list[dict[str, object]], planned_end: datetime) -> bool:
    for version in versions:
        if version.get("status") not in {"draft", "approved", "uploaded"}:
            continue
        candidate_end = _parse_datetime(version.get("planned_end"))
        if candidate_end is not None and candidate_end > planned_end:
            return True
    return False


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
