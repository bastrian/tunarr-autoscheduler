from __future__ import annotations

import asyncio

from tunarr_autoscheduler.core.event_bus import EventBus
from tunarr_autoscheduler.core.job_manager import JobManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import JobStatus
from tunarr_autoscheduler.models.config import ChannelConfig


class State:
    def __init__(self) -> None:
        self.saved_jobs = []

    async def save_job(self, job) -> None:
        self.saved_jobs.append({
            "status": job.status.value,
            "stage": job.current_stage,
            "checkpoint_id": job.checkpoint_id,
            "schedule_version_id": job.schedule_version_id,
        })


class Orchestrator:
    async def execute(
        self,
        channel_config: ChannelConfig,
        generation_id: str,
        job_id: str,
        generation_mode: str = "fresh",
        parent_version: int | None = None,
        progress_callback=None,
    ) -> Timeline:
        if progress_callback:
            await progress_callback("validator")
            await progress_callback("schedule_persister")
        return Timeline(metadata={"schedule_version_id": "version-1"})


async def test_job_manager_persists_stage_progress_and_schedule_version() -> None:
    state = State()
    manager = JobManager(
        state=state,
        orchestrator=Orchestrator(),
        checkpoint=None,
        event_bus=EventBus(),
    )

    job = await manager.run_generation(ChannelConfig(id="ch1", scheduling_enabled=True))

    assert job.current_stage == "completed"
    assert job.schedule_version_id == "version-1"
    assert any(saved["stage"] == "validator" for saved in state.saved_jobs)
    assert any(saved["stage"] == "schedule_persister" for saved in state.saved_jobs)
    assert state.saved_jobs[-1]["schedule_version_id"] == "version-1"
    assert state.saved_jobs[-1]["checkpoint_id"] is not None


async def test_cancel_generation_returns_false_when_no_job_is_running() -> None:
    manager = JobManager(
        state=State(),
        orchestrator=Orchestrator(),
        checkpoint=None,
        event_bus=EventBus(),
    )

    cancelled = await manager.cancel_generation("ch1")

    assert cancelled is False


async def test_cancel_generation_cancels_active_background_job() -> None:
    class SlowOrchestrator:
        async def execute(
            self,
            channel_config: ChannelConfig,
            generation_id: str,
            job_id: str,
            generation_mode: str = "fresh",
            parent_version: int | None = None,
            progress_callback=None,
        ) -> Timeline:
            await asyncio.sleep(60)
            return Timeline()

    manager = JobManager(
        state=State(),
        orchestrator=SlowOrchestrator(),
        checkpoint=None,
        event_bus=EventBus(),
    )
    job = await manager.start_generation(ChannelConfig(id="ch1", scheduling_enabled=True))
    await asyncio.sleep(0)

    cancelled = await manager.cancel_generation("ch1")
    await asyncio.sleep(0)

    assert cancelled is True
    assert job.status == JobStatus.CANCELLED
    assert manager.is_running("ch1") is False
