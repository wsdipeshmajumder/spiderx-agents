"""Platform settings — read-through cache over the `platform_settings` table.

Use this everywhere a value used to be a hardcoded constant in app code.
Example:
    from backend import settings as cfg
    free_mins = await cfg.get("limits.free_minutes_per_month", 30)
    if await cfg.get("features.signups_open", True) is False:
        raise HTTPException(403, "signups closed")

Caching rules:
  * 60s TTL — settings change rarely; the cost of a fresh PK lookup is
    cheap, but cold-cache hit rate from a 60s TTL keeps the hot path
    (every request reads `models.agent_model_id`, every signup reads
    `features.signups_open`) at zero DB cost.
  * Cache invalidates immediately on any `set()` from the admin route,
    so an admin flipping a flag sees the effect on the next request,
    not 60 seconds later.
  * Process-local cache. Multi-worker deployments will diverge for at
    most one TTL window — acceptable for an internal-config table that
    isn't on a hot consistency path. When we go multi-worker on Railway
    we'll add a Redis-pub-sub invalidate broadcast; until then the
    `_last_load` timestamp is the safety net.

The `_DEFAULTS` mapping at the bottom mirrors the Alembic 0004 seed. If
a key gets accidentally deleted from the DB, the default kicks in so the
app never 500s — the operator gets the last-known-good behaviour while
they fix the row.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from . import db_pg as db


_CACHE: dict[str, Any] = {}
_CATEGORY_CACHE: dict[str, str] = {}     # key → category, for the admin UI
_LABEL_CACHE: dict[str, str] = {}        # key → label
_DESC_CACHE: dict[str, str] = {}         # key → description
_LAST_LOAD: float = 0.0
_TTL_S: float = 60.0
_LOCK = asyncio.Lock()


async def _refresh() -> None:
    """Pull every row out of platform_settings and replace the in-memory
    cache. Called under `_LOCK` so concurrent first-callers don't all
    hit the DB."""
    global _LAST_LOAD
    rows = await db.list_platform_settings()
    _CACHE.clear()
    _CATEGORY_CACHE.clear()
    _LABEL_CACHE.clear()
    _DESC_CACHE.clear()
    for r in rows:
        _CACHE[r["key"]] = r["value"]
        _CATEGORY_CACHE[r["key"]] = r["category"]
        _LABEL_CACHE[r["key"]] = r["label"]
        _DESC_CACHE[r["key"]] = r.get("description") or ""
    _LAST_LOAD = time.time()


async def _ensure_fresh() -> None:
    if time.time() - _LAST_LOAD < _TTL_S and _CACHE:
        return
    async with _LOCK:
        # Double-check under the lock — another coroutine may have just refreshed.
        if time.time() - _LAST_LOAD >= _TTL_S or not _CACHE:
            await _refresh()


async def get(key: str, default: Any = None) -> Any:
    """The single read primitive. Returns the value or `default` if the
    key isn't in the table. Hot path — guard against import-time DB calls."""
    await _ensure_fresh()
    if key in _CACHE:
        return _CACHE[key]
    # Fallback to the static defaults map below — covers the case where
    # the DB row got deleted but we still want the app to function.
    return _DEFAULTS.get(key, default)


async def get_many(category: Optional[str] = None) -> list[dict[str, Any]]:
    """Returns every cached row (or every row in a category) with full
    metadata for the admin UI. Always pulls a fresh snapshot to avoid
    the admin seeing stale values while editing."""
    async with _LOCK:
        await _refresh()
    out: list[dict[str, Any]] = []
    for key, value in _CACHE.items():
        if category and _CATEGORY_CACHE.get(key) != category:
            continue
        out.append({
            "key": key,
            "value": value,
            "category": _CATEGORY_CACHE.get(key, "other"),
            "label": _LABEL_CACHE.get(key, key),
            "description": _DESC_CACHE.get(key, ""),
        })
    # Stable sort: category then key, so the UI doesn't reshuffle.
    out.sort(key=lambda r: (r["category"], r["key"]))
    return out


async def set(key: str, value: Any, updated_by: int) -> dict[str, Any]:
    """Update one setting. Cache invalidates immediately so the next
    `get()` sees the new value without waiting for the TTL window.
    Returns the row's previous + new value so the admin route can pin
    the diff to the audit log."""
    before = _CACHE.get(key)
    updated = await db.set_platform_setting(key, value, updated_by=updated_by)
    if updated:
        # Re-populate the cache from this single row rather than a full
        # refresh — keeps the change visible immediately even under load.
        _CACHE[key] = updated["value"]
        _CATEGORY_CACHE[key] = updated["category"]
        _LABEL_CACHE[key] = updated["label"]
        _DESC_CACHE[key] = updated.get("description") or ""
    return {"key": key, "before": before, "after": value, "row": updated}


def invalidate() -> None:
    """Force-evict the cache. Useful for tests; production normally relies
    on the per-set immediate refresh + the 60s TTL."""
    global _LAST_LOAD
    _CACHE.clear()
    _CATEGORY_CACHE.clear()
    _LABEL_CACHE.clear()
    _DESC_CACHE.clear()
    _LAST_LOAD = 0.0


# ─── Static fallback defaults ────────────────────────────────────────────
# Mirrors the Alembic 0004 seed. Survives accidental row deletion so the
# app keeps working with last-known-good behaviour.
_DEFAULTS: dict[str, Any] = {
    "models.builder_model_id":      "gemini-3.1-flash-live-preview",
    "models.agent_model_id":        "gemini-3.1-flash-live-preview",
    "models.tts_preview_model":     "gemini-2.5-flash-preview-tts",
    "limits.free_minutes_per_month": 30,
    "limits.max_agents_free":       3,
    "limits.max_kb_chars":          100000,
    "limits.invite_ttl_days":       7,
    "features.ambience_beta":       True,
    "features.knowledge_url_fetch": False,
    "features.signups_open":        True,
    # Build 244 — Ask-Eva floating helper. When false, the bubble + the
    # expanded card never render anywhere in the SPA. Default ON so the
    # feature works the moment a fresh install boots; flip OFF via
    # Platform settings → Features when a tenant doesn't want it.
    "features.eva_assist":          True,
    "branding.support_email":       "support@spiderx.ai",
    "branding.brand_palette":       {"primary": "#a78bfa", "accent": "#2563eb"},
    # ── Agent healthcheck (build 231) ──
    # See backend/agent_healthcheck.py for the probe implementations.
    # Defaults mirror Alembic 0023; presence here keeps the app working
    # if that migration hasn't run yet.
    "healthcheck.level2_enabled":            True,
    "healthcheck.level3_enabled":            False,   # opt-in: real Gemini cost
    "healthcheck.level3_sample_size":        25,
    "healthcheck.email_on_failure":          True,
    "healthcheck.email_recipients":          "",
    "healthcheck.level4_pstn_enabled":       False,   # placeholder
    "healthcheck.level4_pstn_provider":      "twilio",
    "healthcheck.level4_pstn_from_number":   "",
    "healthcheck.level4_pstn_to_number":     "",
}
