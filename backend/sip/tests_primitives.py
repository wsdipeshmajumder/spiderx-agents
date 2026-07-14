"""Self-contained checks for the SIP media primitives (run: python -m backend.sip.tests_primitives).

Covers the pure logic we CAN verify without a live call: G.711 quantisation
invariants, RTP header round-trip + sender cadence, and SDP offer→codec→answer
against a realistic Grandstream/Tata offer. Live INVITE/RTP flow is tested later
against the actual UCM.
"""
from . import g711, rtp
from . import sdp as sdp_mod


def check(name, cond):
    if not cond:
        raise AssertionError("FAIL: " + name)
    print("  ok:", name)


def test_g711():
    print("G.711 codec")
    check("frame geometry 160/160", g711.SAMPLES_PER_FRAME == 160 and g711.FRAME_BYTES == 160)
    # Quantisation invariant: a decoded G.711 level is a FIXED POINT — decode →
    # encode → decode returns the same PCM. (Re-encoding is not byte-identical
    # for all 256 codes because ±0 share a level, but the audio value is stable,
    # which is the property the RTP path actually relies on.)
    for pt, label in ((g711.PAYLOAD_PCMU, "PCMU"), (g711.PAYLOAD_PCMA, "PCMA")):
        codes = bytes(range(256))
        pcm1 = g711.decode_to_pcm8k(codes, pt)
        pcm2 = g711.decode_to_pcm8k(g711.encode_from_pcm8k(pcm1, pt), pt)
        check(f"{label} decoded level is a fixed point over all 256 codes", pcm2 == pcm1)
        check(f"{label} decodes to 16-bit PCM (2 bytes/sample)", len(pcm1) == 512)
    check("silence frame is 160 bytes", len(g711.silence(g711.PAYLOAD_PCMU)) == 160)


def test_rtp():
    print("RTP")
    p = rtp.RtpPacket(payload_type=0, sequence=1000, timestamp=160, ssrc=0xDEADBEEF,
                      payload=b"\x00" * 160, marker=True)
    wire = p.build()
    check("header is 12 bytes + payload", len(wire) == 12 + 160)
    q = rtp.RtpPacket.parse(wire)
    check("round-trips pt/seq/ts/ssrc/marker",
          q.payload_type == 0 and q.sequence == 1000 and q.timestamp == 160
          and q.ssrc == 0xDEADBEEF and q.marker and q.payload == b"\x00" * 160)

    # parse must skip a CSRC list + a header extension without corrupting payload
    import struct
    hdr = struct.pack("!BBHII", (2 << 6) | 0x10 | 0x01, 8, 5, 320, 0x1111)  # CC=1, X=1, PT=8
    csrc = struct.pack("!I", 0x2222)
    ext = struct.pack("!HH", 0xBEDE, 1) + b"\xAA\xBB\xCC\xDD"               # 1 ext word
    r = rtp.RtpPacket.parse(hdr + csrc + ext + b"payload")
    check("skips CSRC + extension, payload intact", r.payload == b"payload" and r.payload_type == 8)

    s = rtp.RtpSender(payload_type=0, ssrc=1, first_seq=100, first_ts=0)
    a = s.next_packet(b"\x00" * 160)
    b = s.next_packet(b"\x00" * 160)
    check("first packet sets marker", a.marker and not b.marker)
    check("sequence increments by 1", b.sequence == a.sequence + 1)
    check("timestamp advances by 160 samples", b.timestamp == a.timestamp + 160)
    s.mark_gap()
    c = s.next_packet(b"\x00" * 160)
    check("mark_gap re-asserts marker on next talk-spurt", c.marker)


GRANDSTREAM_OFFER = (
    "v=0\r\n"
    "o=GrandstreamUCM 8000 8000 IN IP4 10.79.217.132\r\n"
    "s=SIP Call\r\n"
    "c=IN IP4 10.79.217.132\r\n"
    "t=0 0\r\n"
    "m=audio 10004 RTP/AVP 0 8 9 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-16\r\n"
    "a=ptime:20\r\n"
    "a=sendrecv\r\n"
)


def test_sdp():
    print("SDP")
    offer = sdp_mod.parse_offer(GRANDSTREAM_OFFER)
    check("parses remote RTP ip:port", offer.remote_ip == "10.79.217.132" and offer.remote_port == 10004)
    check("captures offered payload types incl. DTMF", offer.payload_types == [0, 8, 9, 101])
    check("identifies telephone-event PT", offer.dtmf_pt == 101)
    pt = sdp_mod.choose_codec(offer)
    check("chooses PCMU (our top preference) from the offer", pt == g711.PAYLOAD_PCMU)

    answer = sdp_mod.build_answer(local_ip="10.79.217.50", local_port=40000,
                                  payload_type=pt, dtmf_pt=offer.dtmf_pt)
    check("answer advertises our ip:port + single codec", "m=audio 40000 RTP/AVP 0 101" in answer)
    check("answer carries PCMU rtpmap", "a=rtpmap:0 PCMU/8000" in answer)
    check("answer echoes DTMF telephone-event", "a=rtpmap:101 telephone-event/8000" in answer)
    check("answer is sendrecv @ ptime 20", "a=sendrecv" in answer and "a=ptime:20" in answer)
    # the answer must itself be a parseable, self-consistent SDP
    reparsed = sdp_mod.parse_offer(answer)
    check("answer re-parses to our ip:port", reparsed.remote_ip == "10.79.217.50" and reparsed.remote_port == 40000)

    # A PCMA-only peer must negotiate A-law, not fail.
    pcma_only = GRANDSTREAM_OFFER.replace("RTP/AVP 0 8 9 101", "RTP/AVP 8 101")
    check("negotiates PCMA when PCMU absent", sdp_mod.choose_codec(sdp_mod.parse_offer(pcma_only)) == g711.PAYLOAD_PCMA)


if __name__ == "__main__":
    test_g711()
    test_rtp()
    test_sdp()
    print("\nALL PRIMITIVE CHECKS PASSED ✅")
