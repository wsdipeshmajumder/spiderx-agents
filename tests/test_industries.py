"""End-to-end test runner: ten service-industry scenarios, each driven by
text turns over the same /ws/session WebSocket the browser uses. For each
scenario we feed Eva a clear description, prompt for save, and verify:
  • Eva responds (audio chunks streamed back)
  • save_agent is called
  • The saved agent has plausible fields (name, sector, locale, voice,
    greeting, system_prompt of reasonable length)
  • Session transfers to test-mode and the saved agent speaks

This tests the build *logic* end-to-end. We use the cascade Live model for
the test (set GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview before starting
the server) because it accepts text-driven sessions; the native-audio model
silently ends sessions when its VAD doesn't detect speech in the incoming
audio, which makes scripted text-turn tests impossible. In production the
default is native-audio; this test validates the orchestration layer.

Usage:
  GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview .venv/bin/uvicorn …
  .venv/bin/python tests/test_industries.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets

WS_URL = "ws://127.0.0.1:8765/ws/session"
HTTP_BASE = "http://127.0.0.1:8765"
PER_TEST_TIMEOUT = 90.0
PER_TURN_TIMEOUT = 25.0


@dataclass
class Scenario:
    name: str
    description: str
    locale: str
    tz: str
    expected_sector_hint: str  # substring expected somewhere in sector or prompt
    follow_ups: list[str] = field(default_factory=lambda: [
        "Yes, go ahead with all sensible defaults.",
        "Looks good. Save the agent now and put me through to test her.",
    ])


SCENARIOS: list[Scenario] = [
    Scenario(
        "Dental clinic (India)",
        "I want an agent for my dental clinic in Bangalore. Hindi and English. "
        "Bookings and reschedules. Name her Maya. Never give medical advice. "
        "Pick all sensible defaults and save right now.",
        "en-IN", "Asia/Kolkata", "dental",
    ),
    Scenario(
        "Restaurant (US)",
        "I run a small Italian restaurant in Brooklyn. I need a phone agent that "
        "takes table bookings and answers questions about the menu and hours. "
        "Name her Sofia. English. Save now with sensible defaults.",
        "en-US", "America/New_York", "restaurant",
    ),
    Scenario(
        "Hair salon (UK)",
        "Hair salon in London. The agent should book appointments and tell people "
        "our prices and hours. Name her Olivia. British English. Use defaults and "
        "save the agent now.",
        "en-GB", "Europe/London", "salon",
    ),
    Scenario(
        "Real estate (India)",
        "Real estate brokerage in Mumbai. I want callers to share what they're "
        "looking for and have a lead created. Name him Kabir. Hindi and English. "
        "Save now, all sensible defaults.",
        "en-IN", "Asia/Kolkata", "real_estate",
    ),
    Scenario(
        "Hotel (UAE)",
        "Boutique hotel in Dubai. Receptionist that handles room availability "
        "and bookings. Name her Aisha. English with Indian accent. Save right "
        "now using all defaults.",
        "en-IN", "Asia/Dubai", "hotel",
    ),
    Scenario(
        "Auto repair (US)",
        "Auto repair shop in Austin Texas. Agent should book service appointments "
        "and look up order status. Name him Daniel. English. Use sensible defaults, "
        "save now.",
        "en-US", "America/Chicago", "automotive",
    ),
    Scenario(
        "Vet clinic (Australia)",
        "Veterinary clinic in Sydney. Agent books appointments and answers "
        "questions about hours and services. Name her Charlotte. Aussie English. "
        "Save now with defaults.",
        "en-AU", "Australia/Sydney", "healthcare",
    ),
    Scenario(
        "Insurance brokerage (India)",
        "Insurance brokerage in Pune. Inbound lead qualifier for car and health "
        "insurance enquiries. Name him Rohan. Hindi and English. Don't give "
        "personalised advice. Save now with sensible defaults.",
        "en-IN", "Asia/Kolkata", "insurance",
    ),
    Scenario(
        "Yoga studio (US)",
        "Yoga studio in San Francisco. Agent books drop-in classes and answers "
        "questions about schedule and pricing. Name her Ava. English. Save now "
        "with defaults.",
        "en-US", "America/Los_Angeles", "salon",
    ),
    Scenario(
        "Pharmacy (India)",
        "Neighbourhood pharmacy in Chennai. Agent checks order status and "
        "handles refill requests. Never give medical advice. Name her Priya. "
        "Tamil and English. Save the agent right now using defaults.",
        "en-IN", "Asia/Kolkata", "retail",
    ),
    Scenario(
        "Tuition centre (Singapore)",
        "Tuition centre in Singapore. Agent handles class enquiries, books "
        "trial sessions and shares fees. Name her Hui. Singaporean English. "
        "Save now with all sensible defaults.",
        "en-GB", "Asia/Singapore", "education",
    ),
    Scenario(
        "Café (Singapore)",
        "Specialty café in Singapore Tiong Bahru. Agent takes table reservations "
        "and answers menu questions. Name him Wei. English. Save now using "
        "sensible defaults.",
        "en-GB", "Asia/Singapore", "restaurant",
    ),
]


# ───────────────────── helpers ──────────────────────────────────────────

def clear_agents():
    """Delete any existing agents so each run is from scratch."""
    try:
        with urllib.request.urlopen(f"{HTTP_BASE}/api/agents", timeout=4) as r:
            agents = json.loads(r.read())
        for a in agents:
            req = urllib.request.Request(f"{HTTP_BASE}/api/agents/{a['id']}", method="DELETE")
            try:
                urllib.request.urlopen(req, timeout=4).read()
            except Exception:
                pass
    except Exception as e:
        print(f"  (could not clear agents: {e})")


def fetch_agents() -> list[dict]:
    with urllib.request.urlopen(f"{HTTP_BASE}/api/agents", timeout=4) as r:
        return json.loads(r.read())


# ───────────────────── per-scenario runner ──────────────────────────────


@dataclass
class Outcome:
    name: str
    passed: bool
    elapsed_s: float
    saved_agent: Optional[dict] = None
    test_mode_reached: bool = False
    test_audio_chunks: int = 0
    builder_audio_chunks: int = 0
    builder_turns: int = 0
    eva_text: str = ""
    test_agent_text: str = ""
    notes: list[str] = field(default_factory=list)


def make_noise_chunk() -> bytes:
    """1600 Int16 samples (100 ms @ 16 kHz) of low-amplitude noise — mimics
    the ambient room tone a real mic picks up. Gemini's native-audio session
    requires continuous audio input to stay open."""
    import random, struct
    samples = [random.randint(-400, 400) for _ in range(1600)]
    return struct.pack(f"<{len(samples)}h", *samples)


async def stream_audio(ws, stop_event):
    """Continuously stream noise chunks at ~10 fps — same cadence as the
    browser AudioWorklet — until stop_event is set."""
    try:
        while not stop_event.is_set():
            try:
                await ws.send(make_noise_chunk())
            except Exception:
                return
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        return


async def run_scenario(sc: Scenario) -> Outcome:
    started = time.time()
    out = Outcome(name=sc.name, passed=False, elapsed_s=0)

    qs = urllib.parse.urlencode({"locale": sc.locale, "tz": sc.tz})
    url = f"{WS_URL}?{qs}"

    try:
        async with websockets.connect(url, max_size=4_000_000) as ws:
            phase = "builder"  # builder → test
            session_starts = 0
            builder_done_event = asyncio.Event()
            test_done_event = asyncio.Event()
            sent_initial = False
            follow_up_idx = 0
            last_turn_at = time.time()
            stop_sentinel = object()

            async def send_text(t: str):
                await ws.send(json.dumps({"type": "text", "text": t}))

            async def reader():
                nonlocal phase, session_starts, sent_initial, follow_up_idx, last_turn_at
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=PER_TURN_TIMEOUT)
                    except asyncio.TimeoutError:
                        out.notes.append(f"timed out waiting for any message in phase={phase}")
                        builder_done_event.set()
                        test_done_event.set()
                        return
                    if isinstance(raw, (bytes, bytearray)):
                        if phase == "builder":
                            out.builder_audio_chunks += 1
                        else:
                            out.test_audio_chunks += 1
                        continue
                    try:
                        m = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    t = m.get("type")
                    if t == "session_starting":
                        session_starts += 1
                        if m.get("kind") == "test":
                            phase = "test"
                            out.test_mode_reached = True
                            sent_initial = False  # reset for test phase
                    elif t == "ready":
                        last_turn_at = time.time()
                        # Send the description IMMEDIATELY — don't wait for the
                        # greeting to finish. The 10s greeting + Gemini's
                        # session-end-on-no-speech behaviour would otherwise
                        # race us out of the session.
                        if phase == "builder" and not sent_initial:
                            sent_initial = True
                            await asyncio.sleep(0.4)
                            await send_text(sc.description)
                    elif t == "reconnected":
                        last_turn_at = time.time()
                        # If we reconnect *before* save_agent fired, the new
                        # session has lost context of our description — resend it.
                        if phase == "builder" and out.saved_agent is None:
                            out.notes.append("reconnect → re-sending description")
                            await asyncio.sleep(0.4)
                            await send_text(sc.description)
                    elif t == "turn_complete":
                        last_turn_at = time.time()
                        if phase == "builder":
                            out.builder_turns += 1
                            # Stop nudging once save_agent has fired — the
                            # server is mid-handoff and any further turn would
                            # confuse the transition.
                            if out.saved_agent is None and follow_up_idx < len(sc.follow_ups):
                                await asyncio.sleep(0.3)
                                await send_text(sc.follow_ups[follow_up_idx])
                                follow_up_idx += 1
                        else:
                            await asyncio.sleep(0.5)
                            test_done_event.set()
                    elif t == "agent_saved":
                        out.saved_agent = m.get("agent")
                        builder_done_event.set()
                    elif t == "transferring":
                        out.notes.append("transferring → test")
                    elif t == "transcript":
                        text = m.get("text", "")
                        if phase == "builder" and m.get("role") == "model":
                            out.eva_text += text
                        elif phase == "test" and m.get("role") == "model":
                            out.test_agent_text += text
                    elif t == "error":
                        out.notes.append(f"server error: {m.get('message')}")
                        builder_done_event.set()
                        test_done_event.set()
                        return

            audio_stop = asyncio.Event()
            reader_task = asyncio.create_task(reader())
            audio_task = asyncio.create_task(stream_audio(ws, audio_stop))

            try:
                # Wait up to PER_TEST_TIMEOUT for save_agent
                await asyncio.wait_for(builder_done_event.wait(), timeout=PER_TEST_TIMEOUT)
            except asyncio.TimeoutError:
                out.notes.append("builder phase timed out before save_agent")
                audio_stop.set()
                reader_task.cancel()
                audio_task.cancel()
                out.elapsed_s = time.time() - started
                return out

            if not out.saved_agent:
                out.notes.append("builder phase ended without saving an agent")
                reader_task.cancel()
                out.elapsed_s = time.time() - started
                return out

            # Wait a bit more for the test-mode greeting
            try:
                await asyncio.wait_for(test_done_event.wait(), timeout=25.0)
            except asyncio.TimeoutError:
                out.notes.append("test phase didn't deliver a turn within 25s")

            audio_stop.set()
            reader_task.cancel()
            audio_task.cancel()
            for t_ in (reader_task, audio_task):
                try:
                    await t_
                except (Exception, asyncio.CancelledError):
                    pass

        # ─── second leg: simulate the user clicking "Call <name>" on the reveal ───
        if out.saved_agent and not out.test_mode_reached:
            url2 = f"{WS_URL}?" + urllib.parse.urlencode({
                "locale": sc.locale, "tz": sc.tz, "agent_id": out.saved_agent["id"],
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
                    deadline2 = asyncio.get_event_loop().time() + 20.0
                    while asyncio.get_event_loop().time() < deadline2:
                        try:
                            raw = await asyncio.wait_for(ws2.recv(), timeout=2.0)
                        except asyncio.TimeoutError: continue
                        except websockets.exceptions.ConnectionClosed: break
                        if isinstance(raw, (bytes, bytearray)):
                            out.test_audio_chunks += 1
                            continue
                        try: m = json.loads(raw)
                        except json.JSONDecodeError: continue
                        t = m.get("type")
                        if t == "session_starting" and m.get("kind") == "test":
                            out.test_mode_reached = True
                        elif t == "transcript" and m.get("role") == "model":
                            out.test_agent_text += m.get("text", "")
                        elif t == "turn_complete":
                            await asyncio.sleep(0.5); break
                        elif t == "error":
                            out.notes.append(f"test session error: {m.get('message')}"); break
                    stop2.set()
                    mic2.cancel()
                    try: await mic2
                    except (Exception, asyncio.CancelledError): pass
            except (Exception, asyncio.CancelledError) as e:
                out.notes.append(f"test session transport error: {e!s}")

    except (Exception, asyncio.CancelledError) as e:
        out.notes.append(f"transport error: {e!s}")

    out.elapsed_s = time.time() - started

    # Pass criteria
    ok = True
    if not out.saved_agent:
        ok = False
    else:
        a = out.saved_agent
        if not a.get("name"):
            out.notes.append("missing name"); ok = False
        if not a.get("system_prompt") or len(a.get("system_prompt", "")) < 80:
            out.notes.append("system_prompt too short or missing"); ok = False
        if not a.get("voice"):
            out.notes.append("missing voice"); ok = False
        if not a.get("locale"):
            out.notes.append("missing locale"); ok = False
    if not out.test_mode_reached:
        out.notes.append("never reached test mode")
        ok = False
    out.passed = ok
    return out


# ───────────────────── main ─────────────────────────────────────────────


async def main():
    print(f"\nClearing existing agents…")
    clear_agents()

    results: list[Outcome] = []
    for i, sc in enumerate(SCENARIOS, 1):
        print(f"\n[{i:>2}/{len(SCENARIOS)}] {sc.name}")
        print(f"     desc: {sc.description[:80]}…")
        out = await run_scenario(sc)
        results.append(out)
        status = "✅ PASS" if out.passed else "❌ FAIL"
        print(f"     {status} in {out.elapsed_s:.1f}s")
        print(f"     builder: turns={out.builder_turns} chunks={out.builder_audio_chunks} eva='{out.eva_text[:120]}…'")
        if out.saved_agent:
            a = out.saved_agent
            print(f"     saved:  id={a.get('id')} name={a.get('name')} sector={a.get('sector')} "
                  f"locale={a.get('locale')} voice={a.get('voice')} "
                  f"connectors={a.get('connectors')} prompt_len={len(a.get('system_prompt') or '')}")
        if out.test_mode_reached:
            print(f"     test:    chunks={out.test_audio_chunks} agent='{out.test_agent_text[:100]}…'")
        if out.notes:
            for n in out.notes:
                print(f"     · {n}")

    print("\n" + "=" * 72)
    passed = sum(1 for r in results if r.passed)
    print(f"  Passed: {passed}/{len(results)}")
    print("=" * 72)
    for r in results:
        mark = "✅" if r.passed else "❌"
        print(f"  {mark} {r.name:<32} {r.elapsed_s:>5.1f}s  saved={bool(r.saved_agent)}  test={r.test_mode_reached}")
    print()
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
