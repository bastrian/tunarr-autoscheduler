from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tunarr_autoscheduler.core.auth import verify_password

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    template = request.app.state.templates.get_template("login.html")
    config = request.app.state.core.config_manager.config()
    return HTMLResponse(template.render(
        request=request,
        error=None,
        username=config.auth.username,
    ))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    core = request.app.state.core
    config = core.config_manager.config()
    if username != config.auth.username or not verify_password(password, config.auth.password_hash):
        template = request.app.state.templates.get_template("login.html")
        return HTMLResponse(
            template.render(
                request=request,
                error="Invalid username or password",
                username=config.auth.username,
            ),
            status_code=401,
        )

    signer = request.app.state.session_signer
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "tunarr_session",
        signer.sign("admin").decode(),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/public/login", response_class=HTMLResponse)
async def public_login_form(request: Request) -> HTMLResponse:
    template = request.app.state.templates.get_template("public_login.html")
    return HTMLResponse(template.render(
        request=request,
        error=None,
        return_to=_safe_return_to(str(request.query_params.get("return_to", "/epg"))),
    ))


@router.post("/public/login", response_class=HTMLResponse)
async def public_login(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    return_to = _safe_return_to(str(form.get("return_to", "/epg")))
    core = request.app.state.core
    config = core.config_manager.config()
    if not username or not password:
        return _public_login_error(request, "Username and password are required", return_to)
    try:
        async with httpx.AsyncClient(
            base_url=config.jellyfin.url.rstrip("/"),
            timeout=15.0,
        ) as client:
            response = await client.post(
                "/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={
                    "X-Emby-Authorization": (
                        'MediaBrowser Client="Tunarr AutoScheduler", '
                        'Device="Public EPG", DeviceId="tunarr-autoscheduler-public", '
                        'Version="1.0"'
                    ),
                },
            )
        if response.status_code in {401, 403}:
            return _public_login_error(request, "Invalid Jellyfin username or password", return_to)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return _public_login_error(request, "Could not verify Jellyfin login", return_to)

    user = payload.get("User") if isinstance(payload, dict) else None
    user_id = str(user.get("Id") if isinstance(user, dict) else "").strip()
    if not user_id:
        return _public_login_error(request, "Jellyfin login did not return a user", return_to)
    signer = request.app.state.session_signer
    redirect = RedirectResponse(return_to, status_code=303)
    redirect.set_cookie(
        "public_jellyfin_session",
        signer.sign(f"jellyfin:{user_id}").decode(),
        httponly=True,
        samesite="lax",
    )
    return redirect


@router.post("/logout", response_class=HTMLResponse)
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("tunarr_session")
    response.delete_cookie("public_jellyfin_session")
    return response


def _public_login_error(request: Request, error: str, return_to: str) -> HTMLResponse:
    template = request.app.state.templates.get_template("public_login.html")
    return HTMLResponse(
        template.render(request=request, error=error, return_to=return_to),
        status_code=401,
    )


def _safe_return_to(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/epg"
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/epg"
    if parsed.path in {"/epg", "/public/epg"}:
        suffix = ""
        if parsed.query:
            suffix += f"?{parsed.query}"
        if parsed.fragment:
            suffix += f"#{parsed.fragment}"
        return f"{parsed.path}{suffix}"
    if parsed.path.startswith(("/epg/", "/public/epg/")):
        return value
    return "/epg"
