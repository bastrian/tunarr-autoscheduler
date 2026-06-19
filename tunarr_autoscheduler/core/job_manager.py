from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from tunarr_autoscheduler.core.checkpoint import CheckpointManager
from tunarr_autoscheduler.core.event_bus import Event, EventBus
from tunarr_autoscheduler.core.plugin_loader import PipelineOrchestrator
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    send_notification,
)
from tunarr_autoscheduler.models.blocks import GenerationJob, JobStatus
from tunarr_autoscheduler.models.config import ChannelConfig


class JobManager:
    def __init__(
        self,
        state: StateManager,
        orchestrator: PipelineOrchestrator,
        checkpoint: CheckpointManager,
        event_bus: EventBus,
        notification_router: NotificationRouter | None = None,
    ):
        self._state = state
        self._orchestrator = orchestrator
        self._checkpoint = checkpoint
        self._event_bus = event_bus
        self._notification_router = notification_router
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_jobs: dict[str, GenerationJob] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[GenerationJob]] = {}

    async def run_generation(
        self,
        channel_config: ChannelConfig,
        generation_mode: str = "fresh",
        parent_version: int | None = None,
    ) -> GenerationJob:
        if not channel_config.scheduling_enabled:
            raise ValueError(f"Channel {channel_config.id} has scheduling disabled")

        job = self._create_job(channel_config.id)
        await self._run_job(channel_config, job, generation_mode, parent_version)
        return job

    async def start_generation(
        self,
        channel_config: ChannelConfig,
        generation_mode: str = "fresh",
        parent_version: int | None = None,
    ) -> GenerationJob:
        if not channel_config.scheduling_enabled:
            raise ValueError(f"Channel {channel_config.id} has scheduling disabled")

        channel_id = channel_config.id
        if self.is_running(channel_id):
            await self.cancel_generation(channel_id)

        job = self._create_job(channel_id)
        self._active_jobs[channel_id] = job
        task = asyncio.create_task(
            self._run_job(channel_config, job, generation_mode, parent_version),
        )
        self._tasks[channel_id] = task
        return job

    def _create_job(self, channel_id: str) -> GenerationJob:
        return GenerationJob(
            id=uuid.uuid4().hex,
            channel_id=channel_id,
            status=JobStatus.RUNNING,
            started_at=datetime.now(tz=UTC),
            current_stage="initializing",
        )

    async def _run_job(
        self,
        channel_config: ChannelConfig,
        job: GenerationJob,
        generation_mode: str,
        parent_version: int | None,
    ) -> GenerationJob:
        channel_id = channel_config.id
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()

        async with self._locks[channel_id]:
            generation_id = uuid.uuid4().hex
            self._active_jobs[channel_id] = job
            cancel_event = asyncio.Event()
            self._cancel_flags[channel_id] = cancel_event

            try:
                job.checkpoint_id = generation_id
                await self._state.save_job(job)
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                timeline = await self._orchestrator.execute(
                    channel_config,
                    generation_id,
                    job.id,
                    generation_mode=generation_mode,
                    parent_version=parent_version,
                    progress_callback=lambda stage: self._record_stage(job, stage),
                )
                version_id = timeline.metadata.get("schedule_version_id")
                if version_id:
                    job.schedule_version_id = str(version_id)
                schedule_status = str(timeline.metadata.get("schedule_status", "draft"))
                if schedule_status == "invalid":
                    await send_notification(
                        self._notification_router,
                        NotificationMessage(
                            event_type="schedule_invalid",
                            title=f"Invalid schedule for {channel_config.name or channel_id}",
                            message="Schedule generation completed but validation failed.",
                            severity="warning",
                            channel_id=channel_id,
                            details={
                                "job_id": job.id,
                                "schedule_version": timeline.metadata.get("schedule_version"),
                                "errors": timeline.metadata.get("validation_errors", []),
                            },
                        ),
                    )
                job.status = JobStatus.COMPLETED
                job.current_stage = "completed"
                job.completed_at = datetime.now(tz=UTC)
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                job.current_stage = "cancelled"
                job.completed_at = datetime.now(tz=UTC)
            except Exception as e:
                job.status = JobStatus.FAILED
                job.current_stage = "failed"
                job.error_message = str(e)
                job.completed_at = datetime.now(tz=UTC)
                await self._event_bus.emit(
                    Event.PIPELINE_FAILED, channel_id=channel_id, error=str(e),
                )
                await send_notification(
                    self._notification_router,
                    NotificationMessage(
                        event_type="generation_failed",
                        title=f"Generation failed for {channel_config.name or channel_id}",
                        message=str(e),
                        severity="danger",
                        channel_id=channel_id,
                        details={"job_id": job.id, "stage": job.current_stage},
                    ),
                )
            finally:
                await self._state.save_job(job)
                if self._active_jobs.get(channel_id) is job:
                    self._active_jobs.pop(channel_id, None)
                if self._cancel_flags.get(channel_id) is cancel_event:
                    self._cancel_flags.pop(channel_id, None)
                task = self._tasks.get(channel_id)
                if task is asyncio.current_task():
                    self._tasks.pop(channel_id, None)

            return job

    async def _record_stage(self, job: GenerationJob, stage: str) -> None:
        job.current_stage = stage
        await self._state.save_job(job)

    async def cancel_generation(self, channel_id: str) -> bool:
        cancelled = False
        if channel_id in self._cancel_flags:
            self._cancel_flags[channel_id].set()
            cancelled = True
        task = self._tasks.get(channel_id)
        if task and not task.done():
            task.cancel()
            cancelled = True
        return cancelled

    def get_active_job(self, channel_id: str) -> GenerationJob | None:
        return self._active_jobs.get(channel_id)

    def is_running(self, channel_id: str) -> bool:
        job = self._active_jobs.get(channel_id)
        return job is not None and job.status == JobStatus.RUNNING
