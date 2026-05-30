// AudioEngine encapsulates:
//   - mic capture at 16 kHz, PCM16 chunks fed to onMicChunk(buf)
//   - speaker playback queue at 24 kHz from PCM16 chunks pushed via playPcm(buf)
//   - level meters (RMS) for the voice-blob UI: getMicLevel() / getOutLevel()

export class AudioEngine {
  constructor() {
    this.micCtx = null;
    this.outCtx = null;
    this.micNode = null;
    this.micStream = null;
    this.micAnalyser = null;
    this.outAnalyser = null;
    this.outGain = null;
    this.nextStartTime = 0;
    this.onMicChunk = null;
    this._levelMic = 0;
    this._levelOut = 0;
    this._raf = null;
    this._meterBuf = null;
    this._muted = false;
  }

  async start({ onMicChunk } = {}) {
    this.onMicChunk = onMicChunk || (() => {});
    // Use the device's native sample rate for the mic context. Some browsers
    // (Safari especially) silently ignore a requested 16 kHz and keep 48 kHz
    // running anyway — which would send time-warped audio to Gemini and break
    // speech detection. The recorder worklet now resamples internally.
    this.micCtx = new (window.AudioContext || window.webkitAudioContext)();
    this.outCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    await this.micCtx.resume();
    await this.outCtx.resume();

    await this.micCtx.audioWorklet.addModule("/static/recorder-worklet.js");

    // Mic constraints: echo cancellation ON. Without it, Eva's audio bleeds
    // back through laptop speakers into the mic, Gemini's input transcription
    // catches phrases Eva just said, the model generates a parallel response
    // to its own voice, and the transcript ends up with two interleaved model
    // turns ("Hi, EvaHi there. here. I'm Eva..."). EC was off historically
    // because we worried about over-suppressing soft-spoken users — that was
    // the wrong trade. Headphones still help, but the default has to work
    // without them.
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const src = this.micCtx.createMediaStreamSource(this.micStream);
    this.micAnalyser = this.micCtx.createAnalyser();
    this.micAnalyser.fftSize = 512;
    src.connect(this.micAnalyser);

    this.micNode = new AudioWorkletNode(this.micCtx, "recorder-worklet");
    this.micNode.port.onmessage = (e) => {
      if (e.data && e.data.type === "info") {
        console.log("[mic] worklet running:", e.data);
        return;
      }
      // ArrayBuffer of Int16 samples at 16 kHz — forward to caller…
      this.onMicChunk(e.data);
      // …and locally detect user speech for instant barge-in. If the user
      // is talking while Eva is audibly playing, kill the playback queue
      // RIGHT NOW so Eva yields the line. Don't wait for Gemini's
      // server-side `interrupted` event — it's not reliable or fast enough
      // for a natural back-and-forth.
      this._checkBargeIn(e.data);
    };
    src.connect(this.micNode);
    // Worklet must connect to something to keep running in some browsers
    const silentGain = this.micCtx.createGain();
    silentGain.gain.value = 0;
    this.micNode.connect(silentGain).connect(this.micCtx.destination);

    this.outGain = this.outCtx.createGain();
    this.outAnalyser = this.outCtx.createAnalyser();
    this.outAnalyser.fftSize = 512;
    this.outGain.connect(this.outAnalyser);
    this.outAnalyser.connect(this.outCtx.destination);

    this.nextStartTime = this.outCtx.currentTime + 0.05;
    this._meterBuf = new Uint8Array(this.micAnalyser.frequencyBinCount);
    this._loopMeters();
  }

  setMuted(muted) {
    this._muted = !!muted;
    if (this.micNode) this.micNode.port.postMessage({ type: "muted", muted: !!muted });
  }

  /**
   * Local barge-in: if the user is CLEARLY talking (sustained high-peak audio
   * across multiple chunks) AND Eva is currently audible, flush her playback
   * immediately so the user takes the floor without waiting for the server.
   *
   * False positives are unacceptable: speaker bleed of Eva's own voice (when
   * the user is on laptop speakers) can hit peaks of 5000–8000 between her
   * sentences, and a naive threshold makes Eva cut herself off after every
   * sentence — that's exactly the symptom users keep reporting. So we
   * over-gate:
   *   - Threshold raised to 12000 (well above any plausible speaker bleed —
   *     real close-mic speech peaks at 12000–22000).
   *   - Sustained-signal requirement raised to FOUR consecutive chunks (~400
   *     ms of "actually talking", not a single noise spike).
   *   - Grace window at Eva's playback start extended to 2000 ms.
   *   - Server-side VAD at LOW sensitivity provides the backstop. Even if
   *     this client-side check is conservative, real user interrupts still
   *     register server-side once the model turn completes.
   *
   * Net: Eva finishes her sentences. User has to clearly interrupt
   * (sustained, loud) to barge in.
   */
  _checkBargeIn(arrayBuffer) {
    try {
      const view = new Int16Array(arrayBuffer);
      let peak = 0;
      for (let i = 0; i < view.length; i++) {
        const v = view[i] < 0 ? -view[i] : view[i];
        if (v > peak) peak = v;
      }
      // Bleed-proof threshold — well above any laptop-speaker bleed level.
      const PEAK_TALK = 12000;
      const loudNow = peak > PEAK_TALK;
      // Sustained: require four consecutive loud chunks (~400 ms).
      this._loudStreak = loudNow ? (this._loudStreak || 0) + 1 : 0;
      const USER_TALKING = this._loudStreak >= 4;

      const queuedAudioMs = (this.nextStartTime - this.outCtx.currentTime) * 1000;
      const EVA_TALKING = queuedAudioMs > 400;
      // Track when Eva's playback queue went from empty → busy. Long grace
      // window: the first 2 seconds of any Eva turn are protected so she can
      // finish her opener cleanly before barge-in is even considered.
      if (queuedAudioMs > 400 && !this._evaSpeakingSince) {
        this._evaSpeakingSince = performance.now();
      } else if (queuedAudioMs <= 100) {
        this._evaSpeakingSince = 0;
      }
      const evaJustStarted = this._evaSpeakingSince &&
                             (performance.now() - this._evaSpeakingSince) < 2000;

      if (USER_TALKING && EVA_TALKING && !evaJustStarted) {
        const now = performance.now();
        if (!this._lastBargeIn || now - this._lastBargeIn > 1000) {
          this._lastBargeIn = now;
          this._loudStreak = 0;
          this.flushPlayback();
          if (this.onBargeIn) this.onBargeIn();
        }
      }
    } catch {
      /* if anything goes wrong with the peak scan, fall back silently */
    }
  }

  playPcm(arrayBuffer) {
    if (!this.outCtx) return;
    const int16 = new Int16Array(arrayBuffer);
    if (!int16.length) return;
    const float = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float[i] = int16[i] / 0x8000;
    const buf = this.outCtx.createBuffer(1, float.length, 24000);
    buf.getChannelData(0).set(float);
    const src = this.outCtx.createBufferSource();
    src.buffer = buf;
    src.connect(this.outGain);
    const now = this.outCtx.currentTime;
    const startAt = Math.max(now + 0.02, this.nextStartTime);
    src.start(startAt);
    this.nextStartTime = startAt + buf.duration;
    // Track scheduled sources so we can hard-stop them on barge-in.
    if (!this._scheduledSources) this._scheduledSources = [];
    this._scheduledSources.push(src);
    src.onended = () => {
      const i = this._scheduledSources.indexOf(src);
      if (i >= 0) this._scheduledSources.splice(i, 1);
    };
  }

  flushPlayback() {
    // Hard-stop pending audio. We do all three of:
    //   1. stop() every scheduled BufferSource (kills already-queued audio)
    //   2. disconnect the outGain (breaks the chain just in case)
    //   3. rewind nextStartTime so new audio plays immediately
    if (!this.outCtx) return;
    if (this._scheduledSources) {
      for (const s of this._scheduledSources.splice(0)) {
        try { s.stop(); s.disconnect(); } catch {}
      }
    }
    try {
      this.outGain.disconnect();
    } catch {}
    this.outGain = this.outCtx.createGain();
    this.outGain.connect(this.outAnalyser);
    this.nextStartTime = this.outCtx.currentTime + 0.02;
  }

  _rms(analyser) {
    if (!analyser) return 0;
    const buf = this._meterBuf;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / buf.length);
  }

  _loopMeters() {
    const tick = () => {
      // exponential decay so the blob feels organic, not jittery
      const mic = this._rms(this.micAnalyser);
      const out = this._rms(this.outAnalyser);
      this._levelMic = Math.max(mic, this._levelMic * 0.85);
      this._levelOut = Math.max(out, this._levelOut * 0.85);
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  getMicLevel() {
    // When muted, the blob must NOT animate to your voice. The mic analyser
    // is wired to the raw mic source (so we can still detect speech for
    // barge-in scans), but for the UI we report zero — the blob then
    // honestly reflects "no audio is going out from this side."
    return this._muted ? 0 : this._levelMic;
  }
  getOutLevel() {
    return this._levelOut;
  }

  /**
   * Ambience layer (Beta) — plays a low-volume looping background track in
   * parallel with Eva's PCM stream so the call doesn't feel like it's
   * happening in a soundproof booth. We use a plain <audio loop> element
   * routed through a GainNode on the same output context so the gain is
   * tied to the rest of the playback. Silent no-op if the file is missing.
   *
   * @param {string|null} src   Path like "/static/voice-samples/ambience/office.wav"
   *                            (or null to clear).
   * @param {number} volume     0.0–1.0. Defaults to 0.18 — barely-there but
   *                            audibly present on most laptop speakers.
   */
  setAmbience(src, volume = 0.18) {
    if (!this.outCtx) return;
    // Tear down any previous instance first.
    try { this._ambElement?.pause(); } catch {}
    try { this._ambSource?.disconnect(); } catch {}
    try { this._ambGain?.disconnect(); } catch {}
    this._ambElement = this._ambSource = this._ambGain = null;
    if (!src) return;

    const el = new Audio(src);
    el.loop = true;
    el.crossOrigin = "anonymous";
    el.preload = "auto";
    // Route through Web Audio so we share the destination with Eva's voice.
    // If MediaElementAudioSourceNode fails (rare — sandbox / autoplay
    // restrictions), we still want SOMETHING playing, so fall back to the
    // bare <audio> element with its own volume.
    let source, gain;
    try {
      source = this.outCtx.createMediaElementSource(el);
      gain = this.outCtx.createGain();
      gain.gain.value = Math.max(0, Math.min(1, volume));
      source.connect(gain).connect(this.outCtx.destination);
    } catch {
      el.volume = Math.max(0, Math.min(1, volume));
    }
    el.play().catch(() => {
      // Autoplay was blocked — most likely we're not in a user-gesture
      // context yet. Will retry on the next user-driven setAmbience call.
    });
    this._ambElement = el;
    this._ambSource = source;
    this._ambGain = gain;
  }

  /** Update the ambience volume in place without restarting playback. */
  setAmbienceVolume(volume) {
    const v = Math.max(0, Math.min(1, volume));
    if (this._ambGain) this._ambGain.gain.value = v;
    else if (this._ambElement) this._ambElement.volume = v;
  }

  async stop() {
    if (this._raf) cancelAnimationFrame(this._raf);
    try { this._ambElement?.pause(); } catch {}
    try { this._ambSource?.disconnect(); } catch {}
    try { this._ambGain?.disconnect(); } catch {}
    this._ambElement = this._ambSource = this._ambGain = null;
    try {
      this.micNode?.disconnect();
      this.micAnalyser?.disconnect();
    } catch {}
    try {
      this.outGain?.disconnect();
      this.outAnalyser?.disconnect();
    } catch {}
    try {
      this.micStream?.getTracks().forEach((t) => t.stop());
    } catch {}
    try {
      await this.micCtx?.close();
    } catch {}
    try {
      await this.outCtx?.close();
    } catch {}
    this.micCtx = this.outCtx = this.micNode = this.micStream = null;
    this.micAnalyser = this.outAnalyser = this.outGain = null;
  }
}
