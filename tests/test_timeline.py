from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.models.blocks import (
    AdBlock,
    EpisodeBlock,
    FillerBlock,
    FillerType,
    MovieBlock,
    OfflineBlock,
    SlotBlock,
)


class TestTimeline:
    def test_empty_timeline(self):
        tl = Timeline()
        assert len(tl.blocks) == 0
        assert tl.total_duration() == timedelta(0)

    def test_insert_block(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        block = EpisodeBlock(
            start_time=now,
            end_time=now + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            episode_id="ep1",
            show_id="show1",
            season_number=1,
            episode_number=1,
            runtime_seconds=1800,
        )
        tl.insert(block)
        assert len(tl.blocks) == 1
        assert tl.blocks[0].episode_id == "ep1"

    def test_insert_maintains_order(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=1, minutes=30),
            duration=timedelta(minutes=30),
            episode_id="ep2",
            show_id="show1",
            season_number=1,
            episode_number=2,
            runtime_seconds=1800,
        )
        b2 = EpisodeBlock(
            start_time=now,
            end_time=now + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            episode_id="ep1",
            show_id="show1",
            season_number=1,
            episode_number=1,
            runtime_seconds=1800,
        )
        tl.insert(b1)
        tl.insert(b2)
        assert tl.blocks[0].episode_id == "ep1"
        assert tl.blocks[1].episode_id == "ep2"

    def test_remove_block(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        block = EpisodeBlock(
            start_time=now,
            end_time=now + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            episode_id="ep1",
            show_id="show1",
            season_number=1,
            episode_number=1,
            runtime_seconds=1800,
        )
        tl.insert(block)
        tl.remove(block.id)
        assert len(tl.blocks) == 0

    def test_query(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=3600,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(hours=2), end_time=now + timedelta(hours=3),
            duration=timedelta(hours=1),
            episode_id="ep2", show_id="show1",
            season_number=1, episode_number=2, runtime_seconds=3600,
        )
        tl.insert(b1)
        tl.insert(b2)
        results = tl.query(now + timedelta(minutes=30), now + timedelta(hours=2, minutes=30))
        assert len(results) == 2

    def test_find_gaps(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=3600,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(hours=1, minutes=30),
            end_time=now + timedelta(hours=2, minutes=30),
            duration=timedelta(hours=1),
            episode_id="ep2", show_id="show1",
            season_number=1, episode_number=2, runtime_seconds=3600,
        )
        tl.insert(b1)
        tl.insert(b2)
        gaps = tl.find_gaps()
        assert len(gaps) == 1
        gap_start, gap_end = gaps[0]
        gap_duration = (gap_end - gap_start).total_seconds()
        assert abs(gap_duration - 1800) < 2

    def test_validate_no_errors(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=3600,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(hours=1), end_time=now + timedelta(hours=2),
            duration=timedelta(hours=1),
            episode_id="ep2", show_id="show1",
            season_number=1, episode_number=2, runtime_seconds=3600,
        )
        tl.insert(b1)
        tl.insert(b2)
        errors = tl.validate()
        assert len(errors) == 0

    def test_validate_overlap(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1, minutes=30),
            duration=timedelta(hours=1, minutes=30),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=5400,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(hours=1), end_time=now + timedelta(hours=2),
            duration=timedelta(hours=1),
            episode_id="ep2", show_id="show1",
            season_number=1, episode_number=2, runtime_seconds=3600,
        )
        tl.insert(b1)
        tl.insert(b2)
        errors = tl.validate()
        assert len(errors) >= 1
        assert "overlap" in errors[0].lower()

    def test_validate_subsecond_overlap(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now,
            end_time=now + timedelta(seconds=60),
            duration=timedelta(seconds=60),
            episode_id="ep1",
            show_id="show1",
            season_number=1,
            episode_number=1,
            runtime_seconds=60,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(seconds=59, milliseconds=500),
            end_time=now + timedelta(seconds=120),
            duration=timedelta(seconds=60, milliseconds=500),
            episode_id="ep2",
            show_id="show1",
            season_number=1,
            episode_number=2,
            runtime_seconds=60,
        )
        tl.insert(b1)
        tl.insert(b2)

        errors = tl.validate()

        assert len(errors) >= 1
        assert "overlap" in errors[0].lower()

    def test_snapshot_roundtrip(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        block = EpisodeBlock(
            start_time=now, end_time=now + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=1800,
            metadata={"title": "Pilot", "show_name": "Test Show"},
        )
        tl.insert(block)

        snapshot = tl.snapshot()
        restored = Timeline.from_snapshot(snapshot)

        assert len(restored.blocks) == 1
        restored_block = restored.blocks[0]
        assert isinstance(restored_block, EpisodeBlock)
        assert restored_block.episode_id == "ep1"
        assert restored_block.show_id == "show1"
        assert restored_block.season_number == 1
        assert restored_block.episode_number == 1
        assert restored_block.metadata["title"] == "Pilot"

    def test_movie_block(self):
        now = datetime.now(tz=UTC)
        block = MovieBlock(
            start_time=now, end_time=now + timedelta(hours=2),
            duration=timedelta(hours=2),
            movie_id="movie1", runtime_seconds=7200, year=2020,
            metadata={"title": "Test Movie"},
        )
        assert block.block_type.value == "movie"
        assert block.runtime_seconds == 7200
        assert block.year == 2020

    def test_ad_block(self):
        now = datetime.now(tz=UTC)
        block = AdBlock(
            start_time=now, end_time=now + timedelta(minutes=3),
            duration=timedelta(minutes=3),
            ad_count=6, total_duration_seconds=180,
        )
        assert block.block_type.value == "ad"
        assert block.ad_count == 6

    def test_total_duration(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        b1 = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=3600,
        )
        b2 = EpisodeBlock(
            start_time=now + timedelta(hours=1), end_time=now + timedelta(hours=2),
            duration=timedelta(hours=1),
            episode_id="ep2", show_id="show1",
            season_number=1, episode_number=2, runtime_seconds=3600,
        )
        tl.insert(b1)
        tl.insert(b2)
        assert tl.total_duration() == timedelta(hours=2)

    def test_time_shift(self):
        tl = Timeline()
        now = datetime.now(tz=UTC)
        block = EpisodeBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            episode_id="ep1", show_id="show1",
            season_number=1, episode_number=1, runtime_seconds=3600,
        )
        tl.insert(block)
        tl.time_shift(timedelta(hours=2))
        assert tl.blocks[0].start_time == now + timedelta(hours=2)
        assert tl.blocks[0].end_time == now + timedelta(hours=3)


class TestOfflineBlock:
    def test_offline_block(self):
        now = datetime.now(tz=UTC)
        block = OfflineBlock(
            start_time=now, end_time=now + timedelta(hours=1),
            duration=timedelta(hours=1),
            reason="No media available",
        )
        assert block.block_type.value == "offline"
        assert block.reason == "No media available"


class TestFillerBlock:
    def test_filler_block(self):
        now = datetime.now(tz=UTC)
        block = FillerBlock(
            start_time=now, end_time=now + timedelta(minutes=5),
            duration=timedelta(minutes=5),
        )
        assert block.block_type.value == "filler"

    def test_filler_block_snapshot_roundtrip_preserves_enum(self):
        now = datetime.now(tz=UTC)
        tl = Timeline()
        tl.insert(FillerBlock(
            start_time=now,
            end_time=now + timedelta(minutes=1),
            duration=timedelta(minutes=1),
            filler_type=FillerType.TRAILER,
        ))

        restored = Timeline.from_snapshot(tl.snapshot())

        assert isinstance(restored.blocks[0], FillerBlock)
        assert restored.blocks[0].filler_type == FillerType.TRAILER


class TestSlotBlock:
    def test_slot_block_snapshot_roundtrip(self):
        now = datetime.now(tz=UTC)
        tl = Timeline()
        tl.insert(SlotBlock(
            start_time=now,
            end_time=now + timedelta(minutes=30),
            duration=timedelta(minutes=30),
            metadata={"daypart": "morning", "allow_movies": False},
        ))

        restored = Timeline.from_snapshot(tl.snapshot())

        assert isinstance(restored.blocks[0], SlotBlock)
        assert restored.blocks[0].block_type.value == "slot"
        assert restored.blocks[0].metadata["daypart"] == "morning"
