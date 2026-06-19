from __future__ import annotations

import httpx

from tunarr_autoscheduler.integrations.jellyfin.client import JellyfinClient


async def test_get_all_media_paginates_until_short_page() -> None:
    start_indexes: list[int] = []
    fields: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start_index = int(request.url.params.get("StartIndex", "0"))
        start_indexes.append(start_index)
        fields.append(str(request.url.params.get("Fields", "")))
        page_size = int(request.url.params["Limit"])
        item_count = page_size if start_index == 0 else 2
        return httpx.Response(
            200,
            json={"Items": [{"Id": f"item-{start_index + i}"} for i in range(item_count)]},
        )

    client = JellyfinClient("http://jellyfin.test", "key", "user")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://jellyfin.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        items = await client.get_all_media(page_size=3)
    finally:
        await client.close()

    assert start_indexes == [0, 3]
    assert all("MediaStreams" in field for field in fields)
    assert all("ProviderIds" in field for field in fields)
    assert len(items) == 5
