"""Fish Audio TTS provider (Phase 1 — voice-engine option).

A thin async client over Fish Audio's REST TTS API (https://fish.audio),
added as a SELECTABLE voice engine alongside the default Gemini native audio.
Phase 1 wires the provider + a synthesis path used by the "Preview voice"
button in Voice & behaviour; it does NOT touch the live Gemini phone pipeline
(that stays the default and unchanged). Phase 2 would route live calls through
an STT → LLM → Fish-TTS cascade.

API shape validated against the live service: `POST /v1/tts` with a Bearer key,
a `model:` header selecting the TTS backbone (`s1` / `speech-1.6` / `speech-1.5`),
and a JSON body `{text, reference_id, format, …}` returning audio bytes. A 402
means the Fish *API credit* (separate from platform credit) is exhausted.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

FISH_API_BASE = os.environ.get("FISH_AUDIO_API_BASE", "https://api.fish.audio").rstrip("/")
# Default TTS backbone. s1 is the newest; overridable per call / via env.
FISH_TTS_MODEL = os.environ.get("FISH_TTS_MODEL", "s1")

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
