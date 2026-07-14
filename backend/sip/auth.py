"""SIP digest authentication (RFC 3261 §22 / RFC 2617) for REGISTER — and,
optionally, for INVITE — when the operator prefers a credentialed trunk over
IP-peering.

Flow: an unauthenticated request gets a `401` (REGISTER) / `407` (proxy) with a
`WWW-Authenticate` challenge carrying a fresh nonce; the UA retries with an
`Authorization` header whose `response` we recompute and compare. Supports both
classic (no qop) and `qop=auth` (nc/cnonce) — Grandstream uses qop=auth.

We store nothing secret on the wire: the operator enters username/password into
the UCM trunk; here we hold the same password (or an HA1) to verify against.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Optional


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def parse_authorization(header: str) -> dict:
    """Parse a `Digest k=v, k="v", …` Authorization/Proxy-Authorization header
    into a dict. Tolerant of quoting and stray whitespace."""
    header = header.strip()
    if header[:7].lower() == "digest ":
        header = header[7:]
    out: dict[str, str] = {}
    # split on commas that separate params (values here never contain a comma)
    for part in header.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip().lower()] = v.strip().strip('"')
    return out


def make_challenge(realm: str, *, nonce: Optional[str] = None, qop: str = "auth",
                   proxy: bool = False) -> tuple[str, str, str]:
    """Return (nonce, header_name, header_value) for a 401/407 challenge."""
    nonce = nonce or (secrets.token_hex(16) + hex(int(time.time()))[2:])
    header_name = "Proxy-Authenticate" if proxy else "WWW-Authenticate"
    value = f'Digest realm="{realm}", nonce="{nonce}", qop="{qop}", algorithm=MD5'
    return nonce, header_name, value


def expected_response(*, username: str, password: str, realm: str, nonce: str,
                      method: str, uri: str, qop: Optional[str] = None,
                      nc: Optional[str] = None, cnonce: Optional[str] = None,
                      ha1: Optional[str] = None) -> str:
    """Compute the digest `response` the UA should have sent. Pass `ha1` to verify
    against a stored HA1 instead of a plaintext password."""
    ha1 = ha1 or _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def verify(auth_header: str, *, method: str, realm: str,
           password: Optional[str] = None, ha1: Optional[str] = None,
           expected_nonce: Optional[str] = None) -> tuple[bool, str]:
    """Verify an Authorization header. Returns (ok, username). `expected_nonce`,
    if given, must match (prevents replay with a stale/foreign nonce)."""
    p = parse_authorization(auth_header)
    username = p.get("username", "")
    nonce = p.get("nonce", "")
    uri = p.get("uri", "")
    resp = p.get("response", "")
    if not (username and nonce and uri and resp):
        return False, username
    if expected_nonce is not None and nonce != expected_nonce:
        return False, username
    want = expected_response(
        username=username, password=password or "", realm=realm, nonce=nonce,
        method=method, uri=uri, qop=p.get("qop"), nc=p.get("nc"),
        cnonce=p.get("cnonce"), ha1=ha1,
    )
    # constant-time compare
    return secrets.compare_digest(want, resp), username
