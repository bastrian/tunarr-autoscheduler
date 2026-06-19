from __future__ import annotations

from typing import Any

import httpx

RECOMMENDATION_ITEM_FIELDS = (
    "Genres",
    "Tags",
    "Studios",
    "ProviderIds",
    "MediaStreams",
    "Overview",
    "OfficialRating",
    "CommunityRating",
    "CriticRating",
)


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, user_id: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._user_id = user_id
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-Emby-Token": self._api_key},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def check_connection(self) -> bool:
        try:
            resp = await self._client.get("/System/Info/Public")
            resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def get_items(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = params or {}
        query.setdefault("UserId", self._user_id)
        resp = await self._client.get("/Items", params=query)
        resp.raise_for_status()
        data = resp.json()
        return data.get("Items", [])  # type: ignore[no-any-return]

    async def get_item(self, item_id: str) -> dict[str, Any] | None:
        resp = await self._client.get(
            f"/Users/{self._user_id}/Items/{item_id}",
            params={"Fields": ",".join(RECOMMENDATION_ITEM_FIELDS)},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_episodes(
        self, show_id: str, season_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "UserId": self._user_id,
        }
        if season_id:
            params["SeasonId"] = season_id
        resp = await self._client.get(f"/Shows/{show_id}/Episodes", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("Items", [])  # type: ignore[no-any-return]

    async def get_recently_added(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.get_items({
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": limit,
            "Recursive": "true",
            "IncludeItemTypes": "Episode,Movie",
            "Fields": ",".join(RECOMMENDATION_ITEM_FIELDS),
        })

    async def get_all_media(self, page_size: int = 500) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_index = 0
        while True:
            page = await self.get_items({
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "StartIndex": start_index,
                "Limit": page_size,
                "Recursive": "true",
                "IncludeItemTypes": "Episode,Movie",
                "Fields": ",".join(RECOMMENDATION_ITEM_FIELDS),
            })
            items.extend(page)
            if len(page) < page_size:
                break
            start_index += len(page)
        return items

    async def get_seasons(self, show_id: str) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/Shows/{show_id}/Seasons", params={"UserId": self._user_id})
        resp.raise_for_status()
        data = resp.json()
        return data.get("Items", [])  # type: ignore[no-any-return]

    async def get_movies(self) -> list[dict[str, Any]]:
        return await self.get_items({
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": ",".join(RECOMMENDATION_ITEM_FIELDS),
        })

    async def get_shows(self) -> list[dict[str, Any]]:
        return await self.get_items({
            "IncludeItemTypes": "Series",
            "Recursive": "true",
        })
