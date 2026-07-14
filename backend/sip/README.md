# `sipd` — native SIP UAS for direct-trunk voice AI

A standalone SIP User-Agent-Server that accepts inbound `INVITE`s straight from a
PBX/SIP-trunk (e.g. your Grandstream UCM6300A) and bridges the call's audio to a
live Gemini agent — no Twilio/Plivo in the middle. This is the "point your trunk
at us" path a Vapi-style product needs.

It is a **separate process** from the web app: the app runs on Railway (HTTP
only), which can't expose SIP/RTP UDP. Run `sipd` on a host the UCM can reach —
for the first bring-up, a box on the UCM's LAN subnet (IP-peer, no NAT).

## The AI-side endpoint (what to put in the UCM trunk)

| Requirement | What sipd provides |
|---|---|
| SIP endpoint host/port | `SIP_LOCAL_IP:SIP_PORT` (default `:5060`, UDP) |
| Accepts INVITE from the UCM | Yes — UAS: `100 → 180 → 200 OK`(+SDP) → `ACK` |
| Codec (PSTN) | **PCMU** and **PCMA** negotiated from the offer (PCMU preferred); DTMF `telephone-event` echoed. G.722 not in v1. |
| Answer + bidirectional audio | RTP ↔ 8 kHz PCM ↔ (resample) ↔ Gemini Live (16 kHz in / 24 kHz out) |
| Hangup | `BYE` → `200 OK` + media teardown; `CANCEL` before answer → `487` |
| Auth — IP peering | `SIP_ALLOWED_PEERS` source-IP allowlist = **the UCM's LAN IP** (the UCM is what sends us the INVITE, so allow *its* address — not Tata's) |
| Auth — registration | Set `SIP_AUTH_USER`/`SIP_AUTH_PASS`; sipd challenges REGISTER (and, with `SIP_AUTH_CALLS=1`, INVITE) with MD5 digest, `qop=auth` — enter the same user/pass in the UCM trunk |

**Agent routing:** the INVITE's request-URI user-part `agent-<id>` selects the
agent (matches `sip_config.inbound_uri_for()`), else `SIP_DEFAULT_AGENT`. For
your one-DID case, just set `SIP_DEFAULT_AGENT` to the target agent.

## Run

```bash
# LAN, IP-peer, your one DID → agent 5.
# SIP_LOCAL_IP   = the box running sipd
# SIP_ALLOWED_PEERS = the UCM6300A's LAN IP (it sends us the calls)
SIP_LOCAL_IP=10.79.217.50 \
SIP_ALLOWED_PEERS=<UCM_LAN_IP> \
SIP_DEFAULT_AGENT=5 \
  .venv/bin/python -m backend.sip
```

Registration-based instead of IP-peer:

```bash
SIP_LOCAL_IP=10.79.217.50 SIP_DEFAULT_AGENT=5 \
SIP_AUTH_USER=tata_ai SIP_AUTH_PASS='choose-a-strong-secret' SIP_AUTH_CALLS=1 \
  .venv/bin/python -m backend.sip
```

All env vars are documented in `__main__.py`.

## UCM6300A side (you do this; the other 29 DIDs stay untouched)

1. **New SIP trunk** → SIP peer, no registration (IP-peer) → host `SIP_LOCAL_IP`,
   port `5060`. (Or registration mode with the user/pass above.)
2. **New inbound route** matching ONLY the chosen DID → destination = this trunk.
3. Leave the existing `_+913365430XXX` route and the other 29 DIDs as-is.

## Design

```
UCM ──SIP/RTP──► server.py (UAS: dialogs, auth)
                    └─ media.py (RtpSession: G.711 codec, 20 ms pacer, symmetric-RTP latch)
                         └─ gemini_handler.py (resample 8k↔16k/24k, barge-in, connector tools)
                              └─ reuses gemini_bridge._live_config / _agent_system_prompt / connectors
```
`g711.py` · `rtp.py` · `sdp.py` · `sipmsg.py` · `auth.py` are the primitives.
The media handler is pluggable (`EchoHandler` powers the loopback test).

## Verified vs. pending

**Verified (unit + loopback, no hardware):**
- `tests_primitives` — G.711 PCMU/PCMA, RTP, SDP negotiation vs. a real Grandstream offer
- `tests_auth` + `tests_register` — MD5 digest (classic + `qop=auth`) over the wire
- `tests_loopback` — a simulated UCM completes a full call: `INVITE→200→ACK→RTP echo→BYE`

**Pending a live test (M5) — needs your UCM + a real Gemini session:**
- `gemini_handler.py` audio bridge (transport under it is loopback-proven; the
  Gemini wiring mirrors the working carrier path but is not yet run live)
- NAT/public-IP (`rport`/STUN), 200-OK retransmit-until-ACK, re-INVITE/hold,
  server-initiated `BYE` on the `end_call` tool, TLS/SRTP — all v1 out-of-scope,
  fine for a LAN IP-peer bring-up.

Run all checks: `for t in primitives auth register loopback; do python -m backend.sip.tests_$t; done`
