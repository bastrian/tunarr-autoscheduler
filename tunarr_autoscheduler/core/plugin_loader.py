from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from tunarr_autoscheduler.core.event_bus import Event
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.config import AppConfig, ChannelConfig


class PipelineContext:
    def __init__(
        self,
        channel_config: ChannelConfig,
        generation_id: str,
        job_id: str,
        daypart_constraints: dict[str, object] | None = None,
        state: Any = None,
        media_repo: Any = None,
        playlist_repo: Any = None,
        tunarr_client: Any = None,
        metrics: Any = None,
        generation_mode: str = "continue",
        schedule_start: datetime | None = None,
        parent_version: int | None = None,
        reserved_episode_ids: set[str] | None = None,
        reserved_movie_ids: set[str] | None = None,
        tunarr_program_cache: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        tunarr_media_cache: dict[str, bool] | None = None,
        app_config: AppConfig | None = None,
    ):
        self.channel_config = channel_config
        self.generation_id = generation_id
        self.job_id = job_id
        self.daypart_constraints = daypart_constraints or {}
        self.state = state
        self.media_repo = media_repo
        self.playlist_repo = playlist_repo
        self.tunarr_client = tunarr_client
        self.metrics = metrics
        self.generation_mode = generation_mode
        self.schedule_start = schedule_start
        self.parent_version = parent_version
        self.reserved_episode_ids = reserved_episode_ids or set()
        self.reserved_movie_ids = reserved_movie_ids or set()
        self.local_rotation_states: dict[str, Any] = {}
        self.app_config = app_config
        self._tunarr_program_cache = (
            tunarr_program_cache if tunarr_program_cache is not None else {}
        )
        self._tunarr_media_cache = (
            tunarr_media_cache if tunarr_media_cache is not None else {}
        )

    async def get_custom_show_programs(self, custom_show_id: str) -> list[dict[str, Any]]:
        return await self._get_tunarr_programs("custom-show", custom_show_id)

    async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
        return await self._get_tunarr_programs("filler-list", filler_list_id)

    async def filter_tunarr_media(
        self, items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.tunarr_client is None or not hasattr(
            self.tunarr_client, "resolve_jellyfin_program_ids",
        ):
            return items
        item_ids = [str(item.get("id", "")) for item in items if item.get("id")]
        unknown = [
            item_id for item_id in dict.fromkeys(item_ids)
            if item_id not in self._tunarr_media_cache
        ]
        if unknown:
            resolved = await self.tunarr_client.resolve_jellyfin_program_ids(unknown)
            resolved_ids = set(resolved)
            self._tunarr_media_cache.update(
                (item_id, item_id in resolved_ids) for item_id in unknown
            )
        return [
            item for item in items
            if self._tunarr_media_cache.get(str(item.get("id", "")), False)
        ]

    async def _get_tunarr_programs(
        self, source_type: str, source_id: str,
    ) -> list[dict[str, Any]]:
        key = (source_type, source_id)
        cached = self._tunarr_program_cache.get(key)
        if cached is not None:
            return cached

        if self.tunarr_client is None:
            return []
        if source_type == "custom-show":
            programs = await self.tunarr_client.get_custom_show_programs(source_id)
        else:
            programs = await self.tunarr_client.get_filler_list_programs(source_id)
        typed_programs = cast(list[dict[str, Any]], programs)
        self._tunarr_program_cache[key] = typed_programs
        return typed_programs


class Plugin:
    name: str = ""
    dependencies: list[str] = []
    config_schema: type | None = None

    def __init__(self) -> None:
        self.config: Any = None

    def initialize(self, core: Any) -> None:
        pass

    async def process(self, timeline: Timeline, context: PipelineContext) -> Timeline:
        return timeline


class PluginLoader:
    def __init__(self, plugin_dirs: list[str], disabled: list[str] | None = None):
        self._plugin_dirs = plugin_dirs
        self._disabled = disabled or []
        self._plugins: dict[str, Plugin] = {}

    def discover(self) -> dict[str, Plugin]:
        self._plugins = {}

        for plugin_dir in self._plugin_dirs:
            expanded = os.path.expanduser(plugin_dir)
            if Path(expanded).exists():
                sys.path.insert(0, expanded)
                for f in Path(expanded).iterdir():
                    if f.suffix == ".py" and not f.name.startswith("_"):
                        self._load_from_file(f.stem)

        self._load_from_module("tunarr_autoscheduler.plugins")

        return self._plugins

    def _load_from_file(self, module_name: str) -> None:
        try:
            module = importlib.import_module(module_name)
            self._register_plugins(module, module_name)
        except ImportError:
            pass

    def _load_from_module(self, module_path: str) -> None:
        try:
            module = importlib.import_module(module_path)
            self._register_plugins(module, module_path)
            if hasattr(module, "__path__"):
                for module_info in pkgutil.iter_modules(module.__path__, f"{module_path}."):
                    submodule = importlib.import_module(module_info.name)
                    self._register_plugins(submodule, module_info.name)
        except ImportError:
            pass

    def _register_plugins(self, module: Any, source: str) -> None:
        for _, obj in inspect.getmembers(module):
            if (
                inspect.isclass(obj)
                and issubclass(obj, Plugin)
                and obj is not Plugin
            ):
                name = getattr(obj, "name", None) or obj.__name__
                if name in self._disabled:
                    continue
                if name not in self._plugins:
                    self._plugins[name] = obj()

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def get_pipeline_plugins(self, pipeline_names: list[str]) -> list[tuple[str, Plugin]]:
        missing = [
            name
            for name in pipeline_names
            if name not in self._plugins and name not in self._disabled
        ]
        if missing:
            raise ValueError(f"Missing pipeline plugin(s): {', '.join(missing)}")
        return [
            (name, self._plugins[name])
            for name in pipeline_names
            if name in self._plugins
        ]


class PipelineOrchestrator:
    def __init__(
        self,
        plugin_loader: PluginLoader,
        checkpoint_manager: Any,
        event_bus: Any,
        state: Any,
        media_repo: Any = None,
        playlist_repo: Any = None,
        tunarr_client: Any = None,
        metrics: Any = None,
        app_config: AppConfig | None = None,
    ):
        self._plugin_loader = plugin_loader
        self._checkpoint_manager = checkpoint_manager
        self._event_bus = event_bus
        self._state = state
        self._media_repo = media_repo
        self._playlist_repo = playlist_repo
        self._tunarr_client = tunarr_client
        self._metrics = metrics
        self._app_config = app_config

    async def execute(
        self,
        channel_config: ChannelConfig,
        generation_id: str,
        job_id: str,
        generation_mode: str = "fresh",
        parent_version: int | None = None,
        progress_callback: Any = None,
    ) -> Timeline:
        timeline = Timeline()
        pipeline_names = channel_config.pipeline
        stages = self._plugin_loader.get_pipeline_plugins(pipeline_names)

        last_stage = self._checkpoint_manager.get_last_stage(
            channel_config.id, generation_id, pipeline_names,
        )
        start_index = 0
        tunarr_program_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        tunarr_media_cache: dict[str, bool] = {}
        schedule_start: datetime | None = None
        resolved_parent_version: int | None = None
        reserved_episode_ids: set[str] = set()
        reserved_movie_ids: set[str] = set()
        if generation_mode == "follow_up":
            follow_up = await self._state.get_follow_up_context(
                channel_config.id, parent_version=parent_version,
            )
            if follow_up is None:
                if parent_version is not None:
                    raise ValueError(
                        f"Schedule version {parent_version} is not available "
                        "as a follow-up base",
                    )
                raise ValueError("No valid schedule version is available for a follow-up plan")
            schedule_start = follow_up["end_time"]
            resolved_parent_version = follow_up["version"]
            reserved_episode_ids = follow_up["episode_ids"]
            reserved_movie_ids = follow_up.get("movie_ids", set())
        if last_stage and last_stage in pipeline_names:
            stage_names = [name for name, _ in stages]
            start_index = stage_names.index(last_stage) + 1 if last_stage in stage_names else 0
            checkpoint = self._checkpoint_manager.load(
                channel_config.id, generation_id, last_stage,
            )
            if checkpoint:
                timeline = Timeline.from_snapshot(checkpoint)

        for stage_name, plugin in stages[start_index:]:
            if progress_callback:
                await progress_callback(stage_name)

            context = PipelineContext(
                channel_config=channel_config,
                generation_id=generation_id,
                job_id=job_id,
                state=self._state,
                media_repo=self._media_repo,
                playlist_repo=self._playlist_repo,
                tunarr_client=self._tunarr_client,
                metrics=self._metrics,
                generation_mode=generation_mode,
                schedule_start=schedule_start,
                parent_version=resolved_parent_version,
                reserved_episode_ids=reserved_episode_ids,
                reserved_movie_ids=reserved_movie_ids,
                tunarr_program_cache=tunarr_program_cache,
                tunarr_media_cache=tunarr_media_cache,
                app_config=self._app_config,
            )

            await self._event_bus.emit(Event.PIPELINE_STAGE_STARTED,
                channel_id=channel_config.id, stage=stage_name)

            started = time.perf_counter()
            try:
                timeline = await plugin.process(timeline, context)
            except Exception as e:
                if self._metrics:
                    self._metrics.record_pipeline_stage(
                        channel_config.id,
                        stage_name,
                        (time.perf_counter() - started) * 1000,
                        "failed",
                    )
                await self._event_bus.emit(Event.PIPELINE_FAILED,
                    channel_id=channel_config.id, stage=stage_name, error=str(e))
                raise
            if self._metrics:
                self._metrics.record_pipeline_stage(
                    channel_config.id,
                    stage_name,
                    (time.perf_counter() - started) * 1000,
                    "success",
                )

            self._checkpoint_manager.save(
                channel_config.id, generation_id, stage_name, timeline,
            )

            await self._event_bus.emit(Event.PIPELINE_STAGE_COMPLETED,
                channel_id=channel_config.id, stage=stage_name)

        if progress_callback:
            await progress_callback("completed")

        return timeline
