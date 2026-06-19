from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from tunarr_autoscheduler.core.auth import hash_password
from tunarr_autoscheduler.core.backups import create_backup_archive
from tunarr_autoscheduler.integrations.metadata.audit import build_metadata_audit
from tunarr_autoscheduler.integrations.metadata.clients import JellystatClient
from tunarr_autoscheduler.integrations.metadata.service import (
    read_provider_statuses,
    read_rate_limit_alert,
)
from tunarr_autoscheduler.integrations.notifications import list_notification_events
from tunarr_autoscheduler.models.config import AppConfig

router = APIRouter(tags=["settings"])

EVENT_TYPE_OPTIONS = [
    "upload_failed",
    "upload_succeeded",
    "generation_failed",
    "schedule_invalid",
    "schedule_expiring_soon",
    "follow_up_missing",
    "auto_follow_up_generated",
    "auto_follow_up_skipped",
    "auto_follow_up_blocked",
    "auto_follow_up_failed",
    "jellyfin_sync_failed",
    "tunarr_connectivity_failed",
    "backup_failed",
    "backup_succeeded",
]
PROVIDER_OPTIONS = ["telegram", "email", "webhook"]
TIMEZONE_OPTIONS = [
    "Europe/Berlin",
    "UTC",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "America/Chicago",
    "America/Denver",
    "Asia/Tokyo",
    "Australia/Sydney",
]
SMTP_SECURITY_OPTIONS = ["plain", "starttls", "ssl"]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    core = request.app.state.core
    template = request.app.state.templates.get_template("settings.html")
    return HTMLResponse(template.render(
        **await _settings_context(request, core.config_manager.config()),
    ))


@router.get("/notifications", response_class=HTMLResponse)
async def notification_history(request: Request) -> HTMLResponse:
    core = request.app.state.core
    event_type = _filter_value(request.query_params.get("event_type"), EVENT_TYPE_OPTIONS)
    provider = _filter_value(request.query_params.get("provider"), PROVIDER_OPTIONS)
    status = _filter_value(
        request.query_params.get("status"),
        ["sent", "failed", "throttled"],
    )
    events = []
    if getattr(core, "db", None) is not None:
        events = await list_notification_events(
            core.db,
            event_type=event_type,
            provider=provider,
            status=status,
            limit=100,
        )
    template = request.app.state.templates.get_template("notifications.html")
    return HTMLResponse(template.render(
        request=request,
        events=events,
        event_type=event_type or "",
        provider=provider or "",
        status=status or "",
        notification_event_types=EVENT_TYPE_OPTIONS,
        notification_providers=PROVIDER_OPTIONS,
    ))


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> Response:
    core = request.app.state.core
    form = await request.form()
    current_config = core.config_manager.config()
    data = current_config.model_dump(mode="python")
    data["timezone"] = str(form.get("timezone", current_config.timezone)).strip()
    if "metadata_submitted" in form:
        data["metadata"] = _metadata_from_form(form, data.get("metadata") or {})
    if "backups_submitted" in form:
        backups = dict(data.get("backups") or {})
        backups["enabled"] = form.get("backups_enabled") == "on"
        backups["interval_hours"] = _int_form(
            form.get("backups_interval_hours"),
            backups.get("interval_hours", 24),
        )
        backups["output_dir"] = str(
            form.get("backups_output_dir", backups.get("output_dir", "~/.tunarr/backups")),
        ).strip() or "~/.tunarr/backups"
        backups["retention_count"] = _int_form(
            form.get("backups_retention_count"),
            backups.get("retention_count", 7),
        )
        backups["min_free_mb"] = _int_form(
            form.get("backups_min_free_mb"),
            backups.get("min_free_mb", 1024),
        )
        backups["size_multiplier"] = _int_form(
            form.get("backups_size_multiplier"),
            backups.get("size_multiplier", 3),
        )
        data["backups"] = backups
    if "public_access_submitted" in form:
        public_access = dict(data.get("public_access") or {})
        requested_epg = str(form.get("public_epg_access", public_access.get("epg", "public")))
        public_access["epg"] = requested_epg if requested_epg in {
            "disabled",
            "jellyfin_login",
            "public",
        } else "public"
        data["public_access"] = public_access
    if "connections_submitted" in form:
        jellyfin = dict(data.get("jellyfin") or {})
        jellyfin["url"] = str(form.get("jellyfin_url", jellyfin.get("url", ""))).strip()
        jellyfin["api_key"] = str(
            form.get("jellyfin_api_key", jellyfin.get("api_key", "")),
        ).strip()
        jellyfin["user_id"] = str(
            form.get("jellyfin_user_id", jellyfin.get("user_id", "")),
        ).strip()
        jellyfin["sync_interval_minutes"] = _int_form(
            form.get("jellyfin_sync_interval_minutes"),
            jellyfin.get("sync_interval_minutes", 15),
        )
        data["jellyfin"] = jellyfin
        tunarr = dict(data.get("tunarr") or {})
        tunarr["url"] = str(form.get("tunarr_url", tunarr.get("url", ""))).strip()
        data["tunarr"] = tunarr
    if "auth_submitted" in form:
        auth_error = _auth_update_error(form, current_config)
        if auth_error:
            template = request.app.state.templates.get_template("settings.html")
            return HTMLResponse(
                template.render(
                    **await _settings_context(
                        request,
                        current_config,
                        error=auth_error,
                    ),
                ),
                status_code=400,
            )
        auth = dict(data.get("auth") or {})
        auth["username"] = str(form.get("auth_username", "")).strip()
        auth["password_hash"] = hash_password(str(form.get("auth_new_password", "")))
        data["auth"] = auth
    if "notifications_submitted" in form:
        notifications = dict(data.get("notifications") or {})
        notifications["enabled"] = form.get("notifications_enabled") == "on"
        notifications["telegram"] = {
            "enabled": form.get("telegram_enabled") == "on",
            "bot_token": str(form.get("telegram_bot_token", "")).strip(),
            "chat_id": str(form.get("telegram_chat_id", "")).strip(),
        }
        notifications["email"] = {
            "enabled": form.get("email_enabled") == "on",
            "smtp_host": str(form.get("email_smtp_host", "")).strip(),
            "smtp_port": _int_form(form.get("email_smtp_port"), 587),
            "username": str(form.get("email_username", "")).strip(),
            "password": str(form.get("email_password", "")).strip(),
            "from_address": str(form.get("email_from_address", "")).strip(),
            "to_addresses": [
                item.strip()
                for item in str(form.get("email_to_addresses", "")).split(",")
                if item.strip()
            ],
            "smtp_security": _smtp_security_value(form.get("email_smtp_security")),
            "use_tls": _smtp_security_value(form.get("email_smtp_security")) == "starttls",
        }
        try:
            webhook_headers = _json_dict(str(form.get("webhook_headers_json", "{}")))
        except ValueError as e:
            template = request.app.state.templates.get_template("settings.html")
            return HTMLResponse(
                template.render(
                    **await _settings_context(
                        request,
                        current_config,
                        error=str(e),
                    ),
                ),
                status_code=400,
            )
        notifications["webhook"] = {
            "enabled": form.get("webhook_enabled") == "on",
            "url": str(form.get("webhook_url", "")).strip(),
            "headers": webhook_headers,
        }
        notifications["rules"] = _notification_rules_from_form(form)
        data["notifications"] = notifications
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as e:
        template = request.app.state.templates.get_template("settings.html")
        return HTMLResponse(
            template.render(
                **await _settings_context(
                    request,
                    current_config,
                    error=str(e),
                ),
            ),
            status_code=400,
        )
    core.config_manager.save(config)
    if "auth_submitted" in form:
        response = RedirectResponse("/login?credentials_changed=1", status_code=303)
        response.delete_cookie("tunarr_session")
        return response
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/jellystat/test", response_class=HTMLResponse)
async def test_jellystat_settings(request: Request) -> HTMLResponse:
    core = request.app.state.core
    form = await request.form()
    current_config = core.config_manager.config()
    data = current_config.model_dump(mode="python")
    data["metadata"] = _metadata_from_form(form, data.get("metadata") or {})
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as e:
        template = request.app.state.templates.get_template("settings.html")
        return HTMLResponse(
            template.render(
                **await _settings_context(
                    request,
                    current_config,
                    error=str(e),
                ),
            ),
            status_code=400,
        )
    metadata = config.metadata
    client = JellystatClient(
        base_url=metadata.jellystat_url,
        api_token=metadata.jellystat_api_token,
        rate_limit_per_minute=metadata.jellystat_rate_limit_per_minute,
    )
    result = await client.check_connection(days=metadata.jellystat_days)
    template = request.app.state.templates.get_template("settings.html")
    return HTMLResponse(template.render(
        **await _settings_context(
            request,
            config,
            jellystat_test_result=result,
        ),
    ))


@router.get("/settings/backups/{backup_name}/download")
async def download_backup(request: Request, backup_name: str) -> FileResponse:
    config = request.app.state.core.config_manager.config()
    backup_path = _backup_archive_path(config, backup_name)
    return FileResponse(
        backup_path,
        media_type="application/zip",
        filename=backup_path.name,
    )


@router.post("/settings/backups/{backup_name}/delete")
async def delete_backup(request: Request, backup_name: str) -> RedirectResponse:
    config = request.app.state.core.config_manager.config()
    backup_path = _backup_archive_path(config, backup_name)
    backup_path.unlink(missing_ok=True)
    return RedirectResponse("/settings?saved=1#settings-operations", status_code=303)


@router.post("/settings/backups/restore")
async def restore_backup(request: Request) -> RedirectResponse:
    core = request.app.state.core
    form = await request.form()
    config = core.config_manager.config()
    selected_name = str(form.get("backup_name", "")).strip()
    upload = form.get("backup_upload")
    if isinstance(upload, UploadFile) and upload.filename:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp_file:
            shutil.copyfileobj(upload.file, temp_file)
            archive_path = Path(temp_file.name)
    elif selected_name:
        archive_path = _backup_archive_path(config, selected_name)
    else:
        return RedirectResponse(
            "/settings?backup_error=missing#settings-operations",
            status_code=303,
        )
    try:
        await create_backup_archive(
            config=config,
            config_path=_config_path(core),
            output_dir=config.backups.output_dir,
            backup_config=config.backups,
            notification_router=getattr(core, "notification_router", None),
        )
        _restore_backup_archive(archive_path, core)
    except (ValueError, zipfile.BadZipFile):
        return RedirectResponse(
            "/settings?backup_error=invalid#settings-operations",
            status_code=303,
        )
    finally:
        if isinstance(upload, UploadFile) and upload.filename:
            archive_path.unlink(missing_ok=True)
    return RedirectResponse("/settings?restored=1#settings-operations", status_code=303)


@router.post("/api/channels/sync")
async def sync_channels(request: Request) -> JSONResponse:
    core = request.app.state.core
    result = await core.channel_sync_engine.sync()
    return JSONResponse(result)


@router.post("/api/media/sync")
async def sync_media(request: Request) -> JSONResponse:
    core = request.app.state.core
    if core.media_sync is None:
        return JSONResponse({"error": "media sync unavailable"}, status_code=503)
    result = await core.media_sync.sync_now()
    return JSONResponse(result)


@router.post("/api/webhooks/jellyfin/media")
async def jellyfin_media_webhook(request: Request) -> JSONResponse:
    core = request.app.state.core
    if core.media_sync is None:
        return JSONResponse({"error": "media sync unavailable"}, status_code=503)
    payload = await _json_payload(request)
    item_id = _webhook_item_id(payload)
    event_name = _webhook_event_name(payload)
    if not item_id:
        return JSONResponse({
            "status": "ignored",
            "reason": "missing_item_id",
        })
    if not hasattr(core.media_sync, "sync_item"):
        return JSONResponse({"error": "targeted media sync unavailable"}, status_code=503)
    result = await core.media_sync.sync_item(item_id, event_name=event_name)
    return JSONResponse({
        "status": "accepted",
        "sync": result,
    })


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _webhook_item_id(payload: dict[str, Any]) -> str | None:
    candidates = [
        payload.get("ItemId"),
        payload.get("itemId"),
        payload.get("item_id"),
        payload.get("Id"),
        payload.get("id"),
    ]
    item = payload.get("Item")
    if isinstance(item, dict):
        candidates.extend([item.get("Id"), item.get("ItemId")])
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if raw:
            return raw
    return None


def _webhook_event_name(payload: dict[str, Any]) -> str | None:
    for key in ("NotificationType", "Event", "event", "Type", "type"):
        raw = str(payload.get(key) or "").strip()
        if raw:
            return raw
    return None


async def _settings_context(
    request: Request,
    config: AppConfig,
    *,
    error: str = "",
    jellystat_test_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    core = request.app.state.core
    audit = {}
    media_repo = getattr(core, "media_repo", None)
    if media_repo is not None:
        audit = build_metadata_audit(await media_repo.get_all_available())
    notification_events = []
    if getattr(core, "db", None) is not None and hasattr(core.db, "fetch_all"):
        notification_events = await list_notification_events(core.db, limit=10)
    return {
        "request": request,
        "config": config,
        "error": error,
        "metadata_audit": audit,
        "metadata_rate_limit_alert": read_rate_limit_alert(),
        "metadata_provider_status": read_provider_statuses(),
        "notification_events": notification_events,
        "webhook_headers_json": _webhook_headers_json(config),
        "notification_event_types": EVENT_TYPE_OPTIONS,
        "notification_providers": PROVIDER_OPTIONS,
        "timezone_options": TIMEZONE_OPTIONS,
        "smtp_security_options": SMTP_SECURITY_OPTIONS,
        "backup_archives": _list_backup_archives(config),
        "jellystat_test_result": jellystat_test_result,
    }


def _metadata_from_form(form: Any, current_metadata: Any) -> dict[str, Any]:
    metadata = dict(current_metadata or {})
    metadata["tmdb_enabled"] = form.get("metadata_tmdb_enabled") == "on"
    metadata["tmdb_api_key"] = str(
        form.get("metadata_tmdb_api_key", metadata.get("tmdb_api_key", "")),
    ).strip()
    metadata["tmdb_language"] = str(
        form.get("metadata_tmdb_language", metadata.get("tmdb_language", "de-DE")),
    ).strip() or "de-DE"
    metadata["tvdb_enabled"] = form.get("metadata_tvdb_enabled") == "on"
    metadata["tvdb_api_key"] = str(
        form.get("metadata_tvdb_api_key", metadata.get("tvdb_api_key", "")),
    ).strip()
    metadata["omdb_enabled"] = form.get("metadata_omdb_enabled") == "on"
    metadata["omdb_api_key"] = str(
        form.get("metadata_omdb_api_key", metadata.get("omdb_api_key", "")),
    ).strip()
    metadata["jellystat_enabled"] = form.get("metadata_jellystat_enabled") == "on"
    metadata["jellystat_url"] = str(
        form.get("metadata_jellystat_url", metadata.get("jellystat_url", "")),
    ).strip()
    metadata["jellystat_api_token"] = str(
        form.get("metadata_jellystat_api_token", metadata.get("jellystat_api_token", "")),
    ).strip()
    metadata["jellystat_days"] = _int_form(
        form.get("metadata_jellystat_days"),
        metadata.get("jellystat_days", 90),
    )
    metadata["jellystat_activity_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_activity_weight"),
        metadata.get("jellystat_activity_weight", 10),
    )
    metadata["jellystat_completion_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_completion_weight"),
        metadata.get("jellystat_completion_weight", 8),
    )
    metadata["jellystat_trend_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_trend_weight"),
        metadata.get("jellystat_trend_weight", 8),
    )
    metadata["jellystat_genre_trend_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_genre_trend_weight"),
        metadata.get("jellystat_genre_trend_weight", 6),
    )
    metadata["jellystat_underused_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_underused_weight"),
        metadata.get("jellystat_underused_weight", 6),
    )
    metadata["jellystat_stale_weight"] = _nonnegative_int_form(
        form.get("metadata_jellystat_stale_weight"),
        metadata.get("jellystat_stale_weight", 4),
    )
    metadata["tmdb_rate_limit_per_minute"] = _int_form(
        form.get("metadata_tmdb_rate_limit_per_minute"),
        metadata.get("tmdb_rate_limit_per_minute", 120),
    )
    metadata["tvdb_rate_limit_per_minute"] = _int_form(
        form.get("metadata_tvdb_rate_limit_per_minute"),
        metadata.get("tvdb_rate_limit_per_minute", 60),
    )
    metadata["omdb_rate_limit_per_minute"] = _int_form(
        form.get("metadata_omdb_rate_limit_per_minute"),
        metadata.get("omdb_rate_limit_per_minute", 60),
    )
    metadata["jellystat_rate_limit_per_minute"] = _int_form(
        form.get("metadata_jellystat_rate_limit_per_minute"),
        metadata.get("jellystat_rate_limit_per_minute", 30),
    )
    metadata["cache_ttl_days"] = _int_form(
        form.get("metadata_cache_ttl_days"),
        metadata.get("cache_ttl_days", 14),
    )
    return metadata


def _auth_update_error(form: Any, _config: AppConfig) -> str:
    username = str(form.get("auth_username", "")).strip()
    new_password = str(form.get("auth_new_password", ""))
    confirm_password = str(form.get("auth_confirm_password", ""))
    if not username:
        return "Admin username is required."
    if len(new_password) < 8:
        return "New password must be at least 8 characters long."
    if new_password != confirm_password:
        return "New password confirmation does not match."
    return ""


def _int_form(value: object, default: object) -> int:
    try:
        parsed = int(str(value if value not in {"", None} else default))
    except ValueError:
        parsed = int(str(default))
    return max(1, parsed)


def _smtp_security_value(value: object) -> str:
    raw = str(value or "starttls").strip().lower()
    return raw if raw in SMTP_SECURITY_OPTIONS else "starttls"


def _backup_directory(config: AppConfig) -> Path:
    return Path(config.backups.output_dir).expanduser().resolve()


def _backup_archive_path(config: AppConfig, backup_name: str) -> Path:
    if Path(backup_name).name != backup_name:
        raise ValueError("Invalid backup name.")
    backup_path = (_backup_directory(config) / backup_name).resolve()
    if backup_path.parent != _backup_directory(config):
        raise ValueError("Invalid backup path.")
    if not backup_path.name.startswith("tunarr-autoscheduler-backup-"):
        raise ValueError("Invalid backup archive.")
    if backup_path.suffix != ".zip" or not backup_path.exists():
        raise ValueError("Backup archive not found.")
    return backup_path


def _list_backup_archives(config: AppConfig) -> list[dict[str, object]]:
    directory = _backup_directory(config)
    if not directory.exists():
        return []
    archives = sorted(
        directory.glob("tunarr-autoscheduler-backup-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": archive.name,
            "size": _format_bytes(archive.stat().st_size),
            "modified": archive.stat().st_mtime,
        }
        for archive in archives
        if archive.is_file()
    ]


def _restore_backup_archive(archive_path: Path, core: Any) -> None:
    config_path = Path(_config_path(core)).expanduser()
    config = core.config_manager.config()
    db_path = Path(config.database.url.replace("sqlite+aiosqlite:///", "")).expanduser()
    with zipfile.ZipFile(archive_path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or "config.yaml" not in names:
            raise ValueError("Backup archive is missing required files.")
        with tempfile.TemporaryDirectory(prefix="tunarr-autoscheduler-restore-") as temp_dir:
            temp_root = Path(temp_dir)
            restored_config = temp_root / "config.yaml"
            restored_db = temp_root / "scheduler.db"
            restored_config.write_bytes(zf.read("config.yaml"))
            if "scheduler.db" in names:
                restored_db.write_bytes(zf.read("scheduler.db"))
            if config_path.exists():
                shutil.copy2(
                    config_path,
                    config_path.with_suffix(config_path.suffix + ".pre-restore"),
                )
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(restored_config, config_path)
            if restored_db.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)
                if db_path.exists():
                    shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".pre-restore"))
                shutil.copy2(restored_db, db_path)
    if hasattr(core.config_manager, "load"):
        core.config_manager.load()


def _config_path(core: Any) -> str:
    return str(getattr(core.config_manager, "config_path", "~/.tunarr/config.yaml"))


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _webhook_headers_json(config: AppConfig) -> str:
    return json_dumps(config.notifications.webhook.headers)


def _json_dict(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("Webhook headers must be valid JSON.") from e
    if not isinstance(payload, dict):
        raise ValueError("Webhook headers must be a JSON object.")
    return {str(key): str(value) for key, value in payload.items()}


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True)


def _notification_rules_from_form(form: Any) -> list[dict[str, Any]]:
    event_types = [str(value) for value in form.getlist("rule_event_type")]
    rules: list[dict[str, Any]] = []
    for index, event_type in enumerate(event_types):
        event_type = event_type.strip()
        if not event_type:
            continue
        providers = [
            provider
            for provider in PROVIDER_OPTIONS
            if form.get(f"rule_{index}_{provider}") == "on"
        ]
        rules.append({
            "event_type": event_type,
            "enabled": form.get(f"rule_{index}_enabled") == "on",
            "providers": providers,
            "throttle_minutes": _nonnegative_int_form(
                form.get(f"rule_{index}_throttle_minutes"),
                30,
            ),
            "quiet_hours_start": str(form.get(f"rule_{index}_quiet_start", "")).strip(),
            "quiet_hours_end": str(form.get(f"rule_{index}_quiet_end", "")).strip(),
        })
    return rules


def _filter_value(value: str | None, allowed: list[str]) -> str | None:
    value = (value or "").strip()
    return value if value in allowed else None


def _nonnegative_int_form(value: object, default: object) -> int:
    try:
        parsed = int(str(value if value not in {"", None} else default))
    except ValueError:
        parsed = int(str(default))
    return max(0, parsed)
