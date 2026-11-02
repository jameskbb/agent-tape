"""Retry policy: which failures are retried and how long to wait (pure)."""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field

from agenttape.domain.enums import DEFAULT_RETRYABLE, FailureCategory


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 300.0
    jitter_ratio: float = 0.0
    retry_on: frozenset[FailureCategory] = field(default_factory=lambda: DEFAULT_RETRYABLE)

    @classmethod
    def from_config(
        cls,
        config: dict[str, object] | None,
        *,
        default_max_attempts: int,
        default_base: float,
        default_max: float,
    ) -> RetryPolicy:
        cfg = config or {}
        retry_on_raw = cfg.get("retry_on")
        retry_on = (
            frozenset(FailureCategory(str(c)) for c in retry_on_raw)
            if isinstance(retry_on_raw, Iterable) and not isinstance(retry_on_raw, str | bytes)
            else DEFAULT_RETRYABLE
        )
        return cls(
            max_attempts=max(1, int(str(cfg.get("max_attempts", default_max_attempts)))),
            backoff_base_seconds=float(str(cfg.get("backoff_base_seconds", default_base))),
            backoff_max_seconds=float(str(cfg.get("backoff_max_seconds", default_max))),
            jitter_ratio=float(str(cfg.get("jitter_ratio", 0.0))),
            retry_on=retry_on,
        )

    def should_retry(self, category: FailureCategory | str | None, attempt_number: int) -> bool:
        if category is None or attempt_number >= self.max_attempts:
            return False
        return FailureCategory(category) in self.retry_on

    def backoff_seconds(self, attempt_number: int, rng: random.Random | None = None) -> float:
        """Delay before attempt ``attempt_number + 1``: ``base * 2**(n-1)``, capped, jittered."""
        exponent = max(0, attempt_number - 1)
        delay = min(self.backoff_max_seconds, self.backoff_base_seconds * float(2**exponent))
        if self.jitter_ratio > 0:
            r = rng or random.Random()
            delay = delay * (1 - self.jitter_ratio + 2 * self.jitter_ratio * r.random())
        return max(0.0, min(delay, self.backoff_max_seconds))
