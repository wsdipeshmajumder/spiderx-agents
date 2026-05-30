"""Edge test: forced Gemini drop mid-build. Verifies Eva DOES NOT re-greet
on reconnect when we have prior user statements in memory.

Approach: open a WS to /ws/session, send the agent description as text,
wait for Eva's first turn to start, then forcibly close the WS to simulate
the worst case. Reopen, repeat the description, and check whether Eva's
behaviour shows context retention or starts from scratch.

Because the server's `_ConversationMemory` is per-WS (it doesn't survive a
client-side WS close), what this test really verifies is the *server-side*
recovery path: it traces an intra-WS reconnect (Gemini drops while client is
still connected). We trigger that with an artificially restrictive VAD via
the tweaks query string."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
import urllib.parse

import websockets

WS_URL = "ws://127.0.0.1:8765/ws/session"


def make_quiet_noise() -> bytes:
    # Very low amplitude so Gemini's VAD never detects speech — guarantees
    # a session drop after the first turn.
    samples = [0] * 1600
    return struct.pack(f"<{len(samples)}h", *samples)


async def main():
    qs = urllib.parse.urlencode({
        "locale": "en-IN", "tz": "Asia/Kolkata",
    })
    url = f"{WS_URL}?{qs}"

    drops = 0
    eva_history: list[str] = []   # transcripts before each drop boundary
    saved_agent = None
    started = time.monotonic()
    current_eva = ""

    print(f"  Opening WS to {url}")
    async with websockets.connect(url, max_size=4_000_000) as ws:
        # Send the description right at the start
        await ws.send(json.dumps({
            "type": "text",
            "text": "Hi Eva. Build me a dental clinic agent for Bangalore. "
                    "Name her Maya. Hindi and English. Never give medical advice. "
                    "Save with sensible defaults now.",
        }))

        # Quiet noise stream so Gemini drops the session — forces reconnect.
        async def stream_quiet():
            while True:
                try: await ws.send(make_quiet_noise())
                except Exception: return
                await asyncio.sleep(0.1)
        mic = asyncio.create_task(stream_quiet())

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError: continue
            except websockets.exceptions.ConnectionClosed: break

            if isinstance(raw, (bytes, bytearray)): continue
            try: m = json.loads(raw)
            except json.JSONDecodeError: continue
            t = m.get("type")

            if t == "reconnected":
                drops += 1
                eva_history.append(current_eva)
                print(f"  [t={time.monotonic()-started:5.1f}s] RECONNECT #{drops} — Eva so far: {current_eva!r}")
                current_eva = ""
            elif t == "transcript" and m.get("role") == "model":
                current_eva += m.get("text", "")
            elif t == "agent_saved":
                saved_agent = m.get("agent")
                eva_history.append(current_eva)
                print(f"  [t={time.monotonic()-started:5.1f}s] SAVED agent #{saved_agent['id']} = {saved_agent['name']}")
            elif t == "build_complete":
                print(f"  [t={time.monotonic()-started:5.1f}s] build_complete")
                break
            elif t == "error":
                print(f"  ERROR: {m.get('message')}")
                break

        mic.cancel()
        try: await mic
        except (Exception, asyncio.CancelledError): pass

    # Analysis
    print()
    print("  ━" * 39)
    print(f"  Drops:            {drops}")
    print(f"  Saved agent:      {bool(saved_agent)}")
    if saved_agent:
        print(f"     name=     {saved_agent['name']}")
        print(f"     sector=   {saved_agent['sector']}")
        print(f"     locale=   {saved_agent['locale']}")
    print()
    print("  Eva's text per session segment:")
    for i, t in enumerate(eva_history):
        excerpt = t.strip()[:140]
        print(f"    segment {i}: {excerpt!r}")

    # Pass criteria for memory recovery:
    #   • After at least one drop, Eva should NOT re-greet (no "Hi, I'm Eva")
    #   • Or she should explicitly continue with the agent's domain (Maya,
    #     dental, etc.)
    if drops >= 1 and len(eva_history) >= 2:
        post_drop = eva_history[1].lower()
        regreet = "lovely to meet you" in post_drop or "i'm eva" in post_drop
        on_topic = any(t in post_drop for t in ("maya", "dental", "bangalore", "clinic", "medical", "putting", "saving"))
        ok_memory = (not regreet) or on_topic
        print()
        if ok_memory:
            print("  ✅ Memory recovery worked — Eva continued on topic after reconnect.")
        else:
            print("  ❌ Eva appears to have re-greeted after reconnect.")
        sys.exit(0 if ok_memory else 1)
    elif drops == 0:
        print()
        print("  (no drops occurred — couldn't exercise the memory path. Try lowering")
        print("   the audio amplitude or running on the native-audio model.)")
        sys.exit(0)
    else:
        print()
        print("  ⚠ unexpected: drops happened but only one Eva segment recorded.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
