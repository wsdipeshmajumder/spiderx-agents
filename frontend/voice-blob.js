// SpiderX AI voice blob — iridescent glass sphere, mystically alive.
//
// The blob is the entire UI, so it has to *hold the eye* on its own — without
// audio, without interaction, without a tap. Visual ingredients:
//
//   • Two counter-rotating iridescent gradients (one slower than the other)
//     so the colour field never stops shifting.
//   • A flowing turbulence filter that warps the sphere's silhouette over time,
//     so the blob is a living glass surface rather than a static circle.
//   • A drifting bright caustic spot (the "soul") that wanders inside the
//     sphere on a slow Lissajous path.
//   • Sparkle dust that wanders on Lissajous orbits when idle, and re-forms
//     into a flowing audio-synced waveform across the blob's diameter when
//     a call is active (listen / speak / thinking). When the call ends, the
//     motes drift back to wandering — the blob has two states of being.
//   • A breathing scale + halo that respond to audio when there is some,
//     and to a gentle sine pulse when there isn't.
//
// All animation is rAF-driven and mutates DOM attributes directly so React
// never re-renders during motion.

import React, { useEffect, useRef, useState } from "react";
import htm from "htm";

const html = htm.bind(React.createElement);

export function VoiceBlob({ engineRef, mode = "idle", size = 460 }) {
  const wrapRef = useRef(null);
  const haloRef = useRef(null);
  const sphereRef = useRef(null);
  const irisARef = useRef(null);
  const irisBRef = useRef(null);
  const soulRef = useRef(null);
  const rimRef = useRef(null);
  const sparkRef = useRef(null);
  const turbRef = useRef(null);
  // Concentric "sound-wave" rings that emanate from the blob's edge and ripple
  // outward. They breathe gently when idle and bloom into vivid pulses when
  // there's audio energy (the user or the agent speaking). Four rings, each
  // with a phase offset, so one is always near-emitted while another is fading.
  const wave1Ref = useRef(null);
  const wave2Ref = useRef(null);
  const wave3Ref = useRef(null);
  const wave4Ref = useRef(null);
  // refs the rAF tick reads each frame, so changing mode / engineRef doesn't
  // tear down the particle system or reset positions.
  const modeRef = useRef(mode);
  const engineRefRef = useRef(engineRef);
  useEffect(() => { modeRef.current = mode; }, [mode]);
  useEffect(() => { engineRefRef.current = engineRef; }, [engineRef]);

  // Theme awareness — the blob's outer/rim layers blend differently against
  // a cream page vs. near-black. We subscribe to the data-theme attribute on
  // <html> so a toggle re-renders the SVG stop-colors (and the spark canvas
  // re-picks colour each rAF via themeRef). Zero per-frame cost — observer
  // only fires on actual attribute changes.
  const [theme, setTheme] = useState(() =>
    (typeof document !== "undefined" && document.documentElement.getAttribute("data-theme")) || "light"
  );
  useEffect(() => {
    if (typeof document === "undefined") return;
    const el = document.documentElement;
    const obs = new MutationObserver(() => setTheme(el.getAttribute("data-theme") || "light"));
    obs.observe(el, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  const themeRef = useRef(theme);
  useEffect(() => { themeRef.current = theme; }, [theme]);

  // sparkle particle system — wandering motes when idle, audio-synced
  // waveform when a call is active. Particles never get destroyed; we just
  // ease each one between its idle orbit position and its waveform slot.
  useEffect(() => {
    const canvas = sparkRef.current;
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const N = 44;
    const Rmax = size * 0.34;
    const cx = size / 2;
    const cy = size / 2;
    // Particles get a deterministic slot along the wave (-1..+1) so the wave
    // shape stays readable as a sine; each also keeps an idle orbit so it
    // wanders organically when the call ends.
    const particles = Array.from({ length: N }, (_, i) => ({
      r: Math.sqrt(Math.random()) * Rmax,
      a0: Math.random() * Math.PI * 2,
      omega: (Math.random() < 0.5 ? -1 : 1) * (0.00006 + Math.random() * 0.00018),
      tilt: (Math.random() - 0.5) * 0.6,
      size: 0.4 + Math.random() * 1.6,
      phase: Math.random() * Math.PI * 2,
      twinkleSpeed: 0.0006 + Math.random() * 0.0014,
      hueShift: Math.random(),
      // Slot along the wave: evenly distributed across [-1, +1] with a tiny
      // jitter so the wave doesn't look like a graph on millimetre paper.
      waveX: (i / (N - 1)) * 2 - 1 + (Math.random() - 0.5) * 0.04,
      waveJitterPhase: Math.random() * Math.PI * 2,
      // Per-particle smoothed (x, y) so the transition between modes is silky.
      x: cx,
      y: cy,
      _init: false,
    }));

    // Excited factor (0..1) — interpolates toward 1 when the agent is in a
    // live call (listen / speak / thinking) and back to 0 when idle/error.
    let excited = 0;

    // Smoothed audio level — drives the wave amplitude. We blend mic and out
    // so it reacts to whoever is talking.
    let levelSmoothed = 0;

    let raf;
    let lastT = performance.now();
    const tick = (t) => {
      const dt = Math.min(64, t - lastT);
      lastT = t;
      ctx.clearRect(0, 0, size, size);

      const currentMode = modeRef.current;
      const eng = engineRefRef.current?.current;
      const mic = eng ? eng.getMicLevel() : 0;
      const out = eng ? eng.getOutLevel() : 0;
      const liveCall = currentMode === "listen" || currentMode === "speak" || currentMode === "thinking";
      const target = liveCall ? 1 : 0;
      // Asymmetric ease — ramp in over ~400ms, ramp out over ~900ms so the
      // wave forms quickly when a call starts and dissolves softly when it ends.
      const tau = target > excited ? 400 : 900;
      excited += (target - excited) * Math.min(1, dt / tau);

      // Audio level — peak of mic/out, gently amplified, smoothed. With no
      // real audio the amplitude is zero, so the wave collapses to a straight
      // line through the blob's centre. "Honest silence."
      const rawLevel = Math.min(1, Math.max(mic * 1.7, out * 2.0));
      levelSmoothed += (rawLevel - levelSmoothed) * Math.min(1, dt / 90);
      const amp = levelSmoothed * 0.95;

      // Wave geometry: horizontal sine across the blob's diameter, with a
      // travelling phase so the wave appears to flow rather than stand still.
      const waveSpan = size * 0.36;            // half-width of the wave in px
      const baseAmpPx = size * 0.20;            // max y-deflection in px
      const waveFreq = 2.6;                     // ~2-3 humps across the diameter
      const waveSpeed = 0.0038;                 // travelling phase rate

      for (const p of particles) {
        // Idle target — current Lissajous orbit (unchanged behaviour).
        const a = p.a0 + t * p.omega;
        const idleX = cx + Math.cos(a) * p.r;
        const idleY = cy + Math.sin(a) * p.r * (1 + p.tilt * 0.3);

        // Wave target — particle sits at its waveX slot, y oscillates.
        const wx = cx + p.waveX * waveSpan;
        const phase = p.waveX * waveFreq * Math.PI + t * waveSpeed + p.waveJitterPhase * 0.4;
        const wy = cy + Math.sin(phase) * baseAmpPx * amp;

        // Lerp between idle and wave based on excited factor.
        const tx = idleX + (wx - idleX) * excited;
        const ty = idleY + (wy - idleY) * excited;

        // Smooth the per-particle path so mode transitions feel like a flock
        // gathering rather than a snap.
        if (!p._init) { p.x = tx; p.y = ty; p._init = true; }
        const smooth = 0.18;
        p.x += (tx - p.x) * smooth;
        p.y += (ty - p.y) * smooth;

        // Particles in the wave glow brighter (it reads as energy gathering)
        // and shrink slightly so the line stays crisp; idle motes are softer.
        const baseTw = 0.25 + 0.75 * (0.5 + 0.5 * Math.sin(t * p.twinkleSpeed + p.phase));
        const tw = baseTw * (1 - excited * 0.25) + excited * 0.55;
        const sz = p.size * (1 - excited * 0.25) + excited * 0.9;

        // Spark colour adapts to theme so it's never invisible:
        //   dark theme → warm-white motes (additive glow on near-black)
        //   light theme → brand violet/magenta tint with multiply blend (set
        //                  via CSS) so the dots tint the cream rather than
        //                  bleach into it. Brand designer's call — sparks
        //                  become a dedicated light-mode tell.
        let r, g, b;
        if (themeRef.current === "light") {
          r = 168 + Math.floor(p.hueShift * 30);   // #a78bfa-ish
          g = 130 + Math.floor((1 - p.hueShift) * 35);
          b = 234;
        } else {
          r = 240 + Math.floor(p.hueShift * 15);
          g = 220 + Math.floor((1 - p.hueShift) * 25);
          b = 245;
        }
        ctx.beginPath();
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${tw.toFixed(3)})`;
        ctx.arc(p.x, p.y, sz, 0, Math.PI * 2);
        ctx.fill();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [size]);

  // Audio-reactive animation — every dynamic value is driven by REAL audio
  // level. No fake sine-wave breathing: when there is true silence (e.g. the
  // user has muted, or it's a pause in conversation), the blob is honestly
  // still. Counter-rotating iridescence and turbulence drift remain — those
  // are decorative surface motion, not "alive" cues.
  useEffect(() => {
    let raf;
    let levelSmoothed = 0;       // attack/decay so the blob doesn't twitch
    let lastT = performance.now();
    const tick = () => {
      const eng = engineRef?.current;
      const mic = eng ? eng.getMicLevel() : 0;
      const out = eng ? eng.getOutLevel() : 0;
      const t = performance.now();
      const dt = Math.min(64, t - lastT);
      lastT = t;

      // True audio level — clamped, peak of mic / out.
      const rawLevel = Math.min(1, Math.max(mic * 1.9, out * 2.2));
      // Asymmetric smoothing: snap up (60 ms) for responsiveness, decay
      // slowly (260 ms) so a syllable doesn't strobe the blob.
      const tau = rawLevel > levelSmoothed ? 60 : 260;
      levelSmoothed += (rawLevel - levelSmoothed) * Math.min(1, dt / tau);
      const level = levelSmoothed;

      // Scale: ALL energy from real audio. With level=0 the blob sits at 1.0
      // (no breathing). At loud audio it swells up to ~1.18.
      const scale = 1 + level * 0.18;
      if (sphereRef.current) {
        sphereRef.current.style.transform = `scale(${scale.toFixed(3)})`;
      }
      if (haloRef.current) {
        // A faint baseline halo so the blob has a soft outer glow even when
        // silent (you'd still see it as a sphere with a rim of light), but
        // the dramatic bloom happens only with real audio.
        const ha = 0.18 + level * 0.65;
        const hs = 1 + level * 0.5;
        haloRef.current.style.opacity = ha.toFixed(2);
        haloRef.current.style.transform = `scale(${hs.toFixed(3)})`;
      }

      // Counter-rotating iridescent gradients — these are surface DECORATION,
      // not life signs, so they keep drifting regardless of audio. They make
      // the orb a beautiful object to look at when silent.
      if (irisARef.current) {
        const rotA = (t * 0.012) % 360;
        irisARef.current.setAttribute("transform", `rotate(${rotA.toFixed(2)} ${size / 2} ${size / 2})`);
      }
      if (irisBRef.current) {
        const rotB = -((t * 0.028) % 360);
        irisBRef.current.setAttribute("transform", `rotate(${rotB.toFixed(2)} ${size / 2} ${size / 2})`);
      }

      // The "soul" — wanders on a Lissajous orbit (decorative motion, keeps
      // drifting), but its size and brightness now scale only with real audio.
      if (soulRef.current) {
        const r = size * 0.16;
        const sx = size * 0.5 + Math.cos(t * 0.00033) * r * 1.1;
        const sy = size * 0.5 + Math.sin(t * 0.00041) * r * 0.85;
        soulRef.current.setAttribute("cx", sx.toFixed(2));
        soulRef.current.setAttribute("cy", sy.toFixed(2));
        soulRef.current.setAttribute("r", (size * 0.13 + level * size * 0.06).toFixed(2));
        soulRef.current.style.opacity = (0.65 + level * 0.3).toFixed(2);
      }

      // SVG turbulence — silhouette ripples (surface decoration, audio-agnostic).
      if (turbRef.current) {
        const fx = 0.008 + 0.004 * Math.sin(t * 0.0004);
        const fy = 0.011 + 0.005 * Math.cos(t * 0.00035);
        turbRef.current.setAttribute("baseFrequency", `${fx.toFixed(4)} ${fy.toFixed(4)}`);
      }

      if (rimRef.current) {
        rimRef.current.style.opacity = (0.45 + level * 0.45).toFixed(2);
      }

      // Sound-wave rings — emit outward from the blob's edge. With no real
      // audio, the rings are essentially invisible (tiny floor so they don't
      // pop into existence the moment someone speaks). With audio they bloom.
      const wRefs = [wave1Ref.current, wave2Ref.current, wave3Ref.current, wave4Ref.current];
      const blobR = size * 0.41;
      const outerR = size * 0.72;
      const period = 2400;     // ms per full ring cycle
      const ambient = 0.012;   // near-invisible floor — silence reads as still
      const audioBloom = level * 0.85;
      for (let i = 0; i < wRefs.length; i++) {
        const el = wRefs[i];
        if (!el) continue;
        const phase = (((t + i * (period / wRefs.length)) % period) / period);
        const r = blobR + (outerR - blobR) * phase;
        const fadeIn = Math.min(1, phase / 0.12);
        const fadeOut = (1 - phase) * (1 - phase);
        const op = (ambient + audioBloom) * fadeIn * fadeOut * 1.2;
        const sw = 0.8 + level * 2.2 + (1 - phase) * 0.4;
        el.setAttribute("r", r.toFixed(2));
        el.setAttribute("stroke-opacity", op.toFixed(3));
        el.setAttribute("stroke-width", sw.toFixed(2));
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [engineRef, size]);

  const palette = paletteFor(mode, theme);

  return html`
    <div ref=${wrapRef} class="vb-wrap" style=${{ width: size, height: size }}>
      <div
        ref=${haloRef}
        class="vb-halo"
        style=${{
          background: `radial-gradient(circle, ${palette.halo} 0%, transparent 65%)`,
        }}
      ></div>
      <!--
        Sound-wave rings: live behind the orb, extend OUTSIDE the wrap (hence
        overflow:visible). Each is a stroke-only circle whose r and opacity are
        driven by rAF — see the wave loop above. The rings act as the orb's
        outer breathing aura: a subtle, always-on glow that pulses outward when
        someone speaks, hinting "this is alive, this is listening."
      -->
      <svg
        viewBox=${`0 0 ${size} ${size}`}
        width=${size}
        height=${size}
        class="vb-waves"
        style=${{ overflow: "visible" }}
      >
        <circle ref=${wave1Ref} cx=${size / 2} cy=${size / 2} r=${size * 0.41}
                fill="none" stroke=${palette.rim} stroke-opacity="0" stroke-width="1"
                vector-effect="non-scaling-stroke" />
        <circle ref=${wave2Ref} cx=${size / 2} cy=${size / 2} r=${size * 0.41}
                fill="none" stroke=${palette.warm} stroke-opacity="0" stroke-width="1"
                vector-effect="non-scaling-stroke" />
        <circle ref=${wave3Ref} cx=${size / 2} cy=${size / 2} r=${size * 0.41}
                fill="none" stroke=${palette.violet} stroke-opacity="0" stroke-width="1"
                vector-effect="non-scaling-stroke" />
        <circle ref=${wave4Ref} cx=${size / 2} cy=${size / 2} r=${size * 0.41}
                fill="none" stroke=${palette.rim} stroke-opacity="0" stroke-width="1"
                vector-effect="non-scaling-stroke" />
      </svg>
      <svg
        viewBox=${`0 0 ${size} ${size}`}
        width=${size}
        height=${size}
        class="vb-svg"
      >
        <defs>
          <radialGradient id=${`vb-iris-a-${size}`} cx="48%" cy="38%" r="65%">
            <stop offset="0%" stop-color=${palette.center} stop-opacity="0.92" />
            <stop offset="40%" stop-color=${palette.warm} stop-opacity="0.72" />
            <stop offset="70%" stop-color=${palette.cool} stop-opacity="0.65" />
            <stop offset="100%" stop-color=${palette.rim} stop-opacity="0.5" />
          </radialGradient>
          <radialGradient id=${`vb-iris-b-${size}`} cx="62%" cy="62%" r="60%">
            <stop offset="0%" stop-color=${palette.warm} stop-opacity="0.55" />
            <stop offset="55%" stop-color=${palette.violet} stop-opacity="0.55" />
            <stop offset="100%" stop-color=${palette.violet} stop-opacity="0" />
          </radialGradient>
          <radialGradient id=${`vb-soul-${size}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stop-color="#ffffff" stop-opacity="0.85" />
            <stop offset="55%" stop-color=${palette.soulMid} stop-opacity="0.22" />
            <stop offset="100%" stop-color=${palette.soulMid} stop-opacity="0" />
          </radialGradient>
          <radialGradient id=${`vb-hi2-${size}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stop-color="#ffffff" stop-opacity="0.5" />
            <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
          </radialGradient>
          <!-- Rim is the brand signature — hue locked; light theme just feathers
               wider (88%→100% with lower max α) so the silhouette dissolves
               into cream instead of stamping. -->
          <radialGradient id=${`vb-rim-${size}`} cx="50%" cy="50%" r="50%">
            <stop offset=${theme === "light" ? "88%" : "92%"} stop-color=${palette.rim} stop-opacity="0" />
            <stop offset=${theme === "light" ? "97%" : "97%"} stop-color=${palette.rim} stop-opacity=${theme === "light" ? "0.4" : "0.6"} />
            <stop offset="100%" stop-color=${palette.rim} stop-opacity=${theme === "light" ? "0.18" : "0.3"} />
          </radialGradient>
          <filter id=${`vb-blur-${size}`} x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
          <filter id=${`vb-ripple-${size}`} x="-20%" y="-20%" width="140%" height="140%">
            <feTurbulence ref=${turbRef} type="fractalNoise" baseFrequency="0.01 0.012" numOctaves="2" seed="3" />
            <feDisplacementMap in="SourceGraphic" scale="${(size * 0.018).toFixed(1)}" />
          </filter>
          <clipPath id=${`vb-clip-${size}`}>
            <circle cx=${size / 2} cy=${size / 2} r=${size * 0.41} />
          </clipPath>
        </defs>

        <g
          ref=${sphereRef}
          style=${{
            transformOrigin: `${size / 2}px ${size / 2}px`,
            transition: "transform 80ms linear",
          }}
        >
          <g clip-path=${`url(#vb-clip-${size})`}>
            <!-- Base fill is theme-aware: deep ink on dark, warm-cream on light,
                 so the iris radial gradients (which have alpha holes) don't reveal
                 a muddy near-black on a cream page. -->
            <circle cx=${size / 2} cy=${size / 2} r=${size * 0.41} fill=${palette.baseFill} />

            <g filter=${`url(#vb-ripple-${size})`}>
              <g ref=${irisARef}>
                <circle cx=${size / 2} cy=${size / 2} r=${size * 0.41} fill=${`url(#vb-iris-a-${size})`} />
              </g>
              <g ref=${irisBRef}>
                <circle cx=${size / 2} cy=${size / 2} r=${size * 0.41} fill=${`url(#vb-iris-b-${size})`} />
              </g>
            </g>

            <!-- The wandering soul — bright caustic that drifts inside -->
            <circle
              ref=${soulRef}
              cx=${size * 0.4}
              cy=${size * 0.4}
              r=${size * 0.13}
              fill=${`url(#vb-soul-${size})`}
              filter=${`url(#vb-blur-${size})`}
            />

            <!-- Small secondary reflection -->
            <ellipse
              cx=${size * 0.68}
              cy=${size * 0.72}
              rx=${size * 0.09}
              ry=${size * 0.055}
              fill=${`url(#vb-hi2-${size})`}
              filter=${`url(#vb-blur-${size})`}
              opacity="0.7"
            />

            <!-- Faint inner ambient haze -->
            <ellipse
              cx=${size * 0.45}
              cy=${size * 0.68}
              rx=${size * 0.22}
              ry=${size * 0.08}
              fill="#ffffff"
              opacity="0.05"
              filter=${`url(#vb-blur-${size})`}
            />
          </g>

          <!-- Rim light (outside the clip so it stays sharp) -->
          <circle
            ref=${rimRef}
            cx=${size / 2}
            cy=${size / 2}
            r=${size * 0.41}
            fill=${`url(#vb-rim-${size})`}
          />
        </g>
      </svg>
      <canvas ref=${sparkRef} class="vb-spark"></canvas>
    </div>
  `;
}

function paletteFor(mode, theme = "dark") {
  // Two base palettes — dark = the original tuning (glow-on-black),
  // light = panel-reviewed adaptations:
  //   - rim hue locked (it's the brand signature) but lightness dropped ~6%
  //   - halo shifted to warmer rose (#fb7185) at same alpha so radiance survives cream
  //   - cyan deepened to #67e8f9 so iridescence reads on a high-key bg
  //   - `baseFill` and `soulMid` adapt so alpha layers stack cleanly on warm vs. dark
  const idle = theme === "light" ? {
    center: "#fde7f3",
    warm: "#f9a8d4",
    cool: "#67e8f9",
    violet: "#a78bfa",
    rim: "#e8909c",
    halo: "rgba(251, 113, 133, 0.32)",
    baseFill: "#fff5f9",
    soulMid: "#e0f2fe",
  } : {
    center: "#fde7f3",
    warm: "#fbcfe8",
    cool: "#a5f3fc",
    violet: "#c4b5fd",
    rim: "#fda4af",
    halo: "rgba(244, 114, 182, 0.35)",
    baseFill: "#0e0a1a",
    soulMid: "#ffffff",
  };
  if (mode === "listen")   return { ...idle, cool:   theme === "light" ? "#0ea5e9" : "#7dd3fc", halo: "rgba(56, 189, 248, 0.45)" };
  if (mode === "speak")    return { ...idle, warm:   "#fb7185", halo: "rgba(251, 113, 133, 0.55)" };
  if (mode === "thinking") return { ...idle, violet: theme === "light" ? "#9333ea" : "#c084fc", halo: "rgba(192, 132, 252, 0.5)" };
  if (mode === "error")    return { ...idle, rim:    "#f87171", halo: "rgba(248, 113, 113, 0.5)" };
  // "calling" — pre-connection ringing state. Reads as warmer than idle but
  // calmer than "speak". Particles stay in their Lissajous wander (NOT in
  // the wave-line snap that live-call modes use) — the dial state should
  // feel like she's gathering herself before she speaks.
  if (mode === "calling")  return { ...idle, warm: "#fbbf24", rim: theme === "light" ? "#f59e0b" : "#fcd34d", halo: "rgba(251, 191, 36, 0.32)" };
  return idle;
}
