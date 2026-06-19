from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta

from tunarr_autoscheduler.core.state import StateManager
from tunarr_autoscheduler.core.timeline import Timeline
from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.models.blocks import EpisodeBlock, MovieBlock


def _episode_timeline(
    start: datetime,
    episode_id: str,
    duration: timedelta = timedelta(minutes=30),
) -> Timeline:
    timeline = Timeline()
    timeline.insert(EpisodeBlock(
        start_time=start,
        end_time=start + duration,
        duration=duration,
        episode_id=episode_id,
        show_id="show-1",
        season_number=1,
        episode_number=1,
        runtime_seconds=int(duration.total_seconds()),
    ))
    return timeline


def _movie_timeline(
    start: datetime,
    movie_id: str,
    duration: timedelta = timedelta(minutes=90),
) -> Timeline:
    timeline = Timeline()
    timeline.insert(MovieBlock(
        start_time=start,
        end_time=start + duration,
        duration=duration,
        movie_id=movie_id,
        runtime_seconds=int(duration.total_seconds()),
    ))
    return timeline


async def test_follow_up_context_uses_latest_valid_end_and_reserves_episodes() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            first = _episode_timeline(start, "episode-1")
            second = _episode_timeline(start + timedelta(minutes=30), "episode-2")
            invalid = _episode_timeline(start + timedelta(days=7), "ignored-episode")

            await state.save_schedule_version(
                "channel-1", 1, json.dumps(first.snapshot(), default=str),
                status="uploaded",
            )
            await state.save_schedule_version(
                "channel-1", 2, json.dumps(second.snapshot(), default=str),
                status="draft",
                parent_version=1,
            )
            await state.save_schedule_version(
                "channel-1", 3, json.dumps(invalid.snapshot(), default=str),
                status="invalid",
            )

            context = await state.get_follow_up_context("channel-1")
        finally:
            await db.disconnect()

    assert context is not None
    assert context["version"] == 2
    assert context["end_time"] == start + timedelta(hours=1)
    assert context["planned_start"] == start
    assert context["planned_end"] == start + timedelta(hours=1)
    assert context["chain_versions"] == [1, 2]
    assert context["episode_ids"] == {"episode-1", "episode-2"}


async def test_follow_up_context_uses_latest_planned_end_not_highest_version() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            later = _episode_timeline(start + timedelta(days=1), "episode-later")
            earlier_high_version = _episode_timeline(start, "episode-earlier")
            movie = _movie_timeline(start + timedelta(days=2), "movie-1")

            await state.save_schedule_version(
                "channel-1", 10, json.dumps(earlier_high_version.snapshot(), default=str),
                status="draft",
            )
            await state.save_schedule_version(
                "channel-1", 4, json.dumps(later.snapshot(), default=str),
                status="uploaded",
            )
            await state.save_schedule_version(
                "channel-1", 5, json.dumps(movie.snapshot(), default=str),
                status="approved",
            )

            context = await state.get_follow_up_context("channel-1")
        finally:
            await db.disconnect()

    assert context is not None
    assert context["version"] == 5
    assert context["status"] == "approved"
    assert context["movie_ids"] == {"movie-1"}
    assert context["episode_ids"] == {"episode-later", "episode-earlier"}
    assert context["chain_versions"] == [10, 4, 5]


async def test_follow_up_context_skips_corrupt_and_empty_versions() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            valid = _episode_timeline(start, "episode-1")
            empty = Timeline()

            await state.save_schedule_version(
                "channel-1", 1, "{not json",
                status="uploaded",
            )
            await state.save_schedule_version(
                "channel-1", 2, json.dumps(empty.snapshot(), default=str),
                status="approved",
            )
            await state.save_schedule_version(
                "channel-1", 3, json.dumps(valid.snapshot(), default=str),
                status="draft",
            )

            context = await state.get_follow_up_context("channel-1")
        finally:
            await db.disconnect()

    assert context is not None
    assert context["version"] == 3
    assert context["chain_versions"] == [3]
    assert context["episode_ids"] == {"episode-1"}


async def test_follow_up_context_can_use_explicit_parent_version() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            first = _episode_timeline(start, "episode-1")
            second = _episode_timeline(start + timedelta(hours=1), "episode-2")
            later = _episode_timeline(start + timedelta(hours=2), "episode-3")

            await state.save_schedule_version(
                "channel-1", 1, json.dumps(first.snapshot(), default=str),
                status="uploaded",
            )
            await state.save_schedule_version(
                "channel-1", 2, json.dumps(second.snapshot(), default=str),
                status="approved",
                parent_version=1,
            )
            await state.save_schedule_version(
                "channel-1", 3, json.dumps(later.snapshot(), default=str),
                status="draft",
                parent_version=2,
            )

            context = await state.get_follow_up_context("channel-1", parent_version=2)
        finally:
            await db.disconnect()

    assert context is not None
    assert context["version"] == 2
    assert context["planned_end"] == start + timedelta(hours=1, minutes=30)
    assert context["chain_versions"] == [1, 2]
    assert context["episode_ids"] == {"episode-1", "episode-2"}


async def test_follow_up_context_reports_gaps_between_chain_versions() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            first = _episode_timeline(start, "episode-1", timedelta(hours=1))
            second = _episode_timeline(start + timedelta(hours=3), "episode-2")

            await state.save_schedule_version(
                "channel-1", 1, json.dumps(first.snapshot(), default=str),
                status="uploaded",
            )
            await state.save_schedule_version(
                "channel-1", 2, json.dumps(second.snapshot(), default=str),
                status="draft",
                parent_version=1,
            )

            context = await state.get_follow_up_context("channel-1")
        finally:
            await db.disconnect()

    assert context is not None
    assert context["chain_versions"] == [1, 2]
    assert context["gaps"] == [{
        "start": start + timedelta(hours=1),
        "end": start + timedelta(hours=3),
        "minutes": 120,
    }]


async def test_follow_up_context_reserves_mixed_content_across_long_chain() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 1, 6, tzinfo=UTC)
            for version in range(1, 5):
                version_start = start + timedelta(days=version * 3)
                timeline = (
                    _movie_timeline(version_start, f"movie-{version}")
                    if version % 2 == 0
                    else _episode_timeline(version_start, f"episode-{version}")
                )
                await state.save_schedule_version(
                    "channel-1",
                    version,
                    json.dumps(timeline.snapshot(), default=str),
                    status="uploaded" if version == 1 else "draft",
                    parent_version=version - 1 if version > 1 else None,
                )

            context = await state.get_follow_up_context("channel-1")
        finally:
            await db.disconnect()

    assert context is not None
    assert context["chain_versions"] == [1, 2, 3, 4]
    assert context["episode_ids"] == {"episode-1", "episode-3"}
    assert context["movie_ids"] == {"movie-2", "movie-4"}


async def test_list_versions_includes_planned_period() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            start = datetime(2026, 6, 11, 6, tzinfo=UTC)
            timeline = _episode_timeline(start, "episode-1", timedelta(hours=1))
            await state.save_schedule_version(
                "channel-1", 1, json.dumps(timeline.snapshot(), default=str),
            )

            versions = await state.list_versions("channel-1")
        finally:
            await db.disconnect()

    assert versions[0]["planned_start"] == start.isoformat()
    assert versions[0]["planned_end"] == (start + timedelta(hours=1)).isoformat()


async def test_upload_attempts_are_persisted_and_filterable() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            state = StateManager(db)
            await state.record_upload_attempt(
                "channel-1",
                7,
                "success",
                "Uploaded.",
                {"mode": "manual", "final_status": 200},
            )
            await state.record_upload_attempt(
                "channel-2",
                3,
                "failed",
                "Rejected.",
                {"status_code": 400},
            )

            all_attempts = await state.list_upload_attempts()
            filtered = await state.list_upload_attempts("channel-1")
        finally:
            await db.disconnect()

    assert len(all_attempts) == 2
    assert len(filtered) == 1
    assert filtered[0]["schedule_version"] == 7
    assert filtered[0]["status"] == "success"
    assert filtered[0]["details"]["final_status"] == 200
