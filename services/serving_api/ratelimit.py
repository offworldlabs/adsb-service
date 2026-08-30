"""Per-client token-bucket rate limiter with a bounded LRU table (DESIGN.md §5)."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable


class TokenBucketLimiter:
    """Token bucket per key (client IP), monotonic clock, bounded table.

    The table is LRU-bounded (AI-08 bounded-everything): the least recently
    seen client is evicted first. An evicted client that returns gets a fresh
    full bucket — under table pressure the limiter forgives rather than
    over-punishes, which is the safe direction for a loopback-fronted API.

    sec-7: that forgiveness is capped by a global new-key admission bucket,
    or rotating > lru_size source keys (an IPv6 /64 holds plenty) would mint
    a fresh full burst per evicted key. When the admission budget is
    exhausted, unseen keys are treated as rate-limited; established keys are
    unaffected (and are not evicted by the rejected flood).
    """

    def __init__(
        self,
        rate: float,
        burst: float,
        lru_size: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        new_key_rate: float = 50.0,
        new_key_burst: float = 200.0,
    ):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        if lru_size <= 0:
            raise ValueError("lru_size must be > 0")
        if new_key_rate <= 0:
            raise ValueError("new_key_rate must be > 0")
        if new_key_burst <= 0:
            raise ValueError("new_key_burst must be > 0")
        self._rate = float(rate)
        self._burst = float(burst)
        self._lru_size = int(lru_size)
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._new_key_rate = float(new_key_rate)
        self._new_key_burst = float(new_key_burst)
        self._admit_tokens = float(new_key_burst)
        self._admit_last = clock()

    def __len__(self) -> int:
        return len(self._buckets)

    def __contains__(self, key: str) -> bool:
        return key in self._buckets

    def check(self, key: str) -> tuple[bool, float]:
        """Try to spend one token for ``key``.

        Returns ``(allowed, retry_after_seconds)``; ``retry_after_seconds`` is
        0.0 when allowed, else the time until one full token accrues.
        """
        now = self._clock()
        entry = self._buckets.get(key)
        if entry is None and not self._admit_new_key(now):
            # admission budget exhausted: reject without inserting, so the
            # flood cannot evict established clients either (sec-7)
            return False, (1.0 - self._admit_tokens) / self._new_key_rate
        tokens, last = entry if entry is not None else (self._burst, now)
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            allowed = True
            retry_after = 0.0
            tokens -= 1.0
        else:
            allowed = False
            retry_after = (1.0 - tokens) / self._rate
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._lru_size:
            self._buckets.popitem(last=False)
        return allowed, retry_after

    def _admit_new_key(self, now: float) -> bool:
        self._admit_tokens = min(
            self._new_key_burst,
            self._admit_tokens + (now - self._admit_last) * self._new_key_rate,
        )
        self._admit_last = now
        if self._admit_tokens < 1.0:
            return False
        self._admit_tokens -= 1.0
        return True
