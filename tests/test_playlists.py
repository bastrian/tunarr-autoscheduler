from __future__ import annotations

import tempfile

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.db.repositories.media_repo import MediaRepository
from tunarr_autoscheduler.db.repositories.playlist_repo import PlaylistRepository
from tunarr_autoscheduler.db.schema import run_migrations
from tunarr_autoscheduler.models.playlist import PlaylistItem
from tunarr_autoscheduler.models.schedule import MediaCacheEntry


async def test_playlist_repository_preserves_mixed_item_order() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            repo = PlaylistRepository(db)
            playlist = await repo.create(
                "Prime Time",
                "Series and movies",
                [
                    PlaylistItem(
                        media_type="movie", media_id="movie-1",
                        title="Movie", position=1,
                    ),
                    PlaylistItem(
                        media_type="series", media_id="series-1",
                        title="Series", position=0,
                    ),
                ],
            )
            loaded = await repo.get(playlist.id)
        finally:
            await db.disconnect()

    assert loaded is not None
    assert [(item.media_type, item.media_id) for item in loaded.items] == [
        ("series", "series-1"),
        ("movie", "movie-1"),
    ]


async def test_playlist_repository_updates_and_deletes() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            repo = PlaylistRepository(db)
            category = await repo.create_category("FlixWolf One", "Main channel")
            playlist = await repo.create("Old")
            updated = await repo.update(
                playlist.id,
                "New",
                "",
                [PlaylistItem(
                    media_type="movie", media_id="movie-2",
                    title="Second Movie", position=0,
                )],
                category_id=category.id,
                channel_scope="ch1",
                tags=["Prime", "prime", " crime "],
            )
            deleted = await repo.delete(playlist.id)
            missing = await repo.get(playlist.id)
        finally:
            await db.disconnect()

    assert updated is not None
    assert updated.name == "New"
    assert updated.category_id == category.id
    assert updated.category_name == "FlixWolf One"
    assert updated.channel_scope == "ch1"
    assert updated.tags == ["crime", "prime"]
    assert updated.items[0].media_id == "movie-2"
    assert deleted is True
    assert missing is None


async def test_playlist_repository_manages_categories_and_clears_on_delete() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            repo = PlaylistRepository(db)
            category = await repo.create_category("One", "Original")
            playlist = await repo.create(
                "Scoped",
                category_id=category.id,
                channel_scope="ch1",
                tags=["docu", "late night"],
            )
            updated_category = await repo.update_category(category.id, "Two", "Renamed")
            loaded = await repo.get(playlist.id)
            tags = await repo.list_tags()
            deleted = await repo.delete_category(category.id)
            uncategorized = await repo.get(playlist.id)
        finally:
            await db.disconnect()

    assert updated_category is not None
    assert updated_category.name == "Two"
    assert loaded is not None
    assert loaded.category_name == "Two"
    assert loaded.tags == ["docu", "late night"]
    assert tags == ["docu", "late night"]
    assert deleted is True
    assert uncategorized is not None
    assert uncategorized.category_id == ""
    assert uncategorized.category_name == ""


async def test_playlist_repository_builds_recommendation_terms_by_media_id() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            repo = PlaylistRepository(db)
            category = await repo.create_category("Anime Channels", "Japanese animation")
            await repo.create(
                "Late Night Anime",
                "Curated anime block",
                [
                    PlaylistItem(
                        media_type="series",
                        media_id="series-1",
                        title="Series",
                        position=0,
                    ),
                    PlaylistItem(
                        media_type="movie",
                        media_id="movie-1",
                        title="Movie",
                        position=1,
                    ),
                ],
                category_id=category.id,
                tags=["Subbed Anime", "Late-Night"],
            )
            terms = await repo.get_recommendation_terms_by_media_id()
        finally:
            await db.disconnect()

    assert "anime" in terms["series-1"]
    assert "anime" in terms["movie-1"]
    assert "late" in terms["series-1"]
    assert "night" in terms["series-1"]
    assert "subbed" in terms["movie-1"]


async def test_media_repository_groups_playlist_series_and_movies() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(tmp.name)
        await db.connect()
        try:
            await run_migrations(db)
            repo = MediaRepository(db)
            await repo.save_many([
                MediaCacheEntry(
                    id="episode-1",
                    item_type="episode",
                    source_type="jellyfin",
                    source_id="episode-1",
                    title="Pilot",
                    duration_seconds=1800,
                    metadata={"series_id": "series-1", "series_name": "Series One"},
                ),
                MediaCacheEntry(
                    id="episode-2",
                    item_type="episode",
                    source_type="jellyfin",
                    source_id="episode-2",
                    title="Second",
                    duration_seconds=1800,
                    metadata={"series_id": "series-1", "series_name": "Series One"},
                ),
                MediaCacheEntry(
                    id="movie-1",
                    item_type="movie",
                    source_type="jellyfin",
                    source_id="movie-1",
                    title="Movie One",
                    duration_seconds=5400,
                    metadata={"year": 2025},
                ),
            ])
            options = await repo.get_playlist_options()
        finally:
            await db.disconnect()

    assert options == [
        {
            "key": "series:series-1",
            "media_type": "series",
            "media_id": "series-1",
            "title": "Series One",
            "details": "2 episodes",
        },
        {
            "key": "movie:movie-1",
            "media_type": "movie",
            "media_id": "movie-1",
            "title": "Movie One",
            "details": "2025",
        },
    ]
