from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(path))
        self._conn.row_factory = aiosqlite.Row

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, *params: Any) -> None:
        assert self._conn is not None
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def execute_many(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        assert self._conn is not None
        await self._conn.executemany(sql, params_list)
        await self._conn.commit()

    async def fetch_one(self, sql: str, *params: Any) -> dict[str, Any] | None:
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        assert self._conn is not None
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    @property
    def path(self) -> str:
        return self._db_path
