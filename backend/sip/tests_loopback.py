"""End-to-end loopback test: a fake UCM (SIP+RTP client) calls our UAS.

Proves the whole transport without hardware or Gemini:
  INVITE → 100/180/200(+SDP) → ACK → RTP in → RTP echoed back → BYE → 200

Run: python -m backend.sip.tests_loopback
"""
import asyncio
import secrets

from . import g711, sipmsg
from . import sdp as sdp_mod
from .rtp import RtpPacket
from .media import EchoHandler
from .server import SipServer, SipConfig

SERVER_IP = "127.0.0.1"
SERVER_SIP_PORT = 55060


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.q.put_nowait((data, addr))


def _offer(client_ip, rtp_port):
    return (
        "v=0\r\n"
        f"o=fakeucm 1 1 IN IP4 {client_ip}\r\n"
        "s=call\r\n"
        f"c=IN IP4 {client_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=ptime:20\r\n"
        "a=sendrecv\r\n"
    )


def _invite(*, ruri, via_host, via_port, call_id, from_tag, sdp):
    body = sdp.encode()
    head = (
        f"INVITE {ruri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {via_host}:{via_port};branch=z9hG4bK{secrets.token_hex(4)}\r\n"
        "Max-Forwards: 70\r\n"
        f"From: <sip:+913365430123@{via_host}>;tag={from_tag}\r\n"
        f"To: <{ruri}>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        f"Contact: <sip:caller@{via_host}:{via_port}>\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    return head.encode() + body


def _bare(method, cseq, *, ruri, via_host, via_port, call_id, from_tag, to_tag):
    to = f"<{ruri}>" + (f";tag={to_tag}" if to_tag else "")
    return (
        f"{method} {ruri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {via_host}:{via_port};branch=z9hG4bK{secrets.token_hex(4)}\r\n"
        "Max-Forwards: 70\r\n"
        f"From: <sip:+913365430123@{via_host}>;tag={from_tag}\r\n"
        f"To: {to}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} {method}\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()


async def main():
    loop = asyncio.get_event_loop()
    server = await SipServer.start(
        SipConfig(local_ip=SERVER_IP, sip_port=SERVER_SIP_PORT, rtp_ports=range(41000, 41010)),
        handler_factory=lambda agent_id, invite, session: EchoHandler(),
    )

    # fake-UCM sockets: one for SIP, one for RTP media
    sip_tr, sip_proto = await loop.create_datagram_endpoint(_Collector, local_addr=(SERVER_IP, 0))
    rtp_tr, rtp_proto = await loop.create_datagram_endpoint(_Collector, local_addr=(SERVER_IP, 0))
    sip_port = sip_tr.get_extra_info("sockname")[1]
    rtp_port = rtp_tr.get_extra_info("sockname")[1]

    ruri = f"sip:agent-5@{SERVER_IP}:{SERVER_SIP_PORT}"
    call_id = f"{secrets.token_hex(8)}@{SERVER_IP}"
    from_tag = secrets.token_hex(4)
    server_addr = (SERVER_IP, SERVER_SIP_PORT)

    # 1) INVITE → collect provisional + final responses
    sip_tr.sendto(_invite(ruri=ruri, via_host=SERVER_IP, via_port=sip_port,
                          call_id=call_id, from_tag=from_tag, sdp=_offer(SERVER_IP, rtp_port)),
                  server_addr)
    statuses, final = [], None
    for _ in range(4):
        data, _ = await asyncio.wait_for(sip_proto.q.get(), timeout=3)
        r = sipmsg.parse(data)
        statuses.append(r.status)
        if r.status >= 200:
            final = r
            break
    assert 100 in statuses, f"no 100 Trying (got {statuses})"
    assert 180 in statuses, f"no 180 Ringing (got {statuses})"
    assert final is not None and final.status == 200, f"no 200 OK (got {statuses})"
    print("  ok: INVITE handshake →", statuses)

    # 2) parse the answer SDP for the server's RTP ip:port + negotiated codec
    ans = sdp_mod.parse_offer(final.body.decode())
    assert ans.remote_ip == SERVER_IP and ans.remote_port in range(41000, 41010)
    pt = sdp_mod.choose_codec(ans)
    assert pt == g711.PAYLOAD_PCMU
    server_rtp = (ans.remote_ip, ans.remote_port)
    to_tag = (final.get("to") or "").split("tag=")[-1]
    print(f"  ok: 200 OK answered PCMU, server RTP {server_rtp[0]}:{server_rtp[1]}, To-tag set")

    # 3) ACK
    sip_tr.sendto(_bare("ACK", 1, ruri=ruri, via_host=SERVER_IP, via_port=sip_port,
                        call_id=call_id, from_tag=from_tag, to_tag=to_tag), server_addr)

    # 4) stream 5 distinctive RTP frames; the echo handler must send them back
    tone_pcm = (b"\xA0\x0F") * g711.SAMPLES_PER_FRAME     # constant ~4000 sample
    sent_payload = g711.encode_from_pcm8k(tone_pcm, g711.PAYLOAD_PCMU)
    for i in range(5):
        pkt = RtpPacket(payload_type=0, sequence=i, timestamp=i * 160,
                        ssrc=0x55, payload=sent_payload, marker=(i == 0))
        rtp_tr.sendto(pkt.build(), server_rtp)
        await asyncio.sleep(0.02)

    # collect ~400ms of echoed media, count exact-match frames (vs silence fill)
    echoed = 0
    async def drain():
        nonlocal echoed
        while True:
            data, _ = await rtp_proto.q.get()
            if RtpPacket.parse(data).payload == sent_payload:
                echoed += 1
    try:
        await asyncio.wait_for(drain(), timeout=0.4)
    except asyncio.TimeoutError:
        pass
    assert echoed >= 3, f"expected ≥3 echoed tone frames, got {echoed}"
    print(f"  ok: RTP bridged — {echoed} tone frames echoed back through the media loop")

    # 5) BYE → 200 + teardown
    sip_tr.sendto(_bare("BYE", 2, ruri=ruri, via_host=SERVER_IP, via_port=sip_port,
                        call_id=call_id, from_tag=from_tag, to_tag=to_tag), server_addr)
    data, _ = await asyncio.wait_for(sip_proto.q.get(), timeout=3)
    assert sipmsg.parse(data).status == 200, "no 200 to BYE"
    assert call_id not in server.dialogs, "dialog not torn down after BYE"
    print("  ok: BYE → 200, dialog + media torn down")

    sip_tr.close(); rtp_tr.close()
    await server.stop()
    print("\nLOOPBACK CALL PASSED ✅  (INVITE→200→ACK→RTP echo→BYE)")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
