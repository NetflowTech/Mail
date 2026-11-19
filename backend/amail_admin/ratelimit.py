"""In-memory sliding-window rate limiter (one process).

Keys come and go: the device API is reachable from the internet, so every
address that ever knocks gets a key. Keys whose window has emptied are
dropped on a periodic sweep, so memory stays proportional to the clients
active in the last window, not to everyone who ever connected.
"""
from __future__ import annotations

import threading
import time
from collections import deque

SWEEP_EVERY = 60.0


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        # key -> (hits, window length): the window is needed to know when
        # a key has gone quiet for good.
        self._hits: dict[str, tuple[deque, float]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._swept = clock()

    def allow(self, key: str, limit: int, per_seconds: float) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._swept >= SWEEP_EVERY:
                self._sweep(now)
            entry = self._hits.get(key)
            if entry is None:
                entry = (deque(), float(per_seconds))
                self._hits[key] = entry
            q = entry[0]
            while q and now - q[0] > per_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def _sweep(self, now: float) -> None:
        self._swept = now
        for key in [k for k, (q, window) in self._hits.items()
                    if not q or now - q[-1] > window]:
            del self._hits[key]

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)
