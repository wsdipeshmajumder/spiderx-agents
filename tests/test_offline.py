"""Offline regression suite — pure unit tests, NO server / DB / API keys.

Runs in ~1s with `python tests/test_offline.py` (or via unittest discovery).
This is the CI-safe, always-runnable half of the eval story: it guards the
code that has no HTTP surface and so is invisible to `eval_suite.py` — the
Fish Audio voice pipeline (audio codecs, sentence flushing, the live-call
_bridge branch + its degrade-to-Gemini safety net), the fish_audio client
shape, and the build-number lockstep convention.

`eval_suite.py` covers the live HTTP/WS surface (needs a running server);
this covers the in-process logic. Together they are the regression gate wired
into `.githooks/pre-push` and `.github/workflows/evals.yml`.

Design rule: every fixture is synthesized in-code (WAV tones, fake carrier/
session objects) so there are zero external dependencies — no network, no
/tmp files, no Fish credit, no Gemini key.
"""
from __future__ import annotations

import asyncio
import io
import math
import queue
import re
import struct
import sys
import types
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _ensure_loop() -> None:
    """py3.9 binds a loop lazily via get_event_loop(); asyncio.run() closes it
    and leaves the main thread with none, which breaks a later asyncio.Lock()/
    Queue() constructed at import time. Guarantee a usable loop. No-op semantics
    on py3.10+ (incl. the 3.13 prod/CI runtime), where construction never
    touches the loop."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

import audioop  # noqa: E402  (audioop-lts on py3.13)

from backend.telephony.audio import (  # noqa: E402
    chunk_ulaw,
    pcm16_resample,
    pcm16_to_ulaw8k,
    pcm24k_to_ulaw,
    ulaw_to_pcm16k,
    wav_to_pcm16_mono,
)
from backend.telephony.base import (  # noqa: E402
    TelephonyProvider,
    WsStart,
    _drain_queue,
    _fish_flush,
    _bridge,
)


# ─── fixtures ────────────────────────────────────────────────────────────


def make_pcm16(rate: int, secs: float, freq: int = 220) -> bytes:
    """Mono PCM16 sine tone."""
    n = int(rate * secs)
    return b"".join(struct.pack("<h", int(math.sin(2 * math.pi * freq * i / rate) * 20000))
                     for i in range(n))


def make_wav(rate: int = 44100, secs: float = 0.4, channels: int = 1,
             width: int = 2, freq: int = 220) -> bytes:
    """A self-contained WAV blob shaped like Fish's TTS output."""
    n = int(rate * secs)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            val = math.sin(2 * math.pi * freq * i / rate)
            if width == 2:
                sample = struct.pack("<h", int(val * 20000))
            elif width == 1:
                sample = struct.pack("B", int(val * 100) + 128)
            else:
                raise ValueError("test supports width 1 or 2")
            frames += sample * channels
        w.writeframes(bytes(frames))
    return buf.getvalue()


# ─── 1. telephony audio codecs ───────────────────────────────────────────


class TestAudioCodecs(unittest.TestCase):
    def test_ulaw_to_pcm16k_upsamples_2x(self):
        pcm8k = make_pcm16(8000, 0.02)          # 160 samples
        ulaw = audioop.lin2ulaw(pcm8k, 2)       # 160 bytes
        pcm16k, state = ulaw_to_pcm16k(ulaw, None)
        # 160 @8k → ~320 @16k → ~640 bytes (ratecv boundary slack).
        self.assertAlmostEqual(len(pcm16k), 640, delta=8)
        self.assertIsNotNone(state)
        # State threads without raising on the next frame.
        pcm16k2, _ = ulaw_to_pcm16k(ulaw, state)
        self.assertTrue(pcm16k2)

    def test_pcm24k_to_ulaw_downsamples_to_8k(self):
        pcm24k = make_pcm16(24000, 0.02)        # 480 samples
        ulaw, state = pcm24k_to_ulaw(pcm24k, None)
        self.assertAlmostEqual(len(ulaw), 160, delta=8)  # 0.02s @ 8k µ-law

    def test_wav_mono_16bit_roundtrip(self):
        pcm, rate = wav_to_pcm16_mono(make_wav(rate=44100, secs=0.1))
        self.assertEqual(rate, 44100)
        self.assertAlmostEqual(len(pcm) // 2, 4410, delta=2)

    def test_wav_stereo_collapses_to_mono(self):
        pcm, rate = wav_to_pcm16_mono(make_wav(rate=24000, secs=0.1, channels=2))
        self.assertEqual(rate, 24000)
        self.assertAlmostEqual(len(pcm) // 2, 2400, delta=2)  # mono sample count

    def test_wav_8bit_normalised_to_16bit(self):
        pcm, rate = wav_to_pcm16_mono(make_wav(rate=16000, secs=0.1, width=1))
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(len(pcm) // 2, 1600, delta=2)  # width doubled to 16-bit

    def test_wav_garbage_raises(self):
        with self.assertRaises(Exception):
            wav_to_pcm16_mono(b"not a wav at all")

    def test_pcm16_to_ulaw8k(self):
        pcm = make_pcm16(44100, 0.1)
        ulaw = pcm16_to_ulaw8k(pcm, 44100)
        self.assertAlmostEqual(len(ulaw), 800, delta=16)  # 0.1s @ 8k

    def test_pcm16_to_ulaw8k_noop_when_already_8k(self):
        pcm = make_pcm16(8000, 0.1)
        ulaw = pcm16_to_ulaw8k(pcm, 8000)
        self.assertEqual(len(ulaw), 800)

    def test_pcm16_resample_ratio(self):
        pcm = make_pcm16(44100, 0.1)
        out = pcm16_resample(pcm, 44100, 24000)
        self.assertAlmostEqual(len(out) // 2, 2400, delta=8)
        # identity path returns input unchanged
        self.assertEqual(pcm16_resample(pcm, 24000, 24000), pcm)

    def test_chunk_ulaw_frames(self):
        frames = chunk_ulaw(b"\x00" * 325, 160)
        self.assertEqual([len(f) for f in frames], [160, 160, 5])


# ─── 2. Fish sentence flushing + barge-in ────────────────────────────────


class TestFishFlush(unittest.TestCase):
    def _collect(self, q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    def test_sentence_boundaries_flush_incrementally(self):
        # A stdlib queue.Queue exposes the same put_nowait/get_nowait/empty/qsize
        # the flush helpers use, and needs no event loop (loop-agnostic test).
        q = queue.Queue()
        fx = {"active": True, "gen": 0, "text": ""}
        for delta in ["Hello there", "! How can ", "I help you", " today? and more"]:
            fx["text"] += delta
            _fish_flush(q, fx, final=False)
        segs = [s for s, _ in self._collect(q)]
        self.assertEqual(segs, ["Hello there!", "How can I help you today?"])
        self.assertEqual(fx["text"].strip(), "and more")   # tail stays buffered

    def test_final_flush_emits_tail(self):
        q = queue.Queue()
        fx = {"active": True, "gen": 3, "text": "no terminator here"}
        _fish_flush(q, fx, final=True)
        seg, gen = q.get_nowait()
        self.assertEqual(seg, "no terminator here")
        self.assertEqual(gen, 3)                     # tagged with current generation
        self.assertEqual(fx["text"], "")

    def test_long_fragment_force_flushes_without_punctuation(self):
        q = queue.Queue()
        fx = {"active": True, "gen": 0, "text": "word " * 60}  # >200 chars, no '.'
        _fish_flush(q, fx, final=False)
        self.assertEqual(q.qsize(), 1)               # flushed despite no boundary

    def test_short_fragment_waits(self):
        q = queue.Queue()
        fx = {"active": True, "gen": 0, "text": "still going"}
        _fish_flush(q, fx, final=False)
        self.assertEqual(q.qsize(), 0)               # no boundary, under length cap → hold

    def test_drain_queue_empties(self):
        q = queue.Queue()
        for i in range(5):
            q.put_nowait((f"s{i}", 0))
        _drain_queue(q)
        self.assertEqual(q.qsize(), 0)


# ─── 3. Live-call _bridge Fish branch (fakes) ────────────────────────────


class _FakeProvider(TelephonyProvider):
    name = "faketel"
    display_name = "FakeTel"

    def answer_xml(self, **k):
        return ("", "")

    def parse_ws_message(self, raw):
        return None

    def encode_outbound_audio(self, *, stream_id, ulaw_frame):
        return {"event": "media", "sid": stream_id, "n": len(ulaw_frame)}

    def parse_hangup_webhook(self, form):
        return {}

    def clear_outbound(self, *, stream_id):
        return {"event": "clear"}


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def receive_text(self):
        await asyncio.sleep(3600)   # park; the call ends when the session drains

    async def send_text(self, s):
        self.sent.append(s)


class _FakeSession:
    def __init__(self, script):
        self.script = script

    async def send_client_content(self, **k):
        pass

    async def send_realtime_input(self, **k):
        pass

    async def send_tool_response(self, **k):
        pass

    async def receive(self):
        for item in self.script:
            await asyncio.sleep(0.02)
            yield item


def _resp(*, ot=None, it=None, audio=None, turn_complete=False, interrupted=False):
    sc = types.SimpleNamespace(
        interrupted=interrupted,
        input_transcription=(types.SimpleNamespace(text=it) if it else None),
        output_transcription=(types.SimpleNamespace(text=ot) if ot else None),
        turn_complete=turn_complete,
        model_turn=(types.SimpleNamespace(parts=[types.SimpleNamespace(
            inline_data=types.SimpleNamespace(data=audio))]) if audio else None),
    )
    return types.SimpleNamespace(server_content=sc, session_resumption_update=None,
                                 go_away=None, tool_call=None)


class TestBridgeFishPath(unittest.TestCase):
    def _run(self, voice_provider, synth_impl, *, fish_voice_id="voice-abc123"):
        from backend import fish_audio
        from backend.gemini_bridge import _ConversationMemory
        orig_synth, orig_cfg = fish_audio.synthesize, fish_audio.is_configured
        fish_audio.synthesize = synth_impl
        fish_audio.is_configured = lambda: True
        try:
            agent = {"id": 1, "voice_tweaks": {"voice_provider": voice_provider,
                                                "fish_voice_id": fish_voice_id},
                     "recording_enabled": False}
            ws, provider = _FakeWS(), _FakeProvider()
            call_state = {"stream_id": "SID", "persisted": False}
            script = [
                _resp(ot="Hello there! How can I help you today?"),
                _resp(audio=b"\x01\x02" * 2400),     # Gemini's own voice (0.1s @24k)
                _resp(turn_complete=True),
                _resp(ot="Second sentence here."),
                _resp(audio=b"\x01\x02" * 2400),
                _resp(turn_complete=True),
            ]

            async def go():
                await asyncio.wait_for(_bridge(
                    ws, provider, _FakeSession(script), agent, [],
                    memory=_ConversationMemory(), caller_done=asyncio.Event(),
                    send_kickoff=False, call_state=call_state), timeout=15)
            asyncio.run(go())
            return sum(1 for s in ws.sent if '"event": "media"' in s)
        finally:
            fish_audio.synthesize, fish_audio.is_configured = orig_synth, orig_cfg
            # asyncio.run() closed the main-thread loop; restore one so later
            # tests (import-time asyncio.Lock/Queue on py3.9) still work.
            _ensure_loop()

    def test_fish_drives_audio_and_suppresses_gemini(self):
        wav = make_wav(rate=44100, secs=0.4)         # 0.4s → ~20 µ-law frames/sentence
        async def good(text, **k):
            return wav
        n = self._run("fish", good)
        # 2 sentences ≈ 40 Fish frames; Gemini's 0.1s clips (~5 frames each) are
        # suppressed. A high count proves Fish drove the audio.
        self.assertGreater(n, 32, "Fish audio should dominate; Gemini suppressed")

    def test_synth_failure_degrades_to_gemini_voice(self):
        from backend import fish_audio
        async def boom(text, **k):
            raise fish_audio.FishAudioError("boom", status=502)
        n = self._run("fish", boom)
        # Fish emits nothing; the call falls back to Gemini's own audio (small
        # frame count) instead of going silent — never break a call.
        self.assertTrue(0 < n < 32, f"expected Gemini fallback frames, got {n}")

    def test_gemini_provider_path_unchanged(self):
        async def good(text, **k):
            return make_wav()
        n = self._run("gemini", good)
        self.assertTrue(0 < n < 32, "gemini path streams Gemini audio as before")

    def test_no_fish_voice_id_falls_back_to_gemini(self):
        # Build 416 regression: a real production call (agent 5 "Mira")
        # had voice_provider defaulting to "fish" with no fish_voice_id
        # ever chosen. Fish must NOT activate in that shape — it must fall
        # back to Gemini's own voice, exactly like test_gemini_provider_
        # path_unchanged above, not synthesize with reference_id omitted
        # (which is what caused the reported "voice changed every time,
        # 2-way AI talking" bug: Fish's free backbone doesn't reliably pick
        # the same voice per request without one).
        async def good(text, **k):
            return make_wav()
        n = self._run("fish", good, fish_voice_id="")
        self.assertTrue(0 < n < 32, f"expected Gemini fallback frames, got {n}")


class _FakeWSBrowser:
    """Minimal WebSocket fake for the browser voice-test path. Distinct from
    _FakeWS above (shaped for telephony's send_text-only carrier protocol) —
    _pump_gemini_to_client / _fish_player_for_ws call send_bytes (binary PCM
    frames) and read client_state (the self-termination check), neither of
    which _FakeWS supports."""

    def __init__(self):
        from starlette.websockets import WebSocketState
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.client_state = WebSocketState.CONNECTED

    async def send_text(self, s):
        self.sent_text.append(s)

    async def send_bytes(self, b):
        self.sent_bytes.append(b)


class TestBrowserFishPath(unittest.TestCase):
    """build 410 — Fish Audio now also drives live audio on the BROWSER
    voice-test path (gemini_bridge.run_session), not only telephony. Same
    queue/gen protocol as TestBridgeFishPath above, adapted to the browser's
    raw PCM16@24kHz wire format (_send_bytes) instead of telephony's mu-law
    carrier frames. Explicit ask: "based on the agent voice model selected,
    the web demo shud work for both providers" — previously the browser
    ALWAYS played Gemini's own voice regardless of a saved agent's
    voice_tweaks.voice_provider, so "Test in your browser" never actually
    previewed what a Fish-voiced agent (the platform-wide default) sounds
    like on a real call. This closes that gap."""

    def _run(self, voice_provider, synth_impl, *, fish_voice_id=""):
        from backend import fish_audio
        from backend.gemini_bridge import (
            _ConversationMemory, _SessionState, _Handoff,
            _pump_gemini_to_client, _fish_player_for_ws,
        )
        orig_synth, orig_cfg = fish_audio.synthesize, fish_audio.is_configured
        fish_audio.synthesize = synth_impl
        fish_audio.is_configured = lambda: True
        try:
            ws = _FakeWSBrowser()
            script = [
                _resp(ot="Hello there! How can I help you today?"),
                _resp(audio=b"\x01\x02" * 2400),     # Gemini's own voice (0.1s @24k = 4800 bytes)
                _resp(turn_complete=True),
                _resp(ot="Second sentence here."),
                _resp(audio=b"\x01\x02" * 2400),
                _resp(turn_complete=True),
            ]
            session = _FakeSession(script)
            state = _SessionState()
            memory = _ConversationMemory()
            handoff = _Handoff()
            fx = {"active": voice_provider == "fish", "gen": 0, "text": ""}
            agent = {"id": 1, "_recording_writer": None}

            async def _noop(*a, **k):
                return {"ok": True}

            async def go():
                # stop/fish_q are asyncio primitives — must be constructed
                # INSIDE the loop that will await them (asyncio.run() below
                # always spins up a fresh loop; a Queue/Event built outside
                # it in this test process, after prior tests already left an
                # older loop as "current", raises "attached to a different
                # loop" under Python 3.9's eager loop-binding). Production
                # code doesn't hit this — run_session already runs inside
                # FastAPI's one long-lived loop, so its `asyncio.Queue()` is
                # always constructed "inside" the right loop already.
                stop = asyncio.Event()
                fish_q: "asyncio.Queue" = asyncio.Queue()
                pump = asyncio.create_task(_pump_gemini_to_client(
                    ws, session, stop, state, memory,
                    handoff=handoff, on_save_agent=_noop, on_select_agent=_noop,
                    on_connector_call=_noop, fx=fx, fish_q=fish_q,
                ))
                fish_task = None
                if fx["active"]:
                    fish_task = asyncio.create_task(
                        _fish_player_for_ws(ws, fish_q, fx, agent, fish_voice_id or None)
                    )
                await asyncio.wait_for(pump, timeout=15)
                if fish_task is not None:
                    await asyncio.sleep(0.3)   # let queued Fish segments finish playing
                    fish_q.put_nowait(None)
                    await asyncio.wait_for(fish_task, timeout=5)

            asyncio.run(go())
            return sum(len(b) for b in ws.sent_bytes)
        finally:
            fish_audio.synthesize, fish_audio.is_configured = orig_synth, orig_cfg
            _ensure_loop()

    def test_fish_drives_audio_and_suppresses_gemini(self):
        wav = make_wav(rate=44100, secs=0.4)
        async def good(text, **k):
            return wav
        n = self._run("fish", good)
        # 2 sentences @ 0.4s, resampled to PCM16@24kHz ≈ 2 * 19200 bytes.
        # Gemini's own two 4800-byte clips (9600 total) would be far smaller
        # if NOT suppressed — a high count proves Fish drove the audio.
        self.assertGreater(n, 20000, "Fish audio should dominate; Gemini suppressed")

    def test_synth_failure_degrades_to_gemini_voice(self):
        from backend import fish_audio
        async def boom(text, **k):
            raise fish_audio.FishAudioError("boom", status=502)
        n = self._run("fish", boom)
        # Fish emits nothing; falls back to exactly Gemini's own two
        # 4800-byte clips instead of going silent — never break a call.
        self.assertEqual(n, 9600, f"expected exactly Gemini's own audio bytes, got {n}")

    def test_gemini_provider_path_unchanged(self):
        async def good(text, **k):
            return make_wav()
        n = self._run("gemini", good)
        self.assertEqual(n, 9600, "gemini path streams Gemini audio unchanged")

    def test_fx_none_defaults_to_inactive(self):
        # Callers that don't pass fx/fish_q at all (run_helper_session) must
        # behave exactly as before this build — no Fish branching at all.
        from backend.gemini_bridge import _ConversationMemory, _SessionState, _Handoff, _pump_gemini_to_client
        ws = _FakeWSBrowser()
        script = [_resp(ot="Hi"), _resp(audio=b"\x01\x02" * 2400), _resp(turn_complete=True)]
        session = _FakeSession(script)
        state = _SessionState()
        memory = _ConversationMemory()
        handoff = _Handoff()

        async def _noop(*a, **k):
            return {"ok": True}

        async def go():
            stop = asyncio.Event()   # constructed inside the loop — see TestBrowserFishPath._run
            await asyncio.wait_for(_pump_gemini_to_client(
                ws, session, stop, state, memory,
                handoff=handoff, on_save_agent=_noop, on_select_agent=_noop,
                on_connector_call=_noop,
            ), timeout=15)

        asyncio.run(go())
        _ensure_loop()
        self.assertEqual(sum(len(b) for b in ws.sent_bytes), 4800)

    def test_interrupted_bumps_fish_gen_and_drains_queue(self):
        from backend.gemini_bridge import _ConversationMemory, _SessionState, _Handoff, _pump_gemini_to_client
        ws = _FakeWSBrowser()
        script = [
            _resp(ot="Partial sentence without a stop"),
            _resp(interrupted=True),
            _resp(turn_complete=True),
        ]
        session = _FakeSession(script)
        state = _SessionState()
        memory = _ConversationMemory()
        handoff = _Handoff()
        fx = {"active": True, "gen": 0, "text": ""}
        result: dict[str, Any] = {}

        async def _noop(*a, **k):
            return {"ok": True}

        async def go():
            stop = asyncio.Event()   # constructed inside the loop — see TestBrowserFishPath._run
            fish_q: "asyncio.Queue" = asyncio.Queue()
            fish_q.put_nowait(("stale segment", 0))  # pretend something was already queued
            await asyncio.wait_for(_pump_gemini_to_client(
                ws, session, stop, state, memory,
                handoff=handoff, on_save_agent=_noop, on_select_agent=_noop,
                on_connector_call=_noop, fx=fx, fish_q=fish_q,
            ), timeout=15)
            result["queue_empty"] = fish_q.empty()

        asyncio.run(go())
        _ensure_loop()
        self.assertEqual(fx["gen"], 1)
        self.assertEqual(fx["text"], "")
        self.assertTrue(result["queue_empty"])

    def test_fish_player_self_terminates_when_ws_already_disconnected(self):
        from starlette.websockets import WebSocketState
        from backend.gemini_bridge import _fish_player_for_ws
        ws = _FakeWSBrowser()
        ws.client_state = WebSocketState.DISCONNECTED
        fx = {"active": True, "gen": 0, "text": ""}

        async def go():
            fish_q: "asyncio.Queue" = asyncio.Queue()   # constructed inside the loop
            await asyncio.wait_for(_fish_player_for_ws(ws, fish_q, fx, {"id": 1}, None), timeout=2)

        asyncio.run(go())
        _ensure_loop()

    def test_fish_player_stops_on_none_sentinel(self):
        from backend.gemini_bridge import _fish_player_for_ws
        ws = _FakeWSBrowser()
        fx = {"active": True, "gen": 0, "text": ""}

        async def go():
            fish_q: "asyncio.Queue" = asyncio.Queue()   # constructed inside the loop
            fish_q.put_nowait(None)
            await asyncio.wait_for(_fish_player_for_ws(ws, fish_q, fx, {"id": 1}, None), timeout=2)

        asyncio.run(go())
        _ensure_loop()


class TestPostReconnectSuppression(unittest.TestCase):
    """build 419 — the code-level backstop behind build 418's prompt fix.
    Root cause (confirmed via production Railway logs on call 337/338):
    gemini-3.1-flash-live-preview has a Google-acknowledged, still-open bug
    (googleapis/python-genai#2580) ignoring our automatic_activity_detection.
    silence_duration_ms config, so session.receive() exhausts after roughly
    one turn far more often than the "few minutes" the reconnect machinery
    was designed around — every ~5-10s on a silent test call. Build 418's
    "stay COMPLETELY SILENT" reconnect instruction helps but a live call
    still showed the model speaking anyway on a later reconnect — LLM
    instruction-following isn't 100% reliable, especially against a model
    with confirmed timing bugs. This is the guarantee: whatever the model
    says right after a reconnect is discarded in code — not queued, not
    played, not persisted — until genuine caller speech (`input_transcription`)
    actually arrives, no matter what the model does or doesn't obey."""

    def _run_turn(self, *, suppressed_at_start, script):
        from backend.gemini_bridge import _ConversationMemory, _SessionState, _Handoff, _pump_gemini_to_client
        ws = _FakeWSBrowser()
        session = _FakeSession(script)
        state = _SessionState()
        state.suppress_until_real_input = suppressed_at_start
        memory = _ConversationMemory()
        handoff = _Handoff()

        async def _noop(*a, **k):
            return {"ok": True}

        async def go():
            stop = asyncio.Event()
            await asyncio.wait_for(_pump_gemini_to_client(
                ws, session, stop, state, memory,
                handoff=handoff, on_save_agent=_noop, on_select_agent=_noop,
                on_connector_call=_noop,
            ), timeout=15)

        asyncio.run(go())
        _ensure_loop()
        return ws, state, memory

    def test_phantom_turn_after_reconnect_produces_no_audio(self):
        script = [
            _resp(ot="Umm, let me just check that for you."),
            _resp(audio=b"\x01\x02" * 2400),
            _resp(turn_complete=True),
        ]
        ws, state, memory = self._run_turn(suppressed_at_start=True, script=script)
        self.assertEqual(ws.sent_bytes, [], "no audio should reach the client while suppressed")
        self.assertEqual(memory.turns, [], "a suppressed phantom turn must not enter conversation memory")
        self.assertTrue(state.suppress_until_real_input,
                         "the model's own output must never lift suppression")

    def test_real_caller_speech_lifts_suppression_and_next_turn_plays(self):
        script = [
            _resp(it="Want to know more."),
            _resp(ot="Sure, let me check that for you."),
            _resp(audio=b"\x01\x02" * 2400),
            _resp(turn_complete=True),
        ]
        ws, state, memory = self._run_turn(suppressed_at_start=True, script=script)
        self.assertFalse(state.suppress_until_real_input, "real input_transcription must clear it")
        self.assertEqual(ws.sent_bytes, [b"\x01\x02" * 2400], "the response to REAL input must play")
        self.assertEqual([t["role"] for t in memory.turns], ["user", "model"])

    def test_not_suppressed_by_default_unchanged_behavior(self):
        script = [
            _resp(ot="Hi there."),
            _resp(audio=b"\x01\x02" * 2400),
            _resp(turn_complete=True),
        ]
        ws, state, memory = self._run_turn(suppressed_at_start=False, script=script)
        self.assertEqual(ws.sent_bytes, [b"\x01\x02" * 2400])
        self.assertEqual([t["role"] for t in memory.turns], ["model"])


# ─── 4. fish_audio client shape ──────────────────────────────────────────


class TestFishAudioClient(unittest.TestCase):
    def test_default_model_is_free_backbone(self):
        from backend import fish_audio
        self.assertEqual(fish_audio.FISH_TTS_MODEL, "s2.1-pro-free")

    def test_is_configured_reflects_env(self):
        import os
        from backend import fish_audio
        old = os.environ.pop("FISH_AUDIO_API_KEY", None)
        try:
            self.assertFalse(fish_audio.is_configured())
            os.environ["FISH_AUDIO_API_KEY"] = "sk-fish-test"
            self.assertTrue(fish_audio.is_configured())
        finally:
            os.environ.pop("FISH_AUDIO_API_KEY", None)
            if old is not None:
                os.environ["FISH_AUDIO_API_KEY"] = old

    def test_default_voices_shape(self):
        from backend import fish_audio
        self.assertTrue(fish_audio.DEFAULT_VOICES)
        for v in fish_audio.DEFAULT_VOICES:
            self.assertIn("id", v)
            self.assertIn("label", v)

    def test_error_carries_status(self):
        from backend import fish_audio
        e = fish_audio.FishAudioError("nope", status=402)
        self.assertEqual(e.status, 402)


class TestResolveVoiceEngine(unittest.TestCase):
    """build 416 — the exact regression a real production call surfaced
    (agent 5 'Mira', reported: kept talking on its own, wouldn't let the
    caller in, voice changed every time — a garbled two-source mix). Root
    cause: voice_provider defaults to "fish" for every agent that never
    touched the field, and Fish was activating with no fish_voice_id set —
    reference_id omitted, so Fish's free backbone doesn't reliably pick the
    same voice per request, and an intermittent synth failure flips the
    existing degrade-to-Gemini safety net mid-call. `resolve_voice_engine`
    is now the ONE place both live-call paths (telephony + browser test)
    resolve this, so they can't drift out of sync on the fix again."""

    def setUp(self):
        from backend import fish_audio
        self.fa = fish_audio
        self._orig_is_configured = fish_audio.is_configured
        fish_audio.is_configured = lambda: True

    def tearDown(self):
        self.fa.is_configured = self._orig_is_configured

    def test_fish_with_voice_id_activates(self):
        active, voice_id = self.fa.resolve_voice_engine(
            {"voice_provider": "fish", "fish_voice_id": "abc123"})
        self.assertTrue(active)
        self.assertEqual(voice_id, "abc123")

    def test_fish_without_voice_id_does_not_activate(self):
        active, voice_id = self.fa.resolve_voice_engine(
            {"voice_provider": "fish", "fish_voice_id": ""})
        self.assertFalse(active)
        self.assertIsNone(voice_id)

    def test_missing_voice_tweaks_defaults_to_fish_but_no_voice_id_so_inactive(self):
        # The exact agent-5 "Mira" shape: voice_tweaks present but with no
        # voice_provider/fish_voice_id keys at all (never touched).
        active, voice_id = self.fa.resolve_voice_engine(
            {"ambience": "quiet", "sensitivity": "low"})
        self.assertFalse(active)
        self.assertIsNone(voice_id)

    def test_none_voice_tweaks_is_safe(self):
        active, voice_id = self.fa.resolve_voice_engine(None)
        self.assertFalse(active)
        self.assertIsNone(voice_id)

    def test_explicit_gemini_provider_never_activates_fish(self):
        active, voice_id = self.fa.resolve_voice_engine(
            {"voice_provider": "gemini", "fish_voice_id": "abc123"})
        self.assertFalse(active)

    def test_not_configured_overrides_everything(self):
        self.fa.is_configured = lambda: False
        active, _ = self.fa.resolve_voice_engine(
            {"voice_provider": "fish", "fish_voice_id": "abc123"})
        self.assertFalse(active)


# ─── 5. build-number lockstep convention ─────────────────────────────────


class TestBuildLockstep(unittest.TestCase):
    def _grep(self, path, pattern):
        m = re.search(pattern, (REPO / path).read_text())
        self.assertIsNotNone(m, f"{pattern!r} not found in {path}")
        return int(m.group(1))

    def test_app_and_frontend_builds_match(self):
        app = self._grep("backend/app.py", r"APP_BUILD\s*=\s*(\d+)")
        sxai = self._grep("frontend/app.js", r"SXAI_BUILD\s*=\s*(\d+)")
        self.assertEqual(app, sxai, "APP_BUILD and SXAI_BUILD must stay in lockstep")

    def test_claude_md_records_current_build(self):
        app = self._grep("backend/app.py", r"APP_BUILD\s*=\s*(\d+)")
        claude = self._grep("CLAUDE.md", r"Current build:\s*\*\*(\d+)\*\*")
        self.assertEqual(app, claude, "CLAUDE.md 'Current build' must match APP_BUILD")

    def test_rubric_last_updated_matches_build(self):
        app = self._grep("backend/app.py", r"APP_BUILD\s*=\s*(\d+)")
        rubric = self._grep("EVAL_RUBRIC.md", r"Last updated: build (\d+)")
        self.assertEqual(app, rubric, "EVAL_RUBRIC.md 'Last updated: build N' must "
                                      "match APP_BUILD (update the rubric on every push)")

    def test_index_uses_build_placeholder(self):
        idx = (REPO / "frontend/index.html").read_text()
        self.assertIn("app.js?v={BUILD}", idx)
        self.assertIn("styles.css?v={BUILD}", idx)


class TestStaticImportCacheBust(unittest.TestCase):
    """build 417 incident: app.js imports audio-engine.js via a STATIC ES
    `import` with its OWN manually-maintained `?v=N` query param — separate
    from and NOT covered by the SXAI_BUILD/APP_BUILD lockstep above (a
    static `import` specifier must be a string literal, so it can't
    reference the SXAI_BUILD constant directly). build 410 edited
    audio-engine.js (the mic-echo-gate fix for "agent keeps talking on its
    own") and bumped SXAI_BUILD — but NOT audio-engine.js's own `?v=23`,
    so any browser with that exact URL already cached kept running the
    PRE-FIX file indefinitely. A live tester reproduced the bug on build
    416 while deliberately staying silent specifically to verify the fix —
    it hadn't actually reached their browser. This test hashes
    audio-engine.js's content against a known snapshot: if the file
    changes, the hash mismatches, forcing whoever's editing it to ALSO
    bump `?v=N` in app.js's import line — a build check, not just a
    comment someone can miss."""

    def test_audio_engine_content_change_forces_a_version_bump(self):
        import hashlib
        content = (REPO / "frontend/audio-engine.js").read_text()
        actual = hashlib.sha256(content.encode()).hexdigest()[:16]
        # Snapshot as of build 417 (?v=24 in app.js). If this assertion
        # fails: you changed audio-engine.js — bump `?v=N` in app.js's
        # `import { AudioEngine } from "/static/audio-engine.js?v=N"` AND
        # update the hash below to the new content's hash.
        expected = "66b2886eca837f15"
        self.assertEqual(actual, expected,
            "audio-engine.js changed but its cache-bust wasn't updated — "
            "bump `?v=N` in frontend/app.js's import AND this test's "
            "expected hash, or browsers with a cached copy of the old URL "
            "will keep running stale code (see class docstring).")

    def test_audio_engine_import_has_a_version_query_param(self):
        app_js = (REPO / "frontend/app.js").read_text()
        m = re.search(r'audio-engine\.js\?v=(\d+)', app_js)
        self.assertIsNotNone(m, "audio-engine.js import lost its ?v=N cache-bust")


# ─── 6. import sanity (catches import-time breakage before deploy) ───────


class TestVoiceProviderMigration(unittest.TestCase):
    """Guard the engine-aware ledger migration (build 379) stays well-formed."""

    def _mig(self):
        return (REPO / "backend/alembic/versions/0034_voice_provider.py").read_text()

    def test_chained_on_prev_head(self):
        self.assertIn('down_revision = "0033_caller_number"', self._mig())

    def test_stamps_engine_on_calls_and_ledger(self):
        m = self._mig()
        self.assertIn("ALTER TABLE calls ADD COLUMN IF NOT EXISTS voice_provider", m)
        self.assertIn("ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS voice_provider", m)

    def test_seeds_zero_fish_pricing_dimension(self):
        m = self._mig()
        self.assertIn("'fish'", m)
        self.assertIn("tts.pro.voice", m)  # ₹0 dimension, rolled forward later


class TestGeoipProvenance(unittest.TestCase):
    """Build 391 — city/country chip. Only the offline-safe half: the
    X-Forwarded-For parse and the private/loopback/invalid short-circuit
    (no real network call, per this suite's no-network design rule)."""

    def setUp(self):
        _ensure_loop()
        import backend.chat_bridge as cb
        self.cb = cb

    def _ws(self, headers=None, client_host=None):
        return types.SimpleNamespace(
            headers=headers or {},
            client=types.SimpleNamespace(host=client_host) if client_host else None,
        )

    def test_client_ip_prefers_x_forwarded_for_first_hop(self):
        ws = self._ws(headers={"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, client_host="10.0.0.1")
        self.assertEqual(self.cb._client_ip(ws), "203.0.113.5")

    def test_client_ip_falls_back_to_ws_client_host(self):
        ws = self._ws(client_host="203.0.113.7")
        self.assertEqual(self.cb._client_ip(ws), "203.0.113.7")

    def test_client_ip_empty_when_neither_available(self):
        self.assertEqual(self.cb._client_ip(self._ws()), "")

    def test_geoip_lookup_skips_private_and_loopback_with_no_network(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "::1"):
            self.assertEqual(asyncio.run(self.cb._geoip_lookup(ip)), {})

    def test_geoip_lookup_empty_for_blank_or_invalid_ip(self):
        self.assertEqual(asyncio.run(self.cb._geoip_lookup("")), {})
        self.assertEqual(asyncio.run(self.cb._geoip_lookup("not-an-ip")), {})


class TestRecordingWriterAlignment(unittest.TestCase):
    """build 393 — agent.wav must stay wall-clock aligned with caller.wav.

    Tester report: a live call recording where the bot's answers drift
    earlier and earlier, ending up mixed BEFORE the caller question that
    prompted them. Root cause: write_agent() only fires while Gemini is
    actually emitting TTS, with no silence written during the gaps while
    Eva isn't speaking — so agent.wav is a "compressed" track (all TTS
    back-to-back, dead air squeezed out) while caller.wav (continuous mic
    tap) stays real-time. mix_to_stereo's naive sample-index interleave
    then drifts the agent channel earlier as the call goes on. Fixed by
    padding each stream with silence up to elapsed wall-clock time before
    every real chunk. Empirically confirmed against a real recording: the
    agent channel was packed solid for the first ~57s of a 91s call, then
    silent for the last ~34s, while the caller channel kept its normal
    on/off speech pattern the whole way through."""

    def setUp(self):
        from backend import recordings
        self.recordings = recordings

    class _FakeWave:
        def __init__(self):
            self.written = bytearray()

        def writeframesraw(self, b):
            self.written += b

    def _new_writer(self, started_at):
        w = self.recordings.RecordingWriter.__new__(self.recordings.RecordingWriter)
        w._closed = False
        w._started_at = started_at
        return w

    def test_pad_to_now_inserts_silence_for_elapsed_time(self):
        from datetime import datetime, timedelta, timezone
        w = self._new_writer(datetime.now(timezone.utc) - timedelta(seconds=2.0))
        fake = self._FakeWave()
        new_total = w._pad_to_now(fake, 0, self.recordings.AGENT_RATE_HZ)
        expected = int(2.0 * self.recordings.AGENT_RATE_HZ) * 2  # int16 mono
        # Real wall-clock elapses a hair more than 2.0s between the timestamp
        # above and _pad_to_now's own now() call — allow a small tolerance.
        self.assertGreaterEqual(new_total, expected)
        self.assertLess(new_total, expected + int(0.2 * self.recordings.AGENT_RATE_HZ) * 2)
        self.assertEqual(len(fake.written), new_total)
        self.assertEqual(bytes(fake.written), b"\x00" * new_total)

    def test_pad_to_now_is_noop_when_caught_up(self):
        from datetime import datetime, timezone
        w = self._new_writer(datetime.now(timezone.utc))
        fake = self._FakeWave()
        # written_bytes already "in the future" relative to elapsed time.
        new_total = w._pad_to_now(fake, 10_000_000, self.recordings.AGENT_RATE_HZ)
        self.assertEqual(new_total, 10_000_000)
        self.assertEqual(len(fake.written), 0)

    def test_write_agent_pads_through_a_silent_gap(self):
        """Reproduces the reported bug shape: Eva speaks, goes quiet for a
        stretch (caller talking), then speaks again — agent.wav must grow
        through the gap so its total duration tracks real elapsed time, not
        just active-speech time."""
        from datetime import datetime, timedelta, timezone
        w = self._new_writer(datetime.now(timezone.utc))
        w._agent_wave = self._FakeWave()
        w._agent_bytes = 0

        chunk = b"\x01\x00" * 100  # 200 bytes of "speech"
        w.write_agent(chunk)
        self.assertEqual(w._agent_bytes, len(chunk))  # no gap yet — no padding

        # Simulate Eva going quiet for 1s while the caller talks, by rewinding
        # _started_at (equivalent to real time having advanced 1s).
        w._started_at -= timedelta(seconds=1.0)
        w.write_agent(chunk)

        # The gap-fill target already covers the first chunk's bytes as part
        # of "what should exist by the 1s mark" — only the second chunk adds
        # on top of that (pushing slightly past the 1s mark).
        min_expected = int(1.0 * self.recordings.AGENT_RATE_HZ) * 2 + len(chunk)
        self.assertGreaterEqual(w._agent_bytes, min_expected)
        # And the padding is genuinely silent, not garbage.
        pad_region = bytes(w._agent_wave.written[len(chunk):len(chunk) + 1000])
        self.assertEqual(pad_region, b"\x00" * len(pad_region))

    def test_write_caller_and_write_agent_stay_wall_clock_aligned(self):
        """End-to-end shape check: after caller speaks continuously and agent
        speaks in two bursts with a gap, both streams' durations should match
        real elapsed time (within a fraction of a second), not each stream's
        own active-audio total — which is exactly what let the bug ship
        (both streams individually "worked", they just drifted apart)."""
        from datetime import datetime, timedelta, timezone
        w = self._new_writer(datetime.now(timezone.utc) - timedelta(seconds=3.0))
        w._caller_wave = self._FakeWave()
        w._agent_wave = self._FakeWave()
        w._caller_bytes = 0
        w._agent_bytes = 0

        w.write_caller(b"\x00\x01" * 500)
        w.write_agent(b"\x00\x01" * 500)  # only ~1/8 as much "real" agent audio

        caller_duration_s = w._caller_bytes / 2 / self.recordings.CALLER_RATE_HZ
        agent_duration_s = w._agent_bytes / 2 / self.recordings.AGENT_RATE_HZ
        # Both tracks should reflect ~3s of real elapsed time, not the tiny
        # amount of actual audio bytes each call wrote.
        self.assertGreater(caller_duration_s, 2.9)
        self.assertGreater(agent_duration_s, 2.9)
        self.assertLess(abs(caller_duration_s - agent_duration_s), 0.2)


class TestAgentConfigWarnings(unittest.TestCase):
    """build 409 — config-rot detector. Root-caused against production agent
    id 6 (Gajraj Hyundai / 'Kavya'): its system_prompt mandates `send_email`
    for every lead but `email_send` was never added to the agent's
    `connectors`, and its SMS-to-sales body used `{{GAJRAJ_SALES_SMS}}`
    which was never set under Variables — both silent, both meaning every
    captured lead was dropped with no error anywhere. Neither failure mode
    trips a call-time exception (prompt composition never raises), so this
    is a pure-text static check run on every save + hourly healthcheck,
    generic across every tenant/sector — not a Gajraj-specific patch.

    IMPORTANT prior mistake, caught by sanity-checking against the real
    agent-6 row before shipping: an earlier version of this fix also made
    `_substitute_variables` blank out any unresolved `{{key}}` at runtime.
    That looked safer in isolation but actively corrupted Kavya's prompt —
    it has a whole "STATE VARIABLES" section using `{{lower_snake_case}}`
    as intentional in-prompt notation for conversation state the MODEL
    fills in as it talks (`{{caller_name}}`, `{{price_quoted}}`, 20+ more),
    never meant to be resolved by this function at all. Blanking those
    turned instructions like 'Namaste {{caller_name}}, thank you for
    calling' into 'Namaste , thank you for calling' for every one of them.
    Runtime substitution is reverted to its original leave-literal
    behaviour; detection is scoped instead (`all_caps_only` in the freeform
    system_prompt) so it flags the 4 genuine gaps (SHOWROOM_ADDRESS,
    SHOWROOM_HOURS, SHOWROOM_MAPS_HINT, GAJRAJ_SALES_SMS — all ALL_CAPS
    deploy-time constants) without the 20+ state-variable false positives."""

    def setUp(self):
        _ensure_loop()
        import backend.gemini_bridge as gb
        self.gb = gb

    def test_substitute_variables_leaves_unresolved_placeholder_literal(self):
        # Must NOT blank/rewrite an unresolved placeholder — see class
        # docstring for why (state-variable notation must survive intact).
        out = self.gb._substitute_variables("to: {{SALES_SMS}}", {})
        self.assertEqual(out, "to: {{SALES_SMS}}")

    def test_substitute_variables_still_fills_known_keys(self):
        out = self.gb._substitute_variables("Hi from {{business_name}}", {"business_name": "Acme"})
        self.assertEqual(out, "Hi from Acme")

    def test_substitute_variables_mixed_known_and_unknown(self):
        out = self.gb._substitute_variables("{{city}} / {{missing}}", {"city": "Kolkata"})
        self.assertEqual(out, "Kolkata / {{missing}}")

    def test_unresolved_template_vars_reports_missing_only(self):
        names = self.gb._unresolved_template_vars(
            "{{a}} and {{b}} and {{a}}", {"a": "x"})
        self.assertEqual(names, ["b"])  # de-duplicated, "a" resolved

    def test_unresolved_template_vars_empty_when_all_resolved(self):
        self.assertEqual(
            self.gb._unresolved_template_vars("{{a}}", {"a": "1"}), [])

    def test_unresolved_template_vars_all_caps_only_filters_lowercase(self):
        names = self.gb._unresolved_template_vars(
            "{{SHOWROOM_ADDRESS}} and {{caller_name}} and {{ACTIVE_LANGUAGE}}",
            {}, all_caps_only=True)
        self.assertEqual(names, ["SHOWROOM_ADDRESS", "ACTIVE_LANGUAGE"])

    def test_agent_config_warnings_flags_unresolved_variable_in_system_prompt(self):
        agent = {
            "system_prompt": 'send_sms to="{{GAJRAJ_SALES_SMS}}"',
            "connectors": ["sms_send"],
            "variables": {},
        }
        warnings = self.gb.agent_config_warnings(agent)
        kinds = [w["kind"] for w in warnings]
        self.assertIn("unresolved_variable", kinds)
        detail = next(w["detail"] for w in warnings if w["kind"] == "unresolved_variable")
        self.assertIn("GAJRAJ_SALES_SMS", detail)

    def test_agent_config_warnings_ignores_state_variable_notation_in_system_prompt(self):
        # The false positive this class's docstring describes: state-slot
        # notation the model fills in itself must never be flagged.
        agent = {
            "system_prompt": (
                "STATE VARIABLES: {{caller_name}} {{phone_number}} {{price_quoted}}\n"
                'Say: "Namaste {{caller_name}}, thank you for calling."'
            ),
            "connectors": [],
            "variables": {},
            "voice_tweaks": {"voice_provider": "gemini"},
        }
        self.assertEqual(self.gb.agent_config_warnings(agent), [])

    def test_agent_config_warnings_flags_unresolved_variable_in_persona(self):
        # persona/greeting/chat-instructions are short direct-output strings
        # — ANY unresolved placeholder there (even lower_snake_case) is a
        # real gap, unlike the freeform system_prompt body.
        agent = {
            "system_prompt": "",
            "persona": "Friendly receptionist for {{business_name}}.",
            "connectors": [],
            "variables": {},
            "voice_tweaks": {"voice_provider": "gemini"},
        }
        warnings = self.gb.agent_config_warnings(agent)
        self.assertTrue(any("business_name" in w["detail"] for w in warnings))

    def test_agent_config_warnings_flags_unprovisioned_connector(self):
        # Prompt tells the model to call send_email; connectors list doesn't
        # include email_send — Gemini has no such tool declared, so it's a
        # guaranteed no-op every call. This is the exact Gajraj/Kavya shape.
        agent = {
            "system_prompt": "When done, call send_email with the lead details.",
            "connectors": ["calendar_check", "calendar_book", "sms_send", "knowledge_base_search"],
            "variables": {},
        }
        warnings = self.gb.agent_config_warnings(agent)
        kinds = [w["kind"] for w in warnings]
        self.assertIn("connector_not_provisioned", kinds)
        detail = next(w["detail"] for w in warnings if w["kind"] == "connector_not_provisioned")
        self.assertIn("email_send", detail)

    def test_agent_config_warnings_clean_agent_has_none(self):
        agent = {
            "system_prompt": "Greet the caller and call end_call when done.",
            "persona": "Friendly receptionist for {{business_name}}.",
            "greeting": "Hi, thanks for calling {{business_name}}!",
            "connectors": ["calendar_check", "calendar_book"],
            "variables": {"business_name": "Acme Dental"},
            "voice_tweaks": {"voice_provider": "gemini"},
        }
        self.assertEqual(self.gb.agent_config_warnings(agent), [])

    def test_agent_config_warnings_end_call_never_flagged(self):
        # end_call is always force-included at session-open regardless of
        # the agent's `connectors` list (see gemini_bridge's tool_ids build)
        # — must never be flagged as "not provisioned".
        agent = {
            "system_prompt": "Wrap up by calling end_call with the outcome.",
            "connectors": [],
            "variables": {},
            "voice_tweaks": {"voice_provider": "gemini"},
        }
        self.assertEqual(self.gb.agent_config_warnings(agent), [])

    def test_agent_config_warnings_flags_fish_without_voice_id(self):
        # Fish Audio is the platform-wide DEFAULT live-call voice engine
        # (backend/telephony/base.py) — confirmed live on agent 6: an agent
        # can resolve to Fish with no fish_voice_id ever chosen, so Fish
        # speaks in its own generic default voice on every real call.
        agent = {"system_prompt": "", "connectors": [], "variables": {},
                  "voice_tweaks": {"voice_provider": "fish", "fish_voice_id": ""}}
        warnings = self.gb.agent_config_warnings(agent)
        self.assertEqual([w["kind"] for w in warnings], ["fish_voice_not_selected"])

    def test_agent_config_warnings_flags_fish_default_when_voice_tweaks_absent(self):
        # voice_provider defaults to "fish" when voice_tweaks is missing
        # entirely, not just when it's explicitly set to "fish".
        agent = {"system_prompt": "", "connectors": [], "variables": {}}
        warnings = self.gb.agent_config_warnings(agent)
        self.assertEqual([w["kind"] for w in warnings], ["fish_voice_not_selected"])

    def test_agent_config_warnings_fish_with_voice_id_is_clean(self):
        agent = {"system_prompt": "", "connectors": [], "variables": {},
                  "voice_tweaks": {"voice_provider": "fish", "fish_voice_id": "abc123"}}
        self.assertEqual(self.gb.agent_config_warnings(agent), [])

    def test_agent_config_warnings_scans_chat_instructions_too(self):
        agent = {
            "system_prompt": "",
            "chat_settings": {"instructions": "Sign off as {{support_agent_name}}."},
            "connectors": [],
            "variables": {},
        }
        warnings = self.gb.agent_config_warnings(agent)
        self.assertTrue(any("support_agent_name" in w["detail"] for w in warnings))


class TestReconnectSteerNeverInvitesSelfAnswer(unittest.TestCase):
    """build 418 — the REAL root cause behind the "agent keeps talking on
    its own" reports (builds 410, 416, 417 all targeted wrong or incomplete
    causes; this is the one production logs actually confirmed). Evidence:
    a single 25s silent test call showed 3 "reconnects" ~5-8s apart, every
    one logged 'gemini stream ended cleanly' with NO exception and NO
    go_away — i.e. session.receive()'s generator ending after roughly one
    turn cycle is NORMAL, not a real connection failure. Every such cycle
    re-entered the resume_handle reconnect branch, whose kickoff text used
    to end "...if the caller's most recent question is still unanswered,
    answer it now, directly...". Mira's last utterance was typically HER
    OWN half-finished question ("what date works for you?") — which she'd
    then read as unanswered and answer herself, live, with the caller
    saying nothing at all. Mic peaks logged for that call (203, 1276, 2785)
    were far below the 12000 barge-in/echo threshold, ruling out echo
    bleed as the mechanism this time. Fix: no discretion — resuming after a
    drop must produce silence, unconditionally, until the caller actually
    speaks. This test can't invoke live Gemini, so it guards the source
    text directly (same style as TestBuildLockstep below) — the one thing
    a unit test CAN prove is that the loophole phrase is gone and the
    mandatory-silence replacement is present."""

    def setUp(self):
        self.src = (REPO / "backend/gemini_bridge.py").read_text()

    def _resume_handle_branch(self) -> str:
        start = self.src.index("elif resume_handle:")
        end = self.src.index("elif memory.turns:", start)
        return self.src[start:end]

    def _resume_handle_kickoff_text(self) -> str:
        # Isolate just the string literal the model actually receives —
        # NOT the surrounding comment, which deliberately quotes the old,
        # removed phrasing as an explanation and would otherwise false-
        # positive against a naive substring check over the whole branch.
        branch = self._resume_handle_branch()
        start = branch.index("kickoff_text = (")
        end = branch.index(")\n", start)
        return branch[start:end]

    def test_answer_it_now_loophole_is_gone(self):
        kickoff = self._resume_handle_kickoff_text()
        self.assertNotIn("answer it", kickoff.lower(),
            "the reconnect-steer kickoff must not give the model discretion "
            "to answer its OWN dangling question when resuming — that's "
            "exactly what caused the runaway self-conversation bug")

    def test_mandatory_silence_instruction_present(self):
        branch = self._resume_handle_branch()
        self.assertIn("COMPLETELY SILENT", branch)
        self.assertIn("do not continue or answer your own last question".upper(),
                       branch.upper())

    def test_kickoff_still_sent_with_turn_complete_true(self):
        # Confirms this fix didn't accidentally change the SDK call shape —
        # only the instruction text — which would be a much bigger, less
        # certain change to make without live Gemini access to verify.
        self.assertIn('turn_complete=True', self.src)


class TestImportSanity(unittest.TestCase):
    def setUp(self):
        _ensure_loop()   # backend.settings builds an asyncio.Lock() at import time

    def test_core_modules_import(self):
        import importlib
        for mod in ("backend.app", "backend.telephony.base",
                    "backend.telephony.audio", "backend.fish_audio",
                    "backend.gemini_bridge"):
            importlib.import_module(mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
