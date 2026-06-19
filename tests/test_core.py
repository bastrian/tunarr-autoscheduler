from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from tunarr_autoscheduler.core.checkpoint import CheckpointManager
from tunarr_autoscheduler.core.event_bus import Event, EventBus
from tunarr_autoscheduler.core.metrics import MetricsCollector
from tunarr_autoscheduler.core.plugin_loader import (
    PipelineContext,
    PipelineOrchestrator,
    Plugin,
    PluginLoader,
)
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import EpisodeBlock
from tunarr_autoscheduler.models.config import ChannelConfig


class RecordingPlugin(Plugin):
    name = "recording"

    async def process(self, timeline, context):
        timeline.metadata.setdefault("stages", []).append(self.name)
        return timeline


class OtherRecordingPlugin(RecordingPlugin):
    name = "other"


class TestEventBus:
    async def test_emit_and_subscribe(self):
        bus = EventBus()
        received = []

        async def handler(**kwargs):
            received.append(kwargs)

        bus.subscribe(Event.PIPELINE_STAGE_COMPLETED, handler)
        await bus.emit(Event.PIPELINE_STAGE_COMPLETED, channel_id="ch1", stage="test")
        assert len(received) == 1
        assert received[0]["channel_id"] == "ch1"

    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def handler(**kwargs):
            received.append(kwargs)

        bus.subscribe(Event.MEDIA_MISSING, handler)
        bus.unsubscribe(Event.MEDIA_MISSING, handler)
        await bus.emit(Event.MEDIA_MISSING, item_id="test")
        assert len(received) == 0


class TestCheckpointManager:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = CheckpointManager(base_dir=tmp)
            tl = Timeline()
            now = datetime.now(tz=UTC)
            tl.insert(EpisodeBlock(
                start_time=now, end_time=now + timedelta(hours=1),
                duration=timedelta(hours=1),
                episode_id="ep1", show_id="show1",
                season_number=1, episode_number=1, runtime_seconds=3600,
            ))

            path = mgr.save("ch1", "gen1", "test_stage", tl)
            assert os.path.exists(path)

            restored_data = mgr.load("ch1", "gen1", "test_stage")
            assert restored_data is not None

            restored_tl = Timeline.from_snapshot(restored_data)
            assert len(restored_tl.blocks) == 1

    def test_get_last_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = CheckpointManager(base_dir=tmp)
            tl = Timeline()
            datetime.now(tz=UTC)

            mgr.save("ch1", "gen1", "stage1", tl)
            mgr.save("ch1", "gen1", "stage2", tl)

            last = mgr.get_last_stage("ch1", "gen1")
            assert last == "stage2"

    def test_get_last_stage_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = CheckpointManager(base_dir=tmp)
            assert mgr.get_last_stage("ch1", "gen1") is None

    def test_get_last_stage_uses_pipeline_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = CheckpointManager(base_dir=tmp)
            tl = Timeline()

            mgr.save("ch1", "gen1", "z_first", tl)
            mgr.save("ch1", "gen1", "a_second", tl)

            last = mgr.get_last_stage(
                "ch1",
                "gen1",
                pipeline_order=["z_first", "a_second"],
            )
            assert last == "a_second"


class TestPipelineOrchestrator:
    async def test_pipeline_context_filters_and_caches_tunarr_media(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def resolve_jellyfin_program_ids(self, item_ids):
                self.calls.append(item_ids)
                return {"available": "program-1"}

        client = Client()
        cache = {}
        context = PipelineContext(
            channel_config=ChannelConfig(id="ch1"),
            generation_id="gen1",
            job_id="job1",
            tunarr_client=client,
            tunarr_media_cache=cache,
        )
        items = [{"id": "available"}, {"id": "missing"}]

        assert await context.filter_tunarr_media(items) == [{"id": "available"}]
        assert await context.filter_tunarr_media(items) == [{"id": "available"}]
        assert client.calls == [["available", "missing"]]

    async def test_pipeline_context_caches_tunarr_program_lists(self):
        class Client:
            def __init__(self):
                self.custom_calls = 0
                self.filler_calls = 0

            async def get_custom_show_programs(self, custom_show_id):
                self.custom_calls += 1
                return [{"id": custom_show_id}]

            async def get_filler_list_programs(self, filler_list_id):
                self.filler_calls += 1
                return [{"id": filler_list_id}]

        client = Client()
        context = PipelineContext(
            channel_config=ChannelConfig(id="ch1"),
            generation_id="gen1",
            job_id="job1",
            tunarr_client=client,
        )

        first_custom = await context.get_custom_show_programs("shared-id")
        second_custom = await context.get_custom_show_programs("shared-id")
        first_filler = await context.get_filler_list_programs("shared-id")
        second_filler = await context.get_filler_list_programs("shared-id")

        assert first_custom == second_custom == [{"id": "shared-id"}]
        assert first_filler == second_filler == [{"id": "shared-id"}]
        assert client.custom_calls == 1
        assert client.filler_calls == 1

    async def test_tunarr_program_cache_is_shared_across_pipeline_stages(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def get_custom_show_programs(self, custom_show_id):
                self.calls += 1
                return [{"id": custom_show_id}]

        class FirstLookupPlugin(Plugin):
            name = "first_lookup"

            async def process(self, timeline, context):
                timeline.metadata["first"] = await context.get_custom_show_programs("show-1")
                return timeline

        class SecondLookupPlugin(Plugin):
            name = "second_lookup"

            async def process(self, timeline, context):
                timeline.metadata["second"] = await context.get_custom_show_programs("show-1")
                return timeline

        client = Client()
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {
            "first_lookup": FirstLookupPlugin(),
            "second_lookup": SecondLookupPlugin(),
        }
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=EventBus(),
            state=None,
            tunarr_client=client,
        )

        timeline = await orchestrator.execute(
            ChannelConfig(id="ch1", pipeline=["first_lookup", "second_lookup"]),
            generation_id="gen1",
            job_id="job1",
        )

        assert timeline.metadata["first"] == timeline.metadata["second"]
        assert client.calls == 1

    async def test_events_use_event_enum(self):
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {"recording": RecordingPlugin()}
        bus = EventBus()
        received = []

        async def handler(**kwargs):
            received.append(kwargs)

        bus.subscribe(Event.PIPELINE_STAGE_COMPLETED, handler)
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=bus,
            state=None,
        )

        await orchestrator.execute(
            ChannelConfig(id="ch1", pipeline=["recording"]),
            generation_id="gen1",
            job_id="job1",
        )

        assert len(received) == 1
        assert received[0]["stage"] == "recording"

    async def test_missing_plugin_fails_without_misalignment(self):
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {"other": OtherRecordingPlugin()}
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=EventBus(),
            state=None,
        )

        with pytest.raises(ValueError, match="missing"):
            await orchestrator.execute(
                ChannelConfig(id="ch1", pipeline=["missing", "other"]),
                generation_id="gen1",
                job_id="job1",
            )

    async def test_tunarr_client_reaches_context(self):
        class Client:
            pass

        class ClientRecordingPlugin(Plugin):
            name = "client_recorder"

            async def process(self, timeline, context):
                timeline.metadata["client"] = context.tunarr_client
                return timeline

        client = Client()
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {"client_recorder": ClientRecordingPlugin()}
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=EventBus(),
            state=None,
            tunarr_client=client,
        )

        timeline = await orchestrator.execute(
            ChannelConfig(id="ch1", pipeline=["client_recorder"]),
            generation_id="gen1",
            job_id="job1",
        )

        assert timeline.metadata["client"] is client

    async def test_progress_callback_receives_stage_updates(self):
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {"recording": RecordingPlugin()}
        stages = []
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=EventBus(),
            state=None,
        )

        await orchestrator.execute(
            ChannelConfig(id="ch1", pipeline=["recording"]),
            generation_id="gen1",
            job_id="job1",
            progress_callback=lambda stage: _record_stage(stages, stage),
        )

        assert stages == ["recording", "completed"]

    async def test_pipeline_records_stage_duration_metrics(self):
        metrics = MetricsCollector()
        loader = PluginLoader(plugin_dirs=[])
        loader._plugins = {"recording": RecordingPlugin()}
        orchestrator = PipelineOrchestrator(
            plugin_loader=loader,
            checkpoint_manager=CheckpointManager(base_dir=tempfile.mkdtemp()),
            event_bus=EventBus(),
            state=None,
            metrics=metrics,
        )

        await orchestrator.execute(
            ChannelConfig(id="ch1", pipeline=["recording"]),
            generation_id="gen1",
            job_id="job1",
        )

        data = metrics.get_metrics()
        assert data["pipeline_stage_counts"]["ch1:recording"]["success"] == 1
        assert data["pipeline_stage_durations"]["ch1:recording"][0] >= 0


async def _record_stage(stages: list[str], stage: str) -> None:
    stages.append(stage)
