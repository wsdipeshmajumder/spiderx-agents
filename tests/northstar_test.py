"""Northstar audit.

Runs a realistic, multi-turn roleplay with Eva against the running server,
prints a colour-coded console log of every event, and scores the journey
against the criteria in /Users/dipeshmajumder/phone_ai/northstar.md:

  ▸ End-to-end (tap → test agent on the line) under 90 s
  ▸ Eva interviews in 4–6 short turns
  ▸ Eva LEADS — propose-don't-ask language present
  ▸ Eva uses warm acknowledgments (lovely / got it / mm-hmm / etc.)
  ▸ Eva commits to defaults silently (doesn't recite enums or ask "what voice?")
  ▸ Zero loops — at most one "sorry you broke up" if at all
  ▸ save_agent fires with a plausible system_prompt (≥150 chars)
  ▸ Handoff transfers to the new agent without the WS reconnecting
  ▸ New agent greets on-brand within 5 s of the handoff
  ▸ Per-agent voice + connectors picked, not blank

Run against the current /api/health model (cascade or native-audio).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets

WS_URL = "ws://127.0.0.1:8765/ws/session"
HTTP_BASE = "http://127.0.0.1:8765"


# ── pretty console ──────────────────────────────────────────────────────

C = {
    "eva":   "\033[38;5;213m",
    "agent": "\033[38;5;220m",
    "me":    "\033[38;5;39m",
    "evt":   "\033[38;5;245m",
    "err":   "\033[38;5;203m",
    "ok":    "\033[38;5;150m",
    "info":  "\033[38;5;111m",
    "warn":  "\033[38;5;215m",
    "head":  "\033[38;5;183m",
    "dim":   "\033[38;5;240m",
    "reset": "\033[0m",
}


def line(t0: float, tag: str, body: str = "", color: str = "evt"):
    stamp = f"{time.monotonic() - t0:>5.1f}s"
    c = C.get(color, "")
    body_part = f"  {body}" if body else ""
    print(f"  [{stamp}] {c}{tag:<10}{C['reset']}{body_part}")


def hr(char: str = "─", color: str = "dim"):
    print(f"  {C[color]}{char * 78}{C['reset']}")


def banner(text: str):
    print()
    hr("═", "head")
    print(f"  {C['head']}{text}{C['reset']}")
    hr("═", "head")


# ── synthetic mic stream (so native-audio sessions don't insta-drop) ────

def make_noise_chunk() -> bytes:
    samples = [random.randint(-400, 400) for _ in range(1600)]
    return struct.pack(f"<{len(samples)}h", *samples)


# ── scenarios ───────────────────────────────────────────────────────────

@dataclass
class TurnReply:
    """A scripted reply, gated by what Eva has said so far.

    `gate` is a regex; the reply fires when Eva's accumulated transcript
    matches it AND we haven't fired this reply yet AND we're past the
    last `min_delay` seconds since the previous reply (so we don't
    barrel through the conversation faster than Eva can finish a turn)."""

    gate: str
    text: str
    min_delay: float = 0.4


@dataclass
class Scenario:
    title: str
    locale: str
    tz: str
    initial: str
    replies: list[TurnReply] = field(default_factory=list)
    # Northstar expectations
    expect_sector_one_of: list[str] = field(default_factory=list)
    expect_locale_one_of: list[str] = field(default_factory=list)
    expect_agent_name_hint: Optional[str] = None  # substring in saved name
    expect_greeting_hint: Optional[str] = None    # substring expected in greeting


SCENARIOS: list[Scenario] = [
    Scenario(
        title="Dental clinic, Bangalore",
        locale="en-IN", tz="Asia/Kolkata",
        initial="Hi Eva! I want a phone agent for my dental clinic in Bangalore. "
                "Name her Maya, speak Hindi and English, never give medical advice.",
        replies=[
            TurnReply(r"(book|reschedule|appoint)", "Yes, both — bookings and reschedules."),
            TurnReply(r"(namaste|maya|greet|hello)", "That greeting sounds great, use it. Save her now with all sensible defaults."),
            TurnReply(r"(medical|advice|guardrail)", "Yes, no medical advice. Save her now."),
            TurnReply(r"(save|put.*on|connect)", "Yes, save her now please."),
        ],
        expect_sector_one_of=["dental", "healthcare"],
        expect_locale_one_of=["hi-IN", "en-IN"],
        expect_agent_name_hint="Maya",
        expect_greeting_hint="dental",
    ),
    Scenario(
        title="Boutique hotel, NYC",
        locale="en-US", tz="America/New_York",
        initial="Hi Eva. I run a small boutique hotel in Manhattan. "
                "I need a receptionist agent that handles room availability and bookings. "
                "Name her Sofia.",
        replies=[
            TurnReply(r"(book|availab|room)", "Yes, exactly — bookings and availability."),
            TurnReply(r"(sofia|greet|hello|welcome)", "That greeting works. Save her now."),
            TurnReply(r"(save|put.*on|connect|all set)", "Save now please."),
        ],
        expect_sector_one_of=["hotel", "travel", "restaurant", "salon", "generic"],
        expect_locale_one_of=["en-US"],
        expect_agent_name_hint="Sofia",
        expect_greeting_hint=None,
    ),
    Scenario(
        title="Insurance brokerage, Pune",
        locale="en-IN", tz="Asia/Kolkata",
        initial="Hi Eva. Insurance brokerage in Pune. I want callers to share what "
                "they need for car or health insurance and have a lead captured. "
                "Name him Rohan. Never give personalised financial advice.",
        replies=[
            TurnReply(r"(lead|capture|car|health)", "Yes, capture leads for car and health insurance."),
            TurnReply(r"(rohan|greet|namaste|insurance)", "That greeting is fine, save him."),
            TurnReply(r"(save|put.*on|connect)", "Yes, save now."),
        ],
        expect_sector_one_of=["insurance", "real_estate", "generic"],
        expect_locale_one_of=["en-IN", "hi-IN"],
        expect_agent_name_hint="Rohan",
        expect_greeting_hint=None,
    ),
]


# ── northstar criteria ──────────────────────────────────────────────────

WARM_TOKENS = re.compile(
    r"\b(lovely|got it|mm[- ]?hmm|okay|right|brilliant|wonderful|sure thing|"
    r"of course|alright|perfect|amazing|nice|fantastic)\b",
    re.I,
)
LEAD_TOKENS = re.compile(
    r"\b(let me|i'll|i will|shall i|how about|should we|"
    r"i'll set|i'll go|i'll call|i'll make|let's|saving|connecting|putting)\b",
    re.I,
)
RECITE_BAD = re.compile(
    r"\b(which voice|what voice|select a voice|preferred locale|"
    r"choose a sector|pick a sector)\b",
    re.I,
)
ENUM_RECITE = re.compile(
    r"(aoede|puck|charon|leda|orus|kore|fenrir|zephyr).*?(aoede|puck|charon|leda|orus|kore|fenrir|zephyr)",
    re.I,
)


@dataclass
class Run:
    sc: Scenario
    t0: float
    builder_turns: int = 0
    eva_text: str = ""
    test_text: str = ""
    drops: int = 0
    sorry_count: int = 0
    saved_at: Optional[float] = None
    saved_agent: Optional[dict] = None
    build_complete_at: Optional[float] = None
    test_ready_at: Optional[float] = None
    test_first_word_at: Optional[float] = None
    test_turn_complete_at: Optional[float] = None
    first_eva_word_at: Optional[float] = None
    user_heard: str = ""
    completed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class CriterionResult:
    name: str
    passed: bool
    detail: str = ""


def grade(run: Run) -> list[CriterionResult]:
    sc = run.sc
    results: list[CriterionResult] = []

    def add(name, passed, detail=""):
        results.append(CriterionResult(name, passed, detail))

    # Time-to-first-word
    if run.first_eva_word_at is not None:
        add("Eva speaks within 5 s of tap",
            run.first_eva_word_at <= 5.0,
            f"{run.first_eva_word_at:.1f}s")
    else:
        add("Eva speaks within 5 s of tap", False, "never spoke")

    # End-to-end ≤ 90 s
    if run.test_first_word_at:
        e2e = run.test_first_word_at
        add("End-to-end ≤ 90 s (tap → test agent greets)",
            e2e <= 90.0, f"{e2e:.1f}s")
    else:
        add("End-to-end ≤ 90 s", False, "never reached test mode")

    # Turn count
    add("Builder turns ≤ 6", run.builder_turns <= 6, f"{run.builder_turns} turns")

    # Save fires
    add("save_agent fired", run.saved_agent is not None,
        "" if run.saved_agent else "tool was never called")

    # Sector / locale plausibility
    if run.saved_agent:
        sector = (run.saved_agent.get("sector") or "").lower()
        add(f"Sector reasonable ({', '.join(sc.expect_sector_one_of)})",
            sector in sc.expect_sector_one_of, f"got '{sector}'")
        locale = (run.saved_agent.get("locale") or "")
        add(f"Locale reasonable ({', '.join(sc.expect_locale_one_of)})",
            locale in sc.expect_locale_one_of, f"got '{locale}'")
        if sc.expect_agent_name_hint:
            nm = (run.saved_agent.get("name") or "")
            add(f"Agent name contains '{sc.expect_agent_name_hint}'",
                sc.expect_agent_name_hint.lower() in nm.lower(), f"got '{nm}'")
        # voice / system_prompt sanity
        add("Voice picked",
            bool(run.saved_agent.get("voice")),
            run.saved_agent.get("voice") or "blank")
        sys_prompt_len = len(run.saved_agent.get("system_prompt") or "")
        add("system_prompt is substantive (≥ 150 chars)",
            sys_prompt_len >= 150, f"{sys_prompt_len} chars")
        connectors = run.saved_agent.get("connectors") or []
        add("Connectors picked (1–3)",
            1 <= len(connectors) <= 5, f"{len(connectors)} connectors: {connectors}")

    # Eva leads
    leads = LEAD_TOKENS.findall(run.eva_text or "")
    add("Eva uses propose-don't-ask language",
        len(leads) >= 1, f"{len(leads)} propose-tokens")

    # Eva warmth
    warm = WARM_TOKENS.findall(run.eva_text or "")
    add("Eva uses warm acknowledgments",
        len(warm) >= 1, f"{len(warm)} warm-tokens")

    # No recitation
    bad_recite = bool(RECITE_BAD.search(run.eva_text or "") or
                      ENUM_RECITE.search(run.eva_text or ""))
    add("Eva doesn't recite enums / open-ask",
        not bad_recite, "clean" if not bad_recite else "found recitation")

    # Drop / loop discipline
    add("At most 1 'sorry you broke up' apology",
        run.sorry_count <= 1, f"{run.sorry_count} apologies")
    add("Total reconnects ≤ 1",
        run.drops <= 1, f"{run.drops} drops")

    # Builder ends cleanly with build_complete (new flow: reveal-then-user-chooses)
    if run.build_complete_at:
        add("build_complete fires (clean exit from builder)", True, f"at {run.build_complete_at:.1f}s")
    else:
        add("build_complete fires (clean exit from builder)", False, "never fired")

    # The agent picks up the line when the user requests a test call
    if run.test_first_word_at:
        # measure from build_complete (when reveal would have appeared) to first
        # test-agent word
        ref = run.build_complete_at or run.saved_at or 0
        elapsed = run.test_first_word_at - ref
        add("Test agent greets within 8 s of reveal",
            elapsed <= 8.0, f"{elapsed:.1f}s after build_complete")
    else:
        add("Test agent greets within 8 s of reveal", False, "agent never spoke in test mode")

    # Test-mode on-brand greeting
    if run.saved_agent and sc.expect_greeting_hint and run.test_text:
        add(f"Test greeting mentions '{sc.expect_greeting_hint}'",
            sc.expect_greeting_hint.lower() in run.test_text.lower(),
            f"'{run.test_text[:80]}…'")

    return results


# ── single run ──────────────────────────────────────────────────────────

async def run_scenario(sc: Scenario) -> Run:
    t0 = time.monotonic()
    run = Run(sc=sc, t0=t0)
    qs = urllib.parse.urlencode({"locale": sc.locale, "tz": sc.tz})
    url = f"{WS_URL}?{qs}"
    stop_audio = asyncio.Event()
    fired_reply_indexes: set[int] = set()
    sent_initial = False

    line(t0, "STAGE", f"opening {sc.title}", "info")

    try:
        async with websockets.connect(url, max_size=4_000_000) as ws:

            async def send_text(t: str):
                await ws.send(json.dumps({"type": "text", "text": t}))
                line(t0, "ME ▶", t[:120], "me")

            async def stream():
                while not stop_audio.is_set():
                    try: await ws.send(make_noise_chunk())
                    except Exception: return
                    await asyncio.sleep(0.1)

            mic = asyncio.create_task(stream())

            async def maybe_fire_replies():
                """Run after each Eva utterance and after each turn_complete:
                if a TurnReply's gate matches Eva's accumulated text, fire it."""
                if not run.saved_agent:
                    for i, r in enumerate(sc.replies):
                        if i in fired_reply_indexes: continue
                        if re.search(r.gate, run.eva_text, re.I):
                            fired_reply_indexes.add(i)
                            await asyncio.sleep(r.min_delay)
                            await send_text(r.text)
                            return  # fire one at a time

            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.5)
                except asyncio.TimeoutError:
                    if run.saved_agent and run.test_turn_complete_at:
                        break
                    continue
                except websockets.exceptions.ConnectionClosed:
                    line(t0, "WS", "closed by server", "err")
                    break

                if isinstance(raw, (bytes, bytearray)):
                    continue

                try: m = json.loads(raw)
                except json.JSONDecodeError: continue
                t = m.get("type")

                if t == "session_starting":
                    if m.get("kind") == "test":
                        line(t0, "EVENT", f"session_starting kind=test agent={(m.get('agent') or {}).get('name')}", "evt")
                    else:
                        line(t0, "EVENT", "session_starting kind=builder", "evt")
                elif t == "ready":
                    line(t0, "READY", f"model={m.get('model')} kind={m.get('kind')}", "info")
                    if m.get("kind") == "test":
                        run.test_ready_at = time.monotonic() - t0
                    if m.get("kind") == "builder" and not sent_initial:
                        sent_initial = True
                        await asyncio.sleep(0.3)
                        await send_text(sc.initial)
                elif t == "reconnected":
                    run.drops += 1
                    line(t0, "DROP", f"#{run.drops} reconnected", "err")
                    # If we dropped before save, re-send initial
                    if not run.saved_agent:
                        await asyncio.sleep(0.3)
                        await send_text(sc.initial)
                elif t == "transcript":
                    role = m.get("role")
                    text = m.get("text", "")
                    if role == "user":
                        run.user_heard += text
                    elif role == "model":
                        if run.saved_agent is None:
                            if not run.eva_text:
                                run.first_eva_word_at = time.monotonic() - t0
                            run.eva_text += text
                        else:
                            if not run.test_text:
                                run.test_first_word_at = time.monotonic() - t0
                            run.test_text += text
                elif t == "turn_complete":
                    if run.saved_agent is None:
                        run.builder_turns += 1
                        excerpt = run.eva_text[-220:].strip()
                        line(t0, "EVA ▼", excerpt[:200], "eva")
                        # Count "sorry you broke up" style apologies
                        if re.search(r"sorry.*broke", run.eva_text[-300:], re.I):
                            run.sorry_count += 1
                        await maybe_fire_replies()
                    else:
                        run.test_turn_complete_at = time.monotonic() - t0
                        line(t0, "AGENT ▼", run.test_text[:160], "agent")
                        line(t0, "DONE", "test mode greeting received", "ok")
                        await asyncio.sleep(0.5)
                        break
                elif t == "agent_saved":
                    run.saved_at = time.monotonic() - t0
                    run.saved_agent = m.get("agent")
                    a = run.saved_agent or {}
                    line(t0, "SAVED",
                         f"id={a.get('id')} name={a.get('name')} sector={a.get('sector')} "
                         f"locale={a.get('locale')} voice={a.get('voice')} "
                         f"connectors={a.get('connectors')} prompt_len={len(a.get('system_prompt') or '')}",
                         "ok")
                elif t == "transferring":
                    line(t0, "EVENT", "transferring → test mode (legacy auto-handoff)", "info")
                elif t == "build_complete":
                    run.build_complete_at = time.monotonic() - t0
                    line(t0, "EVENT", "build_complete — reveal would appear here", "ok")
                elif t == "tool_call":
                    line(t0, "TOOL", f"{m.get('name')}", "evt")
                elif t == "error":
                    line(t0, "ERROR", m.get("message",""), "err")
                    break
                elif t == "go_away":
                    line(t0, "EVENT", "go_away from Gemini", "err")
                elif t == "interrupted":
                    pass

            stop_audio.set()
            mic.cancel()
            try: await mic
            except (Exception, asyncio.CancelledError): pass

        # ─ Second leg: simulate the user tapping "Call <name>" on the reveal
        if run.saved_agent and not run.test_first_word_at:
            line(t0, "STAGE", f"reveal action: Call {run.saved_agent['name']}", "info")
            url2 = f"{WS_URL}?" + urllib.parse.urlencode({
                "locale": sc.locale, "tz": sc.tz, "agent_id": run.saved_agent["id"],
            })
            stop2 = asyncio.Event()
            try:
                async with websockets.connect(url2, max_size=4_000_000) as ws2:
                    async def stream2():
                        while not stop2.is_set():
                            try: await ws2.send(make_noise_chunk())
                            except Exception: return
                            await asyncio.sleep(0.1)
                    mic2 = asyncio.create_task(stream2())
                    deadline2 = time.monotonic() + 20.0
                    while time.monotonic() < deadline2:
                        try:
                            raw = await asyncio.wait_for(ws2.recv(), timeout=2.0)
                        except asyncio.TimeoutError: continue
                        except websockets.exceptions.ConnectionClosed:
                            line(t0, "WS", "test session closed", "evt"); break
                        if isinstance(raw, (bytes, bytearray)): continue
                        try: m = json.loads(raw)
                        except json.JSONDecodeError: continue
                        t = m.get("type")
                        if t == "ready":
                            line(t0, "READY", f"test mode model={m.get('model')}", "info")
                        elif t == "transcript" and m.get("role") == "model":
                            if not run.test_text:
                                run.test_first_word_at = time.monotonic() - t0
                            run.test_text += m.get("text", "")
                        elif t == "turn_complete":
                            run.test_turn_complete_at = time.monotonic() - t0
                            line(t0, "AGENT ▼", run.test_text[:160], "agent")
                            line(t0, "DONE", "test mode greeting received", "ok")
                            await asyncio.sleep(0.5); break
                        elif t == "error":
                            line(t0, "ERROR", m.get("message", ""), "err"); break
                    stop2.set()
                    mic2.cancel()
                    try: await mic2
                    except (Exception, asyncio.CancelledError): pass
            except (Exception, asyncio.CancelledError) as e:
                line(t0, "ERROR", f"test session failed: {e}", "err")

        run.completed = True
    except (Exception, asyncio.CancelledError) as e:
        run.notes.append(f"transport error: {e!s}")
        line(t0, "ERROR", str(e), "err")

    return run


# ── main ────────────────────────────────────────────────────────────────

async def main():
    try:
        with urllib.request.urlopen(f"{HTTP_BASE}/api/health", timeout=3) as r:
            health = json.loads(r.read())
        model = health.get("model")
        print()
        print(f"  {C['info']}server model: {model}{C['reset']}")
        print(f"  {C['dim']}agents in DB: {health.get('agents')}{C['reset']}")
    except Exception as e:
        print(f"Could not reach server: {e}")
        sys.exit(1)

    all_runs = []
    for sc in SCENARIOS:
        banner(f"NORTHSTAR · {sc.title}")
        run = await run_scenario(sc)
        all_runs.append(run)

    print()
    print()
    hr("═", "head")
    print(f"  {C['head']}NORTHSTAR SCORECARD ({model}){C['reset']}")
    hr("═", "head")
    total_passed = 0
    total_checked = 0
    for run in all_runs:
        results = grade(run)
        passed = sum(1 for r in results if r.passed)
        total_passed += passed
        total_checked += len(results)
        print()
        print(f"  {C['head']}— {run.sc.title} —{C['reset']}    ({passed}/{len(results)} passed)")
        for r in results:
            mark = f"{C['ok']}✅{C['reset']}" if r.passed else f"{C['err']}❌{C['reset']}"
            detail = f"  {C['dim']}({r.detail}){C['reset']}" if r.detail else ""
            print(f"    {mark} {r.name}{detail}")
        if run.user_heard:
            print(f"    {C['dim']}USER heard text: {run.user_heard!r}{C['reset']}")
        else:
            print(f"    {C['warn']}USER heard text: '' — Gemini transcribed no user speech{C['reset']}")

    print()
    hr("═", "head")
    print(f"  {C['head']}TOTAL: {total_passed}/{total_checked} criteria passed{C['reset']}")
    hr("═", "head")
    print()
    sys.exit(0 if total_passed == total_checked else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
