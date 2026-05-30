"""Roleplay a real conversation with Eva over the same WebSocket the browser
uses, and print a clean second-by-second transcript of what happens — every
event, every transcript, every reconnect.

Run while the server is up at 127.0.0.1:8765. The chosen model is whatever
the server is configured with (see /api/health).
"""

from __future__ import annotations

import asyncio
import json
import random
import struct
import sys
import time
import urllib.parse

import websockets

WS_URL = "ws://127.0.0.1:8765/ws/session"
HTTP_BASE = "http://127.0.0.1:8765"

# ── helpers ─────────────────────────────────────────────────────────────

def t0_now():
    return time.monotonic()


class Clock:
    def __init__(self): self.t0 = t0_now()
    def stamp(self) -> str: return f"{t0_now() - self.t0:>5.1f}s"


def log_line(clock: Clock, tag: str, body: str = "", color: str = ""):
    colors = {
        "eva":    "\033[38;5;213m",   # pink
        "agent":  "\033[38;5;220m",   # gold
        "me":     "\033[38;5;39m",    # blue
        "evt":    "\033[38;5;245m",   # grey
        "err":    "\033[38;5;203m",   # coral
        "ok":     "\033[38;5;150m",   # green
        "info":   "\033[38;5;111m",   # soft blue
        "reset":  "\033[0m",
    }
    c = colors.get(color, "")
    r = colors["reset"] if c else ""
    body_part = f"  {body}" if body else ""
    print(f"  [{clock.stamp()}] {c}{tag:<9}{r}{body_part}")


def make_noise_chunk(amp: int = 800) -> bytes:
    """Speech-like noise: pseudo-random amplitude with a low-frequency
    envelope so it doesn't look like pure white noise."""
    samples = []
    for i in range(1600):
        env = 0.4 + 0.6 * abs(((i / 1600) * 4) - 2)
        s = int(random.uniform(-amp, amp) * env)
        samples.append(s)
    return struct.pack(f"<{len(samples)}h", *samples)


# ── roleplay ────────────────────────────────────────────────────────────


async def run_roleplay(scenario: dict):
    clock = Clock()
    qs = urllib.parse.urlencode({"locale": scenario["locale"], "tz": scenario["tz"]})
    url = f"{WS_URL}?{qs}"

    print()
    print("═" * 78)
    print(f"  Scenario: {scenario['title']}")
    print(f"  Locale: {scenario['locale']}   Timezone: {scenario['tz']}")
    print(f"  Model in use (per /api/health): {scenario.get('model','(see server)')}")
    print("═" * 78)

    eva_running_text = ""
    test_running_text = ""
    user_heard_running = ""
    phase = "builder"
    saved_agent = None
    audio_in = 0
    audio_out_builder = 0
    audio_out_test = 0
    drops = 0
    last_announced_eva_len = 0
    last_announced_test_len = 0
    stop_audio = asyncio.Event()

    async with websockets.connect(url, max_size=4_000_000) as ws:
        log_line(clock, "WS", "connected", "info")

        async def stream_mic():
            while not stop_audio.is_set():
                try: await ws.send(make_noise_chunk())
                except Exception: return
                await asyncio.sleep(0.1)

        mic_task = asyncio.create_task(stream_mic())

        # Replies from the "user" (us) — fired off in response to particular
        # Eva moments.
        replies = list(scenario["user_replies"])
        reply_idx = 0
        sent_any = False

        async def maybe_reply(reason: str):
            nonlocal reply_idx, sent_any
            if reply_idx < len(replies):
                msg = replies[reply_idx]
                reply_idx += 1
                sent_any = True
                await asyncio.sleep(0.4)
                await ws.send(json.dumps({"type": "text", "text": msg}))
                log_line(clock, "ME ▶", msg, "me")

        deadline = t0_now() + scenario.get("duration_s", 45)
        while t0_now() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                log_line(clock, "WS", "closed by server", "err")
                break

            if isinstance(raw, (bytes, bytearray)):
                if phase == "builder": audio_out_builder += 1
                else: audio_out_test += 1
                continue

            try: m = json.loads(raw)
            except json.JSONDecodeError: continue
            t = m.get("type")
            if t == "session_starting":
                if m.get("kind") == "test":
                    phase = "test"
                    a = m.get("agent") or {}
                    log_line(clock, "EVENT", f"session_starting kind=test agent={a.get('name')}", "evt")
                else:
                    log_line(clock, "EVENT", f"session_starting kind=builder", "evt")
            elif t == "ready":
                log_line(clock, "READY", f"model={m.get('model')} kind={m.get('kind','builder')}", "info")
                # First reply (the description) goes right after ready
                if phase == "builder" and not sent_any:
                    await maybe_reply("initial")
            elif t == "reconnected":
                drops += 1
                log_line(clock, "DROP", f"#{drops} reconnected (phase={phase})", "err")
                if phase == "builder" and not saved_agent and reply_idx < len(replies):
                    # Re-send the next message in case the prior went to the
                    # dying session
                    await maybe_reply("re-send")
            elif t == "transcript":
                role = m.get("role")
                text = m.get("text", "")
                if role == "model" and phase == "builder":
                    eva_running_text += text
                    if len(eva_running_text) > last_announced_eva_len + 30:
                        excerpt = eva_running_text[last_announced_eva_len:].strip()
                        if excerpt:
                            log_line(clock, "EVA ▼", excerpt, "eva")
                            last_announced_eva_len = len(eva_running_text)
                elif role == "model" and phase == "test":
                    test_running_text += text
                elif role == "user":
                    user_heard_running += text
            elif t == "turn_complete":
                if phase == "builder":
                    if eva_running_text and len(eva_running_text) > last_announced_eva_len:
                        excerpt = eva_running_text[last_announced_eva_len:].strip()
                        if excerpt: log_line(clock, "EVA ▼", excerpt, "eva")
                        last_announced_eva_len = len(eva_running_text)
                    log_line(clock, "EVENT", f"turn_complete (audio_out_chunks={audio_out_builder})", "evt")
                    # Send the next reply after Eva finishes a turn
                    if reply_idx < len(replies) and not saved_agent:
                        await maybe_reply("after-turn")
                else:
                    if test_running_text and len(test_running_text) > last_announced_test_len:
                        excerpt = test_running_text[last_announced_test_len:].strip()
                        if excerpt: log_line(clock, "AGENT ▼", excerpt, "agent")
                        last_announced_test_len = len(test_running_text)
                    log_line(clock, "EVENT", f"turn_complete in test mode (audio_out_chunks={audio_out_test})", "evt")
                    # After we hear the agent greet, end the call.
                    await asyncio.sleep(1.0)
                    break
            elif t == "agent_saved":
                saved_agent = m.get("agent")
                a = saved_agent or {}
                log_line(clock, "SAVED",
                         f"id={a.get('id')} name={a.get('name')} sector={a.get('sector')} "
                         f"locale={a.get('locale')} voice={a.get('voice')} "
                         f"connectors={a.get('connectors')} prompt_len={len(a.get('system_prompt') or '')}",
                         "ok")
            elif t == "transferring":
                log_line(clock, "EVENT", "transferring → test mode", "info")
            elif t == "tool_call":
                log_line(clock, "TOOL", f"{m.get('name')} — {m.get('label','')[:60]}", "evt")
            elif t == "error":
                log_line(clock, "ERROR", m.get("message",""), "err")
                break
            elif t == "go_away":
                log_line(clock, "EVENT", "go_away from Gemini", "err")
            elif t == "interrupted":
                log_line(clock, "EVENT", "interrupted (model audio cut)", "evt")
            # else: unknown event, ignore

        stop_audio.set()
        mic_task.cancel()
        try: await mic_task
        except (Exception, asyncio.CancelledError): pass

    print()
    print("─" * 78)
    print(f"  Result")
    print(f"    drops:           {drops}")
    print(f"    audio chunks in: {audio_in if audio_in else '(silent mic test)'} (we streamed continuous noise)")
    print(f"    builder audio chunks out: {audio_out_builder}")
    if phase == "test":
        print(f"    test    audio chunks out: {audio_out_test}")
    if saved_agent:
        a = saved_agent
        print(f"    saved agent:     id={a.get('id')} {a.get('name')} ({a.get('sector')} / {a.get('locale')} / {a.get('voice')})")
    print(f"    USER heard text: {user_heard_running!r}")
    print(f"    Eva text total:  {eva_running_text!r}")
    if test_running_text:
        print(f"    Agent text:      {test_running_text!r}")
    print("─" * 78)


# ── scenarios ───────────────────────────────────────────────────────────


SCENARIOS = [
    {
        "title": "Build a dental-clinic agent (India)",
        "locale": "en-IN", "tz": "Asia/Kolkata",
        "user_replies": [
            "Hi Eva! I want a phone agent for my dental clinic in Bangalore. Bookings and reschedules. Use Hindi as the main language, English when needed. Name her Maya. Never give medical advice.",
            "Yes go ahead, save her now with all the sensible defaults you usually pick.",
        ],
        "duration_s": 50,
    },
    {
        "title": "Build a hotel receptionist (US)",
        "locale": "en-US", "tz": "America/New_York",
        "user_replies": [
            "Hi Eva. I run a small boutique hotel in Manhattan. I need an agent that handles room availability and bookings. Name her Sofia. English. Save now with defaults please.",
            "Yes, all defaults fine. Save now.",
        ],
        "duration_s": 50,
    },
]


async def main():
    import urllib.request
    try:
        with urllib.request.urlopen(f"{HTTP_BASE}/api/health", timeout=3) as r:
            health = json.loads(r.read())
        model = health.get("model")
    except Exception as e:
        print(f"Could not reach server: {e}")
        sys.exit(1)

    for sc in SCENARIOS:
        sc["model"] = model
        try:
            await run_roleplay(sc)
        except (Exception, asyncio.CancelledError) as e:
            print(f"\n  scenario errored: {e}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
