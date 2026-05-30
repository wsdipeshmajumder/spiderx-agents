"""Per-org rate limit — in-memory token bucket.

Phase 6 ships the simplest thing that works: an in-process counter
keyed by org_id with the limit + window read from platform_settings.
For single-worker dev this is correct. Multi-worker Railway scales it
to "per-worker" which is fine until traffic gets noticeable; the
upgrade path is to swap the in-memory dict for a Redis-backed counter
without touching call sites.

Token bucket model:
  - Each org gets a bucket with `capacity` tokens.
  - Every request consumes 1 token.
  - Tokens refill linearly over `window_s` seconds back to capacity.

Defaults (overridable via platform_settings):
  - capacity:  60   (60 requests per window)
  - window_s:  60   (1-minute window)

This gives roughly 1 req/sec sustained per org. Bursts up to the
capacity are absorbed instantly. Super-admin routes bypass the limit
entirely (they're rare + high-trust).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import HTTPException


# org_id -> (tokens, last_refill_at)
_BUCKETS: dict[int, tuple[float, float]] = {}
_LOCK = asyncio.Lock()


async def _config() -> tuple[float, float]:
    """Reads capacity + window from platform_settings with a sane fallback.
    Cached behind settings.py's 60s TTL so this isn't a hot DB hit."""
    from . import settings as cfg
    capacity = float(await cfg.get("limits.rate_capacity", 60))
    window_s = float(await cfg.get("limits.rate_window_s", 60))
    return capacity, max(1.0, window_s)


async def acquire(org_id: Optional[int]) -> None:
    """Consume one token for this org. Raises 429 if the bucket is empty.

    org_id=None (anonymous / super-admin call) bypasses the limit — those
    paths are either rare (admin) or pre-auth (signup) and have their
    own gates."""
    if org_id is None:
        return
    capacity, window_s = await _config()
    refill_rate = capacity / window_s  # tokens per second
    now = time.monotonic()
    async with _LOCK:
        tokens, last = _BUCKETS.get(org_id, (capacity, now))
        elapsed = now - last
        tokens = min(capacity, tokens + elapsed * refill_rate)
        if tokens < 1.0:
            retry_in = (1.0 - tokens) / refill_rate
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited",
                        "message": f"Too many requests. Retry in {retry_in:.1f}s.",
                        "retry_after_s": round(retry_in, 1)},
                headers={"Retry-After": str(int(retry_in) + 1)},
            )
        _BUCKETS[org_id] = (tokens - 1.0, now)


def reset() -> None:
    """Test helper — wipe every bucket. Production callers don't use this."""
    _BUCKETS.clear()
