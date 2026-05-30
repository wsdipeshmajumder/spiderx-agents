// AudioWorklet that delivers PCM16 mono audio at exactly 16 kHz to the main
// thread, regardless of what sample rate the host AudioContext actually
// chose. (Some browsers — notably Safari — ignore a requested sampleRate
// and keep the device's native rate, typically 48 kHz. If we shipped the raw
// 48 kHz samples with a `rate=16000` mime tag, Gemini Live would receive
// time-warped garbage and its VAD would never detect speech.)
//
// Strategy:
//   • Read `sampleRate` (the global, set by the AudioContext) on construction.
//   • Float32 mic samples come in as 128-sample buffers via `process()`.
//   • We accumulate, downsample with linear interpolation, then convert to
//     Int16 and post in ~100 ms chunks (1600 samples @ 16 kHz = 3200 bytes).

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 1600; // 100 ms at 16 kHz

class RecorderWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.inRate = sampleRate; // AudioContext rate (16000 or 48000 typically)
    this.ratio = this.inRate / TARGET_RATE;
    this.outBuffer = new Float32Array(0);
    this.fracIndex = 0; // running fractional read position in the input stream
    this.muted = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "muted") this.muted = !!e.data.muted;
    };
    // Tell main thread what we negotiated
    this.port.postMessage({ type: "info", inRate: this.inRate, target: TARGET_RATE });
  }

  // Resample `input` (Float32, at this.inRate) to TARGET_RATE.
  // Maintains fractional index across calls so chunks join seamlessly.
  _resample(input) {
    if (this.inRate === TARGET_RATE) return input;
    const out = [];
    let i = this.fracIndex;
    while (i < input.length - 1) {
      const i0 = Math.floor(i);
      const frac = i - i0;
      const a = input[i0];
      const b = input[i0 + 1];
      out.push(a + (b - a) * frac);
      i += this.ratio;
    }
    // Carry remainder for the next call
    this.fracIndex = i - input.length;
    return new Float32Array(out);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || this.muted) return true;
    const ch0 = input[0];

    const resampled = this._resample(ch0);

    // Append to output buffer
    const merged = new Float32Array(this.outBuffer.length + resampled.length);
    merged.set(this.outBuffer, 0);
    merged.set(resampled, this.outBuffer.length);
    this.outBuffer = merged;

    // Flush 100 ms chunks
    while (this.outBuffer.length >= CHUNK_SAMPLES) {
      const chunk = this.outBuffer.slice(0, CHUNK_SAMPLES);
      this.outBuffer = this.outBuffer.slice(CHUNK_SAMPLES);
      const int16 = new Int16Array(CHUNK_SAMPLES);
      for (let j = 0; j < CHUNK_SAMPLES; j++) {
        const s = Math.max(-1, Math.min(1, chunk[j]));
        int16[j] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
      }
      this.port.postMessage(int16.buffer, [int16.buffer]);
    }
    return true;
  }
}

registerProcessor("recorder-worklet", RecorderWorklet);
