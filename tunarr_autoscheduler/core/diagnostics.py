from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tunarr_autoscheduler.core.config import ConfigManager
from tunarr_autoscheduler.core.schedule_health import build_schedule_health
from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.config import AppConfig

SECRET_KEYS = {
    "api_key",
    "api_keys",
    "password",
    "password_hash",
    "session_secret",
    "token",
    "bot_token",
    "webhook_url",
    "headers",
}


async def create_diagnostic_bundle(
    *,
    config_manager: ConfigManager,
    state: StateManager | None,
    output_dir: str | Path = "~/.tunarr/diagnostics",
    keep_latest_only: bool = True,
) -> Path:
    config = config_manager.config()
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    if keep_latest_only:
        for old_bundle in destination.glob("tunarr-autoscheduler-diagnostics-*.zip"):
            old_bundle.unlink(missing_ok=True)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_path = destination / f"tunarr-autoscheduler-diagnostics-{timestamp}.zip"
    manifest: dict[str, Any] = {
        "created_at": datetime.now(tz=UTC).isoformat(),
        "schema": "tunarr_autoscheduler.diagnostics.v1",
        "files": [],
    }
    with tempfile.TemporaryDirectory(prefix="tunarr-autoscheduler-diagnostics-") as temp_dir:
        temp_root = Path(temp_dir)
        _write_json(temp_root / "runtime.json", _runtime_info(config))
        _write_json(temp_root / "config.redacted.json", _redact(config.model_dump(mode="json")))
        _write_json(
            temp_root / "schedule_health.json",
            await _schedule_health_payload(config, state),
        )
        _write_json(
            temp_root / "upload_attempts.json",
            await _upload_attempts_payload(config, state),
        )
        _write_log_tail(config, temp_root / "scheduler.log.tail")
        db_path = _database_path(config)
        if db_path.exists():
            _backup_sqlite_database(db_path, temp_root / "scheduler.db")
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(temp_root.iterdir()):
                zf.write(path, path.name)
                manifest["files"].append(path.name)
            manifest["files"].append("manifest.json")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return archive_path


def _runtime_info(config: AppConfig) -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "created_at": datetime.now(tz=UTC).isoformat(),
        "timezone": config.timezone,
        "database_url": _redact_value(config.database.url),
        "log_file": config.server.log_file,
        "channels": [
            {
                "id": channel.id,
                "name": channel.name,
                "profile": channel.channel_profile,
                "scheduling_enabled": channel.scheduling_enabled,
                "public_epg_enabled": channel.public_epg_enabled,
            }
            for channel in config.channels
        ],
    }


async def _schedule_health_payload(
    config: AppConfig,
    state: StateManager | None,
) -> list[dict[str, Any]]:
    if state is None:
        return []
    payload: list[dict[str, Any]] = []
    for channel in config.channels:
        versions = await state.list_versions(channel.id)
        for version in versions[:5]:
            version_number = int(str(version["version"]))
            timeline_json = await state.get_schedule_version(channel.id, version_number)
            if not timeline_json:
                continue
            try:
                timeline = Timeline.from_snapshot(json.loads(timeline_json))
                health = build_schedule_health(version, timeline)
            except (TypeError, ValueError, json.JSONDecodeError):
                health = {
                    "level": "danger",
                    "summary": "Schedule health could not be read",
                    "issues": ["Timeline data is invalid."],
                }
            payload.append({
                "channel_id": channel.id,
                "channel_name": channel.name,
                "version": version_number,
                "status": version.get("status"),
                "created_at": version.get("created_at"),
                "health": health,
            })
    return payload


async def _upload_attempts_payload(
    config: AppConfig,
    state: StateManager | None,
) -> list[dict[str, Any]]:
    if state is None:
        return []
    attempts: list[dict[str, Any]] = []
    for channel in config.channels:
        attempts.extend(await state.list_upload_attempts(channel.id, limit=20))
    attempts.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return attempts[:100]


def _write_log_tail(config: AppConfig, destination: Path, max_bytes: int = 200_000) -> None:
    log_path = Path(config.server.log_file).expanduser()
    if not log_path.exists():
        destination.write_text("Log file not found.\n", encoding="utf-8")
        return
    with log_path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - max_bytes))
        data = source.read()
    destination.write_text(
        data.decode("utf-8", errors="replace"),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in SECRET_KEYS or any(secret in key.lower() for secret in SECRET_KEYS):
                redacted[key] = "***REDACTED***" if item else ""
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_value(value: str) -> str:
    if "@" in value and "://" in value:
        prefix, suffix = value.split("://", 1)
        if "@" in suffix:
            return f"{prefix}://***REDACTED***@{suffix.split('@', 1)[1]}"
    return value


def _database_path(config: AppConfig) -> Path:
    return Path(config.database.url.replace("sqlite+aiosqlite:///", "")).expanduser()


def _backup_sqlite_database(source: Path, destination: Path) -> None:
    try:
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dest_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
    except sqlite3.Error:
        shutil.copy2(source, destination)
