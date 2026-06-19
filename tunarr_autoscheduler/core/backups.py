from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    send_notification,
)
from tunarr_autoscheduler.models.config import AppConfig, BackupConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    manifest: dict[str, Any]


class BackupSafetyError(RuntimeError):
    pass


async def create_backup_archive(
    *,
    config: AppConfig,
    config_path: str | Path,
    output_dir: str | Path | None = None,
    backup_config: BackupConfig | None = None,
    notification_router: NotificationRouter | None = None,
) -> BackupResult:
    backup_config = backup_config or config.backups
    config_file = Path(config_path).expanduser()
    db_path = Path(config.database.url.replace("sqlite+aiosqlite:///", "")).expanduser()
    destination = Path(output_dir or backup_config.output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)

    sources = [path for path in (config_file, db_path) if path.exists()]
    estimated_source_bytes = sum(path.stat().st_size for path in sources)
    required_bytes = _required_free_bytes(
        estimated_source_bytes,
        min_free_mb=backup_config.min_free_mb,
        multiplier=backup_config.size_multiplier,
    )
    free_bytes = shutil.disk_usage(destination).free
    if free_bytes < required_bytes:
        raise BackupSafetyError(
            "Not enough free space for backup: "
            f"{_format_bytes(free_bytes)} available, "
            f"{_format_bytes(required_bytes)} required.",
        )

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_path = destination / f"tunarr-autoscheduler-backup-{timestamp}.zip"
    manifest: dict[str, Any] = {
        "schema": "tunarr_autoscheduler.backup.v1",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "config_path": str(config_file),
        "database_path": str(db_path),
        "output_dir": str(destination),
        "estimated_source_bytes": estimated_source_bytes,
        "free_bytes_before": free_bytes,
        "required_free_bytes": required_bytes,
        "files": [],
        "missing": [],
        "safeguards": {
            "min_free_mb": backup_config.min_free_mb,
            "size_multiplier": backup_config.size_multiplier,
            "retention_count": backup_config.retention_count,
        },
    }
    files = manifest["files"]
    missing = manifest["missing"]
    assert isinstance(files, list)
    assert isinstance(missing, list)

    with tempfile.TemporaryDirectory(prefix="tunarr-autoscheduler-backup-") as temp_dir:
        temp_root = Path(temp_dir)
        db_backup_path = temp_root / "scheduler.db"
        if db_path.exists():
            _backup_sqlite_database(db_path, db_backup_path)
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            if config_file.exists():
                zf.write(config_file, "config.yaml")
                files.append("config.yaml")
            else:
                missing.append(str(config_file))
            if db_backup_path.exists():
                zf.write(db_backup_path, "scheduler.db")
                files.append("scheduler.db")
            else:
                missing.append(str(db_path))
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    manifest["archive_bytes"] = archive_path.stat().st_size
    _apply_backup_retention(destination, backup_config.retention_count)
    await send_notification(
        notification_router,
        NotificationMessage(
            event_type="backup_succeeded",
            title="Scheduler backup completed",
            message=f"Backup written: {archive_path}",
            severity="success",
            details={
                "archive": str(archive_path),
                "archive_bytes": manifest["archive_bytes"],
                "missing": missing,
            },
        ),
    )
    return BackupResult(archive_path=archive_path, manifest=manifest)


async def notify_backup_failed(
    notification_router: NotificationRouter | None,
    *,
    output_dir: str | Path,
    error: Exception,
) -> None:
    await send_notification(
        notification_router,
        NotificationMessage(
            event_type="backup_failed",
            title="Scheduler backup failed",
            message=str(error),
            severity="danger",
            details={"output_dir": str(output_dir), "error": type(error).__name__},
        ),
    )


class BackupMonitorEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        config_path: str | Path,
        notification_router: NotificationRouter | None = None,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._notification_router = notification_router
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if not self._config.backups.enabled:
            logger.info("Scheduled backups are disabled")
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

    async def run_once(self) -> BackupResult:
        return await create_backup_archive(
            config=self._config,
            config_path=self._config_path,
            backup_config=self._config.backups,
            notification_router=self._notification_router,
        )

    async def _run_loop(self) -> None:
        interval = max(1, self._config.backups.interval_hours) * 3600
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception("Scheduled backup failed")
                await notify_backup_failed(
                    self._notification_router,
                    output_dir=self._config.backups.output_dir,
                    error=exc,
                )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue


def _required_free_bytes(source_bytes: int, *, min_free_mb: int, multiplier: int) -> int:
    min_free_bytes = max(0, min_free_mb) * 1024 * 1024
    source_budget = max(1, multiplier) * max(source_bytes, 1024 * 1024)
    return max(min_free_bytes, source_budget)


def _apply_backup_retention(destination: Path, retention_count: int) -> None:
    if retention_count <= 0:
        return
    archives = sorted(
        destination.glob("tunarr-autoscheduler-backup-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_archive in archives[retention_count:]:
        old_archive.unlink(missing_ok=True)


def _backup_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn:
        with sqlite3.connect(destination) as dest_conn:
            source_conn.backup(dest_conn)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"
