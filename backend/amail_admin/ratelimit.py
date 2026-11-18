"""In-memory sliding-window rate limiter (one process, a handful of PCs)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock

    def allow(self, key: str, limit: int, per_seconds: float) -> bool:
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > per_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
