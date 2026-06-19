from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tunarr_autoscheduler.core.backups import BackupSafetyError, create_backup_archive
from tunarr_autoscheduler.core.config import ConfigManager
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.main import (
    _backup_data_cli,
    _cli_parser,
    _diagnostic_bundle_cli,
    _schedule_health_cli,
)
from tunarr_autoscheduler.models.blocks import EpisodeBlock
from tunarr_autoscheduler.models.config import AppConfig, BackupConfig, ChannelConfig


def test_upload_schedule_cli_defaults_to_manual_path() -> None:
    args = _cli_parser().parse_args(["upload-schedule", "ch1", "12"])

    assert args.command == "upload-schedule"
    assert args.channel_id == "ch1"
    assert args.version == 12
    assert args.time_compat is False


def test_upload_schedule_cli_can_enable_time_compatibility_path() -> None:
    args = _cli_parser().parse_args([
        "upload-schedule",
        "ch1",
        "12",
        "--time-compat",
        "--dump-generated",
    ])

    assert args.time_compat is True
    assert args.dump_generated is True


def test_schedule_health_cli_parser_defaults_to_latest() -> None:
    args = _cli_parser().parse_args(["schedule-health", "ch1"])

    assert args.command == "schedule-health"
    assert args.channel_id == "ch1"
    assert args.version == "latest"
    assert args.json is False


def test_backup_data_cli_parser_defaults_to_backups_dir() -> None:
    args = _cli_parser().parse_args(["backup-data"])

    assert args.command == "backup-data"
    assert args.output_dir == "~/.tunarr/backups"
    assert args.retention_count is None
    assert args.min_free_mb is None


def test_backup_data_cli_parser_accepts_safeguards() -> None:
    args = _cli_parser().parse_args([
        "backup-data",
        "--retention-count",
        "3",
        "--min-free-mb",
        "2048",
        "--size-multiplier",
        "4",
    ])

    assert args.retention_count == 3
    assert args.min_free_mb == 2048
    assert args.size_multiplier == 4


def test_diagnostic_bundle_cli_parser_defaults_to_single_latest_bundle() -> None:
    args = _cli_parser().parse_args(["diagnostic-bundle", "--json"])

    assert args.command == "diagnostic-bundle"
    assert args.output_dir == "~/.tunarr/diagnostics"
    assert args.keep_history is False
    assert args.json is True


def test_recommend_diagnostics_cli_parser() -> None:
    args = _cli_parser().parse_args(["recommend", "diagnostics", "--json"])

    assert args.command == "recommend"
    assert args.recommend_command == "diagnostics"
    assert args.json is True


def test_recommend_run_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "run",
        "--profile",
        "anime-series",
        "--language-rule",
        "english_audio",
        "--limit",
        "5",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "run"
    assert args.profile == "anime-series"
    assert args.language_rule == "english_audio"
    assert args.limit == 5


def test_recommend_profiles_cli_parser() -> None:
    args = _cli_parser().parse_args(["recommend", "profiles", "--json"])

    assert args.command == "recommend"
    assert args.recommend_command == "profiles"
    assert args.json is True


def test_recommend_scan_cli_parser() -> None:
    args = _cli_parser().parse_args(["recommend", "scan", "--json"])

    assert args.command == "recommend"
    assert args.recommend_command == "scan"
    assert args.json is True


def test_recommend_explain_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "explain",
        "--profile",
        "prime-time-movies",
        "--item-id",
        "movie-1",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "explain"
    assert args.profile == "prime-time-movies"
    assert args.item_id == "movie-1"


def test_recommend_create_playlist_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "create-playlist",
        "--profile",
        "movie-channel-pool",
        "--name",
        "Movies",
        "--limit",
        "10",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "create-playlist"
    assert args.profile == "movie-channel-pool"
    assert args.name == "Movies"
    assert args.limit == 10


def test_recommend_builder_dry_run_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "builder-dry-run",
        "--mode",
        "channel",
        "--builder-mode",
        "improve",
        "--channel-id",
        "ch1",
        "--themes",
        "Sci-Fi,Mystery",
        "--seed",
        "Star Trek",
        "--balance-mode",
        "series_heavy",
        "--max-movies-per-theme",
        "1",
        "--json",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "builder-dry-run"
    assert args.builder_mode == "improve"
    assert args.channel_id == "ch1"
    assert args.themes == "Sci-Fi,Mystery"
    assert args.balance_mode == "series_heavy"
    assert args.max_movies_per_theme == 1
    assert args.json is True


def test_recommend_builder_apply_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "builder-apply",
        "--run-id",
        "run-1",
        "--generate-draft",
        "--generation-mode",
        "follow-up",
        "--parent-version",
        "4",
        "--json",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "builder-apply"
    assert args.run_id == "run-1"
    assert args.generate_draft is True
    assert args.generation_mode == "follow-up"
    assert args.parent_version == 4


def test_metadata_audit_cli_parser() -> None:
    args = _cli_parser().parse_args(["metadata-audit", "--json"])

    assert args.command == "metadata-audit"
    assert args.json is True


def test_metadata_refresh_cli_parser_defaults_to_dry_run() -> None:
    args = _cli_parser().parse_args(["metadata-refresh", "--limit", "10"])

    assert args.command == "metadata-refresh"
    assert args.apply is False
    assert args.limit == 10


def test_metadata_refresh_cli_parser_can_apply() -> None:
    args = _cli_parser().parse_args(["metadata-refresh", "--apply", "--json"])

    assert args.apply is True
    assert args.json is True


def test_notify_test_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "notify-test",
        "--event-type",
        "upload_failed",
        "--title",
        "Upload failed",
        "--message",
        "Nope",
        "--json",
    ])

    assert args.command == "notify-test"
    assert args.event_type == "upload_failed"
    assert args.title == "Upload failed"
    assert args.message == "Nope"
    assert args.json is True


def test_recommend_run_cli_accepts_custom_profile_id() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "run",
        "--profile",
        "custom-profile",
    ])

    assert args.profile == "custom-profile"


def test_recommend_compare_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "recommend",
        "compare",
        "--profiles",
        "morning-sitcoms,series-marathon",
        "--limit",
        "10",
        "--json",
    ])

    assert args.command == "recommend"
    assert args.recommend_command == "compare"
    assert args.profiles == "morning-sitcoms,series-marathon"
    assert args.limit == 10
    assert args.json is True


async def test_backup_data_cli_creates_zip_archive(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tunarr_dir = tmp_path / ".tunarr"
    tunarr_dir.mkdir()
    manager = ConfigManager()
    manager._config = AppConfig(channels=[ChannelConfig(id="ch1", name="CLI Channel")])
    manager.save()
    db_path = tunarr_dir / "scheduler.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO sample DEFAULT VALUES")
    conn.commit()
    conn.close()

    output_dir = tmp_path / "backups"
    await _backup_data_cli(argparse.Namespace(output_dir=str(output_dir)))

    output = capsys.readouterr().out
    assert "Backup written:" in output
    archives = list(output_dir.glob("tunarr-autoscheduler-backup-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as zf:
        assert "config.yaml" in zf.namelist()
        assert "scheduler.db" in zf.namelist()
        assert "manifest.json" in zf.namelist()


async def test_backup_archive_refuses_when_disk_space_is_too_low(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "scheduler.db"
    config_path.write_text("jellyfin: {}\n", encoding="utf-8")
    db_path.write_bytes(b"db")
    config = AppConfig()
    config.database.url = f"sqlite+aiosqlite:///{db_path}"

    monkeypatch.setattr(
        "tunarr_autoscheduler.core.backups.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024),
    )

    with pytest.raises(BackupSafetyError):
        await create_backup_archive(
            config=config,
            config_path=config_path,
            output_dir=tmp_path / "backups",
            backup_config=BackupConfig(min_free_mb=2, retention_count=1),
        )


async def test_backup_archive_applies_retention(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "scheduler.db"
    config_path.write_text("jellyfin: {}\n", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    output_dir = tmp_path / "backups"
    output_dir.mkdir()
    old = output_dir / "tunarr-autoscheduler-backup-old.zip"
    old.write_text("old", encoding="utf-8")
    config = AppConfig()
    config.database.url = f"sqlite+aiosqlite:///{db_path}"

    await create_backup_archive(
        config=config,
        config_path=config_path,
        output_dir=output_dir,
        backup_config=BackupConfig(min_free_mb=1, retention_count=1),
    )

    archives = list(output_dir.glob("tunarr-autoscheduler-backup-*.zip"))
    assert len(archives) == 1
    assert archives[0] != old


async def test_diagnostic_bundle_cli_creates_redacted_single_latest_archive(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tunarr_dir = tmp_path / ".tunarr"
    tunarr_dir.mkdir()
    manager = ConfigManager()
    manager._config = AppConfig(channels=[ChannelConfig(id="ch1", name="CLI Channel")])
    manager._config.jellyfin.api_key = "secret-key"
    manager.save()
    db_path = tunarr_dir / "scheduler.db"
    db = Database(str(db_path))
    await db.connect()
    await run_migrations(db)
    await db.disconnect()
    output_dir = tmp_path / "diagnostics"
    old = output_dir / "tunarr-autoscheduler-diagnostics-old.zip"
    output_dir.mkdir()
    old.write_text("old", encoding="utf-8")

    await _diagnostic_bundle_cli(argparse.Namespace(
        output_dir=str(output_dir),
        keep_history=False,
        json=False,
    ))

    output = capsys.readouterr().out
    assert "Diagnostic bundle written:" in output
    archives = list(output_dir.glob("tunarr-autoscheduler-diagnostics-*.zip"))
    assert len(archives) == 1
    assert archives[0] != old
    with zipfile.ZipFile(archives[0]) as zf:
        assert "config.redacted.json" in zf.namelist()
        assert "schedule_health.json" in zf.namelist()
        assert "upload_attempts.json" in zf.namelist()
        config_text = zf.read("config.redacted.json").decode()
        assert "secret-key" not in config_text
        assert "***REDACTED***" in config_text


def test_check_schedule_expiry_cli_parser() -> None:
    args = _cli_parser().parse_args([
        "check-schedule-expiry",
        "--warning-hours",
        "24",
        "--json",
    ])

    assert args.command == "check-schedule-expiry"
    assert args.warning_hours == 24
    assert args.json is True
    assert args.auto is False


def test_check_schedule_expiry_cli_parser_accepts_auto() -> None:
    args = _cli_parser().parse_args([
        "check-schedule-expiry",
        "--auto",
    ])

    assert args.command == "check-schedule-expiry"
    assert args.auto is True


async def test_schedule_health_cli_reads_saved_schedule(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tunarr_dir = tmp_path / ".tunarr"
    tunarr_dir.mkdir()
    manager = ConfigManager()
    manager._config = AppConfig(channels=[ChannelConfig(id="ch1", name="CLI Channel")])
    manager.save()
    db_path = tunarr_dir / "scheduler.db"
    db = Database(str(db_path))
    await db.connect()
    await run_migrations(db)
    state = StateManager(db)
    timeline = Timeline()
    timeline.insert(EpisodeBlock(
        start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
        duration=timedelta(minutes=30),
        episode_id="episode-1",
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=1800,
    ))
    await state.save_schedule_version(
        "ch1",
        1,
        json.dumps(timeline.snapshot()),
        status="draft",
    )
    await db.disconnect()

    await _schedule_health_cli(argparse.Namespace(
        channel_id="ch1",
        version="latest",
        json=False,
    ))

    output = capsys.readouterr().out
    assert "Schedule health for CLI Channel (ch1) v1 [draft]" in output
    assert "Looks healthy" in output
    assert "content=100.0%" in output
