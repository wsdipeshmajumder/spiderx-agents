"""SIP UAS — accepts inbound INVITEs from a trunk (e.g. the Grandstream UCM) and
bridges the call's audio to a pluggable MediaHandler (Gemini in production, Echo
in the loopback test).

Transaction scope is what a UAS terminating calls needs, not a full stack:
  INVITE  → 100 Trying → 180 Ringing → 200 OK (SDP answer + Contact)  [→ ACK]
  BYE     → 200 OK, tear down media
  CANCEL  → 200 OK (+ 487 to the pending INVITE)
  OPTIONS → 200 OK (keepalive / reachability probe)
  REGISTER→ delegated to `auth` (M4); 200 or 401 challenge

Auth model: IP-peer by default (a source-IP allowlist — matches the UCM's
IP-authenticated SIP-peer trunk). Registration/digest is layered on in M4.

NOT yet (M5 hardening, called out so nothing is silently missing): INVITE 200-OK
retransmission until ACK, re-INVITE/hold, TLS/SRTP, DTMF (RFC 2833) events, and
public-NAT rport/STUN. v1 targets a LAN IP-peer path where these rarely bite.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import sipmsg
from . import auth as sip_auth
from . import sdp as sdp_mod
from .media import MediaHandler, MediaSession

log = logging.getLogger("sip.server")

_RURI_USER = re.compile(r"sip:([^@;>]+)@")


def _new_tag() -> str:
    return secrets.token_hex(6)


@dataclass
class Dialog:
    call_id: str
    to_tag: str
    media: Optional[MediaSession] = None
    handler_task: Optional[asyncio.Task] = None
    confirmed: bool = False
    peer: tuple = ()                       # signalling source (ip, port)


# handler_factory(agent_id, invite, session) -> MediaHandler|None
# agent_id is the DB-resolved agent when a directory is configured, else None
# (the factory then resolves from the request-URI / env default).
HandlerFactory = Callable[[Optional[int], sipmsg.SipMessage, MediaSession], Optional[MediaHandler]]


@dataclass
class SipConfig:
    local_ip: str                          # the IP the trunk reaches us on (SDP c=/Contact)
    sip_port: int = 5060
    rtp_ports: range = field(default_factory=lambda: range(40000, 40100))
    allowed_peers: Optional[set] = None    # source-IP allowlist for IP-peer auth; None = allow any
    user_agent: str = "SpiderX-SIP/0.1"
    # Digest auth (M4): credentials the operator enters into the UCM trunk. When
    # set, REGISTER is always challenged; INVITE is challenged only if auth_calls.
    realm: str = "spiderx.ai"
    credentials: Optional[dict] = None     # username → password
    auth_calls: bool = False               # require digest on INVITE (else IP-peer)
    # Multi-tenant self-serve: an object exposing async resolve_by_username(u) and
    # resolve_by_did(d) → ResolvedAgent|None (see sip/directory.py). When set, each
    # call is matched to an agent + its own auth policy from the DB, and the global
    # allowed_peers/credentials above are ignored.
    directory: Optional[object] = None


def _ip_allowed(ip: str, allowed: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed or []:
        try:
            if addr in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            continue
    return False


class SipServer(asyncio.DatagramProtocol):
    def __init__(self, config: SipConfig, handler_factory: HandlerFactory):
        self.cfg = config
        self.handler_factory = handler_factory
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.dialogs: dict[str, Dialog] = {}
        self._nonces: set = set()          # nonces we've issued (replay guard)

    # ── lifecycle ──────────────────────────────────────────────────────────
    @classmethod
    async def start(cls, config: SipConfig, handler_factory: HandlerFactory) -> "SipServer":
        loop = asyncio.get_event_loop()
        self = cls(config, handler_factory)
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(config.local_ip, config.sip_port))
        log.info("SIP UAS listening on %s:%s (peers=%s)",
                 config.local_ip, config.sip_port,
                 "any" if config.allowed_peers is None else sorted(config.allowed_peers))
        return self

    async def stop(self):
        for d in list(self.dialogs.values()):
            await self._teardown(d)
        if self.transport:
            self.transport.close()

    # ── datagram dispatch ──────────────────────────────────────────────────
    def datagram_received(self, data: bytes, addr):
        try:
            msg = sipmsg.parse(data)
        except ValueError as e:
            log.warning("bad SIP datagram from %s: %s", addr, e)
            return
        if not msg.is_request:
            return                          # UAS: we don't act on responses in v1
        method = msg.method
        if method == "INVITE":
            asyncio.ensure_future(self._on_invite(msg, addr))
        elif method == "ACK":
            self._on_ack(msg, addr)
        elif method == "BYE":
            asyncio.ensure_future(self._on_bye(msg, addr))
        elif method == "CANCEL":
            asyncio.ensure_future(self._on_cancel(msg, addr))
        elif method == "OPTIONS":
            self._reply(msg, 200)
        elif method == "REGISTER":
            asyncio.ensure_future(self._on_register(msg, addr))
        else:
            self._reply(msg, 405, extra=[("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS")])

    # ── helpers ────────────────────────────────────────────────────────────
    def _send(self, data: bytes, addr):
        if self.transport:
            self.transport.sendto(data, addr)

    def _reply(self, req: sipmsg.SipMessage, status: int, *, to_tag: Optional[str] = None,
               contact: Optional[str] = None, body: bytes = b"",
               extra: Optional[list] = None, addr=None):
        resp = sipmsg.build_response(req, status, to_tag=to_tag, contact=contact,
                                     body=body, extra_headers=extra)
        self._send(resp, addr or self._reply_addr(req))

    def _reply_addr(self, req: sipmsg.SipMessage) -> tuple:
        """Where to send a response: honour the top Via's received/rport when the
        peer NATs, else the Via host:port. v1 keeps it simple — the caller passes
        the real source addr, which is correct for symmetric UDP."""
        return self._last_addr

    def _peer_allowed(self, addr) -> bool:
        if self.cfg.allowed_peers is None:
            return True
        return addr[0] in self.cfg.allowed_peers

    def _challenge(self, req: sipmsg.SipMessage, addr, *, proxy: bool):
        """Send a 401/407 with a fresh digest nonce and remember it."""
        nonce, hname, hval = sip_auth.make_challenge(self.cfg.realm, proxy=proxy)
        self._nonces.add(nonce)
        if len(self._nonces) > 4096:                     # bound the replay set
            self._nonces = set(list(self._nonces)[-2048:])
        self._reply(req, 407 if proxy else 401, extra=[(hname, hval)], addr=addr)

    def _authenticated(self, req: sipmsg.SipMessage) -> bool:
        """True if the request carries a valid Authorization for a nonce we issued.
        `credentials` maps username→password; the request's username must be known."""
        hdr = req.get("authorization") or req.get("proxy-authorization")
        if not hdr:
            return False
        params = sip_auth.parse_authorization(hdr)
        user = params.get("username", "")
        creds = self.cfg.credentials or {}
        if user not in creds or params.get("nonce", "") not in self._nonces:
            return False
        ok, _ = sip_auth.verify(hdr, method=req.method, realm=self.cfg.realm,
                                password=creds[user], expected_nonce=params.get("nonce"))
        return ok

    def _contact_uri(self, req: sipmsg.SipMessage) -> str:
        m = _RURI_USER.search(req.request_uri or "")
        user = m.group(1) if m else "ai"
        return f"sip:{user}@{self.cfg.local_ip}:{self.cfg.sip_port}"

    def _dialed_did(self, req: sipmsg.SipMessage) -> str:
        """The number the caller dialled — the request-URI user-part, else the
        To header's user-part. sipd matches this against each agent's saved DID."""
        for src in (req.request_uri or "", req.get("to") or ""):
            m = _RURI_USER.search(src)
            if m:
                return m.group(1)
        return ""

    async def _resolve_and_auth(self, msg: sipmsg.SipMessage, addr):
        """Multi-tenant path: map the INVITE to an agent from the DB directory and
        enforce THAT agent's own auth policy. Returns a ResolvedAgent or None (and
        sends the appropriate 401/403/404 itself)."""
        d = self.cfg.directory
        authz = msg.get("authorization") or msg.get("proxy-authorization")
        if authz:                                        # credentialed trunk
            params = sip_auth.parse_authorization(authz)
            ra = await d.resolve_by_username(params.get("username", ""))
            if not ra or not ra.trunk_password:
                self._reply(msg, 403, addr=addr); return None
            if params.get("nonce", "") not in self._nonces:
                self._challenge(msg, addr, proxy=False); return None
            ok, _ = sip_auth.verify(authz, method=msg.method, realm=self.cfg.realm,
                                    password=ra.trunk_password, expected_nonce=params.get("nonce"))
            if not ok:
                self._challenge(msg, addr, proxy=False); return None
            return ra
        # unauthenticated → match by dialled DID, then IP-peer check
        ra = await d.resolve_by_did(self._dialed_did(msg))
        if not ra:
            self._reply(msg, 404, addr=addr); return None
        if ra.trunk_username and not ra.allowed_ips:     # this agent uses credentials
            self._challenge(msg, addr, proxy=False); return None
        if ra.allowed_ips and not _ip_allowed(addr[0], ra.allowed_ips):
            log.warning("INVITE %s from %s not in agent %s allowlist → 403", msg.call_id, addr[0], ra.agent_id)
            self._reply(msg, 403, addr=addr); return None
        return ra

    # ── method handlers ────────────────────────────────────────────────────
    async def _on_invite(self, msg: sipmsg.SipMessage, addr):
        self._last_addr = addr
        if self.cfg.directory is not None:
            resolved = await self._resolve_and_auth(msg, addr)
            if resolved is None:
                return                                    # error response already sent
            agent_id = resolved.agent_id
        else:
            if not self._peer_allowed(addr):
                log.warning("INVITE from disallowed peer %s → 403", addr)
                self._reply(msg, 403, addr=addr)
                return
            if self.cfg.auth_calls and self.cfg.credentials and not self._authenticated(msg):
                log.info("INVITE %s unauthenticated → 401 challenge", msg.call_id)
                self._challenge(msg, addr, proxy=False)
                return
            agent_id = None                               # factory resolves from URI/env default

        # Re-INVITE for an existing dialog (hold/resume/renegotiate) — v1 just
        # re-200s with the same media; full renegotiation is M5.
        existing = self.dialogs.get(msg.call_id)

        try:
            offer = sdp_mod.parse_offer(msg.body.decode("utf-8", "replace"))
            pt = sdp_mod.choose_codec(offer)
        except ValueError as e:
            log.warning("INVITE unusable SDP from %s: %s", addr, e)
            self._reply(msg, 488, addr=addr)      # Not Acceptable Here
            return

        self._reply(msg, 100, addr=addr)          # Trying
        self._reply(msg, 180, addr=addr)          # Ringing

        if existing and existing.media:
            dlg = existing
            session = existing.media
        else:
            session = MediaSession(local_ip=self.cfg.local_ip,
                                   remote_ip=offer.remote_ip, remote_port=offer.remote_port,
                                   payload_type=pt)
            try:
                await session.start(self.cfg.rtp_ports)
            except RuntimeError as e:
                log.error("no RTP port for call %s: %s", msg.call_id, e)
                self._reply(msg, 503, addr=addr)
                return
            handler = self.handler_factory(agent_id, msg, session)
            if handler is None:                       # unresolvable agent / DID
                await session.close()
                self._reply(msg, 404, addr=addr)
                return
            dlg = Dialog(call_id=msg.call_id, to_tag=_new_tag(), media=session, peer=addr)
            self.dialogs[msg.call_id] = dlg
            dlg.handler_task = asyncio.ensure_future(self._run_handler(handler, session, dlg))

        answer = sdp_mod.build_answer(local_ip=self.cfg.local_ip, local_port=session.local_port,
                                      payload_type=pt, dtmf_pt=offer.dtmf_pt)
        self._reply(msg, 200, to_tag=dlg.to_tag, contact=self._contact_uri(msg),
                    body=answer.encode("utf-8"), addr=addr)
        log.info("INVITE %s answered (pt=%s, rtp=%s)", msg.call_id, pt, session.local_port)

    async def _run_handler(self, handler: MediaHandler, session: MediaSession, dlg: Dialog):
        try:
            await handler.run(session)
        except asyncio.CancelledError:
            pass
        except Exception:                          # a handler crash must end the call cleanly
            log.exception("media handler for %s crashed", dlg.call_id)

    async def _on_register(self, msg: sipmsg.SipMessage, addr):
        self._last_addr = addr
        authz = msg.get("authorization") or msg.get("proxy-authorization")
        if self.cfg.directory is not None:
            if not authz:
                self._challenge(msg, addr, proxy=False); return
            params = sip_auth.parse_authorization(authz)
            ra = await self.cfg.directory.resolve_by_username(params.get("username", ""))
            if (not ra or not ra.trunk_password
                    or params.get("nonce", "") not in self._nonces):
                self._challenge(msg, addr, proxy=False); return
            ok, _ = sip_auth.verify(authz, method="REGISTER", realm=self.cfg.realm,
                                    password=ra.trunk_password, expected_nonce=params.get("nonce"))
            if ok:
                self._reply(msg, 200, extra=[("Expires", msg.get("expires") or "3600")], addr=addr)
                log.info("REGISTER ok: agent=%s from %s", ra.agent_id, addr)
            else:
                self._challenge(msg, addr, proxy=False)
            return
        # legacy global mode
        if not self.cfg.credentials:
            self._reply(msg, 200, addr=addr)             # IP-peer: accept probes
        elif self._authenticated(msg):
            self._reply(msg, 200, extra=[("Expires", msg.get("expires") or "3600")], addr=addr)
            log.info("REGISTER ok from %s", addr)
        else:
            self._challenge(msg, addr, proxy=False)

    def _on_ack(self, msg: sipmsg.SipMessage, addr):
        self._last_addr = addr
        dlg = self.dialogs.get(msg.call_id)
        if dlg:
            dlg.confirmed = True
            log.info("ACK %s — dialog confirmed", msg.call_id)

    async def _on_bye(self, msg: sipmsg.SipMessage, addr):
        self._last_addr = addr
        dlg = self.dialogs.pop(msg.call_id, None)
        self._reply(msg, 200, addr=addr)
        if dlg:
            await self._teardown(dlg)
            log.info("BYE %s — call ended", msg.call_id)

    async def _on_cancel(self, msg: sipmsg.SipMessage, addr):
        self._last_addr = addr
        self._reply(msg, 200, addr=addr)          # 200 to the CANCEL itself
        dlg = self.dialogs.pop(msg.call_id, None)
        if dlg and not dlg.confirmed:
            self._reply(msg, 487, addr=addr)      # Request Terminated for the INVITE
            await self._teardown(dlg)
            log.info("CANCEL %s — pending call aborted", msg.call_id)

    async def _teardown(self, dlg: Dialog):
        if dlg.handler_task:
            dlg.handler_task.cancel()
        if dlg.media:
            await dlg.media.close()
