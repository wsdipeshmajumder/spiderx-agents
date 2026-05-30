"""Human test — Eva is exercised the way an actual end user would:
real synthesised speech (macOS `say`), one voice per locale, multi-turn
audio conversation, scored against every expectation that's been called
out in this project:

  • Eva is the agent builder for non-technical operators
  • Conversational build (4 target locales: US · UK · SG · IN)
  • Eva does ALL the hard work silently (vapi/vani.ai-style automation)
  • After build, Eva steps out — agent is the hero
  • Apple-grade polish (no enum recitation, no list-reading, warm acks)
  • No re-greeting loops
  • build_complete fires
  • Agent saved with sector / locale / voice / connectors / system_prompt /
    on-brand greeting

Run:  .venv/bin/python tests/human_test.py
"""

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
import urllib.request
import wave
from dataclasses import dataclass, field
from typing import Optional

import websockets


# ── pretty ──────────────────────────────────────────────────────────────

C = {
    "user": "\033[38;5;39m",
    "eva":  "\033[38;5;213m",
    "ok":   "\033[38;5;150m",
    "err":  "\033[38;5;203m",
    "evt":  "\033[38;5;245m",
    "info": "\033[38;5;111m",
    "dim":  "\033[38;5;240m",
    "head": "\033[38;5;183m",
    "_":    "\033[0m",
}
def line(t0, tag, body="", color="evt"):
    s = f"{time.monotonic() - t0:>5.1f}s"
    print(f"  [{s}] {C.get(color,'')}{tag:<10}{C['_']}{('  ' + body) if body else ''}")
def hr(c="─"): print(f"  {C['dim']}{c*78}{C['_']}")
def banner(t):
    print(); hr("═"); print(f"  {C['head']}{t}{C['_']}"); hr("═")


# ── tts ────────────────────────────────────────────────────────────────

def synth(text: str, voice: str) -> bytes:
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
async def stream(ws, pcm, pace_ms=20):
    for i in range(0, len(pcm), CHUNK):
        try: await ws.send(pcm[i:i+CHUNK])
        except Exception: return
        await asyncio.sleep(pace_ms/1000)
async def pad(ws, count=10, pace_ms=20):
    for _ in range(count):
        try: await ws.send(SILENCE)
        except Exception: return
        await asyncio.sleep(pace_ms/1000)


# ── scenarios ──────────────────────────────────────────────────────────

@dataclass
class Scenario:
    title: str
    region: str
    voice_for_say: str    # macOS `say` voice
    locale: str           # browser locale we hand to server
    tz: str               # browser timezone
    spoken_lines: list[str] = field(default_factory=list)
    # expectations
    expect_sector: list[str] = field(default_factory=list)
    expect_locale_starts: list[str] = field(default_factory=list)
    expect_name_hint: Optional[str] = None
    expect_greeting_substring: Optional[str] = None


SCENARIOS: list[Scenario] = [
    Scenario(
        title="India · dental clinic, Bangalore",
        region="India",
        voice_for_say="Aman",      # en-IN male
        locale="en-IN", tz="Asia/Kolkata",
        spoken_lines=[
            "Hi Eva. I want a phone agent for my dental clinic in Bangalore. "
            "Name her Maya. Hindi and English. She handles bookings and reschedules. "
            "Never give medical advice. Save with sensible defaults.",
            "Yes, all good. Save now please.",
            "Yes save now.",
        ],
        expect_sector=["dental", "healthcare"],
        expect_locale_starts=["hi-IN", "en-IN"],
        expect_name_hint="Maya",
        expect_greeting_substring="dental",
    ),
    Scenario(
        title="United States · Italian restaurant, Brooklyn",
        region="USA",
        voice_for_say="Samantha",  # en-US female
        locale="en-US", tz="America/New_York",
        spoken_lines=[
            "Hi Eva. I run an Italian restaurant in Brooklyn. "
            "I want a phone agent that takes table bookings and answers questions about hours. "
            "Name her Sofia. Save with sensible defaults.",
            "Yes, save now.",
            "Yes save Sofia.",
        ],
        expect_sector=["restaurant", "hospitality", "travel"],
        expect_locale_starts=["en-US"],
        expect_name_hint="Sofia",
        expect_greeting_substring=None,
    ),
    Scenario(
        title="United Kingdom · hair salon, London",
        region="UK",
        voice_for_say="Daniel",   # en-GB male
        locale="en-GB", tz="Europe/London",
        spoken_lines=[
            "Hi Eva. I run a hair salon in London. "
            "I need a phone agent to book appointments and tell people our hours and prices. "
            "Name her Olivia. Save with sensible defaults.",
            "Yes save Olivia now.",
            "Yes save now.",
        ],
        expect_sector=["salon", "hospitality"],
        expect_locale_starts=["en-GB"],
        expect_name_hint="Olivia",
        expect_greeting_substring=None,
    ),
    Scenario(
        title="Singapore · specialty café, Tiong Bahru",
        region="Singapore",
        voice_for_say="Daniel",   # en-GB used for Singapore (Live API doesn't ship en-SG)
        locale="en-GB", tz="Asia/Singapore",
        spoken_lines=[
            "Hi Eva. I run a specialty café in Tiong Bahru, Singapore. "
            "I want a phone agent that takes reservations and answers questions about the menu. "
            "Name him Wei. English. Save with sensible defaults.",
            "Yes save Wei now.",
            "Save now.",
        ],
        expect_sector=["restaurant", "hospitality", "salon", "generic"],
        expect_locale_starts=["en-GB", "en-IN", "en-US"],
        expect_name_hint="Wei",
        expect_greeting_substring=None,
    ),
]


# ── grader ─────────────────────────────────────────────────────────────

WARM_TOKENS = re.compile(r"\b(lovely|got it|mm[- ]?hmm|okay|right|brilliant|wonderful|sure thing|of course|alright|perfect|amazing|nice|fantastic|great|excellent)\b", re.I)
LEAD_TOKENS = re.compile(r"\b(let me|i'll|i will|how about|should we|i'll set|i'll go|i'll call|i'll make|let's|saving|connecting|putting|got it|perfect)\b", re.I)
RECITE_BAD = re.compile(r"\b(which voice|what voice|select a voice|preferred locale|choose a sector|pick a sector)\b", re.I)
ENUM_RECITE = re.compile(r"(aoede|puck|charon|leda|orus|kore|fenrir|zephyr).*?(aoede|puck|charon|leda|orus|kore|fenrir|zephyr)", re.I)


@dataclass
class Result:
    sc: Scenario
    t0: float
    user_heard: str = ""
    eva_text: str = ""
    saved: Optional[dict] = None
    elapsed: float = 0.0
    build_complete: bool = False
    drops: int = 0


def grade(r: Result) -> list[tuple[str, bool, str]]:
    a = r.saved or {}
    ok = []
    def add(name, passed, detail=""):
        ok.append((name, passed, detail))

    add("Eva spoke first (no greeting demanded from me)", bool(r.eva_text), f"{len(r.eva_text)} chars")
    add("save_agent fired (Eva built it for me)", r.saved is not None,
        "" if r.saved else "no save_agent in this run")
    add("build_complete fired (Eva stepped out cleanly)", r.build_complete,
        "" if r.build_complete else "build_complete missing")
    add("Total reconnects ≤ 1", r.drops <= 1, f"{r.drops} reconnects")

    if r.saved:
        add(f"Agent name matches '{r.sc.expect_name_hint}'",
            (r.sc.expect_name_hint or "").lower() in (a.get("name") or "").lower() if r.sc.expect_name_hint else True,
            f"got '{a.get('name')}'")
        add(f"Sector in {r.sc.expect_sector}",
            (a.get("sector") or "") in r.sc.expect_sector,
            f"got '{a.get('sector')}'")
        add(f"Locale starts with one of {r.sc.expect_locale_starts}",
            any((a.get("locale") or "").startswith(x) for x in r.sc.expect_locale_starts),
            f"got '{a.get('locale')}'")
        add("Voice picked silently", bool(a.get("voice")), f"got '{a.get('voice')}'")
        add("system_prompt substantive (≥150 chars)",
            len(a.get("system_prompt") or "") >= 150,
            f"{len(a.get('system_prompt') or '')} chars")
        add("Connectors picked (0-5)",
            isinstance(a.get("connectors"), list) and 0 <= len(a["connectors"]) <= 5,
            f"{len(a.get('connectors') or [])} picked: {a.get('connectors')}")
        add("Greeting present", bool((a.get("greeting") or "").strip()), f"'{(a.get('greeting') or '')[:60]}'")
        if r.sc.expect_greeting_substring:
            add(f"Greeting mentions '{r.sc.expect_greeting_substring}'",
                r.sc.expect_greeting_substring.lower() in (a.get("greeting") or "").lower(),
                f"'{a.get('greeting') or ''}'")

    # Apple-polish behaviours (warmth + lead, no enum-recite)
    warm = len(WARM_TOKENS.findall(r.eva_text or ""))
    lead = len(LEAD_TOKENS.findall(r.eva_text or ""))
    add("Eva used warm acknowledgments", warm >= 1, f"{warm} tokens")
    add("Eva used propose-don't-ask language", lead >= 1, f"{lead} tokens")
    add("Eva didn't recite enums / open-ask",
        not (RECITE_BAD.search(r.eva_text or "") or ENUM_RECITE.search(r.eva_text or "")),
        "clean")

    # End-to-end timing
    add("End-to-end ≤ 75 s (real-time speech build)", r.elapsed <= 75, f"{r.elapsed:.1f}s")

    return ok


# ── runner ─────────────────────────────────────────────────────────────

async def run_one(sc: Scenario) -> Result:
    banner(f"HUMAN TEST · {sc.title}    [voice={sc.voice_for_say}]")
    url = f"ws://127.0.0.1:8765/ws/session?{urllib.parse.urlencode({'locale': sc.locale, 'tz': sc.tz})}"
    t0 = time.monotonic()
    r = Result(sc=sc, t0=t0)
    eva_just = asyncio.Event()
    finished = asyncio.Event()
    next_turn = 0

    async with websockets.connect(url, max_size=4_000_000) as ws:
        async def reader():
            seg = ""
            while True:
                try: raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
                except asyncio.TimeoutError: continue
                except websockets.exceptions.ConnectionClosed:
                    line(t0, "WS", "closed", "evt"); finished.set(); return
                if isinstance(raw, (bytes, bytearray)): continue
                try: m = json.loads(raw)
                except json.JSONDecodeError: continue
                t = m.get("type")
                if t == "ready":
                    line(t0, "READY", f"{m.get('model')}/{m.get('kind')}", "info"); eva_just.set()
                elif t == "session_starting":
                    line(t0, "EVENT", f"session_starting kind={m.get('kind')}", "evt")
                elif t == "reconnected":
                    r.drops += 1
                    line(t0, "DROP", f"#{r.drops} reconnected", "err"); eva_just.set()
                elif t == "transcript" and m.get("role") == "model":
                    seg += m["text"]; r.eva_text += m["text"]
                elif t == "transcript" and m.get("role") == "user":
                    r.user_heard += m["text"]
                elif t == "turn_complete":
                    if seg.strip(): line(t0, "EVA  ▼", seg.strip()[:200], "eva"); seg = ""
                    eva_just.set()
                elif t == "agent_saved":
                    r.saved = m["agent"]; a = r.saved
                    line(t0, "SAVED", f"id={a.get('id')} {a.get('name')} {a.get('sector')}/{a.get('locale')}/{a.get('voice')}", "ok")
                elif t == "build_complete":
                    r.build_complete = True
                    line(t0, "EVENT", "build_complete", "info"); finished.set(); return
                elif t == "error":
                    line(t0, "ERROR", m.get("message",""), "err"); finished.set(); return

        async def speaker():
            nonlocal next_turn
            # wait for ready
            await eva_just.wait(); eva_just.clear()
            # wait for the kickoff greeting to finish
            try: await asyncio.wait_for(eva_just.wait(), timeout=15.0)
            except asyncio.TimeoutError: pass
            eva_just.clear()
            while next_turn < len(sc.spoken_lines) and not finished.is_set():
                text = sc.spoken_lines[next_turn]; next_turn += 1
                line(t0, "TTS", f'synth "{text[:60]}…"', "dim")
                pcm = await asyncio.to_thread(synth, text, sc.voice_for_say)
                dur = len(pcm) / 2 / 16000
                line(t0, "USER ▶", text, "user")
                line(t0, "STREAM", f"{dur:.1f}s @ 5× real-time", "dim")
                await stream(ws, pcm, pace_ms=20)
                await pad(ws, 12)
                try: await asyncio.wait_for(eva_just.wait(), timeout=15.0)
                except asyncio.TimeoutError: pass
                eva_just.clear()
                if r.saved: break

        rt = asyncio.create_task(reader()); st = asyncio.create_task(speaker())
        # Generous timeout — real audio + Gemini drops can push past 60s
        try: await asyncio.wait_for(finished.wait(), timeout=120.0)
        except asyncio.TimeoutError: line(t0, "TIMEOUT", "120s", "err")
        rt.cancel(); st.cancel()
        for tk in (rt, st):
            try: await tk
            except (Exception, asyncio.CancelledError): pass

    r.elapsed = time.monotonic() - t0
    return r


async def main():
    results = []
    for sc in SCENARIOS:
        try:
            r = await run_one(sc)
            results.append(r)
        except (Exception, asyncio.CancelledError) as e:
            line(time.monotonic(), "ERROR", str(e), "err")

    # SCORECARD
    print()
    hr("═"); print(f"  {C['head']}HUMAN-TEST SCORECARD (real-speech, 4 locales){C['_']}"); hr("═")

    total_pass = 0; total_count = 0
    for r in results:
        rows = grade(r)
        passed = sum(1 for _, p, _ in rows if p)
        total_pass += passed; total_count += len(rows)
        print()
        print(f"  {C['head']}— {r.sc.title} —{C['_']}    ({passed}/{len(rows)} passed in {r.elapsed:.1f}s)")
        for name, p, detail in rows:
            mark = f"{C['ok']}✅{C['_']}" if p else f"{C['err']}❌{C['_']}"
            dd = f"  {C['dim']}({detail}){C['_']}" if detail else ""
            print(f"    {mark} {name}{dd}")
        if r.user_heard:
            print(f"    {C['user']}Gemini heard:{C['_']} {r.user_heard.strip()[:200]!r}")
        else:
            print(f"    {C['err']}Gemini heard: '' (VAD missed the speech){C['_']}")

    print()
    hr("═"); print(f"  {C['head']}TOTAL: {total_pass}/{total_count} expectations met{C['_']}"); hr("═")
    print()


if __name__ == "__main__":
    asyncio.run(main())
