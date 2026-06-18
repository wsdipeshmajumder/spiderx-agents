"""Audio codec helpers shared across telephony providers.

Every provider in our adapter family streams µ-law 8 kHz mono in 20 ms frames
(160 bytes). The only thing that differs is the WebSocket envelope shape
(`event: media` vs `event: playAudio` etc.) — the wire codec itself is
identical. This module owns the codec layer so providers can stay short and
boring.

The Gemini Live API speaks PCM16: 16 kHz on the inbound side (we send 16k
PCM blobs), 24 kHz on the outbound side (we receive 24k PCM responses).
The resampling state must persist for the duration of the call (audioop.ratecv
is stateful — passing fresh `None` per chunk produces audible clicks at
every frame boundary).
"""
from __future__ import annotations

import audioop


# 20 ms of µ-law 8 kHz → 160 bytes per frame. Carriers expect outbound audio
# split into ~20 ms chunks; bigger chunks cause clipping at the playback end.
ULAW_FRAME_BYTES = 160


def ulaw_to_pcm16k(ulaw: bytes, state: object | None) -> tuple[bytes, object]:
    """Decode µ-law 8 kHz mono → PCM16 16 kHz mono.

    `state` is the resampler's internal state; pass it back in on the next
    call so the resampler picks up where it left off. Returns the converted
    audio and the new state.
    """
    pcm8k = audioop.ulaw2lin(ulaw, 2)
    pcm16k, new_state = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, state)
    return pcm16k, new_state


def pcm24k_to_ulaw(pcm24k: bytes, state: object | None) -> tuple[bytes, object]:
    """Resample PCM16 24 kHz mono → PCM16 8 kHz mono and µ-law encode.

    Returns (ulaw_bytes, new_state). Carriers consume µ-law 8 kHz so we
    downsample first, then encode.
    """
    pcm8k, new_state = audioop.ratecv(pcm24k, 2, 1, 24000, 8000, state)
    ulaw = audioop.lin2ulaw(pcm8k, 2)
    return ulaw, new_state


def chunk_ulaw(ulaw: bytes, frame_bytes: int = ULAW_FRAME_BYTES) -> list[bytes]:
    """Split a µ-law buffer into 20 ms frames for outbound streaming.

    Carriers prefer many small frames over one large blob — large blobs
    can stall their jitter buffer or be silently truncated.
    """
    return [ulaw[i:i + frame_bytes] for i in range(0, len(ulaw), frame_bytes)]
