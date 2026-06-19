from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["audit"])


@router.get("/audit", response_class=HTMLResponse)
async def audit_log(request: Request) -> HTMLResponse:
    core = request.app.state.core
    repo = getattr(core, "audit_repo", None)
    channel_id = str(request.query_params.get("channel_id", "")).strip()
    action = str(request.query_params.get("action", "")).strip()
    status = str(request.query_params.get("status", "")).strip()
    events = []
    if repo is not None:
        events = await repo.list_events(
            channel_id=channel_id,
            action=action,
            status=status,
            limit=200,
        )
    actions = sorted({str(event["action"]) for event in events})
    statuses = sorted({str(event["status"]) for event in events})
    template = request.app.state.templates.get_template("audit.html")
    return HTMLResponse(template.render(
        request=request,
        events=events,
        channels=core.config_manager.config().channels,
        selected_channel_id=channel_id,
        selected_action=action,
        selected_status=status,
        actions=actions,
        statuses=statuses,
        timezone=core.config_manager.config().timezone,
    ))
