"""Rate limiting.

Two jobs: slow brute-force on the auth endpoints, and cap LLM spend per user.
The second one is a security control, not a billing feature -- an attacker who
cannot read your data can still empty your OpenAI account.

This implementation is **in-process**, so each app instance counts separately.
That is fine for one instance and wrong for several: swap `_Bucket` for Redis
before scaling out, or the effective limit silently multiplies by the instance
count.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int) -> None:
        """Record a hit for `key`; raise 429 if it exceeds `limit` in the window."""
        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = int(hits[0] - cutoff) + 1
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many requests. Please slow down.",
                    headers={"Retry-After": str(retry_after)},
                )

            hits.append(now)

            # Opportunistic cleanup so abandoned keys do not grow without bound.
            if len(self._hits) > 10_000:
                for stale_key in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale_key]


limiter = RateLimiter()
