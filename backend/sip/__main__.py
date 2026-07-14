"""Run the SIP UAS: `python -m backend.sip`.

Deploy this on a host the UCM can reach — for the first test, a box on the UCM's
LAN subnet (IP-peer, no NAT). It is a SEPARATE process from the web app (Railway
can't expose SIP/RTP), so it has its own lifecycle.

Configure via env:
  SIP_LOCAL_IP        (required) the IP the UCM reaches us on — goes in SDP c= / Contact
  SIP_PORT            default 5060
  SIP_RTP_START/END   RTP UDP port range, default 40000–40100
  SIP_ALLOWED_PEERS   comma-sep source-IP allowlist (e.g. the UCM/Tata IPs). empty = any
  SIP_DEFAULT_AGENT   agent id to route to when the URI has no `agent-<id>` user-part
                      (set this — your one DID maps to one agent)
  SIP_REALM           digest realm, default spiderx.ai
  SIP_AUTH_USER/PASS  enable credentialed trunk (what you'd type into the UCM)
  SIP_AUTH_CALLS      "1" to require digest on INVITE too (else IP-peer for calls)

Example (LAN, IP-peer, one DID → agent 5):
  SIP_LOCAL_IP=10.79.217.50 SIP_ALLOWED_PEERS=10.79.217.132 SIP_DEFAULT_AGENT=5 \
      python -m backend.sip
"""
import asyncio
import logging
import os

from .server import SipServer, SipConfig
from .gemini_handler import gemini_factory


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


async def _main():
    local_ip = os.environ.get("SIP_LOCAL_IP", "").strip()
    if not local_ip:
        raise SystemExit("SIP_LOCAL_IP is required (the IP the UCM reaches us on)")

    peers = {p.strip() for p in os.environ.get("SIP_ALLOWED_PEERS", "").split(",") if p.strip()}
    user = os.environ.get("SIP_AUTH_USER", "").strip()
    pw = os.environ.get("SIP_AUTH_PASS", "").strip()
    default_agent = _env_int("SIP_DEFAULT_AGENT", 0) or None

    # No fixed agent → self-serve/multi-tenant: resolve each call to an agent from
    # the DB directory (what the dashboard "Connect your phone system" card writes).
    # With SIP_DEFAULT_AGENT set → static single-agent mode (env auth, no DB).
    directory = None
    if default_agent is None:
        from . import directory as directory  # noqa: PLW0127 — module used as the directory object

    cfg = SipConfig(
        local_ip=local_ip,
        sip_port=_env_int("SIP_PORT", 5060),
        rtp_ports=range(_env_int("SIP_RTP_START", 40000), _env_int("SIP_RTP_END", 40100)),
        allowed_peers=(peers or None),
        realm=os.environ.get("SIP_REALM", "spiderx.ai"),
        credentials=({user: pw} if user and pw else None),
        auth_calls=os.environ.get("SIP_AUTH_CALLS", "") == "1",
        directory=directory,
    )
    server = await SipServer.start(cfg, gemini_factory(default_agent_id=default_agent))
    logging.info("sipd ready (%s) — point trunks at %s:%s",
                 "self-serve · DB directory" if directory else f"static agent {default_agent}",
                 local_ip, cfg.sip_port)
    try:
        await asyncio.Event().wait()          # run until killed
    finally:
        await server.stop()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("SIP_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
