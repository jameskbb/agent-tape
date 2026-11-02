"""In-process token-bucket rate limiter (per key), used by auth and webhook endpoints."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """``rate`` tokens per ``per_seconds``, bursting up to ``burst`` (default ``rate``)."""

    def __init__(
        self,
        rate: float,
        per_seconds: float = 60.0,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if rate <= 0 or per_seconds <= 0:
            raise ValueError("rate and per_seconds must be positive")
        self.capacity = float(burst if burst is not None else rate)
        self.refill_per_second = rate / per_seconds
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict(now)
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True
            return False

    def reset(self) -> None:
        """Clear every bucket - for tests, and for an operator-triggered reset; production
        code never needs this (a context's limiter lives for the whole process lifetime)."""
        with self._lock:
            self._buckets.clear()

    def retry_after(self, key: str, cost: float = 1.0) -> float:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.tokens >= cost:
                return 0.0
            return (cost - bucket.tokens) / self.refill_per_second

    def _evict(self, now: float) -> None:
        full = [
            k
            for k, b in self._buckets.items()
            if b.tokens + (now - b.updated) * self.refill_per_second >= self.capacity
        ]
        for k in full or list(self._buckets)[: len(self._buckets) // 2]:
            self._buckets.pop(k, None)
