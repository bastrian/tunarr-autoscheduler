from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic


class AsyncRateLimiter:
    def __init__(self, *, max_calls: int, period_seconds: float = 60.0):
        self.max_calls = max(1, max_calls)
        self.period_seconds = max(0.1, period_seconds)
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                while self._calls and now - self._calls[0] >= self.period_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait_seconds = self.period_seconds - (now - self._calls[0])
            await asyncio.sleep(max(0.05, wait_seconds))


DEFAULT_PROVIDER_LIMITS = {
    "tmdb": 120,
    "tvdb": 60,
    "omdb": 60,
}
