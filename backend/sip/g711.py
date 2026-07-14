"""G.711 codec + RTP-payload/clock facts for the native SIP UAS.

The PSTN side of a Grandstream/Tata call is almost always G.711 at 8 kHz:
  • PCMU (µ-law) — RTP payload type 0
  • PCMA (A-law) — RTP payload type 8
G.722 (payload 9) is wideband 16 kHz but — RTP historically clocks it at 8000;
we leave it out of v1 (optional per the integration spec).

The AI pipeline (Gemini Live bridge) speaks 16 kHz PCM in / 24 kHz PCM out, and
`telephony/audio.py` already owns the µ-law↔PCM resampling. This module is the
thin codec layer the RTP path uses to turn wire bytes into 8 kHz linear PCM and
back, for BOTH µ-law and A-law, so SDP can negotiate either.
"""
from __future__ import annotations

import audioop

# RTP static payload types (RFC 3551) we support, mapped to their SDP encoding
# name. Order = our preference when the offer lists several.
PAYLOAD_PCMU = 0
PAYLOAD_PCMA = 8

# encoding name (as it appears in SDP rtpmap) → RTP payload type
NAME_TO_PT = {"PCMU": PAYLOAD_PCMU, "PCMA": PAYLOAD_PCMA}
PT_TO_NAME = {v: k for k, v in NAME_TO_PT.items()}

CLOCK_HZ = 8000                 # G.711 sample clock (also the RTP timestamp clock)
FRAME_MS = 20                   # standard PSTN packetisation
SAMPLES_PER_FRAME = CLOCK_HZ * FRAME_MS // 1000   # 160 samples @ 8 kHz / 20 ms
FRAME_BYTES = SAMPLES_PER_FRAME                    # G.711 is 1 byte/sample → 160


def decode_to_pcm8k(payload: bytes, pt: int) -> bytes:
    """G.711 wire bytes (µ-law or A-law) → signed 16-bit linear PCM @ 8 kHz."""
    if pt == PAYLOAD_PCMU:
        return audioop.ulaw2lin(payload, 2)
    if pt == PAYLOAD_PCMA:
        return audioop.alaw2lin(payload, 2)
    raise ValueError(f"unsupported RTP payload type {pt}")


def encode_from_pcm8k(pcm8k: bytes, pt: int) -> bytes:
    """Signed 16-bit linear PCM @ 8 kHz → G.711 wire bytes for the negotiated PT."""
    if pt == PAYLOAD_PCMU:
        return audioop.lin2ulaw(pcm8k, 2)
    if pt == PAYLOAD_PCMA:
        return audioop.lin2alaw(pcm8k, 2)
    raise ValueError(f"unsupported RTP payload type {pt}")


def silence(pt: int, frames: int = 1) -> bytes:
    """One (or more) frame(s) of G.711 silence for the given codec — used to
    keep RTP flowing (comfort noise substitute) when the AI isn't speaking so
    the carrier doesn't tear the call down for media starvation."""
    quiet_pcm = b"\x00\x00" * (SAMPLES_PER_FRAME * frames)
    return encode_from_pcm8k(quiet_pcm, pt)
