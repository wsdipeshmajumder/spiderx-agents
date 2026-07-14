"""RTP media session for one call + a pluggable media handler.

The session speaks pure G.711 8 kHz PCM on its edges: inbound RTP is decoded to
PCM8k and pushed to `inbound_q`; whatever PCM8k the handler puts on `outbound_q`
is encoded and paced back out at a strict 20 ms cadence. Handlers do their own
rate conversion (Gemini wants 16 kHz in / 24 kHz out) — the session stays codec-
only so it's trivial to reason about and to loopback-test.

Two real-world behaviours that matter even on a LAN:
  • Symmetric-RTP latching — we send to wherever the peer's media ACTUALLY comes
    from (learned from the first inbound packet), not just the SDP-advertised
    address. Grandstream/Tata and any NAT in the path make the two differ.
  • Continuous egress — when the handler has nothing to say we still emit G.711
    silence so the carrier doesn't tear the call down for media starvation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import g711
from .rtp import RtpPacket, RtpSender

log = logging.getLogger("sip.media")

_FRAME_SEC = g711.FRAME_MS / 1000.0        # 0.020
_Q_MAX = 100                                # ~2s of frames; drop beyond (live audio)


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, session: "MediaSession"):
        self.session = session

    def datagram_received(self, data: bytes, addr):
        self.session._on_rtp(data, addr)


class MediaSession:
    def __init__(self, *, local_ip: str, remote_ip: str, remote_port: int,
                 payload_type: int, ssrc: int = 0x1A2B3C4D):
        self.local_ip = local_ip
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.payload_type = payload_type
        self.local_port = 0                 # filled after bind
        self.inbound_q: asyncio.Queue = asyncio.Queue(maxsize=_Q_MAX)
        self.outbound_q: asyncio.Queue = asyncio.Queue(maxsize=_Q_MAX)
        self._sender = RtpSender(payload_type, ssrc=ssrc)
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._send_task: Optional[asyncio.Task] = None
        self._latched = False
        self._closed = False
        self._rx = 0
        self._tx = 0

    async def start(self, port_range: range = range(40000, 40100)) -> int:
        """Bind an RTP UDP socket (first free port in range) and start the send
        pacer. Returns the bound local port to advertise in the SDP answer."""
        loop = asyncio.get_event_loop()
        last_err = None
        for port in port_range:
            try:
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _RtpProtocol(self), local_addr=(self.local_ip, port))
                self.local_port = port
                break
            except OSError as e:
                last_err = e
                continue
        if not self._transport:
            raise RuntimeError(f"no free RTP port in {port_range}: {last_err}")
        self._send_task = asyncio.ensure_future(self._send_loop())
        log.info("media: bound RTP %s:%s → peer %s:%s pt=%s",
                 self.local_ip, self.local_port, self.remote_ip, self.remote_port, self.payload_type)
        return self.local_port

    def _on_rtp(self, data: bytes, addr):
        # Latch: trust the source of real media over the SDP address (NAT / port
        # split). Only latch to the peer we expect by IP unless nothing latched yet.
        if not self._latched:
            self.remote_ip, self.remote_port = addr
            self._latched = True
            log.info("media: latched egress to %s:%s", *addr)
        try:
            pkt = RtpPacket.parse(data)
        except ValueError:
            return
        if pkt.payload_type not in (g711.PAYLOAD_PCMU, g711.PAYLOAD_PCMA):
            return                          # ignore DTMF/other for now (M5 hardening)
        try:
            pcm8k = g711.decode_to_pcm8k(pkt.payload, pkt.payload_type)
        except ValueError:
            return
        self._rx += 1
        try:
            self.inbound_q.put_nowait(pcm8k)
        except asyncio.QueueFull:
            _drop(self.inbound_q)           # keep the freshest audio (low latency)
            try:
                self.inbound_q.put_nowait(pcm8k)
            except asyncio.QueueFull:
                pass

    async def _send_loop(self):
        """Emit one RTP frame every 20 ms — the handler's audio if available, else
        silence — with monotonic drift correction so we don't skew over a long call."""
        next_t = time.monotonic()
        while not self._closed:
            next_t += _FRAME_SEC
            try:
                pcm8k = self.outbound_q.get_nowait()
                spoke = True
            except asyncio.QueueEmpty:
                pcm8k = b"\x00\x00" * g711.SAMPLES_PER_FRAME
                spoke = False
            # normalise to exactly one frame worth of samples
            want = g711.FRAME_BYTES
            payload = g711.encode_from_pcm8k(pcm8k, self.payload_type)
            if len(payload) != want:
                payload = (payload + g711.silence(self.payload_type))[:want]
            if not spoke:
                self._sender.mark_gap()
            pkt = self._sender.next_packet(payload)
            if self._transport and (self.remote_ip and self.remote_port):
                try:
                    self._transport.sendto(pkt.build(), (self.remote_ip, self.remote_port))
                    self._tx += 1
                except OSError:
                    pass
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()   # we fell behind; resync rather than burst

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._send_task:
            self._send_task.cancel()
        if self._transport:
            self._transport.close()
        log.info("media: closed (rx=%d tx=%d)", self._rx, self._tx)


def _drop(q: asyncio.Queue):
    try:
        q.get_nowait()
    except asyncio.QueueEmpty:
        pass


class MediaHandler:
    """Consumes `session.inbound_q` (PCM8k) and produces onto `session.outbound_q`
    (PCM8k). Subclass `run` for real behaviour. The session owns the RTP; the
    handler owns the 'what to say'."""
    async def run(self, session: MediaSession):
        raise NotImplementedError


class EchoHandler(MediaHandler):
    """Loopback: send the caller's audio straight back. Proves the SIP+RTP path
    end-to-end without Gemini (used by the local integration test)."""
    async def run(self, session: MediaSession):
        while True:
            frame = await session.inbound_q.get()
            try:
                session.outbound_q.put_nowait(frame)
            except asyncio.QueueFull:
                pass
