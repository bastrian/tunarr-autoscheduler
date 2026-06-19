from __future__ import annotations

from typing import Any, cast

from tunarr_autoscheduler.core.event_bus import Event, EventBus
from tunarr_autoscheduler.integrations.jellyfin.sync import MediaSyncEngine
from tunarr_autoscheduler.models.schedule import MediaCacheEntry


class FakeJellyfinClient:
    def __init__(self, items: dict[str, dict[str, Any] | None]) -> None:
        self.items = items
        self.calls: list[tuple[str, str]] = []

    async def get_item(self, item_id: str) -> dict[str, Any] | None:
        self.calls.append(("get_item", item_id))
        return self.items.get(item_id)

    async def get_all_media(self) -> list[dict[str, Any]]:
        return [item for item in self.items.values() if item is not None]


class FakeMediaRepository:
    def __init__(self, entries: dict[str, MediaCacheEntry] | None = None) -> None:
        self.entries = entries or {}
        self.saved: list[MediaCacheEntry] = []
        self.unavailable: list[str] = []

    async def get(self, item_id: str) -> MediaCacheEntry | None:
        return self.entries.get(item_id)

    async def save(self, entry: MediaCacheEntry) -> None:
        self.entries[entry.id] = entry
        self.saved.append(entry)

    async def save_many(self, entries: list[MediaCacheEntry]) -> None:
        for entry in entries:
            await self.save(entry)

    async def mark_unavailable(self, item_id: str) -> None:
        self.unavailable.append(item_id)
        existing = self.entries.get(item_id)
        if existing is not None:
            existing.available = False

    async def get_known_ids(self, source_type: str) -> set[str]:
        return set(self.entries)


def _movie(item_id: str = "movie-1") -> dict[str, Any]:
    return {
        "Id": item_id,
        "Type": "Movie",
        "Name": "Movie One",
        "RunTimeTicks": 5_400 * 10_000_000,
        "ProductionYear": 2026,
    }


def _anime_episode(item_id: str = "episode-1") -> dict[str, Any]:
    item = _episode(item_id)
    item.update({
        "Genres": ["Anime", "Sci-Fi"],
        "Tags": ["anime"],
        "Studios": [{"Name": "Sunrise"}],
        "OfficialRating": "TV-14",
        "CommunityRating": 8.5,
        "ProviderIds": {"Tvdb": "12345", "Imdb": "tt12345"},
        "MediaStreams": [
            {"Type": "Audio", "Language": "jpn"},
            {"Type": "Subtitle", "Language": "English"},
        ],
    })
    return item


def _episode(item_id: str = "episode-1") -> dict[str, Any]:
    return {
        "Id": item_id,
        "Type": "Episode",
        "Name": "Pilot",
        "RunTimeTicks": 1_800 * 10_000_000,
        "SeriesId": "series-1",
        "SeriesName": "Series One",
        "IndexNumber": 1,
        "ParentIndexNumber": 1,
    }


async def test_targeted_media_sync_creates_movie_from_read_only_get_item() -> None:
    client = FakeJellyfinClient({"movie-1": _movie()})
    repo = FakeMediaRepository()
    engine = MediaSyncEngine(cast(Any, client), cast(Any, repo), EventBus())

    result = await engine.sync_item("movie-1", event_name="ItemAdded")

    assert result["status"] == "created"
    assert result["new_movies"] == 1
    assert client.calls == [("get_item", "movie-1")]
    assert repo.saved[0].item_type == "movie"
    assert repo.saved[0].duration_seconds == 5400
    assert repo.saved[0].metadata["year"] == 2026


async def test_targeted_media_sync_marks_deleted_item_unavailable_without_jellyfin_call() -> None:
    client = FakeJellyfinClient({"movie-1": _movie()})
    repo = FakeMediaRepository()
    engine = MediaSyncEngine(cast(Any, client), cast(Any, repo), EventBus())

    result = await engine.sync_item("movie-1", event_name="ItemDeleted")

    assert result["status"] == "removed"
    assert result["removed_items"] == 1
    assert client.calls == []
    assert repo.unavailable == ["movie-1"]


async def test_targeted_media_sync_ignores_unsupported_media_types() -> None:
    client = FakeJellyfinClient({
        "series-1": {"Id": "series-1", "Type": "Series", "Name": "Series One"},
    })
    repo = FakeMediaRepository()
    engine = MediaSyncEngine(cast(Any, client), cast(Any, repo), EventBus())

    result = await engine.sync_item("series-1", event_name="ItemUpdated")

    assert result["status"] == "ignored"
    assert result["ignored_items"] == 1
    assert repo.saved == []
    assert repo.unavailable == []


async def test_targeted_media_sync_reactivates_unavailable_episode_show() -> None:
    events: list[dict[str, Any]] = []
    bus = EventBus()
    bus.subscribe(Event.SHOW_REACTIVATED, lambda **kwargs: events.append(kwargs))
    existing = MediaCacheEntry(
        id="episode-1",
        item_type="episode",
        source_type="jellyfin",
        source_id="episode-1",
        title="Pilot",
        duration_seconds=1800,
        metadata={"series_id": "series-1"},
        available=False,
    )
    client = FakeJellyfinClient({"episode-1": _episode()})
    repo = FakeMediaRepository({"episode-1": existing})
    engine = MediaSyncEngine(cast(Any, client), cast(Any, repo), bus)

    result = await engine.sync_item("episode-1", event_name="ItemUpdated")

    assert result["status"] == "updated"
    assert result["updated_items"] == 1
    assert events[0]["event"] == Event.SHOW_REACTIVATED
    assert events[0]["show_id"] == "series-1"


async def test_targeted_media_sync_stores_recommendation_metadata() -> None:
    client = FakeJellyfinClient({"episode-1": _anime_episode()})
    repo = FakeMediaRepository()
    engine = MediaSyncEngine(cast(Any, client), cast(Any, repo), EventBus())

    result = await engine.sync_item("episode-1", event_name="ItemAdded")

    assert result["status"] == "created"
    metadata = repo.saved[0].metadata
    assert metadata["genres"] == ["Anime", "Sci-Fi"]
    assert metadata["tags"] == ["anime"]
    assert metadata["studios"] == ["Sunrise"]
    assert metadata["parental_rating"] == "TV-14"
    assert metadata["community_rating"] == 8.5
    assert metadata["provider_ids"] == {"Tvdb": "12345", "Imdb": "tt12345"}
    assert metadata["audio_languages"] == ["ja"]
    assert metadata["subtitle_languages"] == ["en"]
