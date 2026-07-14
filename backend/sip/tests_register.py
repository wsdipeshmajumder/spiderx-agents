"""REGISTER digest flow through the real UAS (run: python -m backend.sip.tests_register).

Proves credentialed-trunk auth over the wire:
  REGISTER (no auth) → 401 + nonce → REGISTER (Authorization) → 200
  REGISTER (wrong password) → 401
"""
import asyncio
import secrets

from . import auth, sipmsg
from .server import SipServer, SipConfig

IP = "127.0.0.1"
PORT = 55070
USER, PW, REALM = "tata_ai", "s3cr3t-pass", "spiderx.ai"


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.q.put_nowait((data, addr))


def _register(via_port, call_id, cseq, *, authz=None):
    ruri = f"sip:{IP}:{PORT}"
    lines = [
        f"REGISTER {ruri} SIP/2.0",
        f"Via: SIP/2.0/UDP {IP}:{via_port};branch=z9hG4bK{secrets.token_hex(4)}",
        "Max-Forwards: 70",
        f"From: <sip:{USER}@{REALM}>;tag={secrets.token_hex(4)}",
        f"To: <sip:{USER}@{REALM}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        f"Contact: <sip:{USER}@{IP}:{via_port}>",
        "Expires: 3600",
    ]
    if authz:
        lines.append(f"Authorization: {authz}")
    lines += ["Content-Length: 0", "", ""]
    return "\r\n".join(lines).encode()


def _authz(nonce, *, password):
    ruri = f"sip:{IP}:{PORT}"
    resp = auth.expected_response(username=USER, password=password, realm=REALM,
                                  nonce=nonce, method="REGISTER", uri=ruri,
                                  qop="auth", nc="00000001", cnonce="abc123")
    return (f'Digest username="{USER}", realm="{REALM}", nonce="{nonce}", uri="{ruri}", '
            f'response="{resp}", qop=auth, nc=00000001, cnonce="abc123", algorithm=MD5')


async def main():
    loop = asyncio.get_event_loop()
    server = await SipServer.start(
        SipConfig(local_ip=IP, sip_port=PORT, credentials={USER: PW}, realm=REALM),
        handler_factory=lambda agent_id, i, s: None,
    )
    tr, proto = await loop.create_datagram_endpoint(_Collector, local_addr=(IP, 0))
    vport = tr.get_extra_info("sockname")[1]
    server_addr = (IP, PORT)
    call_id = f"{secrets.token_hex(6)}@{IP}"

    # 1) unauthenticated → 401 + challenge
    tr.sendto(_register(vport, call_id, 1), server_addr)
    data, _ = await asyncio.wait_for(proto.q.get(), timeout=3)
    r = sipmsg.parse(data)
    assert r.status == 401, f"expected 401, got {r.status}"
    chal = auth.parse_authorization(r.get("www-authenticate") or "")
    nonce = chal.get("nonce")
    assert nonce and chal.get("realm") == REALM, "challenge missing nonce/realm"
    print(f"  ok: unauthenticated REGISTER → 401 with nonce + realm=\"{REALM}\"")

    # 2) correct credentials → 200
    tr.sendto(_register(vport, call_id, 2, authz=_authz(nonce, password=PW)), server_addr)
    data, _ = await asyncio.wait_for(proto.q.get(), timeout=3)
    r = sipmsg.parse(data)
    assert r.status == 200, f"expected 200 for valid creds, got {r.status}"
    assert (r.get("expires") or "") == "3600", "200 should echo Expires"
    print("  ok: REGISTER with valid digest → 200 (Expires echoed)")

    # 3) wrong password → 401 (need a fresh nonce first)
    tr.sendto(_register(vport, call_id, 3), server_addr)
    data, _ = await asyncio.wait_for(proto.q.get(), timeout=3)
    nonce2 = auth.parse_authorization(sipmsg.parse(data).get("www-authenticate") or "").get("nonce")
    tr.sendto(_register(vport, call_id, 4, authz=_authz(nonce2, password="WRONG")), server_addr)
    data, _ = await asyncio.wait_for(proto.q.get(), timeout=3)
    assert sipmsg.parse(data).status == 401, "wrong password must be rejected"
    print("  ok: REGISTER with wrong password → 401 (rejected)")

    tr.close()
    await server.stop()
    print("\nREGISTER DIGEST FLOW PASSED ✅")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
