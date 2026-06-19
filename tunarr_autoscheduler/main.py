from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import httpx
import uvicorn

from tunarr_autoscheduler.core.app_logging import configure_logging
from tunarr_autoscheduler.core.backups import (
    BackupMonitorEngine,
    BackupResult,
    create_backup_archive,
    notify_backup_failed,
)
from tunarr_autoscheduler.core.checkpoint import CheckpointManager
from tunarr_autoscheduler.core.config import ConfigManager
from tunarr_autoscheduler.core.diagnostics import create_diagnostic_bundle
from tunarr_autoscheduler.core.event_bus import EventBus
from tunarr_autoscheduler.core.job_manager import JobManager
from tunarr_autoscheduler.core.metrics import MetricsCollector
from tunarr_autoscheduler.core.plugin_loader import PipelineOrchestrator, PluginLoader
from tunarr_autoscheduler.core.schedule_health import build_schedule_health, metric_map
from tunarr_autoscheduler.core.schedule_monitor import ScheduleMonitorEngine, check_schedule_expiry
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.repositories.audit_repo import AuditLogRepository
from tunarr_autoscheduler.db.repositories.media_repo import MediaRepository
from tunarr_autoscheduler.db.repositories.playlist_repo import PlaylistRepository
from tunarr_autoscheduler.db.repositories.recommendation_profile_repo import (
    RecommendationProfileRepository,
)
from tunarr_autoscheduler.db.repositories.recommendation_run_repo import (
    RecommendationRunRepository,
)
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.integrations.jellyfin.client import JellyfinClient
from tunarr_autoscheduler.integrations.jellyfin.sync import MediaSyncEngine
from tunarr_autoscheduler.integrations.metadata.audit import build_metadata_audit
from tunarr_autoscheduler.integrations.metadata.cache import ExternalMetadataCacheRepository
from tunarr_autoscheduler.integrations.metadata.service import MetadataEnrichmentService
from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    send_notification,
)
from tunarr_autoscheduler.integrations.tunarr.channel_sync import ChannelSyncEngine
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient, _schedule_payload
from tunarr_autoscheduler.models.playlist import PlaylistItem
from tunarr_autoscheduler.plugins.tunarr_uploader import TunarrUploader
from tunarr_autoscheduler.recommendations import BUILT_IN_PROFILES, RecommendationEngine
from tunarr_autoscheduler.recommendations.signals import build_external_signals
from tunarr_autoscheduler.web.app import create_app
from tunarr_autoscheduler.web.routes.recommendations import (
    _build_recommendation_plan,
    apply_recommendation_plan_to_core,
)

logger = logging.getLogger(__name__)


class Core:
    def __init__(self) -> None:
        self.config_manager: ConfigManager = ConfigManager()
        self.event_bus: EventBus = EventBus()
        self.metrics: MetricsCollector = MetricsCollector()
        self.checkpoint_manager: CheckpointManager = CheckpointManager()
        self.plugin_loader: PluginLoader = PluginLoader(
            plugin_dirs=[],
            disabled=[],
        )
        self.state: StateManager | None = None
        self.job_manager: JobManager | None = None
        self.pipeline_orchestrator: PipelineOrchestrator | None = None
        self.jellyfin_client: JellyfinClient | None = None
        self.tunarr_client: TunarrClient | None = None
        self.media_sync: MediaSyncEngine | None = None
        self.schedule_monitor: ScheduleMonitorEngine | None = None
        self.backup_monitor: BackupMonitorEngine | None = None
        self.media_repo: MediaRepository | None = None
        self.playlist_repo: PlaylistRepository | None = None
        self.recommendation_profile_repo: RecommendationProfileRepository | None = None
        self.recommendation_run_repo: RecommendationRunRepository | None = None
        self.channel_sync_engine: ChannelSyncEngine | None = None
        self.notification_router: NotificationRouter | None = None
        self.audit_repo: AuditLogRepository | None = None
        self.db: Database | None = None


async def run_scheduler() -> None:
    core = Core()
    if not core.config_manager.exists():
        core.config_manager.write_default_template()
        print(
            "Default config written to ~/.tunarr/config.yaml. "
            "Open the setup page to configure credentials."
        )

    config = core.config_manager.load()
    configure_logging(config.server.log_level, config.server.log_file)
    logger.info("Starting Tunarr AutoScheduler")

    if (
        not core.config_manager.credentials_configured()
        or not core.config_manager.auth_configured()
    ):
        print(
            "Setup required. Open the web UI and configure the admin password, "
            "Jellyfin credentials, and Tunarr URL."
        )
        logger.warning("Setup required before full scheduler startup")
        app = create_app(core)
        app.state.restart_after_setup = True
        await _serve_app(app, config.server.host, config.server.port, config.server.log_level)
        return

    db_path = config.database.url.replace("sqlite+aiosqlite:///", "")
    db_path = os.path.expanduser(db_path)
    logger.debug("Resolved database path: %s", db_path)

    core.db = Database(db_path)
    await core.db.connect()
    await run_migrations(core.db)

    core.state = StateManager(core.db)
    core.media_repo = MediaRepository(core.db)
    core.playlist_repo = PlaylistRepository(core.db)
    core.recommendation_profile_repo = RecommendationProfileRepository(core.db)
    core.recommendation_run_repo = RecommendationRunRepository(core.db)
    core.notification_router = NotificationRouter(config=config, db=core.db)
    core.audit_repo = AuditLogRepository(core.db)

    core.plugin_loader = PluginLoader(
        plugin_dirs=[os.path.expanduser(d) for d in config.plugins.directories],
        disabled=config.plugins.disabled,
    )
    core.plugin_loader.discover()
    logger.debug("Plugin discovery complete")

    core.checkpoint_manager = CheckpointManager()
    core.event_bus = EventBus()

    core.jellyfin_client = JellyfinClient(
        base_url=config.jellyfin.url,
        api_key=config.jellyfin.api_key,
        user_id=config.jellyfin.user_id,
    )

    core.tunarr_client = TunarrClient(
        base_url=config.tunarr.url,
    )

    core.pipeline_orchestrator = PipelineOrchestrator(
        plugin_loader=core.plugin_loader,
        checkpoint_manager=core.checkpoint_manager,
        event_bus=core.event_bus,
        state=core.state,
        media_repo=core.media_repo,
        playlist_repo=core.playlist_repo,
        tunarr_client=core.tunarr_client,
        metrics=core.metrics,
        app_config=config,
    )

    core.job_manager = JobManager(
        state=core.state,
        orchestrator=core.pipeline_orchestrator,
        checkpoint=core.checkpoint_manager,
        event_bus=core.event_bus,
        notification_router=core.notification_router,
    )

    core.media_sync = MediaSyncEngine(
        client=core.jellyfin_client,
        media_repo=core.media_repo,
        event_bus=core.event_bus,
        interval_minutes=config.jellyfin.sync_interval_minutes,
        metrics=core.metrics,
        notification_router=core.notification_router,
    )

    core.channel_sync_engine = ChannelSyncEngine(
        tunarr_client=core.tunarr_client,
        config_manager=core.config_manager,
    )

    logger.info("Running channel sync on startup")
    try:
        sync_result = await core.channel_sync_engine.sync()
        logger.info("Channel sync result: %s", sync_result)
    except Exception as e:
        logger.warning("Channel sync failed on startup (Tunarr may not be ready): %s", e)
        await send_notification(
            core.notification_router,
            NotificationMessage(
                event_type="tunarr_connectivity_failed",
                title="Tunarr connectivity failed",
                message=str(e),
                severity="danger",
                details={"source": "startup_channel_sync"},
            ),
        )

    logger.info("Starting media sync engine")
    await core.media_sync.start()
    core.schedule_monitor = ScheduleMonitorEngine(
        config=config,
        state=core.state,
        job_manager=core.job_manager,
        notification_router=core.notification_router,
        tunarr_client=core.tunarr_client,
    )
    logger.info("Starting schedule monitor")
    await core.schedule_monitor.start()
    core.backup_monitor = BackupMonitorEngine(
        config=config,
        config_path=core.config_manager.config_path,
        notification_router=core.notification_router,
    )
    logger.info("Starting backup monitor")
    await core.backup_monitor.start()

    app = create_app(core)

    logger.info("Starting web server on %s:%s", config.server.host, config.server.port)
    try:
        await _serve_app(app, config.server.host, config.server.port, config.server.log_level)
    finally:
        if core.backup_monitor:
            await core.backup_monitor.stop()
        if core.schedule_monitor:
            await core.schedule_monitor.stop()
        if core.media_sync:
            await core.media_sync.stop()
        if core.jellyfin_client:
            await core.jellyfin_client.close()
        if core.tunarr_client:
            await core.tunarr_client.close()
        if core.notification_router:
            await core.notification_router.close()
        if core.db:
            await core.db.disconnect()


async def _serve_app(app: Any, host: str, port: int, log_level: str) -> None:
    server_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
    )
    server = uvicorn.Server(server_config)
    await server.serve()


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tunarr-autoscheduler")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("sync-channels", help="Sync Tunarr channels into scheduler config")

    generate = subcommands.add_parser("generate-schedule", help="Generate a schedule version")
    generate.add_argument("channel_id")
    generate.add_argument(
        "--mode",
        choices=("fresh", "follow-up"),
        default="fresh",
        help="Start from the beginning or continue after the latest valid schedule",
    )
    generate.add_argument(
        "--parent-version",
        type=int,
        default=None,
        help="Follow up after a specific schedule version instead of the latest planned end",
    )

    upload = subcommands.add_parser("upload-schedule", help="Upload a saved schedule version")
    upload.add_argument("channel_id")
    upload.add_argument("version", type=int)
    upload.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payload without POSTing to Tunarr",
    )
    upload.add_argument(
        "--dump-payload",
        action="store_true",
        help="Print the converted scheduler payload before upload",
    )
    upload.add_argument(
        "--dump-tunarr-payload",
        action="store_true",
        help="Print the sanitized schedule payload sent to Tunarr",
    )
    upload.add_argument(
        "--dump-generated",
        action="store_true",
        help="Print Tunarr's generated schedule-time-slots response before persisting",
    )
    upload.add_argument(
        "--time-compat",
        action="store_true",
        help=(
            "Exercise Tunarr's legacy persistent time programming path with generated "
            "manual fallback. The default upload uses the stable manual lineup path."
        ),
    )

    list_schedules = subcommands.add_parser(
        "list-schedules",
        help="List recent saved schedule versions for a channel",
    )
    list_schedules.add_argument("channel_id")

    health = subcommands.add_parser(
        "schedule-health",
        help="Read saved schedule health without calling Tunarr or Jellyfin",
    )
    health.add_argument("channel_id")
    health.add_argument(
        "--version",
        default="latest",
        help="Schedule version to inspect, or 'latest' (default)",
    )
    health.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    backup = subcommands.add_parser(
        "backup-data",
        help="Create a zip backup of config.yaml and scheduler.db",
    )
    backup.add_argument(
        "--output-dir",
        default="~/.tunarr/backups",
        help="Directory for backup archives",
    )
    backup.add_argument(
        "--retention-count",
        type=int,
        default=None,
        help="Number of backup archives to keep in the output directory",
    )
    backup.add_argument(
        "--min-free-mb",
        type=int,
        default=None,
        help="Minimum free disk space required before writing the backup",
    )
    backup.add_argument(
        "--size-multiplier",
        type=int,
        default=None,
        help="Require this many times the source data size as free space",
    )

    diagnostics = subcommands.add_parser(
        "diagnostic-bundle",
        help="Create a redacted diagnostic zip for support and AI handoff",
    )
    diagnostics.add_argument(
        "--output-dir",
        default="~/.tunarr/diagnostics",
        help="Directory for diagnostic bundles",
    )
    diagnostics.add_argument(
        "--keep-history",
        action="store_true",
        help="Keep older diagnostic bundles instead of deleting them first",
    )
    diagnostics.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    expiry = subcommands.add_parser(
        "check-schedule-expiry",
        help="Send notifications for schedules that are close to ending",
    )
    expiry.add_argument(
        "--warning-hours",
        type=int,
        default=12,
        help="Warn when uploaded schedules end within this many hours",
    )
    expiry.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    expiry.add_argument(
        "--auto",
        action="store_true",
        help="Generate configured automatic follow-ups when safeguards allow it",
    )

    metadata_audit = subcommands.add_parser(
        "metadata-audit",
        help="Inspect local provider-ID coverage for TMDB, TVDB, and IMDb",
    )
    metadata_audit.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    metadata_refresh = subcommands.add_parser(
        "metadata-refresh",
        help="Refresh external metadata cache from local provider IDs",
    )
    metadata_refresh.add_argument(
        "--apply",
        action="store_true",
        help="Perform external API requests. Without this flag only a dry-run summary is printed.",
    )
    metadata_refresh.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of metadata candidates to inspect",
    )
    metadata_refresh.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    notify_test = subcommands.add_parser(
        "notify-test",
        help="Send a test notification through configured notification routing",
    )
    notify_test.add_argument(
        "--event-type",
        default="test",
        help="Notification event type to route",
    )
    notify_test.add_argument(
        "--title",
        default="Test notification",
        help="Notification title",
    )
    notify_test.add_argument(
        "--message",
        default="This is a test from Tunarr AutoScheduler.",
        help="Notification message",
    )
    notify_test.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    recommend = subcommands.add_parser(
        "recommend",
        help="Inspect media recommendation readiness and run recommendation profiles",
    )
    recommend_subcommands = recommend.add_subparsers(dest="recommend_command", required=True)
    recommend_diagnostics = recommend_subcommands.add_parser(
        "diagnostics",
        help="Show local media-cache readiness for recommendations",
    )
    recommend_diagnostics.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    recommend_scan = recommend_subcommands.add_parser(
        "scan",
        help="Alias for diagnostics; scans recommendation metadata readiness",
    )
    recommend_scan.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    recommend_profiles = recommend_subcommands.add_parser(
        "profiles",
        help="List built-in and custom recommendation profiles",
    )
    recommend_profiles.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    recommend_run = recommend_subcommands.add_parser(
        "run",
        help="Run a recommendation profile against the local media cache",
    )
    recommend_run.add_argument(
        "--profile",
        required=True,
        help="Recommendation profile to run",
    )
    recommend_run.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of recommendations to print",
    )
    recommend_run.add_argument(
        "--include-excluded",
        action="store_true",
        help="Include candidates rejected by hard filters",
    )
    recommend_run.add_argument(
        "--language-rule",
        choices=(
            "none",
            "english_audio",
            "english_subtitles",
            "english_audio_or_subtitles",
            "prefer_english_audio_allow_subtitles",
        ),
        default=None,
        help="Override the profile language rule for this run",
    )
    recommend_run.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    recommend_compare = recommend_subcommands.add_parser(
        "compare",
        help="Compare recommendation profiles by top candidates, score, and overlap",
    )
    recommend_compare.add_argument(
        "--profiles",
        required=True,
        help="Comma-separated profile IDs to compare",
    )
    recommend_compare.add_argument("--limit", type=int, default=25)
    recommend_compare.add_argument(
        "--language-rule",
        choices=(
            "none",
            "english_audio",
            "english_subtitles",
            "english_audio_or_subtitles",
            "prefer_english_audio_allow_subtitles",
        ),
        default=None,
    )
    recommend_compare.add_argument("--json", action="store_true")
    recommend_explain = recommend_subcommands.add_parser(
        "explain",
        help="Explain how one media item scores against a profile",
    )
    recommend_explain.add_argument("--item-id", required=True)
    recommend_explain.add_argument("--profile", required=True)
    recommend_explain.add_argument(
        "--language-rule",
        choices=(
            "none",
            "english_audio",
            "english_subtitles",
            "english_audio_or_subtitles",
            "prefer_english_audio_allow_subtitles",
        ),
        default=None,
    )
    recommend_explain.add_argument("--json", action="store_true")
    recommend_playlist = recommend_subcommands.add_parser(
        "create-playlist",
        help="Create a scheduler playlist from the top accepted recommendations",
    )
    recommend_playlist.add_argument("--profile", required=True)
    recommend_playlist.add_argument("--name", required=True)
    recommend_playlist.add_argument("--limit", type=int, default=25)
    recommend_playlist.add_argument("--category-id", default="")
    recommend_playlist.add_argument("--channel-scope", default="")
    recommend_playlist.add_argument("--tags", default="recommended")
    recommend_playlist.add_argument("--json", action="store_true")
    recommend_builder = recommend_subcommands.add_parser(
        "builder-dry-run",
        help="Preview a recommendation builder channel/daypart plan without saving it",
    )
    recommend_builder.add_argument("--mode", choices=("channel", "daypart"), default="channel")
    recommend_builder.add_argument(
        "--builder-mode",
        choices=("scratch", "improve"),
        default="scratch",
        help="Build from default windows or improve the selected channel's existing dayparts",
    )
    recommend_builder.add_argument("--channel-id", default="")
    recommend_builder.add_argument("--channel-name", default="Recommended Channel")
    recommend_builder.add_argument("--profile", default="auto")
    recommend_builder.add_argument("--themes", default="")
    recommend_builder.add_argument("--seed", default="")
    recommend_builder.add_argument(
        "--language-rule",
        choices=(
            "profile_default",
            "none",
            "english_audio",
            "english_subtitles",
            "english_audio_or_subtitles",
            "prefer_english_audio_allow_subtitles",
        ),
        default="profile_default",
    )
    recommend_builder.add_argument("--per-theme-limit", type=int, default=12)
    recommend_builder.add_argument(
        "--balance-mode",
        choices=("tv_balanced", "series_heavy", "series_only", "mixed", "movie_friendly"),
        default="tv_balanced",
    )
    recommend_builder.add_argument("--max-movies-per-theme", type=int, default=None)
    recommend_builder.add_argument("--min-series-per-theme", type=int, default=3)
    recommend_builder.add_argument("--create-channel", action="store_true")
    recommend_builder.add_argument("--append-dayparts", action="store_true")
    recommend_builder.add_argument("--json", action="store_true")
    recommend_apply = recommend_subcommands.add_parser(
        "builder-apply",
        help="Apply a saved recommendation builder run",
    )
    recommend_apply.add_argument("--run-id", required=True)
    recommend_apply.add_argument(
        "--generate-draft",
        action="store_true",
        help="Generate a draft schedule after applying the run",
    )
    recommend_apply.add_argument(
        "--generation-mode",
        choices=("fresh", "follow-up"),
        default="fresh",
    )
    recommend_apply.add_argument("--parent-version", type=int, default=None)
    recommend_apply.add_argument("--json", action="store_true")
    return parser


def run_cli(argv: list[str]) -> None:
    args = _cli_parser().parse_args(argv)
    if args.command == "sync-channels":
        asyncio.run(_sync_channels_cli())
        return
    if args.command == "generate-schedule":
        asyncio.run(_generate_schedule_cli(args))
        return
    if args.command == "upload-schedule":
        asyncio.run(_upload_schedule_cli(args))
        return
    if args.command == "list-schedules":
        asyncio.run(_list_schedules_cli(args))
        return
    if args.command == "schedule-health":
        asyncio.run(_schedule_health_cli(args))
        return
    if args.command == "backup-data":
        asyncio.run(_backup_data_cli(args))
        return
    if args.command == "diagnostic-bundle":
        asyncio.run(_diagnostic_bundle_cli(args))
        return
    if args.command == "check-schedule-expiry":
        asyncio.run(_check_schedule_expiry_cli(args))
        return
    if args.command == "metadata-audit":
        asyncio.run(_metadata_audit_cli(args))
        return
    if args.command == "metadata-refresh":
        asyncio.run(_metadata_refresh_cli(args))
        return
    if args.command == "notify-test":
        asyncio.run(_notify_test_cli(args))
        return
    if args.command == "recommend":
        asyncio.run(_recommend_cli(args))
        return
    raise SystemExit(f"Unknown command: {args.command}")


async def _load_cli_core(
    *,
    require_credentials: bool = True,
    include_tunarr: bool = True,
    run_db_setup: bool = True,
) -> Core:
    core = Core()
    if not core.config_manager.exists():
        core.config_manager.write_default_template()
        raise RuntimeError(
            "Default config written to ~/.tunarr/config.yaml. "
            "Open the web UI setup page to configure credentials.",
        )
    config = core.config_manager.load()
    configure_logging(config.server.log_level, config.server.log_file)
    if require_credentials and not core.config_manager.credentials_configured():
        raise RuntimeError(
            "Credentials not configured. Open the web UI setup page to configure "
            "Jellyfin credentials and the Tunarr URL.",
        )

    db_path = config.database.url.replace("sqlite+aiosqlite:///", "")
    db_path = os.path.expanduser(db_path)
    if not run_db_setup and not Path(db_path).exists():
        raise RuntimeError(f"Scheduler database not found: {db_path}")
    core.db = Database(db_path)
    await core.db.connect()
    if run_db_setup:
        await run_migrations(core.db)
    core.state = StateManager(core.db)
    if include_tunarr:
        core.tunarr_client = TunarrClient(base_url=config.tunarr.url)
    core.notification_router = NotificationRouter(config=config, db=core.db)
    return core


async def _close_cli_core(core: Core) -> None:
    if core.tunarr_client:
        await core.tunarr_client.close()
    if core.notification_router:
        await core.notification_router.close()
    if core.db:
        await core.db.disconnect()


async def _sync_channels_cli() -> None:
    core = await _load_cli_core()
    try:
        assert core.tunarr_client is not None
        core.channel_sync_engine = ChannelSyncEngine(
            tunarr_client=core.tunarr_client,
            config_manager=core.config_manager,
        )
        result = await core.channel_sync_engine.sync()
        print(f"Sync complete: {result}")
    finally:
        await _close_cli_core(core)


async def _list_schedules_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core()
    try:
        assert core.state is not None
        versions = await core.state.list_versions(args.channel_id)
        if not versions:
            print(f"No schedule versions found for channel {args.channel_id}.")
            return
        for version in versions:
            print(
                f"v{version['version']}\t{version['status']}\t"
                f"{version['created_at']}\t{version['id']}",
            )
    finally:
        await _close_cli_core(core)


async def _schedule_health_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=False,
        include_tunarr=False,
    )
    try:
        assert core.state is not None
        channel = next(
            (
                item for item in core.config_manager.config().channels
                if item.id == args.channel_id
            ),
            None,
        )
        if channel is None:
            raise RuntimeError(f"Channel not found: {args.channel_id}")

        version = await _resolve_health_version(core.state, args.channel_id, str(args.version))
        meta = await core.state.get_schedule_version_meta(args.channel_id, version)
        if meta is None:
            raise RuntimeError(f"Schedule version not found: {args.channel_id} v{version}")
        timeline = Timeline.from_snapshot(json.loads(str(meta["timeline_json"])))
        health = build_schedule_health(meta, timeline)
        payload = {
            "channel_id": args.channel_id,
            "channel_name": channel.name,
            "health": health,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        metrics = metric_map(health)
        print(
            f"Schedule health for {channel.name} ({args.channel_id}) "
            f"v{health['version']} [{health['status']}]: "
            f"{health['summary']} ({health['level']})",
        )
        print(
            "Metrics: "
            f"content={metrics.get('Content', '0.0%')} "
            f"standby={metrics.get('Standby', '0.0%')} "
            f"ads={metrics.get('Ads', '0.0%')} "
            f"filler={metrics.get('Filler', '0.0%')}",
        )
        print("Issues:")
        for issue in cast(list[str], health["issues"]):
            print(f"- {issue}")
    finally:
        await _close_cli_core(core)


async def _resolve_health_version(
    state: StateManager,
    channel_id: str,
    requested: str,
) -> int:
    if requested != "latest":
        try:
            version = int(requested)
        except ValueError as e:
            raise RuntimeError("--version must be an integer or 'latest'.") from e
        if version <= 0:
            raise RuntimeError("--version must be a positive integer.")
        return version
    versions = await state.list_versions(channel_id)
    if not versions:
        raise RuntimeError(f"No schedule versions found for channel {channel_id}.")
    return int(str(versions[0]["version"]))


async def _backup_data_cli(args: argparse.Namespace) -> None:
    manager = ConfigManager()
    if not manager.exists():
        raise RuntimeError("Config not found: ~/.tunarr/config.yaml")
    config = manager.load()
    core: Core | None = None
    result: BackupResult | None = None
    try:
        core = await _load_cli_core(require_credentials=False, include_tunarr=False)
        retention_count = getattr(args, "retention_count", None)
        min_free_mb = getattr(args, "min_free_mb", None)
        size_multiplier = getattr(args, "size_multiplier", None)
        backup_config = config.backups.model_copy(update={
            "output_dir": str(args.output_dir),
            **(
                {"retention_count": retention_count}
                if retention_count is not None else {}
            ),
            **({"min_free_mb": min_free_mb} if min_free_mb is not None else {}),
            **(
                {"size_multiplier": size_multiplier}
                if size_multiplier is not None else {}
            ),
        })
        result = await create_backup_archive(
            config=config,
            config_path=manager.config_path,
            output_dir=args.output_dir,
            backup_config=backup_config,
            notification_router=core.notification_router,
        )
    except Exception as e:
        if core is None:
            core = await _load_cli_core(require_credentials=False, include_tunarr=False)
        await notify_backup_failed(
            core.notification_router,
            output_dir=args.output_dir,
            error=e,
        )
        raise
    finally:
        if core is not None:
            await _close_cli_core(core)
    assert result is not None
    print(f"Backup written: {result.archive_path}")
    missing = result.manifest.get("missing", [])
    if missing:
        print(f"Missing source file(s): {', '.join(str(item) for item in missing)}")
    if isinstance(result.manifest.get("archive_bytes"), int):
        print(f"Archive size: {result.manifest['archive_bytes']} bytes")


async def _diagnostic_bundle_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=False,
        include_tunarr=False,
        run_db_setup=False,
    )
    try:
        archive_path = await create_diagnostic_bundle(
            config_manager=core.config_manager,
            state=core.state,
            output_dir=str(args.output_dir),
            keep_latest_only=not bool(args.keep_history),
        )
        payload = {
            "archive": str(archive_path),
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        print(f"Diagnostic bundle written: {archive_path}")
    finally:
        await _close_cli_core(core)

async def _recommend_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=False,
        include_tunarr=False,
        run_db_setup=False,
    )
    try:
        assert core.db is not None
        media_repo = MediaRepository(core.db)
        playlist_repo = PlaylistRepository(core.db)
        profile_repo = RecommendationProfileRepository(core.db)
        run_repo = RecommendationRunRepository(core.db)
        core.media_repo = media_repo
        core.playlist_repo = playlist_repo
        core.recommendation_profile_repo = profile_repo
        core.recommendation_run_repo = run_repo
        profiles = dict(BUILT_IN_PROFILES)
        for profile in await profile_repo.list_all():
            profiles[profile.id] = profile
        engine = RecommendationEngine(
            media_repo,
            await playlist_repo.get_recommendation_terms_by_media_id(),
            profiles=profiles,
            external_signals_by_media_id=await build_external_signals(
                await media_repo.get_all_available(),
                ExternalMetadataCacheRepository(core.db),
            ),
            signal_weights=_recommendation_signal_weights(core.config_manager.config().metadata),
        )
        if args.recommend_command in {"diagnostics", "scan"}:
            diagnostics = await engine.diagnostics()
            if args.json:
                print(json.dumps(diagnostics, indent=2, sort_keys=True))
            else:
                _print_recommend_diagnostics(diagnostics)
            return
        if args.recommend_command == "profiles":
            profile_payload = [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "media_types": list(profile.media_types),
                    "description": profile.description,
                    "custom": profile.id not in BUILT_IN_PROFILES,
                }
                for profile in profiles.values()
            ]
            if args.json:
                print(json.dumps(profile_payload, indent=2, sort_keys=True))
            else:
                _print_recommend_profiles(profile_payload)
            return
        if args.recommend_command == "run":
            results = await engine.run(
                str(args.profile),
                limit=max(1, int(args.limit)),
                include_excluded=bool(args.include_excluded),
                language_rule=args.language_rule,
            )
            payload = {
                "profile": args.profile,
                "results": [result.as_dict() for result in results],
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_recommend_results(str(args.profile), results, profiles)
            return
        if args.recommend_command == "compare":
            selected_profiles = [
                item.strip()
                for item in str(args.profiles).split(",")
                if item.strip()
            ][:4]
            if len(selected_profiles) < 2:
                raise RuntimeError("Select at least two profiles to compare.")
            unknown = [profile_id for profile_id in selected_profiles if profile_id not in profiles]
            if unknown:
                raise RuntimeError(f"Unknown recommendation profile(s): {', '.join(unknown)}")
            limit = max(1, min(100, int(args.limit)))
            payload = await _recommendation_compare_payload(
                engine,
                profiles,
                selected_profiles,
                limit,
                args.language_rule,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_recommend_compare(payload)
            return
        if args.recommend_command == "explain":
            result = await engine.explain(
                str(args.item_id),
                str(args.profile),
                language_rule=args.language_rule,
            )
            if result is None:
                raise RuntimeError(
                    f"No recommendation candidate found for item id: {args.item_id}",
                )
            payload = {"profile": args.profile, "result": result.as_dict()}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_recommend_results(str(args.profile), [result], profiles)
            return
        if args.recommend_command == "create-playlist":
            results = await engine.run(
                str(args.profile),
                limit=max(1, int(args.limit)),
                include_excluded=False,
            )
            items: list[PlaylistItem] = []
            for result in results:
                if not result.accepted or result.candidate.media_type not in {"series", "movie"}:
                    continue
                media_type = cast(Literal["series", "movie"], result.candidate.media_type)
                items.append(
                    PlaylistItem(
                        media_type=media_type,
                        media_id=result.candidate.id,
                        title=result.candidate.title,
                        position=len(items),
                    ),
                )
            playlist = await playlist_repo.create(
                name=str(args.name),
                description=f"Generated from recommendation profile {args.profile}.",
                category_id=str(args.category_id),
                channel_scope=str(args.channel_scope),
                tags=[tag.strip() for tag in str(args.tags).split(",") if tag.strip()],
                items=items,
            )
            payload = {
                "playlist_id": playlist.id,
                "name": playlist.name,
                "items": len(items),
                "profile": args.profile,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"Created playlist {playlist.name} ({playlist.id}) "
                    f"with {len(items)} item(s).",
                )
            return
        if args.recommend_command == "builder-dry-run":
            form = {
                "mode": args.mode,
                "builder_mode": args.builder_mode,
                "channel_id": str(args.channel_id).strip(),
                "channel_name": str(args.channel_name).strip(),
                "profile": str(args.profile),
                "themes": str(args.themes).strip(),
                "seed": str(args.seed).strip(),
                "language_rule": str(args.language_rule),
                "per_theme_limit": max(1, min(100, int(args.per_theme_limit))),
                "balance_mode": str(args.balance_mode),
                "max_movies_per_theme": args.max_movies_per_theme,
                "min_series_per_theme": max(1, min(100, int(args.min_series_per_theme))),
                "create_channel": bool(args.create_channel),
                "replace_dayparts": not bool(args.append_dayparts),
                "preview": True,
            }
            plan = await _build_recommendation_plan(_cli_request(core), form)
            if args.json:
                print(json.dumps(plan, indent=2, sort_keys=True))
            else:
                _print_recommendation_plan(plan)
            return
        if args.recommend_command == "builder-apply":
            run = await run_repo.get(str(args.run_id))
            if run is None:
                raise RuntimeError(f"Recommendation run not found: {args.run_id}")
            request_data = run.get("request", {})
            plan = run.get("result", {})
            if not isinstance(request_data, dict) or not isinstance(plan, dict):
                raise RuntimeError(f"Recommendation run is invalid: {args.run_id}")
            channel_id = str(
                request_data.get("channel_id") or plan.get("channel_id") or "",
            ).strip()
            channel = next(
                (
                    item for item in core.config_manager.config().channels
                    if item.id == channel_id
                ),
                None,
            )
            if run.get("status") != "applied" or channel is None:
                channel = await apply_recommendation_plan_to_core(
                    core,
                    playlist_repo,
                    request_data,
                    plan,
                )
                await run_repo.mark_applied(str(args.run_id))
            assert channel is not None
            apply_payload: dict[str, Any] = {
                "run_id": args.run_id,
                "channel_id": channel.id,
                "channel_name": channel.name,
                "applied": True,
            }
            if args.generate_draft:
                await _prepare_generation_core(core)
                assert core.job_manager is not None
                generation_mode = "follow_up" if args.generation_mode == "follow-up" else "fresh"
                job = await core.job_manager.run_generation(
                    channel,
                    generation_mode,
                    parent_version=args.parent_version if generation_mode == "follow_up" else None,
                )
                apply_payload["generation"] = {
                    "job_id": job.id,
                    "status": job.status.value,
                    "version_id": job.schedule_version_id,
                    "stage": job.current_stage,
                }
            if args.json:
                print(json.dumps(apply_payload, indent=2, sort_keys=True))
            else:
                print(
                    f"Applied recommendation run {args.run_id} to "
                    f"{channel.name or channel.id}.",
                )
                if "generation" in apply_payload:
                    generation = cast(dict[str, Any], apply_payload["generation"])
                    print(
                        "Generation "
                        f"{generation['status']}: job={generation['job_id']} "
                        f"version_id={generation.get('version_id') or '-'}",
                    )
            return
        raise RuntimeError(f"Unknown recommend command: {args.recommend_command}")
    finally:
        await _close_cli_core(core)


async def _metadata_audit_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=False,
        include_tunarr=False,
    )
    try:
        assert core.db is not None
        media_repo = MediaRepository(core.db)
        audit = build_metadata_audit(await media_repo.get_all_available())
        if args.json:
            print(json.dumps(audit, indent=2, sort_keys=True))
            return
        _print_metadata_audit(audit)
    finally:
        await _close_cli_core(core)


async def _metadata_refresh_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=False,
        include_tunarr=False,
    )
    try:
        assert core.db is not None
        media_repo = MediaRepository(core.db)
        cache = ExternalMetadataCacheRepository(core.db)
        service = MetadataEnrichmentService(
            cache=cache,
            config=core.config_manager.config().metadata,
        )
        summary = await service.refresh(
            await media_repo.get_all_available(),
            dry_run=not bool(args.apply),
            limit=args.limit,
        )
        payload = {
            "dry_run": not bool(args.apply),
            "summary": summary.as_dict(),
            "cache": await cache.stats(),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        _print_metadata_refresh(payload)
    finally:
        await _close_cli_core(core)


def _print_recommend_diagnostics(diagnostics: dict[str, Any]) -> None:
    print(f"Recommendation diagnostics: {diagnostics['total_available']} available items")
    print("Media types:")
    by_type = cast(dict[str, int], diagnostics["by_type"])
    for media_type, count in sorted(by_type.items()):
        print(f"- {media_type}: {count}")
    print("Metadata coverage:")
    coverage = cast(dict[str, dict[str, float | int]], diagnostics["metadata_coverage"])
    for field, stats in sorted(coverage.items()):
        print(f"- {field}: {stats['count']} ({stats['percent']}%)")
    print("Profiles:")
    profiles = cast(list[dict[str, str]], diagnostics["profiles"])
    for profile in profiles:
        print(f"- {profile['id']}: {profile['name']}")


def _print_recommend_results(
    profile_id: str,
    results: list[Any],
    profiles: dict[str, Any] | None = None,
) -> None:
    profile = (profiles or BUILT_IN_PROFILES)[profile_id]
    print(f"Recommendations for {profile.name} ({profile.id})")
    if not results:
        print("No matching recommendations found.")
        return
    for index, result in enumerate(results, start=1):
        payload = result.as_dict()
        status = "accepted" if payload["accepted"] else "excluded"
        runtime = payload["average_runtime_minutes"]
        runtime_text = f"{runtime}m" if runtime is not None else "unknown runtime"
        print(
            f"{index}. {payload['title']} [{payload['media_type']}] "
            f"{payload['score']}/100 {status} - {runtime_text}, "
            f"{payload['item_count']} item(s)",
        )
        for reason in payload["reasons"]:
            print(f"   + {reason}")
        for warning in payload["warnings"]:
            print(f"   ! {warning}")
        for exclusion in payload["exclusions"]:
            print(f"   - {exclusion}")


async def _recommendation_compare_payload(
    engine: RecommendationEngine,
    profiles: dict[str, Any],
    selected_profiles: list[str],
    limit: int,
    language_rule: str | None,
) -> dict[str, Any]:
    accepted_sets: dict[str, set[str]] = {}
    summaries: list[dict[str, Any]] = []
    for profile_id in selected_profiles:
        results = await engine.run(
            profile_id,
            limit=max(limit, 100),
            language_rule=language_rule,
        )
        accepted = [result for result in results if result.accepted][:limit]
        accepted_sets[profile_id] = {
            f"{result.candidate.media_type}:{result.candidate.id}"
            for result in accepted
        }
        scores = [result.score for result in accepted]
        summaries.append({
            "profile_id": profile_id,
            "profile_name": profiles[profile_id].name,
            "count": len(accepted),
            "average_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "top_items": [result.as_dict() for result in accepted[:10]],
            "warnings": sorted({
                warning for result in accepted for warning in result.warnings
            })[:10],
        })
    overlaps: list[dict[str, Any]] = []
    for left_index, left in enumerate(selected_profiles):
        for right in selected_profiles[left_index + 1:]:
            shared = accepted_sets[left] & accepted_sets[right]
            overlaps.append({
                "left_profile_id": left,
                "right_profile_id": right,
                "left_profile_name": profiles[left].name,
                "right_profile_name": profiles[right].name,
                "count": len(shared),
                "percent": round(
                    len(shared)
                    / max(1, min(len(accepted_sets[left]), len(accepted_sets[right])))
                    * 100,
                    1,
                ),
            })
    return {
        "profiles": selected_profiles,
        "limit": limit,
        "language_rule": language_rule or "profile_default",
        "summaries": summaries,
        "overlaps": overlaps,
    }


def _print_recommend_compare(payload: dict[str, Any]) -> None:
    print(
        "Recommendation profile comparison: "
        f"{', '.join(cast(list[str], payload['profiles']))}",
    )
    for summary in cast(list[dict[str, Any]], payload["summaries"]):
        print(
            f"- {summary['profile_name']} ({summary['profile_id']}): "
            f"{summary['count']} candidates, avg score {summary['average_score']}",
        )
        for item in cast(list[dict[str, Any]], summary["top_items"])[:5]:
            print(f"  {item['score']}/100 {item['media_type']} {item['title']}")
    overlaps = cast(list[dict[str, Any]], payload["overlaps"])
    if overlaps:
        print("Overlap:")
        for overlap in overlaps:
            print(
                f"- {overlap['left_profile_name']} / {overlap['right_profile_name']}: "
                f"{overlap['count']} shared ({overlap['percent']}%)",
            )


def _cli_request(core: Core) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(core=core)))


def _print_recommendation_plan(plan: dict[str, Any]) -> None:
    print(f"{plan.get('title', 'Recommendation plan')}")
    print(
        f"Mode={plan.get('mode', '-')} channel={plan.get('channel_name', '-')} "
        f"profile={plan.get('profile_name', plan.get('profile', '-'))}",
    )
    for daypart in cast(list[dict[str, Any]], plan.get("dayparts", [])):
        items = cast(list[dict[str, Any]], daypart.get("items", []))
        warnings = cast(list[str], daypart.get("warnings", []))
        print(
            f"- {daypart.get('name', '-')} "
            f"{daypart.get('start_time', '-')}->{daypart.get('end_time', '-')} "
            f"{daypart.get('profile_name', daypart.get('profile', '-'))}: "
            f"{len(items)} item(s)",
        )
        for warning in warnings:
            print(f"  warning: {warning}")
        for item in items[:10]:
            print(
                f"  {item.get('media_type', '-')} "
                f"{item.get('score', '-')} {item.get('title', '-')}",
            )


def _recommendation_signal_weights(metadata_config: Any) -> dict[str, int]:
    return {
        "activity": int(getattr(metadata_config, "jellystat_activity_weight", 10)),
        "completion": int(getattr(metadata_config, "jellystat_completion_weight", 8)),
        "trend": int(getattr(metadata_config, "jellystat_trend_weight", 8)),
        "genre_trend": int(getattr(metadata_config, "jellystat_genre_trend_weight", 6)),
        "underused": int(getattr(metadata_config, "jellystat_underused_weight", 6)),
        "stale": int(getattr(metadata_config, "jellystat_stale_weight", 4)),
    }


def _print_recommend_profiles(profiles: list[dict[str, Any]]) -> None:
    print("Recommendation profiles:")
    for profile in profiles:
        marker = "custom" if profile["custom"] else "built-in"
        types = ", ".join(cast(list[str], profile["media_types"]))
        print(f"- {profile['id']}: {profile['name']} ({marker}, {types})")


def _print_metadata_audit(audit: dict[str, Any]) -> None:
    print(f"Metadata provider-ID audit: {audit['total']} movie/series records")
    by_type = cast(dict[str, dict[str, Any]], audit["by_type"])
    for media_type, data in by_type.items():
        print(f"{media_type}: {data['total']}")
        providers = cast(dict[str, dict[str, float | int]], data["providers"])
        for provider, stats in providers.items():
            print(
                f"- {provider}: {stats['count']}/{data['total']} "
                f"({stats['percent']}%), missing {stats['missing']}",
            )


def _print_metadata_refresh(payload: dict[str, Any]) -> None:
    mode = "dry-run" if payload["dry_run"] else "apply"
    summary = cast(dict[str, Any], payload["summary"])
    print(f"Metadata refresh ({mode}):")
    print(
        f"candidates={summary['candidates']} cached={summary['cached']} "
        f"missing={summary['missing']} expired={summary['expired']} "
        f"fetched={summary['fetched']} skipped={summary['skipped']}",
    )
    providers = summary.get("rate_limited_providers", [])
    if summary.get("rate_limited"):
        print(
            "Rate limited: "
            f"{summary['rate_limited']} provider event(s)"
            f" ({', '.join(providers)})",
        )
    provider_statuses = cast(dict[str, Any], summary.get("provider_statuses", {}))
    jellystat_statuses = provider_statuses.get("jellystat")
    if isinstance(jellystat_statuses, dict):
        print("Jellystat status:")
        for name, status in sorted(jellystat_statuses.items()):
            if isinstance(status, dict):
                state = "OK" if status.get("ok") else f"HTTP {status.get('status_code', 'error')}"
                rows = status.get("rows", 0)
                print(f"- {name}: {state}, rows={rows}")
    cache = cast(dict[str, dict[str, int]], payload["cache"])
    if cache:
        print("Cache:")
        for provider, stats in sorted(cache.items()):
            print(f"- {provider}: fresh={stats['fresh']} expired={stats['expired']}")


async def _notify_test_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(require_credentials=False, include_tunarr=False)
    try:
        assert core.notification_router is not None
        results = await core.notification_router.send(NotificationMessage(
            event_type=args.event_type,
            title=args.title,
            message=args.message,
            severity="info",
            details={"source": "cli"},
        ))
        payload = {"event_type": args.event_type, "results": results}
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            if not results:
                print("No notification routes matched or notifications are disabled.")
            for result in results:
                print(
                    f"{result['provider']}: {result['status']} "
                    f"event={result['event_type']}"
                )
    finally:
        await _close_cli_core(core)


async def _check_schedule_expiry_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core(
        require_credentials=bool(args.auto),
        include_tunarr=bool(args.auto),
    )
    try:
        assert core.state is not None
        if args.auto:
            await _prepare_generation_core(core)
        result = await check_schedule_expiry(
            config=core.config_manager.config(),
            state=core.state,
            notification_router=core.notification_router,
            warning_hours=args.warning_hours,
            job_manager=core.job_manager if args.auto else None,
            tunarr_client=core.tunarr_client if args.auto else None,
            automatic=bool(args.auto),
        )
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return
        print(
            "Schedule expiry check: "
            f"{len(result['expiring'])} expiring, "
            f"{len(result['missing_follow_up'])} missing follow-up, "
            f"{len(result['automatic_actions'])} automatic action(s)",
        )
    finally:
        await _close_cli_core(core)


async def _prepare_generation_core(core: Core) -> None:
    config = core.config_manager.config()
    assert core.state is not None
    assert core.db is not None
    core.media_repo = MediaRepository(core.db)
    core.playlist_repo = PlaylistRepository(core.db)
    core.plugin_loader = PluginLoader(
        plugin_dirs=[os.path.expanduser(d) for d in config.plugins.directories],
        disabled=config.plugins.disabled,
    )
    core.plugin_loader.discover()
    core.pipeline_orchestrator = PipelineOrchestrator(
        plugin_loader=core.plugin_loader,
        checkpoint_manager=core.checkpoint_manager,
        event_bus=core.event_bus,
        state=core.state,
        media_repo=core.media_repo,
        playlist_repo=core.playlist_repo,
        tunarr_client=core.tunarr_client,
        metrics=core.metrics,
        app_config=config,
    )
    core.job_manager = JobManager(
        state=core.state,
        orchestrator=core.pipeline_orchestrator,
        checkpoint=core.checkpoint_manager,
        event_bus=core.event_bus,
        notification_router=core.notification_router,
    )


async def _generate_schedule_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core()
    try:
        config = core.config_manager.config()
        channel = next((c for c in config.channels if c.id == args.channel_id), None)
        if channel is None:
            raise RuntimeError(f"Channel not found: {args.channel_id}")

        assert core.state is not None
        assert core.tunarr_client is not None
        assert core.db is not None

        await _prepare_generation_core(core)
        assert core.job_manager is not None

        generation_mode = "follow_up" if args.mode == "follow-up" else "fresh"
        job = await core.job_manager.run_generation(
            channel,
            generation_mode,
            parent_version=args.parent_version if generation_mode == "follow_up" else None,
        )
        print(
            f"Generation {job.status.value}: channel={args.channel_id} "
            f"job={job.id} version_id={job.schedule_version_id or '-'} "
            f"stage={job.current_stage}"
        )
        if job.error_message:
            raise RuntimeError(job.error_message)
    finally:
        await _close_cli_core(core)


async def _upload_schedule_cli(args: argparse.Namespace) -> None:
    core = await _load_cli_core()
    try:
        assert core.state is not None
        assert core.tunarr_client is not None
        meta = await core.state.get_schedule_version_meta(args.channel_id, args.version)
        if meta is None:
            raise RuntimeError(f"Schedule version not found: {args.channel_id} v{args.version}")
        channel = next(
            (
                item for item in core.config_manager.config().channels
                if item.id == args.channel_id
            ),
            None,
        )
        if channel is None:
            raise RuntimeError(f"Channel not found: {args.channel_id}")

        timeline = Timeline.from_snapshot(json.loads(str(meta["timeline_json"])))
        schedule_data = TunarrUploader()._convert_timeline(timeline)
        if args.dump_payload:
            print(json.dumps(schedule_data, indent=2, sort_keys=True))
        if args.dump_tunarr_payload or args.dry_run:
            print(
                json.dumps(
                    {"schedule": _schedule_payload(schedule_data["schedule"])},
                    indent=2,
                    sort_keys=True,
                ),
            )
        if args.dry_run:
            print("Dry run complete; upload skipped.")
            return

        try:
            if args.time_compat:
                schedule, generated = await core.tunarr_client.generate_schedule(
                    args.channel_id,
                    schedule_data,
                )
                if args.dump_generated:
                    print(json.dumps(generated, indent=2, sort_keys=True))
                await core.tunarr_client.persist_time_schedule_with_fallback(
                    args.channel_id,
                    schedule,
                    generated,
                )
            elif args.dump_generated:
                schedule, generated = await core.tunarr_client.generate_schedule(
                    args.channel_id,
                    schedule_data,
                )
                print(json.dumps(generated, indent=2, sort_keys=True))
                await core.tunarr_client.persist_generated_schedule(
                    args.channel_id,
                    schedule,
                    generated,
                )
            else:
                await core.tunarr_client.upload_timeline(
                    args.channel_id,
                    timeline,
                    station_id_custom_show_id=(
                        channel.continuity.station_id_custom_show_id
                    ),
                    bumper_custom_show_id=channel.continuity.bumper_custom_show_id,
                )
        except httpx.HTTPStatusError as e:
            body = e.response.text
            await core.state.record_upload_attempt(
                args.channel_id,
                args.version,
                "failed",
                f"Tunarr rejected upload ({e.response.status_code}): {body}",
                {"status_code": e.response.status_code},
            )
            core.metrics.record_upload(args.channel_id, "failed")
            if core.notification_router is not None:
                await core.notification_router.send(NotificationMessage(
                    event_type="upload_failed",
                    title=f"Upload failed for {channel.name or args.channel_id}",
                    message=f"Tunarr rejected schedule version {args.version}: {body}",
                    severity="danger",
                    channel_id=args.channel_id,
                    details={
                        "version": args.version,
                        "status_code": e.response.status_code,
                    },
                ))
            raise RuntimeError(
                f"Tunarr rejected upload ({e.response.status_code}): {body}",
            ) from e
        await core.state.set_schedule_status(args.channel_id, args.version, "uploaded")
        upload_details = {}
        if "schedule_data" in locals():
            upload_details = {"mode": "manual"}
        await core.state.record_upload_attempt(
            args.channel_id,
            args.version,
            "success",
            "Uploaded schedule version.",
            upload_details,
        )
        core.metrics.record_upload(args.channel_id, "success")
        if core.notification_router is not None:
            await core.notification_router.send(NotificationMessage(
                event_type="upload_succeeded",
                title=f"Upload succeeded for {channel.name or args.channel_id}",
                message=f"Uploaded schedule version {args.version}.",
                severity="success",
                channel_id=args.channel_id,
                details={"version": args.version},
            ))
        print(f"Uploaded schedule version {args.version} for channel {args.channel_id}.")
    finally:
        await _close_cli_core(core)


def main() -> None:
    cli_commands = {
        "sync-channels",
        "generate-schedule",
        "upload-schedule",
        "list-schedules",
        "schedule-health",
        "backup-data",
        "diagnostic-bundle",
        "recommend",
        "metadata-audit",
        "metadata-refresh",
        "notify-test",
        "check-schedule-expiry",
    }
    if len(sys.argv) > 1 and sys.argv[1] in cli_commands:
        run_cli(sys.argv[1:])
    else:
        asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
