from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, Signer
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tunarr_autoscheduler.core.schedule_monitor import channel_schedule_statuses
from tunarr_autoscheduler.core.timezones import format_datetime
from tunarr_autoscheduler.integrations.jellyfin.client import JellyfinClient
from tunarr_autoscheduler.integrations.tunarr.client import TunarrClient
from tunarr_autoscheduler.web.routes.audit import router as audit_router
from tunarr_autoscheduler.web.routes.auth import router as auth_router
from tunarr_autoscheduler.web.routes.channels import router as channels_router
from tunarr_autoscheduler.web.routes.jobs import router as jobs_router
from tunarr_autoscheduler.web.routes.playlists import router as playlists_router
from tunarr_autoscheduler.web.routes.public import router as public_router
from tunarr_autoscheduler.web.routes.recommendations import router as recommendations_router
from tunarr_autoscheduler.web.routes.schedules import router as schedules_router
from tunarr_autoscheduler.web.routes.settings import router as settings_router
from tunarr_autoscheduler.web.routes.setup import router as setup_router

if TYPE_CHECKING:
    from tunarr_autoscheduler.main import Core

logger = logging.getLogger(__name__)


def create_app(core: Core) -> FastAPI:
    app = FastAPI(title="Tunarr AutoScheduler")

    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["local_datetime"] = format_datetime

    app.state.core = core
    app.state.templates = env
    app.state.session_signer = Signer(core.config_manager.config().auth.session_secret)
    app.state.jellyfin_client_class = JellyfinClient
    app.state.tunarr_client_class = TunarrClient
    app.state.restart_after_setup = False

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.middleware("http")(_request_logging_middleware)
    app.middleware("http")(_auth_middleware)
    app.include_router(setup_router, prefix="")
    app.include_router(auth_router, prefix="")
    app.include_router(audit_router, prefix="")
    app.include_router(channels_router, prefix="")
    app.include_router(schedules_router, prefix="")
    app.include_router(jobs_router, prefix="")
    app.include_router(playlists_router, prefix="")
    app.include_router(recommendations_router, prefix="")
    app.include_router(settings_router, prefix="")
    app.include_router(public_router, prefix="")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        core = request.app.state.core
        channels = core.config_manager.config().channels
        active_jobs = {
            c.id: core.job_manager.get_active_job(c.id)
            for c in channels
        }
        schedule_statuses = {}
        if getattr(core, "state", None) is not None:
            schedule_statuses = await channel_schedule_statuses(
                config=core.config_manager.config(),
                state=core.state,
            )
        template = request.app.state.templates.get_template("dashboard.html")
        return HTMLResponse(template.render(
            request=request,
            channels=channels,
            active_jobs=active_jobs,
            schedule_statuses=schedule_statuses,
            timezone=core.config_manager.config().timezone,
            saved_channel=request.query_params.get("saved_channel"),
        ))

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        core = request.app.state.core
        checks = {"app": True}
        db = getattr(core, "db", None)
        if db is not None:
            try:
                await db.fetch_one("SELECT 1 AS ok")
                checks["database"] = True
            except Exception:
                checks["database"] = False
        status = "ok" if all(checks.values()) else "degraded"
        return JSONResponse({"status": status, "checks": checks})

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        core = request.app.state.core
        checks = {
            "database": await _check_database(core),
            "jellyfin": await _check_client(getattr(core, "jellyfin_client", None)),
            "tunarr": await _check_client(getattr(core, "tunarr_client", None)),
        }
        ready_status = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready_status else "not_ready", "checks": checks},
            status_code=200 if ready_status else 503,
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        core = request.app.state.core
        return PlainTextResponse(
            core.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4",
        )

    return app


async def _request_logging_middleware(request: Request, call_next: Any) -> Any:
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "HTTP request failed method=%s path=%s elapsed_ms=%.2f",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "HTTP request method=%s path=%s status=%s elapsed_ms=%.2f client=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request.client.host if request.client else "-",
    )
    return response


async def _auth_middleware(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if _is_always_public_path(path):
        return await call_next(request)

    core = request.app.state.core
    if _is_public_epg_path(path):
        access = core.config_manager.config().public_access.epg
        if access == "disabled":
            return PlainTextResponse("Public EPG is disabled", status_code=404)
        if access == "public" or _has_valid_public_jellyfin_session(request):
            return await call_next(request)
        if _wants_html(request) and not _is_public_epg_data_path(path):
            return RedirectResponse(
                f"/public/login?return_to={request.url.path}",
                status_code=303,
            )
        return JSONResponse({"error": "Jellyfin login required"}, status_code=401)

    if not core.config_manager.auth_configured():
        return RedirectResponse("/setup", status_code=303)

    cookie = request.cookies.get("tunarr_session")
    if cookie:
        try:
            user = request.app.state.session_signer.unsign(cookie).decode()
            if user == "admin":
                return await call_next(request)
        except BadSignature:
            pass
    return RedirectResponse("/login", status_code=303)


def _is_always_public_path(path: str) -> bool:
    return (
        path in {"/health", "/ready", "/login", "/setup", "/public/login"}
        or path.startswith("/static/")
        or path == "/api/webhooks/jellyfin/media"
    )


def _is_public_epg_path(path: str) -> bool:
    return path == "/epg" or path.startswith("/public/epg")


def _is_public_epg_data_path(path: str) -> bool:
    return (
        path.endswith(".json")
        or path.endswith(".xml")
        or "/images/" in path
        or path.endswith("/xmltv")
    )


def _has_valid_public_jellyfin_session(request: Request) -> bool:
    cookie = request.cookies.get("public_jellyfin_session")
    if not cookie:
        return False
    try:
        value = request.app.state.session_signer.unsign(cookie).decode()
    except BadSignature:
        return False
    return value.startswith("jellyfin:") and len(value) > len("jellyfin:")


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept


async def _check_database(core: object) -> bool:
    db = getattr(core, "db", None)
    if db is None:
        return False
    try:
        await db.fetch_one("SELECT 1 AS ok")
    except Exception:
        return False
    return True


async def _check_client(client: object | None) -> bool:
    if client is None or not hasattr(client, "check_connection"):
        return False
    typed_client: Any = client
    return bool(await typed_client.check_connection())
