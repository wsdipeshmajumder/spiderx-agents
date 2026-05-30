#!/usr/bin/env python3
"""Generate per-industry ambience loops for the Voice settings page.

Each loop is ~24s of seamlessly-looped low-volume audio that sits underneath
Eva's voice on a real call. The goal isn't audiophile fidelity — it's just
enough background presence to break the "AI in a vacuum" feeling.

We synthesise everything from scratch so there are no licensing questions and
the loops are perfectly reproducible: pink/brown noise base, optional layers
(typing, HVAC hum, occasional beeps, distant murmur). Output: 24 kHz mono
16-bit PCM, wrapped in a WAV header. The same sample rate Gemini's voice
output uses, so the audio engine can play both without resampling.

Usage:
    .venv/bin/python scripts/gen_ambience.py

Re-run any time the recipes change. The files are committed as source of truth.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "frontend" / "voice-samples" / "ambience"
SR = 24_000
DUR = 24.0   # seconds per loop — short enough to be tiny on disk, long enough
             # that nobody clocks the loop point on a 5-minute call.
CROSSFADE_S = 1.5   # ramp the head into the tail so the loop seam disappears.


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _seamless(samples: np.ndarray, fade_s: float = CROSSFADE_S) -> np.ndarray:
    """Crossfade the head of the buffer into the tail so the loop seam is
    inaudible. Returns a buffer the same length as the input."""
    n = len(samples)
    fade = int(fade_s * SR)
    if fade <= 0 or fade * 2 >= n:
        return samples
    out = samples.copy()
    # Take a copy of the head, ramp it down; take the corresponding tail, ramp
    # it up; sum them at the tail so when the player wraps around, the
    # waveform leading into sample 0 matches what was at sample 0.
    ramp_in  = np.linspace(0.0, 1.0, fade)
    ramp_out = 1.0 - ramp_in
    head = out[:fade].copy()
    out[-fade:] = out[-fade:] * ramp_out + head * ramp_in
    return out


def _pink_noise(n: int, seed: int = 0) -> np.ndarray:
    """Voss-McCartney pink-noise approximation — sums multiple octaves of
    white noise, each updating at half the rate of the previous. Gives a
    fairly even -3dB/octave slope without needing scipy filters."""
    rng = np.random.default_rng(seed)
    octaves = 8
    bins = np.zeros((octaves, n), dtype=np.float32)
    for o in range(octaves):
        step = 1 << o
        # Sample one new value every `step` frames; hold between.
        held = rng.standard_normal((n + step - 1) // step).astype(np.float32)
        bins[o] = np.repeat(held, step)[:n]
    pink = bins.mean(axis=0)
    pink /= np.std(pink) + 1e-9
    return pink * 0.35


def _brown_noise(n: int, seed: int = 1) -> np.ndarray:
    """Brown / red noise — heavier low end than pink. Used for HVAC rumble."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n).astype(np.float32)
    # 1-pole integrator (leaky to avoid DC drift)
    out = np.zeros(n, dtype=np.float32)
    k = 0.985
    s = 0.0
    for i in range(n):
        s = k * s + white[i] * 0.05
        out[i] = s
    out /= np.std(out) + 1e-9
    return out * 0.35


def _hvac_hum(n: int, freq: float = 62.0, seed: int = 2) -> np.ndarray:
    """Low-frequency sinusoid + harmonic + slow LFO modulation — sounds like
    distant air-handling. The slow LFO keeps it from feeling synthetic."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    fund = np.sin(2 * np.pi * freq * t)
    harm = 0.35 * np.sin(2 * np.pi * (freq * 2) * t)
    # Slow amplitude wobble — a 0.13 Hz LFO that meanders.
    lfo = 0.85 + 0.15 * np.sin(2 * np.pi * 0.13 * t + rng.standard_normal() * 2)
    return ((fund + harm) * lfo * 0.18).astype(np.float32)


def _typing_bursts(n: int, density: float, seed: int = 3) -> np.ndarray:
    """Synthetic keyboard taps: short, high-mid bursts of band-pass-filtered
    noise at random intervals. `density` is taps per second (e.g. 4.0 for
    casual typing, 9.0 for a busy office)."""
    rng = np.random.default_rng(seed)
    out = np.zeros(n, dtype=np.float32)
    total = DUR
    n_taps = int(total * density)
    for _ in range(n_taps):
        pos = int(rng.uniform(0, n))
        # Short tap ~25ms, exponentially decaying.
        tap_len = int(SR * rng.uniform(0.02, 0.05))
        if pos + tap_len >= n:
            continue
        env = np.exp(-np.linspace(0, 8, tap_len))
        # Band-passed white-ish noise: filter via a difference equation.
        white = rng.standard_normal(tap_len).astype(np.float32)
        # 1st-order high-pass at ~1.5 kHz: difference of consecutive samples.
        hp = np.empty_like(white)
        prev = 0.0
        cutoff = 0.62  # close to 1/4 SR ≈ 1.5kHz when SR is 24k.
        for i, x in enumerate(white):
            hp[i] = x - prev
            prev = cutoff * prev + (1.0 - cutoff) * x
        amp = rng.uniform(0.10, 0.22)
        out[pos:pos + tap_len] += hp * env * amp
    return out


def _murmur(n: int, density: float = 0.65, seed: int = 4) -> np.ndarray:
    """Distant chatter approximation — band-pass-filtered noise with slow
    syllabic envelope modulation. Sounds like talking through a wall."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n).astype(np.float32)
    # Band-pass via cascade of simple high-pass and low-pass.
    bp = np.empty_like(white)
    prev_hp, prev_lp = 0.0, 0.0
    hp_a, lp_a = 0.985, 0.86   # roughly 300 Hz–2 kHz band
    for i, x in enumerate(white):
        hp_v = x - prev_hp + 0.7 * prev_hp
        prev_hp = x
        prev_lp = lp_a * prev_lp + (1.0 - lp_a) * hp_v
        bp[i] = prev_lp
    bp /= np.std(bp) + 1e-9

    # Syllabic envelope — slow random ramps at ~3-7 Hz.
    env = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        seg = int(rng.uniform(0.10, 0.25) * SR)
        level = rng.uniform(0.0, 1.0) * density
        end = min(pos + seg, n)
        env[pos:end] = np.linspace(env[pos - 1] if pos else 0.0, level, end - pos)
        pos = end
    # Smooth the envelope.
    sm = np.empty_like(env)
    a = 0.96
    s = 0.0
    for i, x in enumerate(env):
        s = a * s + (1 - a) * x
        sm[i] = s
    return bp * sm * 0.32


def _occasional_beep(n: int, period_s: float, freq: float = 1320.0, seed: int = 5) -> np.ndarray:
    """Very occasional faint sine pings — clinical / medical monitor vibe."""
    rng = np.random.default_rng(seed)
    out = np.zeros(n, dtype=np.float32)
    pos = int(rng.uniform(0, SR * period_s))
    while pos < n:
        beep_len = int(SR * 0.18)
        if pos + beep_len < n:
            t = np.arange(beep_len) / SR
            env = np.exp(-np.linspace(0, 6, beep_len))
            out[pos:pos + beep_len] += 0.06 * env * np.sin(2 * np.pi * freq * t)
        pos += int(SR * period_s * rng.uniform(0.8, 1.4))
    return out


# ─── Industry recipes ──────────────────────────────────────────────────────
# Each entry produces one WAV. Volumes are pre-attenuated so the loop is
# safe to play at gain=1; the UI layer can scale down further if needed.

def gen_office() -> np.ndarray:
    """Open-plan office — pink noise hum + light keyboard + distant chatter.
    Good default for SaaS support / banking / education / professional services."""
    n = int(SR * DUR)
    out  = _pink_noise(n, seed=11) * 0.30
    out += _typing_bursts(n, density=5.5, seed=12)
    out += _murmur(n, density=0.55, seed=13)
    out += _hvac_hum(n, freq=60.0, seed=14) * 0.40
    return _seamless(out * 0.45)


def gen_busy_office() -> np.ndarray:
    """Higher chatter density — outbound sales / dense call-centre feel."""
    n = int(SR * DUR)
    out  = _pink_noise(n, seed=21) * 0.30
    out += _typing_bursts(n, density=8.5, seed=22)
    out += _murmur(n, density=0.85, seed=23)
    out += _hvac_hum(n, freq=58.0, seed=24) * 0.45
    return _seamless(out * 0.50)


def gen_clinic() -> np.ndarray:
    """Medical / dental waiting room — very quiet pink, occasional faint
    monitor ping, gentle HVAC."""
    n = int(SR * DUR)
    out  = _pink_noise(n, seed=31) * 0.22
    out += _hvac_hum(n, freq=64.0, seed=32) * 0.30
    out += _occasional_beep(n, period_s=7.0, freq=1320.0, seed=33)
    return _seamless(out * 0.35)


def gen_cafe() -> np.ndarray:
    """Restaurant / café / hotel lobby — heavier murmur, brownish wash."""
    n = int(SR * DUR)
    out  = _brown_noise(n, seed=41) * 0.30
    out += _murmur(n, density=0.95, seed=42)
    out += _pink_noise(n, seed=43) * 0.18
    return _seamless(out * 0.55)


def gen_workshop() -> np.ndarray:
    """Garage / workshop — brown rumble + slow low-frequency clanks."""
    n = int(SR * DUR)
    out  = _brown_noise(n, seed=51) * 0.42
    out += _hvac_hum(n, freq=46.0, seed=52) * 0.55
    # Sparse low clanks (basically a slower beep with lower freq).
    out += _occasional_beep(n, period_s=4.5, freq=180.0, seed=53) * 1.8
    return _seamless(out * 0.50)


def gen_quiet() -> np.ndarray:
    """Near-silent room tone for legal, premium hotels, anywhere noise would
    feel cheap. Pure HVAC + a sliver of pink."""
    n = int(SR * DUR)
    out  = _pink_noise(n, seed=61) * 0.10
    out += _hvac_hum(n, freq=58.0, seed=62) * 0.25
    return _seamless(out * 0.30)


RECIPES = {
    "office":      gen_office,
    "busy_office": gen_busy_office,
    "clinic":      gen_clinic,
    "cafe":        gen_cafe,
    "workshop":    gen_workshop,
    "quiet":       gen_quiet,
}


def main() -> int:
    print(f"writing ambience loops to {OUT_DIR}")
    for name, fn in RECIPES.items():
        path = OUT_DIR / f"{name}.wav"
        samples = fn()
        _write_wav(path, samples)
        print(f"  · {name:12s} → {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
