"""Multi-tenant directory routing (run: python -m backend.sip.tests_directory).

Proves the DB-driven self-serve path WITHOUT a database, using a fake directory:
  • a call to a saved DID resolves to that agent and is answered
  • an unknown DID → 404
  • a call from an IP outside the agent's allowlist → 403
Plus the pure DID-matching helpers.
"""
import asyncio
import secrets

from . import sipmsg
from .directory import ResolvedAgent, normalize_did, did_matches
from .media import EchoHandler
from .server import SipServer, SipConfig
from .tests_loopback import _Collector, _invite, _offer

IP = "127.0.0.1"
PORT = 55080


def check(name, cond):
    if not cond:
        raise AssertionError("FAIL: " + name)
    print("  ok:", name)


def test_matching():
    print("DID matching")
    check("normalize strips +/spaces/dashes", normalize_did("+91 336-543 0101") == "913365430101")
    check("drops 00 intl prefix", normalize_did("0091 3365430101") == "913365430101")
    check("exact match", did_matches("+913365430101", "913365430101"))
    check("national 10-digit suffix match", did_matches("+913365430101", "3365430101"))
    check("different numbers don't match", not did_matches("+913365430101", "+913365430102"))


class FakeDirectory:
    def __init__(self, by_did):
        self._by_did = by_did

    async def resolve_by_did(self, did):
        for k, v in self._by_did.items():
            if did_matches(k, did):
                return v
        return None

    async def resolve_by_username(self, username):
        return None


async def _invite_status(sip_tr, proto, server_addr, *, ruri, rtp_port):
    call_id = f"{secrets.token_hex(6)}@{IP}"
    port = sip_tr.get_extra_info("sockname")[1]
    sip_tr.sendto(_invite(ruri=ruri, via_host=IP, via_port=port, call_id=call_id,
                          from_tag=secrets.token_hex(4), sdp=_offer(IP, rtp_port)), server_addr)
    final = None
    for _ in range(4):
        data, _ = await asyncio.wait_for(proto.q.get(), timeout=3)
        r = sipmsg.parse(data)
        if r.status >= 200:
            final = r
            break
    return final.status if final else None


async def main():
    test_matching()
    print("directory routing")
    loop = asyncio.get_event_loop()
    seen = {}
    directory = FakeDirectory({
        "+913365430101": ResolvedAgent(agent_id=1, allowed_ips=["127.0.0.1"]),
        "+913365430102": ResolvedAgent(agent_id=2, allowed_ips=["10.0.0.99"]),  # not us
    })

    def factory(agent_id, invite, session):
        seen["agent_id"] = agent_id
        return EchoHandler()

    server = await SipServer.start(
        SipConfig(local_ip=IP, sip_port=PORT, rtp_ports=range(41100, 41110), directory=directory),
        handler_factory=factory)
    sip_tr, proto = await loop.create_datagram_endpoint(_Collector, local_addr=(IP, 0))
    rtp_tr, _ = await loop.create_datagram_endpoint(_Collector, local_addr=(IP, 0))
    rtp_port = rtp_tr.get_extra_info("sockname")[1]
    server_addr = (IP, PORT)

    # saved DID, from an allowed IP → answered, routed to agent 1
    s1 = await _invite_status(sip_tr, proto, server_addr,
                              ruri=f"sip:+913365430101@{IP}:{PORT}", rtp_port=rtp_port)
    check("saved DID from allowed IP → 200", s1 == 200)
    check("routed to the DID's agent (id=1)", seen.get("agent_id") == 1)

    # unknown DID → 404
    s2 = await _invite_status(sip_tr, proto, server_addr,
                              ruri=f"sip:+919999999999@{IP}:{PORT}", rtp_port=rtp_port)
    check("unknown DID → 404", s2 == 404)

    # known DID but our IP isn't in that agent's allowlist → 403
    s3 = await _invite_status(sip_tr, proto, server_addr,
                              ruri=f"sip:+913365430102@{IP}:{PORT}", rtp_port=rtp_port)
    check("DID from non-allowlisted IP → 403", s3 == 403)

    sip_tr.close(); rtp_tr.close()
    await server.stop()
    print("\nDIRECTORY ROUTING PASSED ✅  (DID→agent + per-agent IP auth)")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
