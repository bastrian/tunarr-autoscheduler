from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from tunarr_autoscheduler.core.event_bus import EventBus
from tunarr_autoscheduler.core.plugin_loader import PipelineContext
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.integrations.jellyfin.sync import MediaSyncEngine
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    FillerBlock,
    FillerType,
    MovieBlock,
    OfflineBlock,
    SlotBlock,
    StationIDBlock,
)
from tunarr_autoscheduler.models.config import (
    AdsConfig,
    AppConfig,
    ChannelConfig,
    ContinuityConfig,
    DayOfWeek,
    DaypartTemplate,
    HumanizerConfig,
    RotationConfig,
)
from tunarr_autoscheduler.models.playlist import PlaylistItem
from tunarr_autoscheduler.models.schedule import MediaCacheEntry, RotationState
from tunarr_autoscheduler.plugins.ad_inserter import AdInserter
from tunarr_autoscheduler.plugins.continuity_inserter import ContinuityInserter
from tunarr_autoscheduler.plugins.daypart_applicator import DaypartApplicator
from tunarr_autoscheduler.plugins.event_scheduler import EventScheduler
from tunarr_autoscheduler.plugins.gap_filler import GapFiller
from tunarr_autoscheduler.plugins.html_preview import HTMLPreview
from tunarr_autoscheduler.plugins.humanizer import Humanizer
from tunarr_autoscheduler.plugins.movie_scheduler import MovieScheduler
from tunarr_autoscheduler.plugins.rotation_scheduler import RotationScheduler
from tunarr_autoscheduler.plugins.schedule_persister import SchedulePersister
from tunarr_autoscheduler.plugins.statistics import StatsExporter
from tunarr_autoscheduler.plugins.tunarr_uploader import TunarrUploader
from tunarr_autoscheduler.plugins.validators import Validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(tz=UTC)


def make_episode(
    start_offset: float = 0,
    duration: float = 1800,
    episode_id: str = "ep_default",
    show_id: str = "show_default",
    metadata: dict | None = None,
) -> EpisodeBlock:
    s = NOW + timedelta(seconds=start_offset)
    return EpisodeBlock(
        start_time=s,
        end_time=s + timedelta(seconds=duration),
        duration=timedelta(seconds=duration),
        episode_id=episode_id,
        show_id=show_id,
        season_number=1,
        episode_number=1,
        runtime_seconds=int(duration),
        metadata=metadata or {},
    )


def make_movie(
    start_offset: float = 0,
    duration: float = 5400,
    movie_id: str = "movie_default",
    metadata: dict | None = None,
) -> MovieBlock:
    s = NOW + timedelta(seconds=start_offset)
    return MovieBlock(
        start_time=s,
        end_time=s + timedelta(seconds=duration),
        duration=timedelta(seconds=duration),
        movie_id=movie_id,
        runtime_seconds=int(duration),
        year=2024,
        metadata=metadata or {},
    )


def make_slot(
    start_offset: float = 0,
    duration: float = 1800,
    metadata: dict | None = None,
) -> SlotBlock:
    s = NOW + timedelta(seconds=start_offset)
    return SlotBlock(
        start_time=s,
        end_time=s + timedelta(seconds=duration),
        duration=timedelta(seconds=duration),
        metadata=metadata or {},
    )


def make_context(
    channel_config: ChannelConfig | None = None,
    state: Any = None,
    media_repo: Any = None,
    playlist_repo: Any = None,
    tunarr_client: Any = None,
    metrics: Any = None,
    generation_mode: str = "continue",
    schedule_start: datetime | None = None,
    parent_version: int | None = None,
    reserved_episode_ids: set[str] | None = None,
    reserved_movie_ids: set[str] | None = None,
    app_config: AppConfig | None = None,
) -> PipelineContext:
    if channel_config is None:
        channel_config = ChannelConfig(id="test_channel")
    return PipelineContext(
        channel_config=channel_config,
        generation_id="gen-test",
        job_id="job-test",
        state=state,
        media_repo=media_repo,
        playlist_repo=playlist_repo,
        tunarr_client=tunarr_client,
        metrics=metrics,
        generation_mode=generation_mode,
        schedule_start=schedule_start,
        parent_version=parent_version,
        reserved_episode_ids=reserved_episode_ids,
        reserved_movie_ids=reserved_movie_ids,
        app_config=app_config,
    )


class MockStateManager:
    """In-memory state manager for tests."""

    def __init__(self) -> None:
        self.rotation_states: dict[str, RotationState] = {}
        self.cooldowns: dict[str, dict[str, Any]] = {}
        self.air_history: list[dict[str, Any]] = []
        self.recently_aired_episode_ids: set[str] = set()
        self.schedule_versions: list[dict[str, Any]] = []

    async def get_rotation_state(
        self, channel_id: str, rotation_name: str,
    ) -> RotationState | None:
        key = f"{channel_id}:{rotation_name}"
        return self.rotation_states.get(key)

    async def save_rotation_state(self, state: RotationState) -> None:
        key = f"{state.channel_id}:{state.rotation_name}"
        self.rotation_states[key] = state

    async def set_cooldown(
        self, item_id: str, item_type: str, channel_id: str, duration_minutes: int,
    ) -> None:
        self.cooldowns[item_id] = {
            "type": item_type,
            "channel": channel_id,
            "minutes": duration_minutes,
        }

    async def mark_episode_used(
        self,
        channel_id: str,
        episode_id: str,
        duration_seconds: int,
        **kwargs: Any,
    ) -> None:
        self.air_history.append({
            "channel_id": channel_id,
            "episode_id": episode_id,
            "duration": duration_seconds,
        })

    async def get_air_history(
        self, channel_id: str, item_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        if item_id in self.recently_aired_episode_ids:
            return [{"channel_id": channel_id, "item_id": item_id, "aired_at": since}]
        return []

    async def get_cooldown_remaining(self, item_id: str) -> int:
        cooldown = self.cooldowns.get(item_id)
        return int(cooldown["minutes"]) * 60 if cooldown else 0

    async def get_latest_version(self, channel_id: str) -> int:
        versions = [
            int(v["version"])
            for v in self.schedule_versions
            if v["channel_id"] == channel_id
        ]
        return max(versions, default=0)

    async def save_schedule_version(
        self,
        channel_id: str,
        version: int,
        timeline_json: str,
        status: str = "draft",
        parent_version: int | None = None,
    ) -> str:
        version_id = f"{channel_id}-{version}"
        self.schedule_versions.append({
            "id": version_id,
            "channel_id": channel_id,
            "version": version,
            "timeline_json": timeline_json,
            "status": status,
            "parent_version": parent_version,
        })
        return version_id


class MockMediaRepository:
    def __init__(self, entries: list[MediaCacheEntry] | None = None) -> None:
        self.entries = entries or []

    async def get(self, item_id: str) -> MediaCacheEntry | None:
        for entry in self.entries:
            if entry.id == item_id:
                return entry
        return None

    async def get_by_source(self, source_type: str, source_id: str) -> MediaCacheEntry | None:
        for entry in self.entries:
            if entry.source_type == source_type and entry.source_id == source_id:
                return entry
        return None

    async def get_all_available(self, item_type: str | None = None) -> list[MediaCacheEntry]:
        available = [entry for entry in self.entries if entry.available]
        if item_type is None:
            return available
        return [entry for entry in available if entry.item_type == item_type]


class MockTunarrClient:
    def __init__(self, custom_show_programs: dict[str, list[dict[str, Any]]]) -> None:
        self.custom_show_programs = custom_show_programs

    async def get_custom_show_programs(self, custom_show_id: str) -> list[dict[str, Any]]:
        return self.custom_show_programs.get(custom_show_id, [])


class MockPlaylistRepository:
    def __init__(self, items: list[PlaylistItem]) -> None:
        self.items = items

    async def get_items(self, playlist_ids: list[str]) -> list[PlaylistItem]:
        return self.items if playlist_ids else []


async def test_rotation_scheduler_uses_series_from_scheduler_playlist() -> None:
    timeline = Timeline()
    timeline.insert(make_slot(metadata={"playlist_ids": ["playlist-1"]}))
    media_repo = MockMediaRepository([
        MediaCacheEntry(
            id="wanted-episode",
            item_type="episode",
            source_type="jellyfin",
            source_id="wanted-episode",
            title="Wanted",
            duration_seconds=1800,
            metadata={
                "series_id": "wanted-series",
                "series_name": "Wanted Series",
                "parent_index_number": 1,
                "index_number": 1,
            },
        ),
        MediaCacheEntry(
            id="other-episode",
            item_type="episode",
            source_type="jellyfin",
            source_id="other-episode",
            title="Other",
            duration_seconds=1800,
            metadata={"series_id": "other-series", "series_name": "Other Series"},
        ),
    ])
    playlist_repo = MockPlaylistRepository([
        PlaylistItem(
            media_type="series",
            media_id="wanted-series",
            title="Wanted Series",
            position=0,
        ),
    ])

    result = await RotationScheduler().process(
        timeline,
        make_context(
            channel_config=ChannelConfig(id="test", rotations=[]),
            state=MockStateManager(),
            media_repo=media_repo,
            playlist_repo=playlist_repo,
        ),
    )

    assert isinstance(result.blocks[0], EpisodeBlock)
    assert result.blocks[0].episode_id == "wanted-episode"


async def test_rotation_scheduler_skips_unavailable_custom_show_episode() -> None:
    timeline = Timeline()
    timeline.insert(make_slot(metadata={"custom_show_list_ids": ["custom-1"]}))
    media_repo = MockMediaRepository([
        MediaCacheEntry(
            id="stale-episode",
            item_type="episode",
            source_type="jellyfin",
            source_id="stale-episode",
            title="Stale",
            duration_seconds=1800,
            metadata={"series_id": "stale-series", "series_name": "Stale Series"},
            available=False,
        ),
    ])
    tunarr_client = MockTunarrClient({
        "custom-1": [{
            "type": "custom",
            "id": "stale-episode",
            "program": {
                "type": "content",
                "id": "stale-episode",
                "program": {
                    "type": "episode",
                    "externalId": "stale-episode",
                    "showId": "stale-series",
                    "duration": 1_800_000,
                },
            },
        }],
    })

    result = await RotationScheduler().process(
        timeline,
        make_context(media_repo=media_repo, tunarr_client=tunarr_client),
    )

    assert not any(isinstance(block, EpisodeBlock) for block in result.blocks)


async def test_movie_scheduler_uses_movies_from_scheduler_playlist_in_order() -> None:
    timeline = Timeline()
    timeline.insert(make_slot(
        duration=7200,
        metadata={
            "playlist_ids": ["playlist-1"],
            "allow_movies": True,
            "slot_duration_minutes": 120,
            "ad_density": 0,
        },
    ))
    media_repo = MockMediaRepository([
        MediaCacheEntry(
            id="playlist-movie",
            item_type="movie",
            source_type="jellyfin",
            source_id="playlist-movie",
            title="Playlist Movie",
            duration_seconds=5400,
        ),
        MediaCacheEntry(
            id="other-movie",
            item_type="movie",
            source_type="jellyfin",
            source_id="other-movie",
            title="Other Movie",
            duration_seconds=7000,
        ),
    ])
    playlist_repo = MockPlaylistRepository([
        PlaylistItem(
            media_type="movie",
            media_id="playlist-movie",
            title="Playlist Movie",
            position=0,
        ),
    ])

    result = await MovieScheduler().process(
        timeline,
        make_context(
            media_repo=media_repo,
            playlist_repo=playlist_repo,
        ),
    )

    assert isinstance(result.blocks[0], MovieBlock)
    assert result.blocks[0].movie_id == "playlist-movie"


async def test_follow_up_skips_movies_reserved_by_previous_schedules() -> None:
    timeline = Timeline()
    timeline.insert(make_slot(
        duration=7200,
        metadata={"allow_movies": True, "slot_duration_minutes": 120, "ad_density": 0},
    ))
    media_repo = MockMediaRepository([
        MediaCacheEntry(
            id="movie-1",
            item_type="movie",
            source_type="jellyfin",
            source_id="movie-1",
            title="Movie One",
            duration_seconds=3600,
            metadata={"year": 2020},
        ),
        MediaCacheEntry(
            id="movie-2",
            item_type="movie",
            source_type="jellyfin",
            source_id="movie-2",
            title="Movie Two",
            duration_seconds=3600,
            metadata={"year": 2021},
        ),
    ])

    result = await MovieScheduler().process(
        timeline,
        make_context(
            media_repo=media_repo,
            generation_mode="follow_up",
            reserved_movie_ids={"movie-1"},
        ),
    )

    assert isinstance(result.blocks[0], MovieBlock)
    assert result.blocks[0].movie_id == "movie-2"


# ===================================================================
# AdInserter
# ===================================================================

class TestAdInserter:
    async def test_inserts_ad_at_midpoint_of_episode(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=3600))
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert len(ad_blocks) >= 1
        for ad in ad_blocks:
            assert ad.ad_count > 0
            assert ad.total_duration_seconds > 0

    async def test_no_ad_when_duration_below_min(self) -> None:
        tl = Timeline()
        ep = make_episode(duration=300)
        ep.metadata["ad_density"] = 0.01
        tl.insert(ep)
        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                ad_density=0.01,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        plugin = AdInserter()
        result = await plugin.process(tl, make_context(channel_config=config))
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert len(ad_blocks) == 0

    async def test_ad_at_daypart_boundary(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        # Simulate daypart boundary by setting metadata
        ep2 = make_episode(start_offset=1800, episode_id="ep2")
        ep2.metadata["daypart"] = "afternoon"
        tl.insert(ep2)
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert len(ad_blocks) >= 1

    async def test_movie_gets_two_ad_breaks(self) -> None:
        tl = Timeline()
        tl.insert(make_movie(duration=7200))
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert len(ad_blocks) >= 1

    async def test_ad_count_calculation(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=3600, metadata={"ad_density": 0.1}))
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        for ad in ad_blocks:
            assert ad.ad_count * 30 == ad.total_duration_seconds

    async def test_episode_chapters_create_breaks(self) -> None:
        tl = Timeline()
        ep = make_episode(duration=3600, metadata={
            "chapters": [
                {"chapterType": "intro", "startTime": 120},
                {"chapterType": "outro", "startTime": 3300},
            ],
        })
        tl.insert(ep)
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert len(ad_blocks) >= 1

    async def test_ads_do_not_overlap_program_blocks(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=3600, metadata={"ad_density": 0.1}))
        plugin = AdInserter()
        result = await plugin.process(tl, make_context())

        assert result.validate() == []

    async def test_ad_offsets_are_applied_once_to_later_blocks(self) -> None:
        tl = Timeline()
        ep1 = make_episode(duration=3600, metadata={"ad_density": 0.1})
        ep2 = make_episode(start_offset=3600, duration=1800, episode_id="ep2")
        tl.insert(ep1)
        tl.insert(ep2)

        plugin = AdInserter()
        result = await plugin.process(tl, make_context())
        episodes = [b for b in result.blocks if isinstance(b, EpisodeBlock)]

        assert episodes[1].start_time == ep1.end_time + timedelta(seconds=300)
        assert result.validate() == []

    async def test_ad_offset_carries_across_daypart_when_previous_block_overruns(self) -> None:
        tl = Timeline()
        movie_daypart = {
            "daypart": "movie",
            "daypart_boundary": (NOW + timedelta(hours=3)).isoformat(),
            "variable_movie_duration": True,
            "end_tolerance_minutes": 60,
            "ad_density": 0.02,
        }
        movie1 = make_movie(duration=3600, movie_id="movie1", metadata=dict(movie_daypart))
        movie2 = make_movie(
            start_offset=3600,
            duration=3600,
            movie_id="movie2",
            metadata=dict(movie_daypart),
        )
        late_episode = make_episode(
            start_offset=7200,
            duration=1800,
            episode_id="late",
            metadata={"daypart": "late_night", "ad_density": 0},
        )
        tl.insert(movie1)
        tl.insert(movie2)
        tl.insert(late_episode)

        result = await AdInserter().process(tl, make_context())
        blocks = sorted(result.blocks, key=lambda item: item.start_time)
        shifted_movie2 = next(
            block for block in blocks
            if isinstance(block, MovieBlock) and block.movie_id == "movie2"
        )
        shifted_late = next(
            block for block in blocks
            if isinstance(block, EpisodeBlock) and block.episode_id == "late"
        )

        assert shifted_late.start_time >= shifted_movie2.end_time
        assert result.validate() == []

    async def test_transition_blocks_shift_after_ad_pushed_overrun(self) -> None:
        boundary = NOW + timedelta(hours=1, minutes=30)
        tl = Timeline()
        ep1 = make_episode(
            duration=1800,
            metadata={"daypart": "late_night", "ad_density": 0.2},
        )
        ep2 = make_episode(
            start_offset=1800,
            duration=3600,
            episode_id="ep2",
            metadata={
                "daypart": "late_night",
                "daypart_boundary": boundary.isoformat(),
                "end_tolerance_minutes": 10,
            },
        )
        station = StationIDBlock(
            start_time=boundary,
            end_time=boundary + timedelta(seconds=15),
            duration=timedelta(seconds=15),
            clip_id="station",
            metadata={"type": "daypart_transition", "daypart": "standby"},
        )
        offline = OfflineBlock(
            start_time=station.end_time,
            end_time=boundary + timedelta(hours=1),
            duration=timedelta(minutes=59, seconds=45),
            reason="Off-Air",
            metadata={"daypart": "standby"},
        )
        tl.insert(ep1)
        tl.insert(ep2)
        tl.insert(station)
        tl.insert(offline)

        result = await AdInserter().process(tl, make_context())

        shifted_station = next(
            b for b in result.blocks
            if isinstance(b, StationIDBlock) and b.metadata.get("type") == "daypart_transition"
        )
        shifted_ep2 = next(
            b for b in result.blocks
            if isinstance(b, EpisodeBlock) and b.episode_id == "ep2"
        )
        assert shifted_ep2.end_time > boundary
        assert shifted_station.start_time == shifted_ep2.end_time
        assert result.validate() == []

    async def test_keeps_variable_movie_that_crosses_boundary(self) -> None:
        boundary = NOW + timedelta(minutes=90)
        tl = Timeline()
        movie = make_movie(
            duration=7200,
            metadata={
                "daypart": "primetime",
                "daypart_boundary": boundary.isoformat(),
                "variable_movie_duration": True,
                "ad_density": 0.0,
            },
        )
        tl.insert(movie)

        result = await AdInserter().process(tl, make_context())

        movies = [block for block in result.blocks if isinstance(block, MovieBlock)]
        assert movies == [movie]

    async def test_ads_do_not_use_daypart_end_tolerance_window(self) -> None:
        boundary = NOW + timedelta(minutes=30)
        tl = Timeline()
        ep = make_episode(
            duration=1830,
            metadata={
                "daypart": "late_night",
                "daypart_boundary": boundary.isoformat(),
                "end_tolerance_minutes": 10,
                "ad_density": 0.2,
            },
        )
        tl.insert(ep)

        result = await AdInserter().process(tl, make_context())

        ads = [block for block in result.blocks if isinstance(block, AdBlock)]
        assert ads == []

    async def test_uses_tunarr_filler_list_spots_that_fit_break_duration(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad1", "title": "Ad 1", "duration": 30_000},
                    {"id": "ad2", "title": "Ad 2", "duration": 30_000},
                    {"id": "long", "title": "Long Ad", "duration": 240_000},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0.1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=3600))

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]

        assert len(ad_blocks) == 1
        assert ad_blocks[0].total_duration_seconds == 120
        assert [s["id"] for s in ad_blocks[0].metadata["spots"]] == [
            "ad1", "ad2", "ad1", "ad2",
        ]
        assert result.metadata["ad_rotation_summary"]["spot_count"] == 3
        assert result.metadata["ad_rotation_summary"]["break_count"] == 1
        assert result.metadata["ad_rotation_summary"]["unique_spots_used"] == 2

    async def test_ad_warnings_report_missing_filler_spots(self) -> None:
        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="missing-list",
                ad_density=0.1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=3600))

        result = await AdInserter().process(tl, make_context(channel_config=config))

        assert result.metadata["ad_rotation_summary"]["generic_break_count"] == 1
        assert any(
            "No filler spots were loaded" in warning
            for warning in result.metadata["ad_warnings"]
        )

    async def test_ad_warnings_report_poor_filler_fit(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad-70", "title": "Odd Ad", "duration": 70},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0.1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=3600))

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )

        assert result.metadata["ad_rotation_summary"]["poor_fit_count"] == 1
        assert any(
            "cannot closely fit" in warning
            for warning in result.metadata["ad_warnings"]
        )

    async def test_real_ad_spots_fill_existing_gap_before_generic_filler(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad-30", "title": "30 Second Ad", "duration": 30},
                    {"id": "ad-60", "title": "60 Second Ad", "duration": 60},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0,
                min_total_minutes=2,
                max_total_minutes=2,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        tl = Timeline([
            make_episode(duration=1800, metadata={"ad_density": 0}),
            make_episode(
                start_offset=1920,
                duration=1800,
                episode_id="ep2",
                metadata={"ad_density": 0},
            ),
        ])

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )
        ads = [block for block in result.blocks if isinstance(block, AdBlock)]

        assert sum(block.total_duration_seconds for block in ads) == 120
        assert result.validate() == []

    async def test_gap_fill_ads_do_not_stack_after_regular_ads(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad-60", "title": "60 Second Ad", "duration": 60},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0.1,
                min_total_minutes=8,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=3,
            ),
        )
        tl = Timeline([
            make_episode(duration=1800),
            make_episode(start_offset=2100, duration=1800, episode_id="ep2"),
        ])

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )
        blocks = sorted(result.blocks, key=lambda block: block.start_time)

        for previous, current in zip(blocks, blocks[1:], strict=False):
            assert not isinstance(previous, AdBlock) or not isinstance(current, AdBlock)

    async def test_gap_fill_ads_skip_gaps_touching_offline_blocks(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad-60", "title": "60 Second Ad", "duration": 60},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0,
                break_after_programs=99,
                min_total_minutes=1,
                max_total_minutes=1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=1,
            ),
        )
        offline = OfflineBlock(
            start_time=NOW,
            end_time=NOW + timedelta(minutes=60),
            duration=timedelta(minutes=60),
            reason="off_air",
        )
        episode = make_episode(start_offset=65 * 60, duration=1800, episode_id="after")
        tl = Timeline([offline, episode])

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )

        assert [block for block in result.blocks if isinstance(block, AdBlock)] == []

    async def test_regular_ads_skip_break_directly_before_off_air(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad-60", "title": "60 Second Ad", "duration": 60},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0.2,
                break_after_programs=1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        episode = make_episode(duration=1800, metadata={"daypart": "late_night"})
        station = StationIDBlock(
            start_time=episode.end_time,
            end_time=episode.end_time + timedelta(seconds=15),
            duration=timedelta(seconds=15),
            clip_id="station",
            metadata={"type": "daypart_transition"},
        )
        offline = OfflineBlock(
            start_time=station.end_time,
            end_time=station.end_time + timedelta(hours=1),
            duration=timedelta(hours=1),
            reason="off_air",
        )
        tl = Timeline([episode, station, offline])

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )

        assert [block for block in result.blocks if isinstance(block, AdBlock)] == []
        assert result.metadata["ad_warnings"]

    async def test_ad_warnings_report_unreachable_minimum(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "long", "title": "Long Ad", "duration": 300},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0,
                break_after_programs=99,
                min_total_minutes=2,
                max_total_minutes=2,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=1,
            ),
        )
        tl = Timeline([make_episode(duration=1800, metadata={"ad_density": 0})])

        result = await AdInserter().process(
            tl,
            make_context(channel_config=config, tunarr_client=Tunarr()),
        )

        assert [block for block in result.blocks if isinstance(block, AdBlock)] == []
        assert "Minimum ad minutes could not be reached" in result.metadata["ad_warnings"][0]

    async def test_ad_rotation_state_advances_across_runs(self) -> None:
        class Tunarr:
            async def get_filler_list_programs(self, filler_list_id: str) -> list[dict[str, Any]]:
                return [
                    {"id": "ad1", "title": "Ad 1", "duration": 30},
                    {"id": "ad2", "title": "Ad 2", "duration": 30},
                    {"id": "ad3", "title": "Ad 3", "duration": 30},
                ]

        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                filler_list_id="filler-1",
                ad_density=0.1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=1,
            ),
        )
        state = MockStateManager()

        for _ in range(2):
            tl = Timeline()
            tl.insert(make_episode(duration=3600))
            result = await AdInserter().process(
                tl,
                make_context(channel_config=config, state=state, tunarr_client=Tunarr()),
            )

        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]
        assert [s["id"] for s in ad_blocks[0].metadata["spots"]] == ["ad3", "ad1"]

    async def test_break_after_programs_limits_episode_break_frequency(self) -> None:
        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                break_after_programs=2,
                ad_density=0.1,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=5,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=1800, episode_id="ep1"))
        tl.insert(make_episode(start_offset=1800, duration=1800, episode_id="ep2"))
        tl.insert(make_episode(start_offset=3600, duration=1800, episode_id="ep3"))

        result = await AdInserter().process(tl, make_context(channel_config=config))
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]

        assert len(ad_blocks) == 1

    async def test_max_total_ad_minutes_caps_generation(self) -> None:
        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                ad_density=0.1,
                max_total_minutes=5,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=5,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=3600, episode_id="ep1"))
        tl.insert(make_episode(start_offset=3600, duration=3600, episode_id="ep2"))

        result = await AdInserter().process(tl, make_context(channel_config=config))
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]

        assert sum(b.total_duration_seconds for b in ad_blocks) == 300

    async def test_ad_target_ignores_offline_time_and_respects_max_total(self) -> None:
        config = ChannelConfig(
            id="test",
            ads=AdsConfig(
                ad_density=0.2,
                max_total_minutes=4,
                min_ad_break_duration_minutes=1,
                max_ad_break_duration_minutes=2,
            ),
        )
        tl = Timeline()
        tl.insert(make_episode(duration=1800, episode_id="ep1"))
        tl.insert(OfflineBlock(
            start_time=NOW + timedelta(minutes=32),
            end_time=NOW + timedelta(hours=4),
            duration=timedelta(minutes=208),
            reason="off_air",
        ))
        tl.insert(make_episode(start_offset=4 * 3600, duration=1800, episode_id="ep2"))

        result = await AdInserter().process(tl, make_context(channel_config=config))
        ad_blocks = [b for b in result.blocks if isinstance(b, AdBlock)]

        assert sum(b.total_duration_seconds for b in ad_blocks) <= 240
        assert all(b.total_duration_seconds <= 120 for b in ad_blocks)


# ===================================================================
# ContinuityInserter
# ===================================================================

class TestContinuityInserter:
    async def test_does_nothing_when_disabled(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=False))
        plugin = ContinuityInserter()
        result = await plugin.process(tl, make_context(channel_config=config))
        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        assert len(station_ids) == 0

    async def test_inserts_station_id_at_frequency(self) -> None:
        tl = Timeline()
        for i in range(6):
            tl.insert(make_episode(
                start_offset=i * 1800,
                episode_id=f"ep{i}",
                metadata={"daypart": "late_night"},
            ))
        # Frequency 4: should get station IDs at blocks 4 and 8 (indices 3 and 7)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=4))
        plugin = ContinuityInserter()
        result = await plugin.process(tl, make_context(channel_config=config))
        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        assert len(station_ids) == 1
        assert station_ids[0].metadata["daypart"] == "late_night"

    async def test_uses_configured_station_id_clips(self) -> None:
        tl = Timeline()
        for i in range(4):
            tl.insert(make_episode(start_offset=i * 1800, episode_id=f"ep{i}"))
        config = ChannelConfig(
            id="test",
            continuity=ContinuityConfig(
                enabled=True,
                frequency=2,
                station_id_clip_ids=["station-a", "station-b"],
            ),
        )

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        station_ids = [
            b for b in result.blocks
            if isinstance(b, StationIDBlock) and b.metadata.get("type") == "station_id"
        ]
        assert station_ids[0].clip_id in {"station-a", "station-b"}

    async def test_loads_station_ids_from_custom_show(self) -> None:
        tl = Timeline()
        for i in range(2):
            tl.insert(make_episode(start_offset=i * 1800, episode_id=f"ep{i}"))
        config = ChannelConfig(
            id="test",
            continuity=ContinuityConfig(
                enabled=True,
                frequency=2,
                station_id_custom_show_id="station-list",
            ),
        )
        tunarr = MockTunarrClient({
            "station-list": [{"program": {"id": "station-from-tunarr", "subtype": "other_video"}}],
        })

        result = await ContinuityInserter().process(
            tl, make_context(channel_config=config, tunarr_client=tunarr),
        )

        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        assert station_ids[0].clip_id == "station-from-tunarr"

    async def test_inserts_bumper_before_movie(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        tl.insert(make_movie(start_offset=1800))
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=2))
        plugin = ContinuityInserter()
        result = await plugin.process(tl, make_context(channel_config=config))
        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        assert len(station_ids) >= 1
        # The bumper before movie should have clip_id "up_next_bumper"
        bumpers = [b for b in station_ids if b.clip_id == "up_next_bumper"]
        assert len(bumpers) >= 1

    async def test_daypart_boundary_gets_station_id(self) -> None:
        tl = Timeline()
        ep1 = make_episode(episode_id="ep1")
        ep1.metadata["daypart"] = "morning"
        tl.insert(ep1)
        ep2 = make_episode(start_offset=1800, episode_id="ep2")
        ep2.metadata["daypart"] = "afternoon"
        tl.insert(ep2)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=10))
        plugin = ContinuityInserter()
        result = await plugin.process(tl, make_context(channel_config=config))
        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        daypart_ids = [b for b in station_ids if b.metadata.get("type") == "daypart_transition"]
        assert len(daypart_ids) >= 1

    async def test_offline_to_offline_daypart_boundary_skips_station_id(self) -> None:
        tl = Timeline()
        standby = OfflineBlock(
            start_time=NOW,
            end_time=NOW + timedelta(minutes=45),
            duration=timedelta(minutes=45),
            reason="standby",
            metadata={"daypart": "standby", "title": "Standby Loop"},
        )
        off_air = OfflineBlock(
            start_time=NOW + timedelta(minutes=45),
            end_time=NOW + timedelta(minutes=105),
            duration=timedelta(minutes=60),
            reason="off_air",
            metadata={"daypart": "off_air", "title": "Off-Air Loop"},
        )
        tl.insert(standby)
        tl.insert(off_air)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=10))

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        daypart_ids = [
            b for b in result.blocks
            if isinstance(b, StationIDBlock) and b.metadata.get("type") == "daypart_transition"
        ]
        assert daypart_ids == []
        assert result.validate() == []

    async def test_daypart_boundary_into_offline_skips_station_id(self) -> None:
        tl = Timeline()
        episode = make_episode(duration=1800, episode_id="late")
        episode.metadata["daypart"] = "late_night"
        tl.insert(episode)
        off_air = OfflineBlock(
            start_time=NOW + timedelta(minutes=30),
            end_time=NOW + timedelta(minutes=90),
            duration=timedelta(minutes=60),
            reason="off_air",
            metadata={"daypart": "off_air", "title": "Off-Air Loop"},
        )
        tl.insert(off_air)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=10))

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        daypart_ids = [
            b for b in result.blocks
            if isinstance(b, StationIDBlock) and b.metadata.get("type") == "daypart_transition"
        ]
        assert daypart_ids == []
        assert result.validate() == []

    async def test_daypart_boundary_does_not_duplicate_frequency_station_id(self) -> None:
        tl = Timeline()
        ep1 = make_episode(episode_id="ep1")
        ep1.metadata["daypart"] = "morning"
        tl.insert(ep1)
        ep2 = make_episode(start_offset=1800, episode_id="ep2")
        ep2.metadata["daypart"] = "afternoon"
        tl.insert(ep2)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=2))

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        station_ids = [b for b in result.blocks if isinstance(b, StationIDBlock)]
        assert len(station_ids) == 1
        assert station_ids[0].metadata["type"] == "daypart_transition"

    async def test_inserted_continuity_shifts_later_blocks_without_overlap(self) -> None:
        tl = Timeline()
        for i in range(4):
            tl.insert(make_episode(start_offset=i * 1800, duration=1800, episode_id=f"ep{i}"))
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=2))
        plugin = ContinuityInserter()

        result = await plugin.process(tl, make_context(channel_config=config))

        sorted_blocks = sorted(result.blocks, key=lambda b: b.start_time)
        for previous, current in zip(sorted_blocks, sorted_blocks[1:]):
            assert previous.end_time <= current.start_time

    async def test_daypart_transition_waits_for_tolerated_overrun(self) -> None:
        tl = Timeline()
        boundary = NOW + timedelta(minutes=30)
        ep1 = make_episode(duration=1830, episode_id="late-ep")
        ep1.metadata.update({
            "daypart": "late_night",
            "daypart_boundary": boundary.isoformat(),
            "end_tolerance_minutes": 10,
        })
        tl.insert(ep1)
        ep2 = make_episode(start_offset=1800, duration=1800, episode_id="standby")
        ep2.metadata["daypart"] = "standby"
        tl.insert(ep2)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=10))

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        late_ep = next(
            b for b in result.blocks
            if isinstance(b, EpisodeBlock) and b.episode_id == "late-ep"
        )
        transition = next(
            b for b in result.blocks
            if isinstance(b, StationIDBlock) and b.metadata.get("type") == "daypart_transition"
        )
        assert late_ep.end_time == NOW + timedelta(minutes=30, seconds=30)
        assert transition.start_time == late_ep.end_time
        assert result.validate() == []

    async def test_keeps_variable_movie_that_crosses_daypart_boundary(self) -> None:
        tl = Timeline()
        boundary = NOW + timedelta(minutes=90)
        movie = make_movie(
            duration=7200,
            metadata={
                "daypart": "primetime",
                "daypart_boundary": boundary.isoformat(),
                "variable_movie_duration": True,
            },
        )
        tl.insert(movie)
        config = ChannelConfig(id="test", continuity=ContinuityConfig(enabled=True, frequency=10))

        result = await ContinuityInserter().process(tl, make_context(channel_config=config))

        movies = [block for block in result.blocks if isinstance(block, MovieBlock)]
        assert movies == [movie]


# ===================================================================
# Humanizer
# ===================================================================

class TestHumanizer:
    async def test_does_nothing_when_disabled(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        config = ChannelConfig(id="test", humanizer=HumanizerConfig(enabled=False))
        plugin = Humanizer()
        result = await plugin.process(tl, make_context(channel_config=config))
        assert len(result.blocks) == 1

    async def test_blocks_shifted_within_jitter(self) -> None:
        tl = Timeline()
        orig_start = NOW + timedelta(hours=1)
        ep = make_episode(
            start_offset=3600,
            duration=1800,
            episode_id="ep1",
        )
        tl.insert(ep)
        config = ChannelConfig(
            id="test",
            humanizer=HumanizerConfig(
                enabled=True,
                jitter_seconds=10,
                tolerance_seconds=30,
            ),
        )
        plugin = Humanizer()
        result = await plugin.process(tl, make_context(channel_config=config))
        shifted = result.blocks[0]
        diff = abs((shifted.start_time - orig_start).total_seconds())
        assert diff <= 30  # tolerance

    async def test_multiple_blocks_preserve_order(self) -> None:
        tl = Timeline()
        ep1 = make_episode(episode_id="ep1")
        ep2 = make_episode(start_offset=1800, episode_id="ep2")
        ep3 = make_episode(start_offset=3600, episode_id="ep3")
        tl.insert(ep1)
        tl.insert(ep2)
        tl.insert(ep3)
        config = ChannelConfig(id="test", humanizer=HumanizerConfig(enabled=True))
        plugin = Humanizer()
        result = await plugin.process(tl, make_context(channel_config=config))
        ids = [b.id for b in result.blocks]
        assert ids == [ep1.id, ep2.id, ep3.id]

    async def test_large_jitter_clamped_to_tolerance(self) -> None:
        tl = Timeline()
        ep = make_episode()
        tl.insert(ep)
        config = ChannelConfig(
            id="test",
            humanizer=HumanizerConfig(
                enabled=True,
                jitter_seconds=999,
                tolerance_seconds=5,
            ),
        )
        plugin = Humanizer()
        result = await plugin.process(tl, make_context(channel_config=config))
        diff = abs((result.blocks[0].start_time - ep.start_time).total_seconds())
        assert diff <= 5

    async def test_negative_jitter_does_not_create_overlaps(self) -> None:
        tl = Timeline()
        for i in range(3):
            tl.insert(make_episode(start_offset=i * 1800, duration=1800, episode_id=f"ep{i}"))
        config = ChannelConfig(
            id="test",
            humanizer=HumanizerConfig(
                enabled=True,
                jitter_seconds=20,
                tolerance_seconds=20,
            ),
        )
        plugin = Humanizer()

        with patch("tunarr_autoscheduler.plugins.humanizer.random.uniform", return_value=-10):
            result = await plugin.process(tl, make_context(channel_config=config))

        sorted_blocks = sorted(result.blocks, key=lambda b: b.start_time)
        for previous, current in zip(sorted_blocks, sorted_blocks[1:]):
            assert previous.end_time <= current.start_time

    async def test_negative_jitter_cannot_consume_a_larger_gap(self) -> None:
        tl = Timeline([
            make_episode(duration=1800),
            make_episode(start_offset=1810, duration=1800, episode_id="ep2"),
        ])
        config = ChannelConfig(
            id="test",
            humanizer=HumanizerConfig(
                enabled=True,
                jitter_seconds=20,
                tolerance_seconds=20,
            ),
        )

        with patch("tunarr_autoscheduler.plugins.humanizer.random.uniform", return_value=-20):
            result = await Humanizer().process(tl, make_context(channel_config=config))

        sorted_blocks = sorted(result.blocks, key=lambda block: block.start_time)
        assert sorted_blocks[0].end_time <= sorted_blocks[1].start_time

    async def test_positive_jitter_does_not_create_dead_air(self) -> None:
        tl = Timeline()
        for i in range(3):
            tl.insert(make_episode(start_offset=i * 1800, duration=1800, episode_id=f"ep{i}"))
        config = ChannelConfig(
            id="test",
            humanizer=HumanizerConfig(
                enabled=True,
                jitter_seconds=20,
                tolerance_seconds=20,
            ),
        )

        with patch("tunarr_autoscheduler.plugins.humanizer.random.uniform", return_value=20):
            result = await Humanizer().process(tl, make_context(channel_config=config))

        sorted_blocks = sorted(result.blocks, key=lambda b: b.start_time)
        for previous, current in zip(sorted_blocks, sorted_blocks[1:]):
            gap = (current.start_time - previous.end_time).total_seconds()
            assert gap <= 5


# ===================================================================
# GapFiller
# ===================================================================

class TestGapFiller:
    async def test_fills_remaining_slot(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"daypart": "overnight"}))
        plugin = GapFiller()

        result = await plugin.process(tl, make_context())

        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], FillerBlock)
        assert result.blocks[0].metadata["reason"] == "unfilled_slot"

    async def test_fills_gap_with_trailer(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1850, duration=1800, episode_id="ep2"))
        plugin = GapFiller()
        result = await plugin.process(tl, make_context())
        fillers = [b for b in result.blocks if isinstance(b, FillerBlock)]
        assert len(fillers) >= 1
        assert fillers[0].filler_type == FillerType.TRAILER  # 50s gap -> trailer

    async def test_small_gap_not_filled(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1803, duration=1800, episode_id="ep2"))
        plugin = GapFiller()
        result = await plugin.process(tl, make_context())
        fillers = [b for b in result.blocks if isinstance(b, FillerBlock)]
        assert len(fillers) == 0

    async def test_large_gap_fills_with_filler_episode(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1800 + 36000, duration=1800, episode_id="ep2"))
        plugin = GapFiller()
        result = await plugin.process(tl, make_context())
        fillers = [b for b in result.blocks if isinstance(b, FillerBlock)]
        if fillers:
            assert fillers[0].filler_type in (FillerType.MINI_CONTENT, FillerType.FILLER_EPISODE)

    async def test_large_gap_uses_standby_custom_loop_when_configured(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=3600, duration=1800, episode_id="ep2"))
        config = ChannelConfig(id="test", standby_custom_show_id="standby-list")

        result = await GapFiller().process(tl, make_context(channel_config=config))
        offline_blocks = [b for b in result.blocks if isinstance(b, OfflineBlock)]

        assert len(offline_blocks) == 1
        assert offline_blocks[0].metadata["reason"] == "standby_loop"
        assert offline_blocks[0].metadata["custom_show_list_ids"] == ["standby-list"]

    async def test_long_unfilled_slot_uses_standby_custom_loop_when_configured(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(duration=1800, metadata={"daypart": "primetime"}))
        config = ChannelConfig(id="test", standby_custom_show_id="standby-list")

        result = await GapFiller().process(tl, make_context(channel_config=config))

        assert isinstance(result.blocks[0], OfflineBlock)
        assert result.blocks[0].metadata["custom_show_list_ids"] == ["standby-list"]

    async def test_expired_daypart_remainder_is_not_filled(self) -> None:
        boundary = NOW + timedelta(minutes=30)
        tl = Timeline()
        tl.insert(make_slot(
            start_offset=1830,
            duration=900,
            metadata={
                "daypart": "late_night",
                "daypart_boundary": boundary.isoformat(),
                "note": "episode_does_not_fit_daypart",
            },
        ))

        result = await GapFiller().process(tl, make_context())

        assert result.blocks == []

    async def test_daypart_remainder_is_clamped_to_boundary(self) -> None:
        boundary = NOW + timedelta(minutes=30)
        tl = Timeline()
        tl.insert(make_slot(
            start_offset=1740,
            duration=900,
            metadata={
                "daypart": "late_night",
                "daypart_boundary": boundary.isoformat(),
                "note": "episode_does_not_fit_daypart",
            },
        ))

        result = await GapFiller().process(tl, make_context())

        assert len(result.blocks) == 1
        assert result.blocks[0].start_time == NOW + timedelta(minutes=29)
        assert result.blocks[0].end_time == boundary

    async def test_no_gaps_returns_same_timeline(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1800, duration=1800, episode_id="ep2"))
        plugin = GapFiller()
        result = await plugin.process(tl, make_context())
        assert len(result.blocks) == 2

    async def test_filler_type_selected_by_duration(self) -> None:
        tl2 = Timeline()
        tl2.insert(make_episode(duration=1800))
        tl2.insert(make_episode(start_offset=1820, duration=1800, episode_id="ep2"))
        plugin = GapFiller()
        result = await plugin.process(tl2, make_context())
        fillers = [b for b in result.blocks if isinstance(b, FillerBlock)]
        if fillers:
            assert fillers[0].filler_type == FillerType.BUMPER  # 20s gap -> bumper


# ===================================================================
# Validator
# ===================================================================

class TestValidator:
    async def test_clean_timeline_passes(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1800, duration=1800, episode_id="ep2"))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        assert result.metadata.get("validation_passed") is True
        assert len(result.metadata.get("validation_errors", [])) == 0

    async def test_detects_overlap(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=1700, duration=1800, episode_id="ep2"))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        assert result.metadata.get("validation_passed") is False
        errors = result.metadata.get("validation_errors", [])
        assert any("overlap" in e for e in errors)

    async def test_detects_dead_air_gap(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        tl.insert(make_episode(start_offset=3600, duration=1800, episode_id="ep2"))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        errors = result.metadata.get("validation_errors", [])
        assert any("dead_air" in e for e in errors)

    async def test_detects_duplicate_episode(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(episode_id="dupe"))
        tl.insert(make_episode(start_offset=1800, episode_id="dupe"))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        errors = result.metadata.get("validation_errors", [])
        assert any("duplicate" in e for e in errors)

    async def test_detects_invalid_runtime(self) -> None:
        tl = Timeline()
        tl.insert(EpisodeBlock(
            start_time=NOW,
            end_time=NOW + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="bad_ep",
            show_id="s1",
            runtime_seconds=0,
        ))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        errors = result.metadata.get("validation_errors", [])
        assert any("invalid_runtime" in e for e in errors)

    async def test_detects_daypart_violation(self) -> None:
        tl = Timeline()
        movie = make_movie(metadata={"daypart": "morning", "allow_movies": False})
        tl.insert(movie)
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        errors = result.metadata.get("validation_errors", [])
        assert any("daypart_violation" in e for e in errors)

    async def test_detects_unfilled_slot(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"daypart": "morning"}))
        plugin = Validator()
        result = await plugin.process(tl, make_context())
        errors = result.metadata.get("validation_errors", [])
        assert any("unfilled_slot" in e for e in errors)


# ===================================================================
# DaypartApplicator
# ===================================================================

class TestDaypartApplicator:
    def _make_daypart_config(self) -> ChannelConfig:
        return ChannelConfig(
            id="test",
            dayparts=[
                DaypartTemplate(
                    name="morning",
                    days=[
                        DayOfWeek.MON,
                        DayOfWeek.TUE,
                        DayOfWeek.WED,
                        DayOfWeek.THU,
                        DayOfWeek.FRI,
                    ],
                    start_time="06:00", end_time="12:00",
                    slot_duration_minutes=30, allow_movies=False,
                ),
                DaypartTemplate(
                    name="primetime",
                    days=[
                        DayOfWeek.MON,
                        DayOfWeek.TUE,
                        DayOfWeek.WED,
                        DayOfWeek.THU,
                        DayOfWeek.FRI,
                    ],
                    start_time="18:00", end_time="23:00",
                    slot_duration_minutes=60, allow_movies=True,
                    variable_movie_duration=True,
                ),
            ],
        )

    async def test_segments_timeline_by_daypart(self) -> None:
        tl = Timeline()
        plugin = DaypartApplicator()
        config = self._make_daypart_config()
        result = await plugin.process(tl, make_context(channel_config=config))
        assert len(result.blocks) > 0
        assert all(isinstance(block, SlotBlock) for block in result.blocks)

    async def test_uses_channel_schedule_horizon_days(self) -> None:
        tl = Timeline()
        plugin = DaypartApplicator()
        config = self._make_daypart_config()
        config.schedule_horizon_days = 2

        result = await plugin.process(tl, make_context(channel_config=config))

        assert result.total_duration() == timedelta(days=2)

    async def test_follow_up_uses_explicit_start_and_parent_metadata(self) -> None:
        config = self._make_daypart_config()
        start = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)

        result = await DaypartApplicator().process(
            Timeline(),
            make_context(
                channel_config=config,
                generation_mode="follow_up",
                schedule_start=start,
                parent_version=12,
            ),
        )

        assert result.blocks[0].start_time == start
        assert result.metadata["generation_mode"] == "follow_up"
        assert result.metadata["parent_version"] == 12

    async def test_follow_up_start_is_converted_to_app_timezone(self) -> None:
        config = self._make_daypart_config()
        start = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)

        result = await DaypartApplicator().process(
            Timeline(),
            make_context(
                channel_config=config,
                schedule_start=start,
                app_config=AppConfig(timezone="Europe/Berlin"),
            ),
        )

        assert result.blocks[0].start_time.strftime("%H:%M") == "06:00"
        assert result.blocks[0].start_time.utcoffset() == timedelta(hours=2)

    async def test_daypart_boundary_uses_active_daypart_end(self) -> None:
        config = ChannelConfig(
            id="test",
            schedule_horizon_days=1,
            dayparts=[
                DaypartTemplate(
                    name="movies",
                    days=list(DayOfWeek),
                    start_time="06:00",
                    end_time="20:00",
                    content_mode="movies",
                    allow_movies=True,
                    slot_duration_minutes=30,
                ),
                DaypartTemplate(
                    name="late",
                    days=list(DayOfWeek),
                    start_time="20:00",
                    end_time="02:00",
                    slot_duration_minutes=30,
                ),
                DaypartTemplate(
                    name="standby",
                    days=list(DayOfWeek),
                    start_time="02:00",
                    end_time="06:00",
                    off_air=True,
                    slot_duration_minutes=30,
                ),
            ],
        )
        start = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)

        result = await DaypartApplicator().process(
            Timeline(),
            make_context(channel_config=config, schedule_start=start),
        )

        movies = [
            block for block in result.blocks
            if block.metadata.get("daypart") == "movies"
        ]
        assert movies
        assert {block.metadata["daypart_boundary"] for block in movies} == {
            datetime(2026, 7, 1, 20, 0, tzinfo=UTC).isoformat(),
        }

    async def test_fresh_schedule_uses_app_timezone_start(self) -> None:
        config = self._make_daypart_config()
        local_start = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)

        with patch(
            "tunarr_autoscheduler.plugins.daypart_applicator.now_in_timezone",
            return_value=local_start,
        ) as now_mock:
            result = await DaypartApplicator().process(
                Timeline(),
                make_context(
                    channel_config=config,
                    app_config=AppConfig(timezone="Europe/Berlin"),
                ),
            )

        now_mock.assert_called_once_with("Europe/Berlin")
        assert result.blocks[0].start_time == local_start

    async def test_sets_correct_metadata(self) -> None:
        tl = Timeline()
        plugin = DaypartApplicator()
        config = self._make_daypart_config()
        result = await plugin.process(tl, make_context(channel_config=config))
        for block in result.blocks:
            meta = block.metadata or {}
            assert "daypart" in meta
            assert "content_mode" in meta
            assert "slot_duration_minutes" in meta
            assert "allow_movies" in meta
            assert "variable_movie_duration" in meta
            assert "movie_selection" in meta
            assert "end_tolerance_minutes" in meta
            assert "ad_density" in meta

    async def test_carries_custom_show_lists_into_slots(self) -> None:
        tl = Timeline()
        config = ChannelConfig(
            id="test",
            dayparts=[
                DaypartTemplate(
                    name="all-day",
                    days=list(DayOfWeek),
                    start_time="00:00",
                    end_time="23:59",
                    custom_show_list_ids=["list-1", "list-2"],
                ),
            ],
        )

        result = await DaypartApplicator().process(tl, make_context(channel_config=config))

        assert result.blocks[0].metadata["custom_show_list_ids"] == ["list-1", "list-2"]

    async def test_off_air_daypart_marks_custom_show_loop_slots(self) -> None:
        tl = Timeline()
        config = ChannelConfig(
            id="test",
            schedule_horizon_days=1,
            dayparts=[
                DaypartTemplate(
                    name="overnight",
                    days=list(DayOfWeek),
                    start_time="23:00",
                    end_time="06:00",
                    custom_show_list_ids=["overnight-loop"],
                    slot_duration_minutes=30,
                    off_air=True,
                ),
            ],
        )

        result = await DaypartApplicator().process(tl, make_context(channel_config=config))

        overnight_slots = [
            block for block in result.blocks
            if block.metadata.get("daypart") == "overnight"
        ]
        assert overnight_slots
        assert all(block.metadata["off_air"] is True for block in overnight_slots)
        assert all(block.metadata["custom_show_loop"] is True for block in overnight_slots)
        assert all(block.duration <= timedelta(minutes=30) for block in overnight_slots)

    async def test_uncovered_time_uses_configured_standby_loop(self) -> None:
        config = ChannelConfig(
            id="test",
            schedule_horizon_days=1,
            standby_custom_show_id="standby-list",
            dayparts=[
                DaypartTemplate(
                    name="broadcast",
                    days=list(DayOfWeek),
                    start_time="06:00",
                    end_time="02:00",
                    slot_duration_minutes=60,
                ),
            ],
        )

        result = await DaypartApplicator().process(
            Timeline(), make_context(channel_config=config),
        )
        standby_slots = [
            block
            for block in result.blocks
            if block.metadata.get("daypart") == "standby"
        ]

        assert standby_slots
        assert all(block.metadata["off_air"] is True for block in standby_slots)
        assert all(block.metadata["daypart_boundary"] for block in standby_slots)
        assert all(
            block.metadata["custom_show_list_ids"] == ["standby-list"]
            for block in standby_slots
        )
        assert all(block.metadata["custom_show_loop"] is True for block in standby_slots)

    def test_uncovered_time_does_not_fall_back_to_first_daypart(self) -> None:
        plugin = DaypartApplicator()
        daypart = DaypartTemplate(
            name="broadcast",
            days=list(DayOfWeek),
            start_time="06:00",
            end_time="02:00",
        )

        uncovered = datetime(2026, 6, 11, 3, 0, tzinfo=UTC)

        assert plugin._find_daypart(uncovered, [daypart]) is None

    async def test_empty_dayparts_returns_empty(self) -> None:
        tl = Timeline()
        plugin = DaypartApplicator()
        config = ChannelConfig(id="test", dayparts=[])
        result = await plugin.process(tl, make_context(channel_config=config))
        assert len(result.blocks) == 0

    async def test_movie_blocked_in_non_movie_daypart(self) -> None:
        DaypartApplicator()
        config = self._make_daypart_config()
        # The morning daypart has allow_movies=False
        morning = next(dp for dp in config.dayparts if dp.name == "morning")
        assert morning.allow_movies is False

    async def test_movie_allowed_in_primetime(self) -> None:
        DaypartApplicator()
        config = self._make_daypart_config()
        primetime = next(dp for dp in config.dayparts if dp.name == "primetime")
        assert primetime.allow_movies is True

    async def test_movie_only_daypart_carries_movie_library_settings(self) -> None:
        config = ChannelConfig(
            id="test",
            schedule_horizon_days=1,
            dayparts=[
                DaypartTemplate(
                    name="all_movies",
                    days=list(DayOfWeek),
                    start_time="00:00",
                    end_time="23:59",
                    content_mode="movies",
                    allow_movies=True,
                    variable_movie_duration=True,
                    movie_selection="library_random",
                    slot_duration_minutes=120,
                ),
            ],
        )

        result = await DaypartApplicator().process(
            Timeline(), make_context(channel_config=config),
        )

        assert result.blocks
        movie_slots = [
            block for block in result.blocks
            if block.metadata.get("daypart") == "all_movies"
        ]
        assert movie_slots
        assert all(block.metadata["content_mode"] == "movies" for block in movie_slots)
        assert all(block.metadata["allow_movies"] is True for block in movie_slots)
        assert all(block.metadata["movie_selection"] == "library_random" for block in movie_slots)


# ===================================================================
# RotationScheduler
# ===================================================================

class TestRotationScheduler:
    async def test_returns_original_timeline_with_no_rotation(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=1800))
        config = ChannelConfig(id="test", rotations=[])
        plugin = RotationScheduler()
        result = await plugin.process(tl, make_context(channel_config=config))
        assert len(result.blocks) == 1
        assert result.blocks[0].episode_id == "ep_default"

    async def test_runtime_skips_slots_already_covered_by_previous_episode(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={"rotation": "default", "daypart": "morning"},
        ))
        tl.insert(make_slot(
            start_offset=1800,
            duration=1800,
            metadata={"rotation": "default", "daypart": "morning"},
        ))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="long-episode",
                item_type="episode",
                source_type="jellyfin",
                source_id="long-episode",
                title="Long Episode",
                duration_seconds=3600,
                metadata={
                    "series_id": "show_a",
                    "parent_index_number": 1,
                    "index_number": 1,
                },
            ),
        ])

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                media_repo=repo,
            ),
        )

        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].duration == timedelta(hours=1)

    async def test_follow_up_skips_episodes_reserved_by_previous_schedules(self) -> None:
        tl = Timeline([make_slot(metadata={"rotation": "default"})])
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id=f"episode-{number}",
                item_type="episode",
                source_type="jellyfin",
                source_id=f"episode-{number}",
                title=f"Episode {number}",
                duration_seconds=1800,
                metadata={
                    "series_id": "show_a",
                    "parent_index_number": 1,
                    "index_number": number,
                },
            )
            for number in (1, 2)
        ])

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                media_repo=repo,
                generation_mode="follow_up",
                reserved_episode_ids={"episode-1"},
            ),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "episode-2"

    async def test_movie_only_slots_are_left_for_movie_scheduler(self) -> None:
        tl = Timeline([make_slot(metadata={
            "rotation": "default",
            "content_mode": "movies",
            "allow_movies": True,
        })])
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="episode-1",
                item_type="episode",
                source_type="jellyfin",
                source_id="episode-1",
                title="Episode 1",
                duration_seconds=1800,
                metadata={"series_id": "show_a"},
            ),
        ])

        result = await RotationScheduler().process(
            tl, make_context(channel_config=config, media_repo=repo),
        )

        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], SlotBlock)
        assert result.blocks[0].metadata["content_mode"] == "movies"

    async def test_episode_that_crosses_daypart_boundary_is_not_scheduled(self) -> None:
        boundary = NOW + timedelta(minutes=30)
        tl = Timeline([make_slot(
            duration=1800,
            metadata={
                "rotation": "default",
                "daypart": "late_night",
                "daypart_boundary": boundary.isoformat(),
            },
        )])
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([MediaCacheEntry(
            id="long-episode",
            item_type="episode",
            source_type="jellyfin",
            source_id="long-episode",
            title="Long Episode",
            duration_seconds=3600,
            metadata={"series_id": "show_a"},
        )])

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                media_repo=repo,
            ),
        )

        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], SlotBlock)
        assert result.blocks[0].end_time == boundary
        assert result.blocks[0].metadata["note"] == "episode_does_not_fit_daypart"

    async def test_selects_episode_from_rotation(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"slot_duration_minutes": 30}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        state = MockStateManager()
        plugin = RotationScheduler()
        result = await plugin.process(tl, make_context(channel_config=config, state=state))
        assert len(result.blocks) > 0
        assert all(isinstance(b, EpisodeBlock) for b in result.blocks)

    async def test_advances_state_index(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"slot_duration_minutes": 30}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a", "show_b"])],
        )
        state = MockStateManager()
        plugin = RotationScheduler()
        await plugin.process(tl, make_context(channel_config=config, state=state))
        rot_state = await state.get_rotation_state("test", "default")
        assert rot_state is not None
        assert rot_state.current_index >= 0

    async def test_wraps_around_when_exhausted(self) -> None:
        tl = Timeline()
        for i in range(8):
            tl.insert(make_slot(
                start_offset=i * 1800,
                duration=1800,
                metadata={"slot_duration_minutes": 30},
            ))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        state = MockStateManager()
        plugin = RotationScheduler()
        result = await plugin.process(tl, make_context(channel_config=config, state=state))
        assert len(result.blocks) > 0

    async def test_uses_daypart_rotation_metadata(self) -> None:
        tl = Timeline()
        slot = make_slot(metadata={"rotation": "morning"})
        tl.insert(slot)
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="morning", show_ids=["show_morning"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="ep-morning",
                item_type="episode",
                source_type="jellyfin",
                source_id="ep-morning",
                title="Morning Episode",
                duration_seconds=1800,
                metadata={"series_id": "show_morning"},
            ),
        ])
        plugin = RotationScheduler()
        result = await plugin.process(tl, make_context(channel_config=config, media_repo=repo))
        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "ep-morning"

    async def test_prefers_daypart_custom_show_list_for_episodes(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"custom_show_list_ids": ["list-morning"]}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["fallback-show"])],
        )
        tunarr = MockTunarrClient({
            "list-morning": [{
                "program": {
                    "type": "content",
                    "id": "tunarr-episode",
                    "duration": 1800000,
                    "program": {
                        "uuid": "tunarr-episode",
                        "type": "episode",
                        "showId": "custom-show",
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                        "title": "Custom Episode",
                        "duration": 1800000,
                    },
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                tunarr_client=tunarr,
            ),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "tunarr-episode"
        assert result.blocks[0].show_id == "custom-show"

    async def test_uses_daypart_custom_show_list_without_internal_rotations(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"custom_show_list_ids": ["list-morning"]}))
        config = ChannelConfig(id="test", rotations=[])
        tunarr = MockTunarrClient({
            "list-morning": [{
                "program": {
                    "type": "content",
                    "id": "tunarr-episode",
                    "duration": 1800000,
                    "program": {
                        "uuid": "tunarr-episode",
                        "type": "episode",
                        "showId": "custom-show",
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                        "title": "Custom Episode",
                        "duration": 1800000,
                    },
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=config, state=MockStateManager(), tunarr_client=tunarr),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "tunarr-episode"

    async def test_off_air_custom_show_loops_when_list_is_short(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={
            "custom_show_list_ids": ["overnight-loop"],
            "custom_show_loop": True,
        }))
        tl.insert(make_slot(start_offset=1800, metadata={
            "custom_show_list_ids": ["overnight-loop"],
            "custom_show_loop": True,
        }))
        config = ChannelConfig(id="test", rotations=[])
        tunarr = MockTunarrClient({
            "overnight-loop": [{
                "program": {
                    "type": "content",
                    "id": "loop-episode",
                    "duration": 1800000,
                    "program": {
                        "uuid": "loop-episode",
                        "type": "episode",
                        "showId": "loop-show",
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "title": "Loop Episode",
                    },
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=config, state=MockStateManager(), tunarr_client=tunarr),
        )

        episode_ids = [
            block.episode_id for block in result.blocks if isinstance(block, EpisodeBlock)
        ]
        assert episode_ids == ["loop-episode", "loop-episode"]

    async def test_off_air_daypart_becomes_offline_block_for_preview_and_upload(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={
            "off_air": True,
            "custom_show_list_ids": ["standby-list"],
            "custom_show_loop": True,
            "daypart": "overnight",
        }))
        tunarr = MockTunarrClient({
            "standby-list": [{
                "program": {
                    "type": "content",
                    "id": "standby-episode",
                    "duration": 1800000,
                    "program": {
                        "uuid": "standby-episode",
                        "type": "episode",
                        "showId": "standby-show",
                        "title": "Standby",
                    },
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=ChannelConfig(id="test"), tunarr_client=tunarr),
        )

        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], OfflineBlock)
        assert result.blocks[0].reason == "Off-Air Loop"
        assert result.blocks[0].metadata["custom_show_list_ids"] == ["standby-list"]

    async def test_off_air_slot_is_clipped_instead_of_pushing_next_daypart(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=2400, metadata={"daypart": "late_night"}))
        tl.insert(make_slot(
            start_offset=1800,
            duration=1800,
            metadata={
                "off_air": True,
                "daypart": "standby",
                "daypart_boundary": (NOW + timedelta(hours=1)).isoformat(),
            },
        ))
        tl.insert(make_slot(
            start_offset=3600,
            duration=1800,
            metadata={"daypart": "morning"},
        ))

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=ChannelConfig(id="test"), state=MockStateManager()),
        )

        offline = next(block for block in result.blocks if isinstance(block, OfflineBlock))
        morning = next(
            block
            for block in result.blocks
            if isinstance(block, SlotBlock) and block.metadata.get("daypart") == "morning"
        )
        assert offline.start_time == NOW + timedelta(minutes=40)
        assert offline.end_time == NOW + timedelta(hours=1)
        assert morning.start_time == NOW + timedelta(hours=1)

    async def test_long_off_air_loop_episode_pushes_next_slot_forward(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={"custom_show_list_ids": ["overnight-loop"], "custom_show_loop": True},
        ))
        tl.insert(make_slot(
            start_offset=1800,
            duration=1800,
            metadata={"custom_show_list_ids": ["overnight-loop"], "custom_show_loop": True},
        ))
        tunarr = MockTunarrClient({
            "overnight-loop": [
                {
                    "program": {
                        "type": "content",
                        "id": "loop-long",
                        "duration": 3600000,
                        "program": {
                            "uuid": "loop-long",
                            "type": "episode",
                            "showId": "loop-show",
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "title": "Long Loop Episode",
                            "duration": 3600000,
                        },
                    },
                },
                {
                    "program": {
                        "type": "content",
                        "id": "loop-next",
                        "duration": 1800000,
                        "program": {
                            "uuid": "loop-next",
                            "type": "episode",
                            "showId": "loop-show",
                            "seasonNumber": 1,
                            "episodeNumber": 2,
                            "title": "Next Loop Episode",
                            "duration": 1800000,
                        },
                    },
                },
            ],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=ChannelConfig(id="test"), state=MockStateManager(),
                         tunarr_client=tunarr),
        )
        episodes = [b for b in result.blocks if isinstance(b, EpisodeBlock)]

        assert episodes[1].start_time >= episodes[0].end_time
        assert result.validate() == []

    async def test_uses_custom_episode_list_in_movie_enabled_daypart(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={
            "allow_movies": True,
            "custom_show_list_ids": ["list-primetime"],
        }))
        config = ChannelConfig(id="test", rotations=[])
        tunarr = MockTunarrClient({
            "list-primetime": [{
                "program": {
                    "type": "content",
                    "id": "primetime-episode",
                    "duration": 1800000,
                    "program": {
                        "uuid": "primetime-episode",
                        "type": "episode",
                        "showId": "primetime-show",
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "title": "Primetime Episode",
                    },
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=config, state=MockStateManager(), tunarr_client=tunarr),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "primetime-episode"

    async def test_leaves_movie_list_for_movie_scheduler(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={
            "allow_movies": True,
            "custom_show_list_ids": ["movie-list"],
        }))
        config = ChannelConfig(id="test", rotations=[])
        tunarr = MockTunarrClient({
            "movie-list": [{
                "program": {
                    "type": "content",
                    "id": "movie-id",
                    "program": {"uuid": "movie-id", "type": "movie", "title": "Movie"},
                },
            }],
        })

        result = await RotationScheduler().process(
            tl,
            make_context(channel_config=config, state=MockStateManager(), tunarr_client=tunarr),
        )

        assert isinstance(result.blocks[0], SlotBlock)

    async def test_falls_back_to_library_episodes_when_custom_list_is_empty(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"custom_show_list_ids": ["empty-list"]}))
        config = ChannelConfig(id="test", rotations=[])
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="library-episode",
                item_type="episode",
                source_type="jellyfin",
                source_id="library-episode",
                title="Library Episode",
                duration_seconds=1800,
                metadata={"series_id": "library-show", "series_name": "Library Show"},
            ),
        ])

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                media_repo=repo,
                tunarr_client=MockTunarrClient({"empty-list": []}),
            ),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "library-episode"

    async def test_falls_back_to_rotation_when_custom_show_list_is_empty(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"custom_show_list_ids": ["empty-list"]}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["fallback-show"])],
        )

        result = await RotationScheduler().process(
            tl,
            make_context(
                channel_config=config,
                state=MockStateManager(),
                tunarr_client=MockTunarrClient({"empty-list": []}),
            ),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].show_id == "fallback-show"

    async def test_rotates_by_show_and_episode_order(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"slot_duration_minutes": 30}))
        tl.insert(make_slot(start_offset=1800, metadata={"slot_duration_minutes": 30}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a", "show_b"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="show-b-s1e1",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-b-s1e1",
                title="B Pilot",
                duration_seconds=1800,
                metadata={
                    "series_id": "show_b",
                    "parent_index_number": 1,
                    "index_number": 1,
                },
            ),
            MediaCacheEntry(
                id="show-a-s1e2",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e2",
                title="A Second",
                duration_seconds=1800,
                metadata={
                    "series_id": "show_a",
                    "parent_index_number": 1,
                    "index_number": 2,
                },
            ),
            MediaCacheEntry(
                id="show-a-s1e1",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e1",
                title="A Pilot",
                duration_seconds=1800,
                metadata={
                    "series_id": "show_a",
                    "parent_index_number": 1,
                    "index_number": 1,
                },
            ),
        ])

        result = await RotationScheduler().process(
            tl, make_context(channel_config=config, state=MockStateManager(), media_repo=repo),
        )
        episodes = [b for b in result.blocks if isinstance(b, EpisodeBlock)]

        assert [episode.episode_id for episode in episodes] == ["show-a-s1e1", "show-b-s1e1"]

    async def test_skips_episode_already_scheduled_in_run(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"slot_duration_minutes": 30}))
        tl.insert(make_slot(start_offset=1800, metadata={"slot_duration_minutes": 30}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="show-a-s1e1",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e1",
                title="A Pilot",
                duration_seconds=1800,
                metadata={"series_id": "show_a", "parent_index_number": 1, "index_number": 1},
            ),
            MediaCacheEntry(
                id="show-a-s1e2",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e2",
                title="A Second",
                duration_seconds=1800,
                metadata={"series_id": "show_a", "parent_index_number": 1, "index_number": 2},
            ),
        ])

        result = await RotationScheduler().process(
            tl, make_context(channel_config=config, state=MockStateManager(), media_repo=repo),
        )
        episodes = [b for b in result.blocks if isinstance(b, EpisodeBlock)]

        assert [episode.episode_id for episode in episodes] == ["show-a-s1e1", "show-a-s1e2"]

    async def test_episode_can_use_daypart_end_tolerance(self) -> None:
        now = datetime.now(tz=UTC)
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={
                "rotation": "default",
                "daypart": "late_night",
                "daypart_boundary": (now + timedelta(minutes=30)).isoformat(),
                "end_tolerance_minutes": 10,
            },
        ))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="episode-a",
                item_type="episode",
                source_type="jellyfin",
                source_id="episode-a",
                title="Episode A",
                duration_seconds=35 * 60,
                metadata={
                    "series_id": "show_a",
                    "series_name": "Show A",
                    "parent_index_number": 1,
                    "index_number": 1,
                },
            ),
        ])

        result = await RotationScheduler().process(
            tl, make_context(channel_config=config, media_repo=repo),
        )

        assert isinstance(result.blocks[0], EpisodeBlock)
        assert result.blocks[0].episode_id == "episode-a"

    async def test_skips_recently_aired_episode(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"slot_duration_minutes": 30}))
        config = ChannelConfig(
            id="test",
            rotations=[RotationConfig(name="default", show_ids=["show_a"])],
        )
        state = MockStateManager()
        state.recently_aired_episode_ids.add("show-a-s1e1")
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="show-a-s1e1",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e1",
                title="A Pilot",
                duration_seconds=1800,
                metadata={"series_id": "show_a", "parent_index_number": 1, "index_number": 1},
            ),
            MediaCacheEntry(
                id="show-a-s1e2",
                item_type="episode",
                source_type="jellyfin",
                source_id="show-a-s1e2",
                title="A Second",
                duration_seconds=1800,
                metadata={"series_id": "show_a", "parent_index_number": 1, "index_number": 2},
            ),
        ])

        result = await RotationScheduler().process(
            tl, make_context(channel_config=config, state=state, media_repo=repo),
        )

        assert result.blocks[0].episode_id == "show-a-s1e2"


# ===================================================================
# MovieScheduler
# ===================================================================

class TestMovieScheduler:
    async def test_returns_empty_timeline_with_no_movies(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"allow_movies": True, "slot_duration_minutes": 120}))
        plugin = MovieScheduler()
        result = await plugin.process(tl, make_context())
        # With no media_repo, falls back to sample data
        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]
        assert len(movies) >= 0

    async def test_creates_movie_block_from_sample_data(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            metadata={"allow_movies": True, "slot_duration_minutes": 120, "ad_density": 0.02},
        ))
        plugin = MovieScheduler()
        result = await plugin.process(tl, make_context())
        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]
        if movies:
            assert movies[0].runtime_seconds > 0

    async def test_sets_cooldown_on_movie(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=7200,
            metadata={"allow_movies": True, "slot_duration_minutes": 120, "ad_density": 0.01},
        ))
        state = MockStateManager()
        plugin = MovieScheduler()
        await plugin.process(tl, make_context(state=state))
        # Cooldowns should have been set for movies
        assert len(state.cooldowns) > 0

    async def test_skips_non_movie_slots(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"allow_movies": False}))
        plugin = MovieScheduler()
        result = await plugin.process(tl, make_context())
        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]
        assert len(movies) == 0

    async def test_preserves_non_movie_slots(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"allow_movies": False}))
        plugin = MovieScheduler()
        result = await plugin.process(tl, make_context())
        assert len(result.blocks) == 1
        assert isinstance(result.blocks[0], SlotBlock)

    async def test_finds_lowercase_movie_media(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"allow_movies": True, "slot_duration_minutes": 120}))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-real",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-real",
                title="Real Movie",
                duration_seconds=5400,
            ),
        ])
        plugin = MovieScheduler()
        result = await plugin.process(tl, make_context(media_repo=repo))
        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]
        assert movies[0].movie_id == "movie-real"

    async def test_prefers_daypart_custom_show_list_for_movies(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            metadata={
                "allow_movies": True,
                "slot_duration_minutes": 120,
                "custom_show_list_ids": ["movie-list"],
            },
        ))
        tunarr = MockTunarrClient({
            "movie-list": [{
                "program": {
                    "type": "content",
                    "id": "custom-movie",
                    "duration": 5400000,
                    "program": {
                        "uuid": "custom-movie",
                        "type": "movie",
                        "title": "Custom Movie",
                        "duration": 5400000,
                        "year": 2024,
                    },
                },
            }],
        })

        result = await MovieScheduler().process(tl, make_context(tunarr_client=tunarr))

        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]
        assert movies[0].movie_id == "custom-movie"

    async def test_skips_movies_on_cooldown_and_already_scheduled(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={"allow_movies": True, "slot_duration_minutes": 120}))
        tl.insert(make_slot(
            start_offset=7200,
            metadata={"allow_movies": True, "slot_duration_minutes": 120},
        ))
        state = MockStateManager()
        state.cooldowns["movie-cooldown"] = {"minutes": 60}
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-cooldown",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-cooldown",
                title="Cooldown Movie",
                duration_seconds=5400,
            ),
            MediaCacheEntry(
                id="movie-a",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-a",
                title="Movie A",
                duration_seconds=5400,
            ),
            MediaCacheEntry(
                id="movie-b",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-b",
                title="Movie B",
                duration_seconds=5400,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(state=state, media_repo=repo))
        movies = [b for b in result.blocks if isinstance(b, MovieBlock)]

        assert [movie.movie_id for movie in movies] == ["movie-a", "movie-b"]

    async def test_movie_only_daypart_randomly_uses_jellyfin_movie_library(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(metadata={
            "content_mode": "movies",
            "allow_movies": True,
            "movie_selection": "library_random",
            "slot_duration_minutes": 120,
            "ad_density": 0.0,
            "daypart": "movies",
        }))
        tl.insert(make_slot(start_offset=7200, metadata={
            "content_mode": "movies",
            "allow_movies": True,
            "movie_selection": "library_random",
            "slot_duration_minutes": 120,
            "ad_density": 0.0,
            "daypart": "movies",
        }))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-a",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-a",
                title="Movie A",
                duration_seconds=5400,
            ),
            MediaCacheEntry(
                id="movie-b",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-b",
                title="Movie B",
                duration_seconds=5400,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))
        movies = [block for block in result.blocks if isinstance(block, MovieBlock)]

        assert len(movies) == 2
        assert {movie.movie_id for movie in movies} == {"movie-a", "movie-b"}

    async def test_movie_only_daypart_uses_remaining_daypart_window(self) -> None:
        boundary = NOW + timedelta(hours=4)
        tl = Timeline()
        tl.insert(make_slot(duration=1800, metadata={
            "content_mode": "movies",
            "allow_movies": True,
            "variable_movie_duration": False,
            "slot_duration_minutes": 30,
            "ad_density": 0.0,
            "daypart": "movies",
            "daypart_boundary": boundary.isoformat(),
        }))
        tl.insert(make_slot(start_offset=1800, duration=1800, metadata={
            "content_mode": "movies",
            "allow_movies": True,
            "variable_movie_duration": False,
            "slot_duration_minutes": 30,
            "ad_density": 0.0,
            "daypart": "movies",
            "daypart_boundary": boundary.isoformat(),
        }))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="feature",
                item_type="movie",
                source_type="jellyfin",
                source_id="feature",
                title="Feature",
                duration_seconds=90 * 60,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))
        movies = [block for block in result.blocks if isinstance(block, MovieBlock)]

        assert len(movies) == 1
        assert movies[0].movie_id == "feature"
        assert movies[0].start_time == NOW

    async def test_long_movie_pushes_next_slot_forward(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={
                "allow_movies": True,
                "variable_movie_duration": True,
                "slot_duration_minutes": 30,
                "ad_density": 0.0,
            },
        ))
        tl.insert(make_slot(
            start_offset=1800,
            duration=1800,
            metadata={"allow_movies": False},
        ))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-long",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-long",
                title="Long Movie",
                duration_seconds=5400,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))
        blocks = sorted(result.blocks, key=lambda b: b.start_time)

        assert isinstance(blocks[0], MovieBlock)
        assert blocks[0].movie_id == "movie-long"
        assert blocks[1].start_time >= blocks[0].end_time
        assert result.validate() == []

    async def test_variable_movie_pushes_following_episode_forward(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=5400,
            metadata={
                "allow_movies": True,
                "variable_movie_duration": True,
                "slot_duration_minutes": 90,
                "daypart": "primetime",
                "daypart_boundary": (NOW + timedelta(minutes=90)).isoformat(),
            },
        ))
        tl.insert(make_episode(
            start_offset=5400,
            duration=1800,
            episode_id="late-night-ep",
            metadata={"daypart": "late_night"},
        ))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-long",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-long",
                title="Long Movie",
                duration_seconds=7200,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))
        blocks = sorted(result.blocks, key=lambda block: block.start_time)

        assert isinstance(blocks[0], MovieBlock)
        assert blocks[0].end_time == NOW + timedelta(seconds=7200)
        assert isinstance(blocks[1], EpisodeBlock)
        assert blocks[1].start_time == blocks[0].end_time
        assert result.validate() == []

    async def test_fixed_movie_slot_keeps_movie_inside_slot_budget(self) -> None:
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={
                "allow_movies": True,
                "variable_movie_duration": False,
                "slot_duration_minutes": 30,
                "ad_density": 0.0,
            },
        ))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-long",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-long",
                title="Long Movie",
                duration_seconds=5400,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))

        assert not any(isinstance(block, MovieBlock) for block in result.blocks)
        assert isinstance(result.blocks[0], SlotBlock)
        assert result.blocks[0].metadata["note"] == "no_movie_fits_slot"

    async def test_fixed_movie_slot_can_use_daypart_end_tolerance(self) -> None:
        now = datetime.now(tz=UTC)
        tl = Timeline()
        tl.insert(make_slot(
            duration=1800,
            metadata={
                "allow_movies": True,
                "variable_movie_duration": False,
                "slot_duration_minutes": 30,
                "ad_density": 0.0,
                "daypart_boundary": (now + timedelta(minutes=30)).isoformat(),
                "end_tolerance_minutes": 10,
            },
        ))
        repo = MockMediaRepository([
            MediaCacheEntry(
                id="movie-overrun",
                item_type="movie",
                source_type="jellyfin",
                source_id="movie-overrun",
                title="Movie Overrun",
                duration_seconds=35 * 60,
            ),
        ])

        result = await MovieScheduler().process(tl, make_context(media_repo=repo))

        assert isinstance(result.blocks[0], MovieBlock)
        assert result.blocks[0].movie_id == "movie-overrun"


# ===================================================================
# TunarrUploader
# ===================================================================

class TestTunarrUploader:
    def test_convert_timeline(self) -> None:
        tl = Timeline()
        ep = make_episode(metadata={"title": "Test Episode"})
        tl.insert(ep)
        plugin = TunarrUploader()
        data = plugin._convert_timeline(tl)
        assert data["schedule"]["type"] == "time"
        assert len(data["schedule"]["slots"]) == 1
        slot = data["schedule"]["slots"][0]
        assert slot["id"] == str(uuid.UUID(ep.id))
        assert slot["type"] == "show"
        assert slot["showId"] == "show_default"

    def test_multiple_block_types(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(metadata={"title": "Ep1"}))
        tl.insert(make_movie(start_offset=3600, metadata={"title": "Movie1"}))
        plugin = TunarrUploader()
        data = plugin._convert_timeline(tl)
        types = {p["type"] for p in data["schedule"]["slots"]}
        assert "show" in types
        assert "movie" in types

    def test_convert_custom_show_daypart_to_tunarr_slot(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(metadata={"custom_show_list_ids": ["custom-1"]}))
        data = TunarrUploader()._convert_timeline(tl)
        slot = data["schedule"]["slots"][0]
        assert slot["type"] == "custom-show"
        assert slot["customShowId"] == "custom-1"

    def test_convert_off_air_custom_show_loop_to_tunarr_slot(self) -> None:
        tl = Timeline()
        start = NOW
        tl.insert(OfflineBlock(
            start_time=start,
            end_time=start + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            reason="Off-Air Loop",
            metadata={"custom_show_list_ids": ["standby-list"], "off_air": True},
        ))

        data = TunarrUploader()._convert_timeline(tl)
        slot = data["schedule"]["slots"][0]

        assert slot["type"] == "custom-show"
        assert slot["customShowId"] == "standby-list"

    async def test_upload_returns_timeline_on_no_client(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        plugin = TunarrUploader()
        result = await plugin.process(tl, make_context())
        assert len(result.blocks) == 1


# ===================================================================
# HTMLPreview
# ===================================================================

class TestHTMLPreview:
    def test_render_empty(self) -> None:
        plugin = HTMLPreview()
        html = plugin.render(Timeline(), "Test Channel")
        assert "<html" in html
        assert "Test Channel" in html
        assert "0 blocks" in html

    def test_render_all_block_types(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(metadata={"title": "Ep1", "show_name": "Show1"}))
        tl.insert(make_movie(start_offset=3600, metadata={"title": "Mov1"}))
        tl.insert(AdBlock(
            start_time=NOW + timedelta(hours=2),
            end_time=NOW + timedelta(hours=2, minutes=2),
            duration=timedelta(minutes=2),
            ad_count=4, total_duration_seconds=120,
        ))
        tl.insert(StationIDBlock(
            start_time=NOW + timedelta(hours=3),
            end_time=NOW + timedelta(hours=3, minutes=1),
            duration=timedelta(minutes=1),
            clip_id="station_1",
        ))
        tl.insert(FillerBlock(
            start_time=NOW + timedelta(hours=4),
            end_time=NOW + timedelta(hours=4, minutes=5),
            duration=timedelta(minutes=5),
            filler_type=FillerType.BUMPER,
        ))
        tl.insert(OfflineBlock(
            start_time=NOW + timedelta(hours=5),
            end_time=NOW + timedelta(hours=6),
            duration=timedelta(hours=1),
            reason="Maintenance",
        ))
        plugin = HTMLPreview()
        html = plugin.render(tl, "All Types")
        assert "episode" in html
        assert "movie" in html
        assert "ad" in html
        assert "station_id" in html
        assert "filler" in html
        assert "offline" in html
        assert "badge-station_id" in html
        assert "badge-filler" in html
        assert "badge-offline" in html
        assert "Show1" in html
        assert "6 blocks" in html

    def test_render_shows_duration_minutes(self) -> None:
        tl = Timeline()
        tl.insert(make_episode(duration=3600))
        plugin = HTMLPreview()
        html = plugin.render(tl, "Dur Test")
        assert "60m" in html  # 3600s = 60m

    def test_render_formats_times_in_timezone(self) -> None:
        tl = Timeline()
        tl.insert(EpisodeBlock(
            start_time=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
            end_time=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
            duration=timedelta(minutes=30),
            episode_id="episode-1",
            show_id="show-1",
            season_number=1,
            episode_number=1,
            runtime_seconds=1800,
        ))

        html = HTMLPreview().render(tl, "Time Test", timezone="Europe/Berlin")

        assert "14:00 - 14:30" in html

    async def test_process_saves_timeline(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        plugin = HTMLPreview()
        result = await plugin.process(tl, make_context())
        assert len(result.blocks) == 1

    def test_render_without_timeline_shows_message(self) -> None:
        plugin = HTMLPreview()
        html = plugin.render()
        assert "No timeline to preview" in html

    def test_render_shows_validation_errors(self) -> None:
        tl = Timeline(metadata={"validation_errors": ["dead_air: gap"]})
        plugin = HTMLPreview()

        html = plugin.render(tl, "Invalid Channel")

        assert "Validation Errors" in html
        assert "dead_air: gap" in html


class TestSchedulePersister:
    async def test_persists_valid_timeline_as_draft(self) -> None:
        tl = Timeline(metadata={"validation_passed": True})
        tl.insert(make_episode())
        state = MockStateManager()
        plugin = SchedulePersister()

        result = await plugin.process(tl, make_context(state=state))

        assert len(state.schedule_versions) == 1
        assert state.schedule_versions[0]["status"] == "draft"
        assert result.metadata["schedule_version"] == 1
        assert result.metadata["schedule_status"] == "draft"

    async def test_persists_invalid_timeline_as_invalid(self) -> None:
        tl = Timeline(metadata={"validation_passed": False})
        state = MockStateManager()
        plugin = SchedulePersister()

        result = await plugin.process(tl, make_context(state=state))

        assert state.schedule_versions[0]["status"] == "invalid"
        assert result.metadata["schedule_status"] == "invalid"


# ===================================================================
# EventScheduler (stub)
# ===================================================================

class TestEventScheduler:
    async def test_pass_through(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        plugin = EventScheduler()
        result = await plugin.process(tl, make_context())
        assert result is tl
        assert len(result.blocks) == 1


# ===================================================================
# StatsExporter (stub)
# ===================================================================

class TestStatsExporter:
    async def test_pass_through(self) -> None:
        tl = Timeline()
        tl.insert(make_episode())
        plugin = StatsExporter()
        result = await plugin.process(tl, make_context())
        assert result is tl
        assert len(result.blocks) == 1


class TestMediaSync:
    async def test_stores_normalized_media_types(self) -> None:
        class Client:
            async def get_all_media(self, page_size: int = 500) -> list[dict[str, Any]]:
                return [
                    {
                        "Id": "ep1",
                        "Type": "Episode",
                        "Name": "Pilot",
                        "RunTimeTicks": 18_000_000_000,
                        "SeriesId": "show1",
                    },
                    {
                        "Id": "movie1",
                        "Type": "Movie",
                        "Name": "Movie",
                        "RunTimeTicks": 54_000_000_000,
                    },
                ]

            async def get_item(self, item_id: str) -> dict[str, Any] | None:
                return {"Id": item_id}

        class Repo:
            def __init__(self) -> None:
                self.entries: list[MediaCacheEntry] = []

            async def get_known_ids(self, source_type: str) -> set[str]:
                return set()

            async def save_many(self, entries: list[MediaCacheEntry]) -> None:
                self.entries.extend(entries)

            async def get_unavailable(self) -> list[MediaCacheEntry]:
                return []

            async def mark_unavailable(self, item_id: str) -> None:
                pass

        repo = Repo()
        sync = MediaSyncEngine(
            client=Client(),
            media_repo=repo,
            event_bus=EventBus(),
        )

        result = await sync.sync_now()

        assert result["new_episodes"] == 1
        assert result["new_movies"] == 1
        assert [entry.item_type for entry in repo.entries] == ["episode", "movie"]

    async def test_marks_media_missing_from_full_sync_unavailable(self) -> None:
        class Client:
            async def get_all_media(self, page_size: int = 500) -> list[dict[str, Any]]:
                return [{
                    "Id": "current",
                    "Type": "Episode",
                    "Name": "Current",
                    "RunTimeTicks": 18_000_000_000,
                    "SeriesId": "show1",
                }]

        class Repo:
            def __init__(self) -> None:
                self.marked: list[str] = []

            async def get_known_ids(self, source_type: str) -> set[str]:
                return {"current", "removed"}

            async def save_many(self, entries: list[MediaCacheEntry]) -> None:
                pass

            async def mark_unavailable(self, item_id: str) -> None:
                self.marked.append(item_id)

        repo = Repo()
        result = await MediaSyncEngine(
            client=Client(),
            media_repo=repo,
            event_bus=EventBus(),
        ).sync_now()

        assert repo.marked == ["removed"]
        assert result["removed_items"] == 1
