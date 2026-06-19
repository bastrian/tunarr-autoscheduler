from __future__ import annotations

from dataclasses import dataclass, field
from heapq import nsmallest
from statistics import mean
from typing import Any

from tunarr_autoscheduler.db.repositories.media_repo import MediaRepository
from tunarr_autoscheduler.models.schedule import MediaCacheEntry
from tunarr_autoscheduler.recommendations.language import (
    has_english_audio,
    has_english_subtitles,
    normalize_language,
)
from tunarr_autoscheduler.recommendations.profiles import (
    BUILT_IN_PROFILES,
    RecommendationProfile,
)


@dataclass
class RecommendationCandidate:
    id: str
    media_type: str
    title: str
    item_count: int
    average_runtime_seconds: int | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecommendationResult:
    candidate: RecommendationCandidate
    score: int
    reasons: list[str]
    warnings: list[str]
    exclusions: list[str]

    @property
    def accepted(self) -> bool:
        return not self.exclusions

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.candidate.id,
            "media_type": self.candidate.media_type,
            "title": self.candidate.title,
            "score": self.score,
            "accepted": self.accepted,
            "item_count": self.candidate.item_count,
            "average_runtime_minutes": (
                round(self.candidate.average_runtime_seconds / 60, 1)
                if self.candidate.average_runtime_seconds
                else None
            ),
            "genres": self.candidate.metadata.get("genres", []),
            "tags": self.candidate.metadata.get("tags", []),
            "audio_languages": self.candidate.metadata.get("audio_languages", []),
            "subtitle_languages": self.candidate.metadata.get("subtitle_languages", []),
            "manual_terms": self.candidate.metadata.get("manual_terms", []),
            "provider_ids": self.candidate.metadata.get("provider_ids", {}),
            "reasons": self.reasons,
            "warnings": self.warnings,
            "exclusions": self.exclusions,
        }


class RecommendationEngine:
    def __init__(
        self,
        media_repo: MediaRepository,
        manual_terms_by_media_id: dict[str, list[str]] | None = None,
        profiles: dict[str, RecommendationProfile] | None = None,
        external_signals_by_media_id: dict[str, dict[str, Any]] | None = None,
        signal_weights: dict[str, int] | None = None,
    ):
        self._media_repo = media_repo
        self._manual_terms_by_media_id = manual_terms_by_media_id or {}
        self._profiles = profiles or BUILT_IN_PROFILES
        self._external_signals_by_media_id = external_signals_by_media_id or {}
        self._signal_weights = signal_weights or {}

    async def diagnostics(self) -> dict[str, Any]:
        entries = await self._media_repo.get_all_available()
        metadata_fields = {
            "genres": 0,
            "tags": 0,
            "provider_ids": 0,
            "audio_languages": 0,
            "subtitle_languages": 0,
            "parental_rating": 0,
            "overview": 0,
            "year": 0,
        }
        by_type: dict[str, int] = {}
        for entry in entries:
            by_type[entry.item_type] = by_type.get(entry.item_type, 0) + 1
            metadata = entry.metadata or {}
            for key in metadata_fields:
                if metadata.get(key):
                    metadata_fields[key] += 1
        total = len(entries)
        return {
            "total_available": total,
            "by_type": by_type,
            "metadata_coverage": {
                key: {
                    "count": count,
                    "percent": round((count / total) * 100, 1) if total else 0.0,
                }
                for key, count in metadata_fields.items()
            },
            "profiles": [
                {"id": profile.id, "name": profile.name}
                for profile in self._profiles.values()
            ],
        }

    async def run(
        self,
        profile_id: str,
        *,
        limit: int = 20,
        include_excluded: bool = False,
        language_rule: str | None = None,
    ) -> list[RecommendationResult]:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Unknown recommendation profile: {profile_id}")
        if language_rule:
            profile = RecommendationProfile(
                id=profile.id,
                name=profile.name,
                media_types=profile.media_types,
                preferred_genres=profile.preferred_genres,
                preferred_tags=profile.preferred_tags,
                required_terms=profile.required_terms,
                excluded_genres=profile.excluded_genres,
                min_runtime_minutes=profile.min_runtime_minutes,
                max_runtime_minutes=profile.max_runtime_minutes,
                min_items=profile.min_items,
                language_rule=language_rule,
                description=profile.description,
                weights=profile.weights,
            )

        entries = await self._media_repo.get_all_available()
        candidates = _build_candidates(
            entries,
            self._manual_terms_by_media_id,
            self._external_signals_by_media_id,
        )
        results = [
            _score_candidate(candidate, profile, self._signal_weights)
            for candidate in candidates
            if candidate.media_type in profile.media_types
        ]
        if not include_excluded:
            results = [result for result in results if result.accepted]
        key = _result_sort_key
        if limit > 0 and len(results) > limit * 4:
            return nsmallest(limit, results, key=key)
        results.sort(key=key)
        return results[:limit]

    async def explain(
        self,
        item_id: str,
        profile_id: str,
        *,
        language_rule: str | None = None,
    ) -> RecommendationResult | None:
        results = await self.run(
            profile_id,
            limit=10_000,
            include_excluded=True,
            language_rule=language_rule,
        )
        for result in results:
            candidate = result.candidate
            if candidate.id == item_id:
                return result
            provider_ids = candidate.metadata.get("provider_ids")
            if isinstance(provider_ids, dict) and item_id in {
                str(value) for value in provider_ids.values() if value
            }:
                return result
        return None


def _build_candidates(
    entries: list[MediaCacheEntry],
    manual_terms_by_media_id: dict[str, list[str]],
    external_signals_by_media_id: dict[str, dict[str, Any]] | None = None,
) -> list[RecommendationCandidate]:
    candidates: list[RecommendationCandidate] = []
    series_entries: dict[str, list[MediaCacheEntry]] = {}
    external_signals_by_media_id = external_signals_by_media_id or {}
    for entry in entries:
        metadata = entry.metadata or {}
        if entry.item_type == "movie":
            candidates.append(
                _movie_candidate(
                    entry,
                    manual_terms_by_media_id.get(entry.id, []),
                    external_signals_by_media_id.get(entry.id, {}),
                ),
            )
        elif entry.item_type == "episode" and metadata.get("series_id"):
            series_entries.setdefault(str(metadata["series_id"]), []).append(entry)

    for series_id, episodes in series_entries.items():
        candidates.append(
            _series_candidate(
                series_id,
                episodes,
                manual_terms_by_media_id.get(series_id, []),
                external_signals_by_media_id.get(series_id, {}),
            ),
        )
    return candidates


def _result_sort_key(result: RecommendationResult) -> tuple[int, str]:
    return (-result.score, result.candidate.title.lower())


def _movie_candidate(
    entry: MediaCacheEntry,
    manual_terms: list[str],
    external_signals: dict[str, Any],
) -> RecommendationCandidate:
    metadata = dict(entry.metadata or {})
    metadata.setdefault("genres", _string_list(metadata.get("genres")))
    metadata.setdefault("tags", _string_list(metadata.get("tags")))
    metadata.setdefault("audio_languages", _language_list(metadata.get("audio_languages")))
    metadata.setdefault("subtitle_languages", _language_list(metadata.get("subtitle_languages")))
    metadata["manual_terms"] = _string_list(manual_terms)
    metadata["external_signals"] = external_signals
    return RecommendationCandidate(
        id=entry.id,
        media_type="movie",
        title=entry.title,
        item_count=1,
        average_runtime_seconds=entry.duration_seconds,
        metadata=metadata,
    )


def _series_candidate(
    series_id: str,
    episodes: list[MediaCacheEntry],
    manual_terms: list[str],
    external_signals: dict[str, Any],
) -> RecommendationCandidate:
    first_metadata = episodes[0].metadata or {}
    durations = [entry.duration_seconds for entry in episodes if entry.duration_seconds]
    metadata = {
        "series_id": series_id,
        "genres": sorted(_union_metadata(episodes, "genres")),
        "tags": sorted(_union_metadata(episodes, "tags")),
        "studios": sorted(_union_metadata(episodes, "studios")),
        "audio_languages": sorted(_union_languages(episodes, "audio_languages")),
        "subtitle_languages": sorted(_union_languages(episodes, "subtitle_languages")),
        "provider_ids": _merge_provider_ids(episodes),
        "parental_rating": first_metadata.get("parental_rating"),
        "year": first_metadata.get("year"),
        "overview": first_metadata.get("overview"),
        "manual_terms": _string_list(manual_terms),
        "external_signals": external_signals,
    }
    title = str(first_metadata.get("series_name") or episodes[0].title)
    return RecommendationCandidate(
        id=series_id,
        media_type="series",
        title=title,
        item_count=len(episodes),
        average_runtime_seconds=int(mean(durations)) if durations else None,
        metadata=metadata,
    )


def _score_candidate(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
    signal_weights: dict[str, int] | None = None,
) -> RecommendationResult:
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []
    exclusions: list[str] = []
    metadata = candidate.metadata

    genres = _normalized_terms(metadata.get("genres"))
    tags = _normalized_terms(metadata.get("tags"))
    manual_terms = _normalized_terms(metadata.get("manual_terms"))
    preferred_genres = {item.lower() for item in profile.preferred_genres}
    preferred_tags = {item.lower() for item in profile.preferred_tags}
    required_terms = {item.lower() for item in profile.required_terms}
    excluded_genres = {item.lower() for item in profile.excluded_genres}
    all_terms = genres | tags | manual_terms

    if excluded := sorted(genres & excluded_genres):
        exclusions.append(f"excluded genre: {', '.join(excluded)}")
    if required_terms and not (all_terms & required_terms):
        exclusions.append(f"missing required term: {', '.join(sorted(required_terms))}")

    genre_matches = sorted(all_terms & (preferred_genres | preferred_tags))
    if genre_matches:
        points = profile.weights.get("genre", 25)
        score += points
        reasons.append(f"matches profile terms: {', '.join(genre_matches)} (+{points})")
    elif preferred_genres or preferred_tags:
        warnings.append("no preferred genre/tag match")

    runtime_minutes = (
        candidate.average_runtime_seconds / 60
        if candidate.average_runtime_seconds
        else None
    )
    if runtime_minutes is None:
        warnings.append("missing runtime metadata")
    elif _runtime_fits(runtime_minutes, profile):
        points = profile.weights.get("runtime", 20)
        score += points
        reasons.append(f"runtime fits profile: {runtime_minutes:.1f}m (+{points})")
    else:
        exclusions.append(f"runtime outside profile range: {runtime_minutes:.1f}m")

    if candidate.item_count >= profile.min_items:
        points = profile.weights.get("depth", 15)
        if candidate.media_type == "series":
            score += points
            reasons.append(f"enough episodes available: {candidate.item_count} (+{points})")
    else:
        warnings.append(
            f"low content depth: {candidate.item_count} item(s), "
            f"recommended minimum {profile.min_items}",
        )

    _apply_language_rule(candidate, profile, score_data=(reasons, warnings, exclusions))
    if _language_rule_passes(metadata, profile.language_rule):
        score += profile.weights.get("language", 0)

    provider_ids = metadata.get("provider_ids")
    if isinstance(provider_ids, dict) and any(provider_ids.values()):
        points = profile.weights.get("metadata", 10)
        score += points
        reasons.append(f"external provider IDs available (+{points})")
    elif profile.weights.get("metadata"):
        warnings.append("missing external provider IDs")

    if metadata.get("parental_rating"):
        score += min(5, profile.weights.get("rating", 0))

    score += _apply_external_signals(metadata, profile, signal_weights or {}, reasons, warnings)
    manual_matches = sorted(manual_terms & (preferred_genres | preferred_tags | required_terms))
    if manual_matches:
        reasons.append(f"scheduler playlist/category/tag signal: {', '.join(manual_matches)}")

    return RecommendationResult(
        candidate=candidate,
        score=min(score, 100),
        reasons=reasons,
        warnings=warnings,
        exclusions=exclusions,
    )


def _apply_external_signals(
    metadata: dict[str, Any],
    profile: RecommendationProfile,
    signal_weights: dict[str, int],
    reasons: list[str],
    warnings: list[str],
) -> int:
    raw = metadata.get("external_signals")
    if not isinstance(raw, dict) or not raw:
        return 0
    score = 0
    jellystat = raw.get("jellystat")
    if isinstance(jellystat, dict):
        signals = jellystat.get("signals")
        if isinstance(signals, dict):
            for label, points in _jellystat_points(signals, profile, signal_weights):
                score += points
                reasons.append(f"{label} (+{points})")
    library = raw.get("library")
    if isinstance(library, dict):
        if library.get("underused"):
            points = max(0, signal_weights.get("underused", 0))
            if points:
                score += points
                reasons.append(f"underused library signal (+{points})")
        if library.get("stale"):
            points = max(0, signal_weights.get("stale", 0))
            if points:
                score += points
                reasons.append(f"stale-but-relevant library signal (+{points})")
        genre_trends = _normalized_terms(library.get("genre_trend_terms"))
        if genre_trends:
            points = max(0, signal_weights.get("genre_trend", 0))
            if points:
                score += points
                reasons.append(
                    "Jellystat genre-trend signal: "
                    f"{', '.join(sorted(genre_trends)[:4])} (+{points})",
                )
    metadata_points = 0
    for provider in ["tmdb", "omdb", "tvdb"]:
        payload = raw.get(provider)
        if not isinstance(payload, dict):
            continue
        rating = _float_value(payload.get("rating"))
        if rating is not None and rating >= 7:
            metadata_points = max(metadata_points, min(8, int(rating)))
        provider_genres = _normalized_terms(payload.get("genres"))
        existing_genres = _normalized_terms(metadata.get("genres"))
        added = sorted(provider_genres - existing_genres)
        if added:
            metadata.setdefault("external_genres", []).extend(added[:4])
    if metadata_points:
        score += metadata_points
        reasons.append(f"external rating signal (+{metadata_points})")
    if raw and not score:
        warnings.append("external signals available but not score-relevant")
    return min(40, score)


def _jellystat_points(
    signals: dict[str, Any],
    profile: RecommendationProfile,
    signal_weights: dict[str, int],
) -> list[tuple[str, int]]:
    points: list[tuple[str, int]] = []
    base = 0
    for signal_name in ["popular", "viewed"]:
        payload = signals.get(signal_name)
        if not isinstance(payload, dict):
            continue
        rank = _float_value(payload.get("rank"))
        plays = _float_value(payload.get("plays"))
        raw_score = _float_value(payload.get("score"))
        if rank is not None:
            base = max(base, max(1, 12 - int(rank // 5)))
        if plays is not None:
            base = max(base, min(12, int(plays)))
        if raw_score is not None:
            base = max(base, min(12, int(raw_score // 10) or 1))
    weight = signal_weights.get("activity", profile.weights.get("popularity", 10))
    activity = min(max(0, weight), base)
    if activity:
        points.append(("Jellystat activity signal", activity))
    completion = signals.get("completion")
    if isinstance(completion, dict):
        raw_completion = _float_value(completion.get("score"))
        if raw_completion is not None:
            if 0 < raw_completion <= 1:
                raw_completion *= 100
            completion_weight = max(0, signal_weights.get("completion", 0))
            completion_points = min(completion_weight, max(1, int(raw_completion // 20)))
            if completion_points:
                points.append(("Jellystat completion signal", completion_points))
    trend = signals.get("trend")
    if isinstance(trend, dict):
        raw_trend = _float_value(trend.get("score"))
        if raw_trend is not None:
            trend_weight = max(0, signal_weights.get("trend", 0))
            trend_points = min(trend_weight, max(1, int(raw_trend // 20)))
            if trend_points:
                points.append(("Jellystat watch-trend signal", trend_points))
    return points


def _float_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _apply_language_rule(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
    score_data: tuple[list[str], list[str], list[str]],
) -> None:
    reasons, warnings, exclusions = score_data
    metadata = candidate.metadata
    rule = profile.language_rule
    if rule == "none":
        return
    english_audio = has_english_audio(metadata)
    english_subtitles = has_english_subtitles(metadata)
    if english_audio:
        reasons.append("English audio found")
    if english_subtitles:
        reasons.append("English subtitles found")
    if rule == "english_audio" and not english_audio:
        exclusions.append("missing required English audio")
    elif rule == "english_subtitles" and not english_subtitles:
        exclusions.append("missing required English subtitles")
    elif rule == "english_audio_or_subtitles" and not (english_audio or english_subtitles):
        if metadata.get("audio_languages") or metadata.get("subtitle_languages"):
            exclusions.append("missing English audio or subtitles")
        else:
            exclusions.append("language metadata missing")
    elif rule == "prefer_english_audio_allow_subtitles" and not (
        english_audio or english_subtitles
    ):
        warnings.append("no English audio/subtitle fallback found")


def _language_rule_passes(metadata: dict[str, Any], rule: str) -> bool:
    if rule == "none":
        return True
    english_audio = has_english_audio(metadata)
    english_subtitles = has_english_subtitles(metadata)
    if rule == "english_audio":
        return english_audio
    if rule == "english_subtitles":
        return english_subtitles
    if rule == "english_audio_or_subtitles":
        return english_audio or english_subtitles
    if rule == "prefer_english_audio_allow_subtitles":
        return english_audio or english_subtitles
    return True


def _runtime_fits(runtime_minutes: float, profile: RecommendationProfile) -> bool:
    if profile.min_runtime_minutes is not None and runtime_minutes < profile.min_runtime_minutes:
        return False
    if profile.max_runtime_minutes is not None and runtime_minutes > profile.max_runtime_minutes:
        return False
    return True


def _union_metadata(entries: list[MediaCacheEntry], key: str) -> set[str]:
    values: set[str] = set()
    for entry in entries:
        values.update(_string_list((entry.metadata or {}).get(key)))
    return values


def _union_languages(entries: list[MediaCacheEntry], key: str) -> set[str]:
    values: set[str] = set()
    for entry in entries:
        values.update(_language_list((entry.metadata or {}).get(key)))
    return values


def _merge_provider_ids(entries: list[MediaCacheEntry]) -> dict[str, str]:
    provider_ids: dict[str, str] = {}
    for entry in entries:
        raw = (entry.metadata or {}).get("provider_ids")
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if value and key not in provider_ids:
                provider_ids[str(key)] = str(value)
    return provider_ids


def _normalized_terms(value: object) -> set[str]:
    return {item.lower() for item in _string_list(value)}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _language_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        normalized
        for item in value
        if (normalized := normalize_language(item)) is not None
    })
