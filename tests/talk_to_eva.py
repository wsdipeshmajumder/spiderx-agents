"""Speech-driven roleplay: macOS `say` → PCM16 16kHz → WS to Gemini Live.
Gemini hears real human-sounding speech, transcribes it, and responds.

Usage:
    .venv/bin/python tests/talk_to_eva.py [voice]
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import wave

import websockets


C = {
    "user":  "\033[38;5;39m",
    "eva":   "\033[38;5;213m",
    "evt":   "\033[38;5;245m",
    "err":   "\033[38;5;203m",
    "ok":    "\033[38;5;150m",
    "info":  "\033[38;5;111m",
    "dim":   "\033[38;5;240m",
    "head":  "\033[38;5;183m",
    "_":     "\033[0m",
}


def line(t0: float, tag: str, body: str = "", color: str = "evt"):
    stamp = f"{time.monotonic() - t0:>5.1f}s"
    print(f"  [{stamp}] {C.get(color,'')}{tag:<10}{C['_']}{('  ' + body) if body else ''}")


def hr(char: str = "─"):
    print(f"  {C['dim']}{char * 78}{C['_']}")


def synth_speech(text: str, voice: str) -> bytes:
    tmp = tempfile.mkdtemp()
    aiff = os.path.join(tmp, "say.aiff")
    wav = os.path.join(tmp, "say.wav")
    subprocess.run(["say", "-v", voice, "-r", "180", "-o", aiff, text], check=True, capture_output=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True, capture_output=True)
    with wave.open(wav, "rb") as w:
        pcm = w.readframes(w.getnframes())
    try: os.unlink(aiff); os.unlink(wav); os.rmdir(tmp)
    except OSError: pass
    return pcm


CHUNK = 3200          # 100 ms at 16 kHz · Int16 mono
SILENCE = b"\x00" * CHUNK


async def send_pcm(ws, pcm: bytes, pace_ms: int = 20):
    """Stream PCM. Default 20 ms/chunk = 5× real-time, so a 7-s utterance
    arrives in ~1.4 s. Gemini's VAD still hears coherent speech because the
    chunks are still 100 ms of audio each; we just don't sleep a full 100 ms."""
    for i in range(0, len(pcm), CHUNK):
        try: await ws.send(pcm[i:i + CHUNK])
        except Exception: return
        await asyncio.sleep(pace_ms / 1000)


async def send_silence(ws, count: int = 8, pace_ms: int = 20):
    for _ in range(count):
        try: await ws.send(SILENCE)
        except Exception: return
        await asyncio.sleep(pace_ms / 1000)


SCRIPT = [
    # First turn: full spec in one go. Eva needs this complete.
    "Hi Eva. I want to build a phone agent for my dental clinic in Bangalore. "
    "Name her Maya. Hindi and English. She handles bookings and reschedules. "
    "Never give medical advice. Save with sensible defaults.",
    # Patient follow-up if Eva asks for confirmation
    "Yes, sounds great. Save Maya now please.",
]


async def main(voice: str = "Aman"):
    url = f"ws://127.0.0.1:8765/ws/session?{urllib.parse.urlencode({'locale': 'en-IN', 'tz': 'Asia/Kolkata'})}"
    print(); hr("═")
    print(f"  {C['head']}Speech-driven roleplay — voice: {voice}{C['_']}")
    print(f"  WS: {url}"); hr("═")
    t0 = time.monotonic()

    eva_text = ""
    user_heard = ""
    saved_agent = None
    next_turn = 0
    eva_just_finished = asyncio.Event()
    finished_event = asyncio.Event()

    async with websockets.connect(url, max_size=4_000_000) as ws:
        # Background silence stream — keeps Gemini's session alive between
        # explicit user turns. Paused while real speech is being sent so
        # that chunk timing isn't interleaved.
        background_running = True
        speaking_now = asyncio.Event()
        async def background_silence():
            while background_running:
                if not speaking_now.is_set():
                    try: await ws.send(SILENCE)
                    except Exception: return
                await asyncio.sleep(0.1)
        bg_task = asyncio.create_task(background_silence())

        async def reader():
            nonlocal eva_text, user_heard, saved_agent
            seg = ""
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
                except asyncio.TimeoutError: continue
                except websockets.exceptions.ConnectionClosed:
                    line(t0, "WS", "closed by server", "evt"); finished_event.set(); return
                if isinstance(raw, (bytes, bytearray)): continue
                try: m = json.loads(raw)
                except json.JSONDecodeError: continue
                t = m.get("type")
                if t == "ready":
                    line(t0, "READY", f"model={m.get('model')} kind={m.get('kind')}", "info")
                    eva_just_finished.set()   # green-light first turn
                elif t == "session_starting":
                    line(t0, "EVENT", f"session_starting kind={m.get('kind')}", "evt")
                elif t == "reconnected":
                    line(t0, "DROP", "reconnected (memory replay should kick in)", "err")
                    # DON'T set eva_just_finished — reconnect is NOT Eva
                    # finishing a turn. Setting it would make the speaker
                    # barrel ahead with the next nudge mid-build.
                elif t == "transcript" and m.get("role") == "model":
                    seg += m["text"]
                    eva_text += m["text"]
                elif t == "transcript" and m.get("role") == "user":
                    user_heard += m["text"]
                elif t == "turn_complete":
                    if seg.strip():
                        line(t0, "EVA  ▼", seg.strip()[:240], "eva"); seg = ""
                    eva_just_finished.set()
                elif t == "agent_saved":
                    saved_agent = m["agent"]
                    a = saved_agent
                    line(t0, "SAVED", f"id={a.get('id')} name={a.get('name')} sector={a.get('sector')} "
                                      f"locale={a.get('locale')} voice={a.get('voice')}", "ok")
                elif t == "build_complete":
                    line(t0, "EVENT", "build_complete — reveal", "info"); finished_event.set(); return
                elif t == "error":
                    line(t0, "ERROR", m.get("message", ""), "err"); finished_event.set(); return

        async def speaker():
            nonlocal next_turn
            # 1. Wait for ready
            await eva_just_finished.wait(); eva_just_finished.clear()
            # 2. Wait for Eva's initial greeting to complete
            try:
                await asyncio.wait_for(eva_just_finished.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                line(t0, "INFO", "no greeting turn_complete in 15s, speaking anyway", "dim")
            eva_just_finished.clear()

            while next_turn < len(SCRIPT) and not finished_event.is_set():
                text = SCRIPT[next_turn]; next_turn += 1
                line(t0, "TTS", f'synth "{text[:70]}…"', "dim")
                pcm = await asyncio.to_thread(synth_speech, text, voice)
                duration = len(pcm) / 2 / 16000
                line(t0, "USER ▶", text, "user")
                line(t0, "STREAM", f"{duration:.1f}s of speech, sent at 5× real-time", "dim")
                speaking_now.set()
                await send_pcm(ws, pcm, pace_ms=20)
                await send_silence(ws, count=10, pace_ms=20)
                speaking_now.clear()
                # Wait PATIENTLY for Eva — up to 35 s. Long enough for her
                # to respond + start saving + finish saving. Don't barrel
                # ahead with the next nudge; it'll just interrupt her.
                try:
                    await asyncio.wait_for(eva_just_finished.wait(), timeout=35.0)
                except asyncio.TimeoutError:
                    line(t0, "INFO", "Eva quiet 35s, nudging once more", "dim")
                eva_just_finished.clear()
                if saved_agent: break

        reader_task = asyncio.create_task(reader())
        speaker_task = asyncio.create_task(speaker())

        try:
            await asyncio.wait_for(finished_event.wait(), timeout=90.0)
        except asyncio.TimeoutError:
            line(t0, "TIMEOUT", "90s elapsed", "err")
        background_running = False
        reader_task.cancel(); speaker_task.cancel(); bg_task.cancel()
        for tk in (reader_task, speaker_task, bg_task):
            try: await tk
            except (Exception, asyncio.CancelledError): pass

    print(); hr("─")
    print(f"  {C['head']}Result{C['_']}")
    print(f"    {C['eva']}Eva said:{C['_']}     {eva_text.strip()[:300]}")
    print(f"    {C['user']}Gemini heard:{C['_']} {user_heard.strip()[:300] if user_heard.strip() else '(nothing — VAD missed the speech)'}")
    if saved_agent:
        a = saved_agent
        print(f"    {C['ok']}Built agent:{C['_']}  id={a.get('id')} {a.get('name')} ({a.get('sector')} / {a.get('locale')} / {a.get('voice')})")
    else:
        print(f"    {C['err']}Built agent:{C['_']}  none")
    hr("─"); print()


if __name__ == "__main__":
    voice = sys.argv[1] if len(sys.argv) > 1 else "Aman"
    asyncio.run(main(voice))
