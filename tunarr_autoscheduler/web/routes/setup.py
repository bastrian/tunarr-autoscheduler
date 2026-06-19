from __future__ import annotations

import asyncio
import logging
import os
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tunarr_autoscheduler.core.auth import hash_password
from tunarr_autoscheduler.integrations.jellyfin.client import JellyfinClient
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient

router = APIRouter(tags=["setup"])
logger = logging.getLogger(__name__)


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request) -> Response:
    if _setup_complete(request):
        return RedirectResponse("/", status_code=303)
    template = request.app.state.templates.get_template("setup.html")
    return HTMLResponse(template.render(request=request, error=None, values={}))


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request) -> Response:
    if _setup_complete(request):
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    values = {key: str(form.get(key, "")).strip() for key in [
        "admin_username",
        "admin_password",
        "jellyfin_url",
        "jellyfin_api_key",
        "jellyfin_user_id",
        "tunarr_url",
        "timezone",
    ]}
    values["timezone"] = values["timezone"] or "Europe/Berlin"
    error = _validate_setup_values(values)
    if error is None:
        error = await _test_connections(request, values)
    if error is not None:
        template = request.app.state.templates.get_template("setup.html")
        return HTMLResponse(
            template.render(request=request, error=error, values=values),
            status_code=400,
        )

    core = request.app.state.core
    config = core.config_manager.config()
    config.auth.username = values["admin_username"]
    config.auth.password_hash = hash_password(values["admin_password"])
    if not config.auth.session_secret or config.auth.session_secret.startswith("YOUR_"):
        config.auth.session_secret = secrets.token_urlsafe(32)
    config.jellyfin.url = values["jellyfin_url"]
    config.jellyfin.api_key = values["jellyfin_api_key"]
    config.jellyfin.user_id = values["jellyfin_user_id"]
    config.tunarr.url = values["tunarr_url"]
    config.timezone = values["timezone"]
    core.config_manager.save(config)
    if getattr(request.app.state, "restart_after_setup", False):
        asyncio.create_task(_restart_after_setup())

    template = request.app.state.templates.get_template("setup_complete.html")
    return HTMLResponse(template.render(request=request))


def _setup_complete(request: Request) -> bool:
    config = request.app.state.core.config_manager.config()
    return (
        _is_real_value(config.auth.username)
        and _is_real_value(config.auth.password_hash)
        and _is_real_value(config.auth.session_secret)
        and _is_real_value(config.jellyfin.api_key)
        and _is_real_value(config.jellyfin.user_id)
    )


def _is_real_value(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped and not stripped.startswith("YOUR_"))


def _validate_setup_values(values: dict[str, str]) -> str | None:
    for field, label in [
        ("admin_username", "Admin username"),
        ("admin_password", "Admin password"),
        ("jellyfin_url", "Jellyfin URL"),
        ("jellyfin_api_key", "Jellyfin API key"),
        ("jellyfin_user_id", "Jellyfin user ID"),
        ("tunarr_url", "Tunarr URL"),
        ("timezone", "Timezone"),
    ]:
        if not values[field]:
            return f"{label} is required"
    if len(values["admin_password"]) < 8:
        return "Admin password must be at least 8 characters"
    try:
        ZoneInfo(values["timezone"])
    except ZoneInfoNotFoundError:
        return f"Unknown timezone: {values['timezone']}"
    return None


async def _restart_after_setup() -> None:
    logger.info("Initial setup saved; restarting to load full scheduler runtime")
    await asyncio.sleep(1)
    os._exit(0)


async def _test_connections(request: Request, values: dict[str, str]) -> str | None:
    jellyfin_cls = getattr(request.app.state, "jellyfin_client_class", JellyfinClient)
    tunarr_cls = getattr(request.app.state, "tunarr_client_class", TunarrClient)
    jellyfin = jellyfin_cls(
        base_url=values["jellyfin_url"],
        api_key=values["jellyfin_api_key"],
        user_id=values["jellyfin_user_id"],
    )
    tunarr = tunarr_cls(
        base_url=values["tunarr_url"],
    )
    try:
        if not await jellyfin.check_connection():
            return "Could not connect to Jellyfin with the provided credentials"
        if not await tunarr.check_connection():
            return "Could not connect to Tunarr with the provided URL"
    finally:
        await jellyfin.close()
        await tunarr.close()
    return None
