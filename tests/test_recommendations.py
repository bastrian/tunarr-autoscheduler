from __future__ import annotations

import time
from typing import Any

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.repositories.recommendation_profile_repo import (
    RecommendationProfileRepository,
)
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.integrations.metadata.audit import build_metadata_audit
from tunarr_autoscheduler.integrations.metadata.cache import ExternalMetadataCacheRepository
from tunarr_autoscheduler.integrations.metadata.clients import RateLimitExceededError
from tunarr_autoscheduler.integrations.metadata.service import (
    MetadataEnrichmentService,
    read_rate_limit_alert,
)
from tunarr_autoscheduler.models.config import MetadataConfig
from tunarr_autoscheduler.models.schedule import MediaCacheEntry
from tunarr_autoscheduler.recommendations.engine import RecommendationEngine
from tunarr_autoscheduler.recommendations.language import (
    extract_language_metadata,
    normalize_language,
)
from tunarr_autoscheduler.recommendations.profiles import BUILT_IN_PROFILES, RecommendationProfile
from tunarr_autoscheduler.recommendations.signals import build_external_signals


class FakeMediaRepository:
    def __init__(self, entries: list[MediaCacheEntry]) -> None:
        self.entries = entries

    async def get_all_available(self, item_type: str | None = None) -> list[MediaCacheEntry]:
        if item_type is None:
            return self.entries
        return [entry for entry in self.entries if entry.item_type == item_type]


def _episode(
    item_id: str,
    *,
    series_id: str = "series-1",
    series_name: str = "Cowboy Bebop",
    duration: int = 24 * 60,
    genres: list[str] | None = None,
    tags: list[str] | None = None,
    audio: list[str] | None = None,
    subtitles: list[str] | None = None,
    provider_ids: dict[str, str] | None = None,
) -> MediaCacheEntry:
    return MediaCacheEntry(
        id=item_id,
        item_type="episode",
        source_type="jellyfin",
        source_id=item_id,
        title=f"Episode {item_id}",
        duration_seconds=duration,
        metadata={
            "series_id": series_id,
            "series_name": series_name,
            "genres": ["Anime", "Sci-Fi"] if genres is None else genres,
            "tags": ["anime"] if tags is None else tags,
            "audio_languages": ["ja"] if audio is None else audio,
            "subtitle_languages": ["en"] if subtitles is None else subtitles,
            "provider_ids": provider_ids or {"tvdb": "123"},
        },
    )


def _movie(
    item_id: str,
    *,
    title: str = "The Matrix",
    duration: int = 136 * 60,
    genres: list[str] | None = None,
    provider_ids: dict[str, str] | None = None,
    date_added: str | None = None,
) -> MediaCacheEntry:
    metadata: dict[str, Any] = {
        "genres": genres or ["Action", "Sci-Fi"],
        "tags": [],
        "provider_ids": {"tmdb": "603"} if provider_ids is None else provider_ids,
        "audio_languages": ["en"],
        "subtitle_languages": [],
    }
    if date_added:
        metadata["date_added"] = date_added
    return MediaCacheEntry(
        id=item_id,
        item_type="movie",
        source_type="jellyfin",
        source_id=item_id,
        title=title,
        duration_seconds=duration,
        metadata=metadata,
    )


def test_normalize_language_handles_english_aliases() -> None:
    assert normalize_language("en") == "en"
    assert normalize_language("eng") == "en"
    assert normalize_language("English") == "en"
    assert normalize_language("en-US") == "en"
    assert normalize_language("") is None


def test_extract_language_metadata_from_jellyfin_media_streams() -> None:
    metadata = extract_language_metadata([
        {"Type": "Audio", "Language": "jpn"},
        {"Type": "Audio", "Language": "English"},
        {"Type": "Subtitle", "Language": "eng"},
        {"Type": "Subtitle", "Language": "ger"},
        {"Type": "Video", "Language": "en"},
    ])

    assert metadata == {
        "audio_languages": ["en", "ja"],
        "subtitle_languages": ["de", "en"],
    }


async def test_anime_series_recommendation_accepts_english_subtitles() -> None:
    repo = FakeMediaRepository([_episode(f"ep-{index}") for index in range(1, 5)])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    results = await engine.run("anime-series")

    assert len(results) == 1
    result = results[0]
    assert result.accepted is True
    assert result.candidate.title == "Cowboy Bebop"
    assert any("English subtitles found" in reason for reason in result.reasons)


async def test_anime_series_recommendation_excludes_missing_english_language() -> None:
    repo = FakeMediaRepository([
        _episode(f"ep-{index}", audio=["ja"], subtitles=["de"])
        for index in range(1, 5)
    ])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    results = await engine.run("anime-series", include_excluded=True)

    assert len(results) == 1
    assert results[0].accepted is False
    assert "missing English audio or subtitles" in results[0].exclusions


async def test_anime_series_recommendation_requires_anime_term() -> None:
    repo = FakeMediaRepository([
        _episode(
            f"ep-{index}",
            series_id="western-animation",
            series_name="Western Animation",
            genres=["Animation"],
            tags=[],
            audio=["en"],
            subtitles=[],
        )
        for index in range(1, 5)
    ])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    results = await engine.run("anime-series", include_excluded=True)

    assert len(results) == 1
    assert results[0].accepted is False
    assert "missing required term: anime" in results[0].exclusions


async def test_anime_series_recommendation_uses_manual_playlist_terms() -> None:
    repo = FakeMediaRepository([
        _episode(
            f"ep-{index}",
            series_id="western-animation",
            series_name="Marked Anime",
            genres=["Animation"],
            tags=[],
            audio=["en"],
            subtitles=[],
        )
        for index in range(1, 5)
    ])
    engine = RecommendationEngine(
        repo,  # type: ignore[arg-type]
        manual_terms_by_media_id={"western-animation": ["anime"]},
    )

    results = await engine.run("anime-series")

    assert len(results) == 1
    assert results[0].accepted is True
    assert any(
        "scheduler playlist/category/tag signal: anime" in reason
        for reason in results[0].reasons
    )


async def test_prime_time_movies_scores_runtime_and_metadata() -> None:
    repo = FakeMediaRepository([_movie("movie-1")])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    results = await engine.run("prime-time-movies")

    assert len(results) == 1
    assert results[0].candidate.title == "The Matrix"
    assert results[0].score >= 60
    assert any("runtime fits profile" in reason for reason in results[0].reasons)


async def test_recommendation_scores_external_jellystat_signal() -> None:
    repo = FakeMediaRepository([
        _movie("movie-1", title="Popular Movie"),
        _movie("movie-2", title="Quiet Movie"),
    ])
    engine = RecommendationEngine(
        repo,  # type: ignore[arg-type]
        external_signals_by_media_id={
            "movie-1": {
                "jellystat": {
                    "signals": {
                        "popular": {"rank": 1, "plays": 12, "score": 100},
                    },
                },
                "tmdb": {"rating": 8.2, "genres": ["Action"]},
            },
        },
    )

    results = await engine.run("prime-time-movies")

    assert results[0].candidate.id == "movie-1"
    assert any("Jellystat activity signal" in reason for reason in results[0].reasons)
    assert any("external rating signal" in reason for reason in results[0].reasons)


async def test_recommendation_scores_extended_jellystat_and_library_signals() -> None:
    repo = FakeMediaRepository([
        _movie("movie-1", title="Trending Movie"),
        _movie("movie-2", title="Underused Movie", date_added="2024-01-01T00:00:00+00:00"),
    ])
    engine = RecommendationEngine(
        repo,  # type: ignore[arg-type]
        external_signals_by_media_id={
            "movie-1": {
                "jellystat": {
                    "signals": {
                        "popular": {"rank": 10, "score": 80, "completion_rate": 0.9},
                        "viewed": {"rank": 1, "score": 100},
                        "trend": {"score": 90},
                        "completion": {"score": 90},
                    },
                },
            },
            "movie-2": {
                "library": {
                    "underused": True,
                    "stale": True,
                    "age_days": 700,
                    "genre_trend_terms": ["Action"],
                },
            },
        },
        signal_weights={
            "activity": 14,
            "completion": 9,
            "trend": 8,
            "genre_trend": 5,
            "underused": 6,
            "stale": 4,
        },
    )

    results = await engine.run("prime-time-movies")
    reasons_by_id = {
        result.candidate.id: " ".join(result.reasons)
        for result in results
    }

    assert "Jellystat activity signal" in reasons_by_id["movie-1"]
    assert "Jellystat completion signal" in reasons_by_id["movie-1"]
    assert "Jellystat watch-trend signal" in reasons_by_id["movie-1"]
    assert "underused library signal" in reasons_by_id["movie-2"]
    assert "stale-but-relevant library signal" in reasons_by_id["movie-2"]
    assert "Jellystat genre-trend signal" in reasons_by_id["movie-2"]


async def test_recommendation_scores_underused_when_jellystat_is_absent() -> None:
    repo = FakeMediaRepository([_movie("movie-1", title="Quiet Library Pick")])
    engine = RecommendationEngine(
        repo,  # type: ignore[arg-type]
        external_signals_by_media_id={
            "movie-1": {"library": {"underused": True}},
        },
        signal_weights={"underused": 6},
    )

    results = await engine.run("prime-time-movies")

    assert results[0].candidate.id == "movie-1"
    assert any("underused library signal" in reason for reason in results[0].reasons)


async def test_recommendation_external_genres_do_not_overwrite_local_metadata() -> None:
    repo = FakeMediaRepository([
        _movie("movie-1", title="Local Drama", genres=["Drama"], provider_ids={"tmdb": "1"}),
    ])
    engine = RecommendationEngine(
        repo,  # type: ignore[arg-type]
        external_signals_by_media_id={
            "movie-1": {"tmdb": {"rating": 8.5, "genres": ["Horror", "Thriller"]}},
        },
    )

    results = await engine.run("prime-time-movies", include_excluded=True)

    assert results[0].candidate.metadata["genres"] == ["Drama"]
    assert results[0].candidate.metadata["external_genres"] == ["horror", "thriller"]
    assert any("external rating signal" in reason for reason in results[0].reasons)


async def test_build_external_signals_bulk_matches_provider_ids_without_per_item_get() -> None:
    class BulkOnlyCache:
        async def list_fresh(self, provider: str) -> list[dict[str, Any]]:
            rows = {
                "tmdb": [
                    {
                        "media_type": "movie",
                        "provider_id": "603",
                        "payload": {"rating": 8.7},
                    },
                ],
                "omdb": [
                    {
                        "media_type": "movie",
                        "provider_id": "tt0133093",
                        "payload": {"rating": 8.1},
                    },
                ],
                "tvdb": [
                    {
                        "media_type": "series",
                        "provider_id": "123",
                        "payload": {"genres": ["Anime"]},
                    },
                ],
            }
            return rows.get(provider, [])

        async def get(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("Provider cache should be loaded in bulk")

    signals = await build_external_signals(
        [
            _movie(
                "movie-1",
                provider_ids={"Tmdb": "603", "Imdb": "tt0133093"},
            ),
            _episode("ep-1", series_id="series-1", provider_ids={"Tvdb": "123"}),
        ],
        BulkOnlyCache(),  # type: ignore[arg-type]
    )

    assert signals["movie-1"]["tmdb"] == {"rating": 8.7}
    assert signals["movie-1"]["omdb"] == {"rating": 8.1}
    assert signals["series-1"]["tvdb"] == {"genres": ["Anime"]}
    assert signals["series-1"]["library"]["underused"] is True


async def test_build_external_signals_ignores_expired_provider_cache(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        cache = ExternalMetadataCacheRepository(db)
        await cache.set("tmdb", "movie", "fresh", {"rating": 8.0}, ttl_days=14)
        await db.execute(
            "INSERT INTO external_metadata_cache "
            "(provider, media_type, provider_id, payload_json, fetched_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            "tmdb",
            "movie",
            "expired",
            '{"rating": 9.0}',
            "2020-01-01T00:00:00+00:00",
            "2020-01-02T00:00:00+00:00",
        )

        signals = await build_external_signals(
            [
                _movie("movie-fresh", provider_ids={"tmdb": "fresh"}),
                _movie("movie-expired", provider_ids={"tmdb": "expired"}),
            ],
            cache,
        )

        assert signals["movie-fresh"]["tmdb"] == {"rating": 8.0}
        assert "tmdb" not in signals["movie-expired"]
    finally:
        await db.disconnect()


async def test_build_external_signals_derives_genre_trends_from_jellystat_activity() -> None:
    class JellystatCache:
        async def list_fresh(self, provider: str) -> list[dict[str, Any]]:
            if provider == "jellystat":
                return [
                    {
                        "media_type": "movie",
                        "provider_id": "movie-active",
                        "payload": {"signals": {"popular": {"score": 100}}},
                    },
                ]
            return []

    signals = await build_external_signals(
        [
            _movie("movie-active", title="Active Action", genres=["Action"]),
            _movie("movie-related", title="Related Action", genres=["Action"]),
            _movie("movie-other", title="Quiet Drama", genres=["Drama"]),
        ],
        JellystatCache(),  # type: ignore[arg-type]
    )

    assert signals["movie-active"]["jellystat"]["signals"]["popular"]["score"] == 100
    assert signals["movie-related"]["library"]["genre_trend_terms"] == ["action"]
    assert "genre_trend_terms" not in signals["movie-other"]["library"]


async def test_recommendation_engine_handles_large_libraries_quickly() -> None:
    entries = [
        _movie(
            f"movie-{index:04d}",
            title=f"Action Movie {index:04d}",
            provider_ids={"tmdb": str(index)},
        )
        for index in range(3000)
    ]
    repo = FakeMediaRepository(entries)
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    started = time.perf_counter()
    results = await engine.run("prime-time-movies", limit=25)
    elapsed = time.perf_counter() - started

    assert len(results) == 25
    assert results == sorted(results, key=lambda item: (-item.score, item.candidate.title.lower()))
    assert elapsed < 5.0


def test_built_in_profiles_cover_scheduler_setup_use_cases() -> None:
    expected = {
        "anime-series",
        "anime-movies",
        "morning-sitcoms",
        "afternoon-family",
        "prime-time-movies",
        "late-night-genre",
        "kids-family",
        "documentary",
        "series-marathon",
        "movie-channel-pool",
        "standby-off-air",
        "holiday-event",
    }

    assert expected <= set(BUILT_IN_PROFILES)


async def test_recommendation_explain_returns_candidate_by_media_id() -> None:
    repo = FakeMediaRepository([_movie("movie-1")])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    result = await engine.explain("movie-1", "prime-time-movies")

    assert result is not None
    assert result.candidate.title == "The Matrix"
    assert result.accepted is True


async def test_recommendation_explain_matches_provider_id() -> None:
    repo = FakeMediaRepository([_movie("movie-1", provider_ids={"tmdb": "603"})])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    result = await engine.explain("603", "prime-time-movies")

    assert result is not None
    assert result.candidate.id == "movie-1"


async def test_diagnostics_reports_metadata_coverage() -> None:
    repo = FakeMediaRepository([_movie("movie-1"), _episode("ep-1")])
    engine = RecommendationEngine(repo)  # type: ignore[arg-type]

    diagnostics = await engine.diagnostics()

    assert diagnostics["total_available"] == 2
    assert diagnostics["by_type"] == {"movie": 1, "episode": 1}
    coverage = diagnostics["metadata_coverage"]
    assert coverage["genres"]["count"] == 2
    assert coverage["provider_ids"]["count"] == 2
    assert coverage["audio_languages"]["count"] == 2


def test_recommendation_result_json_shape() -> None:
    repo = FakeMediaRepository([_movie("movie-1")])
    assert isinstance(repo.entries[0].metadata, dict)
    assert isinstance(repo.entries[0].metadata["provider_ids"], dict)


async def test_custom_recommendation_profile_repository_roundtrip(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        repo = RecommendationProfileRepository(db)
        profile = RecommendationProfile(
            id="custom-comedy",
            name="Custom Comedy",
            media_types=("series",),
            preferred_genres=("Comedy",),
            min_runtime_minutes=18,
            max_runtime_minutes=35,
            min_items=4,
            language_rule="none",
            description="Light comedy rotation",
            weights={"genre": 40, "runtime": 20},
        )

        saved = await repo.save(profile)
        loaded = await repo.get("custom-comedy")
        listed = await repo.list_all()
        deleted = await repo.delete("custom-comedy")

        assert saved.name == "Custom Comedy"
        assert loaded is not None
        assert loaded.preferred_genres == ("Comedy",)
        assert loaded.weights["genre"] == 40
        assert [item.id for item in listed] == ["custom-comedy"]
        assert deleted is True
        assert await repo.get("custom-comedy") is None
    finally:
        await db.disconnect()


def test_metadata_audit_groups_provider_id_coverage() -> None:
    audit = build_metadata_audit([
        _movie("movie-1", provider_ids={"Tmdb": "603", "Imdb": "tt0133093"}),
        _movie("movie-2", provider_ids={}),
        _episode("ep-1", series_id="series-1", provider_ids={"Tvdb": "123"}),
        _episode("ep-2", series_id="series-1", provider_ids={"Imdb": "tt-series"}),
    ])

    assert audit["total"] == 3
    movies = audit["by_type"]["movie"]["providers"]
    series = audit["by_type"]["series"]["providers"]
    assert movies["tmdb"]["count"] == 1
    assert movies["imdb"]["count"] == 1
    assert series["tvdb"]["count"] == 1
    assert series["imdb"]["count"] == 1


async def test_external_metadata_cache_repository_roundtrip(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        repo = ExternalMetadataCacheRepository(db)
        await repo.set(
            "tmdb",
            "movie",
            "603",
            {"title": "The Matrix"},
            ttl_days=14,
        )

        cached = await repo.get("tmdb", "movie", "603")
        status = await repo.status("tmdb", "movie", "603")
        stats = await repo.stats()

        assert cached == {"title": "The Matrix"}
        assert status == "fresh"
        assert stats == {"tmdb": {"fresh": 1, "expired": 0}}
    finally:
        await db.disconnect()


async def test_metadata_enrichment_service_dry_run_counts_cache_states(tmp_path) -> None:
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        cache = ExternalMetadataCacheRepository(db)
        await cache.set("tmdb", "movie", "603", {"title": "The Matrix"}, ttl_days=14)
        service = MetadataEnrichmentService(
            cache=cache,
            config=MetadataConfig(
                tmdb_enabled=True,
                omdb_enabled=True,
                tmdb_api_key="tmdb-key",
                omdb_api_key="omdb-key",
            ),
        )

        summary = await service.refresh([
            _movie("movie-1", provider_ids={"Tmdb": "603", "Imdb": "tt0133093"}),
            _movie("movie-2", provider_ids={"Tmdb": "604"}),
        ])

        assert summary.as_dict() == {
            "candidates": 3,
            "cached": 1,
            "missing": 2,
            "expired": 0,
            "fetched": 0,
            "skipped": 2,
            "rate_limited": 0,
            "rate_limited_providers": [],
            "provider_statuses": {},
        }
    finally:
        await db.disconnect()


async def test_metadata_enrichment_service_records_rate_limit_alert(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db = Database(str(tmp_path / "scheduler.db"))
    await db.connect()
    try:
        await run_migrations(db)
        cache = ExternalMetadataCacheRepository(db)
        service = MetadataEnrichmentService(
            cache=cache,
            config=MetadataConfig(
                tmdb_enabled=True,
                tmdb_api_key="tmdb-key",
            ),
        )

        async def fake_fetch(request: dict[str, str]) -> dict[str, Any]:
            raise RateLimitExceededError(provider=request["provider"], attempts=6)

        monkeypatch.setattr(service, "_fetch", fake_fetch)

        summary = await service.refresh([
            _movie("movie-1", provider_ids={"Tmdb": "603"}),
            _movie("movie-2", provider_ids={"Tmdb": "604"}),
        ], dry_run=False)

        assert summary.as_dict() == {
            "candidates": 2,
            "cached": 0,
            "missing": 1,
            "expired": 0,
            "fetched": 0,
            "skipped": 2,
            "rate_limited": 1,
            "rate_limited_providers": ["tmdb"],
            "provider_statuses": {},
        }
        alert = read_rate_limit_alert()
        assert alert is not None
        assert alert["provider"] == "tmdb"
        assert alert["attempts"] == 6
    finally:
        await db.disconnect()


def _unused_type_anchor(value: dict[str, Any]) -> dict[str, Any]:
    return value
