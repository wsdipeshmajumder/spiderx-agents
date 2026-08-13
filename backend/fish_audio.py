"""Fish Audio TTS provider — a SELECTABLE voice engine alongside Gemini's
native audio.

A thin async client over Fish Audio's REST TTS API (https://fish.audio).
Drives the "Preview voice" button in Voice & behaviour, AND (build 377+)
live call audio on both the telephony path (`backend/telephony/base.py`,
Fish is the platform-wide default there) and the browser test-call path
(`backend/gemini_bridge.py::run_session`, build 410+) — Gemini stays the
STT + reasoning + tool-calling brain in both, Fish just speaks the words.

API shape validated against the live service: `POST /v1/tts` with a Bearer key,
a `model:` header selecting the TTS backbone (default `s2.1-pro-free` — Fish's
free tier; also `s1` / `speech-1.6` / `speech-1.5` on paid credit), and a JSON
body `{text, reference_id, format, …}` returning audio bytes. A 402 means the
Fish *API credit* (separate from platform credit) is exhausted — not hit on the
free backbone.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import httpx

FISH_API_BASE = os.environ.get("FISH_AUDIO_API_BASE", "https://api.fish.audio").rstrip("/")
# Default TTS backbone. `s2.1-pro-free` is Fish's free tier (state-of-the-art
# model, no API credit required, free through 2026-08-31 per fish.audio/blog/
# s2-1-pro-free-api) — chosen as the default so preview/TTS works out of the box
# without topping up API credit. Overridable per call / via FISH_TTS_MODEL env
# to a paid backbone (e.g. `s1`, `speech-1.6`, `speech-1.5`) if credit is added.
FISH_TTS_MODEL = os.environ.get("FISH_TTS_MODEL", "s2.1-pro-free")

# A small curated set of natural voices so the dropdown is useful out of the box
# (operators can also paste any Fish model id). Ids resolved from the live model
# catalogue; `reference_id` in the TTS body.
DEFAULT_VOICES: list[dict[str, str]] = [
    {"id": "001262690f2a4eea84aa764cc536df24", "label": "Amy — English Woman (US), conversational", "lang": "en"},
    {"id": "d67524ad1936410896ad120583cb1117", "label": "Andrew — English Man (US), storytelling", "lang": "en"},
    {"id": "98e364e9a41c465a9d4fdafc267f84ea", "label": "Anthony — Deep English Man", "lang": "en"},
]


class FishAudioError(Exception):
    """Raised on any Fish API failure. `status` mirrors the HTTP status so the
    caller can surface a precise message (402 = out of API credit)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _api_key() -> str:
    key = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()
    if not key:
        raise FishAudioError("Fish Audio is not configured (FISH_AUDIO_API_KEY unset).", status=503)
    return key


def is_configured() -> bool:
    return bool((os.environ.get("FISH_AUDIO_API_KEY") or "").strip())


def resolve_voice_engine(voice_tweaks: Optional[dict]) -> tuple[bool, Optional[str]]:
    """Resolve an agent's voice_tweaks to (fish_active, fish_voice_id).

    Single source of truth for every live-call path (telephony's `_bridge`
    AND the browser test path's `run_session`), and for the
    `fish_voice_not_selected` config-warning check — so this resolution
    can't drift out of sync between copies again.

    Build 416 incident: a real production call (agent 5 "Mira",
    voice_provider defaulting to "fish" with no fish_voice_id ever chosen)
    showed that activating Fish with `reference_id` omitted is NOT safe —
    the free backbone doesn't reliably pick the same voice per request, and
    an intermittent synth failure flips the existing degrade-to-Gemini
    safety net mid-call, so the caller hears the voice change mid-call,
    which reads as two AIs talking over each other. Requiring an explicit
    fish_voice_id before Fish activates at all (falling back to Gemini's
    own reliable voice otherwise) is the fix — this function is where that
    requirement is now enforced, once, for every caller."""
    vt = voice_tweaks or {}
    provider = str(vt.get("voice_provider") or "fish").strip().lower()
    voice_id = (str(vt.get("fish_voice_id") or "").strip() or None)
    active = provider == "fish" and voice_id is not None and is_configured()
    return active, voice_id


async def synthesize(
    text: str,
    *,
    reference_id: Optional[str] = None,
    backbone: Optional[str] = None,
    fmt: str = "mp3",
    timeout: float = 30.0,
) -> bytes:
    """Synthesize `text` to speech via Fish Audio. Returns the raw audio bytes
    (mp3 by default). Raises FishAudioError on failure (with the HTTP status so
    a 402 'no API credit' surfaces cleanly)."""
    text = (text or "").strip()
    if not text:
        raise FishAudioError("Nothing to synthesize (empty text).", status=400)
    if len(text) > 2000:
        text = text[:2000]
    body: dict[str, Any] = {
        "text": text,
        "format": fmt,
        "mp3_bitrate": 128,
        "normalize": True,
        "latency": "normal",
    }
    ref = (reference_id or "").strip()
    if ref:
        body["reference_id"] = ref
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "model": (backbone or FISH_TTS_MODEL),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{FISH_API_BASE}/v1/tts", json=body, headers=headers)
    except httpx.HTTPError as e:  # noqa: BLE001
        raise FishAudioError(f"Fish Audio request failed: {e}", status=502) from e
    if resp.status_code >= 400:
        # Fish returns JSON errors; surface the message + status verbatim-ish.
        msg = ""
        try:
            msg = (resp.json() or {}).get("message") or ""
        except Exception:  # noqa: BLE001
            msg = (resp.text or "")[:200]
        if resp.status_code == 402:
            msg = msg or "Fish Audio API credit exhausted (top up API credit — it's separate from platform credit)."
        raise FishAudioError(msg or f"Fish Audio error {resp.status_code}", status=resp.status_code)
    audio = resp.content
    if not audio:
        raise FishAudioError("Fish Audio returned no audio.", status=502)
    return audio


async def list_voices(*, limit: int = 12, language: str = "en") -> list[dict[str, str]]:
    """Best-effort fetch of top voice models for the picker. Falls back to the
    curated DEFAULT_VOICES if the catalogue call fails (or isn't reachable)."""
    try:
        headers = {"Authorization": f"Bearer {_api_key()}"}
        params = {"page_size": str(limit), "sort_by": "score", "language": language}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{FISH_API_BASE}/model", params=params, headers=headers)
        if resp.status_code >= 400:
            return list(DEFAULT_VOICES)
        items = (resp.json() or {}).get("items") or []
        out = []
        for m in items:
            mid = m.get("_id") or m.get("id")
            title = (m.get("title") or m.get("name") or "").strip()
            if mid and title:
                out.append({"id": mid, "label": title[:60],
                            "lang": (m.get("languages") or [language])[0]})
        return out or list(DEFAULT_VOICES)
    except Exception:  # noqa: BLE001
        return list(DEFAULT_VOICES)


# ─── Sentence-boundary flush queue ────────────────────────────────────────
#
# Shared by every live-call Fish integration (telephony's `_bridge` AND the
# browser test path's `run_session`) so both synthesize+play incrementally,
# sentence-by-sentence, instead of waiting for a whole model turn — lower
# time-to-first-audio. Lives here (not in either caller) because gemini_bridge
# and telephony.base already have a one-way import relationship
# (telephony.base imports from gemini_bridge) — putting shared Fish helpers
# in this leaf module, which neither of them needs to import FROM the other
# to reach, avoids a circular import.

_SENT_BOUNDARY = re.compile(r"[.!?…]+[\s\"'\)\]]*")
# Flush a run-on fragment even without punctuation once it gets this long, so a
# comma-spliced monologue doesn't stall the voice waiting for a full stop.
_FISH_MAX_FRAGMENT = 200


def _fish_flush(q: "asyncio.Queue", fx: dict[str, Any], *, final: bool) -> None:
    """Move ready text out of the fx buffer onto the synth queue, tagged with
    the current barge-in generation so stale segments can be dropped."""
    text = fx.get("text") or ""
    seg = None
    if final:
        seg, fx["text"] = text.strip(), ""
    else:
        bounds = list(_SENT_BOUNDARY.finditer(text))
        if bounds:
            idx = bounds[-1].end()
            seg, fx["text"] = text[:idx].strip(), text[idx:]
        elif len(text) > _FISH_MAX_FRAGMENT:
            seg, fx["text"] = text.strip(), ""
    if seg:
        try:
            q.put_nowait((seg, fx["gen"]))
        except Exception:  # noqa: BLE001
            pass


def _drain_queue(q: "asyncio.Queue") -> None:
    """Discard everything queued (barge-in: the caller cut the agent off)."""
    while True:
        try:
            q.get_nowait()
        except Exception:  # noqa: BLE001
            break
