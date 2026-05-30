#!/usr/bin/env python3
"""Generate one short audio sample per Gemini Live voice and save it to
frontend/voice-samples/<Voice>.wav so the Voice settings page can preview each
voice with the user without making a real call.

Each sample is a single line that's deliberately written to *showcase* that
voice's character — warm voices sound warm, formal voices sound formal,
energetic voices have a noticeable lift. The prompt to the TTS model includes
a style instruction ("Say warmly…") so the same model variant can portray
different tones credibly.

Output: 24 kHz mono 16-bit PCM wrapped in a standard WAV header. Browsers play
WAV natively, so no transcoding needed.

Usage:
    GEMINI_API_KEY=... .venv/bin/python scripts/gen_voice_samples.py

Re-run anytime the lines or voice catalogue change — the files are committed
as the source of truth.
"""
from __future__ import annotations

import os
import struct
import sys
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "frontend" / "voice-samples"

# Each voice gets a line that shows it off + a style instruction prepended so
# the TTS model leans into the tone. Lines are short (~5s) so the preview is
# a snack, not a meal.
VOICE_SAMPLES = [
    ("Aoede",
     "Say warmly and welcomingly, like a friendly receptionist greeting "
     "someone they're glad to hear from: "
     "\"Hi there — thanks for calling. How can I help you today?\""),
    ("Puck",
     "Say with bright, upbeat energy, like a hospitality host happy to see you: "
     "\"Hi! Lovely to hear from you. What can I do for you today?\""),
    ("Charon",
     "Say calmly and slowly in a low, composed voice, like reassuring someone "
     "who's stressed: "
     "\"Hello. You've reached our line. Take your time — I'm listening.\""),
    ("Kore",
     "Say clearly and neutrally, like a professional receptionist with crisp "
     "diction: "
     "\"Good afternoon. How may I assist you?\""),
    ("Fenrir",
     "Say confidently and energetically, with a slight gruff edge, like a "
     "tradesman happy to help: "
     "\"Hey, thanks for calling — what do you need?\""),
    ("Leda",
     "Say softly and conversationally, like a friend on the phone after dinner: "
     "\"Hi, so glad you called. How can I help?\""),
    ("Orus",
     "Say in a measured, formal voice, polite and precise: "
     "\"Good day. How may I be of service?\""),
    ("Zephyr",
     "Say lightly and breezily, casual and modern: "
     "\"Hey there — how can I help out?\""),
]

MODEL = "gemini-2.5-flash-preview-tts"
SAMPLE_RATE = 24_000
SAMPLE_WIDTH = 2   # 16-bit PCM
CHANNELS = 1


def _write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def _generate_one(client: genai.Client, voice: str, prompt: str) -> bytes:
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    )
                ),
            ),
        ),
    )
    parts = resp.candidates[0].content.parts
    for p in parts:
        if p.inline_data and p.inline_data.data:
            return p.inline_data.data
    raise RuntimeError(f"no audio bytes returned for voice {voice!r}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY env var is not set", file=sys.stderr)
        return 1

    client = genai.Client(api_key=api_key)
    print(f"writing samples to {OUT_DIR}")
    for voice, prompt in VOICE_SAMPLES:
        out = OUT_DIR / f"{voice}.wav"
        try:
            print(f"  · {voice} … ", end="", flush=True)
            pcm = _generate_one(client, voice, prompt)
            _write_wav(out, pcm)
            print(f"{out.stat().st_size:,} bytes")
        except Exception as e:
            print(f"FAILED: {e}")
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
