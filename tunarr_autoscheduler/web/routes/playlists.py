from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tunarr_autoscheduler.models.playlist import PlaylistItem

router = APIRouter(tags=["playlists"])
PAGE_SIZE = 100


@router.get("/playlists", response_class=HTMLResponse)
async def playlist_list(request: Request) -> HTMLResponse:
    repo = _playlist_repo(request)
    playlists = await repo.list_all()
    filters = {
        "q": request.query_params.get("q", "").strip(),
        "category": request.query_params.get("category", "").strip(),
        "tag": request.query_params.get("tag", "").strip(),
        "scope": request.query_params.get("scope", "").strip(),
        "sort": request.query_params.get("sort", "name").strip() or "name",
    }
    playlists = _filter_playlists(playlists, filters)
    playlists = _sort_playlists(playlists, filters["sort"])
    usage = _playlist_usage(request.app.state.core)
    template = request.app.state.templates.get_template("playlists.html")
    return HTMLResponse(template.render(
        request=request,
        playlists=playlists,
        categories=await _playlist_categories(repo),
        tags=await _playlist_tags(repo),
        channels=_channel_options(request.app.state.core),
        category_builder_links=_category_builder_links(await _playlist_categories(repo)),
        tag_builder_links=await _tag_builder_links(repo),
        filters=filters,
        usage=usage,
        saved=request.query_params.get("saved") == "1",
        deleted=request.query_params.get("deleted") == "1",
        delete_blocked=request.query_params.get("delete_blocked") == "1",
        category_saved=request.query_params.get("category_saved") == "1",
        category_deleted=request.query_params.get("category_deleted") == "1",
    ))


@router.get("/playlists/new", response_class=HTMLResponse)
async def playlist_new(request: Request) -> HTMLResponse:
    return await _render_form(request, None)


@router.get("/playlists/media-options", response_class=HTMLResponse)
async def playlist_media_options(request: Request) -> HTMLResponse:
    query = request.query_params.get("q", "").strip().lower()
    media_type = request.query_params.get("media_type", "all")
    page = max(1, _parse_int(request.query_params.get("page"), 1))
    selected_keys = _split_keys(request.query_params.get("selected", ""))
    inventory = await _media_inventory(request.app.state.core)
    selected_rows, page_rows, total, total_pages, page = _browse_inventory(
        inventory, selected_keys, query, media_type, page,
    )
    template = request.app.state.templates.get_template("playlist_media_browser.html")
    return HTMLResponse(template.render(
        selected_rows=selected_rows,
        rows=page_rows,
        page=page,
        total=total,
        total_pages=total_pages,
    ))


@router.post("/playlists", response_class=HTMLResponse)
async def playlist_create(request: Request) -> Response:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return await _render_form(request, None, "Name is required.", status_code=400)
    inventory = await _media_inventory(request.app.state.core)
    items = _parse_items(str(form.get("item_order", "")), inventory)
    await _playlist_repo(request).create(
        name=name,
        description=str(form.get("description", "")).strip(),
        category_id=str(form.get("category_id", "")).strip(),
        channel_scope=str(form.get("channel_scope", "")).strip(),
        tags=_parse_tags(str(form.get("tags", ""))),
        items=items,
    )
    return RedirectResponse("/playlists?saved=1", status_code=303)


@router.get("/playlists/{playlist_id}/edit", response_class=HTMLResponse)
async def playlist_edit(request: Request, playlist_id: str) -> HTMLResponse:
    playlist = await _playlist_repo(request).get(playlist_id)
    if playlist is None:
        return HTMLResponse("Playlist not found", status_code=404)
    return await _render_form(request, playlist)


@router.post("/playlists/{playlist_id}", response_class=HTMLResponse)
async def playlist_update(request: Request, playlist_id: str) -> Response:
    repo = _playlist_repo(request)
    playlist = await repo.get(playlist_id)
    if playlist is None:
        return HTMLResponse("Playlist not found", status_code=404)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return await _render_form(
            request, playlist, "Name is required.", status_code=400,
        )
    inventory = await _media_inventory(request.app.state.core)
    items = _parse_items(str(form.get("item_order", "")), inventory)
    await repo.update(
        playlist_id,
        name,
        str(form.get("description", "")).strip(),
        items,
        category_id=str(form.get("category_id", "")).strip(),
        channel_scope=str(form.get("channel_scope", "")).strip(),
        tags=_parse_tags(str(form.get("tags", ""))),
    )
    return RedirectResponse("/playlists?saved=1", status_code=303)


@router.post("/playlists/{playlist_id}/delete")
async def playlist_delete(request: Request, playlist_id: str) -> RedirectResponse:
    if _playlist_usage(request.app.state.core).get(playlist_id):
        return RedirectResponse("/playlists?delete_blocked=1", status_code=303)
    await _playlist_repo(request).delete(playlist_id)
    return RedirectResponse("/playlists?deleted=1", status_code=303)


@router.get("/playlist-categories", response_class=HTMLResponse)
async def playlist_categories(request: Request) -> HTMLResponse:
    repo = _playlist_repo(request)
    template = request.app.state.templates.get_template("playlist_categories.html")
    return HTMLResponse(template.render(
        request=request,
        categories=await _playlist_categories(repo),
        playlists=await repo.list_all(),
        saved=request.query_params.get("saved") == "1",
        deleted=request.query_params.get("deleted") == "1",
        error=request.query_params.get("error", ""),
    ))


@router.post("/playlist-categories", response_class=HTMLResponse)
async def playlist_category_create(request: Request) -> Response:
    repo = _playlist_repo(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse("/playlist-categories?error=Name%20is%20required", status_code=303)
    try:
        await repo.create_category(name, str(form.get("description", "")).strip())
    except Exception:
        return RedirectResponse(
            "/playlist-categories?error=Category%20name%20already%20exists",
            status_code=303,
        )
    return RedirectResponse("/playlist-categories?saved=1", status_code=303)


@router.post("/playlist-categories/{category_id}", response_class=HTMLResponse)
async def playlist_category_update(request: Request, category_id: str) -> Response:
    repo = _playlist_repo(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse("/playlist-categories?error=Name%20is%20required", status_code=303)
    try:
        updated = await repo.update_category(
            category_id,
            name,
            str(form.get("description", "")).strip(),
        )
    except Exception:
        return RedirectResponse(
            "/playlist-categories?error=Category%20name%20already%20exists",
            status_code=303,
        )
    if updated is None:
        return RedirectResponse(
            "/playlist-categories?error=Category%20not%20found",
            status_code=303,
        )
    return RedirectResponse("/playlist-categories?saved=1", status_code=303)


@router.post("/playlist-categories/{category_id}/delete")
async def playlist_category_delete(request: Request, category_id: str) -> RedirectResponse:
    await _playlist_repo(request).delete_category(category_id)
    return RedirectResponse("/playlist-categories?deleted=1", status_code=303)


async def _render_form(
    request: Request,
    playlist: Any,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    ordered_keys = [
        f"{item.media_type}:{item.media_id}"
        for item in playlist.items
    ] if playlist else []
    template = request.app.state.templates.get_template("playlist_form.html")
    return HTMLResponse(template.render(
        request=request,
        playlist=playlist,
        categories=await _playlist_categories(_playlist_repo(request)),
        tags=await _playlist_tags(_playlist_repo(request)),
        channels=_channel_options(request.app.state.core),
        selected_keys=ordered_keys,
        error=error,
    ), status_code=status_code)


async def _media_inventory(core: Any) -> dict[str, dict[str, str]]:
    repo = getattr(core, "media_repo", None)
    if repo is None:
        return {}
    options = await repo.get_playlist_options()
    return {item["key"]: item for item in options}


def _browse_inventory(
    inventory: dict[str, dict[str, str]],
    selected_keys: list[str],
    query: str,
    media_type: str,
    page: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]], int, int, int]:
    selected = [inventory[key] for key in selected_keys if key in inventory]
    selected_set = set(selected_keys)
    matches = [
        item
        for key, item in inventory.items()
        if key not in selected_set
        and (media_type == "all" or item["media_type"] == media_type)
        and (not query or query in item["title"].lower())
    ]
    total = len(matches)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * PAGE_SIZE
    return selected, matches[start:start + PAGE_SIZE], total, total_pages, page


def _parse_items(
    raw_order: str,
    inventory: dict[str, dict[str, str]],
) -> list[PlaylistItem]:
    items: list[PlaylistItem] = []
    seen: set[str] = set()
    for key in raw_order.splitlines():
        key = key.strip()
        if not key or key in seen or key not in inventory:
            continue
        candidate = inventory[key]
        items.append(PlaylistItem.model_validate({
            "media_type": candidate["media_type"],
            "media_id": candidate["media_id"],
            "title": candidate["title"],
            "position": len(items),
        }))
        seen.add(key)
    return items


def _playlist_repo(request: Request) -> Any:
    repo = getattr(request.app.state.core, "playlist_repo", None)
    if repo is None:
        raise RuntimeError("Playlist repository is unavailable")
    return repo


def _playlist_usage(core: Any) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for channel in getattr(core.config_manager.config(), "channels", []):
        channel_name = getattr(channel, "name", "") or getattr(channel, "id", "channel")
        for daypart in getattr(channel, "dayparts", []):
            for playlist_id in getattr(daypart, "playlist_ids", []):
                usage.setdefault(str(playlist_id), []).append(
                    f"{channel_name} / {getattr(daypart, 'name', 'daypart')}",
                )
    return usage


def _category_builder_links(categories: list[Any]) -> dict[str, str]:
    return {
        str(category.id): "/recommendations/builder?" + urlencode({
            "preview": "1",
            "mode": "channel",
            "builder_mode": "scratch",
            "source_category": str(category.id),
        })
        for category in categories
    }


async def _tag_builder_links(repo: Any) -> dict[str, str]:
    return {
        tag: "/recommendations/builder?" + urlencode({
            "preview": "1",
            "mode": "channel",
            "builder_mode": "scratch",
            "source_tag": tag,
        })
        for tag in await _playlist_tags(repo)
    }


async def _playlist_categories(repo: Any) -> list[Any]:
    if not hasattr(repo, "list_categories"):
        return []
    categories = await repo.list_categories()
    return list(categories)


async def _playlist_tags(repo: Any) -> list[str]:
    if not hasattr(repo, "list_tags"):
        return []
    tags = await repo.list_tags()
    return [str(tag) for tag in tags]


def _channel_options(core: Any) -> list[dict[str, str]]:
    return [
        {
            "id": str(getattr(channel, "id", "")),
            "name": str(getattr(channel, "name", "") or getattr(channel, "id", "")),
        }
        for channel in getattr(core.config_manager.config(), "channels", [])
        if getattr(channel, "id", "")
    ]


def _parse_tags(raw: str) -> list[str]:
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _filter_playlists(playlists: list[Any], filters: dict[str, str]) -> list[Any]:
    query = filters["q"].lower()
    category = filters["category"]
    tag = filters["tag"].lower()
    scope = filters["scope"]
    result = playlists
    if query:
        result = [
            playlist
            for playlist in result
            if query in playlist.name.lower()
            or query in playlist.description.lower()
            or query in " ".join(playlist.tags).lower()
        ]
    if category == "__none__":
        result = [playlist for playlist in result if not playlist.category_id]
    elif category:
        result = [playlist for playlist in result if playlist.category_id == category]
    if tag:
        result = [
            playlist
            for playlist in result
            if tag in {item.lower() for item in playlist.tags}
        ]
    if scope == "__global__":
        result = [playlist for playlist in result if not playlist.channel_scope]
    elif scope:
        result = [playlist for playlist in result if playlist.channel_scope == scope]
    return result


def _sort_playlists(playlists: list[Any], sort: str) -> list[Any]:
    if sort == "category":
        return sorted(playlists, key=lambda item: (
            item.category_name.lower(),
            item.name.lower(),
            item.id,
        ))
    if sort == "updated":
        return sorted(playlists, key=lambda item: item.updated_at, reverse=True)
    if sort == "scope":
        return sorted(playlists, key=lambda item: (
            item.channel_scope or "",
            item.name.lower(),
            item.id,
        ))
    return sorted(playlists, key=lambda item: (item.name.lower(), item.id))


def _split_keys(raw: str) -> list[str]:
    return [key.strip() for key in raw.splitlines() if key.strip()]


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default
