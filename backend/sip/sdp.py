"""Minimal SDP (RFC 4566) offer/answer for a single G.711 audio stream.

We only care about what an inbound PSTN INVITE actually carries: one `m=audio`
line, a connection address, a list of offered payload types, and (optionally) a
`telephone-event` type for RFC 2833 DTMF. We parse that, choose a codec by OUR
preference (PCMU → PCMA), and emit a well-formed answer advertising our RTP
ip:port + the single chosen codec.

Deliberately ignored in v1: multiple media sections, ICE, SRTP crypto, bandwidth
lines. A media-level `c=` overrides the session-level one (RFC 4566 §5.7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .g711 import NAME_TO_PT, PT_TO_NAME, PAYLOAD_PCMU, PAYLOAD_PCMA

# Our codec preference — first match against the offer wins.
CODEC_PREFERENCE = [PAYLOAD_PCMU, PAYLOAD_PCMA]


@dataclass
class SdpOffer:
    remote_ip: str
    remote_port: int
    payload_types: list[int]                    # offered, in the order the peer listed
    rtpmap: dict[int, str] = field(default_factory=dict)   # pt → "PCMU/8000"
    dtmf_pt: Optional[int] = None               # telephone-event payload type, if offered
    ptime: int = 20


def parse_offer(sdp: str) -> SdpOffer:
    session_ip: Optional[str] = None
    media_ip: Optional[str] = None
    port = 0
    pts: list[int] = []
    rtpmap: dict[int, str] = {}
    dtmf_pt: Optional[int] = None
    ptime = 20
    in_audio = False

    for raw in sdp.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or "=" not in line:
            continue
        typ, val = line[0], line[2:]
        if typ == "c" and val.startswith("IN IP4"):
            ip = val.split()[-1].split("/")[0]
            if in_audio:
                media_ip = ip
            else:
                session_ip = ip
        elif typ == "m":
            parts = val.split()
            in_audio = parts and parts[0] == "audio"
            if in_audio and len(parts) >= 4:
                port = int(parts[1])
                for tok in parts[3:]:
                    try:
                        pts.append(int(tok))
                    except ValueError:
                        pass
        elif typ == "a" and in_audio:
            if val.startswith("rtpmap:"):
                body = val[len("rtpmap:"):]
                num, _, enc = body.partition(" ")
                try:
                    pt = int(num)
                except ValueError:
                    continue
                rtpmap[pt] = enc.strip()
                if enc.strip().upper().startswith("TELEPHONE-EVENT"):
                    dtmf_pt = pt
            elif val.startswith("ptime:"):
                try:
                    ptime = int(val[len("ptime:"):])
                except ValueError:
                    pass

    remote_ip = media_ip or session_ip or ""
    if not remote_ip or not port or not pts:
        raise ValueError("SDP offer missing audio connection/port/codecs")
    return SdpOffer(remote_ip=remote_ip, remote_port=port, payload_types=pts,
                    rtpmap=rtpmap, dtmf_pt=dtmf_pt, ptime=ptime)


def choose_codec(offer: SdpOffer) -> int:
    """Return the RTP payload type we'll use, honouring OUR preference among the
    codecs the peer offered. Raises if no common G.711 codec exists."""
    offered = set(offer.payload_types)
    for pt in CODEC_PREFERENCE:
        if pt in offered:
            return pt
    # Some SDPs omit rtpmap for static types; fall back to the static PT set.
    for pt in CODEC_PREFERENCE:
        if pt in (PAYLOAD_PCMU, PAYLOAD_PCMA) and pt in offered:
            return pt
    raise ValueError(f"no common codec; peer offered {sorted(offered)}, "
                     f"we support {CODEC_PREFERENCE}")


def build_answer(*, local_ip: str, local_port: int, payload_type: int,
                 dtmf_pt: Optional[int] = None, session_id: int = 8000) -> str:
    """Build the answer SDP advertising our single chosen codec at local_ip:port.
    If the offer included telephone-event and we know its PT, echo it so DTMF
    (RFC 2833) keeps working end-to-end."""
    name = PT_TO_NAME.get(payload_type, "PCMU")
    fmt_list = str(payload_type) + (f" {dtmf_pt}" if dtmf_pt is not None else "")
    lines = [
        "v=0",
        f"o=spiderx {session_id} {session_id} IN IP4 {local_ip}",
        "s=SpiderX AI",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {local_port} RTP/AVP {fmt_list}",
        f"a=rtpmap:{payload_type} {name}/8000",
    ]
    if dtmf_pt is not None:
        lines.append(f"a=rtpmap:{dtmf_pt} telephone-event/8000")
        lines.append(f"a=fmtp:{dtmf_pt} 0-16")
    lines.append("a=ptime:20")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"
