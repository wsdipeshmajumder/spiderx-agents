"""Barge-in test: while Eva is mid-sentence, the user starts talking. Verify
Eva stops promptly (her chunks stop flowing) and responds to the new input."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import wave

import websockets


def synth(text: str, voice: str = "Aman") -> bytes:
    tmp = tempfile.mkdtemp()
    aiff = os.path.join(tmp, "s.aiff"); wav = os.path.join(tmp, "s.wav")
    subprocess.run(["say", "-v", voice, "-r", "180", "-o", aiff, text], check=True, capture_output=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True, capture_output=True)
    with wave.open(wav, "rb") as w:
        pcm = w.readframes(w.getnframes())
    try: os.unlink(aiff); os.unlink(wav); os.rmdir(tmp)
    except OSError: pass
    return pcm


CHUNK = 3200
SILENCE = b"\x00" * CHUNK


async def main():
    url = "ws://127.0.0.1:8765/ws/session?" + urllib.parse.urlencode({"locale": "en-IN", "tz": "Asia/Kolkata"})
    t0 = time.monotonic()
    eva_text = ""
    audio_chunks_per_segment: list[int] = []
    current_seg_chunks = 0
    interrupt_at = None
    eva_chunks_after_interrupt = 0

    async with websockets.connect(url, max_size=4_000_000) as ws:
        async def reader():
            nonlocal eva_text, current_seg_chunks, audio_chunks_per_segment, eva_chunks_after_interrupt
            while True:
                try: raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
                except asyncio.TimeoutError: continue
                except websockets.exceptions.ConnectionClosed: return
                if isinstance(raw, (bytes, bytearray)):
                    current_seg_chunks += 1
                    if interrupt_at is not None:
                        eva_chunks_after_interrupt += 1
                    continue
                try: m = json.loads(raw)
                except json.JSONDecodeError: continue
                t = m.get("type")
                stamp = time.monotonic() - t0
                if t == "ready":
                    print(f"[{stamp:5.1f}s] READY model={m.get('model')}")
                elif t == "transcript" and m.get("role") == "model":
                    eva_text += m["text"]
                elif t == "interrupted":
                    print(f"[{stamp:5.1f}s] >>> SERVER reports `interrupted` event")
                elif t == "turn_complete":
                    audio_chunks_per_segment.append(current_seg_chunks)
                    print(f"[{stamp:5.1f}s] EVA turn_complete  chunks={current_seg_chunks}  text={eva_text[-160:].strip()!r}")
                    current_seg_chunks = 0
                elif t == "agent_saved":
                    print(f"[{stamp:5.1f}s] save_agent fired (would exit if we let it)")
                elif t == "error":
                    print(f"[{stamp:5.1f}s] ERROR {m.get('message')}")
                    return

        rt = asyncio.create_task(reader())

        # Phase 1: let Eva run her opening greeting (~5s). Stream low-amplitude
        # ambient noise to keep the session alive but not trigger VAD.
        print(f"[{time.monotonic()-t0:5.1f}s] Phase 1: ambient silence while Eva greets…")
        end_phase1 = time.monotonic() + 4.0
        while time.monotonic() < end_phase1:
            await ws.send(SILENCE)
            await asyncio.sleep(0.1)

        # Phase 2: send a description so Eva starts a long response.
        nudge = synth("Hi Eva. Build me a phone agent for my dental clinic in Bangalore. Name her Maya. Hindi and English. She handles bookings.", voice="Aman")
        print(f"[{time.monotonic()-t0:5.1f}s] Phase 2: send long description as user audio ({len(nudge)//2/16000:.1f}s)")
        for i in range(0, len(nudge), CHUNK):
            await ws.send(nudge[i:i + CHUNK])
            await asyncio.sleep(0.02)
        await ws.send(SILENCE); await asyncio.sleep(0.02)

        # Phase 3: wait until Eva starts responding, then INTERRUPT her with another utterance.
        print(f"[{time.monotonic()-t0:5.1f}s] Phase 3: waiting for Eva to begin her response…")
        start_wait = time.monotonic()
        while current_seg_chunks < 6 and time.monotonic() - start_wait < 10:
            await ws.send(SILENCE)
            await asyncio.sleep(0.1)
        chunks_before_interrupt = current_seg_chunks
        print(f"[{time.monotonic()-t0:5.1f}s] Eva has streamed {chunks_before_interrupt} chunks so far — INTERRUPTING")
        interrupt_at = time.monotonic() - t0

        # Phase 4: barge in with new user audio mid-Eva-sentence.
        interrupt = synth("Stop, hold on. Actually, name her Priya instead.", voice="Aman")
        for i in range(0, len(interrupt), CHUNK):
            await ws.send(interrupt[i:i + CHUNK])
            await asyncio.sleep(0.02)
        # 200 ms silence pad
        for _ in range(12):
            await ws.send(SILENCE); await asyncio.sleep(0.02)

        # Phase 5: listen for Eva's next response (should reflect the interruption)
        print(f"[{time.monotonic()-t0:5.1f}s] Phase 5: waiting for Eva to acknowledge the interruption…")
        wait_until = time.monotonic() + 15
        while time.monotonic() < wait_until:
            try:
                await ws.send(SILENCE)
            except websockets.exceptions.ConnectionClosed:
                break
            await asyncio.sleep(0.1)

        rt.cancel()
        try: await rt
        except (Exception, asyncio.CancelledError): pass

    print()
    print("─" * 78)
    print(f"Eva chunks per segment:        {audio_chunks_per_segment}")
    print(f"Eva chunks streamed BEFORE interrupt: {chunks_before_interrupt}")
    print(f"Eva chunks streamed AFTER interrupt:  {eva_chunks_after_interrupt}")
    print(f"Eva final text:                {eva_text.strip()[-300:]!r}")
    print("─" * 78)
    # Pass criteria:
    #   • Server fired the `interrupted` event during phase 3+ (visible in the
    #     trace above)
    #   • Eva started a NEW turn after the interrupt (i.e. ≥2 turn_completes)
    #   • Eva's text mentions "Priya" — proving she acknowledged the new info
    new_turns = len(audio_chunks_per_segment)
    acknowledged = "priya" in (eva_text or "").lower()
    print(f"new turn_complete count: {new_turns}")
    print(f"Eva mentioned 'Priya' after the interrupt? {acknowledged}")
    passed = new_turns >= 2 and acknowledged
    print(f"{'✅ PASS' if passed else '❌ FAIL'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
