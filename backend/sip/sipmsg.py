"""SIP (RFC 3261) message parse + response build — only what a UAS needs.

We are a User Agent Server: we RECEIVE requests (INVITE / ACK / BYE / CANCEL /
OPTIONS / REGISTER) and SEND responses. So parsing is general but building is
response-only. The rules a UAS must not get wrong, and that we handle here:

  • Echo Via (ALL of them, in order), From, Call-ID, CSeq verbatim.
  • Add a tag to the To header on any dialog-creating/‑confirming response
    (we generate one and keep it stable for the dialog).
  • 200 OK to INVITE carries a Contact (where re-INVITE/BYE should target us)
    and the SDP answer with correct Content-Type/Content-Length.

Compact header forms (RFC 3261 §7.3.3) are normalised on parse so callers can
always look up the long name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# compact form → canonical long name (lower-cased keys)
_COMPACT = {
    "v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
    "l": "content-length", "c": "content-type", "s": "subject",
    "k": "supported", "e": "content-encoding", "o": "event",
}


def _canon(name: str) -> str:
    n = name.strip().lower()
    return _COMPACT.get(n, n)


@dataclass
class SipMessage:
    is_request: bool
    method: str = ""            # requests
    request_uri: str = ""       # requests
    status: int = 0             # responses
    reason: str = ""            # responses
    # headers preserved in order, canonical-lower name → list of raw values
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    # ── header access ──────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[str]:
        name = _canon(name)
        for k, v in self.headers:
            if k == name:
                return v
        return None

    def get_all(self, name: str) -> list[str]:
        name = _canon(name)
        return [v for k, v in self.headers if k == name]

    # ── dialog identity helpers ────────────────────────────────────────────
    @property
    def call_id(self) -> str:
        return (self.get("call-id") or "").strip()

    @property
    def cseq_number(self) -> int:
        cseq = self.get("cseq") or ""
        try:
            return int(cseq.split()[0])
        except (ValueError, IndexError):
            return 0

    def branch(self) -> str:
        """The top Via's branch parameter — the transaction key."""
        via = self.get("via") or ""
        for param in via.split(";"):
            p = param.strip()
            if p.lower().startswith("branch="):
                return p[len("branch="):]
        return ""


def parse(data: bytes) -> SipMessage:
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    if not head:
        head, _, body = text.partition("\n\n")
    lines = head.replace("\r\n", "\n").split("\n")
    start = lines[0].split(" ", 2)
    if len(start) < 3:
        raise ValueError("malformed SIP start line")

    if start[0].upper().startswith("SIP/"):
        msg = SipMessage(is_request=False, status=int(start[1]), reason=start[2])
    else:
        msg = SipMessage(is_request=True, method=start[0].upper(), request_uri=start[1])

    # header folding: a line starting with WS continues the previous header
    cur_name = None
    cur_val: list[str] = []

    def flush():
        nonlocal cur_name, cur_val
        if cur_name is not None:
            msg.headers.append((cur_name, " ".join(cur_val).strip()))
        cur_name, cur_val = None, []

    for line in lines[1:]:
        if not line:
            continue
        if line[0] in (" ", "\t") and cur_name is not None:
            cur_val.append(line.strip())
            continue
        flush()
        name, _, val = line.partition(":")
        cur_name = _canon(name)
        cur_val = [val.strip()]
    flush()

    msg.body = body.encode("utf-8", errors="replace")
    return msg


# ── response building ──────────────────────────────────────────────────────
_REASON = {
    100: "Trying", 180: "Ringing", 200: "OK", 400: "Bad Request",
    401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 407: "Proxy Authentication Required",
    408: "Request Timeout", 480: "Temporarily Unavailable",
    486: "Busy Here", 487: "Request Terminated", 488: "Not Acceptable Here",
    500: "Server Internal Error", 503: "Service Unavailable",
}


def build_response(request: SipMessage, status: int, *, reason: Optional[str] = None,
                   to_tag: Optional[str] = None, contact: Optional[str] = None,
                   extra_headers: Optional[list[tuple[str, str]]] = None,
                   body: bytes = b"", content_type: str = "application/sdp") -> bytes:
    """Build a UAS response to `request`. Echoes Via/From/Call-ID/CSeq, appends a
    To-tag (for dialog responses), sets Content-Length, and adds Contact + body
    when supplied (200 OK to INVITE)."""
    reason = reason or _REASON.get(status, "Unknown")
    out = [f"SIP/2.0 {status} {reason}"]

    # Via: echo every Via header, in order (RFC 3261 §8.2.6.2).
    for v in request.get_all("via"):
        out.append(f"Via: {v}")
    out.append(f"From: {request.get('from') or ''}")

    to = request.get("to") or ""
    if to_tag and ";tag=" not in to.lower():
        to = f"{to};tag={to_tag}"
    out.append(f"To: {to}")

    out.append(f"Call-ID: {request.call_id}")
    out.append(f"CSeq: {request.get('cseq') or ''}")
    if contact:
        out.append(f"Contact: <{contact}>")
    for name, val in (extra_headers or []):
        out.append(f"{name}: {val}")
    if body:
        out.append(f"Content-Type: {content_type}")
    out.append(f"Content-Length: {len(body)}")
    out.append("")
    head = "\r\n".join(out) + "\r\n"
    return head.encode("utf-8") + body
