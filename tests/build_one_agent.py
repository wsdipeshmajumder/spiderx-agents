"""Quick one-shot build: open WS, send a description, wait for save, print the new agent id.
Used by the Chrome go-live audit to create a fresh agent we can then visit
in the real browser via /?reveal=<id>."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time

import websockets


async def main(description: str, locale: str = "en-IN", tz: str = "Asia/Kolkata") -> None:
    url = f"ws://127.0.0.1:8765/ws/session?locale={locale}&tz={tz}"
    saved = None
    start = time.monotonic()

    def noise() -> bytes:
        return struct.pack("<1600h", *([0] * 1600))

    async with websockets.connect(url, max_size=4_000_000) as ws:
        await ws.send(json.dumps({"type": "text", "text": description}))
        async def stream():
            while True:
                try: await ws.send(noise())
                except Exception: return
                await asyncio.sleep(0.1)
        s = asyncio.create_task(stream())
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError: continue
            except websockets.exceptions.ConnectionClosed: break
            if isinstance(raw, (bytes, bytearray)): continue
            try: m = json.loads(raw)
            except json.JSONDecodeError: continue
            if m.get("type") == "agent_saved":
                saved = m["agent"]
                elapsed = time.monotonic() - start
                print(f"SAVED id={saved['id']} name={saved['name']} sector={saved['sector']} "
                      f"locale={saved['locale']} voice={saved['voice']} in {elapsed:.1f}s")
            elif m.get("type") == "build_complete":
                break
            elif m.get("type") == "error":
                print(f"ERROR: {m.get('message')}"); break
        s.cancel()
        try: await s
        except (Exception, asyncio.CancelledError): pass

    if saved:
        print(f"NEW_AGENT_ID={saved['id']}")
        sys.exit(0)
    else:
        print("BUILD FAILED")
        sys.exit(1)


if __name__ == "__main__":
    desc = sys.argv[1] if len(sys.argv) > 1 else (
        "Hi Eva. Build me a phone agent for my florist shop in Singapore. "
        "Name her Mei. English with a Singaporean lilt. Save with sensible defaults."
    )
    asyncio.run(main(desc, locale="en-GB", tz="Asia/Singapore"))
