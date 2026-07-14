"""RTP (RFC 3550) packet parse/build + a minimal send/recv stream for G.711.

Scope is deliberately narrow — single dynamic-payload voice media, no CSRC mixing,
no SRTP (v1 is LAN/IP-peer). The 12-byte fixed header is all we emit; on receive we
skip any CSRC list and (if present) the RFC 3550 header extension so we hand the
raw codec payload up to the session.

Outbound timing/sequence is owned by `RtpSender`: G.711 is a constant 8 kHz /
20 ms / 160-sample cadence, so the timestamp just advances by SAMPLES_PER_FRAME
per packet and the sequence number by 1 — the receiver reconstructs order/timing
from these, which is why they must be monotonic and correct.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .g711 import SAMPLES_PER_FRAME

RTP_VERSION = 2
_HDR = struct.Struct("!BBHII")   # V/P/X/CC, M/PT, seq, timestamp, ssrc


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False

    def build(self) -> bytes:
        b0 = (RTP_VERSION << 6)                       # V=2, P=0, X=0, CC=0
        b1 = ((0x80 if self.marker else 0) | (self.payload_type & 0x7F))
        return _HDR.pack(b0, b1, self.sequence & 0xFFFF,
                         self.timestamp & 0xFFFFFFFF, self.ssrc & 0xFFFFFFFF) + self.payload

    @classmethod
    def parse(cls, data: bytes) -> "RtpPacket":
        if len(data) < _HDR.size:
            raise ValueError("short RTP packet")
        b0, b1, seq, ts, ssrc = _HDR.unpack_from(data, 0)
        if (b0 >> 6) != RTP_VERSION:
            raise ValueError("not RTPv2")
        cc = b0 & 0x0F
        has_ext = bool(b0 & 0x10)
        marker = bool(b1 & 0x80)
        pt = b1 & 0x7F
        offset = _HDR.size + cc * 4                    # skip CSRC identifiers
        if has_ext:
            if len(data) < offset + 4:
                raise ValueError("truncated RTP extension")
            ext_words = struct.unpack_from("!H", data, offset + 2)[0]
            offset += 4 + ext_words * 4                # skip profile-ext header + body
        payload = data[offset:]
        return cls(payload_type=pt, sequence=seq, timestamp=ts, ssrc=ssrc,
                   payload=payload, marker=marker)


class RtpSender:
    """Stamps monotonic sequence/timestamp onto outbound G.711 frames for one call.

    `first_seq`/`first_ts`/`ssrc` default to fixed values so unit tests are
    deterministic; the live session seeds them with randomised values (recommended
    by RFC 3550 to avoid cross-session collisions) via the constructor.
    """
    def __init__(self, payload_type: int, ssrc: int = 0x11223344,
                 first_seq: int = 0, first_ts: int = 0):
        self.payload_type = payload_type
        self.ssrc = ssrc & 0xFFFFFFFF
        self._seq = first_seq & 0xFFFF
        self._ts = first_ts & 0xFFFFFFFF
        self._started = False

    def next_packet(self, g711_payload: bytes) -> RtpPacket:
        # Marker bit set on the very first packet of a talk-spurt / stream start.
        marker = not self._started
        self._started = True
        pkt = RtpPacket(payload_type=self.payload_type, sequence=self._seq,
                        timestamp=self._ts, ssrc=self.ssrc, payload=g711_payload,
                        marker=marker)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + SAMPLES_PER_FRAME) & 0xFFFFFFFF
        return pkt

    def mark_gap(self):
        """Call after a period of not sending (e.g. AI was silent and we skipped
        packets) so the NEXT packet re-asserts the marker bit — signals a new
        talk-spurt to the far end's jitter buffer."""
        self._started = False
