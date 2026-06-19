from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tunarr_autoscheduler.db.repositories.recommendation_profile_repo import (
    RecommendationProfileRepository,
)
from tunarr_autoscheduler.integrations.metadata.cache import ExternalMetadataCacheRepository
from tunarr_autoscheduler.models.config import ChannelConfig, DayOfWeek, DaypartTemplate
from tunarr_autoscheduler.models.playlist import PlaylistItem
from tunarr_autoscheduler.recommendations.engine import (
    RecommendationEngine,
    RecommendationResult,
)
from tunarr_autoscheduler.recommendations.profiles import (
    BUILT_IN_PROFILES,
    RecommendationProfile,
)
from tunarr_autoscheduler.recommendations.signals import build_external_signals
from tunarr_autoscheduler.web.routes.playlists import (
    _channel_options,
    _parse_tags,
    _playlist_categories,
    _playlist_tags,
)

router = APIRouter(tags=["recommendations"])

LANGUAGE_RULES = {
    "profile_default": "Profile default",
    "none": "No language filter",
    "english_audio": "English audio required",
    "english_subtitles": "English subtitles required",
    "english_audio_or_subtitles": "English audio or subtitles required",
    "prefer_english_audio_allow_subtitles": "Prefer English, allow fallback",
}


@router.get("/recommendations", response_class=HTMLResponse)
async def recommendation_list(request: Request) -> HTMLResponse:
    form = _query_form(request)
    try:
        results = await _run_recommendations(request, form)
        error = request.query_params.get("error", "")
    except ValueError:
        results = []
        error = "Recommendation request is invalid."
    return await _render_recommendations(request, form, results, error=error)


@router.get("/recommendations/diagnostics", response_class=HTMLResponse)
async def recommendation_diagnostics(request: Request) -> HTMLResponse:
    diagnostics = await (await _engine(request)).diagnostics()
    template = request.app.state.templates.get_template("recommendation_diagnostics.html")
    return HTMLResponse(template.render(
        request=request,
        diagnostics=diagnostics,
    ))


@router.get("/recommendations/compare", response_class=HTMLResponse)
async def recommendation_compare(request: Request) -> HTMLResponse:
    profiles = await _all_profiles(request)
    selected = [
        item.strip()
        for item in request.query_params.get("profiles", "").split(",")
        if item.strip() in profiles
    ]
    if not selected:
        selected = ["morning-sitcoms", "series-marathon"]
    selected = selected[:4]
    limit = _bounded_int(request.query_params.get("limit"), default=25, maximum=100)
    language_rule = request.query_params.get("language_rule", "profile_default")
    compare = await _compare_profiles(request, selected, limit, language_rule)
    template = request.app.state.templates.get_template("recommendation_compare.html")
    return HTMLResponse(template.render(
        request=request,
        profiles=list(profiles.values()),
        selected_profiles=selected,
        limit=limit,
        language_rule=language_rule,
        language_rules=LANGUAGE_RULES,
        compare=compare,
    ))


@router.get("/recommendations/runs", response_class=HTMLResponse)
async def recommendation_runs(request: Request) -> HTMLResponse:
    repo = _run_repo(request)
    template = request.app.state.templates.get_template("recommendation_runs.html")
    return HTMLResponse(template.render(
        request=request,
        runs=await repo.list_recent(),
        saved=request.query_params.get("saved") == "1",
        applied=request.query_params.get("applied") == "1",
        error=request.query_params.get("error", ""),
        timezone=request.app.state.core.config_manager.config().timezone,
    ))


@router.get("/recommendations/builder", response_class=HTMLResponse)
async def recommendation_builder(request: Request) -> HTMLResponse:
    form = _builder_query_form(request)
    form = await _apply_builder_source_defaults(request, form)
    plan = await _build_recommendation_plan(request, form) if form["preview"] else None
    return await _render_builder(
        request,
        form,
        plan,
        saved_run_id=request.query_params.get("run_id", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/recommendations/builder/runs", response_class=HTMLResponse)
async def recommendation_builder_save(request: Request) -> Response:
    form_data = await request.form()
    form = _builder_posted_form(form_data)
    form = await _apply_builder_source_defaults(request, form)
    plan = await _build_recommendation_plan(request, form)
    run = await _run_repo(request).create(
        run_type="auto_generation",
        title=plan["title"],
        request=form,
        result=plan,
    )
    return RedirectResponse(
        f"/recommendations/builder?run_id={run['id']}&preview=1&saved=1",
        status_code=303,
    )


@router.post("/recommendations/runs/{run_id}/apply", response_class=HTMLResponse)
async def recommendation_builder_apply(request: Request, run_id: str) -> Response:
    run = await _run_repo(request).get(run_id)
    if run is None:
        return RedirectResponse("/recommendations/runs?error=Run%20not%20found", status_code=303)
    result = run.get("result", {})
    request_data = run.get("request", {})
    if not isinstance(result, dict) or not isinstance(request_data, dict):
        return RedirectResponse("/recommendations/runs?error=Run%20is%20invalid", status_code=303)
    await _apply_recommendation_plan(request, request_data, result)
    await _run_repo(request).mark_applied(run_id)
    return RedirectResponse("/recommendations/runs?applied=1", status_code=303)


@router.post("/recommendations/runs/{run_id}/generate-draft", response_class=HTMLResponse)
async def recommendation_builder_generate_draft(request: Request, run_id: str) -> Response:
    core = request.app.state.core
    run = await _run_repo(request).get(run_id)
    if run is None:
        return RedirectResponse("/recommendations/runs?error=Run%20not%20found", status_code=303)
    result = run.get("result", {})
    request_data = run.get("request", {})
    if not isinstance(result, dict) or not isinstance(request_data, dict):
        return RedirectResponse("/recommendations/runs?error=Run%20is%20invalid", status_code=303)
    try:
        channel = _find_channel_in_core(
            core,
            str(request_data.get("channel_id") or result.get("channel_id") or "").strip(),
        )
        if run.get("status") != "applied" or channel is None:
            channel = await _apply_recommendation_plan(request, request_data, result)
            await _run_repo(request).mark_applied(run_id)
        if core.job_manager.is_running(channel.id):
            return RedirectResponse(
                "/recommendations/runs?applied=1&error=Generation%20already%20running",
                status_code=303,
            )
        await core.job_manager.start_generation(channel, "fresh")
    except Exception:
        return RedirectResponse("/recommendations/runs?error=generation_failed", status_code=303)
    return RedirectResponse(
        f"/recommendations/runs?applied=1&generated=1&channel_id={channel.id}",
        status_code=303,
    )


@router.get("/recommendations/runs/{run_id}/rerun", response_class=HTMLResponse)
async def recommendation_builder_rerun_preview(request: Request, run_id: str) -> Response:
    run = await _run_repo(request).get(run_id)
    if run is None:
        return RedirectResponse("/recommendations/runs?error=Run%20not%20found", status_code=303)
    form = run.get("request", {})
    if not isinstance(form, dict):
        return RedirectResponse("/recommendations/runs?error=Run%20is%20invalid", status_code=303)
    preview_form = _normalize_builder_form(form)
    preview_form["preview"] = True
    plan = await _build_recommendation_plan(request, preview_form)
    return await _render_builder(
        request,
        preview_form,
        plan,
        rerun_source_id=run_id,
    )


@router.post("/recommendations/runs/{run_id}/rerun", response_class=HTMLResponse)
async def recommendation_builder_rerun_save(request: Request, run_id: str) -> Response:
    run = await _run_repo(request).get(run_id)
    if run is None:
        return RedirectResponse("/recommendations/runs?error=Run%20not%20found", status_code=303)
    form = run.get("request", {})
    if not isinstance(form, dict):
        return RedirectResponse("/recommendations/runs?error=Run%20is%20invalid", status_code=303)
    preview_form = _normalize_builder_form(form)
    preview_form["preview"] = True
    plan = await _build_recommendation_plan(request, preview_form)
    saved = await _run_repo(request).create(
        run_type="auto_generation",
        title=f"{plan['title']} rerun",
        request=preview_form,
        result=plan,
    )
    return RedirectResponse(
        f"/recommendations/builder?run_id={saved['id']}&preview=1&saved=1",
        status_code=303,
    )


@router.get("/recommendations/explain", response_class=HTMLResponse)
async def recommendation_explain(request: Request) -> HTMLResponse:
    profile_id = request.query_params.get("profile", "prime-time-movies")
    item_id = request.query_params.get("item_id", "")
    language_rule = request.query_params.get("language_rule") or None
    if language_rule == "profile_default":
        language_rule = None
    result = None
    error = ""
    if item_id:
        result = await (await _engine(request)).explain(
            item_id,
            profile_id,
            language_rule=language_rule,
        )
        if result is None:
            error = f"No recommendation candidate found for {item_id}."
    profiles = await _all_profiles(request)
    template = request.app.state.templates.get_template("recommendation_explain.html")
    return HTMLResponse(template.render(
        request=request,
        profiles=list(profiles.values()),
        selected_profile=profiles.get(profile_id),
        profile_id=profile_id,
        item_id=item_id,
        language_rule=language_rule or "profile_default",
        language_rules=LANGUAGE_RULES,
        result=_result_view(result) if result else None,
        error=error,
    ))


@router.get("/recommendations/profiles", response_class=HTMLResponse)
async def recommendation_profiles(request: Request) -> HTMLResponse:
    profiles = await _all_profiles(request)
    template = request.app.state.templates.get_template("recommendation_profiles.html")
    return HTMLResponse(template.render(
        request=request,
        built_in_profiles=list(BUILT_IN_PROFILES.values()),
        custom_profiles=[
            profile for profile in profiles.values()
            if profile.id not in BUILT_IN_PROFILES
        ],
        saved=request.query_params.get("saved") == "1",
        deleted=request.query_params.get("deleted") == "1",
        error=request.query_params.get("error", ""),
    ))


@router.get("/recommendations/profiles/new", response_class=HTMLResponse)
async def recommendation_profile_new(request: Request) -> HTMLResponse:
    return await _render_profile_form(request, None)


@router.get("/recommendations/profiles/{profile_id}/edit", response_class=HTMLResponse)
async def recommendation_profile_edit(request: Request, profile_id: str) -> HTMLResponse:
    profile = await _profile_repo(request).get(profile_id)
    if profile is None:
        return HTMLResponse("Recommendation profile not found", status_code=404)
    return await _render_profile_form(request, profile)


@router.post("/recommendations/profiles", response_class=HTMLResponse)
async def recommendation_profile_create(request: Request) -> Response:
    form = await request.form()
    profile = _profile_from_form(form, existing_id="")
    if profile.id in BUILT_IN_PROFILES:
        return RedirectResponse(
            "/recommendations/profiles?error=Profile%20ID%20is%20reserved",
            status_code=303,
        )
    await _profile_repo(request).save(profile)
    return RedirectResponse("/recommendations/profiles?saved=1", status_code=303)


@router.post("/recommendations/profiles/{profile_id}", response_class=HTMLResponse)
async def recommendation_profile_update(request: Request, profile_id: str) -> Response:
    if profile_id in BUILT_IN_PROFILES:
        return RedirectResponse(
            "/recommendations/profiles?error=Built-in%20profiles%20cannot%20be%20edited",
            status_code=303,
        )
    form = await request.form()
    profile = _profile_from_form(form, existing_id=profile_id)
    await _profile_repo(request).save(profile)
    return RedirectResponse("/recommendations/profiles?saved=1", status_code=303)


@router.post("/recommendations/profiles/{profile_id}/delete")
async def recommendation_profile_delete(request: Request, profile_id: str) -> RedirectResponse:
    if profile_id in BUILT_IN_PROFILES:
        return RedirectResponse(
            "/recommendations/profiles?error=Built-in%20profiles%20cannot%20be%20deleted",
            status_code=303,
        )
    await _profile_repo(request).delete(profile_id)
    return RedirectResponse("/recommendations/profiles?deleted=1", status_code=303)


@router.post("/recommendations/playlists", response_class=HTMLResponse)
async def recommendation_playlist_create(request: Request) -> Response:
    form_data = await request.form()
    form = _posted_form(form_data)
    selected_keys = [str(value) for value in form_data.getlist("selected_keys")]
    results = await _run_recommendations(request, form, playlist_limit=max(form["limit"], 500))
    candidates = {
        _candidate_key(result): result
        for result in results
        if result.accepted
    }
    items: list[PlaylistItem] = []
    selected_results: list[RecommendationResult] = []
    seen: set[str] = set()
    for key in selected_keys:
        if key in seen or key not in candidates:
            continue
        result = candidates[key]
        candidate = result.candidate
        items.append(PlaylistItem.model_validate({
            "media_type": candidate.media_type,
            "media_id": candidate.id,
            "title": candidate.title,
            "position": len(items),
        }))
        selected_results.append(result)
        seen.add(key)

    if not items:
        return RedirectResponse("/recommendations?error=select_recommendation", status_code=303)

    name = str(form_data.get("name", "")).strip() or _default_playlist_name(form["profile"])
    description = str(form_data.get("description", "")).strip()
    if not description:
        description = f"Generated from recommendation profile {form['profile']}."
    repo = _playlist_repo(request)
    category_id = str(form_data.get("category_id", "")).strip()
    channel_scope = str(form_data.get("channel_scope", "")).strip()
    tags = _parse_tags(str(form_data.get("tags", "")))
    if str(form_data.get("auto_organize", "")).strip().lower() in {"1", "true", "on"}:
        if not category_id:
            category_id = await _ensure_recommendation_category(repo, form, request)
        tags = _auto_recommendation_tags(tags, form, selected_results, request)
    target_playlist_id = str(form_data.get("target_playlist_id", "")).strip()
    mode = str(form_data.get("playlist_mode", "create")).strip()
    if mode == "append" and not target_playlist_id:
        target_playlist_id = str(form.get("source_playlist_id", "")).strip()
    playlist_id = ""
    if mode in {"replace", "append"} and target_playlist_id:
        existing = await repo.get(target_playlist_id)
        if existing is None:
            return RedirectResponse("/recommendations?error=playlist_not_found", status_code=303)
        if mode == "append":
            items = _merge_playlist_items(existing.items, items)
        await repo.update(
            target_playlist_id,
            name or existing.name,
            description or existing.description,
            items,
            category_id=category_id,
            channel_scope=channel_scope,
        tags=tags,
        )
        playlist_id = target_playlist_id
    else:
        playlist = await repo.create(
            name=name,
            description=description,
            category_id=category_id,
            channel_scope=channel_scope,
            tags=tags,
            items=items,
        )
        playlist_id = playlist.id
    if form.get("assign_to_daypart") and playlist_id:
        if _assign_playlist_to_daypart(request, form, playlist_id):
            return RedirectResponse("/?saved=1&assigned_playlist=1", status_code=303)
        return RedirectResponse("/recommendations?error=daypart_assignment_failed", status_code=303)
    return RedirectResponse("/playlists?saved=1", status_code=303)


@router.post("/recommendations/daypart-fix", response_class=HTMLResponse)
async def recommendation_daypart_fix(request: Request) -> Response:
    form_data = await request.form()
    form = _posted_form(form_data)
    form["assign_to_daypart"] = True
    form["limit"] = _bounded_int(str(form_data.get("limit", "25")), default=25, maximum=100)
    form["min_score"] = _bounded_int(str(form_data.get("min_score", "1")), default=1, maximum=100)
    channel = _find_channel(request, str(form.get("channel_id", "")))
    daypart = _find_daypart(channel, str(form.get("daypart", ""))) if channel else None
    if channel is None or daypart is None:
        return RedirectResponse("/recommendations?error=daypart_not_found", status_code=303)
    results = [
        result for result in await _run_recommendations(request, form, playlist_limit=form["limit"])
        if result.accepted
    ]
    if not results:
        return RedirectResponse("/?error=no_recommendation_fix_candidates", status_code=303)
    repo = _playlist_repo(request)
    tags = _auto_recommendation_tags(
        ["recommended", "auto-fix", daypart.name],
        form,
        results,
        request,
    )
    category_id = await _ensure_recommendation_category(repo, form, request)
    items = [
        PlaylistItem.model_validate({
            "media_type": result.candidate.media_type,
            "media_id": result.candidate.id,
            "title": result.candidate.title,
            "position": index,
        })
        for index, result in enumerate(results)
    ]
    playlist = await repo.create(
        name=f"{channel.name or channel.id} - {daypart.name} Auto Fix",
        description=(
            f"Direct fix playlist generated from {form['profile']} recommendations "
            f"for {daypart.name}."
        ),
        category_id=category_id,
        channel_scope=channel.id,
        tags=tags,
        items=items,
    )
    if playlist.id not in daypart.playlist_ids:
        daypart.playlist_ids.append(playlist.id)
    request.app.state.core.config_manager.save()
    return RedirectResponse("/?saved=1&assigned_playlist=1", status_code=303)


async def _render_recommendations(
    request: Request,
    form: dict[str, Any],
    results: list[RecommendationResult],
    *,
    error: str = "",
) -> HTMLResponse:
    repo = _playlist_repo(request)
    profiles = await _all_profiles(request)
    context = _recommendation_context(request, form)
    similarity = await _playlist_similarity_context(
        request,
        str(form.get("source_playlist_id", "")),
    )
    template = request.app.state.templates.get_template("recommendations.html")
    return HTMLResponse(template.render(
        request=request,
        form=form,
        profiles=list(profiles.values()),
        selected_profile=profiles.get(form["profile"]),
        context=context,
        similarity=similarity,
        language_rules=LANGUAGE_RULES,
        results=[_result_view(result) for result in results],
        playlist_options=await repo.list_all(),
        categories=await _playlist_categories(repo),
        tags=await _playlist_tags(repo),
        channels=_channel_options(request.app.state.core),
        default_playlist_name=_default_playlist_name(form["profile"], context),
        default_tags=", ".join(_default_tags(form["profile"], context)),
        suggested_category_name=_default_category_name(form, context),
        error=error,
    ))


async def _run_recommendations(
    request: Request,
    form: dict[str, Any],
    *,
    playlist_limit: int | None = None,
) -> list[RecommendationResult]:
    engine = await _engine(request)
    language_rule = None if form["language_rule"] == "profile_default" else form["language_rule"]
    results = await engine.run(
        form["profile"],
        limit=playlist_limit or form["limit"],
        include_excluded=form["include_excluded"],
        language_rule=language_rule,
    )
    similarity = await _playlist_similarity_context(
        request,
        str(form.get("source_playlist_id", "")),
    )
    if similarity:
        results = _apply_playlist_similarity(results, similarity)
    filtered = _filter_results(results, form)
    return filtered[:playlist_limit] if playlist_limit is not None else filtered[:form["limit"]]


async def _compare_profiles(
    request: Request,
    profile_ids: list[str],
    limit: int,
    language_rule: str,
) -> dict[str, Any]:
    engine = await _engine(request)
    override = None if language_rule == "profile_default" else language_rule
    profiles = await _all_profiles(request)
    runs: dict[str, list[RecommendationResult]] = {}
    accepted_sets: dict[str, set[str]] = {}
    rows: list[dict[str, Any]] = []
    for profile_id in profile_ids:
        results = await engine.run(
            profile_id,
            limit=max(limit, 100),
            language_rule=override,
        )
        accepted = [result for result in results if result.accepted]
        selected = accepted[:limit]
        runs[profile_id] = selected
        accepted_sets[profile_id] = {_candidate_key(result) for result in selected}
        scores = [result.score for result in selected]
        rows.append({
            "profile": profiles[profile_id],
            "count": len(selected),
            "average_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "top_items": [_result_view(result) for result in selected[:8]],
            "warnings": sorted({
                warning
                for result in selected
                for warning in result.warnings
            })[:5],
        })
    overlaps: list[dict[str, Any]] = []
    for left_index, left in enumerate(profile_ids):
        for right in profile_ids[left_index + 1:]:
            shared = accepted_sets[left] & accepted_sets[right]
            overlaps.append({
                "left": profiles[left].name,
                "right": profiles[right].name,
                "count": len(shared),
                "percent": round(
                    (len(shared) / max(1, min(len(accepted_sets[left]), len(accepted_sets[right]))))
                    * 100,
                    1,
                ),
            })
    return {"rows": rows, "overlaps": overlaps}


def _filter_results(
    results: list[RecommendationResult],
    form: dict[str, Any],
) -> list[RecommendationResult]:
    query = str(form.get("q", "")).lower()
    positive_query, inline_excluded_terms = _split_search_query(query)
    excluded_terms = [
        term.lower()
        for term in [
            *inline_excluded_terms,
            *_csv_list(str(form.get("exclude_q", ""))),
        ]
        if term.strip()
    ]
    media_type = str(form.get("media_type", "all"))
    min_score = int(form.get("min_score", 0))
    filtered = []
    for result in results:
        data = result.as_dict()
        searchable = " ".join([
            result.candidate.title,
            result.candidate.media_type,
            " ".join(str(value) for value in data.get("genres", [])),
            " ".join(str(value) for value in data.get("tags", [])),
            " ".join(str(value) for value in data.get("manual_terms", [])),
            " ".join(result.reasons),
            " ".join(result.warnings),
            " ".join(result.exclusions),
        ]).lower()
        if positive_query and positive_query not in searchable:
            continue
        if any(term in searchable for term in excluded_terms):
            continue
        if media_type != "all" and result.candidate.media_type != media_type:
            continue
        if result.score < min_score:
            continue
        filtered.append(result)
    return filtered


async def _engine(request: Request) -> RecommendationEngine:
    core = request.app.state.core
    media_repo = getattr(core, "media_repo", None)
    if media_repo is None:
        raise RuntimeError("Media repository is unavailable")
    playlist_repo = _playlist_repo(request)
    manual_terms: dict[str, list[str]] = {}
    if hasattr(playlist_repo, "get_recommendation_terms_by_media_id"):
        manual_terms = await playlist_repo.get_recommendation_terms_by_media_id()
    external_signals: dict[str, dict[str, Any]] = {}
    db = getattr(core, "db", None)
    if db is not None and hasattr(db, "fetch_all"):
        entries = await media_repo.get_all_available()
        external_signals = await build_external_signals(
            entries,
            ExternalMetadataCacheRepository(db),
        )
    return RecommendationEngine(
        media_repo,
        manual_terms,
        profiles=await _all_profiles(request),
        external_signals_by_media_id=external_signals,
        signal_weights=_recommendation_signal_weights(core.config_manager.config().metadata),
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


async def _render_profile_form(
    request: Request,
    profile: RecommendationProfile | None,
    error: str = "",
) -> HTMLResponse:
    template = request.app.state.templates.get_template("recommendation_profile_form.html")
    return HTMLResponse(template.render(
        request=request,
        profile=profile,
        language_rules={
            key: label
            for key, label in LANGUAGE_RULES.items()
            if key != "profile_default"
        },
        error=error,
    ))


def _query_form(request: Request) -> dict[str, Any]:
    channel_id = request.query_params.get("channel_id", "").strip()
    daypart_name = request.query_params.get("daypart", "").strip()
    playlist_id = request.query_params.get("playlist_id", "").strip()
    source_playlist_id = request.query_params.get("source_playlist_id", "").strip()
    playlist_mode = request.query_params.get("playlist_mode", "").strip()
    inferred = _infer_context_defaults(request, channel_id, daypart_name)
    has_profile = "profile" in request.query_params
    has_media_type = "media_type" in request.query_params
    return {
        "profile": request.query_params.get("profile", inferred["profile"]),
        "language_rule": request.query_params.get("language_rule", "profile_default"),
        "limit": _bounded_int(request.query_params.get("limit"), default=50, maximum=500),
        "q": request.query_params.get("q", "").strip(),
        "exclude_q": request.query_params.get("exclude_q", "").strip(),
        "media_type": (
            request.query_params.get("media_type", inferred["media_type"]).strip() or "all"
        ),
        "min_score": _bounded_int(request.query_params.get("min_score"), default=0, maximum=100),
        "include_excluded": request.query_params.get("include_excluded") in {"1", "true", "on"},
        "channel_id": channel_id,
        "daypart": daypart_name,
        "playlist_id": playlist_id,
        "source_playlist_id": source_playlist_id,
        "playlist_mode": playlist_mode if playlist_mode in {"replace", "append"} else "",
        "assign_to_daypart": (
            request.query_params.get("assign_to_daypart") in {"1", "true", "on"}
            or bool(channel_id and daypart_name and not has_profile and not has_media_type)
        ),
    }


def _posted_form(form_data: Any) -> dict[str, Any]:
    return {
        "profile": str(form_data.get("profile", "prime-time-movies")),
        "language_rule": str(form_data.get("language_rule", "profile_default")),
        "limit": _bounded_int(str(form_data.get("limit", "50")), default=50, maximum=500),
        "q": str(form_data.get("q", "")).strip(),
        "exclude_q": str(form_data.get("exclude_q", "")).strip(),
        "media_type": str(form_data.get("media_type", "all")).strip() or "all",
        "min_score": _bounded_int(str(form_data.get("min_score", "0")), default=0, maximum=100),
        "include_excluded": str(form_data.get("include_excluded", "")) in {"1", "true", "on"},
        "channel_id": str(form_data.get("channel_id", "")).strip(),
        "daypart": str(form_data.get("daypart", "")).strip(),
        "playlist_id": str(form_data.get("playlist_id", "")).strip(),
        "source_playlist_id": str(form_data.get("source_playlist_id", "")).strip(),
        "playlist_mode": str(form_data.get("playlist_mode", "")).strip(),
        "assign_to_daypart": (
            str(form_data.get("assign_to_daypart", "")) in {"1", "true", "on"}
        ),
    }


def _result_view(result: RecommendationResult) -> dict[str, Any]:
    data = result.as_dict()
    data["key"] = _candidate_key(result)
    data["reason_text"] = "; ".join(result.reasons)
    data["warning_text"] = "; ".join(result.warnings)
    data["exclusion_text"] = "; ".join(result.exclusions)
    return data


async def _playlist_similarity_context(
    request: Request,
    playlist_id: str,
) -> dict[str, Any]:
    if not playlist_id:
        return {}
    repo = _playlist_repo(request)
    playlist = await repo.get(playlist_id)
    if playlist is None:
        return {}
    source_keys = {
        (item.media_type, item.media_id)
        for item in playlist.items
    }
    terms = _similarity_terms([
        playlist.name,
        playlist.description,
        playlist.category_name,
        *playlist.tags,
        *(item.title for item in playlist.items),
    ])
    media_types = Counter(item.media_type for item in playlist.items)
    runtimes: list[int] = []
    media_repo = getattr(request.app.state.core, "media_repo", None)
    if media_repo is not None:
        for entry in await media_repo.get_all_available():
            metadata = entry.metadata or {}
            series_id = str(metadata.get("series_id", ""))
            entry_key = (
                "movie" if entry.item_type == "movie" else "series",
                entry.id if entry.item_type == "movie" else series_id,
            )
            if entry_key not in source_keys:
                continue
            terms.update(_similarity_terms([
                entry.title,
                *[str(item) for item in metadata.get("genres", []) if item],
                *[str(item) for item in metadata.get("tags", []) if item],
            ]))
            if entry.duration_seconds:
                runtimes.append(entry.duration_seconds)
    avg_runtime = int(sum(runtimes) / len(runtimes)) if runtimes else None
    return {
        "playlist": playlist,
        "playlist_id": playlist.id,
        "name": playlist.name,
        "terms": sorted(terms),
        "source_keys": source_keys,
        "media_types": media_types,
        "average_runtime_seconds": avg_runtime,
        "channel_scope": playlist.channel_scope,
    }


def _apply_playlist_similarity(
    results: list[RecommendationResult],
    similarity: dict[str, Any],
) -> list[RecommendationResult]:
    source_keys = {
        (str(media_type), str(media_id))
        for media_type, media_id in similarity.get("source_keys", set())
    }
    source_terms = {
        term for term in similarity.get("terms", [])
        if len(str(term)) >= 3
    }
    media_types = similarity.get("media_types", Counter())
    if not isinstance(media_types, Counter):
        media_types = Counter()
    source_runtime = similarity.get("average_runtime_seconds")
    updated: list[RecommendationResult] = []
    for result in results:
        candidate = result.candidate
        reasons = list(result.reasons)
        warnings = list(result.warnings)
        exclusions = list(result.exclusions)
        key = (candidate.media_type, candidate.id)
        if key in source_keys:
            exclusions.append("already in source playlist")
        candidate_terms = _candidate_similarity_terms(result)
        shared = sorted(candidate_terms & source_terms)
        score = result.score
        if shared:
            points = min(30, 8 + len(shared) * 4)
            score += points
            reasons.append(
                "similar to source playlist terms: "
                f"{', '.join(shared[:6])} (+{points})",
            )
        if media_types and media_types.get(candidate.media_type):
            score += 5
            reasons.append(f"matches source playlist media type: {candidate.media_type} (+5)")
        if (
            isinstance(source_runtime, int)
            and source_runtime > 0
            and candidate.average_runtime_seconds
        ):
            difference = abs(candidate.average_runtime_seconds - source_runtime) / source_runtime
            if difference <= 0.35:
                score += 5
                reasons.append("runtime resembles source playlist average (+5)")
        updated.append(RecommendationResult(
            candidate=candidate,
            score=min(score, 100),
            reasons=reasons,
            warnings=warnings,
            exclusions=exclusions,
        ))
    updated.sort(key=lambda item: (-item.score, item.candidate.title.lower()))
    return updated


def _candidate_similarity_terms(result: RecommendationResult) -> set[str]:
    data = result.as_dict()
    return _similarity_terms([
        result.candidate.title,
        result.candidate.media_type,
        *[str(item) for item in data.get("genres", [])],
        *[str(item) for item in data.get("tags", [])],
        *[str(item) for item in data.get("manual_terms", [])],
    ])


def _similarity_terms(values: list[str]) -> set[str]:
    stop_words = {"and", "the", "with", "from", "playlist", "recommended"}
    terms: set[str] = set()
    for value in values:
        normalized = " ".join(str(value).strip().lower().split())
        if not normalized:
            continue
        terms.add(normalized)
        for part in normalized.replace("-", " ").replace("/", " ").split(" "):
            if len(part) >= 3 and part not in stop_words:
                terms.add(part)
    return terms


def _candidate_key(result: RecommendationResult) -> str:
    candidate = result.candidate
    return f"{candidate.media_type}:{candidate.id}"


def _playlist_repo(request: Request) -> Any:
    repo = getattr(request.app.state.core, "playlist_repo", None)
    if repo is None:
        raise RuntimeError("Playlist repository is unavailable")
    return repo


def _run_repo(request: Request) -> Any:
    repo = getattr(request.app.state.core, "recommendation_run_repo", None)
    if repo is None:
        db = getattr(request.app.state.core, "db", None)
        if db is None:
            raise RuntimeError("Recommendation run repository is unavailable")
        from tunarr_autoscheduler.db.repositories.recommendation_run_repo import (
            RecommendationRunRepository,
        )
        repo = RecommendationRunRepository(db)
        request.app.state.core.recommendation_run_repo = repo
    return repo


def _default_playlist_name(profile_id: str, context: dict[str, Any] | None = None) -> str:
    if context and context.get("channel") and context.get("daypart"):
        channel = context["channel"]
        daypart = context["daypart"]
        return f"{channel.name or channel.id} - {daypart.name} Recommendations"
    profile = BUILT_IN_PROFILES.get(profile_id)
    label = profile.name if profile else profile_id
    return f"Recommended - {label}"


def _default_tags(profile_id: str, context: dict[str, Any] | None = None) -> list[str]:
    tags = ["recommended", profile_id]
    if context and context.get("channel"):
        tags.append(_slug(str(context["channel"].name or context["channel"].id)))
    if context and context.get("daypart"):
        tags.append(_slug(str(context["daypart"].name)))
    return tags


def _default_category_name(
    form: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> str:
    if context and context.get("channel"):
        channel = context["channel"]
        return str(channel.name or channel.id).strip()
    profile = BUILT_IN_PROFILES.get(str(form.get("profile", "")))
    if profile:
        return profile.name
    return str(form.get("profile") or "Recommendations")


async def _ensure_recommendation_category(
    repo: Any,
    form: dict[str, Any],
    request: Request,
) -> str:
    context = _recommendation_context(request, form)
    name = _default_category_name(form, context)
    normalized_name = " ".join(name.lower().split())
    if hasattr(repo, "list_categories"):
        for category in await repo.list_categories():
            if " ".join(str(category.name).lower().split()) == normalized_name:
                return str(category.id)
    if hasattr(repo, "create_category"):
        category = await repo.create_category(
            name,
            "Automatically created from recommendation playlist generation.",
        )
        return str(category.id)
    return ""


def _auto_recommendation_tags(
    submitted_tags: list[str],
    form: dict[str, Any],
    selected_results: list[RecommendationResult],
    request: Request,
) -> list[str]:
    context = _recommendation_context(request, form)
    tags = [
        *submitted_tags,
        *_default_tags(str(form.get("profile", "")), context),
    ]
    term_counts: Counter[str] = Counter()
    for result in selected_results:
        data = result.as_dict()
        for key in ["genres", "tags", "manual_terms"]:
            for value in data.get(key, []):
                text = " ".join(str(value).strip().lower().split())
                if text and len(text) <= 32:
                    term_counts[text] += 1
    tags.extend(term for term, _count in term_counts.most_common(5))
    return _parse_tags(", ".join(tags))


async def _all_profiles(request: Request) -> dict[str, RecommendationProfile]:
    profiles = dict(BUILT_IN_PROFILES)
    repo = _profile_repo(request)
    for profile in await repo.list_all():
        profiles[profile.id] = profile
    return profiles


def _recommendation_context(request: Request, form: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(form.get("channel_id", "")).strip()
    daypart_name = str(form.get("daypart", "")).strip()
    channel = _find_channel(request, channel_id)
    daypart = _find_daypart(channel, daypart_name) if channel else None
    if not channel:
        return {}
    return {
        "channel": channel,
        "daypart": daypart,
        "channel_id": channel.id,
        "daypart_name": daypart.name if daypart else daypart_name,
        "return_url": f"/channels/{channel.id}/config?return_to=/channels/{channel.id}#dayparts",
    }


def _infer_context_defaults(
    request: Request,
    channel_id: str,
    daypart_name: str,
) -> dict[str, str]:
    channel = _find_channel(request, channel_id)
    daypart = _find_daypart(channel, daypart_name) if channel else None
    if daypart is None:
        return {"profile": "prime-time-movies", "media_type": "all"}
    name = daypart.name.lower()
    if daypart.off_air:
        return {"profile": "standby-off-air", "media_type": "all"}
    if any(term in name for term in ("documentary", "docu", "doku")):
        return {"profile": "documentary", "media_type": "series"}
    if any(term in name for term in ("kid", "family", "children")):
        return {"profile": "kids-family", "media_type": "all"}
    if "morning" in name:
        return {"profile": "morning-sitcoms", "media_type": "series"}
    if any(term in name for term in ("afternoon", "daytime")):
        return {"profile": "afternoon-family", "media_type": "series"}
    if any(term in name for term in ("late", "night", "crime", "horror", "sci")):
        return {"profile": "late-night-genre", "media_type": "series"}
    if daypart.content_mode == "movies" or daypart.allow_movies:
        if _time_hour(daypart.start_time) >= 18:
            return {"profile": "prime-time-movies", "media_type": "movie"}
        return {"profile": "movie-channel-pool", "media_type": "movie"}
    if channel.channel_profile == "movie_channel":
        return {"profile": "movie-channel-pool", "media_type": "movie"}
    if channel.channel_profile == "series_marathon":
        return {"profile": "series-marathon", "media_type": "series"}
    return {"profile": "series-marathon", "media_type": "series"}


def _assign_playlist_to_daypart(
    request: Request,
    form: dict[str, Any],
    playlist_id: str,
) -> bool:
    channel = _find_channel(request, str(form.get("channel_id", "")))
    daypart = _find_daypart(channel, str(form.get("daypart", ""))) if channel else None
    if channel is None or daypart is None:
        return False
    if playlist_id not in daypart.playlist_ids:
        daypart.playlist_ids.append(playlist_id)
    request.app.state.core.config_manager.save()
    return True


def _builder_query_form(request: Request) -> dict[str, Any]:
    source_category = request.query_params.get("source_category", "").strip()
    source_tag = request.query_params.get("source_tag", "").strip()
    raw: dict[str, Any] = {
        "mode": request.query_params.get("mode", "channel"),
        "builder_mode": request.query_params.get("builder_mode", "scratch"),
        "channel_id": request.query_params.get("channel_id", "").strip(),
        "channel_name": request.query_params.get("channel_name", "").strip(),
        "profile": request.query_params.get("profile", "series-marathon"),
        "themes": request.query_params.get("themes", "").strip(),
        "seed": request.query_params.get("seed", "").strip(),
        "language_rule": request.query_params.get("language_rule", "profile_default"),
        "per_theme_limit": _bounded_int(
            request.query_params.get("per_theme_limit"), default=12, maximum=100,
        ),
        "balance_mode": request.query_params.get("balance_mode", "tv_balanced"),
        "max_movies_per_theme": _bounded_optional_int(
            request.query_params.get("max_movies_per_theme"), maximum=100,
        ),
        "min_series_per_theme": _bounded_int(
            request.query_params.get("min_series_per_theme"), default=3, maximum=100,
        ),
        "create_channel": request.query_params.get("create_channel") in {"1", "true", "on"},
        "preview": request.query_params.get("preview") in {"1", "true", "on"},
        "source_category": source_category,
        "source_tag": source_tag,
    }
    if "replace_dayparts" in request.query_params:
        raw["replace_dayparts"] = request.query_params.get("replace_dayparts") in {
            "1", "true", "on",
        }
    return _normalize_builder_form(raw)


def _builder_posted_form(form_data: Any) -> dict[str, Any]:
    form = {
        "mode": str(form_data.get("mode", "channel")),
        "builder_mode": str(form_data.get("builder_mode", "scratch")),
        "channel_id": str(form_data.get("channel_id", "")).strip(),
        "channel_name": str(form_data.get("channel_name", "")).strip(),
        "profile": str(form_data.get("profile", "series-marathon")),
        "themes": str(form_data.get("themes", "")).strip(),
        "seed": str(form_data.get("seed", "")).strip(),
        "language_rule": str(form_data.get("language_rule", "profile_default")),
        "per_theme_limit": _bounded_int(
            str(form_data.get("per_theme_limit", "12")), default=12, maximum=100,
        ),
        "balance_mode": str(form_data.get("balance_mode", "tv_balanced")),
        "max_movies_per_theme": _bounded_optional_int(
            str(form_data.get("max_movies_per_theme", "")), maximum=100,
        ),
        "min_series_per_theme": _bounded_int(
            str(form_data.get("min_series_per_theme", "3")), default=3, maximum=100,
        ),
        "create_channel": _truthy(form_data.get("create_channel")),
        "replace_dayparts": _truthy(form_data.get("replace_dayparts")),
        "preview": True,
        "source_category": str(form_data.get("source_category", "")).strip(),
        "source_tag": str(form_data.get("source_tag", "")).strip(),
    }
    manual_dayparts = _manual_daypart_specs(form_data)
    if manual_dayparts:
        form["manual_dayparts"] = manual_dayparts
    return form


def _normalize_builder_form(raw: dict[str, Any]) -> dict[str, Any]:
    builder_mode = str(raw.get("builder_mode") or "scratch")
    if builder_mode not in {"scratch", "improve"}:
        builder_mode = "scratch"
    mode = str(raw.get("mode") or "channel")
    if mode not in {"channel", "daypart"}:
        mode = "channel"
    replace_dayparts = _truthy(raw.get("replace_dayparts"))
    if builder_mode == "improve" and "replace_dayparts" not in raw:
        replace_dayparts = True
    form = {
        "mode": mode,
        "builder_mode": builder_mode,
        "channel_id": str(raw.get("channel_id", "")).strip(),
        "channel_name": str(raw.get("channel_name", "")).strip(),
        "profile": str(raw.get("profile") or "series-marathon"),
        "themes": str(raw.get("themes", "")).strip(),
        "seed": str(raw.get("seed", "")).strip(),
        "language_rule": str(raw.get("language_rule") or "profile_default"),
        "per_theme_limit": _bounded_int(
            str(raw.get("per_theme_limit", "12")), default=12, maximum=100,
        ),
        "balance_mode": str(raw.get("balance_mode") or "tv_balanced"),
        "max_movies_per_theme": _bounded_optional_int(
            str(raw.get("max_movies_per_theme", "")), maximum=100,
        ),
        "min_series_per_theme": _bounded_int(
            str(raw.get("min_series_per_theme", "3")), default=3, maximum=100,
        ),
        "create_channel": _truthy(raw.get("create_channel")),
        "replace_dayparts": replace_dayparts,
        "preview": _truthy(raw.get("preview")),
        "source_category": str(raw.get("source_category", "")).strip(),
        "source_tag": str(raw.get("source_tag", "")).strip(),
    }
    manual_dayparts = raw.get("manual_dayparts")
    if isinstance(manual_dayparts, list):
        form["manual_dayparts"] = manual_dayparts
    return form


async def _render_builder(
    request: Request,
    form: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    saved_run_id: str = "",
    error: str = "",
    rerun_source_id: str = "",
) -> HTMLResponse:
    suggestions = await _theme_suggestions(request)
    template = request.app.state.templates.get_template("recommendation_builder.html")
    return HTMLResponse(template.render(
        request=request,
        form=form,
        plan=plan,
        suggestions=suggestions,
        channels=_channel_options(request.app.state.core),
        profiles=list((await _all_profiles(request)).values()),
        language_rules=LANGUAGE_RULES,
        saved_run_id=saved_run_id,
        rerun_source_id=rerun_source_id,
        error=error,
    ))


async def _apply_builder_source_defaults(
    request: Request,
    form: dict[str, Any],
) -> dict[str, Any]:
    source = await _builder_source_context(request, form)
    if not source:
        return form
    updated = dict(form)
    source_name = str(source.get("name", "")).strip()
    if source_name:
        if not str(updated.get("themes", "")).strip():
            updated["themes"] = source_name
        if not str(updated.get("seed", "")).strip():
            updated["seed"] = source_name
        if not str(updated.get("channel_name", "")).strip():
            updated["channel_name"] = f"{source_name} Channel"
    if str(updated.get("profile", "")) == "series-marathon":
        suggested_profile = str(source.get("profile", "")).strip()
        if suggested_profile:
            updated["profile"] = suggested_profile
    return updated


async def _builder_source_context(
    request: Request,
    form: dict[str, Any],
) -> dict[str, Any]:
    category_id = str(form.get("source_category", "")).strip()
    tag = str(form.get("source_tag", "")).strip()
    if not category_id and not tag:
        return {}
    repo = _playlist_repo(request)
    playlists = await repo.list_all()
    categories = {
        str(category.id): str(category.name)
        for category in await _playlist_categories(repo)
    }
    matching = [
        playlist for playlist in playlists
        if (
            category_id and str(getattr(playlist, "category_id", "")) == category_id
        ) or (
            tag and tag in {str(item) for item in getattr(playlist, "tags", [])}
        )
    ]
    name = categories.get(category_id, "") if category_id else tag
    terms: set[str] = {name, tag}
    media_counts: Counter[str] = Counter()
    for playlist in matching:
        terms.add(str(getattr(playlist, "name", "")))
        terms.update(str(item) for item in getattr(playlist, "tags", []))
        if getattr(playlist, "category_name", ""):
            terms.add(str(getattr(playlist, "category_name", "")))
        for item in getattr(playlist, "items", []):
            media_type = str(getattr(item, "media_type", ""))
            if media_type:
                media_counts[media_type] += 1
            if getattr(item, "title", ""):
                terms.add(str(getattr(item, "title", "")))
    return {
        "type": "category" if category_id else "tag",
        "id": category_id or tag,
        "name": name or category_id or tag,
        "playlist_count": len(matching),
        "terms": [term for term in terms if term.strip()],
        "profile": _profile_for_source(name, media_counts),
    }


async def _build_recommendation_plan(request: Request, form: dict[str, Any]) -> dict[str, Any]:
    profiles = await _all_profiles(request)
    profile_id = str(form.get("profile") or "series-marathon")
    base_profile = profiles.get(profile_id) or BUILT_IN_PROFILES["series-marathon"]
    source_context = await _builder_source_context(request, form)
    themes = _builder_themes(
        str(form.get("themes", "")),
        str(form.get("seed", "")),
        source_context,
    )
    if not themes:
        themes = [_fallback_theme(str(form.get("seed", "")), base_profile)]
    builder_mode = str(form.get("builder_mode") or "scratch")
    templates = _manual_daypart_templates(form)
    if not templates and builder_mode == "improve":
        templates = _existing_daypart_templates(
            request,
            str(form.get("channel_id", "")),
            themes,
            profile_id,
        )
    if not templates:
        templates = _daypart_templates_for_themes(
            themes,
            profile_id,
            str(form.get("mode", "channel")),
        )
    engine = await _engine(request)
    language_rule = None
    if form.get("language_rule") != "profile_default":
        language_rule = str(form.get("language_rule"))
    profile_results: dict[str, list[RecommendationResult]] = {}
    seen_media_ids: set[str] = set()
    seen_titles: set[str] = set()

    dayparts: list[dict[str, Any]] = []
    for index, template in enumerate(templates):
        theme = template["theme"]
        template_profile_id = str(template.get("profile") or profile_id)
        profile = profiles.get(template_profile_id) or base_profile
        per_theme_limit = int(form.get("per_theme_limit", 12))
        if profile.id not in profile_results:
            profile_results[profile.id] = await engine.run(
                profile.id,
                limit=10_000,
                language_rule=language_rule,
            )
        selected = _select_theme_results(
            profile_results[profile.id],
            theme,
            per_theme_limit,
            str(form.get("seed", "")),
            source_terms=source_context.get("terms", []),
            max_movies=_builder_movie_limit(
                profile,
                theme,
                per_theme_limit,
                str(form.get("balance_mode", "tv_balanced")),
                form.get("max_movies_per_theme"),
            ),
            seen_media_ids=seen_media_ids,
            seen_titles=seen_titles,
        )
        min_series = int(form.get("min_series_per_theme", 3))
        warnings = _builder_daypart_warnings(selected, profile, theme, min_series)
        seen_media_ids.update(result.candidate.id for result in selected)
        seen_titles.update(_normalized_title(result.candidate.title) for result in selected)
        dayparts.append({
            **template,
            "playlist_name": f"{_plan_channel_name(form)} - {template['name']}",
            "playlist_tags": ["recommended", _slug(theme), profile.id],
            "profile": profile.id,
            "profile_name": profile.name,
            "content_mode": template.get("content_mode") or _content_mode_for_profile(profile.id),
            "allow_movies": bool(
                template.get("allow_movies", _profile_allows_movies(profile.id)),
            ),
            "items": [_result_to_plan_item(result) for result in selected],
            "warnings": warnings,
            "balance": {
                "mode": form.get("balance_mode", "tv_balanced"),
                "max_movies": _builder_movie_limit(
                    profile,
                    theme,
                    per_theme_limit,
                    str(form.get("balance_mode", "tv_balanced")),
                    form.get("max_movies_per_theme"),
                ),
                "min_series": min_series,
            },
        })
    return {
        "title": f"{_plan_channel_name(form)} recommendation plan",
        "mode": form.get("mode", "channel"),
        "builder_mode": builder_mode,
        "builder_mode_label": (
            "Improve existing config" if builder_mode == "improve" else "Start from scratch"
        ),
        "channel_id": form.get("channel_id", ""),
        "channel_name": _plan_channel_name(form),
        "profile": profile_id,
        "profile_name": (
            "Auto profile per theme" if profile_id == "auto" else base_profile.name
        ),
        "themes": themes,
        "seed": form.get("seed", ""),
        "source_context": source_context,
        "balance_mode": form.get("balance_mode", "tv_balanced"),
        "max_movies_per_theme": form.get("max_movies_per_theme"),
        "min_series_per_theme": form.get("min_series_per_theme", 3),
        "dayparts": dayparts,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }


def _builder_themes(
    raw_themes: str,
    raw_seed: str,
    source_context: dict[str, Any] | None = None,
) -> list[str]:
    themes = [
        item.strip()
        for item in raw_themes.replace("\n", ",").split(",")
        if item.strip()
    ]
    if not themes and source_context:
        source_name = str(source_context.get("name", "")).strip()
        themes = [source_name] if source_name else []
    if not themes:
        themes = [raw_seed.strip()] if raw_seed.strip() else []
    normalized: list[str] = []
    seen: set[str] = set()
    for theme in themes:
        key = theme.lower()
        if key in seen:
            continue
        normalized.append(theme)
        seen.add(key)
    return normalized[:8]


def _manual_daypart_specs(form_data: Any) -> list[dict[str, str]]:
    try:
        count = max(0, min(8, int(str(form_data.get("daypart_count", "0")))))
    except ValueError:
        count = 0
    specs: list[dict[str, str]] = []
    for index in range(count):
        if not _truthy(form_data.get(f"daypart_enabled_{index}", "1")):
            continue
        theme = str(form_data.get(f"daypart_theme_{index}", "")).strip()
        name = str(form_data.get(f"daypart_name_{index}", "")).strip()
        if not theme and not name:
            continue
        specs.append({
            "name": name or _title_label(theme),
            "theme": theme or name,
            "start_time": str(form_data.get(f"daypart_start_{index}", "")).strip() or "06:00",
            "end_time": str(form_data.get(f"daypart_end_{index}", "")).strip() or "12:00",
            "profile": str(form_data.get(f"daypart_profile_{index}", "")).strip() or "auto",
        })
    return specs


def _manual_daypart_templates(form: dict[str, Any]) -> list[dict[str, Any]]:
    manual = form.get("manual_dayparts")
    if not isinstance(manual, list):
        return []
    templates: list[dict[str, Any]] = []
    for index, item in enumerate(manual):
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme", "")).strip()
        requested_profile = str(item.get("profile", "auto")).strip() or "auto"
        resolved_profile = _infer_builder_profile(theme, requested_profile, index)
        templates.append({
            "name": str(item.get("name", "")).strip() or _title_label(theme),
            "theme": theme,
            "start_time": str(item.get("start_time", "")).strip() or "06:00",
            "end_time": str(item.get("end_time", "")).strip() or "12:00",
            "profile": resolved_profile,
            "content_mode": _content_mode_for_profile(resolved_profile),
            "allow_movies": _profile_allows_movies(resolved_profile),
        })
    return templates


def _daypart_templates_for_themes(
    themes: list[str],
    profile_id: str,
    mode: str,
) -> list[dict[str, Any]]:
    if mode == "daypart":
        resolved_profile = _infer_builder_profile(themes[0], profile_id, 0)
        return [{
            "name": _title_label(themes[0]),
            "theme": themes[0],
            "start_time": "18:00",
            "end_time": "22:00",
            "profile": resolved_profile,
            "content_mode": _content_mode_for_profile(resolved_profile),
            "allow_movies": _profile_allows_movies(resolved_profile),
        }]
    windows = [
        ("Morning", "06:00", "11:00"),
        ("Daytime", "11:00", "17:00"),
        ("Prime", "17:00", "22:30"),
        ("Late", "22:30", "02:00"),
    ]
    if len(themes) == 1:
        themes = themes * len(windows)
    result: list[dict[str, Any]] = []
    for index, theme in enumerate(themes[:len(windows)]):
        label, start, end = windows[index]
        resolved_profile = _infer_builder_profile(theme, profile_id, index)
        result.append({
            "name": f"{label} {_title_label(theme)}",
            "theme": theme,
            "start_time": start,
            "end_time": end,
            "profile": resolved_profile,
            "content_mode": _content_mode_for_profile(resolved_profile),
            "allow_movies": _profile_allows_movies(resolved_profile),
        })
    return result


def _existing_daypart_templates(
    request: Request,
    channel_id: str,
    themes: list[str],
    profile_id: str,
) -> list[dict[str, Any]]:
    channel = _find_channel(request, channel_id)
    if channel is None:
        return []
    dayparts = [daypart for daypart in channel.dayparts if not daypart.off_air]
    if not dayparts:
        return []
    templates: list[dict[str, Any]] = []
    for index, daypart in enumerate(dayparts[:8]):
        theme = themes[index % len(themes)] if themes else daypart.name
        requested_profile = _profile_for_existing_daypart(daypart, profile_id)
        resolved_profile = _infer_builder_profile(theme, requested_profile, index)
        templates.append({
            "name": daypart.name,
            "theme": theme,
            "start_time": daypart.start_time,
            "end_time": daypart.end_time,
            "profile": resolved_profile,
            "content_mode": daypart.content_mode or _content_mode_for_profile(resolved_profile),
            "allow_movies": daypart.allow_movies or _profile_allows_movies(resolved_profile),
        })
    return templates


def _profile_for_existing_daypart(daypart: Any, fallback_profile: str) -> str:
    if fallback_profile and fallback_profile != "auto":
        return fallback_profile
    if getattr(daypart, "content_mode", "") == "movies" or getattr(daypart, "allow_movies", False):
        return "movie-channel-pool"
    return "auto"


def _profile_for_source(name: str, media_counts: Counter[str]) -> str:
    if media_counts.get("movie", 0) > media_counts.get("series", 0) * 2:
        return "movie-channel-pool"
    return _infer_builder_profile(name, "auto", 0)


def _infer_builder_profile(theme: str, requested_profile: str, index: int) -> str:
    if requested_profile and requested_profile != "auto":
        return requested_profile
    text = theme.lower()
    if "anime" in text:
        if any(term in text for term in ("movie", "film", "ova")):
            return "anime-movies"
        return "anime-series"
    if any(term in text for term in ("sci-fi", "scifi", "science fiction", "space")):
        return "late-night-genre"
    if "mystery" in text:
        return "late-night-genre"
    documentary_terms = (
        "documentary", "documentaries", "docu", "doku", "nature", "history",
    )
    if any(term in text for term in documentary_terms):
        return "documentary"
    if any(term in text for term in ("kid", "kids", "family", "children", "cartoon")):
        return "kids-family"
    if any(term in text for term in ("holiday", "christmas", "halloween", "event")):
        return "holiday-event"
    if any(term in text for term in ("standby", "off-air", "off air", "loop")):
        return "standby-off-air"
    if any(term in text for term in ("movie", "movies", "film", "films", "cinema", "blockbuster")):
        return "prime-time-movies" if index >= 2 else "movie-channel-pool"
    late_terms = ("crime", "horror", "thriller", "late", "night")
    if any(term in text for term in late_terms):
        return "late-night-genre"
    if any(term in text for term in ("morning", "sitcom", "comedy", "light")) and index == 0:
        return "morning-sitcoms"
    if index == 1:
        return "afternoon-family"
    return "series-marathon"


def _fallback_theme(seed: str, profile: RecommendationProfile) -> str:
    return seed.strip() or profile.name


def _select_theme_results(
    results: list[RecommendationResult],
    theme: str,
    limit: int,
    seed: str,
    *,
    source_terms: list[str] | None = None,
    max_movies: int | None = None,
    seen_media_ids: set[str] | None = None,
    seen_titles: set[str] | None = None,
) -> list[RecommendationResult]:
    terms = _theme_terms(theme)
    phrases = _theme_phrases(theme)
    seed_terms = _split_terms(seed)
    seed_phrases = [
        item.strip().lower()
        for item in seed.replace("\n", ",").split(",")
        if item.strip()
    ]
    source_phrases = [
        _normalized_text(term)
        for term in (source_terms or [])
        if len(str(term).strip()) >= 3
    ]

    def theme_relevance(result: RecommendationResult) -> int:
        haystack = _candidate_haystack(result)
        score = sum(3 for term in terms if term in haystack)
        score += sum(10 for phrase in phrases if phrase and phrase in haystack)
        return score

    def seed_relevance(result: RecommendationResult) -> int:
        haystack = _candidate_haystack(result)
        title = _normalized_text(result.candidate.title)
        score = sum(1 for term in seed_terms if term in haystack)
        score += sum(4 for phrase in seed_phrases if phrase and _normalized_text(phrase) in title)
        score += sum(6 for phrase in source_phrases if phrase and phrase in haystack)
        return score

    accepted = [result for result in results if result.accepted]
    theme_matched = [result for result in accepted if theme_relevance(result) > 0]
    candidates = theme_matched or accepted

    def ranking(result: RecommendationResult) -> tuple[int, int, int, str]:
        return (
            -theme_relevance(result),
            -seed_relevance(result),
            -result.score,
            result.candidate.title.lower(),
        )

    ranked = sorted(candidates, key=ranking)
    if max_movies is None and not seen_media_ids and not seen_titles:
        return ranked[:limit]

    seen_media_ids = seen_media_ids or set()
    seen_titles = seen_titles or set()
    fresh = [
        result for result in ranked
        if result.candidate.id not in seen_media_ids
        and _normalized_title(result.candidate.title) not in seen_titles
    ]
    selected = _select_with_movie_limit(fresh, limit, max_movies)
    if len(selected) >= limit:
        return selected[:limit]
    selected_ids = {result.candidate.id for result in selected}
    movie_count = sum(1 for result in selected if result.candidate.media_type == "movie")
    for result in ranked:
        if len(selected) >= limit:
            break
        if result.candidate.id in selected_ids:
            continue
        if (
            max_movies is not None
            and result.candidate.media_type == "movie"
            and movie_count >= max_movies
        ):
            continue
        selected.append(result)
        selected_ids.add(result.candidate.id)
        if result.candidate.media_type == "movie":
            movie_count += 1
    return selected[:limit]


def _select_with_movie_limit(
    results: list[RecommendationResult],
    limit: int,
    max_movies: int | None,
) -> list[RecommendationResult]:
    selected: list[RecommendationResult] = []
    selected_ids: set[str] = set()
    movie_count = 0
    for result in results:
        if len(selected) >= limit:
            break
        if result.candidate.id in selected_ids:
            continue
        if (
            max_movies is not None
            and result.candidate.media_type == "movie"
            and movie_count >= max_movies
        ):
            continue
        selected.append(result)
        selected_ids.add(result.candidate.id)
        if result.candidate.media_type == "movie":
            movie_count += 1
    return selected


def _candidate_haystack(result: RecommendationResult) -> str:
    data = result.as_dict()
    return _normalized_text(" ".join([
        result.candidate.title,
        " ".join(str(value) for value in data.get("genres", [])),
        " ".join(str(value) for value in data.get("tags", [])),
        " ".join(str(value) for value in data.get("manual_terms", [])),
        " ".join(result.reasons),
    ]))


def _split_terms(value: str) -> list[str]:
    normalized = _normalized_text(value)
    return [term for term in normalized.split() if len(term.strip()) >= 3]


def _theme_terms(value: str) -> list[str]:
    terms = _split_terms(value)
    text = _normalized_text(value)
    if "sci fi" in text or "scifi" in text or "science fiction" in text:
        terms.extend(["sci", "fiction", "space"])
    return list(dict.fromkeys(terms))


def _theme_phrases(value: str) -> list[str]:
    text = _normalized_text(value)
    phrases = [text] if len(text) >= 3 else []
    if "science fiction" in text or "sci fi" in text or "scifi" in text:
        phrases.extend(["science fiction", "sci fi", "sci-fi", "scifi"])
    return list(dict.fromkeys(_normalized_text(phrase) for phrase in phrases if phrase))


def _normalized_text(value: str) -> str:
    return value.replace(",", " ").replace("-", " ").replace("_", " ").lower()


def _normalized_title(value: str) -> str:
    return " ".join(_normalized_text(value).split())


def _result_to_plan_item(result: RecommendationResult) -> dict[str, Any]:
    return {
        "media_type": result.candidate.media_type,
        "media_id": result.candidate.id,
        "title": result.candidate.title,
        "score": result.score,
        "reasons": result.reasons[:5],
    }


async def _apply_recommendation_plan(
    request: Request,
    request_data: dict[str, Any],
    plan: dict[str, Any],
) -> ChannelConfig:
    core = request.app.state.core
    playlist_repo = _playlist_repo(request)
    return await apply_recommendation_plan_to_core(core, playlist_repo, request_data, plan)


async def apply_recommendation_plan_to_core(
    core: Any,
    playlist_repo: Any,
    request_data: dict[str, Any],
    plan: dict[str, Any],
) -> ChannelConfig:
    channel_id = str(request_data.get("channel_id") or plan.get("channel_id") or "").strip()
    channel = cast(ChannelConfig | None, _find_channel_in_core(core, channel_id))
    if channel is None and request_data.get("create_channel"):
        channel_id = _slug(str(plan.get("channel_name") or "recommended-channel"))
        channel = ChannelConfig(
            id=channel_id,
            name=str(plan.get("channel_name") or channel_id),
            scheduling_enabled=True,
            channel_profile=_channel_profile_for_plan(plan),
        )
        core.config_manager.config().channels.append(channel)
    if channel is None:
        raise RuntimeError("No target channel selected for recommendation plan")

    dayparts: list[DaypartTemplate] = []
    for daypart in plan.get("dayparts", []):
        if not isinstance(daypart, dict):
            continue
        items = [
            PlaylistItem(
                media_type=item["media_type"],
                media_id=item["media_id"],
                title=item["title"],
                position=index,
            )
            for index, item in enumerate(daypart.get("items", []))
            if isinstance(item, dict) and item.get("media_type") in {"series", "movie"}
        ]
        playlist = await playlist_repo.create(
            name=str(daypart.get("playlist_name") or daypart.get("name") or "Recommended"),
            description=(
                f"Generated from recommendation run for theme "
                f"{daypart.get('theme', '')}."
            ),
            channel_scope=channel.id,
            tags=[str(tag) for tag in daypart.get("playlist_tags", [])],
            items=items,
        )
        dayparts.append(DaypartTemplate(
            name=str(daypart.get("name") or "recommended"),
            days=list(DayOfWeek),
            start_time=str(daypart.get("start_time") or "06:00"),
            end_time=str(daypart.get("end_time") or "12:00"),
            content_mode=str(daypart.get("content_mode") or "series"),
            allow_movies=bool(daypart.get("allow_movies")),
            variable_movie_duration=bool(daypart.get("allow_movies")),
            movie_selection="library_random" if daypart.get("allow_movies") else "best_fit",
            playlist_ids=[playlist.id],
        ))
    if request_data.get("replace_dayparts", True):
        channel.dayparts = dayparts
    else:
        channel.dayparts.extend(dayparts)
    core.config_manager.save()
    return channel


async def _theme_suggestions(request: Request) -> list[dict[str, Any]]:
    entries = await request.app.state.core.media_repo.get_all_available()
    counter: Counter[str] = Counter()
    for entry in entries:
        metadata = entry.metadata or {}
        for key in ["genres", "tags"]:
            raw = metadata.get(key)
            if isinstance(raw, list):
                counter.update(str(item).strip() for item in raw if str(item).strip())
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(24)
    ]


def _plan_channel_name(form: dict[str, Any]) -> str:
    return str(form.get("channel_name") or "Recommended Channel").strip()


def _content_mode_for_profile(profile_id: str) -> str:
    return "movies" if _profile_allows_movies(profile_id) else "series"


def _profile_allows_movies(profile_id: str) -> bool:
    profile = BUILT_IN_PROFILES.get(profile_id)
    return bool(profile and "movie" in profile.media_types and "series" not in profile.media_types)


def _builder_daypart_warnings(
    selected: list[RecommendationResult],
    profile: RecommendationProfile,
    theme: str,
    min_series: int,
) -> list[str]:
    warnings: list[str] = []
    if len(selected) < max(3, min(profile.min_items, 8)):
        warnings.append(f"Only {len(selected)} matching item(s) found for {theme}.")
    if "series" in profile.media_types:
        series_count = sum(1 for result in selected if result.candidate.media_type == "series")
        if series_count < min_series:
            warnings.append(
                f"Only {series_count} series candidate(s) found; target is {min_series}."
            )
    return warnings


def _builder_movie_limit(
    profile: RecommendationProfile,
    theme: str,
    limit: int,
    balance_mode: str,
    custom_max_movies: object,
) -> int | None:
    if "movie" not in profile.media_types:
        return 0
    if "series" not in profile.media_types:
        return None
    if custom_max_movies is not None:
        parsed_custom = _bounded_optional_int(str(custom_max_movies), maximum=limit)
        return parsed_custom if parsed_custom is not None else None
    if balance_mode == "series_only":
        return 0
    if balance_mode == "series_heavy":
        return max(1, limit // 8)
    if balance_mode == "mixed":
        return max(1, limit // 4)
    if balance_mode == "movie_friendly":
        return max(1, limit // 2)
    if _theme_requests_movie_block(theme):
        return max(1, limit // 3)
    return max(1, limit // 6)


def _theme_requests_movie_block(theme: str) -> bool:
    text = _normalized_text(theme)
    return any(
        term in text
        for term in ("movie", "movies", "film", "films", "cinema", "blockbuster")
    )


def _channel_profile_for_plan(plan: dict[str, Any]) -> str:
    profile = str(plan.get("profile", ""))
    if profile == "movie-channel-pool":
        return "movie_channel"
    if profile == "series-marathon":
        return "series_marathon"
    return "general_tv"


def _title_label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", " ").split()) or "Theme"


def _find_channel(request: Request, channel_id: str) -> Any:
    return _find_channel_in_core(request.app.state.core, channel_id)


def _find_channel_in_core(core: Any, channel_id: str) -> Any:
    if not channel_id:
        return None
    channels = core.config_manager.config().channels
    return next((channel for channel in channels if channel.id == channel_id), None)


def _find_daypart(channel: Any, daypart_name: str) -> Any:
    if channel is None or not daypart_name:
        return None
    return next(
        (
            daypart for daypart in channel.dayparts
            if daypart.name.lower() == daypart_name.lower()
        ),
        None,
    )


def _time_hour(value: str) -> int:
    try:
        return int(value.split(":", 1)[0])
    except (ValueError, IndexError):
        return 0


def _profile_repo(request: Request) -> RecommendationProfileRepository:
    repo = getattr(request.app.state.core, "recommendation_profile_repo", None)
    if repo is not None:
        return cast(RecommendationProfileRepository, repo)
    db = getattr(request.app.state.core, "db", None)
    if db is None:
        raise RuntimeError("Recommendation profile repository is unavailable")
    repo = RecommendationProfileRepository(db)
    request.app.state.core.recommendation_profile_repo = repo
    return repo


def _profile_from_form(form: Any, *, existing_id: str) -> RecommendationProfile:
    raw_id = existing_id or str(form.get("id", "")).strip()
    profile_id = _slug(raw_id or str(form.get("name", "custom-profile")))
    return RecommendationProfile(
        id=profile_id,
        name=str(form.get("name", "")).strip() or profile_id,
        media_types=tuple(_form_list(form.getlist("media_types"))) or ("movie",),
        preferred_genres=tuple(_csv_list(str(form.get("preferred_genres", "")))),
        preferred_tags=tuple(_csv_list(str(form.get("preferred_tags", "")))),
        required_terms=tuple(_csv_list(str(form.get("required_terms", "")))),
        excluded_genres=tuple(_csv_list(str(form.get("excluded_genres", "")))),
        min_runtime_minutes=_form_optional_int(form.get("min_runtime_minutes")),
        max_runtime_minutes=_form_optional_int(form.get("max_runtime_minutes")),
        min_items=max(1, _form_optional_int(form.get("min_items")) or 1),
        language_rule=str(form.get("language_rule", "none")),
        description=str(form.get("description", "")).strip(),
        weights={
            "genre": _form_optional_int(form.get("weight_genre")) or 25,
            "runtime": _form_optional_int(form.get("weight_runtime")) or 20,
            "depth": _form_optional_int(form.get("weight_depth")) or 15,
            "language": _form_optional_int(form.get("weight_language")) or 0,
            "metadata": _form_optional_int(form.get("weight_metadata")) or 10,
            "rating": _form_optional_int(form.get("weight_rating")) or 0,
        },
    )


def _csv_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _form_list(values: list[Any]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


def _form_optional_int(value: object) -> int | None:
    if value in {"", None}:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _slug(value: str) -> str:
    text = value.strip().lower()
    result = []
    previous_dash = False
    for char in text:
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    return "".join(result).strip("-") or "custom-profile"


def _query_string(form: dict[str, Any], *, error: str = "") -> str:
    query = {
        "profile": form["profile"],
        "language_rule": form["language_rule"],
        "limit": str(form["limit"]),
        "q": form["q"],
        "exclude_q": form.get("exclude_q", ""),
        "media_type": form["media_type"],
        "min_score": str(form["min_score"]),
    }
    if form["include_excluded"]:
        query["include_excluded"] = "1"
    if form.get("channel_id"):
        query["channel_id"] = str(form["channel_id"])
    if form.get("daypart"):
        query["daypart"] = str(form["daypart"])
    if form.get("playlist_id"):
        query["playlist_id"] = str(form["playlist_id"])
    if form.get("source_playlist_id"):
        query["source_playlist_id"] = str(form["source_playlist_id"])
    if form.get("playlist_mode"):
        query["playlist_mode"] = str(form["playlist_mode"])
    if form.get("assign_to_daypart"):
        query["assign_to_daypart"] = "1"
    if error:
        query["error"] = error
    return urlencode(query)


def _merge_playlist_items(
    existing_items: list[PlaylistItem],
    new_items: list[PlaylistItem],
) -> list[PlaylistItem]:
    merged: list[PlaylistItem] = []
    seen: set[tuple[str, str]] = set()
    for item in [*existing_items, *new_items]:
        key = (item.media_type, item.media_id)
        if key in seen:
            continue
        merged.append(item.model_copy(update={"position": len(merged)}))
        seen.add(key)
    return merged


def _split_search_query(query: str) -> tuple[str, list[str]]:
    positive_terms: list[str] = []
    excluded_terms: list[str] = []
    for raw_term in query.split():
        term = raw_term.strip()
        if not term:
            continue
        if term.startswith("-") and len(term) > 1:
            excluded_terms.append(term[1:])
        elif term.startswith("not:") and len(term) > 4:
            excluded_terms.append(term[4:])
        else:
            positive_terms.append(term)
    return " ".join(positive_terms), excluded_terms


def _bounded_int(value: str | None, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return max(1, min(maximum, parsed))


def _bounded_optional_int(value: str | None, *, maximum: int) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return max(0, min(maximum, parsed))


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
