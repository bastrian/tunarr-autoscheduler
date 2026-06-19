from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.models.playlist import (
    Playlist,
    PlaylistCategory,
    PlaylistItem,
)


class PlaylistRepository:
    def __init__(self, db: Database):
        self._db = db

    async def list_all(self) -> list[Playlist]:
        rows = await self._db.fetch_all(
            "SELECT p.*, c.name AS category_name "
            "FROM playlists p "
            "LEFT JOIN playlist_categories c ON c.id = p.category_id "
            "ORDER BY lower(p.name), p.id",
        )
        return [await self._row_to_playlist(row) for row in rows]

    async def get(self, playlist_id: str) -> Playlist | None:
        row = await self._db.fetch_one(
            "SELECT p.*, c.name AS category_name "
            "FROM playlists p "
            "LEFT JOIN playlist_categories c ON c.id = p.category_id "
            "WHERE p.id = ?",
            playlist_id,
        )
        return await self._row_to_playlist(row) if row else None

    async def create(
        self,
        name: str,
        description: str = "",
        items: list[PlaylistItem] | None = None,
        category_id: str = "",
        channel_scope: str = "",
        tags: list[str] | None = None,
    ) -> Playlist:
        now = datetime.now(tz=UTC)
        playlist_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO playlists "
            "(id, name, description, category_id, channel_scope, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            playlist_id,
            name,
            description,
            category_id,
            channel_scope,
            now.isoformat(),
            now.isoformat(),
        )
        await self.set_tags(playlist_id, tags or [])
        await self.set_items(playlist_id, items or [])
        playlist = await self.get(playlist_id)
        if playlist is None:
            raise RuntimeError("Playlist was not created")
        return playlist

    async def update(
        self,
        playlist_id: str,
        name: str,
        description: str,
        items: list[PlaylistItem],
        category_id: str = "",
        channel_scope: str = "",
        tags: list[str] | None = None,
    ) -> Playlist | None:
        existing = await self.get(playlist_id)
        if existing is None:
            return None
        await self._db.execute(
            "UPDATE playlists "
            "SET name = ?, description = ?, category_id = ?, channel_scope = ?, "
            "updated_at = ? WHERE id = ?",
            name,
            description,
            category_id,
            channel_scope,
            datetime.now(tz=UTC).isoformat(),
            playlist_id,
        )
        await self.set_tags(playlist_id, tags or [])
        await self.set_items(playlist_id, items)
        return await self.get(playlist_id)

    async def delete(self, playlist_id: str) -> bool:
        existing = await self.get(playlist_id)
        if existing is None:
            return False
        await self._db.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ?", playlist_id,
        )
        await self._db.execute("DELETE FROM playlist_tags WHERE playlist_id = ?", playlist_id)
        await self._db.execute("DELETE FROM playlists WHERE id = ?", playlist_id)
        return True

    async def set_items(self, playlist_id: str, items: list[PlaylistItem]) -> None:
        await self._db.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ?", playlist_id,
        )
        if not items:
            return
        ordered = sorted(items, key=lambda item: item.position)
        await self._db.execute_many(
            "INSERT INTO playlist_items "
            "(playlist_id, media_type, media_id, title, position) VALUES (?, ?, ?, ?, ?)",
            [
                (playlist_id, item.media_type, item.media_id, item.title, position)
                for position, item in enumerate(ordered)
            ],
        )

    async def get_items(self, playlist_ids: list[str]) -> list[PlaylistItem]:
        items: list[PlaylistItem] = []
        for playlist_id in playlist_ids:
            rows = await self._db.fetch_all(
                "SELECT media_type, media_id, title, position FROM playlist_items "
                "WHERE playlist_id = ? ORDER BY position",
                playlist_id,
            )
            items.extend(PlaylistItem.model_validate(row) for row in rows)
        return items

    async def list_categories(self) -> list[PlaylistCategory]:
        rows = await self._db.fetch_all(
            "SELECT * FROM playlist_categories ORDER BY lower(name), id",
        )
        return [
            PlaylistCategory(
                id=row["id"],
                name=row["name"],
                description=row.get("description", ""),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    async def create_category(
        self,
        name: str,
        description: str = "",
    ) -> PlaylistCategory:
        now = datetime.now(tz=UTC)
        category_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO playlist_categories "
            "(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            category_id,
            name,
            description,
            now.isoformat(),
            now.isoformat(),
        )
        category = await self.get_category(category_id)
        if category is None:
            raise RuntimeError("Playlist category was not created")
        return category

    async def get_category(self, category_id: str) -> PlaylistCategory | None:
        row = await self._db.fetch_one(
            "SELECT * FROM playlist_categories WHERE id = ?",
            category_id,
        )
        if row is None:
            return None
        return PlaylistCategory(
            id=row["id"],
            name=row["name"],
            description=row.get("description", ""),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def update_category(
        self,
        category_id: str,
        name: str,
        description: str = "",
    ) -> PlaylistCategory | None:
        existing = await self.get_category(category_id)
        if existing is None:
            return None
        await self._db.execute(
            "UPDATE playlist_categories SET name = ?, description = ?, updated_at = ? "
            "WHERE id = ?",
            name,
            description,
            datetime.now(tz=UTC).isoformat(),
            category_id,
        )
        return await self.get_category(category_id)

    async def delete_category(self, category_id: str) -> bool:
        existing = await self.get_category(category_id)
        if existing is None:
            return False
        await self._db.execute(
            "UPDATE playlists SET category_id = '', updated_at = ? WHERE category_id = ?",
            datetime.now(tz=UTC).isoformat(),
            category_id,
        )
        await self._db.execute("DELETE FROM playlist_categories WHERE id = ?", category_id)
        return True

    async def list_tags(self) -> list[str]:
        rows = await self._db.fetch_all(
            "SELECT DISTINCT tag FROM playlist_tags ORDER BY lower(tag), tag",
        )
        return [row["tag"] for row in rows]

    async def get_recommendation_terms_by_media_id(self) -> dict[str, list[str]]:
        playlists = await self.list_all()
        terms_by_media_id: dict[str, set[str]] = {}
        for playlist in playlists:
            playlist_terms = _recommendation_terms([
                playlist.name,
                playlist.description,
                playlist.category_name,
                *playlist.tags,
            ])
            if not playlist_terms:
                continue
            for item in playlist.items:
                terms_by_media_id.setdefault(item.media_id, set()).update(playlist_terms)
        return {
            media_id: sorted(terms)
            for media_id, terms in terms_by_media_id.items()
        }

    async def set_tags(self, playlist_id: str, tags: list[str]) -> None:
        await self._db.execute("DELETE FROM playlist_tags WHERE playlist_id = ?", playlist_id)
        normalized = _normalize_tags(tags)
        if not normalized:
            return
        await self._db.execute_many(
            "INSERT INTO playlist_tags (playlist_id, tag) VALUES (?, ?)",
            [(playlist_id, tag) for tag in normalized],
        )

    async def get_tags(self, playlist_id: str) -> list[str]:
        rows = await self._db.fetch_all(
            "SELECT tag FROM playlist_tags WHERE playlist_id = ? ORDER BY lower(tag), tag",
            playlist_id,
        )
        return [row["tag"] for row in rows]

    async def _row_to_playlist(self, row: dict[str, Any]) -> Playlist:
        return Playlist(
            id=row["id"],
            name=row["name"],
            description=row.get("description", ""),
            category_id=row.get("category_id", ""),
            category_name=row.get("category_name") or "",
            channel_scope=row.get("channel_scope", ""),
            tags=await self.get_tags(row["id"]),
            items=await self.get_items([row["id"]]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


def _normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = " ".join(raw.strip().lower().split())
        if not tag or tag in seen:
            continue
        normalized.append(tag)
        seen.add(tag)
    return normalized


def _recommendation_terms(values: list[str]) -> set[str]:
    terms: set[str] = set()
    for value in values:
        normalized = " ".join(str(value).strip().lower().split())
        if not normalized:
            continue
        terms.add(normalized)
        terms.update(part for part in normalized.replace("-", " ").split(" ") if part)
    return terms
