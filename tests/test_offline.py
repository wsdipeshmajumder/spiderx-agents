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


def _resp(*, ot=None, audio=None, turn_complete=False, interrupted=False):
    sc = types.SimpleNamespace(
        interrupted=interrupted,
        input_transcription=None,
        output_transcription=(types.SimpleNamespace(text=ot) if ot else None),
        turn_complete=turn_complete,
        model_turn=(types.SimpleNamespace(parts=[types.SimpleNamespace(
            inline_data=types.SimpleNamespace(data=audio))]) if audio else None),
    )
    return types.SimpleNamespace(server_content=sc, session_resumption_update=None,
                                 go_away=None, tool_call=None)


class TestBridgeFishPath(unittest.TestCase):
    def _run(self, voice_provider, synth_impl):
        from backend import fish_audio
        from backend.gemini_bridge import _ConversationMemory
        orig_synth, orig_cfg = fish_audio.synthesize, fish_audio.is_configured
        fish_audio.synthesize = synth_impl
        fish_audio.is_configured = lambda: True
        try:
            agent = {"id": 1, "voice_tweaks": {"voice_provider": voice_provider},
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


# ─── 6. import sanity (catches import-time breakage before deploy) ───────


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
