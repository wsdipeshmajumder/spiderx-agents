"""Call a saved agent in test mode and roleplay as a real caller.
Verifies A-star front-office behaviours: greeting, ack-before-action,
confirmation, close-with-anything-else, multilingual code-switching."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import wave

import websockets


C = {"u":"\033[38;5;39m","a":"\033[38;5;220m","ok":"\033[38;5;150m","e":"\033[38;5;203m","i":"\033[38;5;111m","d":"\033[38;5;240m","h":"\033[38;5;183m","_":"\033[0m"}
def L(t0, tag, body="", color="d"):
    print(f"  [{time.monotonic()-t0:5.1f}s] {C.get(color,'')}{tag:<10}{C['_']}{('  ' + body) if body else ''}")

def synth(text, voice="Aman"):
    tmp = tempfile.mkdtemp()
    aiff = os.path.join(tmp,"s.aiff"); wav = os.path.join(tmp,"s.wav")
    subprocess.run(["say","-v",voice,"-r","180","-o",aiff,text], check=True, capture_output=True)
    subprocess.run(["afconvert","-f","WAVE","-d","LEI16@16000","-c","1",aiff,wav], check=True, capture_output=True)
    with wave.open(wav,"rb") as w: pcm = w.readframes(w.getnframes())
    try: os.unlink(aiff); os.unlink(wav); os.rmdir(tmp)
    except OSError: pass
    return pcm

CHUNK = 3200
SILENCE = b"\x00" * CHUNK

# Caller script for a typical dental clinic call
CALLER_SCRIPT = [
    "Hi there. I'd like to book a dental check-up please.",
    "How about Friday afternoon, sometime around 3 pm?",
    "Yes that works. Book it please. My name is Arjun and my number is 9876543210.",
    "Actually, can you also send me a text confirmation?",
    "Thanks. That's all for now.",
]


async def main(agent_id: int, voice: str = "Aman"):
    url = f"ws://127.0.0.1:8765/ws/session?{urllib.parse.urlencode({'locale':'en-IN','tz':'Asia/Kolkata','agent_id':agent_id})}"
    print(f"\n  {C['h']}Calling agent #{agent_id} as caller voice {voice}{C['_']}\n")
    t0 = time.monotonic()
    agent_text = ""
    agent_just_finished = asyncio.Event()
    finished = asyncio.Event()
    background = True
    speaking = asyncio.Event()

    async with websockets.connect(url, max_size=4_000_000) as ws:
        async def bg():
            while background:
                if not speaking.is_set():
                    try: await ws.send(SILENCE)
                    except Exception: return
                await asyncio.sleep(0.1)
        bg_t = asyncio.create_task(bg())

        async def reader():
            nonlocal agent_text
            seg = ""
            while True:
                try: raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
                except asyncio.TimeoutError: continue
                except websockets.exceptions.ConnectionClosed:
                    L(t0,"WS","closed","d"); finished.set(); return
                if isinstance(raw,(bytes,bytearray)): continue
                try: m = json.loads(raw)
                except json.JSONDecodeError: continue
                t = m.get("type")
                if t == "ready":
                    L(t0,"READY",f"kind={m.get('kind')}","i"); agent_just_finished.set()
                elif t == "session_starting": L(t0,"EVENT",f"kind={m.get('kind')}","d")
                elif t == "reconnected": L(t0,"DROP","reconnect","e")
                elif t == "transcript" and m.get("role")=="model":
                    seg += m["text"]; agent_text += m["text"]
                elif t == "turn_complete":
                    if seg.strip(): L(t0,"AGENT ▼",seg.strip()[:240],"a"); seg = ""
                    agent_just_finished.set()
                elif t == "tool_call": L(t0,"TOOL",m.get("name",""),"d")
                elif t == "error": L(t0,"ERROR",m.get("message",""),"e"); finished.set(); return

        async def speaker():
            # wait for ready
            await agent_just_finished.wait(); agent_just_finished.clear()
            # wait for greeting
            try: await asyncio.wait_for(agent_just_finished.wait(), timeout=12.0)
            except asyncio.TimeoutError: pass
            agent_just_finished.clear()
            for i, text in enumerate(CALLER_SCRIPT):
                if finished.is_set(): break
                pcm = await asyncio.to_thread(synth, text, voice)
                L(t0,"CALLER ▶",text,"u")
                speaking.set()
                for j in range(0,len(pcm),CHUNK):
                    try: await ws.send(pcm[j:j+CHUNK])
                    except Exception: pass
                    await asyncio.sleep(0.02)
                for _ in range(10):
                    try: await ws.send(SILENCE)
                    except Exception: pass
                    await asyncio.sleep(0.02)
                speaking.clear()
                try: await asyncio.wait_for(agent_just_finished.wait(), timeout=25.0)
                except asyncio.TimeoutError: L(t0,"INFO","agent quiet 25s","d")
                agent_just_finished.clear()

        rt = asyncio.create_task(reader()); st = asyncio.create_task(speaker())
        try: await asyncio.wait_for(finished.wait(), timeout=100.0)
        except asyncio.TimeoutError:
            L(t0,"TIMEOUT","100s","e")
        background = False
        rt.cancel(); st.cancel(); bg_t.cancel()
        for tk in (rt,st,bg_t):
            try: await tk
            except (Exception, asyncio.CancelledError): pass

    print()
    print(f"  {C['h']}Agent transcript:{C['_']}")
    print(f"    {agent_text.strip()}")
    print()
    # Score A-star behaviours
    txt = agent_text.lower()
    checks = [
        ("Greeted with name/business", any(s in txt for s in ["maya", "clinic", "dental"])),
        ("Used warm acknowledgment", bool(re.search(r"\b(sure|of course|absolutely|let me|i'll|one moment|got it|alright)\b", txt))),
        ("Mentioned/called a connector (book/check)", bool(re.search(r"\b(check|book|booking|available|slot|schedule|confirm)\b", txt))),
        ("Confirmed details back to caller", bool(re.search(r"\b(friday|3 ?pm|3 o'clock|arjun|9876)\b", txt))),
        ("Offered SMS confirmation", "sms" in txt or "text" in txt or "confirm" in txt),
        ("Closed with 'anything else'", "anything else" in txt or "anything more" in txt or "anything i can" in txt),
        ("Used Hindi/Hinglish", any(w in txt for w in ["namaste","aap","kya","main","hum","kaise","accha","theek","baat"])),
    ]
    print(f"  {C['h']}A-star behaviour checks:{C['_']}")
    passed = 0
    for name, ok in checks:
        m = f"{C['ok']}✅{C['_']}" if ok else f"{C['e']}❌{C['_']}"
        print(f"    {m} {name}")
        if ok: passed += 1
    print()
    print(f"  {C['h']}TOTAL: {passed}/{len(checks)} A-star behaviours observed{C['_']}")


if __name__ == "__main__":
    agent_id = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    voice = sys.argv[2] if len(sys.argv) > 2 else "Aman"
    asyncio.run(main(agent_id, voice))
