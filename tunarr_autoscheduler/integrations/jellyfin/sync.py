from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.core.event_bus import Event, EventBus
from tunarr_autoscheduler.db.repositories.media_repo import MediaRepository, normalize_media_type
from tunarr_autoscheduler.integrations.jellyfin.client import JellyfinClient
from tunarr_autoscheduler.integrations.notifications import (
    NotificationMessage,
    NotificationRouter,
    send_notification,
)
from tunarr_autoscheduler.models.schedule import MediaCacheEntry
from tunarr_autoscheduler.recommendations.language import extract_language_metadata

logger = logging.getLogger(__name__)


class MediaSyncEngine:
    def __init__(
        self,
        client: JellyfinClient,
        media_repo: MediaRepository,
        event_bus: EventBus,
        interval_minutes: int = 15,
        metrics: Any = None,
        notification_router: NotificationRouter | None = None,
    ):
        self._client = client
        self._media_repo = media_repo
        self._event_bus = event_bus
        self._interval = interval_minutes
        self._metrics = metrics
        self._notification_router = notification_router
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def sync_now(self) -> dict[str, Any]:
        return await self._run_sync()

    async def sync_item(self, item_id: str, event_name: str | None = None) -> dict[str, Any]:
        logger.info("Starting targeted media sync item_id=%s event=%s", item_id, event_name)
        start = datetime.now(tz=UTC)
        normalized_event = (event_name or "").strip().lower()
        removed_events = {
            "itemdeleted",
            "itemdeletednotification",
            "deleted",
            "mediaitemdeleted",
        }

        removed_items = 0
        new_episodes = 0
        new_movies = 0
        updated_items = 0
        ignored_items = 0
        reactivated_show_id: str | None = None

        try:
            if normalized_event in removed_events:
                await self._media_repo.mark_unavailable(item_id)
                removed_items = 1
                status = "removed"
            else:
                item = await self._client.get_item(item_id)
                if item is None:
                    await self._media_repo.mark_unavailable(item_id)
                    removed_items = 1
                    status = "removed"
                else:
                    entry = self._entry_from_item(item)
                    if entry is None:
                        ignored_items = 1
                        status = "ignored"
                    else:
                        existing = await self._media_repo.get(entry.id)
                        await self._media_repo.save(entry)
                        is_new = existing is None
                        was_unavailable = existing is not None and not existing.available
                        if entry.item_type == "episode":
                            new_episodes = 1 if is_new else 0
                            if was_unavailable:
                                series_id = (
                                    entry.metadata.get("series_id")
                                    if entry.metadata else None
                                )
                                reactivated_show_id = str(series_id) if series_id else None
                        elif entry.item_type == "movie":
                            new_movies = 1 if is_new else 0
                        updated_items = 0 if is_new else 1
                        status = "created" if is_new else "updated"
        except Exception as e:
            logger.error("Targeted media sync error item_id=%s: %s", item_id, e)
            raise

        duration_ms = (datetime.now(tz=UTC) - start).total_seconds() * 1000
        if reactivated_show_id:
            await self._event_bus.emit(Event.SHOW_REACTIVATED, show_id=reactivated_show_id)
        if self._metrics:
            self._metrics.record_media_sync(duration_ms, new_episodes + new_movies, removed_items)
        logger.info(
            "Targeted media sync complete item_id=%s status=%s new_episodes=%d "
            "new_movies=%d updated=%d removed=%d ignored=%d duration_ms=%d",
            item_id,
            status,
            new_episodes,
            new_movies,
            updated_items,
            removed_items,
            ignored_items,
            duration_ms,
        )
        return {
            "status": status,
            "item_id": item_id,
            "new_episodes": new_episodes,
            "new_movies": new_movies,
            "updated_items": updated_items,
            "removed_items": removed_items,
            "ignored_items": ignored_items,
            "duration_ms": duration_ms,
        }

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                await self._run_sync()
            except Exception as e:
                logger.error("Media sync failed: %s", e)
                await send_notification(
                    self._notification_router,
                    NotificationMessage(
                        event_type="jellyfin_sync_failed",
                        title="Jellyfin media sync failed",
                        message=str(e),
                        severity="danger",
                        details={"source": "media_sync_loop"},
                    ),
                )
            await asyncio.sleep(self._interval * 60)

    async def _run_sync(self) -> dict[str, Any]:
        logger.info("Starting media sync")
        start = datetime.now(tz=UTC)

        new_episodes = 0
        new_movies = 0
        removed_items = 0
        reactivated_shows: set[str] = set()

        try:
            media_items = await self._client.get_all_media()
            known_ids = await self._media_repo.get_known_ids("jellyfin")
            current_ids: set[str] = set()
            entries: list[MediaCacheEntry] = []
            for item in media_items:
                jellyfin_type = item.get("Type", "unknown")
                item_type = normalize_media_type(jellyfin_type)
                item_id = item.get("Id")
                if not item_id:
                    continue
                current_ids.add(str(item_id))

                entry = self._entry_from_item(item)
                if entry is None:
                    continue
                entries.append(entry)

                if item_type == "episode" and item_id not in known_ids:
                    new_episodes += 1
                    series_id = item.get("SeriesId")
                    if series_id:
                        reactivated_shows.add(series_id)
                elif item_type == "movie" and item_id not in known_ids:
                    new_movies += 1

            await self._media_repo.save_many(entries)

            for item_id in known_ids - current_ids:
                await self._media_repo.mark_unavailable(item_id)
                removed_items += 1

        except Exception as e:
            logger.error("Media sync error: %s", e)
            await send_notification(
                self._notification_router,
                NotificationMessage(
                    event_type="jellyfin_sync_failed",
                    title="Jellyfin media sync failed",
                    message=str(e),
                    severity="danger",
                    details={"source": "media_sync"},
                ),
            )
            raise

        duration_ms = (datetime.now(tz=UTC) - start).total_seconds() * 1000
        logger.info(
            "Media sync complete: %d new episodes, %d new movies, %d removed, %dms",
            new_episodes, new_movies, removed_items, duration_ms,
        )

        for show_id in reactivated_shows:
            await self._event_bus.emit(Event.SHOW_REACTIVATED, show_id=show_id)

        if self._metrics:
            self._metrics.record_media_sync(duration_ms, new_episodes + new_movies, removed_items)

        return {
            "new_episodes": new_episodes,
            "new_movies": new_movies,
            "removed_items": removed_items,
            "duration_ms": duration_ms,
        }

    def _entry_from_item(self, item: dict[str, Any]) -> MediaCacheEntry | None:
        item_type = normalize_media_type(str(item.get("Type", "unknown")))
        if item_type not in {"episode", "movie"}:
            return None
        item_id = item.get("Id")
        if not item_id:
            return None
        run_ticks = item.get("RunTimeTicks", 0)
        duration = int(run_ticks / 10_000_000) if run_ticks else None
        provider_ids = item.get("ProviderIds")
        language_metadata = extract_language_metadata(item.get("MediaStreams"))
        return MediaCacheEntry(
            id=str(item_id),
            item_type=item_type,
            source_type="jellyfin",
            source_id=str(item_id),
            title=str(item.get("Name") or "Unknown"),
            duration_seconds=duration,
            metadata={
                "series_id": item.get("SeriesId"),
                "season_id": item.get("SeasonId"),
                "series_name": item.get("SeriesName"),
                "index_number": item.get("IndexNumber"),
                "parent_index_number": item.get("ParentIndexNumber"),
                "year": item.get("ProductionYear"),
                "overview": item.get("Overview"),
                "genres": _string_list(item.get("Genres")),
                "tags": _string_list(item.get("Tags")),
                "studios": _studio_names(item.get("Studios")),
                "parental_rating": item.get("OfficialRating"),
                "community_rating": item.get("CommunityRating"),
                "critic_rating": item.get("CriticRating"),
                "provider_ids": provider_ids if isinstance(provider_ids, dict) else {},
                **language_metadata,
            },
            available=True,
        )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _studio_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("Name"):
            names.append(str(item["Name"]).strip())
        elif str(item).strip():
            names.append(str(item).strip())
    return [name for name in names if name]
