import React, { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { createRoot } from "react-dom/client";
import htm from "htm";
// marked is the markdown renderer powering MarkdownEditor's preview pane.
// Pinned to v12 (the last commonjs-shim-free release before v13's ESM
// reshuffle). Loaded from esm.sh so it benefits from the same browser
// import-map cache as React.
import { marked } from "https://esm.sh/marked@12.0.2";
// EasyMDE — the WYSIWYG-ish markdown editor powering every prompt field
// on the dashboard. Loaded as a UMD <script> tag in index.html (NOT via
// esm.sh) because EasyMDE bundles CodeMirror 5 with `window.CodeMirror`
// globals that don't survive a strict ESM conversion. The script tag
// exposes `window.EasyMDE`; we read it inside the React wrapper at
// mount time with a brief retry loop in case the script hasn't finished
// parsing before React's first paint.

import { AudioEngine } from "/static/audio-engine.js?v=23";
import { VoiceBlob } from "/static/voice-blob.js?v=34";

const html = htm.bind(React.createElement);

// marked is configured once at module load: GitHub-Flavoured Markdown for
// tables/strikethrough/etc., `breaks:true` so a single newline becomes a
// <br> (operators don't expect to need a blank line between paragraphs in
// a prompt editor), no header IDs (we never link to them), no XHTML.
marked.use({ async: false, gfm: true, breaks: true });

// Interaction model
// ─────────────────
//   landing      tap blob               → start a session
//   in a call    tap blob OR mute pill  → toggle mute
//                long-press blob OR hangup pill → end the call
//   any time     tap ⋯ (top-right)      → open the tweaks drawer (saved
//                                          agents + Gemini Live params)

const LONG_PRESS_MS = 550;
const TWEAKS_KEY = "spiderx_eva_tweaks_v1";
const AUTH_KEY = "sxai.user";
const THEME_KEY = "sxai.theme";

// Canonical bundle version. MUST stay in sync with index.html's
// <script src="app.js?v=N"> and backend/app.py's APP_BUILD constant. On
// boot we hit /api/build; if the server reports a newer number, the user
// is running a stale cache — we force-reload once (guarded by
// sessionStorage so a misconfigured CDN can't cause an infinite loop).
const SXAI_BUILD = 213;
(function () {
  if (typeof window === "undefined" || typeof fetch === "undefined") return;
  fetch("/api/build", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const serverBuild = data && Number(data.build);
      if (!Number.isFinite(serverBuild)) return;
      if (serverBuild <= SXAI_BUILD) return;
      // Stale bundle. Reload at most once per (build, session) pair so a
      // server permanently stuck at v=N+1 can't infinite-loop us.
      const guard = `sxai.reloaded_for_build_${serverBuild}`;
      try { if (sessionStorage.getItem(guard) === "1") return; } catch {}
      try { sessionStorage.setItem(guard, "1"); } catch {}
      // Cache-bust the URL so we don't immediately rehydrate from the same
      // stale ServiceWorker / disk cache entry.
      const sep = window.location.search ? "&" : "?";
      const url = `${window.location.pathname}${window.location.search}${sep}sxbuild=${serverBuild}${window.location.hash || ""}`;
      window.location.replace(url);
    })
    .catch(() => {});
})();

// ─────────────────────────────────────────────────────────────────────────
// Theme — light by default to match the builder/dashboard. Persists in
// localStorage; applied to <html data-theme="..."> so CSS variables flip
// across the whole tree (landing, Eva, dashboard).
// ─────────────────────────────────────────────────────────────────────────
function loadTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (t === "dark" || t === "light") return t;
  } catch {}
  return "light";   // default — matches the dashboard
}
function applyTheme(t) {
  try {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem(THEME_KEY, t);
  } catch {}
}
// Apply once at module-load so the first paint already has the right colors
// — no flash from default-dark CSS while React mounts.
if (typeof document !== "undefined") applyTheme(loadTheme());

// ─────────────────────────────────────────────────────────────────────────
// Auth state — stub today (no password check; eventual Auth0 will replace
// the source of truth). We patch window.fetch once at module load so every
// API call carries `X-User-Id` without each call-site remembering. The WS
// helper appends `?user_id=` so Eva can stamp ownership on new agents.
// ─────────────────────────────────────────────────────────────────────────
const SIGNED_OUT_KEY = "sxai.signed_out";

function loadAuth() {
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}
function saveAuth(user) {
  try {
    localStorage.setItem(AUTH_KEY, JSON.stringify(user));
    // Successful auth lifts the signed-out sentinel so a future cold visit
    // re-auto-signs-in (the "fake login" experience).
    localStorage.removeItem(SIGNED_OUT_KEY);
  } catch {}
}
function clearAuth() {
  try {
    localStorage.removeItem(AUTH_KEY);
    // Sentinel: tells the boot auto-sign-in path to back off until the user
    // explicitly logs in again. Without this, /api/me's founder fallback
    // would silently re-sign-in on the next page load.
    localStorage.setItem(SIGNED_OUT_KEY, "1");
  } catch {}
}
function isSignedOut() {
  try { return localStorage.getItem(SIGNED_OUT_KEY) === "1"; } catch { return false; }
}
function currentUserId() {
  return loadAuth()?.id;
}
// Stable per-build session id, shared across the wizard, chat, and voice
// surfaces so a mid-build switch resumes the SAME server-side build_session
// (template + captured answers). Created lazily; cleared on agent_saved.
function ensureBuildSid() {
  let sid = null;
  try { sid = sessionStorage.getItem("eva_build_sid"); } catch {}
  if (!sid) {
    sid = (typeof crypto !== "undefined" && crypto.randomUUID)
      ? crypto.randomUUID()
      : "fb-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { sessionStorage.setItem("eva_build_sid", sid); } catch {}
  }
  return sid;
}
function userInitials(u) {
  if (!u) return "?";
  const src = (u.name || u.email || "?").trim();
  const parts = src.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0][0].toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

// ─────────────────────────────────────────────────────────────────────────
// pronouns(agent) — build 187. Pre-187 the dashboard hard-coded "she" /
// "her" everywhere, which read wrong when Eva created a male-named agent
// (Rohan, Vikram, Arjun). compose_dynamic_agent now picks a gender + a
// matching TTS voice (Aoede/Leda/Kore/Zephyr ↔ female, Charon/Fenrir/
// Puck/Orus ↔ male) and stores it on `variables.gender`. This helper
// returns the pronoun set so the headline can read "Rohan is ready to
// take his first call" / "Priya is ready to take her first call" /
// "Sam is ready to take their first call" without each call-site
// reimplementing the lookup.
//
// Resolution order:
//   1. agent.variables.gender ("female" / "male" / "neutral")
//   2. infer from agent.voice (female voices vs male voices)
//   3. default to a NAME-FIRST set (use the agent's name + "they") so
//      we never confidently misgender an old agent
//
// Each accessor (.subj / .obj / .poss / .reflexive) is a function so
// the caller can request capitalised forms when the pronoun starts a
// sentence ("He is..." vs "...with him").
// Build 210 — prettify an unknown enum slug so the dashboard never
// shows a raw value like `personal_services` to the operator. Used as
// the fallback in every `labelFor(list, id)` lookup — if the id isn't
// in the presets registry (sector renamed / removed), we still
// produce "Personal Services" instead of the slug.
function _prettifyEnumId(id) {
  if (!id) return "—";
  return String(id)
    .replace(/[_\-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

const _FEMALE_VOICES_FE = new Set(["Aoede", "Leda", "Kore", "Zephyr"]);
const _MALE_VOICES_FE   = new Set(["Charon", "Fenrir", "Puck", "Orus"]);
// Build 210 — name-based gender inference. Voice was being trusted as
// the second signal but Eva's save path sometimes stamps a female-voice
// default on an obviously-male name (Rohan + Aoede → "her" copy across
// every page). A short list of common South-Asian + English given
// names corrects the most-visible cases. The operator-set
// `variables.gender` still wins; the voice still backstops a name we
// don't recognise.
const _MALE_NAMES_FE = new Set([
  "rohan","vikram","arjun","raj","rahul","amit","ravi","sam","sameer",
  "ali","ahmed","aditya","akash","ajay","ankit","ankur","anil","abhi",
  "abhishek","aman","aryan","ashok","atul","bharat","chetan","dev",
  "deepak","dinesh","gaurav","gopal","harish","hari","ishaan","jay",
  "kabir","karan","kartik","krishna","manav","manish","mohit","naveen",
  "neeraj","nikhil","nitin","pavan","piyush","prashant","praveen","pratik",
  "rajesh","rakesh","ramesh","rishabh","rishi","ritesh","rohit","sandeep",
  "sanjay","santosh","sachin","saurabh","shashank","shyam","siddharth",
  "sumit","sunil","suresh","tarun","umesh","varun","vinod","vivek","yash",
  "alex","andrew","ben","ben","brian","chris","dan","daniel","david",
  "ed","james","jack","jake","john","luke","mark","matt","mike","michael",
  "nate","nick","paul","peter","ryan","steve","stephen","tom","will",
  "fenrir","charon",
]);
const _FEMALE_NAMES_FE = new Set([
  "priya","anjali","riya","maya","neha","pooja","kavita","kavya","kiran",
  "lakshmi","lalita","meera","mira","nisha","payal","preeti","radhika",
  "rashmi","rekha","ritika","sangeeta","sapna","seema","shilpa","shreya",
  "simran","sneha","sonia","sunita","swati","tanvi","tara","trisha","uma",
  "vandana","vidya","aanya","aarya","aisha","ananya","aparna","asha",
  "deepika","diya","divya","fatima","gauri","hema","ishita","jyoti",
  "kajal","kalpana","komal","leela","madhuri","manisha","mohini","nandini",
  "naina","nehal","nitya","palak","pallavi","parul","poonam","priyanka",
  "ragini","rani","reshma","richa","ridhi","ruchi","ruchika","sakshi",
  "saloni","sandhya","saraswati","savita","shanti","sharmila","shobha",
  "shradha","sita","smita","sushma","tanya","tina","varsha","veena","vinita",
  "yamini","zoya","zoe","alice","amy","anna","beth","claire","emma",
  "emily","eva","grace","jane","julia","kate","laura","lily","mary",
  "olivia","sara","sarah","sophie","tina","aoede","leda","kore",
]);
function _resolveGender(agent) {
  if (!agent) return "neutral";
  const stored = String(agent.variables?.gender || "").trim().toLowerCase();
  if (stored === "female" || stored === "male" || stored === "neutral") return stored;
  // Name-first inference — most operators don't set gender explicitly,
  // and the agent's name is the strongest signal (Eva called him Rohan
  // → he's a he, regardless of which TTS voice the save path picked).
  const firstName = String(agent.name || "")
    .trim()
    .split(/\s+/)[0]
    .toLowerCase()
    .replace(/[^a-z]/g, "");
  if (_MALE_NAMES_FE.has(firstName))   return "male";
  if (_FEMALE_NAMES_FE.has(firstName)) return "female";
  const voice = String(agent.voice || "").trim();
  if (_FEMALE_VOICES_FE.has(voice)) return "female";
  if (_MALE_VOICES_FE.has(voice)) return "male";
  return "neutral";
}
function pronouns(agent) {
  const g = _resolveGender(agent);
  const name = agent?.name || "the agent";
  // Singular-they for neutral so the copy stays grammatical without
  // resorting to the agent's name on every reference (which reads
  // robotic at higher densities).
  if (g === "male") {
    return {
      gender: "male",
      subj: "he", subjCap: "He",
      obj:  "him", objCap:  "Him",
      poss: "his", possCap: "His",
      reflexive: "himself",
      verb: (sing, plur) => sing,  // "is", "has"
    };
  }
  if (g === "female") {
    return {
      gender: "female",
      subj: "she", subjCap: "She",
      obj:  "her", objCap:  "Her",
      poss: "her", possCap: "Her",
      reflexive: "herself",
      verb: (sing, plur) => sing,
    };
  }
  return {
    gender: "neutral",
    subj: "they", subjCap: "They",
    obj:  "them", objCap:  "Them",
    poss: "their", possCap: "Their",
    reflexive: "themself",
    // Singular-they STILL takes plural-form auxiliaries in modern English
    // ("they are ready", not "they is ready"). Callers that need verb
    // agreement use pron.verb("is", "are") and we hand back the right form.
    verb: (sing, plur) => plur,
  };
}

(function patchFetch() {
  if (typeof window === "undefined" || !window.fetch || window.__sxaiFetchPatched) return;
  const original = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : (input?.url || "");
    // Only stamp our own API routes; leave esm.sh / static assets alone.
    if (url.startsWith("/api/") || url.startsWith(location.origin + "/api/")) {
      const uid = currentUserId();
      if (uid) {
        const headers = new Headers(init.headers || (input?.headers) || {});
        if (!headers.has("X-User-Id")) headers.set("X-User-Id", String(uid));
        init = { ...init, headers };
      }
    }
    return original(input, init);
  };
  window.__sxaiFetchPatched = true;
})();

const Icons = {
  mic: html`<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>`,
  micOff: html`<svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M9 9v2a3 3 0 0 0 4.5 2.6"/><path d="M15 15a3 3 0 0 0 .5-1V6a3 3 0 0 0-6-.6"/><path d="M5 11a7 7 0 0 0 1.2 3.9"/><path d="M17.6 17.6A7 7 0 0 0 19 11"/></svg>`,
  hang: html`<svg viewBox="0 0 24 24"><path d="M2 13a14 14 0 0 1 20 0l-2.4 2.4a2 2 0 0 1-2.6.2l-2-1.5a2 2 0 0 1-.7-2.1l.6-2a10 10 0 0 0-5.8 0l.6 2a2 2 0 0 1-.7 2.1l-2 1.5a2 2 0 0 1-2.6-.2L2 13z"/></svg>`,
  dots: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="6" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="18" cy="12" r="1.5"/></svg>`,
  close: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
};

function fmtTimer(secs) {
  const s = Math.max(0, Math.floor(secs));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function loadTweaks() {
  try { return JSON.parse(localStorage.getItem(TWEAKS_KEY) || "{}"); }
  catch { return {}; }
}
function saveTweaks(t) {
  try { localStorage.setItem(TWEAKS_KEY, JSON.stringify(t)); } catch {}
}

function tweaksQuery(tweaks) {
  const q = {};
  for (const [k, v] of Object.entries(tweaks || {})) {
    if (v === undefined || v === null || v === "") continue;
    q[k] = typeof v === "boolean" ? (v ? "1" : "0") : String(v);
  }
  return q;
}

// ───────────────────────── Type-to-Eva ────────────────────────────────────
//
// A persistent text rail at the bottom of the call screen — a robust fallback
// for when the mic isn't getting through to Gemini. Pressing Enter sends the
// text turn over the same WS the audio is using; Eva sees it as a user turn
// and responds in voice as usual. Cuts the "Eva keeps greeting" loop because
// even if the audio path is silent, a typed sentence reaches her clearly.

function TypeRail({ wsRef, placeholder = "Or type to Eva…" }) {
  const [val, setVal] = useState("");
  const submit = (e) => {
    e?.preventDefault();
    const text = val.trim();
    if (!text) return;
    const ws = wsRef?.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "text", text }));
    }
    setVal("");
  };
  return html`
    <div class="type-rail">
      <form onSubmit=${submit}>
        <input
          type="text"
          placeholder=${placeholder}
          value=${val}
          onInput=${(e) => setVal(e.target.value)}
          autoFocus
          autoComplete="off"
          spellCheck="false"
        />
        <button type="submit" disabled=${!val.trim()}>Send</button>
      </form>
    </div>
  `;
}

// ───────────────────────── Live captions ─────────────────────────────────
//
// The blob is the protagonist; the chat drawer is a sidekick. Captions are
// the *acting subtitles* — they sit just below the blob, two lines of clean
// typography that mirror what was just said. They cement understanding
// without ever competing with the orb for the eye.
//
// Layout:
//   ┌── caller line (dim, smaller) ───────────────┐  ← what *you* just said
//   ┌── agent line  (light, larger) ──────────────┐  ← what the agent is saying
//
// Each line fades out a few seconds after the speaker finishes their turn,
// so the screen breathes between exchanges.

function CaptionRail({ userLine, agentLine, agentName, transcriptLen, onOpenChat }) {
  // We still render an *invisible* container when both lines are empty IF
  // there are completed turns, so the "View full chat" affordance remains
  // available in the gaps between turns.
  const hasLines = !!(userLine || agentLine);
  if (!hasLines && transcriptLen === 0) return null;
  return html`
    <div class="captions">
      ${transcriptLen > 0 ? html`
        <button class="cap-history-btn" onClick=${onOpenChat} aria-label="View full conversation">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
          <span>View full chat · ${transcriptLen} ${transcriptLen === 1 ? "turn" : "turns"}</span>
        </button>` : ""}
      ${userLine ? html`
        <div class="cap cap-user">
          <span class="cap-tag">You</span>
          <span class="cap-text">${userLine}</span>
        </div>` : ""}
      ${agentLine ? html`
        <div class="cap cap-agent">
          <span class="cap-tag">${agentName || "Eva"}</span>
          <span class="cap-text">${agentLine}</span>
        </div>` : ""}
    </div>
  `;
}

// ───────────────────────── Chat panel ────────────────────────────────────
//
// The full conversation, top-to-bottom, scrollable. Opens as a glass card
// from the bottom that takes about half the screen so the orb is still
// visible above it (you can see it pulse while reading). Auto-scrolls to
// the bottom as new turns land so a long call never makes you chase the
// latest line. Close button + click-outside both dismiss it.

function fmtTime(ts) {
  try { return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}

function ChatPanel({ open, transcript, agentName, onClose }) {
  const scrollRef = useRef(null);
  useEffect(() => {
    // Auto-scroll to the latest turn whenever the panel is opened OR a new
    // turn arrives. Smooth scroll feels human; an instant jump would be
    // jarring during an active call.
    if (!open) return;
    const el = scrollRef.current;
    if (!el) return;
    requestAnimationFrame(() => {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
  }, [open, transcript.length]);

  if (!open) return null;
  return html`
    <div class="chatpanel-scrim" onClick=${onClose}></div>
    <aside class="chatpanel" role="dialog" aria-label="Conversation history">
      <header class="chatpanel-head">
        <div class="title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          </svg>
          <span>Conversation</span>
          <span class="count">${transcript.length} ${transcript.length === 1 ? "turn" : "turns"}</span>
        </div>
        <button class="x" onClick=${onClose} aria-label="Close conversation">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
            <path d="M6 6l12 12M18 6L6 18"/>
          </svg>
        </button>
      </header>
      <div class="chatpanel-body" ref=${scrollRef}>
        ${transcript.length === 0 ? html`
          <div class="chatpanel-empty">No turns yet. Speak or type to begin the conversation.</div>
        ` : transcript.map((m, i) => html`
          <div key=${i} class=${"chatmsg chatmsg-" + (m.role === "user" ? "user" : "agent")}>
            <div class="who">
              <span class="role">${m.role === "user" ? "You" : (agentName || "Eva")}</span>
              ${m.ts ? html`<span class="ts">${fmtTime(m.ts)}</span>` : ""}
            </div>
            <div class="text">${m.text}</div>
          </div>
        `)}
      </div>
    </aside>
  `;
}

// ───────────────────────── Agent cockpit (post-build) ────────────────────
//
// What the user sees the moment Eva hands off. Replaces the old single
// reveal card with a richer dashboard: hero CTAs for Test / Go live front
// and centre, then context cards (How they behave, Connected to, Hand to
// your team, API) below. The bells-and-whistles section is where the user
// can invite team-mates / ops to handle the finer config (webhook URL,
// variables, outcomes) — keeping the founder out of yak-shaving.

// ───────────────────────── Theatrical unveal ─────────────────────────────
//
// The few seconds between Eva's final "she's all set" and the cockpit
// appearing should feel like a small reveal — not a dialog popping up.
// We stage four beats over ~3.2s:
//   0.0–0.6s   Eva's iridescent orb shrinks, the screen darkens.
//   0.6–1.4s   "MEET" eyebrow fades up.
//   1.4–2.4s   The agent's name flips in with a chromatic shimmer.
//   2.4–3.2s   A one-line tagline (sector + first thing she does) settles.
// At 3.2s we call onDone() and hand off to the cockpit.

// playRevealChime — a short, uplifting major-chord swell synthesized
// with the Web Audio API (no asset to ship, no network fetch). Plays
// once when the reveal mounts: an ascending C-major arpeggio
// (C5→E5→G5→C6) under a soft master swell that fades out over ~2.8s.
// Triangle waves keep it warm rather than harsh. Wrapped in try/catch
// + a suspended-context resume() since the reveal fires a beat after
// the operator's click (their user-activation usually still covers it).
function playRevealChime() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    if (ctx.state === "suspended") { try { ctx.resume(); } catch {} }
    const now = ctx.currentTime;
    const master = ctx.createGain();
    master.gain.setValueAtTime(0.0001, now);
    master.gain.exponentialRampToValueAtTime(0.16, now + 0.45);
    master.gain.exponentialRampToValueAtTime(0.0001, now + 2.8);
    master.connect(ctx.destination);
    // A gentle high "sparkle" reverb-ish tail via a second softer layer.
    const notes = [
      { f: 523.25, t: 0.00 },  // C5
      { f: 659.25, t: 0.13 },  // E5
      { f: 783.99, t: 0.26 },  // G5
      { f: 1046.50, t: 0.42 }, // C6 — resolves up, "lift"
    ];
    notes.forEach(({ f, t }) => {
      const osc = ctx.createOscillator();
      osc.type = "triangle";
      osc.frequency.value = f;
      const g = ctx.createGain();
      const start = now + t;
      g.gain.setValueAtTime(0.0001, start);
      g.gain.linearRampToValueAtTime(0.5, start + 0.05);
      g.gain.exponentialRampToValueAtTime(0.0008, start + 2.2);
      osc.connect(g); g.connect(master);
      osc.start(start);
      osc.stop(start + 2.4);
    });
    setTimeout(() => { try { ctx.close(); } catch {} }, 3300);
  } catch {}
}

function TheatricalUnveal({ agent, presets, onDone }) {
  useEffect(() => {
    if (!agent) return;
    // Uplifting score the moment the curtain lifts.
    playRevealChime();
    const t = setTimeout(() => onDone(), 3300);
    return () => clearTimeout(t);
  }, [agent?.id]);
  if (!agent) return null;
  const labelFor = (list, id) => (list || []).find((x) => x.id === id)?.label || _prettifyEnumId(id);
  const sector = labelFor(presets?.sectors, agent.sector);
  const locale = labelFor(presets?.locales, agent.locale);
  return html`
    <div class="unveal" role="presentation">
      <div class="unveal-curtain"></div>
      <div class="unveal-spot"></div>
      <!-- Light SpiderX logo — the reveal canvas is near-black, so we
           force the wordmark white (the SVG wordmark is currentColor;
           the red X accent stays). Sits at the top as a brand anchor. -->
      <div class="unveal-brand">
        <${SpiderXLogo} height=${26} />
      </div>
      <div class="unveal-eyebrow">— meet —</div>
      <h1 class="unveal-name">
        ${agent.name.split("").map((ch, i) => html`
          <span class="unveal-ch" style=${{ animationDelay: `${1.0 + i * 0.06}s` }} key=${i}>${ch}</span>
        `)}
      </h1>
      <div class="unveal-tagline">
        ${sector ? html`<span>${sector}</span>` : ""}
        ${locale ? html`<span class="dot">·</span><span>${locale}</span>` : ""}
        ${agent.voice ? html`<span class="dot">·</span><span>${voiceTag(agent.voice)}</span>` : ""}
      </div>
      <button class="unveal-skip" onClick=${onDone}>Skip →</button>
    </div>
  `;
}

function AgentCockpit({ agent, presets, onTest, onEdit, onGoLive, onDismiss, onTestPhone, plan, stats }) {
  if (!agent) return null;
  // stats + plan are now lifted to App so they can also drive other surfaces
  // (e.g. call-log). The cockpit just renders.

  // Step state — Test → Knowledge → Tools → Go Live → Performance. Guardrails
  // used to be its own step but every SMB owner accepts the same universally-
  // good defaults (no medical/legal advice, no PII read aloud, human handoff
  // on request), so the step was pure decision fatigue. The rules are now
  // surfaced as a read-only reassurance strip on Go Live, and the full
  // toggle library lives in the Advanced tweaks drawer for power users.
  const STEPS = [
    { id: "test",        label: "Test it" },
    { id: "knowledge",   label: "Knowledge" },
    { id: "tools",       label: "Tools" },
    { id: "live",        label: "Go live" },
    { id: "performance", label: "Performance" },
  ];
  const [stepIdx, setStepIdx] = useState(0);
  // When stats arrive and the agent has calls, jump to the Performance step
  // unless the user has already navigated somewhere intentional.
  useEffect(() => {
    if (stats?.total > 0 && stepIdx === 0) {
      setStepIdx(STEPS.length - 1);
    }
  }, [stats?.total]);
  const step = STEPS[stepIdx];
  const goNext = () => setStepIdx((i) => Math.min(STEPS.length - 1, i + 1));
  const goBack = () => setStepIdx((i) => Math.max(0, i - 1));
  const goJump = (id) => setStepIdx(STEPS.findIndex((s) => s.id === id));

  // Recent calls — fetched when we land on the Performance step (or the
  // cockpit opens for an agent that already has calls).
  const [calls, setCalls] = useState([]);
  useEffect(() => {
    if (!agent?.id || step.id !== "performance") return;
    fetch(`/api/agents/${agent.id}/calls?limit=20`)
      .then((r) => r.json()).then((arr) => setCalls(Array.isArray(arr) ? arr : []))
      .catch(() => {});
  }, [agent?.id, step.id]);

  // Region-aware go-live default — derived from the agent's locale.
  const goLiveRegion = (() => {
    const loc = (agent.locale || navigator.language || "en-US").toUpperCase();
    if (loc.includes("-IN")) return { region: "India",      provider: "GTS",    note: "Indian DID, voice & SMS" };
    if (loc.includes("-SG")) return { region: "Singapore",  provider: "GTS",    note: "SG DID via GTS" };
    if (loc.includes("-US")) return { region: "US",         provider: "Twilio", note: "US DID, voice & SMS" };
    if (loc.includes("-GB") || loc.includes("-UK")) return { region: "UK",     provider: "Twilio", note: "UK DID via Twilio" };
    if (loc.includes("-AU")) return { region: "Australia",  provider: "Twilio", note: "AU DID via Twilio" };
    return { region: "your region", provider: "Twilio", note: "Twilio is the default — Plivo, Telnyx & Vonage also supported" };
  })();

  const labelFor = (list, id) => (list || []).find((x) => x.id === id)?.label || _prettifyEnumId(id);
  const sectorLabel = labelFor(presets?.sectors, agent.sector);
  const localeLabel = labelFor(presets?.locales, agent.locale);
  const connectors = (agent.connectors || []).map((id) => ({ id, label: labelFor(presets?.connectors, id) }));
  const outcomes = agent.outcomes || [];
  const vad = agent.voice_tweaks || {};
  const policy = agent.policy || {};
  const variablesCount = Object.keys(agent.variables || {}).length;
  const hasWebhook = !!(agent.webhook_url && agent.webhook_url.trim());

  // Tagline — prefer a short persona, else first sentence of system_prompt,
  // else sector+locale. Cap to ~140 chars so it never crowds the head card.
  const taglineRaw = (agent.persona || "").trim() || (agent.system_prompt || "").split(/[.!?]/)[0] || "";
  const tagline = taglineRaw.length > 140 ? taglineRaw.slice(0, 138).trimEnd() + "…" : taglineRaw;
  const fallbackTagline = `${sectorLabel || "Phone agent"} · ${localeLabel || ""}`;

  return html`
    <div class="cockpit">
      <div class="cockpit-bg" onClick=${onDismiss}></div>
      <div class="cockpit-shell">
        <header class="cockpit-head">
          <div class="cockpit-eyebrow">
            <span>Step ${stepIdx + 1} of ${STEPS.length}</span>
            <span class="cockpit-plan">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
              ${plan?.label || "Free"} · ${plan?.minutesLeft ?? 300} min left
            </span>
          </div>
          <div class="cockpit-id">
            <div class="cockpit-thumb"></div>
            <div>
              <h1 class="cockpit-name">${agent.name}</h1>
              <p class="cockpit-tagline">${tagline || fallbackTagline}</p>
              <div class="cockpit-pills">
                ${sectorLabel ? html`<span class="pill">${sectorLabel}</span>` : ""}
                ${localeLabel ? html`<span class="pill">${localeLabel}</span>` : ""}
                ${agent.voice ? html`<span class="pill accent">${voiceTag(agent.voice)}</span>` : ""}
                ${stats && stats.total > 0 ? html`<span class="pill">${stats.total} ${stats.total === 1 ? "call" : "calls"}</span>` : ""}
              </div>
            </div>
          </div>
          <button class="cockpit-close" onClick=${onDismiss} aria-label="Close">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 6l12 12M18 6L6 18"/></svg>
          </button>
        </header>

        <!-- Step rail — five dots, click a label to jump. The current step
             has a filled dot; completed steps tick. Linear feel but you can
             skip ahead if you want. -->
        <nav class="cockpit-rail" aria-label="Setup steps">
          ${STEPS.map((s, i) => html`
            <button key=${s.id} class=${"rail-step " + (i === stepIdx ? "active" : (i < stepIdx ? "done" : ""))} onClick=${() => setStepIdx(i)}>
              <span class="rail-dot">${i < stepIdx ? "✓" : (i + 1)}</span>
              <span class="rail-label">${s.label}</span>
            </button>
          `)}
        </nav>

        <!-- The one card visible at a time. -->
        <section class="cockpit-step">
          ${step.id === "test" ? html`
            <header class="step-head">
              <h2>Take ${agent.name} for a spin</h2>
              <p>Two ways. Web chat is instant; phone test calls you back on any number you own.</p>
            </header>
            <div class="step-options">
              <button class="ck-cta ck-cta-primary" onClick=${onTest}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
                <div class="ck-cta-body">
                  <div class="ck-cta-title">Call ${agent.name} in the browser</div>
                  <div class="ck-cta-sub">Talk to her right now — no phone number needed</div>
                </div>
              </button>
              <${PhoneTestForm} agentName=${agent.name} onSubmit=${onTestPhone} />
            </div>
          ` : ""}

          ${step.id === "knowledge" ? html`
            <header class="step-head">
              <h2>Add knowledge ${agent.name} can rely on</h2>
              <p>Upload menus, FAQs, price lists, intake forms. ${agent.name} cites only what you give her — no hallucinated answers.</p>
            </header>
            <div class="step-body">
              <div class="step-dropzone">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
                <div>
                  <div class="dz-title">Drop PDFs, docs or URLs here</div>
                  <div class="dz-sub">or paste in the editor — coming soon, hand-off to ops works today</div>
                </div>
              </div>
              <button class="step-skip" onClick=${onEdit}>Open knowledge editor →</button>
            </div>
          ` : ""}

          ${step.id === "tools" ? html`
            <header class="step-head">
              <h2>What ${agent.name} can do for callers</h2>
              <p>One-click connect — we handle the plumbing on our side, you just say yes.</p>
            </header>
            <div class="step-body">
              <ul class="step-tools">
                ${(connectors.length > 0 ? connectors : [
                  { id: "calendar_check", label: "Book on your calendar" },
                  { id: "sms_send", label: "Text the caller a confirmation" },
                ]).map((c) => html`
                  <li key=${c.id}>
                    <span class="tool-check">${connectors.find((x) => x.id === c.id) ? "✓" : "○"}</span>
                    <span>${c.label}</span>
                  </li>
                `)}
                <li>
                  <span class="tool-check">✓</span>
                  <span>Every call auto-tagged (booked, lead, no-answer, escalated)</span>
                </li>
              </ul>
              <button class="step-skip" onClick=${onEdit}>Connect calendar, CRM or sheets →</button>
            </div>
          ` : ""}

          ${step.id === "performance" ? html`
            <header class="step-head">
              <h2>How ${agent.name} is doing</h2>
              <p>Total ${stats?.total || 0} ${stats?.total === 1 ? "call" : "calls"} · Outcomes auto-classified by <code>end_call</code>. Webhook ${(agent.webhook_url || "").trim() ? "fires on every call" : "not yet configured"}.</p>
            </header>
            <div class="step-body">
              ${stats?.outcomes?.length > 0 ? html`
                <div class="perf-outcomes">
                  ${stats.outcomes.map((o) => html`
                    <div class="perf-outcome" key=${o.outcome}>
                      <div class="perf-outcome-count">${o.count}</div>
                      <div class="perf-outcome-label">${o.outcome.replace(/_/g, " ")}</div>
                    </div>
                  `)}
                </div>
              ` : ""}
              ${calls.length > 0 ? html`
                <ul class="call-log">
                  ${calls.map((c) => html`
                    <li key=${c.id} class="call-row">
                      <div class="call-row-head">
                        <span class=${"call-outcome call-outcome-" + (c.outcome || "unknown")}>${(c.outcome || "unknown").replace(/_/g, " ")}</span>
                        <span class="call-time">${fmtTime(c.started_at)}</span>
                        <span class="call-dur">${Math.round(c.duration_s || 0)}s</span>
                      </div>
                      ${c.summary ? html`<div class="call-summary">${c.summary}</div>` : ""}
                    </li>
                  `)}
                </ul>
              ` : html`
                <div class="perf-empty">
                  <div class="perf-empty-title">No calls yet.</div>
                  <div class="perf-empty-sub">
                    Once ${agent.name} answers ${pronouns(agent).poss} first call, the log + outcomes
                    will land here. Tap <b>Test it</b> to give ${pronouns(agent).obj} a dry run, or
                    <b>Go live</b> to wire a real number.
                  </div>
                </div>
              `}
            </div>
          ` : ""}

          ${step.id === "live" ? html`
            <header class="step-head">
              <h2>Take ${agent.name} live</h2>
              <p>Pick how callers will reach her. Defaults are based on your locale; everything else is one click.</p>
            </header>
            <div class="step-body">
              <div class="live-card">
                <div class="live-card-head">
                  <span class="live-tag">Default · ${goLiveRegion.region}</span>
                  <span class="live-provider">${goLiveRegion.provider}</span>
                </div>
                <div class="live-card-title">Get a real phone number</div>
                <div class="live-card-sub">${goLiveRegion.note}</div>
                <button class="ck-cta ck-cta-primary live-card-cta" onClick=${onGoLive}>
                  <span>Wire ${goLiveRegion.provider} now</span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
                </button>
                <div class="live-alts">
                  <span>or use</span>
                  <button class="live-alt">Plivo</button>
                  <button class="live-alt">Telnyx</button>
                  <button class="live-alt">Vonage</button>
                  <button class="live-alt">Exotel</button>
                </div>
              </div>
              <div class="live-card live-card-secondary">
                <div class="live-card-title">Or embed on your site</div>
                <div class="live-card-sub">Copy a 2-line snippet, visitors can talk to ${agent.name} from any page.</div>
                <button class="ck-cta ck-cta-secondary live-card-cta" onClick=${() => navigator.clipboard?.writeText(`<script src="${location.origin}/embed.js" data-agent="${agent.slug || agent.id}"></script>`)}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
                  <span>Copy embed snippet</span>
                </button>
              </div>
              <div class="live-safety">
                <span class="live-safety-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3l8 4v5c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V7l8-4z"/></svg>
                </span>
                <div class="live-safety-body">
                  <div class="live-safety-title">${agent.name} plays it safe by default</div>
                  <div class="live-safety-rules">
                    Won't give medical, legal or financial advice ·
                    never reads card numbers, OTPs or passwords aloud ·
                    hands off to a human if a caller asks twice.
                    <button class="live-safety-link" onClick=${onEdit}>Fine-tune →</button>
                  </div>
                </div>
              </div>
            </div>
          ` : ""}
        </section>

        <!-- Step nav — singular flow. Back / Skip-for-now / Next. -->
        <footer class="cockpit-foot">
          <button class="cockpit-link" onClick=${stepIdx === 0 ? onDismiss : goBack}>
            ← ${stepIdx === 0 ? "Back to home" : STEPS[stepIdx - 1].label}
          </button>
          <span class="cockpit-foot-id">Agent #${agent.id}${agent.slug ? ` · /agent/${agent.slug}` : ""}</span>
          ${stepIdx < STEPS.length - 1 ? html`
            <button class="cockpit-next" onClick=${goNext}>
              ${["test", "knowledge", "tools", "live"].includes(step.id) ? "Skip · " : ""}${STEPS[stepIdx + 1].label} →
            </button>
          ` : html`
            <button class="cockpit-next" onClick=${onDismiss}>Done →</button>
          `}
        </footer>
      </div>
    </div>
  `;
}

// Phone-test mini-form: enter a number, "Call me". For now it's a stub that
// flashes a hint — real outbound-call dial requires Twilio (US/UK) or GTS
// (IN/SG) wired with PUBLIC_HOST. We surface the UX shape now and wire the
// outbound endpoint when the user's phone provider is set up.
function PhoneTestForm({ agentName, onSubmit }) {
  const [num, setNum] = useState("");
  const submit = (e) => { e?.preventDefault(); if (num.trim()) onSubmit?.(num.trim()); };
  return html`
    <form class="phone-test" onSubmit=${submit}>
      <div class="phone-test-head">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M2 13a14 14 0 0 1 20 0l-2.4 2.4a2 2 0 0 1-2.6.2l-2-1.5a2 2 0 0 1-.7-2.1l.6-2a10 10 0 0 0-5.8 0l.6 2a2 2 0 0 1-.7 2.1l-2 1.5a2 2 0 0 1-2.6-.2L2 13z"/></svg>
        <div>
          <div class="pt-title">Or get a real call on your phone</div>
          <div class="pt-sub">${agentName} will ring you in a few seconds.</div>
        </div>
      </div>
      <div class="phone-test-row">
        <input class="pt-input" type="tel" placeholder="+1 555 123 4567 (your number)"
               value=${num} onInput=${(e) => setNum(e.target.value)} />
        <button class="pt-go" type="submit" disabled=${!num.trim()}>Call me</button>
      </div>
    </form>
  `;
}

function GoLiveModal({ agent, onClose }) {
  // Country options — kept tight on purpose; the long tail goes via the
  // "anywhere else" free-text fallback. Default infers from agent.locale.
  const COUNTRIES = [
    { id: "IN", label: "India", dial: "+91", placeholder: "+91 98XXXXXXXX" },
    { id: "US", label: "United States", dial: "+1", placeholder: "+1 555 0100" },
    { id: "GB", label: "United Kingdom", dial: "+44", placeholder: "+44 7700 900000" },
    { id: "SG", label: "Singapore", dial: "+65", placeholder: "+65 8000 0000" },
    { id: "AE", label: "United Arab Emirates", dial: "+971", placeholder: "+971 50 000 0000" },
    { id: "AU", label: "Australia", dial: "+61", placeholder: "+61 400 000 000" },
    { id: "other", label: "Somewhere else (tell us)", dial: "", placeholder: "+ country code & number" },
  ];
  const defaultCountry = (() => {
    const loc = (agent?.locale || "").toUpperCase();
    const match = COUNTRIES.find((c) => loc.endsWith("-" + c.id));
    return match?.id || "IN";
  })();

  const [country, setCountry] = useState(defaultCountry);
  const [city, setCity] = useState("");
  const [handle, setHandle] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(null);   // { id } when submitted
  const [error, setError] = useState("");
  const [showManual, setShowManual] = useState(false);

  const countryObj = COUNTRIES.find((c) => c.id === country) || COUNTRIES[0];
  const canSubmit = handle.trim().length > 4 && !submitting && agent?.id;

  const submit = async (e) => {
    e?.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError("");
    try {
      const res = await fetch("/api/number-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: agent.id,
          country: countryObj.label,
          city: city.trim() || null,
          delivery_handle: handle.trim(),
          notes: notes.trim() || null,
        }),
      });
      if (!res.ok) throw new Error("server " + res.status);
      const data = await res.json();
      setDone({ id: data.id });
    } catch (err) {
      setError("Couldn't submit — check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  // Manual-setup content for the rare user who wants to wire Twilio themselves.
  const inferredHost = (typeof window !== "undefined" && window.location.host.includes("ngrok"))
    ? window.location.host : "your-host.ngrok-free.app";
  const twimlUrl = `https://${inferredHost}/api/sip/twilio/twiml/${agent?.id}`;

  return html`
    <div class="golive-modal" onClick=${(e) => e.target.classList.contains("golive-modal") && onClose()}>
      <div class="golive-card">
        ${done ? html`
          <h2>You're all set — number on the way.</h2>
          <p class="lede">
            We're picking a ${countryObj.label} number for ${agent?.name || "your agent"} and pointing it at ${pronouns(agent).obj} now.
            You'll get the number on ${handle.trim()} — usually within 1 working hour.
          </p>
          <div class="golive-success">
            <div class="golive-success-row">
              <span class="golive-success-icon">✓</span>
              <span>Request <b>#${done.id}</b> received</span>
            </div>
            <div class="golive-success-row">
              <span class="golive-success-icon">⏱</span>
              <span>Avg. turnaround: under 1 working hour</span>
            </div>
            <div class="golive-success-row">
              <span class="golive-success-icon">↳</span>
              <span>Need it sooner? WhatsApp us on
                <a class="golive-link" href="https://wa.me/918100000000?text=Hi%2C%20I%20just%20requested%20a%20number%20for%20${encodeURIComponent(agent?.name || 'my agent')}%20(request%20%23${done.id})" target="_blank" rel="noopener">+91 81000 00000</a>.
              </span>
            </div>
          </div>
          <div class="golive-actions">
            <button class="copy" onClick=${onClose}>Done</button>
          </div>
        ` : html`
          <h2>Take ${agent?.name || "your agent"} live</h2>
          <p class="lede">
            Tell us where your callers are — we'll set up a real number and text it to you.
            No webhooks, no config: we handle the wiring on our side.
          </p>

          <form class="golive-form" onSubmit=${submit}>
            <label class="golive-field">
              <span class="golive-label">Where are most callers?</span>
              <select class="golive-select" value=${country} onChange=${(e) => setCountry(e.target.value)}>
                ${COUNTRIES.map((c) => html`<option key=${c.id} value=${c.id}>${c.label}</option>`)}
              </select>
            </label>

            <label class="golive-field">
              <span class="golive-label">City or area <span class="golive-opt">(optional)</span></span>
              <input class="golive-input" type="text" placeholder="e.g. Bengaluru, NYC, Greater London"
                     value=${city} onInput=${(e) => setCity(e.target.value)} />
            </label>

            <label class="golive-field">
              <span class="golive-label">Where should we send the number?</span>
              <input class="golive-input" type="tel" placeholder=${countryObj.placeholder}
                     value=${handle} onInput=${(e) => setHandle(e.target.value)} required />
              <span class="golive-help">We'll WhatsApp / SMS the number here once it's live.</span>
            </label>

            ${error ? html`<div class="golive-error">${error}</div>` : ""}

            <div class="golive-actions">
              <button type="button" class="close" onClick=${onClose} disabled=${submitting}>Cancel</button>
              <button type="submit" class="copy" disabled=${!canSubmit}>
                ${submitting ? "Submitting…" : "Get my number"}
              </button>
            </div>
          </form>

          <button class="golive-manual-toggle" type="button" onClick=${() => setShowManual((v) => !v)}>
            ${showManual ? "Hide" : "I have a developer — show manual setup"}
          </button>
          ${showManual ? html`
            <div class="golive-manual">
              <div class="golive-step">
                <div class="num">Step 1 · Tunnel</div>
                <div class="text">
                  Run <code>ngrok http 8765</code> locally and copy the <code>*.ngrok-free.app</code> host.
                  Then add to your <code>.env</code>: <code>PUBLIC_HOST=yourhost.ngrok-free.app</code>.
                </div>
              </div>
              <div class="golive-step">
                <div class="num">Step 2 · Twilio number</div>
                <div class="text">
                  Buy or select a Twilio Voice number. In the Twilio console: <em>A call comes in</em> → Webhook (HTTP POST) →
                  <code>${twimlUrl}</code>
                </div>
              </div>
              <div class="golive-step">
                <div class="num">Step 3 · Call it</div>
                <div class="text">
                  Dial the Twilio number. ${agent?.name || "Your agent"} answers.
                </div>
              </div>
              <div class="golive-actions golive-actions-compact">
                <button class="copy" type="button" onClick=${async () => { try { await navigator.clipboard.writeText(twimlUrl); } catch {} }}>
                  Copy TwiML URL
                </button>
              </div>
            </div>
          ` : ""}
        `}
      </div>
    </div>
  `;
}

// ───────────────────────── Agent editor ──────────────────────────────────

// Canonical business-context fields Eva collects during the interview.
// Stored under `draft.variables` so they can be referenced as {{business_name}}
// etc. in greeting + system prompt. Surfacing them as labelled inputs (rather
// than a raw key=value blob) makes the editor feel like a real business
// profile, and ensures the agent always knows where, when, and on whose
// behalf it's speaking. Missing fields are still editable — that's the
// "add manually" path the user asked for.
// Country catalogue for the org + agent profile country dropdowns. ISO 3166-1
// alpha-2 codes — same shape the integration filter uses. Each entry carries
// the regional-indicator flag emoji so the integration filter + provider
// tags can show the flag inline instead of just the ISO code.
const COUNTRIES = [
  { id: "IN",    label: "India",                 flag: "🇮🇳" },
  { id: "US",    label: "United States",         flag: "🇺🇸" },
  { id: "GB",    label: "United Kingdom",        flag: "🇬🇧" },
  { id: "SG",    label: "Singapore",             flag: "🇸🇬" },
  { id: "AE",    label: "United Arab Emirates",  flag: "🇦🇪" },
  { id: "AU",    label: "Australia",             flag: "🇦🇺" },
  { id: "CA",    label: "Canada",                flag: "🇨🇦" },
  { id: "DE",    label: "Germany",               flag: "🇩🇪" },
  { id: "FR",    label: "France",                flag: "🇫🇷" },
  { id: "ES",    label: "Spain",                 flag: "🇪🇸" },
  { id: "BR",    label: "Brazil",                flag: "🇧🇷" },
  { id: "MX",    label: "Mexico",                flag: "🇲🇽" },
  { id: "ZA",    label: "South Africa",          flag: "🇿🇦" },
  { id: "JP",    label: "Japan",                 flag: "🇯🇵" },
  { id: "CN",    label: "China",                 flag: "🇨🇳" },
  { id: "OTHER", label: "Other / global",        flag: "🌐" },
];
// Tiny helper — returns the flag for an ISO-2 code or 🌐 for "GLOBAL" /
// unknown. Used by the integration filter pills and per-card country tags.
const flagFor = (id) => {
  if (!id) return "";
  if (id === "GLOBAL") return "🌐";
  return (COUNTRIES.find((c) => c.id === id)?.flag) || "🏳️";
};

// Per-day hours editor schema — Monday-first because we lean toward
// hospitality / clinics where the work-week ordering matches the brain.
const DAYS = [
  { id: "mon", label: "Mon" },
  { id: "tue", label: "Tue" },
  { id: "wed", label: "Wed" },
  { id: "thu", label: "Thu" },
  { id: "fri", label: "Fri" },
  { id: "sat", label: "Sat" },
  { id: "sun", label: "Sun" },
];

// Sector-specific profile schema. Every entry beyond the universal fields
// belongs to one sector. Keys are stored under agent.variables prefixed with
// the sector id so they don't collide across sectors (a salon's "service_menu"
// is different from a dental's). The Business profile page shows whichever
// section matches agent.sector; sectors without a schema gracefully fall back
// to the universal common-fields-only layout.
const SECTOR_PROFILE_SCHEMA = {
  restaurant: {
    label: "Restaurant",
    fields: [
      { key: "cuisine_type",       label: "Cuisine type",          placeholder: "e.g. South Indian, Italian, multi-cuisine" },
      { key: "seating_capacity",   label: "Seating capacity",      placeholder: "e.g. 60 covers across two floors" },
      { key: "party_size_max",     label: "Max party size",        placeholder: "e.g. 12" },
      { key: "reservation_policy", label: "Reservation policy",    placeholder: "Walk-ins welcome · 2hr table holds · Deposit for 6+", type: "textarea" },
      { key: "dietary_options",    label: "Dietary options",       placeholder: "Vegan, gluten-free, Jain, halal" },
      { key: "takeout_delivery",   label: "Takeout / delivery",    placeholder: "Takeout yes; delivery via Swiggy + Zomato" },
      { key: "kids_pets",          label: "Kids / pets",           placeholder: "Kid-friendly; well-behaved dogs allowed on the patio" },
      { key: "menu_link",          label: "Menu link",             placeholder: "https://example.com/menu" },
      { key: "private_events",     label: "Private events",        placeholder: "Yes — private dining room for 16, minimum spend ₹40k" },
    ],
    offers_examples: "Tonight: half-priced cocktails 6–8pm · Sunday brunch ₹999 thali · Friday live music starting 9pm",
  },
  salon: {
    label: "Salon / spa",
    fields: [
      { key: "service_categories", label: "Service categories",    placeholder: "Hair, skin, nails, waxing, facials", type: "textarea" },
      { key: "stylists",           label: "Stylists / therapists", placeholder: "Maya · Priya · Raj — all senior", type: "textarea" },
      { key: "walk_ins",           label: "Walk-ins",              placeholder: "Yes for nails / quick cuts; appointment for colour" },
      { key: "appointment_dur",    label: "Typical appointment",   placeholder: "30 min cut · 2hrs colour · 90 min facial" },
      { key: "cancellation",       label: "Cancellation policy",   placeholder: "24hr notice; otherwise 50% charge", type: "textarea" },
      { key: "loyalty",            label: "Loyalty / packages",    placeholder: "10-visit package at 15% off; birthday treatment free" },
    ],
    offers_examples: "Weekday 30% off colour · Free trim with every blow-dry on Tuesdays",
  },
  dental: {
    label: "Dental practice",
    fields: [
      { key: "services_offered",   label: "Services offered",      placeholder: "Cleanings, root canals, implants, orthodontics", type: "textarea" },
      { key: "providers",          label: "Dentists / providers",  placeholder: "Dr. Mehta (general) · Dr. Iyer (ortho)", type: "textarea" },
      { key: "insurance_accepted", label: "Insurance accepted",    placeholder: "Star Health, ICICI Lombard, cash" },
      { key: "new_patient_flow",   label: "New patient flow",      placeholder: "Forms in advance via SMS; bring govt ID" },
      { key: "emergency_policy",   label: "Emergency / after-hours", placeholder: "Call Dr. Mehta directly: 98-…; otherwise next morning" },
    ],
    offers_examples: "Free consultation for new patients · Family plan: ₹999/yr per family member",
  },
  healthcare: {
    label: "Healthcare clinic",
    fields: [
      { key: "specialties",        label: "Specialties",           placeholder: "General medicine, pediatrics, dermatology", type: "textarea" },
      { key: "providers",          label: "Doctors / providers",   placeholder: "Dr. Sharma (GP) · Dr. Banerjee (paeds)", type: "textarea" },
      { key: "insurance_accepted", label: "Insurance accepted",    placeholder: "Listed insurers + cash + UPI" },
      { key: "new_patient_flow",   label: "New patient flow",      placeholder: "Photo of govt ID + insurance card on WhatsApp before visit" },
      { key: "emergency_policy",   label: "Emergency policy",      placeholder: "Direct severe cases to nearest ER, share map link" },
    ],
    offers_examples: "Annual health-check at 25% off through March · Free dietician consultation with any package",
  },
  real_estate: {
    label: "Real estate",
    fields: [
      { key: "neighborhoods",      label: "Areas / neighborhoods", placeholder: "Indiranagar, Koramangala, Whitefield", type: "textarea" },
      { key: "property_types",     label: "Property types",        placeholder: "2/3 BHK apartments, builder floors, villas" },
      { key: "rent_or_sale",       label: "Rent or sale focus",    placeholder: "Sale primary, rentals via partner" },
      { key: "team_members",       label: "Team",                  placeholder: "Anita (sale) · Vikram (rentals)", type: "textarea" },
      { key: "viewing_policy",     label: "Viewings",              placeholder: "Sat/Sun blocks; weekdays by appointment" },
    ],
    offers_examples: "Zero brokerage on Indiranagar 3BHK listings this month · Free legal vetting with every purchase",
  },
  automotive: {
    label: "Automotive service",
    fields: [
      { key: "service_types",      label: "Service types",         placeholder: "Oil change, brakes, alignment, full service", type: "textarea" },
      { key: "brands_serviced",    label: "Brands serviced",       placeholder: "Maruti, Hyundai, Honda + multi-brand" },
      { key: "appointment_walkin", label: "Appointment vs walk-in", placeholder: "Walk-in for oil change; appointment for major work" },
      { key: "pickup_dropoff",     label: "Pickup / drop-off",     placeholder: "Free pickup within 5km" },
      { key: "warranty_policy",    label: "Warranty",              placeholder: "30-day warranty on labour, parts per manufacturer" },
    ],
    offers_examples: "Free brake check this week · 15% off full service on weekdays",
  },
  travel: {
    label: "Hotel / travel",
    fields: [
      { key: "room_types",         label: "Room types",            placeholder: "Deluxe, suite, family room", type: "textarea" },
      { key: "check_in_out",       label: "Check-in / out times",  placeholder: "Check-in 2 PM · Check-out 11 AM" },
      { key: "amenities",          label: "Amenities",             placeholder: "Pool, spa, gym, free Wi-Fi, parking", type: "textarea" },
      { key: "pet_policy",         label: "Pet policy",            placeholder: "Pets on request; ₹500/night" },
      { key: "cancellation",       label: "Cancellation policy",   placeholder: "Free cancel up to 48h before; 50% within 48h" },
    ],
    offers_examples: "Stay 3 nights, get the 4th free · Spa + breakfast package ₹2999",
  },
  retail: {
    label: "Retail / e-commerce",
    fields: [
      { key: "product_categories", label: "Product categories",    placeholder: "Apparel, home, accessories", type: "textarea" },
      { key: "return_policy",      label: "Return policy",         placeholder: "30 days, original tags, free returns" },
      { key: "delivery_areas",     label: "Delivery areas",        placeholder: "All India via Bluedart / DTDC" },
      { key: "store_locations",    label: "Store locations",       placeholder: "Bengaluru · Mumbai · Delhi" },
    ],
    offers_examples: "End-of-season 40% off · Free shipping over ₹1499",
  },
  logistics: {
    label: "Logistics / delivery",
    fields: [
      { key: "service_areas",      label: "Service areas",         placeholder: "Bengaluru metro · Tier-1 across India", type: "textarea" },
      { key: "service_types",      label: "Service types",         placeholder: "Same-day, next-day, intercity courier, bulk haul" },
      { key: "pickup_window",      label: "Pickup window",         placeholder: "Same-day if booked before 1pm" },
      { key: "rates",              label: "Rate cards",            placeholder: "Under 5kg: ₹150 · 5–20kg: ₹350 · Bulk POA", type: "textarea" },
      { key: "tracking_link",      label: "Tracking link",         placeholder: "https://yourshop.com/track" },
    ],
    offers_examples: "First pickup free for new accounts · Volume rebates at 100+ shipments/month",
  },
  education: {
    label: "Education / coaching",
    fields: [
      { key: "subjects",           label: "Subjects offered",      placeholder: "Math, Physics, English, Coding", type: "textarea" },
      { key: "age_groups",         label: "Age groups",            placeholder: "Grades 6–12, college, working professionals" },
      { key: "mode",               label: "Mode",                  placeholder: "Online + in-person at Indiranagar centre" },
      { key: "instructors",        label: "Instructors",           placeholder: "Anita (Math) · Rahul (Physics)", type: "textarea" },
      { key: "demo_policy",        label: "Free demo policy",      placeholder: "Free 1-hr demo before enrolling" },
    ],
    offers_examples: "Sibling discount 15% · Early-bird ₹500 off if you enrol by month-end",
  },
  events: {
    label: "Events / ticketing",
    fields: [
      { key: "event_types",        label: "Event types",           placeholder: "Concerts, comedy, conferences, weddings", type: "textarea" },
      { key: "venue_info",         label: "Venues",                placeholder: "Phoenix Auditorium · open-air at Cubbon Park", type: "textarea" },
      { key: "ticket_tiers",       label: "Ticket tiers",          placeholder: "GA, Premium, VIP — pricing per show" },
      { key: "refund_policy",      label: "Refund policy",         placeholder: "No refunds; transfer allowed up to 24h before" },
    ],
    offers_examples: "Group of 4: 10% off · Early-bird ends Friday",
  },
  legal: {
    label: "Legal intake",
    fields: [
      { key: "practice_areas",     label: "Practice areas",        placeholder: "Civil, family, corporate, IP", type: "textarea" },
      { key: "jurisdictions",      label: "Jurisdictions",         placeholder: "Karnataka High Court, Bengaluru civil courts" },
      { key: "consultation_policy", label: "Consultation policy",  placeholder: "30-min intake call free; in-person ₹2500" },
      { key: "team",               label: "Lawyers / paralegals",  placeholder: "Adv. Mehta (civil) · Adv. Reddy (corporate)", type: "textarea" },
    ],
    offers_examples: "First consultation free for women-led businesses · Pro-bono for select cases",
  },
  insurance: {
    label: "Insurance",
    fields: [
      { key: "product_lines",      label: "Product lines",         placeholder: "Health, motor, term, home", type: "textarea" },
      { key: "insurers_offered",   label: "Insurers represented",  placeholder: "Star, ICICI Lombard, HDFC Ergo", type: "textarea" },
      { key: "claims_support",     label: "Claims support",        placeholder: "End-to-end assistance; cashless network of 6,000 hospitals" },
    ],
    offers_examples: "First-year premium discount with bundle · Free annual policy review",
  },
  banking: {
    label: "Banking / financial",
    fields: [
      { key: "service_lines",      label: "Service lines",         placeholder: "Personal loans, business loans, investment advisory", type: "textarea" },
      { key: "branches",           label: "Branches",              placeholder: "Bengaluru · Mumbai · online" },
      { key: "kyc_policy",         label: "KYC policy",            placeholder: "Aadhaar + PAN + selfie; eKYC supported" },
    ],
    offers_examples: "0% processing fee on personal loans till Friday · 8% on savings for first year",
  },
  saas_support: {
    label: "SaaS support / IT helpdesk",
    fields: [
      { key: "products",           label: "Products supported",    placeholder: "Anchor SaaS dashboard, mobile app, integrations", type: "textarea" },
      { key: "sla",                label: "SLA",                   placeholder: "P1 < 1hr · P2 < 4hr · P3 < 1 business day" },
      { key: "tiers",              label: "Support tiers",         placeholder: "Free, Pro, Enterprise — different response times" },
      { key: "escalation",         label: "Escalation path",       placeholder: "Tier 1 → Tier 2 engineer → CTO on call" },
    ],
    offers_examples: "Free onboarding session for new Pro accounts · Quarterly health-check on Enterprise plans",
  },
  // Catch-all for businesses that don't fit a vertical. Closes the
  // "generic receptionist has no industry-specific form fields" gap from
  // the audit — operator still sees a useful structured form, just with
  // sector-neutral labels.
  generic: {
    label: "General service business",
    fields: [
      { key: "what_we_do",         label: "What we do",            placeholder: "One paragraph in plain language. Eva will use this verbatim.", type: "textarea" },
      { key: "primary_audience",   label: "Who calls us",          placeholder: "e.g. local residents, returning customers, new prospects" },
      { key: "service_areas",      label: "Service area",          placeholder: "e.g. Bengaluru metro · pan-India online" },
      { key: "key_offerings",      label: "Key offerings",         placeholder: "Top 3-5 things you want callers to know you do", type: "textarea" },
      { key: "pricing_signals",    label: "Pricing signals",       placeholder: "e.g. 'starts at ₹999' · 'quotes after intake'" },
      { key: "escalation_policy",  label: "Escalation policy",     placeholder: "When to hand off to a human — urgent topics, VIPs, complaints" },
    ],
    offers_examples: "New-customer 10% off · Free first consultation · Referral bonus ₹500",
  },
};

const CANONICAL_VARS = [
  { key: "business_name", label: "Business name",        placeholder: "e.g. BrightSmile Dental",            type: "text" },
  { key: "industry",      label: "Industry / specialty", placeholder: "e.g. Family dentistry, B2B SaaS",    type: "text", help: "Free text — narrower than the sector dropdown above." },
  { key: "country",       label: "Country",              placeholder: "e.g. India",                          type: "text" },
  { key: "city",          label: "City",                 placeholder: "e.g. Bengaluru",                      type: "text" },
  { key: "address",       label: "Address",              placeholder: "Street, area, pin",                   type: "text" },
  { key: "timezone",      label: "Timezone",             placeholder: "e.g. Asia/Kolkata",                   type: "text", help: "IANA tz name. Drives 'today', 'tomorrow', 'this afternoon' phrasing." },
  { key: "hours",         label: "Business hours",       placeholder: "Mon–Sat 9 AM – 9 PM, closed Sunday",  type: "textarea" },
  { key: "website",       label: "Website",              placeholder: "https://brightsmile.example",         type: "text" },
  { key: "phone",         label: "Escalation phone",     placeholder: "+91 80 1234 5678",                    type: "text", help: "Number a human can be reached on if the caller asks to escalate." },
  { key: "notification_phone", label: "Post-call SMS phone", placeholder: "+91 98765 43210",                type: "text", help: "Where post-call summary SMS goes (paid plans). Often a separate WhatsApp/personal number from the escalation line." },
  { key: "email",         label: "Contact email",        placeholder: "hello@brightsmile.example",           type: "text" },
  { key: "services",      label: "Services offered",     placeholder: "Cleaning, root canal, orthodontics…", type: "textarea" },
  { key: "languages",     label: "Languages spoken",     placeholder: "English, Hindi, Kannada",             type: "text", help: "Tell the agent which languages it may switch to mid-call." },
];
const CANONICAL_KEYS = new Set(CANONICAL_VARS.map((v) => v.key));

function AgentEditor({ draft, updateDraft, updateTweak, toggleArr, presets, schema, saveEdit, closeEdit, saveState, onTest }) {
  const sectors = presets?.sectors || [];
  const locales = presets?.locales || [];
  const voices = presets?.voices || [];
  const guardrails = presets?.guardrails || [];
  const connectors = presets?.connectors || [];

  const tv = draft.voice_tweaks || {};
  const vars = draft.variables || {};
  const updateVar = (k, v) => {
    const next = { ...(draft.variables || {}) };
    if (v === "" || v == null) delete next[k];
    else next[k] = v;
    updateDraft("variables", next);
  };
  const otherVars = Object.entries(vars).filter(([k]) => !CANONICAL_KEYS.has(k));
  const filledCount = CANONICAL_VARS.filter((c) => (vars[c.key] || "").toString().trim()).length;

  return html`
    <div class="tw-editor">
      <button class="back" onClick=${closeEdit}>
        <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>
        <span>Back to all agents</span>
      </button>

      <div class="heading">
        <div class="thumb"></div>
        <div>
          <div class="name">${draft.name || "Untitled"}</div>
          <div class="sub">#${draft.id} · created ${(draft.created_at || "").slice(0, 10)}</div>
        </div>
      </div>

      <div class="tw-jump">
        This panel is focused on the business profile. Persona, knowledge, guardrails, voice tuning, integrations, and developer settings each live on their own page in the left nav.
      </div>

      <div class="tw-section-title">
        Business profile
        <span class="tw-section-pill">${filledCount}/${CANONICAL_VARS.length} filled</span>
      </div>
      <div class="tw-help-top">
        Anything Eva captured in the interview shows up here pre-filled. Anything still blank is fair game — the more ${draft.name || "your agent"} knows about the business, the less it has to improvise on calls.
      </div>

      <div class="tw-grid-2">
        <div class=${"tw-field" + (draft.sector ? "" : " tw-field-empty")}>
          <div class="label-row">
            <span class="name">Sector</span>
            ${draft.sector ? html`<span class="tw-field-tick">✓</span>` : ""}
          </div>
          <select class="tw-select" value=${draft.sector || ""} onChange=${(e) => updateDraft("sector", e.target.value)}>
            <option value="">—</option>
            ${sectors.map((s) => html`<option key=${s.id} value=${s.id}>${s.label}</option>`)}
          </select>
        </div>
        <div class=${"tw-field" + (draft.locale ? "" : " tw-field-empty")}>
          <div class="label-row">
            <span class="name">Locale</span>
            ${draft.locale ? html`<span class="tw-field-tick">✓</span>` : ""}
          </div>
          <select class="tw-select" value=${draft.locale || ""} onChange=${(e) => updateDraft("locale", e.target.value)}>
            <option value="">—</option>
            ${locales.map((l) => html`<option key=${l.id} value=${l.id}>${l.label}</option>`)}
          </select>
        </div>
        ${CANONICAL_VARS.map((c) => html`
          <div class=${"tw-field" + ((vars[c.key] || "").toString().trim() ? "" : " tw-field-empty")} key=${c.key}>
            <div class="label-row">
              <span class="name">${c.label}</span>
              ${(vars[c.key] || "").toString().trim() ? html`<span class="tw-field-tick">✓</span>` : ""}
            </div>
            ${c.help ? html`<div class="help">${c.help}</div>` : ""}
            ${c.type === "textarea" ? html`
              <textarea class="tw-textarea" rows="2" placeholder=${c.placeholder}
                        value=${vars[c.key] || ""}
                        onInput=${(e) => updateVar(c.key, e.target.value)}></textarea>
            ` : html`
              <input class="tw-input" type="text" placeholder=${c.placeholder}
                     value=${vars[c.key] || ""}
                     onInput=${(e) => updateVar(c.key, e.target.value)} />
            `}
          </div>
        `)}
      </div>

      <div class="tw-actions">
        <button class="cancel" onClick=${closeEdit}>Cancel</button>
        <button class="save" onClick=${saveEdit}>Save changes</button>
      </div>
      ${saveState.msg ? html`<div class=${"tw-savehint " + (saveState.cls || "")}>${saveState.msg}</div>` : ""}
    </div>
  `;
}

// ───────────────────────── Tweaks drawer ─────────────────────────────────

// ───────────────────────── Agents list page ──────────────────────────────
//
// Lives at /agents. Replaces the old tweaks-drawer "Your agents" tab. The
// page is intentionally minimal: a grid of agent cards, a "Build new" CTA
// up top, search-as-you-type if the list grows large. Clicking an agent
// routes to /agent/<slug> which mounts the cockpit (where Test / Go-live /
// Performance lives).

// ─────────────────────────────────────────────────────────────────────────
// MarkdownEditor — thin React wrapper around EasyMDE. EasyMDE replaces the
// underlying <textarea> with a CodeMirror surface that renders bold,
// italic, headings, lists, links, and inline code visually inline as the
// operator types — so the editor "looks like a word processor with
// markdown markers visible" rather than "raw source you can preview".
//
// Stored value remains plain markdown text — Gemini Live reads markdown
// fine, and any downstream TTS path can render or strip it without
// behavioural change. This keeps the on-the-wire shape identical to the
// previous textarea-based editor, so no save_args or DB rows need to
// change.
//
// API kept stable with the prior version so the seven call-sites
// (system_prompt, greeting, extra_info notes, knowledge notes, custom
// dos/don'ts, offers) need no changes. `compact` collapses the toolbar
// to bold/italic/list/link + skips side-by-side, `defaultMode="split"`
// auto-toggles EasyMDE's built-in side-by-side preview on mount.
// ─────────────────────────────────────────────────────────────────────────
function MarkdownEditor({
  value,
  onChange,
  rows = 12,
  placeholder = "",
  className = "",
  monospace = false,
  compact = false,
  defaultMode = "edit",
}) {
  // The <textarea> EasyMDE replaces. We never style it directly — EasyMDE
  // hides it and renders a CodeMirror surface in its place.
  const taRef = useRef(null);
  // The EasyMDE instance. Stored in a ref so the cleanup useEffect can
  // tear it down, and so the external-value sync useEffect can call
  // .value() without re-running on every render.
  const mdeRef = useRef(null);
  // The most recent value EasyMDE itself produced. We compare against
  // this in the external-sync useEffect so a re-render triggered by our
  // OWN onChange doesn't loop back into mde.value(...) (which would
  // jump the caret to the end).
  const lastEmittedRef = useRef(value || "");
  // Stable handle to the latest onChange so the EasyMDE listener — set
  // up once at mount — always calls the freshest callback.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!taRef.current || mdeRef.current) return undefined;
    let cancelled = false;
    let retryTimer = null;

    const mount = () => {
      if (cancelled || mdeRef.current) return;
      const E = (typeof window !== "undefined") ? window.EasyMDE : null;
      if (!E) {
        // Script tag is still parsing. Retry a few times before
        // giving up — total wait < 2 s in the worst case.
        retryTimer = setTimeout(mount, 80);
        return;
      }
      if (!taRef.current) return;
      // Toolbar layout. This is a PROMPT editor — the only thing the
      // operator should see are the markdown actions themselves. We
      // drop EasyMDE's preview / side-by-side / fullscreen / undo /
      // redo / quote / horizontal-rule because:
      //   - in-source rendering already styles bold/italic/headings/
      //     lists visually inline, so preview panes are redundant
      //   - undo/redo are handled natively by the browser (Cmd/Ctrl+Z)
      //   - quote and hr are rarely used in agent prompts
      //   - fullscreen is overkill for a 16-row field
      // The compact and full toolbars both ship the same essentials;
      // compact only differs in editor min-height (set elsewhere).
      const toolbar = [
        "bold", "italic", "heading",
        "|", "unordered-list", "ordered-list",
        "|", "link", "code",
      ];
      try {
        const mde = new E({
          element: taRef.current,
          initialValue: value || "",
          placeholder: placeholder,
          spellChecker: false,
          status: false,
          autosave: { enabled: false },
          forceSync: true,
          minHeight: Math.max(80, rows * 22) + "px",
          toolbar: toolbar,
          renderingConfig: {
            codeSyntaxHighlighting: false,
            singleLineBreaks: true,
          },
          previewClass: ["md-preview"],
          lineWrapping: true,
        });
        mdeRef.current = mde;
        mde.codemirror.on("change", () => {
          const v = mde.value();
          lastEmittedRef.current = v;
          onChangeRef.current(v);
        });
        // No auto-open of side-by-side: in-source rendering already
        // styles bold/italic/headings/lists visually, so an extra
        // preview pane would be duplicate information. `defaultMode`
        // is kept on the prop signature for API stability but is now
        // a no-op.
      } catch (err) {
        // EasyMDE init failed (rare — usually means the script loaded
        // but CodeMirror's globals are missing). Log loudly so we can
        // tell from the console; the host textarea stays visible so
        // the operator can still type.
        // eslint-disable-next-line no-console
        console.error("MarkdownEditor: EasyMDE init failed:", err);
      }
    };

    mount();

    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      const m = mdeRef.current;
      if (m) {
        try { m.toTextArea(); } catch { /* harmless on hot reload */ }
        mdeRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External value-sync. Fires when a parent component replaces the
  // value (e.g. a "Reset" button, or a wizard-to-chat sync pulling in
  // the operator's already-answered facts). We skip the round-trip
  // when the change came from the user typing here — that path went
  // through onChange → lastEmittedRef already.
  useEffect(() => {
    const mde = mdeRef.current;
    if (!mde) return;
    const next = value || "";
    if (lastEmittedRef.current === next) return;
    lastEmittedRef.current = next;
    mde.value(next);
  }, [value]);

  // Render the host textarea. EasyMDE swaps it for its own surface on
  // mount, so this exists only briefly during the first render.
  return html`
    <div class=${"md-editor md-easy" + (compact ? " md-easy-compact" : "") + (monospace ? " md-easy-mono" : "") + (className ? " " + className : "")}>
      <textarea ref=${taRef} defaultValue=${value || ""}></textarea>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// DashboardShell — white-theme page chrome with persistent left nav. Used
// by /agents and /agent/<slug>/* routes. The landing splash and /build
// (Eva chat) keep the dark theme; this shell is a separate surface, so we
// don't pollute the marketing/builder feel with dashboard chrome.
// ─────────────────────────────────────────────────────────────────────────
// EmbedView — minimal surface served at /embed/<slug>. Loaded inside the
// floating iframe injected by /static/embed.js on third-party sites. Just
// the orb + a "Tap to talk to <agent>" CTA. No brandbar, no top nav, no
// landing chrome. Uses the SAME `openSession(agent_id)` path the dashboard
// uses, so the WebSocket + AudioEngine + VoiceBlob all just work.
function EmbedView({ slug, blobSize, blobMode, engineRef, onPressStart, onPressEnd, onPressCancel, onStart }) {
  const [agent, setAgent] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    if (!slug) return;
    fetch(`/api/agents/by-slug/${encodeURIComponent(slug)}`)
      .then((r) => r.ok ? r.json() : null)
      .then((a) => a ? setAgent(a) : setErr("Agent not found"))
      .catch(() => setErr("Couldn't reach SpiderX.AI"));
  }, [slug]);

  if (err) return html`<div class="embed-err">${err}</div>`;
  if (!agent) return html`<div class="embed-loading">Loading ${slug}…</div>`;

  return html`
    <div class="embed-shell">
      <div class="embed-orb">
        <button class="blob-tap" type="button"
                onPointerDown=${onPressStart}
                onPointerUp=${onPressEnd}
                onPointerLeave=${onPressCancel}
                onPointerCancel=${onPressCancel}
                aria-label=${`Tap to talk to ${agent.name}`}>
          <${VoiceBlob} engineRef=${engineRef} mode=${blobMode} size=${blobSize} />
        </button>
      </div>
      <div class="embed-meta">
        <div class="embed-name">${agent.name}</div>
        <div class="embed-sub">${agent.persona || "Tap to talk"}</div>
      </div>
      <button class="embed-cta" type="button" onClick=${() => onStart(agent.id)}>
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
        <span>Talk to ${agent.name}</span>
      </button>
      <a class="embed-brand" href="https://spiderx.ai" target="_blank" rel="noopener">
        Powered by SpiderX.AI
      </a>
    </div>
  `;
}

// CookieNotice — region-aware consent banner. Renders only on the landing
// (interior pages require sign-in, which is an explicit consent moment). We
// don't currently set advertising / analytics cookies, just functional
// localStorage (theme, locale, auth), but the notice is required practice
// in EU/UK/CA/IN regardless.
//
// Buckets:
//   EU/EEA + UK  → GDPR — explicit accept/decline split
//   US California → CCPA — opt-out wording ("Do Not Sell / Share")
//   India        → DPDP Act 2023 — notice + accept
//   Other        → polite single-button acknowledgement
//
// Choice persists in localStorage as `sxai.cookie_consent: <region>:<choice>`.
function CookieNotice({ locale }) {
  const STORE_KEY = "sxai.cookie_consent";
  const [decision, setDecision] = useState(() => {
    try { return localStorage.getItem(STORE_KEY); } catch { return null; }
  });
  if (decision) return null;

  const EU_CODES = ["AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IE","IT","LV","LT","LU","MT","NL","PL","PT","RO","SK","SI","ES","SE"];
  const EEA_LIKE = [...EU_CODES, "GB", "IS", "LI", "NO", "CH"];
  const country = locale?.country || "";
  // We don't read CA-state from the browser locale (only country=US); assume
  // CCPA wording when country=US — overrides nothing for non-CA users since
  // the choices are equivalent in effect.
  const region = EEA_LIKE.includes(country) ? "eu"
    : country === "US" ? "us"
    : country === "IN" ? "in"
    : "default";

  // Copy is intentionally terse — the banner is a slim bottom bar, so each
  // body is one short line. Region nuance lives in the law note + actions.
  const COPY = {
    eu: {
      title: "Cookies",
      body: "Essential only — to keep you signed in. No ads or analytics.",
      primary: "Accept all",
      secondary: "Only essential",
      lawNote: "GDPR · UK DPA 2018",
    },
    us: {
      title: "Your privacy choices",
      body: "We don't sell or share your data for advertising.",
      primary: "OK",
      secondary: "Do Not Sell / Share",
      lawNote: "CCPA / CPRA",
    },
    in: {
      title: "We use cookies",
      body: "To keep you signed in and remember your preferences.",
      primary: "I agree",
      secondary: "Learn more",
      lawNote: "India DPDP Act, 2023",
    },
    default: {
      title: "Cookies",
      body: "To keep you signed in. No ads, no analytics.",
      primary: "OK",
      secondary: null,
      lawNote: null,
    },
  };
  const t = COPY[region];

  const choose = (choice) => {
    try { localStorage.setItem(STORE_KEY, `${region}:${choice}:${Date.now()}`); } catch {}
    setDecision(`${region}:${choice}`);
  };

  return html`
    <div class="cookie-notice" role="dialog" aria-live="polite" aria-labelledby="cookie-title">
      <div class="cookie-body">
        <div id="cookie-title" class="cookie-title">${t.title}</div>
        <p class="cookie-text">${t.body}</p>
        ${t.lawNote ? html`<div class="cookie-lawnote">${t.lawNote}</div>` : ""}
      </div>
      <div class="cookie-actions">
        ${t.secondary ? html`
          <button class="cookie-btn cookie-btn-secondary" type="button" onClick=${() => choose(region === "in" ? "learn_more" : region === "us" ? "do_not_sell" : "essential_only")}>
            ${t.secondary}
          </button>
        ` : ""}
        <button class="cookie-btn cookie-btn-primary" type="button" onClick=${() => choose("accept")}>
          ${t.primary}
        </button>
      </div>
    </div>
  `;
}

// LandingArt — ambient decorative layer behind the landing surface. Three
// drifting "auras" (blurred radial discs), a soft pinpoint grid that fades
// to the edges, and an aurora streak across the top. Pure CSS, z:1 (below
// `.lp` at z:4). Respects prefers-reduced-motion via the global media query.
function LandingArt() {
  return html`
    <div class="lp-art" aria-hidden="true">
      <div class="lp-art-aurora"></div>
      <div class="lp-art-grid"></div>
      <div class="lp-art-blob lp-art-blob-1"></div>
      <div class="lp-art-blob lp-art-blob-2"></div>
      <div class="lp-art-blob lp-art-blob-3"></div>
      <div class="lp-art-noise"></div>
    </div>
  `;
}

// SpiderX brand mark — inline SVG so `currentColor` inherits from the parent.
// The red X is hardcoded (#df3739); the "SpiderX.AI" wordmark uses
// `currentColor` so it auto-flips dark/light with the theme. Source asset:
// /static/assets/spiderx-logo.svg (kept in sync — but inlined here so an
// <img> tag isn't needed and theme colour propagates through).
function SpiderXLogo({ height = 28 }) {
  return html`
    <svg viewBox="0 0 873.091 160" height=${height} width="auto" preserveAspectRatio="xMinYMid meet" style=${{ display: "block", color: "inherit" }} aria-hidden="true">
      <path fill="#df3739" fill-rule="evenodd" d="M1229.69,547.638h-27.2l-20.63-29.613-20.64,29.613h-27.19s46.79-67.271,63.77-91.711c2.79-4.208,5.57-6.55,12.43-6.55h19.46l-34.23,49.13Zm-95.66-98.261h27.19l12.83,18.407-13.76,19.287Z" transform="translate(-523.469 -418)" />
      <path fill="currentColor" fill-rule="evenodd" d="M1376.85,547.638V462.487c0-7.369,3.91-13.11,12.21-13.11h7.49v98.261h-19.7Zm-38.45-17.78h-34.45c-5.83,0-7.32,1.564-8.97,5.878-1.92,5.275-4.42,11.9-4.42,11.9h-20.63s6.27-14.362,10.35-24.424c1.83-4.937,4.19-9.266,14.97-9.266h36.58L1317.95,476.3l-9.3,24.543h-19.32l20.9-51.6h15.75l39.62,98.391h-20.64ZM1249.82,548a10.147,10.147,0,1,1,10.15-10.147A10.144,10.144,0,0,1,1249.82,548ZM1129.2,495.572a15.01,15.01,0,0,0-5-.772,13.229,13.229,0,0,0-9.65,3.652q-3.735,3.654-3.73,11.378V537.5a9.706,9.706,0,0,1-9.72,9.694h-8.73V479.348h18.45v6.006c0.05-.058.09-0.119,0.14-0.177q6.06-7.233,17.46-7.234a23.182,23.182,0,0,1,8.95,1.615,19.706,19.706,0,0,1,7.11,5.268l-11.55,13.345A8.611,8.611,0,0,0,1129.2,495.572Zm-48.28,24.091-49.7.122a20.228,20.228,0,0,0,1.47,3.882,15.986,15.986,0,0,0,6.69,6.953,20.772,20.772,0,0,0,10.14,2.388,25.238,25.238,0,0,0,9.43-1.685,18.525,18.525,0,0,0,7.33-5.338l10.98,10.957a33.4,33.4,0,0,1-12.25,8.849,39.338,39.338,0,0,1-15.35,2.95,38.722,38.722,0,0,1-19.08-4.635,34.454,34.454,0,0,1-13.17-12.643,37.243,37.243,0,0,1-.07-36.382,34.405,34.405,0,0,1,12.89-12.643,36.394,36.394,0,0,1,18.3-4.636,33.944,33.944,0,0,1,17.18,4.355,30.514,30.514,0,0,1,11.83,12.081,35.964,35.964,0,0,1,4.23,17.559c0,1.218-.05,2.458-0.14,3.722A24.867,24.867,0,0,1,1080.92,519.663Zm-18.3-18.121a14.2,14.2,0,0,0-5.5-6.18,16.725,16.725,0,0,0-8.73-2.108,18.612,18.612,0,0,0-9.57,2.388,15.77,15.77,0,0,0-6.27,6.813,19.334,19.334,0,0,0-1.33,3.56l32.86-.094A19.7,19.7,0,0,0,1062.62,501.542Zm-78.252,39.74a25.052,25.052,0,0,1-6.055,4.3,29.015,29.015,0,0,1-13.378,3.02,31,31,0,0,1-16.827-4.636,33.56,33.56,0,0,1-11.758-12.642,39.506,39.506,0,0,1,0-36.1,33.57,33.57,0,0,1,11.758-12.643,31.009,31.009,0,0,1,16.827-4.635,28.689,28.689,0,0,1,13.237,3.02,26.35,26.35,0,0,1,6.056,4.231V455.548a9.926,9.926,0,0,1,9.938-9.914h8.794V547.2H984.368v-5.914Zm-1.056-37.492a16.472,16.472,0,0,0-15-8.85,17.349,17.349,0,0,0-9.083,2.318,16.108,16.108,0,0,0-6.055,6.462A20.034,20.034,0,0,0,951,513.2a20.336,20.336,0,0,0,2.182,9.622,15.955,15.955,0,0,0,6.126,6.462,18.169,18.169,0,0,0,17.884-.07,16.578,16.578,0,0,0,6.125-6.462,19.633,19.633,0,0,0,2.183-9.412A19.951,19.951,0,0,0,983.312,503.79Zm-71.652-35.4a10,10,0,0,1-7.463-3.02,10.94,10.94,0,0,1,0-14.961,10.643,10.643,0,0,1,15,0,11.138,11.138,0,0,1,0,14.961A9.946,9.946,0,0,1,911.66,468.391Zm-36.377,75.574a31.369,31.369,0,0,1-16.9,4.636,29.24,29.24,0,0,1-13.237-2.95,25.2,25.2,0,0,1-6.2-4.326v24.693a9.707,9.707,0,0,1-9.718,9.694h-8.589V479.348h18.447v6a26.6,26.6,0,0,1,6.055-4.311,28.172,28.172,0,0,1,13.237-3.09,31.731,31.731,0,0,1,16.968,4.635,32.194,32.194,0,0,1,11.759,12.643,38.239,38.239,0,0,1,4.224,18.121,37.428,37.428,0,0,1-4.295,17.981A32.867,32.867,0,0,1,875.283,543.965Zm-5.14-40.175a16.943,16.943,0,0,0-5.984-6.462,16.608,16.608,0,0,0-9.013-2.388,16.431,16.431,0,0,0-8.8,2.388,16.735,16.735,0,0,0-6.055,6.462,19.621,19.621,0,0,0-2.183,9.412,21.178,21.178,0,0,0,2.042,9.482,15.347,15.347,0,0,0,6.056,6.532,17.408,17.408,0,0,0,9.082,2.388,16.32,16.32,0,0,0,8.872-2.388,16.79,16.79,0,0,0,5.984-6.532,20.038,20.038,0,0,0,2.183-9.482A19.633,19.633,0,0,0,870.143,503.79ZM771.9,484.615a92.453,92.453,0,0,0,9.223,3.583q5.07,1.685,10,3.862a41.6,41.6,0,0,1,9.082,5.479,24.075,24.075,0,0,1,6.689,8.569A29.838,29.838,0,0,1,809.43,519.1q0,13.769-9.717,21.7t-26.755,7.936q-11.829,0-20.348-4.073a45.837,45.837,0,0,1-15.419-12.362l12.673-12.643a33.776,33.776,0,0,0,10.209,8.92,28.545,28.545,0,0,0,14.012,3.161q7.462,0,11.828-2.95a9.316,9.316,0,0,0,4.366-8.147,10.493,10.493,0,0,0-2.535-7.305,20.114,20.114,0,0,0-6.689-4.706,85.484,85.484,0,0,0-9.153-3.512q-5-1.613-9.927-3.722a42.421,42.421,0,0,1-9.153-5.338,23.118,23.118,0,0,1-6.689-8.147q-2.466-4.916-2.465-12.643a26.283,26.283,0,0,1,4.366-15.312A27.9,27.9,0,0,1,760,450.27a41.948,41.948,0,0,1,17.18-3.371,43.433,43.433,0,0,1,18.376,3.792,37.537,37.537,0,0,1,13.308,9.833l-12.674,12.643a30.727,30.727,0,0,0-8.942-7.024A23.2,23.2,0,0,0,776.9,463.9q-6.621,0-10.42,2.6a8.283,8.283,0,0,0-3.8,7.235,8.679,8.679,0,0,0,2.534,6.532A23.571,23.571,0,0,0,771.9,484.615Zm-88.477,93.373H641.293a7.9,7.9,0,0,1-7.9-7.9l0.012-41.729a6.581,6.581,0,0,0-1.927-4.653l-5.023-5.023a6.577,6.577,0,0,0-4.651-1.926l-36.2.012a6.57,6.57,0,0,0-4.647,1.926l-2.905,2.906a6.581,6.581,0,0,0-1.926,4.652l0,36.588a7.9,7.9,0,0,1-7.9,7.9H531.364a7.9,7.9,0,0,1-7.9-7.9V525.981a7.9,7.9,0,0,1,7.9-7.9l36.319,0a6.574,6.574,0,0,0,4.649-1.926l3.177-3.178a6.574,6.574,0,0,0,1.926-4.649l0.007-36.332a7.9,7.9,0,0,1,7.9-7.9l36.323-.007a6.578,6.578,0,0,0,4.648-1.926l3.836-3.837a6.581,6.581,0,0,0,1.926-4.651V425.9a7.9,7.9,0,0,1,7.9-7.9h27.655a7.9,7.9,0,0,1,7.9,7.9v27.641a7.9,7.9,0,0,1-7.9,7.9l-26.718.008a6.573,6.573,0,0,0-4.647,1.927l-4.229,4.229a6.577,6.577,0,0,0-1.926,4.65v36.2a6.582,6.582,0,0,0,1.927,4.652l5,5a6.575,6.575,0,0,0,4.648,1.925l41.741,0a7.9,7.9,0,0,1,7.9,7.9v42.143A7.9,7.9,0,0,1,683.425,577.988Zm228.739-98.64h8.79V547.2H902.225V489.262A9.926,9.926,0,0,1,912.164,479.348Z" transform="translate(-523.469 -418)" />
    </svg>
  `;
}

// Light/dark toggle. Pure visual — flips data-theme on <html> and persists.
// ─────────────────────────────────────────────────────────────────────────
// ─── Industry presets (landing composer dropdown + /for-<industry>) ───────
//
// Selecting an industry on the homepage — or arriving via a per-industry
// deep-link like /for-automobile — does two things:
//   1. Reskins the hero: the headline, sub-copy, textarea placeholder and
//      starter chips all swap to that industry's flavour.
//   2. Presets the build's industry context. The chosen industry id is
//      threaded onto the WS query string (industry=<id>) so the server
//      locks that industry's template up front and Eva skips triage,
//      opening the deterministic interview immediately.
//
// `id` matches the server's template facet (presets.SECTORS id, e.g.
// "automotive"). `slugs` are the public URL forms accepted on /for-<slug>
// (slugs[0] is the canonical one we push to the address bar). We only list
// industries we actually ship a template for, so the "supported" promise
// stays honest; anything else falls through to the generic flow.
const DEFAULT_HERO = {
  headline: { pre: "Build ", em: "Phone AI agents", post: " in a minute." },
  sub: "Describe your business — Eva turns it into an agent that answers your calls 24/7. Books appointments, qualifies leads, transfers when needed.",
  placeholder: "Tell Eva about your business — e.g. 'I run a dental clinic in Bangalore. Callers ask about timings, prices, and book appointments.'",
  // Cross-industry quick-starts. Each carries the industry it should
  // preset so clicking a chip locks the matching template too.
  starters: [
    { emoji: "🦷", label: "Dental clinic",     industry: "dental",     prompt: "I run a dental clinic — callers want to book check-ups, ask about pricing, and reschedule." },
    { emoji: "🐾", label: "Pet salon",         industry: "salon",      prompt: "Pet grooming salon — appointment bookings, service questions, and the occasional emergency." },
    { emoji: "🚗", label: "Car dealership",    industry: "automotive", prompt: "Car dealership — callers ask about models, on-road prices, and want to book test drives." },
    { emoji: "🍽️", label: "Restaurant",        industry: "restaurant", prompt: "Restaurant — table reservations, takeaway orders, and questions about today's specials." },
    { emoji: "💇", label: "Hair / nail salon", industry: "salon",      prompt: "Hair and nail salon — bookings by stylist, walk-in availability, pricing for common services." },
    { emoji: "✨", label: "Something else",    industry: null,         prompt: "" },
  ],
};

const INDUSTRY_PRESETS = {
  automotive: {
    id: "automotive", label: "Car dealership", emoji: "🚗",
    slugs: ["automobile", "automotive", "auto", "car", "car-dealership", "dealership"],
    headline: { pre: "Build a ", em: "dealership receptionist", post: " in a minute." },
    sub: "Eva turns your showroom into a 24/7 phone agent — books test drives, answers model and pricing questions, and routes service calls.",
    placeholder: "Tell Eva about your dealership — e.g. 'We sell Maruti and Hyundai in Pune. Callers ask about on-road prices and book test drives.'",
    starters: [
      { emoji: "🚗", label: "New car sales",  industry: "automotive", prompt: "New-car dealership — callers ask about models, on-road prices, and want to book test drives." },
      { emoji: "🔧", label: "Service centre", industry: "automotive", prompt: "Car service centre — service bookings, pickup-and-drop, and status of cars in for repair." },
      { emoji: "🏍️", label: "Two-wheeler",    industry: "automotive", prompt: "Two-wheeler showroom — bike and scooter enquiries, EMI questions, and test rides." },
      { emoji: "🚙", label: "Used cars",      industry: "automotive", prompt: "Used-car dealership — callers ask about available stock, prices, and exchange offers." },
    ],
  },
  dental: {
    id: "dental", label: "Dental clinic", emoji: "🦷",
    slugs: ["dental", "dentist", "dental-clinic", "dentists"],
    headline: { pre: "Build a ", em: "dental front-desk", post: " in a minute." },
    sub: "Eva answers your clinic's calls 24/7 — books check-ups, handles reschedules, and answers pricing and insurance questions.",
    placeholder: "Tell Eva about your clinic — e.g. 'A dental clinic in Bangalore. Callers book check-ups, ask about prices and timings.'",
    starters: [
      { emoji: "🦷", label: "General dentistry", industry: "dental", prompt: "General dental clinic — callers book check-ups, ask about pricing, and reschedule." },
      { emoji: "✨", label: "Cosmetic / ortho",  industry: "dental", prompt: "Cosmetic and orthodontic clinic — braces, aligners, and whitening enquiries and consultations." },
      { emoji: "🚑", label: "Emergency",         industry: "dental", prompt: "Dental clinic with emergency hours — toothache calls, after-hours triage, and urgent bookings." },
      { emoji: "🏥", label: "Multi-specialty",   industry: "dental", prompt: "Multi-chair dental practice — bookings by dentist, treatment questions, and follow-ups." },
    ],
  },
  restaurant: {
    id: "restaurant", label: "Restaurant", emoji: "🍽️",
    slugs: ["restaurant", "restaurants", "food", "dining", "cafe"],
    headline: { pre: "Build a ", em: "restaurant phone host", post: " in a minute." },
    sub: "Eva picks up every call — takes reservations and takeaway orders, and answers menu and timing questions.",
    placeholder: "Tell Eva about your restaurant — e.g. 'A family restaurant in Mumbai. Callers book tables, order takeaway, ask about today's specials.'",
    starters: [
      { emoji: "🍽️", label: "Dine-in",  industry: "restaurant", prompt: "Restaurant — table reservations, party bookings, and questions about today's specials." },
      { emoji: "🥡", label: "Takeaway", industry: "restaurant", prompt: "Takeaway and delivery kitchen — order taking, menu questions, and delivery timing." },
      { emoji: "☕", label: "Café",     industry: "restaurant", prompt: "Café — table bookings, catering enquiries, and opening-hours questions." },
      { emoji: "🎉", label: "Banquet",  industry: "restaurant", prompt: "Banquet and event venue — date availability, capacity, and catering package enquiries." },
    ],
  },
  salon: {
    id: "salon", label: "Salon / spa", emoji: "💇",
    slugs: ["salon", "spa", "beauty", "salons", "grooming"],
    headline: { pre: "Build a ", em: "salon receptionist", post: " in a minute." },
    sub: "Eva books appointments by stylist, answers service and pricing questions, and handles walk-in availability.",
    placeholder: "Tell Eva about your salon — e.g. 'A hair and nail salon in Pune. Callers book by stylist and ask about pricing.'",
    starters: [
      { emoji: "💇", label: "Hair & beauty", industry: "salon", prompt: "Hair and nail salon — bookings by stylist, walk-in availability, pricing for common services." },
      { emoji: "💆", label: "Day spa",       industry: "salon", prompt: "Day spa — massage and treatment bookings, package questions, and gift vouchers." },
      { emoji: "🐾", label: "Pet grooming",  industry: "salon", prompt: "Pet grooming salon — appointment bookings, service questions, and the occasional emergency." },
      { emoji: "💅", label: "Nail studio",   industry: "salon", prompt: "Nail studio — manicure and pedicure bookings, nail-art enquiries, and pricing." },
    ],
  },
  healthcare: {
    id: "healthcare", label: "Clinic / hospital", emoji: "🩺",
    slugs: ["healthcare", "clinic", "hospital", "medical", "health"],
    headline: { pre: "Build a ", em: "clinic front-desk", post: " in a minute." },
    sub: "Eva answers patient calls 24/7 — books appointments, shares timings and prep instructions, and routes urgent calls.",
    placeholder: "Tell Eva about your clinic — e.g. 'A multi-specialty clinic in Delhi. Patients book appointments and ask about doctors and timings.'",
    starters: [
      { emoji: "🩺", label: "GP / clinic",     industry: "healthcare", prompt: "General clinic — patients book appointments, ask about doctor timings, and reports." },
      { emoji: "🏥", label: "Multi-specialty", industry: "healthcare", prompt: "Multi-specialty clinic — appointment booking by department, doctor availability, and reports." },
      { emoji: "🔬", label: "Diagnostics",     industry: "healthcare", prompt: "Diagnostic lab — test bookings, home-collection slots, and report-ready enquiries." },
      { emoji: "👶", label: "Specialty",       industry: "healthcare", prompt: "Specialty clinic — consultation bookings, follow-ups, and procedure questions." },
    ],
  },
  real_estate: {
    id: "real_estate", label: "Real estate", emoji: "🏢",
    slugs: ["real-estate", "realestate", "realty", "property", "real_estate"],
    headline: { pre: "Build a ", em: "real-estate desk", post: " in a minute." },
    sub: "Eva qualifies enquiries, books site visits, and answers project questions — without ever over-promising on price.",
    placeholder: "Tell Eva about your agency — e.g. 'We sell 2/3 BHK flats in Whitefield. Callers ask about projects and book site visits.'",
    starters: [
      { emoji: "🏢", label: "Flats / apartments", industry: "real_estate", prompt: "Real-estate sales — 2/3 BHK flats; callers ask about projects and book site visits." },
      { emoji: "🔑", label: "Rentals",            industry: "real_estate", prompt: "Rental brokerage — callers ask about available flats, rent, and schedule viewings." },
      { emoji: "🏡", label: "Villas / plots",     industry: "real_estate", prompt: "Villas and plots — premium enquiries, location questions, and site-visit bookings." },
      { emoji: "🏗️", label: "New project",        industry: "real_estate", prompt: "New residential project — pre-launch enquiries, pricing ranges, and site visits." },
    ],
  },
  education: {
    id: "education", label: "Education / coaching", emoji: "📚",
    slugs: ["education", "coaching", "school", "institute", "edtech", "tuition"],
    headline: { pre: "Build an ", em: "admissions counsellor", post: " in a minute." },
    sub: "Eva answers course enquiries, books counselling and demo classes, and shares batch and fee details.",
    placeholder: "Tell Eva about your institute — e.g. 'A NEET coaching institute. Callers ask about batches, fees, and book demos.'",
    starters: [
      { emoji: "📚", label: "Coaching",        industry: "education", prompt: "Coaching institute — course enquiries, batch timings, fees, and demo-class bookings." },
      { emoji: "🏫", label: "School / college", industry: "education", prompt: "School admissions — enquiries about classes, fees, and the admission process." },
      { emoji: "💻", label: "EdTech",          industry: "education", prompt: "Online learning platform — course questions, trial classes, and enrolment help." },
      { emoji: "🗣️", label: "Skills training",  industry: "education", prompt: "Skill training centre — spoken English and IT courses, batches, and enrolments." },
    ],
  },
  travel: {
    id: "travel", label: "Travel / hotel", emoji: "✈️",
    slugs: ["travel", "tour", "tours", "hotel", "holiday", "tourism"],
    headline: { pre: "Build a ", em: "travel booking desk", post: " in a minute." },
    sub: "Eva handles trip enquiries, captures leads, and books holidays — always clear about what's included.",
    placeholder: "Tell Eva about your travel business — e.g. 'We sell Kerala and Goa packages. Callers ask for quotes and book trips.'",
    starters: [
      { emoji: "✈️", label: "Tour packages",  industry: "travel", prompt: "Tour operator — Kerala, Goa, and Himachal packages; callers ask for quotes and book trips." },
      { emoji: "🏨", label: "Hotel / resort", industry: "travel", prompt: "Hotel booking desk — room availability, tariffs, and reservation enquiries." },
      { emoji: "🌴", label: "Honeymoon",      industry: "travel", prompt: "Honeymoon and holiday specialist — Maldives and Dubai packages, and customised itineraries." },
      { emoji: "🧳", label: "Visa / travel",  industry: "travel", prompt: "Travel agency — flight and visa enquiries, package quotes, and booking help." },
    ],
  },
  retail: {
    id: "retail", label: "Retail / e-commerce", emoji: "🛍️",
    slugs: ["retail", "shop", "store", "ecommerce", "e-commerce"],
    headline: { pre: "Build a ", em: "store support line", post: " in a minute." },
    sub: "Eva handles order status, returns, and availability questions — for your store or online shop, 24/7.",
    placeholder: "Tell Eva about your store — e.g. 'An apparel store with online delivery. Callers ask about orders, returns, and stock.'",
    starters: [
      { emoji: "🛍️", label: "Apparel / fashion", industry: "retail", prompt: "Apparel store — callers ask about availability, sizes, returns, and order status." },
      { emoji: "📦", label: "Online store",      industry: "retail", prompt: "Online store support — order status, delivery timing, returns, and refunds." },
      { emoji: "🛒", label: "Grocery / mart",    industry: "retail", prompt: "Grocery and daily-needs store — order taking, delivery slots, and availability." },
      { emoji: "🏬", label: "Electronics",       industry: "retail", prompt: "Electronics store — product availability, pricing, warranty, and order status." },
    ],
  },
};

// Dropdown order — roughly by SMB density in our target markets.
const INDUSTRY_ORDER = [
  "automotive", "dental", "restaurant", "salon", "healthcare",
  "real_estate", "education", "travel", "retail",
];

// Resolve a /for-<slug> URL fragment to an industry id (or null).
function industryFromSlug(slug) {
  const s = String(slug || "").toLowerCase();
  if (!s) return null;
  for (const id of INDUSTRY_ORDER) {
    const p = INDUSTRY_PRESETS[id];
    if (p && (id === s || (p.slugs || []).includes(s))) return id;
  }
  return null;
}

// Canonical /for-<slug> path for an industry id (for address-bar sync).
function industryToPath(id) {
  const p = id && INDUSTRY_PRESETS[id];
  return p && p.slugs && p.slugs.length ? `/for-${p.slugs[0]}` : "/";
}

// LandingHero — prompt-first landing surface.
//
// Used to be voice-first: a big audio-reactive orb dominated the hero,
// the CTA started a Gemini Live session, mic was the only way in.
// That over-indexed on "magical demo" and under-served the actual
// pre-build user, who often wants to TYPE what they want first (no mic
// permission prompt, no live-context fear, easier to draft).
//
// New shape: a large textarea is the primary surface. Operator types
// what they want to build, hits Submit, and we open a session seeded
// with that text as the first user turn. The orb stays available as
// a small "talk instead" toggle for the voice-first path.
//
// Carries:
//  • locale popover (click to switch country/language → persisted)
//  • outcome-led headline + subtitle
//  • PROMPT TEXTAREA + Submit (primary path)
//  • "Or talk to Eva" affordance (secondary, opens mic flow)
//  • For returning users: "Open your agents" remains as a tertiary nav
//  • Microcopy below (free-tier + no-card-required trust signals)
// ─────────────────────────────────────────────────────────────────────────
function LandingHero({ agents, locale, onBuild, onOpenAgents, blobSize, blobMode, engineRef, onPressStart, onPressEnd, onPressCancel, initialIndustry, onIndustryChange }) {
  const isReturning = (agents || []).length > 0;
  const [showLocale, setShowLocale] = useState(false);
  // Build 212 — ref the picker wrap so the outside-click handler can
  // tell "inside" from "outside". Previously a click on a button BEHIND
  // the picker would close it AND trigger that button's onClick — the
  // user observed "click outside the picker → page navigates to
  // /agent/rohan/profile". Using mousedown + a contains() check stops
  // the close handler from leaking the click downstream.
  const localePickerRef = useRef(null);
  const [prompt, setPrompt] = useState("");
  // Selected industry preset (null = "Any industry"). Seeded from the
  // route (/for-<slug> resolves to initialIndustry) and kept in sync if
  // the route changes under us (browser back/forward).
  const [industry, setIndustry] = useState(initialIndustry || null);
  useEffect(() => { setIndustry(initialIndustry || null); }, [initialIndustry]);

  // The active hero config: a specific industry's flavour, or the
  // cross-industry default. Drives headline, sub-copy, placeholder and
  // the starter chips below.
  const hero = (industry && INDUSTRY_PRESETS[industry]) || DEFAULT_HERO;
  const STARTER_PROMPTS = hero.starters;

  // Changing the dropdown reskins the hero AND mirrors the choice into
  // the address bar (/for-<slug>) so it behaves like a real per-industry
  // landing page — shareable, bookmarkable, back-button-friendly. The
  // parent owns the canonical landingIndustry state for popstate sync.
  const onSelectIndustry = (id) => {
    const next = id || null;
    setIndustry(next);
    onIndustryChange && onIndustryChange(next);
  };

  // Submit the typed prompt: opens a session (threading the preset
  // industry so the server locks that template up front), then the
  // WS-ready handler sends the text as the first user turn. Empty input
  // with no industry falls through to the voice path so the button still
  // works; empty input WITH an industry still opens chat so Eva can lead
  // the interview from the first template question.
  const submitPrompt = (e) => {
    e?.preventDefault();
    const text = prompt.trim();
    if (text) onBuild({ initialText: text, startMuted: true, industry });
    else if (industry) onBuild({ initialText: "", startMuted: true, industry });
    else onBuild({});
  };
  const switchToVoice = () => onBuild({ industry, voice: true });  // mic-first, industry preset honoured

  const handleStarter = (item) => {
    // Each chip carries the industry it should preset. A chip's industry
    // wins over the dropdown selection (clicking "Pet grooming" under the
    // salon preset stays salon; clicking it from "Any" locks salon).
    const ind = item.industry || industry || null;
    if (!item.prompt) {
      // "Something else" → just focus the textarea, don't submit.
      try { document.querySelector(".lp-composer-input")?.focus(); } catch {}
      return;
    }
    setPrompt(item.prompt);
    // Tiny delay so React commits the textarea value before the submit
    // path reads its own state (defensive — onBuild reads `prompt` via
    // closure, so this is more about UI continuity than correctness).
    setTimeout(() => onBuild({ initialText: item.prompt, startMuted: true, industry: ind }), 0);
  };
  useEffect(() => {
    if (!showLocale) return;
    // mousedown fires BEFORE click, so we can swallow the event before
    // it reaches whatever button is sitting under the popover. Only
    // trigger close if the click landed OUTSIDE the picker wrap —
    // clicks INSIDE (menu items, the toggle button itself) are handled
    // by their own onClick handlers.
    const onDown = (e) => {
      const wrap = localePickerRef.current;
      if (wrap && wrap.contains(e.target)) return;
      e.preventDefault();
      e.stopPropagation();
      setShowLocale(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setShowLocale(false); };
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [showLocale]);

  // The list a user can actually pick from. Keep tight (≤8) so the popover
  // doesn't become a scroll-fest. Order roughly by audience.
  const PICKABLE = [
    { country: "IN", label: "India",          flag: "🇮🇳", lang: "en", langLabel: "English" },
    { country: "IN", label: "India",          flag: "🇮🇳", lang: "hi", langLabel: "Hindi" },
    { country: "US", label: "United States",  flag: "🇺🇸", lang: "en", langLabel: "English" },
    { country: "GB", label: "United Kingdom", flag: "🇬🇧", lang: "en", langLabel: "English" },
    { country: "SG", label: "Singapore",      flag: "🇸🇬", lang: "en", langLabel: "English" },
    { country: "AE", label: "UAE",            flag: "🇦🇪", lang: "en", langLabel: "English" },
    { country: "AU", label: "Australia",      flag: "🇦🇺", lang: "en", langLabel: "English" },
  ];
  const pickLocale = (item) => {
    try {
      localStorage.setItem("sxai.locale", JSON.stringify({
        country: item.country,
        countryLabel: item.label,
        countryFlag: item.flag,
        language: item.lang,
        languageLabel: item.langLabel,
        bcp47: `${item.lang}-${item.country}`,
      }));
    } catch {}
    window.location.reload();
  };

  return html`
    <section class="lp" aria-labelledby="lp-headline">
      <div class="lp-locale-wrap" ref=${localePickerRef}>
        <button class="lp-locale" type="button" aria-haspopup="listbox" aria-expanded=${showLocale}
                onClick=${(e) => { e.stopPropagation(); setShowLocale((s) => !s); }}>
          <span class="lp-locale-flag" aria-hidden="true">${locale.countryFlag}</span>
          <span class="lp-locale-text">${locale.countryLabel} · ${locale.languageLabel}</span>
          <svg class="lp-locale-chev" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        ${showLocale ? html`
          <ul class="lp-locale-menu" role="listbox" onClick=${(e) => e.stopPropagation()}>
            ${PICKABLE.map((it) => {
              const active = locale.country === it.country && locale.language === it.lang;
              return html`
                <li role="option" key=${`${it.country}-${it.lang}`} aria-selected=${active}>
                  <button class=${"lp-locale-item" + (active ? " active" : "")} type="button" onClick=${() => pickLocale(it)}>
                    <span aria-hidden="true">${it.flag}</span>
                    <span class="lp-locale-item-label">${it.label}</span>
                    <span class="lp-locale-item-lang">${it.langLabel}</span>
                    ${active ? html`<span class="lp-locale-item-tick" aria-hidden="true">✓</span>` : ""}
                  </button>
                </li>
              `;
            })}
          </ul>
        ` : ""}
      </div>

      <div class="lp-hero">
        <h1 class="lp-title" id="lp-headline">${hero.headline.pre}<em>${hero.headline.em}</em>${hero.headline.post}</h1>
        <p class="lp-sub">
          ${hero.sub} ${locale.languageLabel}-speaking by default.
        </p>
      </div>

      <!-- Industry preset selector. Picking an industry reskins the hero
           + presets the build's industry context (Eva locks that
           template up front and skips triage). Mirrors to /for-<slug>
           so each industry has its own shareable landing URL. -->
      <div class="lp-industry" role="group" aria-label="Choose your industry">
        <label class="lp-industry-label" for="lp-industry-select">I'm building for</label>
        <div class="lp-industry-field">
          <span class="lp-industry-emoji" aria-hidden="true">${(industry && INDUSTRY_PRESETS[industry]) ? INDUSTRY_PRESETS[industry].emoji : "🏷️"}</span>
          <select id="lp-industry-select" class="lp-industry-select"
                  value=${industry || ""}
                  onChange=${(e) => onSelectIndustry(e.target.value)}>
            <option value="">Any industry</option>
            ${INDUSTRY_ORDER.map((id) => html`
              <option key=${id} value=${id}>${INDUSTRY_PRESETS[id].label}</option>
            `)}
          </select>
          <svg class="lp-industry-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
        </div>
      </div>

      <!-- Prompt-first composer. Operator types what they want to build;
           Submit opens the session and threads the typed text in as turn 1.
           A small mic toggle lives inside the composer for one-tap entry
           into voice mode without leaving the home page. -->
      <form class="lp-composer" onSubmit=${submitPrompt}>
        <textarea
          class="lp-composer-input"
          placeholder=${isReturning && !industry
            ? "Build another — e.g. 'A salon receptionist in Pune that books appointments and answers pricing.'"
            : hero.placeholder}
          value=${prompt}
          onInput=${(e) => setPrompt(e.target.value)}
          onKeyDown=${(e) => {
            // Cmd/Ctrl+Enter submits; plain Enter keeps the newline so
            // operators can paste multi-line descriptions naturally.
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitPrompt(e);
          }}
          rows="4"
          autoFocus
          spellCheck="true"
          aria-label="Describe your business"
        ></textarea>
        <div class="lp-composer-actions">
          <button class="lp-composer-mic" type="button" onClick=${switchToVoice}
                  aria-label="Talk to Eva instead"
                  title="Talk to Eva instead (Cmd+Shift+M)">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
              <rect x="9" y="3" width="6" height="12" rx="3"/>
              <path d="M5 11a7 7 0 0 0 14 0"/>
              <path d="M12 18v3"/>
            </svg>
            <span class="lp-composer-mic-label">Talk instead</span>
          </button>
          <button class="lp-composer-submit" type="submit" disabled=${!prompt.trim() && !industry}>
            <span>${industry ? `Build my ${INDUSTRY_PRESETS[industry].label.toLowerCase()} agent` : (isReturning ? "Build with Eva" : "Build my agent")}</span>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M5 12h14M13 6l6 6-6 6"/>
            </svg>
          </button>
        </div>
      </form>

      <!-- Starter prompts — quick-start chips for users who need an
           example to anchor on. Click → opens the chat surface with
           that prompt pre-filled as turn 1. The chip's emoji adds a
           hint of personality without being noisy. -->
      <div class="lp-starters" role="group" aria-label="Quick-start prompts">
        <div class="lp-starters-label">${industry ? `Common ${INDUSTRY_PRESETS[industry].label.toLowerCase()} setups:` : "Or get started with:"}</div>
        <div class="lp-starters-chips">
          ${STARTER_PROMPTS.map((item) => html`
            <button class="lp-starter-chip" type="button" key=${item.label}
                    onClick=${() => handleStarter(item)}
                    title=${item.prompt || "Type your own"}>
              <span class="lp-starter-emoji" aria-hidden="true">${item.emoji}</span>
              <span>${item.label}</span>
            </button>
          `)}
        </div>
      </div>

      <!-- Returning-user shortcut: keep "Open your agents" reachable
           below the composer. Voice mode is folded INTO the composer
           via the mic icon, so the old "Or build another with Eva →"
           secondary link is no longer needed. -->
      ${isReturning ? html`
        <div class="lp-altrow">
          <button class="lp-alt" type="button" onClick=${onOpenAgents}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.4"/><path d="M3 19c0-3 3-5 6-5s6 2 6 5"/><path d="M14 16c.7-1 2-1.6 3.5-1.6 2 0 3.5 1.2 3.5 3"/></svg>
            <span>Open your agents</span>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          </button>
        </div>
      ` : ""}

      <div class="lp-trustrow" aria-label="Free tier and call expectations">
        <span class="lp-trust"><span class="lp-trust-dot" aria-hidden="true"></span>300 free minutes</span>
        <span class="lp-trust-sep" aria-hidden="true">·</span>
        <span class="lp-trust">No card required</span>
        <span class="lp-trust-sep" aria-hidden="true">·</span>
        <span class="lp-trust">Type or talk — your call</span>
      </div>
    </section>
  `;
}

// ─── LandingChatView ─────────────────────────────────────────────────────
//
// Text-first build surface. Replaces the voice-only call view when the
// operator initiates a build by typing on the landing page. Looks and
// feels like a modern AI chat product (Claude / emergent.sh / ChatGPT):
// streaming Eva bubbles on the left, operator bubbles on the right,
// composer pinned at the bottom, a "Talk instead" affordance for
// flipping to voice mid-conversation.
//
// Why a dedicated view (vs reusing the call view's CaptionRail/ChatPanel):
//   1. The call view is built around a giant audio-reactive orb. For a
//      text conversation that orb is wasted real-estate and the chat
//      messages got buried in CaptionRail's 2-line ticker.
//   2. We skip the AudioEngine entirely — no mic permission prompt, no
//      output buffering, no autoplay-policy battles. The WS still
//      receives audio chunks (we discard the binary frames); transcript
//      JSON arrives in parallel and drives the bubble stream.
//   3. The chat view shares the SAME `sid` as any subsequent voice
//      session (sessionStorage.eva_build_sid). Server-side state —
//      sector, business_name, template_id, template_answers — is
//      persistent across WS opens, so the operator can flip mid-build
//      with zero context loss.
//
// Lifecycle:
//   - Mounts → opens WS to /ws/session with the sid + query params
//   - Sends `initialText` (from landing composer) as the first user turn
//     once the server's `ready` event arrives
//   - On every server `transcript` event with role=model, appends/streams
//     into the current Eva bubble
//   - On `turn_complete`, finalizes the streaming bubble + clears the
//     "Eva is thinking…" indicator
//   - On `agent_saved`, calls onAgentSaved (parent triggers the reveal)
//   - On `build_complete`, calls onClose (parent returns to landing)
//
// tidyText — insert a space after sentence-ending punctuation when it's
// glued directly to the next capitalized word (e.g. "Got it.Okay" →
// "Got it. Okay"). Eva's terse ack+transition turns arrive as adjacent
// stream fragments with no separating space; this normalizes them at
// display time. Conservative: only fires on ".!?" immediately followed
// by an uppercase A-Z, so decimals ("3.5") and lowercase-joined tokens
// are left alone.
function tidyText(s) {
  return String(s || "").replace(/([.!?])([A-Z])/g, "$1 $2");
}

// QuestionCard — structured "next question" surface, driven by the
// server's `template_question` event. Rendered in BOTH the chat view
// (LandingChatView) and the voice call view, so flipping between
// modes preserves the structured Q&A scaffolding.
//
// Props:
//   question — the question dict from the WS event
//             { id, prompt, type, required, hint, options,
//               suggestions, primary_suggestion,
//               progress: { answered, total, number } }
//   error    — { question_id, error, retry_prompt } | null
//             attached when the last answer failed validation; renders
//             as a soft red banner inside the card body.
//   onAnswer — (text) => void; called when the operator clicks a chip.
//             The host component decides what to do (chat view sends
//             via WS as a text message; call view sends the same).
//   onSkip   — (questionId) => void; called when the operator clicks
//             the Skip chip (only shown on required:false questions).
//             Sends a `template_skip` event to the server.
//   compact  — bool; render in a tighter footprint (call-view overlay).
//   showWaiting — bool; show the "Agent is waiting…" status strip.
function QuestionCard({ question, error, onAnswer, onSkip, compact, showWaiting }) {
  // CRITICAL: hooks MUST be called in the same order on every render.
  // The early `if (!question) return null` used to live above these
  // useState/useEffect calls — which silently violated the Hooks Rules
  // (React errors out with #62 "Cannot read properties of null" on
  // re-render with question set after previously being null). Hooks
  // first, early-return after.
  const [locked, setLocked] = useState(false);
  useEffect(() => { setLocked(false); }, [question?.id]);
  if (!question) return null;
  // Primary suggestion comes first (matches the "PROPOSE NAME" line in
  // the BUILD STATE block); alternates follow. Always end with a Skip
  // chip on optional questions so there's a deterministic way out.
  const primary = question.primary_suggestion;
  // Local "an answer is in flight" lock. Without this, an operator
  // clicking three chips in quick succession (e.g. "pet grooming" →
  // "nail" → "hair" while Eva is still streaming) fires THREE
  // separate user messages — the model gets confused, calls
  // select_build_template repeatedly, and the card never advances.
  // First click flips this true → chip clicks become no-ops + visual
  // disabled state. Reset whenever a new question lands (the
  // `question.id` change is the natural trigger via useState init).
  const guarded = (text) => {
    if (locked) return;
    setLocked(true);
    if (onAnswer) onAnswer(text);
  };
  const guardedSkip = (qid) => {
    if (locked) return;
    setLocked(true);
    if (onSkip) onSkip(qid);
  };
  // Humanize a raw enum value for display: "dine_in_only" → "Dine in
  // only", "takeaway_only" → "Takeaway only", "both" → "Both". We show
  // the friendly label but SEND the raw value on click (enum validation
  // matches against the template's literal options).
  const humanizeOption = (s) => {
    const v = String(s || "").replace(/_/g, " ").trim();
    return v.charAt(0).toUpperCase() + v.slice(1);
  };
  const renderChips = () => {
    const chipClass = "lp-chat-qcard-chip" + (locked ? " lp-chat-qcard-chip-locked" : "");
    if (question.type === "enum" && question.options && question.options.length) {
      return question.options.map((opt) => html`
        <button type="button" class=${chipClass} key=${opt}
                disabled=${locked}
                onClick=${() => guarded(opt)}>
          ${humanizeOption(opt)}
        </button>
      `);
    }
    if (question.suggestions && question.suggestions.length) {
      const alts = question.suggestions.filter((s) => s !== primary);
      const ordered = primary ? [primary, ...alts] : question.suggestions;
      return ordered.map((s, i) => html`
        <button type="button"
                class=${chipClass + (i === 0 && primary ? " lp-chat-qcard-chip-primary" : "")}
                key=${s}
                disabled=${locked}
                onClick=${() => guarded(s)}>
          ${s}
        </button>
      `);
    }
    if (question.type === "bool") {
      return html`
        <button type="button" class=${chipClass} disabled=${locked} onClick=${() => guarded("Yes")}>Yes</button>
        <button type="button" class=${chipClass} disabled=${locked} onClick=${() => guarded("No")}>No</button>
      `;
    }
    return null;
  };
  const chipNodes = renderChips();
  return html`
    <div class=${"lp-chat-qcard" + (compact ? " lp-chat-qcard-compact" : "")}
         role="region" aria-label="Pending question">
      <header class="lp-chat-qcard-banner">
        <span class="lp-chat-qcard-banner-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="9"/><path d="M9.6 9a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 1-1 1.7v.5"/><path d="M12 17h.01"/></svg>
        </span>
        <span>Eva is asking a question — answer to continue:</span>
      </header>
      <div class="lp-chat-qcard-body">
        ${question.progress && question.progress.total ? html`
          <div class="lp-chat-qcard-progress">
            Question ${question.progress.number} of ${question.progress.total}
          </div>
        ` : ""}
        <div class="lp-chat-qcard-prompt">${question.prompt}</div>
        ${question.hint ? html`
          <div class="lp-chat-qcard-hint">${question.hint}</div>
        ` : ""}
        ${error && error.question_id === question.id ? html`
          <div class="lp-chat-qcard-error" role="alert">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
            <span>${error.error || "That didn't quite work — try again."}</span>
          </div>
        ` : ""}
        ${chipNodes ? html`
          <div class="lp-chat-qcard-chips">${chipNodes}</div>
        ` : ""}
        ${!question.required && onSkip ? html`
          <div class="lp-chat-qcard-skiprow">
            <button type="button" class="lp-chat-qcard-skip"
                    disabled=${locked}
                    onClick=${() => guardedSkip(question.id)}>
              Skip this — it's optional →
            </button>
          </div>
        ` : ""}
      </div>
      ${showWaiting ? html`
        <div class="lp-chat-qcard-waiting">
          <span class="lp-chat-waiting-dot" aria-hidden="true"></span>
          <span>${locked ? "Sending your answer…" : "Agent is waiting…"}</span>
        </div>
      ` : ""}
    </div>
  `;
}

// Switch-to-voice: closes this WS, lets the parent open the voice flow
// via the existing openSession path. Same sid → server resumes from the
// build_session row. Gemini Live restarts fresh, but BuildMonitor's
// state-block injects all captured facts into the new system prompt.
function LandingChatView({ initialText, industry, onClose, onSwitchToVoice, onAgentSaved, refreshAgents }) {
  // messages[i] = { role: "user" | "eva", text, ts, streaming? }
  const [messages, setMessages] = useState(() =>
    initialText && initialText.trim()
      ? [{ role: "user", text: initialText.trim(), ts: Date.now() }]
      : []
  );
  const [draft, setDraft] = useState("");
  const [connState, setConnState] = useState("connecting"); // connecting | ready | ended | error
  const [thinking, setThinking] = useState(true);
  // The structured question card. Populated from server `template_question`
  // events, cleared on `template_complete` or when the operator answers
  // (so the card doesn't linger while we wait for the next one). Shape:
  //   { id, prompt, type, required, hint, options, suggestions,
  //     primary_suggestion, progress: {answered, total, number} }
  const [pendingQuestion, setPendingQuestion] = useState(null);
  // Set when the server rejects the last answer (`template_question_error`).
  // Shape: { question_id, error, retry_prompt }. Cleared as soon as the
  // operator submits a new answer or a fresh template_question arrives.
  const [questionError, setQuestionError] = useState(null);
  // Confetti overlay flips true the moment agent_saved arrives. Stays
  // mounted for ~900ms before the chat view unmounts (parent's reveal
  // takes over). Pure CSS — no library, no canvas.
  const [celebrate, setCelebrate] = useState(false);
  const wsRef = useRef(null);
  const evaSegRef = useRef("");     // accumulating model transcript for the current turn
  const userSegRef = useRef("");    // accumulating user transcript (only matters if voice ever flips on)
  const initialSentRef = useRef(false);
  const threadEndRef = useRef(null);
  const composerRef = useRef(null);

  // Auto-scroll to bottom whenever a message lands or streams.
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, thinking]);

  // WS lifecycle. One-shot per mount.
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const qsObj = {
      locale: navigator.language || "en-US",
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      // mode=text flips the server-side Gemini Live session to TEXT
      // response modality. Without this, Eva still SYNTHESISES audio
      // (we just discard it on the client) — wasted Gemini quota +
      // higher latency, and downstream sinks (e.g. a future stricter
      // CSP) might still leak the audio output. With mode=text, the
      // model emits text-only `part.text` payloads — the server's
      // pump routes them to the same `transcript role:model` event
      // our streaming bubble already consumes.
      mode: "text",
    };
    const _uid = currentUserId();
    if (_uid) qsObj.user_id = String(_uid);
    // Industry preset from the landing page — server locks that
    // industry's template up front so Eva skips triage.
    if (industry) qsObj.industry = String(industry);
    // Share the build sid so a later voice flip reuses the same row.
    let buildSid;
    try { buildSid = sessionStorage.getItem("eva_build_sid") || null; } catch { buildSid = null; }
    if (!buildSid) {
      buildSid = (crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : "fb-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      try { sessionStorage.setItem("eva_build_sid", buildSid); } catch {}
    }
    qsObj.sid = buildSid;
    const qs = new URLSearchParams(qsObj).toString();
    const ws = new WebSocket(`${proto}//${location.host}/ws/session?${qs}`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onerror = (e) => {
      console.warn("[LandingChatView] WS error:", e);
      setConnState("error");
    };
    ws.onclose = () => {
      // Don't flap state if we already transitioned to "ended" — parent's
      // close/reveal path is in flight.
      setConnState((prev) => (prev === "ended" ? prev : "ended"));
    };
    ws.onmessage = (ev) => {
      // Binary frames are PCM chunks from Gemini — we ignore them in
      // text-only mode. No audio engine to feed them to anyway.
      if (typeof ev.data !== "string") return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ready") {
        setConnState("ready");
        // Send the operator's typed prompt as turn 1 the moment the
        // server is ready to forward it to Gemini.
        if (initialText && initialText.trim() && !initialSentRef.current) {
          initialSentRef.current = true;
          try { ws.send(JSON.stringify({ type: "text", text: initialText.trim() })); }
          catch (e) { console.warn("[LandingChatView] initial send failed:", e); }
        }
      } else if (msg.type === "transcript") {
        if (msg.role === "model") {
          evaSegRef.current += (msg.text || "");
          setThinking(false);
          // Eva often emits a terse ack + transition as two sentences
          // with no space between them ("Got it.Okay, next:"). The
          // model produces them as adjacent stream fragments. tidyText
          // inserts a space after sentence-ending punctuation when it's
          // immediately glued to the next capitalized word. Re-run on
          // the full accumulated text each chunk so it self-corrects as
          // more arrives.
          const shown = tidyText(evaSegRef.current);
          // Stream into the last "eva" message if it's still streaming;
          // otherwise push a new streaming bubble.
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === "eva" && last.streaming) {
              const next = prev.slice();
              next[next.length - 1] = { ...last, text: shown };
              return next;
            }
            return [...prev, { role: "eva", text: shown, ts: Date.now(), streaming: true }];
          });
        } else if (msg.role === "user") {
          // User transcript from a (potential future) voice turn. We
          // could render it as a finalized user bubble — but the
          // text-first flow already captured it in submit() below, so
          // we skip to avoid duplicates.
          userSegRef.current += (msg.text || "");
        }
      } else if (msg.type === "turn_complete") {
        // Finalize the streaming eva bubble.
        if (evaSegRef.current) {
          setMessages((prev) => {
            const next = prev.slice();
            const last = next[next.length - 1];
            if (last && last.role === "eva" && last.streaming) {
              next[next.length - 1] = { ...last, streaming: false };
            }
            return next;
          });
        }
        evaSegRef.current = "";
        userSegRef.current = "";
      } else if (msg.type === "template_question") {
        // Structured question card — drives the chip/progress UI
        // above the composer. We REPLACE any existing card; idempotent
        // for repeated emissions of the same question id. Clear any
        // stale validation error from the previous question.
        if (msg.question) {
          setPendingQuestion(msg.question);
          setQuestionError(null);
          setThinking(false);
        }
      } else if (msg.type === "template_question_error") {
        // Last answer didn't validate. Attach the error to the
        // current card so the operator sees "didn't work, try again"
        // rather than wondering if their click registered.
        setQuestionError({
          question_id: msg.question_id,
          error: msg.error,
          retry_prompt: msg.retry_prompt,
        });
        setThinking(false);
      } else if (msg.type === "template_complete") {
        // Interview done. Clear the card so Eva's wrap-up beat owns
        // the screen, then she'll fire save_agent → agent_saved.
        setPendingQuestion(null);
        setQuestionError(null);
      } else if (msg.type === "agent_saved") {
        // Burst confetti, hold the chat surface for a beat, then hand
        // off to the parent's reveal path. The 900ms delay lets the
        // confetti finish its first wave before the unveal takes over
        // the screen — feels like a tiny "she's born!" moment.
        console.info("[LandingChatView] agent_saved received", msg.agent?.id, msg.agent?.name);
        setPendingQuestion(null);
        setQuestionError(null);
        setCelebrate(true);
        setTimeout(() => {
          console.info("[LandingChatView] firing onAgentSaved after 900ms hold");
          if (onAgentSaved) onAgentSaved(msg.agent);
          refreshAgents && refreshAgents();
        }, 900);
      } else if (msg.type === "build_complete") {
        // Eva finished the dashboard primer; server is done. Close
        // ourselves so the parent's revealAgent state takes over.
        try { ws.close(); } catch {}
      } else if (msg.type === "error") {
        setMessages((prev) => [...prev, { role: "system", text: msg.message || "Something went wrong.", ts: Date.now() }]);
        setConnState("error");
      }
    };

    return () => {
      try { ws.close(); } catch {}
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Submit a typed message from the operator.
  const submit = (e) => {
    e?.preventDefault();
    const text = draft.trim();
    if (!text) return;
    sendAnswer(text);
  };

  // Shared answer-send path used by both typed Submit and chip clicks.
  // Pushes the bubble locally, sends over the WS as the standard text
  // input the server already understands, and clears the pending card
  // so the operator doesn't see stale chips while waiting for Eva.
  const sendAnswer = (text) => {
    const trimmed = String(text || "").trim();
    if (!trimmed) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setMessages((prev) => [...prev, { role: "user", text: trimmed, ts: Date.now() }]);
    setDraft("");
    setPendingQuestion(null);
    setQuestionError(null);
    setThinking(true);
    try { ws.send(JSON.stringify({ type: "text", text: trimmed })); }
    catch (err) { console.warn("[LandingChatView] send failed:", err); }
    setTimeout(() => { composerRef.current?.focus(); }, 0);
  };

  // Skip an optional question. Sends a server-direct event (no LLM
  // round-trip) so the chip-click is deterministic. Server records
  // null for the question, emits the next card, AND notifies Eva via
  // a system notice so she doesn't re-ask.
  const sendSkip = (questionId) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setMessages((prev) => [...prev, {
      role: "user", text: "(skipped)", ts: Date.now(), muted: true,
    }]);
    setPendingQuestion(null);
    setQuestionError(null);
    setThinking(true);
    try { ws.send(JSON.stringify({ type: "template_skip", question_id: questionId })); }
    catch (err) { console.warn("[LandingChatView] skip send failed:", err); }
  };

  // Switch to voice mode. Closes our WS so the audio session opens
  // cleanly; parent calls openSession() which reuses the same sid.
  const flipToVoice = () => {
    try { wsRef.current?.close(); } catch {}
    wsRef.current = null;
    if (onSwitchToVoice) onSwitchToVoice();
  };

  const composerDisabled = (connState !== "ready");
  // Build-progress derived from the latest template_question event.
  // Once an interview has at least one question, we know the total
  // and can render a thin progress bar + ETA in the header. Estimate
  // ~10s per remaining question (median operator pace from prod).
  const totalQ     = pendingQuestion?.progress?.total ?? 0;
  const answeredQ  = pendingQuestion?.progress?.answered ?? 0;
  const remainingQ = Math.max(0, totalQ - answeredQ);
  const pct        = totalQ > 0 ? Math.round((answeredQ / totalQ) * 100) : 0;
  const etaSeconds = remainingQ * 10;
  const etaLabel   = totalQ === 0 ? null
                   : remainingQ === 0 ? "Almost done"
                   : etaSeconds < 60 ? `~${etaSeconds}s to go`
                   : `~${Math.ceil(etaSeconds / 60)} min to go`;
  // 32 confetti sprites — fixed seed so each piece animates differently
  // without us tracking React keys. `style` MUST be an object (not a
  // CSS string) — React 19 throws "expects a mapping from style
  // properties to values" (minified as error #62) otherwise.
  const CONFETTI_COUNT = 32;
  const confettiSprites = celebrate
    ? Array.from({length: CONFETTI_COUNT}, (_, i) => {
        const left   = Math.round((i / CONFETTI_COUNT) * 100 + (i * 37) % 7);
        const delay  = ((i * 53) % 30) * 10;       // 0–300ms stagger
        const dur    = 900 + ((i * 71) % 400);     // 900–1300ms
        const hueIdx = i % 5;
        return html`
          <span class=${"lp-confetti-piece lp-confetti-c" + hueIdx}
                key=${i}
                style=${{ left: `${left}%`, animationDelay: `${delay}ms`, animationDuration: `${dur}ms` }}>
          </span>
        `;
      })
    : null;
  return html`
    <section class="lp-chat" aria-label="Build with Eva">
      ${celebrate ? html`
        <div class="lp-confetti" aria-hidden="true">${confettiSprites}</div>
      ` : ""}
      <header class="lp-chat-head">
        <div class="lp-chat-head-left">
          <span class="lp-chat-dot lp-chat-dot-${connState}" aria-hidden="true"></span>
          <span class="lp-chat-title">Building with Eva</span>
          <span class="lp-chat-state">${
            connState === "connecting" ? "Connecting…" :
            connState === "ready"      ? (totalQ > 0 ? `${answeredQ} of ${totalQ}` : "Online") :
            connState === "error"      ? "Connection issue" :
                                         "Session ended"
          }</span>
          ${etaLabel ? html`
            <span class="lp-chat-eta" aria-label="Estimated time remaining">· ${etaLabel}</span>
          ` : ""}
        </div>
        <div class="lp-chat-head-right">
          <button class="lp-chat-mic" type="button" onClick=${flipToVoice}
                  aria-label="Switch to voice mode"
                  title="Switch to voice — Eva will speak and you can talk back">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true">
              <rect x="9" y="3" width="6" height="12" rx="3"/>
              <path d="M5 11a7 7 0 0 0 14 0"/>
              <path d="M12 18v3"/>
            </svg>
            <span>Talk instead</span>
          </button>
          <button class="lp-chat-close" type="button" onClick=${onClose}
                  aria-label="Close and return to home">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18"/>
            </svg>
          </button>
        </div>
      </header>

      <!-- Build progress bar — appears the moment a template is locked
           (totalQ > 0). Width animates from 0 → 100% across the interview;
           reaches 100% on template_complete, then the reveal takes over. -->
      ${totalQ > 0 ? html`
        <div class="lp-chat-progress" role="progressbar"
             aria-valuemin="0" aria-valuemax=${String(totalQ)} aria-valuenow=${String(answeredQ)}>
          <div class="lp-chat-progress-fill" style=${{ width: `${pct}%` }}></div>
        </div>
      ` : ""}

      <div class="lp-chat-thread" role="log" aria-live="polite">
        ${messages.map((m, i) => html`
          <div class=${"lp-chat-msg lp-chat-msg-" + m.role} key=${i + "-" + m.ts}>
            ${m.role === "eva" ? html`
              <div class="lp-chat-avatar" aria-hidden="true">E</div>
            ` : ""}
            <div class="lp-chat-bubble">
              ${m.text}
              ${m.streaming ? html`<span class="lp-chat-caret" aria-hidden="true">▍</span>` : ""}
            </div>
          </div>
        `)}
        ${thinking ? html`
          <div class="lp-chat-msg lp-chat-msg-eva lp-chat-msg-typing" key="typing">
            <div class="lp-chat-avatar" aria-hidden="true">E</div>
            <div class="lp-chat-bubble lp-chat-typing">
              <span class="lp-chat-typing-dot"></span>
              <span class="lp-chat-typing-dot"></span>
              <span class="lp-chat-typing-dot"></span>
            </div>
          </div>
        ` : ""}
        <div ref=${threadEndRef}></div>
      </div>

      ${pendingQuestion ? html`
        <${QuestionCard}
          question=${pendingQuestion}
          error=${questionError}
          onAnswer=${sendAnswer}
          onSkip=${sendSkip}
          showWaiting=${true}
        />
      ` : ""}

      <form class="lp-chat-composer" onSubmit=${submit}>
        <textarea
          ref=${composerRef}
          class="lp-chat-composer-input"
          placeholder=${composerDisabled
            ? "Connecting…"
            : (pendingQuestion ? "Type your answer or tap an option above…" : "Message Eva")}
          value=${draft}
          onInput=${(e) => setDraft(e.target.value)}
          onKeyDown=${(e) => {
            if (e.key === "Enter" && !e.shiftKey) submit(e);
          }}
          rows="2"
          autoFocus
          spellCheck="true"
          disabled=${composerDisabled}
        ></textarea>
        <button class="lp-chat-composer-submit" type="submit"
                disabled=${composerDisabled || !draft.trim()}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h14M13 6l6 6-6 6"/>
          </svg>
        </button>
      </form>
    </section>
  `;
}

function ThemeToggle({ theme, onToggle }) {
  const isLight = theme === "light";
  return html`
    <button class="theme-toggle" type="button" aria-label=${isLight ? "Switch to dark" : "Switch to light"}
            title=${isLight ? "Switch to dark" : "Switch to light"} onClick=${onToggle}>
      ${isLight ? html`
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
      ` : html`
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.2 4.2l1.5 1.5M18.3 18.3l1.5 1.5M2 12h2M20 12h2M4.2 19.8l1.5-1.5M18.3 5.7l1.5-1.5"/></svg>
      `}
    </button>
  `;
}

// Compact account chip in the top bar. Click → little dropdown with email +
// Sign out. Replaces the old "D" stub.
function UserMenu({ user, onSignOut, onNav }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    setTimeout(() => window.addEventListener("click", close, { once: true }), 0);
    return () => window.removeEventListener("click", close);
  }, [open]);
  const initials = userInitials(user);
  return html`
    <div class="db-user">
      <button class="db-topbar-avatar" title=${user?.email || "Account"} onClick=${(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
        ${initials}
      </button>
      ${open ? html`
        <div class="db-user-menu" onClick=${(e) => e.stopPropagation()}>
          <div class="db-user-id">
            <div class="db-user-name">${user?.name || "Account"}</div>
            <div class="db-user-email">${user?.email || ""}</div>
          </div>
          <div class="db-user-sep"></div>
          <button class="db-user-item" type="button" onClick=${() => { setOpen(false); onNav && onNav("/account/billing"); }}>
            Billing & plan
          </button>
          ${user?.is_super_admin ? html`
            <button class="db-user-item db-user-item-admin" type="button" onClick=${() => { setOpen(false); onNav && onNav("/admin"); }}>
              Platform admin →
            </button>
          ` : null}
          <button class="db-user-item" type="button" onClick=${() => { setOpen(false); onSignOut && onSignOut(); }}>
            Sign out
          </button>
        </div>
      ` : ""}
    </div>
  `;
}

// ─── WizardView ────────────────────────────────────────────────────────────
//
// The DEFAULT build surface (PO direction): a deterministic, multi-step FORM
// driven by the industry template's question list. Chat and voice are offered
// as alternates in the header. Questions are grouped ~3 per step so it reads
// like the reference UI (left rail persona + marketing + step progress; right
// panel form with Back / Next / Create).
//
// Data: GET /api/build/template?industry=&locale= → { questions[], persona,
// sector, intro }. On finish: POST /api/build/wizard → { agent } → same reveal
// path as chat/voice.
function WizardView({ industry, locale, initialText, presets, onClose, onSwitchToChat, onSwitchToVoice, onAgentSaved }) {
  const [tpl, setTpl] = useState(null);
  const [loadErr, setLoadErr] = useState("");
  const [answers, setAnswers] = useState({});
  const [step, setStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState("");
  const [fieldErrs, setFieldErrs] = useState({});
  // True while the LLM is pre-filling fields from the operator's typed
  // description (landing prompt box). Surfaces a tiny "pre-filling…" note.
  const [prefilling, setPrefilling] = useState(false);
  // Tracks which fields the LLM filled, so we can badge them as "from your
  // description" — a light trust signal that the pre-fill is editable.
  const [prefilled, setPrefilled] = useState({});
  // Fields the operator has touched — once touched, an incoming (slower)
  // LLM pre-fill must NOT clobber their edit.
  const touchedRef = useRef({});
  const mainRef = useRef(null);
  // ── URL → Firecrawl → YAML pre-fill (knowledge base) ─────────────────────
  // Optional on step 1: the operator can paste a website / Google-Maps /
  // local-listing URL. We scrape it server-side via Firecrawl, the best model
  // condenses it to a YAML brief, and they REVIEW + edit it before the agent
  // is created. The YAML is folded into the agent's system prompt at save
  // time under a clearly-bounded KNOWLEDGE block.
  const [urlInput, setUrlInput] = useState("");
  // "idle" | "loading" | "preview" | "error"
  const [urlState, setUrlState] = useState("idle");
  const [urlErr, setUrlErr] = useState("");
  // { yaml, source: {kind, url, title} } once a scrape has been condensed.
  const [knowledge, setKnowledge] = useState(null);

  const FIELDS_PER_STEP = 3;
  const preset = industry ? INDUSTRY_PRESETS[industry] : null;
  const industryLabel = preset ? preset.label : (industry || "business");
  // Catch-all mode: no specific industry but the operator typed a use case →
  // the best model designs a bespoke form + agent for ANY use case.
  const isCatchAll = !industry && !!(initialText || "").trim();

  // Seed the form's `answers` from question defaults + the agent-name
  // suggestion (so step 1 isn't all-blank), plus any pre-filled answers.
  const seedAnswers = (data, prefill) => {
    const seed = {};
    (data.questions || []).forEach((q) => {
      if (q.default !== undefined && q.default !== null && q.default !== "") seed[q.id] = q.default;
      if (q.id === "agent_name" && Array.isArray(q.suggestions) && q.suggestions.length && seed[q.id] == null) {
        seed[q.id] = q.suggestions[0];
      }
    });
    if (prefill && typeof prefill === "object") {
      const badge = {};
      Object.keys(prefill).forEach((id) => { seed[id] = prefill[id]; badge[id] = true; });
      setPrefilled(badge);
    }
    setAnswers(seed);
  };

  // Load the form. Catch-all → POST /api/build/dynamic-template (the best
  // model designs questions + pre-fills). Otherwise → GET the static template
  // and, if the operator typed a description, LLM-extract a pre-fill.
  useEffect(() => {
    let cancelled = false;
    setTpl(null); setLoadErr(""); setStep(0); setAnswers({}); setFieldErrs({}); setSubmitErr("");
    setPrefilling(false); setPrefilled({}); touchedRef.current = {};
    const loc = locale || "en-IN";
    const desc = (initialText || "").trim();

    if (isCatchAll) {
      // Dynamic, use-case-tailored form via the best model (one call returns
      // questions + pre-fill). While it thinks, the big "designing…" state shows.
      fetch("/api/build/dynamic-template", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: desc, locale: loc }),
      })
        .then((r) => r.ok ? r.json() : Promise.reject(new Error("Couldn't design the form (" + r.status + ")")))
        .then((res) => {
          if (cancelled) return;
          const data = res && res.template;
          if (!data) throw new Error("No form returned.");
          setTpl(data);
          seedAnswers(data, res.dynamic ? data.prefill : null);
        })
        .catch((e) => { if (!cancelled) setLoadErr(String(e.message || e)); });
      return () => { cancelled = true; };
    }

    const qs = new URLSearchParams({ industry: industry || "", locale: loc }).toString();
    fetch(`/api/build/template?${qs}`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("Couldn't load the form (" + r.status + ")")))
      .then((data) => {
        if (cancelled) return;
        setTpl(data);
        seedAnswers(data, null);
        // LLM pre-fill from the typed description (industry template path).
        if (desc) {
          setPrefilling(true);
          fetch("/api/build/extract", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ industry: industry || "", locale: loc, text: desc }),
          })
            .then((r) => r.ok ? r.json() : null)
            .then((res) => {
              if (cancelled || !res || !res.answers) return;
              const got = res.answers;
              const ids = Object.keys(got);
              if (!ids.length) return;
              setAnswers((prev) => {
                const next = { ...prev };
                ids.forEach((id) => { if (!touchedRef.current[id]) next[id] = got[id]; });
                return next;
              });
              const badge = {};
              ids.forEach((id) => { if (!touchedRef.current[id]) badge[id] = true; });
              setPrefilled((prev) => ({ ...prev, ...badge }));
            })
            .catch(() => {})
            .finally(() => { if (!cancelled) setPrefilling(false); });
        }
      })
      .catch((e) => { if (!cancelled) setLoadErr(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [industry, locale, initialText]);

  const questions = (tpl && tpl.questions) || [];
  const steps = [];
  for (let i = 0; i < questions.length; i += FIELDS_PER_STEP) steps.push(questions.slice(i, i + FIELDS_PER_STEP));
  const totalSteps = Math.max(steps.length, 1);
  const stepQuestions = steps[step] || [];
  const isLast = step >= totalSteps - 1;

  const setAnswer = (qid, val) => {
    touchedRef.current[qid] = true;   // operator edit wins over late LLM pre-fill
    setAnswers((prev) => ({ ...prev, [qid]: val }));
    setFieldErrs((prev) => { if (!prev[qid]) return prev; const n = { ...prev }; delete n[qid]; return n; });
    setPrefilled((prev) => { if (!prev[qid]) return prev; const n = { ...prev }; delete n[qid]; return n; });
    if (submitErr) setSubmitErr("");
  };
  const humanize = (s) => { const v = String(s || "").replace(/_/g, " ").trim(); return v.charAt(0).toUpperCase() + v.slice(1); };

  const isFilled = (q) => {
    const v = answers[q.id];
    if (q.type === "bool") return v === true || v === false;
    if (Array.isArray(v)) return v.length > 0;
    return v != null && String(v).trim() !== "";
  };
  const stepValid = stepQuestions.every((q) => !q.required || isFilled(q));

  const agentName = (answers.agent_name && String(answers.agent_name).trim()) || "Your agent";

  useEffect(() => { try { mainRef.current?.scrollTo({ top: 0, behavior: "smooth" }); } catch {} }, [step]);

  const goNext = () => { if (!isLast) setStep((s) => s + 1); else submit(); };
  const goBack = () => { if (step > 0) setStep((s) => s - 1); };

  const pullFromUrl = async () => {
    const u = (urlInput || "").trim();
    if (!u) return;
    setUrlState("loading"); setUrlErr("");
    try {
      const ctx = tpl?.use_case || initialText || industryLabel || "";
      const res = await fetch("/api/build/scrape-url", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: u, locale: locale || "en-IN", context: ctx }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        const msg = (d && (d.detail?.message || d.detail)) || ("Couldn't pull from that URL (" + res.status + ").");
        throw new Error(msg);
      }
      const data = await res.json();
      if (!data || !data.yaml) throw new Error("Nothing useful was found on that page.");
      setKnowledge({ yaml: data.yaml, source: data.source || { kind: "url", url: u, title: "" } });
      setUrlState("preview");
    } catch (e) {
      setUrlErr(String(e.message || e));
      setUrlState("error");
    }
  };
  const discardKnowledge = () => {
    setKnowledge(null); setUrlState("idle"); setUrlInput(""); setUrlErr("");
  };

  const submit = async () => {
    setSubmitting(true); setSubmitErr(""); setFieldErrs({});
    try {
      // Dynamic (catch-all) builds carry the use case + generated questions
      // back so the best model can compose a bespoke agent; template builds
      // just send the industry.
      const payload = (tpl && tpl.dynamic)
        ? { dynamic: true, use_case: tpl.use_case || (initialText || ""), sector: tpl.sector,
            questions: tpl.questions, locale: locale || "en-IN", answers }
        : { industry: industry || "", locale: locale || "en-IN", answers };
      if (knowledge && knowledge.yaml) {
        payload.knowledge_yaml = knowledge.yaml;
        payload.knowledge_source = knowledge.source || null;
      }
      const res = await fetch("/api/build/wizard", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.status === 422) {
        const d = await res.json().catch(() => ({}));
        const fields = (d && d.detail && d.detail.fields) || {};
        setFieldErrs(fields);
        const firstBad = questions.findIndex((q) => fields[q.id]);
        if (firstBad >= 0) setStep(Math.floor(firstBad / FIELDS_PER_STEP));
        throw new Error("Please complete the highlighted fields.");
      }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error((d && d.detail) || ("Couldn't save (" + res.status + ")")); }
      const data = await res.json();
      if (data && data.agent) { onAgentSaved && onAgentSaved(data.agent); return; }
      throw new Error("Save returned no agent.");
    } catch (e) {
      setSubmitErr(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  };

  const renderField = (q) => {
    const v = answers[q.id];
    const err = fieldErrs[q.id];
    let control;
    if (q.type === "enum" && Array.isArray(q.options)) {
      control = html`<div class="wiz-chips">
        ${q.options.map((opt) => html`
          <button type="button" key=${opt}
                  class=${"wiz-chip" + (String(v) === String(opt) ? " wiz-chip-on" : "")}
                  onClick=${() => setAnswer(q.id, opt)}>${humanize(opt)}</button>
        `)}
      </div>`;
    } else if (q.type === "bool") {
      control = html`<div class="wiz-chips">
        <button type="button" class=${"wiz-chip" + (v === true ? " wiz-chip-on" : "")} onClick=${() => setAnswer(q.id, true)}>Yes</button>
        <button type="button" class=${"wiz-chip" + (v === false ? " wiz-chip-on" : "")} onClick=${() => setAnswer(q.id, false)}>No</button>
      </div>`;
    } else {
      const inputType = q.type === "email" ? "email" : q.type === "phone" ? "tel" : "text";
      const shown = Array.isArray(v) ? v.join(", ") : (v == null ? "" : v);
      // Build 212 — defensive against browser autofill cross-pollination:
      //   • `name` is q.id so each input has a unique identifier
      //   • `autoComplete="off"` blocks Chrome from grouping the fields
      //     under a single "address book" entry and overwriting siblings
      //   • The wrapping div already keys by q.id; the explicit `key`
      //     here protects against any DOM-node reuse when step content
      //     reflows during re-render.
      // Symptom that prompted this: typing in "business_name" was
      // observed overwriting "services_offered" with the same text.
      control = html`<input
        key=${"wiz-input-" + q.id}
        class=${"wiz-input" + (err ? " wiz-input-err" : "") + (prefilled[q.id] ? " wiz-input-prefilled" : "")}
        type=${inputType}
        name=${q.id}
        autoComplete="off"
        autoCorrect="off"
        spellCheck="false"
        value=${shown}
        placeholder=${q.hint || ""}
        onInput=${(e) => setAnswer(q.id, e.target.value)} />`;
    }
    // Suggestion chips (e.g. agent_name) — quick-pick under text fields.
    const sugs = (q.type !== "enum" && Array.isArray(q.suggestions) && q.suggestions.length)
      ? html`<div class="wiz-suggests">
          ${q.suggestions.map((sg) => html`
            <button type="button" key=${sg}
                    class=${"wiz-suggest" + (String(v) === String(sg) ? " wiz-suggest-on" : "")}
                    onClick=${() => setAnswer(q.id, sg)}>${sg}</button>
          `)}
        </div>`
      : "";
    return html`
      <div class="wiz-field" key=${q.id}>
        <label class="wiz-label">
          ${q.label}${q.required ? html`<span class="wiz-req" aria-hidden="true"> *</span>` : ""}
          ${prefilled[q.id] ? html`<span class="wiz-prefill-badge" title="Pulled from what you typed — edit if needed">✨ from your description</span>` : ""}
        </label>
        ${q.prompt && q.prompt !== q.label ? html`<div class="wiz-prompt">${q.prompt}</div>` : ""}
        ${control}
        ${sugs}
        ${err ? html`<div class="wiz-field-err">${err === "required" ? "This field is required." : err}</div>` : ""}
      </div>
    `;
  };

  // Context handed to the parent when switching to chat/voice mid-wizard.
  // For template builds the parent syncs answers to the build_session; for
  // dynamic (catch-all) builds it folds the use case + answers into a single
  // opening message (there's no static template for the chat to resume).
  const switchMeta = () => {
    const dynamic = !!(tpl && tpl.dynamic);
    const labelById = {};
    ((tpl && tpl.questions) || []).forEach((q) => { labelById[q.id] = q.label || q.id; });
    const summary = Object.keys(answers)
      .filter((id) => { const v = answers[id]; return v != null && String(v).trim() !== ""; })
      .map((id) => {
        let v = answers[id];
        if (Array.isArray(v)) v = v.join(", ");
        if (v === true) v = "yes"; if (v === false) v = "no";
        return `- ${labelById[id] || id}: ${v}`;
      }).join("\n");
    return { dynamic, useCase: (tpl && tpl.use_case) || (initialText || ""), summary };
  };

  // Header alt-mode toggles + close, reused across states.
  const header = html`
    <div class="wiz-head">
      <span class="wiz-pill">${tpl ? `${step + 1} of ${totalSteps}` : "…"}</span>
      <div class="wiz-head-alts">
        <span class="wiz-head-altlabel">Prefer to</span>
        <button class="wiz-altbtn" type="button" onClick=${() => onSwitchToChat && onSwitchToChat(answers, switchMeta())} title="Build by chatting instead — keeps what you've filled">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.5 7.2L4 20l1-4.8A8 8 0 1 1 21 12z"/></svg>
          <span>Chat</span>
        </button>
        <button class="wiz-altbtn" type="button" onClick=${() => onSwitchToVoice && onSwitchToVoice(answers, switchMeta())} title="Build by talking instead — keeps what you've filled">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
          <span>Voice</span>
        </button>
        <button class="wiz-close" type="button" onClick=${onClose} aria-label="Close">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
    </div>`;

  return html`
    <div class="wiz">
      <div class="wiz-shell">
        <!-- Left rail: persona + marketing + step progress -->
        <aside class="wiz-rail">
          <div class="wiz-persona">
            <div class="wiz-persona-avatar" aria-hidden="true">${(agentName[0] || "A").toUpperCase()}</div>
            <div class="wiz-persona-meta">
              <div class="wiz-persona-name">${agentName}</div>
              <div class="wiz-persona-role">${(tpl && tpl.dynamic && tpl.agent_role) ? tpl.agent_role : `${industryLabel} agent`}</div>
            </div>
          </div>
          <h2 class="wiz-rail-headline">
            ${preset ? html`Build your <em>${industryLabel.toLowerCase()}</em> phone agent in minutes.`
                     : html`Build your <em>phone AI agent</em> in minutes.`}
          </h2>
          <p class="wiz-rail-sub">
            Answer a few quick questions and ${agentName === "Your agent" ? "she" : agentName} is ready to take calls 24/7 — bookings, FAQs, transfers.
          </p>
          <div class="wiz-rail-steps">
            <div class="wiz-rail-steplabel">Step ${tpl ? step + 1 : 1} of ${totalSteps}</div>
            <div class="wiz-rail-dots">
              ${Array.from({ length: totalSteps }).map((_, i) => html`
                <span key=${i} class=${"wiz-dot" + (i === step ? " wiz-dot-on" : i < step ? " wiz-dot-done" : "")}></span>
              `)}
            </div>
          </div>
        </aside>

        <!-- Right panel: the form -->
        <section class="wiz-main" ref=${mainRef}>
          ${header}
          ${loadErr ? html`
            <div class="wiz-state">
              <p class="wiz-state-err">${loadErr}</p>
              <button class="wiz-btn-secondary" type="button" onClick=${onClose}>Back to home</button>
            </div>
          ` : !tpl ? html`
            <div class="wiz-state">
              <div class="wiz-spinner" aria-label="Loading"></div>
              ${isCatchAll ? html`<p class="wiz-state-msg">✨ Designing a custom agent for your use case…</p>` : ""}
            </div>
          ` : html`
            <h1 class="wiz-title">
              ${step === 0
                ? (tpl.dynamic ? "Let's set up your custom agent" : html`Let's set up your ${industryLabel.toLowerCase()} agent`)
                : isLast ? "Last details — then meet your agent" : "A few more details"}
            </h1>
            ${step === 0 && tpl.intro ? html`<p class="wiz-intro">${tpl.intro.trim()}</p>` : ""}
            ${step === 0 ? html`
              <div class="wiz-knowledge">
                ${urlState !== "preview" ? html`
                  <div class="wiz-knowledge-head">
                    <span class="wiz-knowledge-emoji" aria-hidden="true">🌐</span>
                    <div class="wiz-knowledge-meta">
                      <div class="wiz-knowledge-title">Got a website, Google Maps listing, or local-listing URL?</div>
                      <div class="wiz-knowledge-sub">Paste it — Eva pulls the facts (hours, services, pricing, FAQs) and shows you a YAML preview to review before saving.</div>
                    </div>
                  </div>
                  <div class="wiz-knowledge-row">
                    <input class="wiz-input wiz-knowledge-input" type="url"
                           placeholder="https://your-business.com or a Google Maps link"
                           value=${urlInput}
                           onInput=${(e) => { setUrlInput(e.target.value); if (urlState === "error") { setUrlErr(""); setUrlState("idle"); } }}
                           disabled=${urlState === "loading"} />
                    <button class="wiz-btn-primary wiz-knowledge-go" type="button"
                            onClick=${pullFromUrl}
                            disabled=${!urlInput.trim() || urlState === "loading"}>
                      ${urlState === "loading"
                        ? html`<span class="wiz-spinner-sm" aria-hidden="true"></span> Pulling…`
                        : "Pull facts"}
                    </button>
                  </div>
                  ${urlState === "error" ? html`<div class="wiz-knowledge-err">${urlErr}</div>` : ""}
                  <div class="wiz-knowledge-foot">Skip this if you'd rather just fill the form below.</div>
                ` : html`
                  <div class="wiz-knowledge-head">
                    <span class="wiz-knowledge-emoji" aria-hidden="true">✅</span>
                    <div class="wiz-knowledge-meta">
                      <div class="wiz-knowledge-title">
                        Facts pulled from <a href=${knowledge.source?.url || "#"} target="_blank" rel="noopener" class="wiz-knowledge-link">${knowledge.source?.title || knowledge.source?.url || "your URL"}</a>
                      </div>
                      <div class="wiz-knowledge-sub">Review and edit — saved to ${agentName === "Your agent" ? "your agent" : agentName}'s knowledge base on Create.</div>
                    </div>
                    <button class="wiz-knowledge-discard" type="button" onClick=${discardKnowledge} title="Discard these facts">Discard</button>
                  </div>
                  <textarea class="wiz-knowledge-yaml"
                            rows="9"
                            spellCheck="false"
                            value=${knowledge.yaml || ""}
                            onInput=${(e) => setKnowledge((k) => ({ ...(k || {}), yaml: e.target.value }))}></textarea>
                  <div class="wiz-knowledge-foot">YAML format — edit freely. Eva uses this verbatim to answer callers.</div>
                `}
              </div>
            ` : ""}
            ${prefilling ? html`<div class="wiz-prefilling"><span class="wiz-spinner-sm" aria-hidden="true"></span> Pre-filling from your description…</div>` : ""}
            <div class="wiz-fields">
              ${stepQuestions.map((q) => renderField(q))}
            </div>
            ${submitErr ? html`<div class="wiz-submit-err">${submitErr}</div>` : ""}
            <div class="wiz-foot">
              ${step > 0
                ? html`<button class="wiz-btn-secondary" type="button" onClick=${goBack} disabled=${submitting}>Back</button>`
                : html`<span></span>`}
              <button class="wiz-btn-primary" type="button" onClick=${goNext}
                      disabled=${!stepValid || submitting}>
                ${submitting ? "Creating…" : isLast ? "Create agent" : "Next"}
                ${!submitting ? html`<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>` : ""}
              </button>
            </div>
          `}
        </section>
      </div>
    </div>
  `;
}

// Sign-in / sign-up — share the same shell so the visual rhythm matches.
// Form fields render inside a centred card with the SpiderX wordmark up top
// and the friendly tagline. Submitting hits /api/auth/(login|signup) and on
// success persists the user via saveAuth() and routes home.
function AuthPage({ mode, defaults, onAuthed, onSwitch }) {
  const [email, setEmail] = useState(defaults?.email || "");
  const [name, setName] = useState(defaults?.name || "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isSignup = mode === "signup";
  const submit = async (e) => {
    e?.preventDefault();
    setError(""); setBusy(true);
    try {
      const body = { email: email.trim() };
      if (isSignup) body.name = name.trim() || null;
      if (password) body.password = password;
      let res = await fetch(isSignup ? "/api/auth/signup" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // Mock convenience: signing in with an email that has no account yet
      // transparently creates one (Auth0 will replace this). Without it, a
      // fresh visitor hitting the build gate would get a dead-end 404.
      if (!res.ok && !isSignup && res.status === 404) {
        res = await fetch("/api/auth/signup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email.trim() }),
        });
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error((d && (d.detail?.message || d.detail)) || "Something went wrong");
      }
      const user = await res.json();
      saveAuth(user);
      onAuthed && onAuthed(user);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  };
  // Mock Google sign-in. Real OAuth lands with Auth0; for now we upsert a
  // demo Google identity server-side so the login→resume flow is demoable.
  const googleSignIn = async () => {
    setError(""); setBusy(true);
    try {
      const guess = email.trim() && email.includes("@") ? email.trim() : "demo.user@gmail.com";
      const res = await fetch("/api/auth/google", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: guess, name: name.trim() || null }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Google sign-in failed");
      }
      const user = await res.json();
      saveAuth(user);
      onAuthed && onAuthed(user);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  };
  return html`
    <div class="db-auth">
      <div class="db-auth-card">
        <div class="db-auth-brand">
          <span class="db-topbar-wordmark" aria-label="SpiderX.AI">
            <span class="db-topbar-wm-spider">Spider</span><span class="db-topbar-wm-x">X</span><span class="db-topbar-wm-ai">.AI</span>
          </span>
          <span class="db-topbar-tag">AI Agent Builder</span>
        </div>
        <h1 class="db-auth-title">${isSignup ? "Create your account" : "Welcome back"}</h1>
        <p class="db-auth-sub">${isSignup ? "Build phone AI agents that ring real numbers. Free for the first 300 minutes." : "Pick up where you left off."}</p>
        <form class="db-form" onSubmit=${submit}>
          ${isSignup ? html`
            <label class="db-form-field">
              <span class="db-form-label">Your name <span class="db-form-opt">(optional)</span></span>
              <input class="db-input" type="text" value=${name} onInput=${(e) => setName(e.target.value)} placeholder="e.g. Dipesh" />
            </label>
          ` : ""}
          <label class="db-form-field">
            <span class="db-form-label">Email</span>
            <input class="db-input" type="email" autoComplete="email" autoFocus value=${email} onInput=${(e) => setEmail(e.target.value)} placeholder="you@company.com" required />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Password</span>
            <input class="db-input" type="password" autoComplete=${isSignup ? "new-password" : "current-password"} value=${password} onInput=${(e) => setPassword(e.target.value)} placeholder="••••••••" />
            <span class="db-form-help">Auth0 takes over later — for now any value works.</span>
          </label>
          ${error ? html`<div class="golive-error">${error}</div>` : ""}
          <button type="submit" class="db-btn-primary db-auth-cta" disabled=${busy || !email.trim()}>
            ${busy ? (isSignup ? "Creating…" : "Signing in…") : (isSignup ? "Create account" : "Sign in")}
          </button>
        </form>
        <button class="db-auth-google" type="button" onClick=${googleSignIn} disabled=${busy}>
          <svg viewBox="0 0 24 24" width="16" height="16"><path fill="#4285F4" d="M21.6 12.2c0-.7-.1-1.3-.2-2H12v3.9h5.4a4.6 4.6 0 0 1-2 3v2.5h3.3c1.9-1.8 3-4.4 3-7.4z"/><path fill="#34A853" d="M12 22c2.7 0 5-.9 6.7-2.4l-3.3-2.5c-.9.6-2 1-3.4 1-2.6 0-4.8-1.7-5.6-4.1H3v2.5C4.6 19.7 8 22 12 22z"/><path fill="#FBBC05" d="M6.4 14a6 6 0 0 1 0-3.9V7.5H3a10 10 0 0 0 0 9z"/><path fill="#EA4335" d="M12 5.9c1.5 0 2.8.5 3.8 1.5l2.9-2.8C16.9 2.9 14.7 2 12 2 8 2 4.6 4.3 3 7.5l3.4 2.6c.8-2.4 3-4.2 5.6-4.2z"/></svg>
          <span>Continue with Google</span>
        </button>
        <div class="db-auth-switch">
          ${isSignup
            ? html`<span>Already have an account?</span> <button type="button" onClick=${() => onSwitch && onSwitch("login")}>Sign in</button>`
            : html`<span>New here?</span> <button type="button" onClick=${() => onSwitch && onSwitch("signup")}>Create account</button>`
          }
        </div>
      </div>
    </div>
  `;
}

// AgentSwitcher — the big primary block at the top of the sidebar that
// doubles as the agent picker. Custom-built (not a native <select>) so we
// can give it (a) internal scrolling for long agent lists and (b) a sticky
// footer with "All agents" + "Build new" that stays reachable no matter
// how far the user scrolls. Closes on outside click + Escape.
function AgentSwitcher({ agents, agent, onNav }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const items = agents || [];
  const label = agent ? agent.name : "Your agents";
  const others = agent
    ? items.filter((a) => String(a.id) !== String(agent.id))
    : items;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const go = (route) => { setOpen(false); onNav && onNav(route); };

  return html`
    <div class=${"db-switcher-root" + (open ? " is-open" : "")} ref=${rootRef}>
      <button class="db-nav-primary db-switcher-trigger" type="button"
              aria-haspopup="listbox" aria-expanded=${open}
              onClick=${() => setOpen((o) => !o)}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M3 7l3-3h12l3 3v13a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7z"/><path d="M3 7h18"/></svg>
        <span class="db-switcher-label">${label}</span>
        <svg class="db-switcher-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
      </button>

      ${open ? html`
        <div class="db-switcher-menu" role="listbox" aria-label="Switch agent">
          ${agent ? html`
            <div class="db-switcher-current">
              <span class="db-switcher-current-tag">Now viewing</span>
              <span class="db-switcher-current-name">${agent.name}</span>
              <svg class="db-switcher-tick" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>
            </div>
          ` : ""}
          ${others.length > 0 ? html`
            <div class="db-switcher-header">Switch to</div>
            <div class="db-switcher-list">
              ${others.map((a) => html`
                <button key=${a.id} class="db-switcher-item" type="button" role="option"
                        onClick=${() => go(`/agent/${a.slug || a.id}`)}>
                  <span class="db-switcher-item-dot" aria-hidden="true"></span>
                  <span class="db-switcher-item-name">${a.name}</span>
                  ${a.published ? html`<span class="db-switcher-item-pill is-live">Live</span>` : html`<span class="db-switcher-item-pill is-draft">Draft</span>`}
                </button>
              `)}
            </div>
          ` : agent ? html`
            <div class="db-switcher-empty">No other agents yet.</div>
          ` : ""}
          <!-- Sticky footer — always reachable regardless of scroll.
               Primary CTA goes on top (Build new), secondary below. -->
          <div class="db-switcher-footer">
            <button class="db-switcher-foot-btn is-primary" type="button" onClick=${() => go("/")}>
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
              <span>Build new agent</span>
            </button>
            <button class="db-switcher-foot-btn" type="button" onClick=${() => go("/agents")}>
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="4" rx="1"/><rect x="3" y="11" width="18" height="4" rx="1"/><rect x="3" y="18" width="12" height="3" rx="1"/></svg>
              <span>View all agents</span>
            </button>
          </div>
        </div>
      ` : ""}
    </div>
  `;
}

function DashboardShell({ activeKey, agent, plan, agents, user: userProp, theme: themeProp, onToggleTheme, onNav, onSignOut, title, subtitle, actions, body, hideSidebar }) {
  // Resolve user/theme from localStorage if the caller didn't pass them in —
  // saves us threading those props through every per-agent page component.
  const user = userProp || loadAuth();
  const [supportOpen, setSupportOpen] = useState(false);
  // Phone sidebar drawer state. The CSS keeps the sidebar inline above
  // ~768px and turns it into an off-canvas slide-in below; toggling the
  // `is-mobile-open` class is what reveals it on phones. We also lock
  // body scroll while the drawer is open so background content doesn't
  // jiggle behind it.
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const prev = document.body.style.overflow;
    if (mobileNavOpen) document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [mobileNavOpen]);
  const [localTheme, setLocalTheme] = useState(() => themeProp || loadTheme());
  const theme = themeProp || localTheme;
  const handleToggle = onToggleTheme || (() => {
    setLocalTheme((t) => {
      const next = t === "light" ? "dark" : "light";
      applyTheme(next);
      return next;
    });
  });
  const signOut = onSignOut || (() => {
    clearAuth();
    try { window.history.replaceState({}, "", "/login"); } catch {}
    window.location.reload();
  });
  // Group structure — collapsible sections of the left nav.
  // navTo also dismisses the mobile drawer so the operator lands on the
  // new page without an open hamburger menu on top of it.
  const navTo = (route) => {
    if (mobileNavOpen) setMobileNavOpen(false);
    if (typeof onNav === "function") onNav(route);
  };
  const agentSlug = agent?.slug || agent?.id;
  const itemActive = (key) => activeKey === key;

  const groups = [
    // Per-agent groups, ordered by the operator's natural build → live → run
    // journey. Each group answers ONE question; mixing different concerns in
    // one group (the old "Test & launch" lumped pre-launch actions with the
    // post-launch analytics — "Call logs" had nothing to do with launching)
    // made the mental model fuzzy and the labels lie.
    //
    //   1. About the business — what she IS and what she KNOWS.
    //   2. Voice & behaviour — how she SOUNDS and what she will / won't do.
    //   3. Test & launch — pre-launch ACTIONS only (try her, publish her).
    //   4. Insights — post-launch RESULTS (call activity + outcomes report).
    //   5. Developer — webhooks + raw data, for power users.
    //
    // Account groups follow at the bottom, separated so the per-agent
    // workflow stays visually distinct from workspace administration.
    agent ? {
      key: "about",
      label: "About the business",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>`,
      items: [
        { key: "overview",  label: "Overview",         route: `/agent/${agentSlug}` },
        // Core purpose sits right under Overview because it answers
        // the most important "what" question — what is she built to
        // do? It was previously embedded inside the Overview page; now
        // it has its own dashboard surface so it's discoverable from
        // the sidebar, deep-linkable, and not buried under stats tiles.
        { key: "purpose",    label: "Core purpose",     route: `/agent/${agentSlug}/purpose` },
        { key: "profile",    label: "Business profile", route: `/agent/${agentSlug}/profile` },
        { key: "extra-info", label: "Additional info",  route: `/agent/${agentSlug}/extra-info` },
        { key: "knowledge",  label: "Knowledge base",   route: `/agent/${agentSlug}/knowledge` },
      ],
    } : null,
    agent ? {
      key: "voice",
      label: "Voice & behaviour",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>`,
      items: [
        { key: "persona",        label: "Persona & tone", route: `/agent/${agentSlug}/persona` },
        { key: "voice-settings", label: "Voice settings", route: `/agent/${agentSlug}/voice` },
        { key: "small-talk",     label: "Small talk",     route: `/agent/${agentSlug}/small-talk` },
        { key: "guardrails",     label: "Guardrails",     route: `/agent/${agentSlug}/guardrails` },
      ],
    } : null,
    agent ? {
      key: "launch",
      label: "Test & launch",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 11l7-7 7 7"/><path d="M12 4v14"/><path d="M5 21h14"/></svg>`,
      items: [
        // Pre-launch ACTIONS only. Activity / outcomes moved to Insights
        // below so this group is purely "make it real".
        { key: "test-call", label: "Get a test call",  route: `/agent/${agentSlug}/test-call` },
        { key: "live",      label: "Go live",          route: `/agent/${agentSlug}/go-live`, statusBadge: agent.published ? "live" : "draft" },
      ],
    } : null,
    agent ? {
      key: "insights",
      label: "Call Analytics",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-7"/></svg>`,
      items: [
        // Logs = what happened. Outcomes = what was the result.
        // Both are POST-launch surfaces — separate from Test & launch.
        { key: "calls",    label: "Call logs",     route: `/agent/${agentSlug}/calls` },
        { key: "outcomes", label: "Call outcomes", route: `/agent/${agentSlug}/outcomes` },
      ],
    } : null,
    agent ? {
      key: "developer",
      label: "Developer",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/></svg>`,
      items: [
        { key: "developer", label: "Webhooks & data", route: `/agent/${agentSlug}/developer` },
      ],
    } : null,
    {
      key: "admin",
      label: "Account",
      icon: html`<svg class="db-nav-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/></svg>`,
      items: [
        { key: "org",          label: "Workspace",      route: "/account/org" },
        { key: "team",         label: "Team & invites", route: "/account/team" },
        { key: "billing",      label: "Billing & plan", route: "/account/billing" },
        { key: "integrations", label: "Integrations",   route: "/account/integrations" },
      ],
    },
  ].filter(Boolean);

  // Default-open: the group that holds the currently-active item, and
  // (when an agent is open) the three primary per-agent groups so the
  // top-of-funnel tabs are visible without an extra click. Developer
  // stays collapsed — it's the power-user surface, not the default path.
  // Default-open ONLY the group containing the current page, plus any
  // group the operator manually opened earlier in this tab (persisted in
  // sessionStorage so navigating doesn't collapse what they just opened).
  // Everything else collapses by default — the page used to show 11 nav
  // items + 3 collapsed heads at once, which crowded the eye-line. Now you
  // see ~2-5 items above, the active group below the head you clicked, and
  // every other group is one click away. Much more breathing room.
  const NAV_OPEN_KEY = agent ? `sxai.nav_open.${agent.id}` : "sxai.nav_open.global";
  const [openGroups, setOpenGroups] = useState(() => {
    const o = {};
    const activeGroup = groups.find((g) => g.items?.some((it) => it.key === activeKey))?.key;
    let saved = {};
    try {
      const raw = sessionStorage.getItem(NAV_OPEN_KEY);
      if (raw) saved = JSON.parse(raw) || {};
    } catch {}
    groups.forEach((g) => {
      o[g.key] = (g.key === activeGroup) || !!saved[g.key];
    });
    return o;
  });
  const toggleGroup = (k) => setOpenGroups((o) => {
    const next = { ...o, [k]: !o[k] };
    // Persist user's manual expansions across page navigations within the
    // same tab — the operator who opened Insights to peek at calls won't
    // have to re-open it after every page change.
    try { sessionStorage.setItem(NAV_OPEN_KEY, JSON.stringify(next)); } catch {}
    return next;
  });

  // Primary sidebar button: contextual — agent name when one is open, else "Your agents".
  const primaryLabel = agent ? agent.name : "Your agents";
  const primaryRoute = agent ? `/agent/${agentSlug}` : "/agents";

  return html`
    <div class="db-root">
      <header class="db-topbar">
        ${hideSidebar ? "" : html`
          <button class="db-topbar-hamburger" type="button"
                  aria-label=${mobileNavOpen ? "Close menu" : "Open menu"}
                  aria-expanded=${mobileNavOpen}
                  onClick=${() => setMobileNavOpen((o) => !o)}>
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              ${mobileNavOpen
                ? html`<path d="M6 6l12 12M6 18L18 6"/>`
                : html`<path d="M3 7h18M3 12h18M3 17h18"/>`}
            </svg>
          </button>
        `}
        <a class="db-topbar-brand db-topbar-brand-link" href="/" onClick=${(e) => { e.preventDefault(); navTo("/agents"); }}
           aria-label="SpiderX.AI — back to all agents">
          <span class="db-topbar-logo">
            <${SpiderXLogo} height=${29} />
          </span>
          <span class="db-topbar-tag">AI Agent Builder</span>
          <!-- Agent switcher moved INTO the sidebar primary block — see
               .db-nav-primary-switch below. The topbar stays focused on
               brand + plan + user, the sidebar owns "what am I looking at +
               where do I want to go". -->
        </a>
        <div class="db-topbar-right">
          <button class="db-topbar-support" type="button" onClick=${() => setSupportOpen(true)}>Help</button>
          <div class="db-topbar-minutes">
            <span>${plan?.minutesLeft ?? 300}/${plan?.minutesTotal ?? 300} mins remaining</span>
          </div>
          <${ThemeToggle} theme=${theme} onToggle=${handleToggle} />
          <${UserMenu} user=${user} onSignOut=${signOut} onNav=${navTo} />
        </div>
      </header>
      <div class=${"db-body" + (mobileNavOpen && !hideSidebar ? " is-mobile-nav-open" : "")}>
        ${hideSidebar ? "" : html`
          <button class=${"db-nav-overlay" + (mobileNavOpen ? " is-visible" : "")}
                  type="button"
                  tabIndex=${mobileNavOpen ? 0 : -1}
                  aria-hidden=${!mobileNavOpen}
                  aria-label="Close menu"
                  onClick=${() => setMobileNavOpen(false)}></button>
          <aside class=${"db-nav" + (mobileNavOpen ? " is-mobile-open" : "")}>
            ${(agents && agents.length > 0) ? html`
              <${AgentSwitcher} agents=${agents} agent=${agent} onNav=${navTo} />
            ` : html`
              <button class="db-nav-primary" onClick=${() => navTo(primaryRoute)}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M3 7l3-3h12l3 3v13a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7z"/><path d="M3 7h18"/></svg>
                <span>${primaryLabel}</span>
              </button>
            `}

            ${groups.map((g) => html`
              <div key=${g.key} class=${"db-nav-group" + (openGroups[g.key] ? " open" : "")}>
                <button class="db-nav-group-head" onClick=${() => toggleGroup(g.key)}>
                  ${g.icon}
                  <span>${g.label}</span>
                  <svg class="db-nav-group-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
                </button>
                ${openGroups[g.key] ? html`
                  <div class="db-nav-group-items">
                    ${g.items.map((it) => html`
                      <button key=${it.key}
                              class=${"db-nav-item" + (itemActive(it.key) ? " active" : "") + (it.key === "live" ? " db-nav-item-golive" : "")}
                              onClick=${() => navTo(it.route)}>
                        ${it.key === "live" ? html`<span class="db-nav-golive-dot" aria-hidden="true"></span>` : ""}
                        <span>${it.label}</span>
                        ${it.statusBadge === "live" ? html`<span class="db-nav-item-badge is-live">Live</span>` : ""}
                        ${it.statusBadge === "draft" ? html`<span class="db-nav-item-badge is-draft">Draft</span>` : ""}
                      </button>
                    `)}
                  </div>
                ` : ""}
              </div>
            `)}

            <div class="db-nav-foot">
              ${plan?.label || "Free"} plan
              <button class="db-nav-upgrade" type="button" onClick=${() => navTo("/account/billing")}>
                <svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><path d="M13 2L4 14h6l-1 8 10-14h-7z"/></svg>
                Upgrade
              </button>
            </div>
          </aside>
        `}
        <main class=${"db-main" + (hideSidebar ? " db-main-wide" : "")}>
          <header class="db-pageheader">
            <div>
              <h1 class="db-pageheader-title">${title}</h1>
              ${subtitle ? html`<div class="db-pageheader-sub">${subtitle}</div>` : ""}
            </div>
            ${actions ? html`<div class="db-pageheader-actions">${actions}</div>` : ""}
          </header>
          <div class="db-content">
            ${body}
          </div>
        </main>
      </div>
      ${supportOpen ? html`<${SupportTicketModal} user=${user} agent=${agent} onClose=${() => setSupportOpen(false)} />` : ""}
    </div>
  `;
}

// Modal — "Raise a Support Ticket". Posts to /api/support/tickets so a human
// can pick it up later; falls back to mailto if the endpoint isn't there yet.
function SupportTicketModal({ user, agent, onClose }) {
  const [topic, setTopic] = useState("general");
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e) => {
    e?.preventDefault?.();
    if (!subject.trim() || !message.trim()) {
      setErr("Add a subject and a message.");
      return;
    }
    setBusy(true); setErr("");
    try {
      const r = await fetch("/api/support/tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic, subject: subject.trim(), message: message.trim(),
          agent_id: agent?.id || null,
          email: user?.email || null,
        }),
      });
      if (!r.ok && r.status !== 404) throw new Error("send failed");
      // 404 = endpoint not wired yet; mail it instead so the user isn't blocked.
      if (r.status === 404) {
        const to = "support@spiderx.ai";
        const subj = encodeURIComponent(`[${topic}] ${subject}`);
        const body = encodeURIComponent(`${message}\n\n— ${user?.email || "anon"}${agent ? ` · agent ${agent.id}` : ""}`);
        window.location.href = `mailto:${to}?subject=${subj}&body=${body}`;
      }
      setDone(true);
    } catch {
      setErr("Couldn't send right now — please email support@spiderx.ai.");
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div class="db-modal-backdrop" onClick=${onClose}>
      <div class="db-modal" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>How can we help?</h2>
          <button class="db-modal-close" type="button" onClick=${onClose} aria-label="Close">×</button>
        </header>
        ${done ? html`
          <div class="db-modal-body">
            <div class="db-modal-success">
              <strong>Ticket sent.</strong>
              <p>We'll reply to <code>${user?.email || "your email"}</code> within one business day.</p>
              <button class="db-modal-btn primary" type="button" onClick=${onClose}>Close</button>
            </div>
          </div>
        ` : html`
          <form class="db-modal-body" onSubmit=${submit}>
            <label class="db-modal-row">
              <span class="db-modal-label">Topic</span>
              <select class="db-modal-input" value=${topic} onChange=${(e) => setTopic(e.target.value)}>
                <option value="general">General question</option>
                <option value="bug">Something is broken</option>
                <option value="billing">Billing</option>
                <option value="numbers">Phone numbers</option>
                <option value="feature">Feature request</option>
              </select>
            </label>
            <label class="db-modal-row">
              <span class="db-modal-label">Subject</span>
              <input class="db-modal-input" type="text" maxlength="120" placeholder="One line"
                     value=${subject} onInput=${(e) => setSubject(e.target.value)} />
            </label>
            <label class="db-modal-row">
              <span class="db-modal-label">Message</span>
              <textarea class="db-modal-input" rows="6" placeholder="What's going on?"
                        value=${message} onInput=${(e) => setMessage(e.target.value)}></textarea>
            </label>
            ${err ? html`<div class="db-modal-err">${err}</div>` : ""}
            <div class="db-modal-foot">
              <button class="db-modal-btn ghost" type="button" onClick=${onClose}>Cancel</button>
              <button class="db-modal-btn primary" type="submit" disabled=${busy}>
                ${busy ? "Sending…" : "Send ticket"}
              </button>
            </div>
          </form>
        `}
      </div>
    </div>
  `;
}

// Agent overview — replaces the dark cockpit overlay. Lives at /agent/<slug>
// inside the DashboardShell so the landing splash / blob no longer bleeds
// through, and so the page can be deep-linked, scrolled, and styled like
// the rest of the dashboard.
// ─────────────────────────────────────────────────────────────────────────
// PurposeBox — "Core purpose" surface on the agent Overview.
// Eva fills this at build time via save_agent.purpose; user edits later.
// Read mode shows summary + answers as chips + active actions as labelled
// pills + post-call channels. Edit mode opens a form with the same library
// of actions the save_agent declaration exposes, so what Eva captures and
// what the user can later edit are 1:1.
// ─────────────────────────────────────────────────────────────────────────
const ACTION_LIBRARY = [
  { id: "callback_request",    label: "Request a callback",         hint: "Capture name + number, mark as urgent if needed." },
  { id: "appointment_booking", label: "Book an appointment",        hint: "Test drive, consultation, demo, service slot." },
  { id: "quote_request",       label: "Take a quote request",       hint: "What they want priced + how to reach back." },
  { id: "inquiry_capture",     label: "Capture an inquiry",         hint: "Generic lead capture — name, intent, contact." },
  { id: "complaint_intake",    label: "Take a complaint",           hint: "Full detail + severity for prompt follow-up." },
  { id: "order_status",        label: "Check order / booking status", hint: "Look up by order id, name, or phone." },
  { id: "support_ticket",      label: "Create a support ticket",    hint: "Route into the support queue." },
  { id: "emergency_routing",   label: "Route emergencies to a human", hint: "Bypass everything else if it's urgent." },
];

function PurposeBox({ agent, plan, defaultEditing = false }) {
  const initial = () => ({
    summary:   (agent?.purpose?.summary || "").trim(),
    answers:   Array.isArray(agent?.purpose?.answers) ? [...agent.purpose.answers] : [],
    actions:   Array.isArray(agent?.purpose?.actions) ? [...agent.purpose.actions] : [],
    post_call: {
      email: !!(agent?.purpose?.post_call?.email),
      sms:   !!(agent?.purpose?.post_call?.sms),
    },
  });
  // `defaultEditing` lets the dedicated Core-purpose page open straight
  // into the edit form (the operator clicked a sidebar item explicitly
  // labelled "Core purpose" — they came here to edit, not to read a
  // collapsed summary card). The read-mode is still the default for
  // any other embed of PurposeBox where editability is secondary.
  const [editing, setEditing] = useState(defaultEditing);
  const [draft, setDraft] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const [chipInput, setChipInput] = useState("");
  // Reset draft when the agent changes (e.g. user navigates to another agent).
  // Reset the form (and edit-mode) whenever we navigate to a different
  // agent. When `defaultEditing` is true (dedicated Core-purpose page),
  // re-entering the page should also re-open the form rather than
  // collapsing to read-mode after a save.
  useEffect(() => { setDraft(initial()); setEditing(defaultEditing); }, [agent?.id, defaultEditing]);

  const planSlug = plan?.plan?.slug || plan?.slug || "free";
  const smsAllowed = planSlug !== "free";

  const purpose = agent?.purpose || {};
  const hasPurpose = !!(purpose.summary || (purpose.answers || []).length || (purpose.actions || []).length);

  const addAnswer = () => {
    const v = chipInput.trim();
    if (!v) return;
    if (draft.answers.includes(v)) { setChipInput(""); return; }
    setDraft({ ...draft, answers: [...draft.answers, v].slice(0, 8) });
    setChipInput("");
  };
  const removeAnswer = (a) =>
    setDraft({ ...draft, answers: draft.answers.filter((x) => x !== a) });
  const toggleAction = (id) => {
    const has = draft.actions.includes(id);
    const next = has ? draft.actions.filter((x) => x !== id) : [...draft.actions, id].slice(0, 4);
    setDraft({ ...draft, actions: next });
  };
  const setPostCall = (k, v) =>
    setDraft({ ...draft, post_call: { ...draft.post_call, [k]: v } });

  const save = async () => {
    setSaving(true); setErr(null);
    try {
      const payload = {
        summary:   draft.summary.trim(),
        answers:   draft.answers.map((a) => a.trim()).filter(Boolean),
        actions:   draft.actions.slice(0, 4),
        post_call: {
          email: !!draft.post_call.email,
          sms:   smsAllowed ? !!draft.post_call.sms : false,
        },
      };
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ purpose: payload }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        setErr(e.detail?.message || e.detail || `Failed (${r.status})`);
        return;
      }
      // Mutate the agent dict in place so the read-mode picks up the new
      // value without a full /api/agents round-trip — the rest of the
      // dashboard refreshes on its own cadence.
      agent.purpose = payload;
      setDraft(initial());
      // On the dedicated Core-purpose page (defaultEditing=true) we keep
      // the form open after save — the operator landed on this page to
      // edit, collapsing to read-only after every save adds an extra
      // click for every iterative tweak. Anywhere else (any future
      // embed where PurposeBox is one card among many) we still collapse
      // to read-mode so the save acts as visual confirmation.
      setEditing(defaultEditing);
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return html`
      <section class="db-purpose db-purpose-edit">
        <header class="db-purpose-head">
          <h3>Core purpose</h3>
          <span class="db-muted db-purpose-help">What ${agent.name} is built to do.</span>
        </header>
        <label class="db-purpose-field">
          <span>One-line summary</span>
          <input
            class="db-input"
            placeholder="e.g. Answer car questions and book test drives at Honda Andheri"
            value=${draft.summary}
            onInput=${(e) => setDraft({ ...draft, summary: e.target.value })}
          />
        </label>
        <label class="db-purpose-field">
          <span>What can ${agent.name} answer about? <em class="db-muted">(up to 8)</em></span>
          <div class="db-chip-editor">
            ${draft.answers.map((a) => html`
              <span class="db-chip" key=${a}>
                ${a}
                <button type="button" class="db-chip-x" onClick=${() => removeAnswer(a)}>×</button>
              </span>
            `)}
            <input
              class="db-chip-input"
              placeholder=${draft.answers.length ? "Add another…" : "e.g. Available models"}
              value=${chipInput}
              onInput=${(e) => setChipInput(e.target.value)}
              onKeyDown=${(e) => {
                if (e.key === "Enter") { e.preventDefault(); addAnswer(); }
                else if (e.key === "Backspace" && !chipInput && draft.answers.length) {
                  setDraft({ ...draft, answers: draft.answers.slice(0, -1) });
                }
              }}
            />
          </div>
        </label>
        <fieldset class="db-purpose-actions">
          <legend>What actions can ${agent.name} take? <em class="db-muted">(pick 2-4)</em></legend>
          ${ACTION_LIBRARY.map((a) => html`
            <label class=${"db-action-card" + (draft.actions.includes(a.id) ? " is-on" : "")} key=${a.id}>
              <input
                type="checkbox"
                checked=${draft.actions.includes(a.id)}
                onChange=${() => toggleAction(a.id)}
              />
              <div class="db-action-body">
                <div class="db-action-label">${a.label}</div>
                <div class="db-muted db-action-hint">${a.hint}</div>
              </div>
            </label>
          `)}
        </fieldset>
        <fieldset class="db-purpose-postcall">
          <legend>After every call</legend>
          <label>
            <input type="checkbox" checked=${draft.post_call.email}
                   onChange=${(e) => setPostCall("email", e.target.checked)} />
            Email summary to the operator
          </label>
          <label class=${smsAllowed ? "" : "db-muted"}>
            <input type="checkbox" checked=${draft.post_call.sms}
                   onChange=${(e) => setPostCall("sms", e.target.checked)}
                   disabled=${!smsAllowed} />
            SMS summary
            ${smsAllowed ? "" : html` <em class="db-muted">— paid plans only</em>`}
          </label>
        </fieldset>
        ${err ? html`<div class="db-error">${err}</div>` : null}
        <div class="db-purpose-actions-row">
          <button class="db-btn-ghost" onClick=${() => { setDraft(initial()); setEditing(false); }}>Cancel</button>
          <button class="db-btn-primary" disabled=${saving} onClick=${save}>
            ${saving ? "Saving…" : "Save purpose"}
          </button>
        </div>
      </section>
    `;
  }

  if (!hasPurpose) {
    return html`
      <section class="db-purpose db-purpose-empty">
        <header class="db-purpose-head">
          <h3>Core purpose</h3>
          <span class="db-muted db-purpose-help">${agent.name} doesn't have a defined purpose yet.</span>
        </header>
        <p class="db-muted">
          Tell ${agent.name} what to answer about and what actions to take —
          the agent stays on-mission, the call log knows what "success"
          looks like, and post-call summaries get the right context.
        </p>
        <button class="db-btn-primary" onClick=${() => setEditing(true)}>
          Define purpose
        </button>
      </section>
    `;
  }

  const activeActions = (purpose.actions || [])
    .map((id) => ACTION_LIBRARY.find((x) => x.id === id))
    .filter(Boolean);

  return html`
    <section class="db-purpose">
      <header class="db-purpose-head">
        <h3>Core purpose</h3>
        <button class="db-btn-ghost db-btn-sm" onClick=${() => setEditing(true)}>Edit</button>
      </header>
      ${purpose.summary ? html`<p class="db-purpose-summary">${purpose.summary}</p>` : null}

      ${(purpose.answers || []).length ? html`
        <div class="db-purpose-group">
          <div class="db-purpose-group-label">Can answer about</div>
          <div class="db-purpose-chips">
            ${purpose.answers.map((a) => html`<span class="db-chip" key=${a}>${a}</span>`)}
          </div>
        </div>
      ` : null}

      ${activeActions.length ? html`
        <div class="db-purpose-group">
          <div class="db-purpose-group-label">Active actions</div>
          <ul class="db-purpose-actions-list">
            ${activeActions.map((a) => html`
              <li key=${a.id}>
                <span class="db-purpose-action-label">${a.label}</span>
                <span class="db-muted">${a.hint}</span>
              </li>
            `)}
          </ul>
        </div>
      ` : null}

      ${(purpose.post_call?.email || purpose.post_call?.sms) ? html`
        <div class="db-purpose-group">
          <div class="db-purpose-group-label">After every call</div>
          <div class="db-purpose-chips">
            ${purpose.post_call.email ? html`<span class="db-chip db-chip-soft">Email summary</span>` : null}
            ${purpose.post_call.sms ? html`
              <span class=${"db-chip " + (smsAllowed ? "db-chip-soft" : "db-chip-disabled")}>
                SMS ${smsAllowed ? "" : "(paid plans only)"}
              </span>
            ` : null}
          </div>
        </div>
      ` : null}
    </section>
  `;
}

// AgentPurposePage — Core purpose gets its own dashboard route.
// Previously the PurposeBox lived only on the Overview, which made it
// easy to miss in a sea of next-step CTAs and stats tiles. The Core
// Purpose IS the agent's reason for existing — what she can answer,
// what she'll do for callers, what gets emailed after the call. It
// deserves a dedicated surface with the standard dashboard chrome.
//
// We just wrap the existing PurposeBox in the standard DashboardShell
// so it gets the sidebar + topbar + title row, and so the existing
// PATCH-driven edit flow keeps working untouched. PurposeBox owns its
// own read/edit toggle internally — this page just provides the frame.
function AgentPurposePage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  // PurposeBox mutates agent.purpose in place after a successful save,
  // but we also call refreshAgent so the agent dict in the parent
  // refreshes from the server (keeps the sidebar's published / draft
  // badge accurate, and the topbar's agent summary up to date).
  useEffect(() => { /* refresh on mount no-op — PurposeBox triggers it after save */ }, []);
  // Open the edit form straight away — the operator clicked into a
  // page literally titled "Core purpose"; making them click another
  // "Edit" button to start editing is friction. PurposeBox respects
  // `defaultEditing` for both initial render AND post-save state so
  // the form stays open through iterative tweaks.
  const body = html`
    <div class="db-overview">
      <${PurposeBox} agent=${agent} plan=${plan} defaultEditing=${true} />
    </div>
  `;
  return html`
    <${DashboardShell}
      activeKey="purpose"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Core purpose"
      subtitle=${`What ${agent.name || "your agent"} is built to do — captured by Eva, editable here.`}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Resolve the industry-adaptive Additional Info group list for an agent
// from the server-provided presets, keyed by the agent's sector (with
// the same alias fallback the backend uses).
function resolveInfoGroups(presets, sector, agentGroups) {
  // A catch-all / dynamic agent carries its OWN best-model-generated schema —
  // that always wins over the static per-sector map.
  if (Array.isArray(agentGroups) && agentGroups.length) return agentGroups;
  const map = (presets && presets.info_groups) || {};
  const aliases = (presets && presets.info_sector_aliases) || {};
  const s = String(sector || "").toLowerCase();
  if (map[s]) return map[s];
  if (aliases[s] && map[aliases[s]]) return map[aliases[s]];
  return map.generic || [];
}

// AgentExtraInfoPage — the "Additional Info" surface. Renders a set of
// accordion field-groups that ADAPT to the agent's industry (a dental
// agent sees Treatments/Doctors/Insurance; a restaurant sees Menu
// Highlights/Daily Specials/Seating; etc.). Each group expands to a
// free-text editor. Saved into agent.extra_info and folded into the
// live-call prompt's REFERENCE INFO section so the agent answers
// callers using it.
function AgentExtraInfoPage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  // `override` holds a freshly regenerated schema so the UI updates instantly
  // (the agent prop refreshes a beat later via refreshAgent).
  const [override, setOverride] = useState(null);
  const groups = override?.groups || resolveInfoGroups(presets, agent.sector, agent.info_groups);
  const [draft, setDraft] = useState(() => ({ ...(agent.extra_info || {}) }));
  const [openId, setOpenId] = useState(groups[0]?.id || null);
  const [state, setState] = useState({ msg: "", cls: "" });
  // Impact-confirmation popup for "Redesign sections" (a destructive change).
  const [regenOpen, setRegenOpen] = useState(false);
  const [regenBusy, setRegenBusy] = useState(false);
  const set = (id, v) => setDraft((d) => ({ ...d, [id]: v }));
  const toggle = (id) => setOpenId((cur) => (cur === id ? null : id));
  const filledCount = groups.filter((g) => (draft[g.id] || "").trim()).length;

  const regenerate = async () => {
    setRegenBusy(true);
    try {
      const r = await fetch(`/api/agents/${agent.id}/regenerate-info-groups`, { method: "POST" });
      if (!r.ok) throw new Error("server " + r.status);
      const data = await r.json();
      if (data && Array.isArray(data.info_groups) && data.info_groups.length) {
        setOverride({ groups: data.info_groups });
        setDraft({ ...(data.extra_info || {}) });
        setOpenId(data.info_groups[0]?.id || null);
        setState({ msg: "Sections redesigned ✓", cls: "ok" });
        refreshAgent && refreshAgent();
        setTimeout(() => setState({ msg: "", cls: "" }), 2600);
      } else {
        throw new Error("no sections");
      }
      setRegenOpen(false);
    } catch {
      setState({ msg: "Couldn't redesign — try again", cls: "err" });
      setRegenOpen(false);
      setTimeout(() => setState({ msg: "", cls: "" }), 3000);
    } finally {
      setRegenBusy(false);
    }
  };

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    // Drop empty groups so extra_info stays tidy.
    const clean = {};
    for (const g of groups) {
      const v = (draft[g.id] || "").trim();
      if (v) clean[g.id] = v;
    }
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ extra_info: clean }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  const body = html`
    <div class="db-overview">
      <p class="db-panel-sub" style=${{ marginBottom: "10px" }}>
        Extra business knowledge for <b>${agent.name}</b>, tailored to your
        industry. Anything you add here, ${agent.name} can use to answer
        callers live. Tap a section to fill it in — all optional.
      </p>
      <div class="db-whatgoeswhere">
        <span class="db-whatgoeswhere-label">Where things live:</span>
        <span><b>Business profile</b> — hours, location, contact</span>
        <span><b>Core purpose</b> — what ${pronouns(agent).subj}'s built to do</span>
        <span><b>Additional info</b> (here) — detailed knowledge ${pronouns(agent).subj} answers with</span>
        <span><b>Knowledge base</b> — freeform notes & source links</span>
      </div>
      <div class="db-info-groups">
        ${groups.map((g) => {
          const open = openId === g.id;
          const val = draft[g.id] || "";
          const filled = !!val.trim();
          return html`
            <div class=${"db-info-group" + (open ? " is-open" : "")} key=${g.id}>
              <button type="button" class="db-info-group-head" onClick=${() => toggle(g.id)}>
                <span class="db-info-group-icon" aria-hidden="true">${g.emoji || "•"}</span>
                <div class="db-info-group-meta">
                  <div class="db-info-group-title">
                    ${g.label}
                    <span class=${"db-info-group-count" + (filled ? " is-filled" : "")}>
                      ${filled ? "✓" : "(0)"}
                    </span>
                  </div>
                  <div class="db-info-group-desc">${g.desc}</div>
                  ${g.info_only ? html`<div class="db-info-group-note">For reference only — doesn't drive bookings.</div>` : ""}
                </div>
                <svg class="db-info-group-chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
              </button>
              ${open ? html`
                <div class="db-info-group-body">
                  <${MarkdownEditor}
                    value=${val}
                    onChange=${(v) => set(g.id, v)}
                    rows=${5}
                    compact=${true}
                    placeholder=${"Add " + g.label.toLowerCase() + " — " + g.desc.toLowerCase() + "."} />
                </div>
              ` : ""}
            </div>
          `;
        })}
      </div>
    </div>
  `;

  const actions = html`
    <span class="db-info-progress">${filledCount} of ${groups.length} filled</span>
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-ghost" type="button" onClick=${() => setRegenOpen(true)} title="Redesign these sections to match the agent's current purpose">
      <span aria-hidden="true">✨</span> Redesign sections
    </button>
    <button class="db-btn-primary" onClick=${save}>Save changes</button>
  `;

  // Impact confirmation — shown BEFORE any regeneration so the operator
  // understands exactly what changes (and what's preserved).
  const impactModal = regenOpen ? html`
    <div class="db-modal-backdrop" onClick=${() => !regenBusy && setRegenOpen(false)}>
      <div class="db-modal" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>Redesign Additional Info?</h2>
          <button class="db-modal-close" type="button" onClick=${() => !regenBusy && setRegenOpen(false)} aria-label="Close">×</button>
        </header>
        <div class="db-modal-body">
          <p class="db-modal-lead">
            Eva will redesign <b>${agent.name}</b>'s Additional Info sections to match its
            current purpose, using the best model. Here's what changes:
          </p>
          <ul class="db-impact-list">
            <li><span class="db-impact-ic" aria-hidden="true">🔄</span> Replaces the current <b>${groups.length}</b> section${groups.length === 1 ? "" : "s"} with a fresh, tailored set.</li>
            <li><span class="db-impact-ic" aria-hidden="true">📝</span> ${filledCount > 0
                ? html`Your <b>${filledCount}</b> filled section${filledCount === 1 ? "" : "s"} — Eva carries the notes into the new sections, but may reorganise or reword them. Give them a quick review after.`
                : html`No sections are filled yet, so nothing is lost.`}</li>
            <li><span class="db-impact-ic" aria-hidden="true">🔒</span> Doesn't touch the business profile, persona, system prompt, voice, or call settings.</li>
            <li><span class="db-impact-ic" aria-hidden="true">↩️</span> This can't be auto-undone — you'd re-edit the sections to change them back.</li>
          </ul>
        </div>
        <div class="db-modal-foot">
          <button class="db-modal-btn ghost" type="button" onClick=${() => setRegenOpen(false)} disabled=${regenBusy}>Cancel</button>
          <button class="db-modal-btn primary" type="button" onClick=${regenerate} disabled=${regenBusy}>
            ${regenBusy ? "Redesigning…" : "Redesign sections"}
          </button>
        </div>
      </div>
    </div>
  ` : "";

  return html`
    <${DashboardShell}
      activeKey="extra-info"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Additional Info"
      subtitle=${`${agent.name} · ${(presets?.sectors || []).find((s) => s.id === agent.sector)?.label || agent.sector || "Business"}`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
    ${impactModal}
  `;
}

function AgentOverviewPage({ agent, agents, presets, plan, stats, onTest, onGoLive, onEdit, onTestPhone, onNav }) {
  const labelFor = (list, id) => (list || []).find((x) => x.id === id)?.label || _prettifyEnumId(id);
  const sectorLabel = labelFor(presets?.sectors, agent.sector);
  const localeLabel = labelFor(presets?.locales, agent.locale);
  const greeting = (agent.greeting || "").trim();
  const tagline = (agent.persona || "").trim() || (agent.system_prompt || "").split(/[.!?]/)[0] || `${sectorLabel || "Phone agent"} · ${localeLabel || ""}`;

  // Recent calls preview — latest 3 from the call log. Lightweight glance,
  // not a full table (the Call logs page has that).
  const [recentCalls, setRecentCalls] = useState([]);
  useEffect(() => {
    if (!agent?.id) return;
    fetch(`/api/agents/${agent.id}/calls?limit=3`)
      .then((r) => r.ok ? r.json() : [])
      .then((arr) => setRecentCalls(Array.isArray(arr) ? arr : []))
      .catch(() => {});
  }, [agent?.id]);
  // Outcomes summary for the Overview tiles — sector × locale × user-input
  // matrix from the report endpoint. Replaces the old hard-coded
  // "Booked / converted" + "Escalated" tiles which were restaurant-shaped
  // labels imposed on every industry (a dental clinic doesn't "book / convert",
  // an automotive agent doesn't "escalate"). Now the tiles reflect what THIS
  // agent's catalogue actually measures.
  const [outcomesReport, setOutcomesReport] = useState(null);
  useEffect(() => {
    if (!agent?.id) return;
    fetch(`/api/agents/${agent.id}/outcomes/report?days=30`)
      .then((r) => r.ok ? r.json() : null)
      .then((d) => setOutcomesReport(d || null))
      .catch(() => {});
  }, [agent?.id]);
  const ocSlug = agent.slug || agent.id;
  const ocGoToCalls = (qs) => onNav && onNav(`/agent/${ocSlug}/calls${qs ? `?${qs}` : ""}`);
  const ocGoToOutcomes = () => onNav && onNav(`/agent/${ocSlug}/outcomes`);
  const totalOC = outcomesReport?.total_calls ?? (stats?.total ?? 0);
  const topOutcome = (outcomesReport?.outcomes || []).filter((o) => o.count > 0).sort((a,b) => b.count - a.count)[0] || null;
  const purposeKpi = outcomesReport?.purpose?.has_purpose ? outcomesReport.purpose : null;
  // Headline KPI: purpose conversion if a purpose is set, else weighted
  // success rate. Both are sector-agnostic but purpose-aware when available.
  const headlineKpi = purposeKpi
    ? { label: "Purpose conversion", value: `${purposeKpi.conversion_rate || 0}%`, sub: `${purposeKpi.primary_count || 0} of ${totalOC} aligned`, clickable: true }
    : { label: "Weighted success",   value: `${outcomesReport?.success_rate || 0}%`, sub: outcomesReport?.weights_overridden ? "your custom weights" : "default weights", clickable: true };
  const fmtDur = (s) => {
    if (!s) return "—";
    const v = Number(s);
    if (!Number.isFinite(v) || v <= 0) return "—";
    if (v < 60) return `${Math.round(v)}s`;
    const m = Math.floor(v / 60); const ss = Math.round(v - m * 60);
    return `${m}m ${ss}s`;
  };
  const avgDur = (() => {
    const totalsMin = outcomesReport?.totals?.minutes;
    if (totalsMin && totalOC > 0) return fmtDur((totalsMin / totalOC) * 60);
    return stats?.avg_duration_s ? fmtDur(stats.avg_duration_s) : "—";
  })();

  // "Edit details" now opens the dedicated Business profile page — that's
  // where the full structured form (location, hours, sector-adaptive fields,
  // offers) lives. The legacy drawer is gone.
  const onEditProfile = () => onNav && onNav(`/agent/${agent.slug || agent.id}/profile`);
  const actions = html`
    <button class="db-btn-ghost" onClick=${onEditProfile}>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 21h4l11-11-4-4L4 17z"/><path d="M15 6l3 3"/></svg>
      <span>Edit details</span>
    </button>
  `;

  // "Next steps" panel — persistent helper that sits next to the agent card.
  // Each step has a done-state derived from the agent's actual state, so the
  // checklist keeps pace with what the user has actually accomplished.
  const isFresh = (() => {
    if (!agent.created_at) return false;
    const iso = agent.created_at.includes("T") ? agent.created_at :
                (agent.created_at.replace(" ", "T") + (agent.created_at.match(/[Z+-]\d{2}:?\d{2}$/) ? "" : "Z"));
    const ageMs = Date.now() - new Date(iso).getTime();
    return ageMs >= 0 && ageMs < 24 * 60 * 60 * 1000;
  })();
  const testDone = (stats?.total || 0) > 0;
  const knowledgeDone = (agent.system_prompt || "").trim().length >= 400;
  // Additional Info is "done" once the operator has filled at least one
  // industry-adaptive group. Starts empty for every agent, so without a
  // next-step nudge operators never discover it — and the live-call
  // REFERENCE INFO benefit never lands. Surfaced as a checklist step.
  const extraInfoDone = (() => {
    const ei = agent.extra_info;
    if (!ei || typeof ei !== "object") return false;
    return Object.values(ei).some((v) => typeof v === "string" && v.trim());
  })();
  const slug = agent.slug || agent.id;

  // Check live-state once: any fulfilled number-request marks Go-live as done.
  const [liveDone, setLiveDone] = useState(false);
  useEffect(() => {
    if (!agent?.id) return;
    fetch(`/api/agents/${agent.id}/number-requests`)
      .then((r) => r.ok ? r.json() : [])
      .then((arr) => {
        const ok = Array.isArray(arr) && arr.some((n) => (n.status || "").toLowerCase() === "fulfilled" || (n.status || "").toLowerCase() === "live");
        setLiveDone(!!ok);
      })
      .catch(() => {});
  }, [agent?.id]);

  const steps = [
    { key: "test",       title: "Test in your browser",  sub: "10-second sanity check — no phone needed.",                              done: testDone,       onClick: onTest,                                                cta: testDone ? "Test again" : "Test →",  primary: !testDone },
    { key: "extra-info", title: "Add business details",  sub: `Menu, pricing, policies — what ${agent.name} answers callers with.`,     done: extraInfoDone,  onClick: () => onNav && onNav(`/agent/${slug}/extra-info`),     cta: extraInfoDone ? "Edit" : "Open →",   primary: !extraInfoDone && testDone },
    { key: "live",       title: "Go live",               sub: "Get a real phone number, or embed the bubble on your site.",            done: liveDone,       onClick: () => onNav && onNav(`/agent/${slug}/go-live`),         cta: liveDone ? "Manage" : "Open →",      primary: !liveDone && testDone && extraInfoDone },
  ];

  const body = html`
    <div class="db-overview">
      <div class="db-overview-top">
        <section class="db-hero">
          <div class="db-hero-thumb"></div>
          <div class="db-hero-body">
            <div class="db-hero-eyebrow">
              <span>Phone AI agent</span>
              <span class="db-hero-mode" title="Answers inbound calls. Outbound launches later — when it does, you'll be able to flip the mode here.">
                <svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M22 12l-4-4v3H4v2h14v3z" transform="rotate(180 12 12)"/></svg>
                Inbound calls
              </span>
            </div>
            <h2 class="db-hero-name">${agent.name}</h2>
            <p class="db-hero-tagline">${tagline.slice(0, 160)}</p>
            <div class="db-hero-pills">
              ${sectorLabel ? html`<span class="db-pill">${sectorLabel}</span>` : ""}
              ${localeLabel ? html`<span class="db-pill">${localeLabel}</span>` : ""}
              ${agent.voice ? html`<span class="db-pill db-pill-accent">${voiceTag(agent.voice)}</span>` : ""}
            </div>
            ${greeting ? html`<div class="db-hero-greeting">"${greeting}"</div>` : ""}
            <div class="db-hero-cta">
              <button type="button" class="db-btn-primary db-hero-test" onClick=${onTest}>
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
                <span>Test ${agent.name} now</span>
              </button>
              <span class="db-hero-cta-hint">Web call · no phone needed</span>
            </div>
          </div>
        </section>

        <aside class="db-next-steps">
          <header class="db-next-steps-head">
            <h3 class="db-next-steps-title">Next steps</h3>
            ${isFresh ? html`<span class="db-next-steps-fresh"><span aria-hidden="true">✨</span> New</span>` : ""}
          </header>
          <ol class="db-next-steps-list">
            ${steps.map((s, i) => html`
              <li class=${"db-next-step" + (s.done ? " done" : "")} key=${s.key}>
                <span class="db-next-step-num">${s.done ? html`<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L20 7"/></svg>` : i + 1}</span>
                <div class="db-next-step-body">
                  <div class="db-next-step-title">${s.title}</div>
                  <div class="db-next-step-sub">${s.sub}</div>
                </div>
                <button type="button"
                        class=${"db-btn-sm " + (s.primary ? "db-btn-primary" : "db-btn-ghost")}
                        onClick=${s.onClick}>
                  ${s.cta}
                </button>
              </li>
            `)}
          </ol>
        </aside>
      </div>

      <!--
        Core purpose moved to its own dashboard page (/agent/<slug>/purpose).
        Previously rendered inline here via <PurposeBox/>, but it deserved a
        dedicated surface — discoverable from the sidebar, deep-linkable,
        not crowded against stats tiles. The Overview now stays focused on
        "what's happening" (next steps + recent activity) rather than
        doubling as an editor.
      -->

      <!-- Headline — one warm sentence that tells the operator how the
           agent is doing RIGHT NOW. Adapts to: not-yet-live vs. live, no
           calls vs. some calls, weighted-success vs. purpose-conversion,
           and surfaces the top sector-specific outcome by name. Replaces
           the stat tiles being the only narrative on the page. -->
      ${(() => {
        const name = agent.name || "Your agent";
        const live = !!agent.published;
        // pronouns() picks she/he/they based on agent.variables.gender or
        // (fallback) the chosen voice. Lets a male-named agent like Rohan
        // be referred to as "his" instead of "her" in every UI string.
        const pn = pronouns(agent);
        let title, sub;
        if (totalOC === 0) {
          if (live) {
            title = `${name} is live and waiting for ${pn.poss} first caller.`;
            sub = "Once a call comes in, it'll show up below in Recent activity.";
          } else {
            title = `${name} is ready to take ${pn.poss} first call.`;
            sub = `Send yourself a test call to see ${pn.obj} in action, then publish when you're happy.`;
          }
        } else if (purposeKpi && purposeKpi.primary_count > 0) {
          const pct = purposeKpi.conversion_rate || 0;
          const moodTitle = pct >= 70 ? `${name} is having a strong month`
                          : pct >= 40 ? `${name} is steady this month`
                          :              `${name} is finding ${pn.poss} feet`;
          const primary = (purposeKpi.primary_outcomes || [])
            .filter((o) => o.count > 0).sort((a, b) => b.count - a.count)[0];
          if (primary) {
            title = `${moodTitle} — ${primary.count} ${primary.label.toLowerCase()} across ${totalOC} call${totalOC === 1 ? "" : "s"}.`;
            sub = `${pct}% of calls aligned with what ${pn.subj} ${pn.verb("was","were")} built for.`;
          } else {
            title = `${moodTitle}.`;
            sub = `${pct}% of ${totalOC} calls aligned with what ${pn.subj} ${pn.verb("was","were")} built for.`;
          }
        } else if (topOutcome) {
          title = `${name} handled ${totalOC} call${totalOC === 1 ? "" : "s"} this month — top result was ${topOutcome.label.toLowerCase()}.`;
          sub = `${topOutcome.count} of ${totalOC} (${topOutcome.share}%). Set a Core purpose to track conversion against a goal.`;
        } else {
          title = `${name} handled ${totalOC} call${totalOC === 1 ? "" : "s"} in the last 30 days.`;
          sub = "";
        }
        return html`
          <section class="db-headline">
            <h2 class="db-headline-text">${title}</h2>
            ${sub ? html`<p class="db-headline-sub">${sub}</p>` : ""}
          </section>
        `;
      })()}

      <!-- Outcome short-summary tiles — sourced from the Call outcomes
           report (sector × locale × user-input matrix), not hard-coded
           labels. Clicking drills into the relevant view, just like the
           Call outcomes page tiles do. -->
      <section class="db-stats">
        <button type="button" class="db-stat db-stat-blue db-stat-click"
                onClick=${() => ocGoToCalls("")}
                title="See all calls in the last 30 days">
          <div class="db-stat-label">Total calls</div>
          <div class="db-stat-value">${totalOC}</div>
          <div class="db-stat-sub">last 30 days</div>
        </button>
        <button type="button" class="db-stat db-stat-green db-stat-click"
                onClick=${ocGoToOutcomes}
                title="Open the Call outcomes report">
          <div class="db-stat-label">${headlineKpi.label}</div>
          <div class="db-stat-value">${headlineKpi.value}</div>
          <div class="db-stat-sub">${headlineKpi.sub}</div>
        </button>
        ${topOutcome ? html`
          <button type="button" class="db-stat db-stat-pink db-stat-click"
                  onClick=${() => ocGoToCalls(`outcome=${encodeURIComponent(topOutcome.id)}`)}
                  title=${`See the ${topOutcome.count} calls where outcome = ${topOutcome.label}`}>
            <div class="db-stat-label">Top outcome</div>
            <div class="db-stat-value db-stat-value-sm">${topOutcome.label}</div>
            <div class="db-stat-sub">${topOutcome.count} call${topOutcome.count === 1 ? "" : "s"} · ${topOutcome.share}%</div>
          </button>
        ` : html`
          <div class="db-stat db-stat-pink">
            <div class="db-stat-label">Top outcome</div>
            <div class="db-stat-value">—</div>
            <div class="db-stat-sub">No calls yet</div>
          </div>
        `}
        <div class="db-stat db-stat-yellow">
          <div class="db-stat-label">Avg. duration</div>
          <div class="db-stat-value">${avgDur}</div>
          <div class="db-stat-sub">${totalOC > 0 ? `${totalOC} call${totalOC === 1 ? "" : "s"}` : "No calls yet"}</div>
        </div>
      </section>

      <!-- Recent activity — last 3 calls. Glanceable; the Call logs page has
           the full table + filters. Empty state nudges the user to the test
           call so the dashboard never feels dead. -->
      <section class="db-panel">
        <div class="db-panel-head">
          <div>
            <h3 class="db-panel-title">Recent activity</h3>
            <div class="db-panel-sub">${recentCalls.length === 0 ? "No calls yet" : `Last ${recentCalls.length} ${recentCalls.length === 1 ? "call" : "calls"}`}</div>
          </div>
          ${recentCalls.length > 0 ? html`
            <button class="db-btn-ghost db-btn-sm" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/calls`)}>See all →</button>
          ` : ""}
        </div>
        ${recentCalls.length === 0 ? html`
          <div class="db-recent-empty">
            <div class="db-recent-empty-sub">Nothing here yet — once ${agent.name} starts taking calls, you'll see the latest three at a glance.</div>
          </div>
        ` : html`
          <ul class="db-recent-list">
            ${recentCalls.map((c) => html`
              <li class="db-recent-item" key=${c.id}>
                <span class=${"db-tag " + (c.outcome === "booked" ? "db-tag-green" : c.outcome === "escalated" ? "db-tag-purple" : c.outcome === "lead" ? "db-tag-blue" : "db-tag-grey")}>${(c.outcome || "unknown").replace(/_/g, " ")}</span>
                <span class="db-recent-summary">${c.summary || c.reason || "—"}</span>
                <span class="db-recent-meta">${c.duration_s ? Math.round(c.duration_s) + "s" : "—"}</span>
              </li>
            `)}
          </ul>
        `}
      </section>
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="overview"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title=${agent.name}
      subtitle=${sectorLabel ? `${sectorLabel} · ${localeLabel || ""}` : "Phone AI agent"}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// OutcomeCatalogueEditor (build 213) — operator-facing CRUD over the
// resolved outcome catalogue. Behind a "Customise outcomes" toggle on
// the Call outcomes page. Lets the business:
//   • Rename a default outcome ("Test drive booked" → "Showroom visit
//     confirmed") — staff-shop language wins over the catalogue default.
//   • Reclassify a default outcome to a different KIND (success /
//     qualified / info / failure) — what's a "win" varies by business.
//   • Add a fully-custom outcome that isn't in any sector catalogue.
//   • Hide a default outcome that doesn't apply (a dental clinic
//     inheriting test_drive_booked from a bad template).
//
// All edits live in one JSONB blob `agents.outcome_overrides` —
// PATCHed via the existing /api/agents/{id} endpoint. The catalogue
// resolution server-side applies the overrides at read time, so the
// agent's runtime vocabulary, end_call validation, dashboard report,
// and EOD digest all see the same final list with one source of truth.
// ─────────────────────────────────────────────────────────────────────────
const _OC_KIND_OPTIONS = [
  { value: "success",   label: "🏆 Success",   help: "Primary KPI — agent fulfilled the call's purpose." },
  { value: "qualified", label: "📞 Qualified", help: "Useful but not the win — captured lead, scheduled callback." },
  { value: "info",      label: "💬 Info-only", help: "Informational — answered an FAQ, gave hours / price." },
  { value: "failure",   label: "⚠️  Failure",  help: "Unwanted result — abandoned, voicemail, complaint left open." },
];
function _slugifyOutcomeId(s) {
  return String(s || "")
    .trim().toLowerCase()
    .replace(/[^a-z0-9_\s-]+/g, "")
    .replace(/[\s-]+/g, "_")
    .slice(0, 60);
}

function OutcomeCatalogueEditor({ agent, outcomes, onSaved }) {
  // Local draft state — keyed by outcome id so we don't lose unsaved
  // edits when the parent re-renders the report.
  const initial = (agent.outcome_overrides && typeof agent.outcome_overrides === "object")
    ? agent.outcome_overrides
    : {};
  const [edited,  setEdited]  = useState(() => ({ ...(initial.edited  || {}) }));
  const [removed, setRemoved] = useState(() => new Set((initial.removed || [])));
  const [added,   setAdded]   = useState(() => [...(initial.added || [])]);
  const [adding,  setAdding]  = useState(false);
  const [newRow,  setNewRow]  = useState({ id: "", label: "", kind: "success", description: "" });
  const [busy,    setBusy]    = useState(false);
  const [msg,     setMsg]     = useState("");

  // What changed since the last save? Drives the visibility of the
  // sticky "Save / Discard" footer.
  const dirty = (
    Object.keys(edited).length !== Object.keys(initial.edited || {}).length ||
    JSON.stringify(edited) !== JSON.stringify(initial.edited || {}) ||
    removed.size !== (initial.removed || []).length ||
    [...removed].some((id) => !(initial.removed || []).includes(id)) ||
    added.length !== (initial.added || []).length ||
    JSON.stringify(added) !== JSON.stringify(initial.added || [])
  );

  // Per-row helpers — `outcome` is one row from the resolved catalogue.
  const isHidden = (id) => removed.has(id);
  const editedRow = (id) => edited[id] || {};
  const labelFor  = (o) => editedRow(o.id).label ?? o.label;
  const kindFor   = (o) => editedRow(o.id).kind  ?? o.kind;

  const setField = (id, key, val) => {
    setEdited((prev) => {
      const next = { ...prev, [id]: { ...(prev[id] || {}), [key]: val } };
      // If both label + kind match the original catalogue value, drop
      // the override entirely — keeps the blob small + the row's
      // "edited" badge stays accurate.
      const original = outcomes.find((o) => o.id === id) || {};
      const merged = next[id];
      if (
        (merged.label == null || merged.label === original.label) &&
        (merged.kind  == null || merged.kind  === original.kind) &&
        (merged.description == null || merged.description === original.description)
      ) {
        const { [id]: _, ...rest } = next;
        return rest;
      }
      return next;
    });
  };

  const toggleRemoved = (id) => {
    setRemoved((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const removeCustom = (id) => {
    setAdded((prev) => prev.filter((r) => r.id !== id));
  };

  const addCustomRow = () => {
    const id = _slugifyOutcomeId(newRow.id || newRow.label);
    if (!id || !newRow.label.trim()) {
      setMsg("Need an id and a label.");
      return;
    }
    if (outcomes.some((o) => o.id === id) || added.some((a) => a.id === id)) {
      setMsg(`"${id}" already exists — pick a different id.`);
      return;
    }
    setAdded((prev) => [...prev, {
      id, label: newRow.label.trim(),
      kind: newRow.kind,
      description: (newRow.description || "").trim(),
    }]);
    setNewRow({ id: "", label: "", kind: "success", description: "" });
    setAdding(false);
    setMsg("");
  };

  const save = async () => {
    setBusy(true); setMsg("");
    try {
      const blob = {
        edited,
        added,
        removed: [...removed],
      };
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome_overrides: blob }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setMsg("Saved ✓");
      onSaved && onSaved();
      setTimeout(() => setMsg(""), 1800);
    } catch (e) {
      setMsg("Couldn't save — try again.");
    } finally {
      setBusy(false);
    }
  };

  const discard = () => {
    setEdited({ ...(initial.edited || {}) });
    setRemoved(new Set(initial.removed || []));
    setAdded([...(initial.added || [])]);
    setMsg("");
  };

  const resetAll = async () => {
    if (busy) return;
    setBusy(true); setMsg("");
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome_overrides: null }),  // null → defaults
      });
      if (!r.ok) throw new Error("server " + r.status);
      setEdited({}); setRemoved(new Set()); setAdded([]);
      setMsg("Reset to defaults ✓");
      onSaved && onSaved();
      setTimeout(() => setMsg(""), 2000);
    } catch (e) {
      setMsg("Couldn't reset — try again.");
    } finally {
      setBusy(false);
    }
  };

  // Render a single editable row. Default (catalogue) rows can be
  // edited or hidden; custom rows can be fully edited or fully
  // removed. Edits land in `edited[id]`; hides go into `removed`.
  const renderRow = (o, isCustom) => {
    const hidden = !isCustom && isHidden(o.id);
    const isEdited = !isCustom && (
      editedRow(o.id).label != null || editedRow(o.id).kind != null
    );
    return html`
      <li key=${o.id} class=${"oc-edit-row" + (hidden ? " is-hidden" : "")}>
        <div class="oc-edit-row-top">
          <div class="oc-edit-id">
            <code>${o.id}</code>
            ${isCustom ? html`<span class="oc-edit-badge oc-edit-badge-custom">Added by you</span>` : ""}
            ${isEdited ? html`<span class="oc-edit-badge oc-edit-badge-edited">Edited</span>` : ""}
            ${hidden ? html`<span class="oc-edit-badge oc-edit-badge-hidden">Hidden</span>` : ""}
          </div>
          <div class="oc-edit-actions">
            ${isCustom
              ? html`<button class="oc-edit-trash" type="button" title="Remove this custom outcome"
                             onClick=${() => removeCustom(o.id)}>Remove</button>`
              : hidden
                ? html`<button class="oc-edit-restore" type="button" title="Restore this default outcome"
                               onClick=${() => toggleRemoved(o.id)}>Restore</button>`
                : html`<button class="oc-edit-hide" type="button" title="Hide this outcome from the agent's vocabulary"
                               onClick=${() => toggleRemoved(o.id)}>Hide</button>`}
          </div>
        </div>
        <div class="oc-edit-fields">
          <label class="oc-edit-field">
            <span class="oc-edit-flabel">Label</span>
            <input class="db-input" type="text" maxlength="80"
                   value=${labelFor(o)}
                   disabled=${hidden}
                   onInput=${(e) => isCustom
                     ? setAdded((prev) => prev.map((r) => r.id === o.id ? { ...r, label: e.target.value } : r))
                     : setField(o.id, "label", e.target.value)} />
          </label>
          <label class="oc-edit-field">
            <span class="oc-edit-flabel">Kind</span>
            <select class="db-input"
                    value=${kindFor(o)}
                    disabled=${hidden}
                    onChange=${(e) => isCustom
                      ? setAdded((prev) => prev.map((r) => r.id === o.id ? { ...r, kind: e.target.value } : r))
                      : setField(o.id, "kind", e.target.value)}>
              ${_OC_KIND_OPTIONS.map((k) => html`
                <option key=${k.value} value=${k.value}>${k.label}</option>
              `)}
            </select>
          </label>
        </div>
      </li>
    `;
  };

  // Custom rows are appended at the bottom; the resolved `outcomes`
  // list already includes them (with is_custom: true) on subsequent
  // loads, but UNSAVED added rows live only in the local `added`
  // array until save. Render both, deduping by id.
  const sawIds = new Set(outcomes.map((o) => o.id));
  const unsavedAdds = added.filter((a) => !sawIds.has(a.id));

  return html`
    <section class="db-panel oc-edit-panel">
      <div class="oc-drawer-head">
        <h3 class="db-panel-title">Customise outcomes <span class="db-panel-pill">${outcomes.length + unsavedAdds.length}</span></h3>
        <p class="db-panel-sub">
          Rename what doesn't sound like how your staff talk, reclassify what counts
          as a "win" for your business, add custom outcomes the catalogue missed,
          or hide ones that don't apply. ${agent.sector || "This agent"} × ${agent.locale || "your locale"}.
        </p>
      </div>

      <ul class="oc-edit-list">
        ${outcomes.map((o) => renderRow(o, !!o.is_custom))}
        ${unsavedAdds.map((o) => renderRow(o, true))}
      </ul>

      ${adding ? html`
        <div class="oc-edit-addcard">
          <div class="oc-edit-addcard-title">New custom outcome</div>
          <div class="oc-edit-fields">
            <label class="oc-edit-field">
              <span class="oc-edit-flabel">Label (what callers see internally)</span>
              <input class="db-input" type="text" maxlength="80"
                     value=${newRow.label}
                     placeholder="e.g. Insurance docs collected"
                     onInput=${(e) => setNewRow((r) => ({ ...r, label: e.target.value, id: r.id || _slugifyOutcomeId(e.target.value) }))} />
            </label>
            <label class="oc-edit-field">
              <span class="oc-edit-flabel">Kind</span>
              <select class="db-input" value=${newRow.kind}
                      onChange=${(e) => setNewRow((r) => ({ ...r, kind: e.target.value }))}>
                ${_OC_KIND_OPTIONS.map((k) => html`<option key=${k.value} value=${k.value}>${k.label}</option>`)}
              </select>
            </label>
          </div>
          <label class="oc-edit-field">
            <span class="oc-edit-flabel">id (auto from label — edit if you want)</span>
            <input class="db-input db-mono" type="text" maxlength="60"
                   value=${newRow.id}
                   placeholder="e.g. insurance_docs_collected"
                   onInput=${(e) => setNewRow((r) => ({ ...r, id: _slugifyOutcomeId(e.target.value) }))} />
          </label>
          <label class="oc-edit-field">
            <span class="oc-edit-flabel">Description (optional — helps the agent decide when to log it)</span>
            <textarea class="db-input" rows="2" maxlength="280"
                      value=${newRow.description}
                      placeholder="When the caller agreed to upload their insurance card photo before we hung up."
                      onInput=${(e) => setNewRow((r) => ({ ...r, description: e.target.value }))}></textarea>
          </label>
          <div class="oc-edit-addcard-actions">
            <button class="db-btn-primary db-btn-sm" type="button" onClick=${addCustomRow}>Add outcome</button>
            <button class="db-btn-ghost db-btn-sm" type="button"
                    onClick=${() => { setAdding(false); setNewRow({ id: "", label: "", kind: "success", description: "" }); }}>Cancel</button>
          </div>
        </div>
      ` : html`
        <div class="oc-edit-addrow">
          <button class="db-btn-ghost db-btn-sm oc-edit-add" type="button"
                  onClick=${() => setAdding(true)}>+ Add a custom outcome</button>
          <button class="db-btn-ghost db-btn-sm oc-edit-reset" type="button"
                  onClick=${resetAll} disabled=${busy}>Reset all to defaults</button>
        </div>
      `}

      ${dirty || msg ? html`
        <div class="oc-edit-footer">
          <div class=${"oc-edit-msg" + (msg.startsWith("Couldn't") || msg.startsWith("Need") || msg.includes("already") ? " is-err" : " is-ok")}>
            ${msg || "You have unsaved changes."}
          </div>
          <div>
            <button class="db-btn-ghost db-btn-sm" type="button" onClick=${discard} disabled=${busy}>Discard</button>
            <button class="db-btn-primary db-btn-sm" type="button" onClick=${save} disabled=${busy || !dirty}>
              ${busy ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
      ` : ""}
    </section>
  `;
}

// AgentCallOutcomesPage — /agent/<slug>/outcomes. The "results" page that
// answers "how well is this agent doing?". Call logs is WHAT happened on
// each call; Call outcomes is what was the RESULT, aggregated and bucketed.
//
// Data comes from /api/agents/<id>/outcomes/report, which joins the per-agent
// catalogue (industry × locale × user-input matrix) with the rollup table
// and returns a weighted success rate + per-kind totals + the full per-
// outcome breakdown. The catalogue card at the bottom lists what THIS agent
// can log — so the operator sees the matrix that drives the page.
function AgentCallOutcomesPage({ agent, agents, presets, plan, onNav }) {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState(null);
  const [err, setErr] = useState("");
  const [catalogueOpen, setCatalogueOpen] = useState(false);
  // Operator-editable success weights — defaults live server-side, the
  // dashboard lets the business override them per agent. Draft state is
  // seeded from the report (which echoes the effective weights). PATCH
  // persists onto agents.outcome_weights JSONB, then re-fetches.
  const [weightsOpen, setWeightsOpen] = useState(false);
  const [weightsDraft, setWeightsDraft] = useState(null);
  const [weightsBusy, setWeightsBusy] = useState(false);
  const [weightsMsg, setWeightsMsg] = useState("");

  const loadReport = () => {
    let cancelled = false;
    setReport(null); setErr("");
    fetch(`/api/agents/${agent.id}/outcomes/report?days=${days}`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then((d) => { if (!cancelled) setReport(d); })
      .catch((e) => { if (!cancelled) setErr(String(e.message || e)); });
    return () => { cancelled = true; };
  };
  useEffect(loadReport, [agent?.id, days]);

  // Seed the draft from the report's effective weights when it loads, so the
  // sliders show what's currently applied (custom or default).
  useEffect(() => {
    if (report && !weightsDraft) {
      setWeightsDraft({ ...(report.weights || report.default_weights || { success: 1, qualified: 0.5, info: 0.2, failure: 0 }) });
    }
  }, [report]);

  const setWeight = (k, v) => setWeightsDraft((d) => ({ ...(d || {}), [k]: v }));
  const saveWeights = async () => {
    if (!weightsDraft) return;
    setWeightsBusy(true); setWeightsMsg("");
    try {
      const clean = {};
      ["success","qualified","info","failure"].forEach((k) => {
        const n = Number(weightsDraft[k]);
        if (Number.isFinite(n)) clean[k] = Math.max(0, Math.min(1, n));
      });
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome_weights: clean }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setWeightsMsg("Saved ✓");
      // Reload report so the KPI + per-outcome share pick up the new weights.
      const fresh = await fetch(`/api/agents/${agent.id}/outcomes/report?days=${days}`).then((x) => x.json());
      setReport(fresh);
      setTimeout(() => setWeightsMsg(""), 2200);
    } catch (e) {
      setWeightsMsg("Couldn't save — try again");
    } finally { setWeightsBusy(false); }
  };
  const resetWeights = async () => {
    setWeightsBusy(true); setWeightsMsg("");
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome_weights: null }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      const fresh = await fetch(`/api/agents/${agent.id}/outcomes/report?days=${days}`).then((x) => x.json());
      setReport(fresh);
      setWeightsDraft({ ...(fresh.default_weights || fresh.weights || {}) });
      setWeightsMsg("Reset to defaults ✓");
      setTimeout(() => setWeightsMsg(""), 2200);
    } catch (e) { setWeightsMsg("Couldn't reset — try again"); }
    finally { setWeightsBusy(false); }
  };

  const KIND_META = {
    success:   { label: "Success",    color: "#16a34a", icon: "🏆" },
    qualified: { label: "Qualified",  color: "#6366f1", icon: "📞" },
    info:      { label: "Info-only",  color: "#94a3b8", icon: "💬" },
    failure:   { label: "Failure",    color: "#dc2626", icon: "⚠️" },
  };
  const successRate = report?.success_rate ?? 0;
  const totalCalls = report?.total_calls ?? 0;
  const totalMinutes = Math.round((report?.totals?.minutes || 0));
  const byKind = report?.by_kind || { success: 0, qualified: 0, info: 0, failure: 0 };

  // Per-day series → tiny sparkline normalised to 0-100% height.
  const series = (report?.series || []).map((r) => ({
    day: r.day, calls: Number(r.calls || 0),
  }));
  const maxCalls = Math.max(1, ...series.map((s) => s.calls));

  const ranges = [
    { v: 7,   label: "7 days" },
    { v: 30,  label: "30 days" },
    { v: 90,  label: "90 days" },
    { v: 365, label: "1 year" },
  ];

  // Drill from an aggregate stat → the filtered Call log. The receiving
  // page reads ?outcome= / ?kind= and applies a client-side filter +
  // shows an active-filter banner with a Clear button.
  const slug = agent.slug || agent.id;
  const goToCalls = (qs) => onNav && onNav(`/agent/${slug}/calls${qs ? `?${qs}` : ""}`);
  const bestOutcome = (report?.outcomes || []).filter((o) => o.count > 0).sort((a,b)=>b.count-a.count)[0] || null;

  // ── Outcomes grouped by KIND ────────────────────────────────────────────
  // Cleaner read: one list, four sub-headers, no separate "kind grid". Zero-
  // count outcomes hide behind a quiet per-kind expander so the page stays
  // tight on the data that's actually happening.
  const [openEmpties, setOpenEmpties] = useState({});
  const grouped = ["success","qualified","info","failure"].map((k) => {
    const items = (report?.outcomes || []).filter((o) => o.kind === k);
    return { kind: k, items, count: byKind[k] || 0 };
  });
  // Trend / catalogue drawer state (weightsOpen already defined above).
  const [trendOpen, setTrendOpen] = useState(false);
  const catLen = (report?.outcomes || []).length;
  const orphans = report?.orphan_outcomes || [];

  // Composition bar — single horizontal bar showing the four kinds stacked
  // by share. Reads cleaner than four separate bars and ties the headline
  // success number directly to its underlying mix.
  const compShares = ["success","qualified","info","failure"].map((k) => ({
    kind: k,
    pct: totalCalls ? (byKind[k] / totalCalls * 100) : 0,
    color: KIND_META[k].color,
  }));

  const body = html`
    <div class="db-overview">
      <!-- Hero: weighted success + kind breakdown side by side. -->
      <div class="oc-hero">
        <button type="button" class="oc-hero-card oc-hero-success oc-clickable"
                disabled=${!totalCalls}
                onClick=${() => totalCalls && goToCalls("")}
                title=${totalCalls ? "See all calls in this range" : "No calls yet"}>
          <div class="oc-hero-label">Success rate</div>
          <div class="oc-hero-value">${successRate}<span class="oc-hero-unit">%</span></div>
          <div class="oc-hero-bar" aria-hidden="true">
            ${compShares.map((s, i) => s.pct > 0 ? html`<span key=${i} style=${{ width: `${s.pct}%`, background: s.color }}></span>` : "")}
          </div>
          <div class="oc-hero-foot">
            <span>${totalCalls} call${totalCalls === 1 ? "" : "s"} · ${totalMinutes} min talk · ${days}d</span>
          </div>
        </button>

        <div class="oc-hero-card oc-hero-kinds">
          <div class="oc-hero-label">By kind</div>
          <ul class="oc-kind-list">
            ${["success","qualified","info","failure"].map((k) => {
              const meta = KIND_META[k];
              const n = byKind[k] || 0;
              const share = totalCalls ? Math.round((n / totalCalls) * 100) : 0;
              const clickable = n > 0;
              return html`
                <li key=${k}>
                  <button type="button"
                          class=${"oc-kind-line oc-kind-line-" + k + (clickable ? " oc-clickable" : "")}
                          disabled=${!clickable}
                          title=${clickable ? `Open the ${meta.label.toLowerCase()} calls` : ""}
                          onClick=${() => clickable && goToCalls(`kind=${k}`)}>
                    <span class="oc-kind-dot" style=${{ background: meta.color }}></span>
                    <span class="oc-kind-name">${meta.label}</span>
                    <span class="oc-kind-line-count">${n}</span>
                    <span class="oc-kind-line-share">${share}%</span>
                  </button>
                </li>
              `;
            })}
          </ul>
        </div>
      </div>

      <!-- Purpose alignment — joins the agent's Core purpose to the
           outcomes report so the operator sees if the agent's actually
           doing the job it was built for. Hidden when no purpose is set. -->
      ${(report?.purpose?.has_purpose) ? html`
        <section class="db-panel oc-purpose">
          <div class="oc-purpose-head">
            <span class="oc-purpose-star" aria-hidden="true">⭐</span>
            <div class="oc-purpose-meta">
              <div class="oc-purpose-title">Core purpose</div>
              ${report.purpose.summary ? html`<p class="oc-purpose-summary">${report.purpose.summary}</p>` : ""}
            </div>
            <button class="oc-purpose-conv oc-clickable" type="button"
                    title=${`See the ${report.purpose.primary_count} purpose-aligned calls`}
                    disabled=${!report.purpose.primary_count}
                    onClick=${() => report.purpose.primary_count && goToCalls(report.purpose.primary_outcome_ids.map((id) => `outcome=${encodeURIComponent(id)}`).slice(0,1).join(""))}>
              <div class="oc-purpose-conv-num">${report.purpose.conversion_rate}<span>%</span></div>
              <div class="oc-purpose-conv-sub">${report.purpose.primary_count} of ${totalCalls || 0} aligned</div>
            </button>
          </div>
          ${(report.purpose.primary_outcomes || []).length > 0 ? html`
            <div class="oc-purpose-chips">
              <span class="oc-purpose-chip-label">Counts toward purpose:</span>
              ${report.purpose.primary_outcomes.map((o) => html`
                <button type="button" key=${o.id}
                        class=${"oc-purpose-chip" + (o.count > 0 ? " oc-clickable" : "")}
                        disabled=${o.count === 0}
                        onClick=${() => o.count > 0 && goToCalls(`outcome=${encodeURIComponent(o.id)}`)}
                        title=${o.description}>
                  <span class="oc-kind-dot" style=${{ background: KIND_META[o.kind].color }}></span>
                  <span>${o.label}</span>
                  <span class="oc-purpose-chip-count">${o.count}</span>
                </button>
              `)}
            </div>
          ` : ""}
        </section>
      ` : ((report?.purpose && report.purpose.has_purpose === false && agent?.purpose?.actions?.length) ? html`
        <div class="oc-purpose-empty">
          <span aria-hidden="true">⭐</span>
          <span>This agent's purpose is set, but none of its actions map to a tracked outcome for the <b>${agent.sector || "current"}</b> sector. Edit on <a href=${`/agent/${slug}/purpose`} onClick=${(e) => { e.preventDefault(); onNav && onNav(`/agent/${slug}/purpose`); }}>Core purpose</a>.</span>
        </div>
      ` : (report ? html`
        <div class="oc-purpose-empty">
          <span aria-hidden="true">⭐</span>
          <span>No <b>Core purpose</b> set yet. Define one on <a href=${`/agent/${slug}/purpose`} onClick=${(e) => { e.preventDefault(); onNav && onNav(`/agent/${slug}/purpose`); }}>Core purpose</a> to see conversion against what this agent was built for.</span>
        </div>
      ` : ""))}

      <!-- Outcomes — grouped by kind, with zero-count rows tucked away. -->
      <section class="db-panel oc-grouped">
        ${grouped.map(({ kind, items, count: kindCount }) => {
          if (items.length === 0) return "";
          const filled = items.filter((o) => o.count > 0);
          const empties = items.filter((o) => o.count === 0);
          const rowsToShow = openEmpties[kind] ? items : filled;
          const share = totalCalls ? Math.round(kindCount / totalCalls * 100) : 0;
          return html`
            <div key=${kind} class=${"oc-group oc-group-" + kind}>
              <div class="oc-group-head">
                <span class="oc-kind-dot" style=${{ background: KIND_META[kind].color }}></span>
                <span class="oc-group-name">${KIND_META[kind].label}</span>
                ${kindCount > 0
                  ? html`<span class="oc-group-meta">${kindCount} call${kindCount === 1 ? "" : "s"} · ${share}%</span>`
                  : html`<span class="oc-group-meta oc-group-meta-quiet">No calls in this kind</span>`}
              </div>
              ${rowsToShow.length === 0 ? html`<div class="oc-group-empty">All outcomes for this kind are zero right now.</div>` : html`
                <ul class="oc-rows-clean">
                  ${rowsToShow.map((o) => {
                    const clickable = o.count > 0;
                    return html`
                      <li key=${o.id}
                          class=${"oc-row-clean" + (clickable ? " oc-clickable" : " is-empty") + (o.is_primary ? " is-primary" : "")}
                          onClick=${clickable ? () => goToCalls(`outcome=${encodeURIComponent(o.id)}`) : null}
                          title=${o.is_primary ? `Primary — counts toward purpose. ${o.description || ""}` : (o.description || "")}>
                        <span class="oc-row-name">${o.is_primary ? html`<span class="oc-row-star" aria-hidden="true">⭐</span>` : ""}${o.label}</span>
                        <span class="oc-row-meter" aria-hidden="true">
                          <span class="oc-row-meter-fill" style=${{ width: `${Math.min(100, o.share)}%`, background: KIND_META[o.kind].color }}></span>
                        </span>
                        <span class="oc-row-count">${o.count}</span>
                        <span class="oc-row-share">${o.share}%</span>
                      </li>
                    `;
                  })}
                </ul>
              `}
              ${empties.length > 0 ? html`
                <button class="oc-group-more" type="button"
                        onClick=${() => setOpenEmpties((p) => ({ ...p, [kind]: !p[kind] }))}>
                  ${openEmpties[kind]
                    ? `Hide ${empties.length} empty outcome${empties.length === 1 ? "" : "s"}`
                    : `Show ${empties.length} empty outcome${empties.length === 1 ? "" : "s"}`}
                </button>
              ` : ""}
            </div>
          `;
        })}
        ${orphans.length > 0 ? html`
          <div class="oc-group oc-group-orphan">
            <div class="oc-group-head">
              <span class="oc-kind-dot" style=${{ background: "#b45309" }}></span>
              <span class="oc-group-name">Unrecognised outcomes</span>
              <span class="oc-group-meta">Logged but not in the sector catalogue</span>
            </div>
            <ul class="oc-rows-clean">
              ${orphans.map((o) => html`
                <li key=${o.id} class="oc-row-clean">
                  <span class="oc-row-name">${o.label}</span>
                  <span class="oc-row-meter"></span>
                  <span class="oc-row-count">${o.count}</span>
                  <span class="oc-row-share">${o.share}%</span>
                </li>
              `)}
            </ul>
          </div>
        ` : ""}
      </section>

      <!-- Tools row — quiet buttons that open inline drawers. "Customize
           weights" moved OUT of the primary row in build 186: the term
           "weights" is jargon most operators don't think in. Catalogue
           and trend stay because they're plain-English reads. -->
      <div class="oc-tools">
        <button class=${"oc-tool" + (catalogueOpen ? " is-open" : "")} type="button"
                onClick=${() => setCatalogueOpen((v) => !v)}>
          <span aria-hidden="true">📚</span>
          <span>${catalogueOpen ? "Hide outcomes editor" : `Customise outcomes (${catLen})`}</span>
        </button>
        ${series.length > 0 ? html`
          <button class=${"oc-tool" + (trendOpen ? " is-open" : "")} type="button"
                  onClick=${() => setTrendOpen((v) => !v)}>
            <span aria-hidden="true">📈</span>
            <span>${trendOpen ? "Hide trend" : "Daily trend"}</span>
          </button>
        ` : ""}
      </div>

      <!-- Weights drawer — advanced. Accessible only via the small
           "Advanced" link at the bottom of the page (build 186). The
           copy was rewritten to plain English: "How calls count" instead
           of "weights", with a one-liner explaining what changes when
           you slide. -->
      ${weightsOpen && weightsDraft ? html`
        <section class="db-panel oc-drawer">
          <div class="oc-drawer-head">
            <h3 class="db-panel-title">How calls count toward your success rate ${report?.weights_overridden ? html`<span class="db-panel-pill">Customized</span>` : html`<span class="db-panel-pill">Default</span>`}</h3>
            <p class="db-panel-sub">By default, every <b>Success</b> call counts as a full win, a <b>Qualified</b> lead counts as half a win, an <b>Info-only</b> call as a fifth, and a <b>Failure</b> as zero. Slide to change how much each kind contributes to the headline rate. Most operators never need to touch this.</p>
          </div>
          <div class="oc-weights-grid">
            ${["success","qualified","info","failure"].map((k) => {
              const meta = KIND_META[k];
              const dflt = (report?.default_weights || {})[k];
              const v = Number.isFinite(weightsDraft[k]) ? weightsDraft[k] : (dflt ?? 0);
              return html`
                <div key=${k} class="oc-weight-card">
                  <div class="oc-weight-head">
                    <span class="oc-kind-dot" style=${{ background: meta.color }}></span>
                    <span class="oc-weight-label">${meta.label}</span>
                  </div>
                  <div class="oc-weight-controls">
                    <input class="oc-weight-slider" type="range" min="0" max="1" step="0.05"
                           value=${v}
                           onInput=${(e) => setWeight(k, parseFloat(e.target.value))} />
                    <input class="oc-weight-num" type="number" min="0" max="1" step="0.05"
                           value=${v.toFixed ? v.toFixed(2) : v}
                           onInput=${(e) => setWeight(k, parseFloat(e.target.value))} />
                  </div>
                  <div class="oc-weight-default">Default: ${(dflt ?? 0) === 1 ? "full win" : (dflt ?? 0) === 0.5 ? "half a win" : (dflt ?? 0) === 0.2 ? "a fifth" : "zero"}</div>
                </div>
              `;
            })}
          </div>
          <div class="oc-weights-foot">
            <div class="oc-weights-help">Headline % = a weighted average across all calls.</div>
            <div class="oc-weights-actions">
              ${weightsMsg ? html`<span class="oc-weights-msg">${weightsMsg}</span>` : ""}
              <button class="db-btn-ghost" type="button" onClick=${resetWeights} disabled=${weightsBusy}>Reset to default</button>
              <button class="db-btn-primary" type="button" onClick=${saveWeights} disabled=${weightsBusy}>${weightsBusy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        </section>
      ` : ""}

      <!-- Catalogue drawer + editor (build 213) -->
      ${catalogueOpen && catLen > 0 ? html`
        <${OutcomeCatalogueEditor}
          agent=${agent}
          outcomes=${report.outcomes || []}
          onSaved=${loadReport} />
      ` : ""}

      <!-- Daily trend drawer -->
      ${trendOpen && series.length > 0 ? html`
        <section class="db-panel oc-drawer">
          <div class="oc-drawer-head">
            <h3 class="db-panel-title">Daily volume <span class="db-panel-pill">last ${series.length} days</span></h3>
          </div>
          <div class="oc-spark">
            ${series.map((s, i) => html`
              <div key=${i} class="oc-spark-bar" title=${`${s.day}: ${s.calls} calls`}>
                <div class="oc-spark-fill" style=${{ height: `${Math.round((s.calls / maxCalls) * 100)}%` }}></div>
              </div>
            `)}
          </div>
        </section>
      ` : ""}

      ${err ? html`<p class="db-form-help" style=${{ color: "#b91c1c" }}>Couldn't load report: ${err}</p>` : ""}

      <!-- Quiet "Advanced" footer link — opens the weights drawer for
           operators who DO want to retune how each kind contributes to
           the headline. Build 186 moved this out of the primary tools
           row so the page stays focused on the four kinds. -->
      <div class="oc-advanced-foot">
        <button class="oc-advanced-link" type="button"
                onClick=${() => setWeightsOpen((v) => !v)}>
          ${weightsOpen ? "Hide advanced settings" : "Advanced — how each kind contributes to the score"}
          ${report?.weights_overridden && !weightsOpen ? html`<span class="oc-tool-dot" title="You've customised this"></span>` : ""}
        </button>
      </div>
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="outcomes"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Call outcomes"
      subtitle=${`What's the result of every call — sector ${agent.sector || "—"} · locale ${agent.locale || "—"}.`}
      actions=${html`
        <div class="oc-range">
          ${ranges.map((r) => html`
            <button key=${r.v} type="button"
                    class=${"oc-range-btn" + (days === r.v ? " is-active" : "")}
                    onClick=${() => setDays(r.v)}>${r.label}</button>
          `)}
        </div>
      `}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Call-log page — /agent/<slug>/calls. Stats tiles + table of calls.
// Aggregations are computed client-side from the existing /calls endpoint
// to avoid a new backend route; if the call volume grows past a few hundred
// per agent, we'll add a `/call-summary` endpoint that aggregates in SQL.
// ─────────────────────────────────────────────────────────────────────────
// CallDetailModal — opened from the row "Details" button on AgentCallsPage
// (build 188). Mirrors the layout the operator reference shows: Date/Time,
// Phone, Duration + Recording row up top; Summary; Call Analysis (extracted
// entity chips); chat-style Transcript with "User:" / "AI:" bubbles.
//
// Recording playback is intentionally stubbed — the audio capture path
// hasn't shipped yet, so the button is disabled with an explanatory caption.
// When recording lands, set `data.recording_available=true` server-side
// and this component renders an <audio controls /> against `data.recording_url`.
// ─────────────────────────────────────────────────────────────────────────
function CallDetailModal({ loading, data, agent, onClose }) {
  const fmtDate = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
    });
  };
  const fmtDur = (s) => {
    const n = Number(s || 0);
    if (!n) return "—";
    const m = Math.floor(n / 60);
    const ss = Math.round(n % 60).toString().padStart(2, "0");
    return `${m}:${ss} mins`;
  };
  // Extracted JSONB → flat list of {key, val} chips. Skip booleans /
  // empty strings — they read awkwardly as chips. Truncate long values.
  const chipFor = (k, v) => {
    if (v == null || v === "" || typeof v === "boolean") return null;
    const s = (Array.isArray(v) ? v.join(", ") : String(v)).trim();
    if (!s) return null;
    return `${s.length > 32 ? s.slice(0, 32) + "…" : s}`;
  };
  // Stable pastel palette — each chip gets its own colour by hashing
  // the key. Mirrors the reference design where every extracted entity
  // (date / time / count / location / etc.) reads as a distinct token.
  const CHIP_PALETTE = [
    { bg: "#fee2e2", fg: "#991b1b" }, // red
    { bg: "#fed7aa", fg: "#9a3412" }, // orange
    { bg: "#fef3c7", fg: "#92400e" }, // amber
    { bg: "#d9f99d", fg: "#3f6212" }, // lime
    { bg: "#d1fae5", fg: "#065f46" }, // emerald
    { bg: "#cffafe", fg: "#155e75" }, // cyan
    { bg: "#dbeafe", fg: "#1e3a8a" }, // blue
    { bg: "#ede9fe", fg: "#5b21b6" }, // violet
    { bg: "#fce7f3", fg: "#9f1239" }, // pink
  ];
  const chipColor = (key) => {
    let h = 0;
    const s = String(key || "");
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return CHIP_PALETTE[h % CHIP_PALETTE.length];
  };
  const chips = data && !data._err && data.extracted && typeof data.extracted === "object"
    ? Object.entries(data.extracted).map(([k, v]) => ({ k, label: chipFor(k, v) })).filter((x) => x.label)
    : [];

  return html`
    <div class="db-modal-backdrop call-detail-backdrop" onClick=${onClose}>
      <div class="db-modal db-modal-wide call-detail-modal" onClick=${(e) => e.stopPropagation()}>
        <div class="call-detail-head">
          <h2 class="call-detail-title">Call Details</h2>
          <button class="db-modal-close" type="button" aria-label="Close" onClick=${onClose}>×</button>
        </div>

        ${loading ? html`<div class="db-empty"><div class="db-empty-sub">Loading…</div></div>` :
          data?._err ? html`<div class="db-empty"><div class="db-empty-sub">Couldn't load this call.</div></div>` :
          data ? html`
            <div class="call-detail-body">
              <div class="call-detail-meta-grid">
                <div class="call-detail-meta-cell">
                  <div class="call-detail-meta-label">📅 Date/Time</div>
                  <div class="call-detail-meta-value">${fmtDate(data.started_at)}</div>
                </div>
                <div class="call-detail-meta-cell">
                  <div class="call-detail-meta-label">📞 Phone Number</div>
                  <div class="call-detail-meta-value">${data.phone_number || "Web / test call"}</div>
                </div>
                <div class="call-detail-meta-cell">
                  <div class="call-detail-meta-label">🕐 Duration</div>
                  <div class="call-detail-meta-value">${fmtDur(data.duration_s)}</div>
                </div>
                <div class="call-detail-meta-cell">
                  <div class="call-detail-meta-label">▶️ Recording</div>
                  ${data.recording_available
                    ? html`
                      <audio controls preload="metadata"
                             src=${data.recording_url}
                             class="call-detail-audio call-detail-audio-wide"
                             title="Caller on left, agent on right"></audio>
                    `
                    : html`
                      <button class="db-btn-primary call-detail-rec-btn" type="button" disabled
                              title=${data.recording_status || ""}>
                        <span>▶</span><span>Not available</span>
                      </button>
                    `}
                  ${data.recording_expires_at && !data.recording_purged_at ? html`
                    <div class="call-detail-rec-meta">
                      Retained until <b>${fmtDate(data.recording_expires_at).split(",")[0]}</b> · 180-day policy
                    </div>
                  ` : ""}
                  ${data.recording_purged_at ? html`
                    <div class="call-detail-rec-meta call-detail-rec-meta-purged">
                      Purged ${fmtDate(data.recording_purged_at)}
                    </div>
                  ` : ""}
                </div>
              </div>

              <div class="call-detail-section">
                <div class="call-detail-section-label">Call Summary:</div>
                <div class="call-detail-summary">${data.summary || data.final_message || "No summary captured."}</div>
              </div>

              ${chips.length > 0 ? html`
                <div class="call-detail-section">
                  <div class="call-detail-section-label">Call Analysis:</div>
                  <div class="call-detail-chips">
                    ${chips.map((c, i) => {
                      const col = chipColor(c.k);
                      return html`<span key=${i} class="call-detail-chip"
                                  style=${{ background: col.bg, color: col.fg, borderColor: col.bg }}>${c.label}</span>`;
                    })}
                  </div>
                </div>
              ` : ""}

              ${data.lead_quality || data.sentiment ? html`
                <div class="call-detail-section call-detail-mood">
                  ${data.lead_quality ? html`<span class=${"db-lead db-lead-" + data.lead_quality}>${data.lead_quality}</span>` : ""}
                  ${data.sentiment ? html`<span class=${"db-mood db-mood-" + data.sentiment}>${data.sentiment}</span>` : ""}
                  ${data.lead_signals ? html`<span class="call-detail-signals">${data.lead_signals}</span>` : ""}
                </div>
              ` : ""}

              <div class="call-detail-section">
                <div class="call-detail-section-label">Transcript</div>
                ${(data.transcript_turns || []).length === 0 ? html`
                  <div class="call-detail-tx-empty">
                    No transcript was captured for this call.
                    ${data.id ? html` (Calls before build 188 don't have transcripts saved — newer calls will.)` : ""}
                  </div>
                ` : html`
                  <div class="call-detail-tx">
                    <div class="call-detail-tx-marker">Call Started</div>
                    ${(data.transcript_turns || []).map((t, i) => {
                      // Map any role variant to user vs agent. Gemini Live
                      // emits "user" / "model". Some legacy data may use
                      // "assistant" / "ai".
                      const isUser = /^(user|caller|human)/i.test(t.role || "");
                      return html`
                        <div key=${i} class=${"call-detail-tx-row " + (isUser ? "is-user" : "is-agent")}>
                          <div class="call-detail-tx-bubble">
                            <span class="call-detail-tx-who">${isUser ? "User:" : (agent?.name ? `${agent.name}:` : "AI:")}</span>
                            <span class="call-detail-tx-text">${t.text}</span>
                          </div>
                        </div>
                      `;
                    })}
                  </div>
                `}
              </div>
            </div>
          ` : ""}

        <div class="call-detail-foot">
          <button class="db-btn-ghost" type="button" onClick=${onClose}>Close</button>
        </div>
      </div>
    </div>
  `;
}

function AgentCallsPage({ agent, agents, presets, plan, onNav, onEdit }) {
  const [allCalls, setAllCalls] = useState([]);
  const [loading, setLoading] = useState(true);
  // Build 188: per-row Details modal. `detailId` holds the call.id being
  // shown; `detail` is the fetched payload. Null = modal closed.
  const [detailId, setDetailId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  useEffect(() => {
    if (!detailId || !agent?.id) { setDetail(null); return; }
    setDetailLoading(true);
    fetch(`/api/agents/${agent.id}/calls/${detailId}`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then((d) => setDetail(d))
      .catch(() => setDetail({ _err: true }))
      .finally(() => setDetailLoading(false));
  }, [detailId, agent?.id]);
  // URL-driven filter — clicking on the Call outcomes page navigates here
  // with ?outcome=<id> or ?kind=<success|qualified|info|failure>. Read
  // location.search reactively so back/forward + in-app nav both update it.
  const [filter, setFilter] = useState(() => {
    if (typeof location === "undefined") return null;
    const q = new URLSearchParams(location.search);
    const o = (q.get("outcome") || "").trim();
    const k = (q.get("kind") || "").trim();
    return o ? { kind: "outcome", value: o } : (k ? { kind: "kind", value: k } : null);
  });
  // Re-read on popstate so in-app navigation updates the filter without a reload.
  useEffect(() => {
    const sync = () => {
      const q = new URLSearchParams(location.search);
      const o = (q.get("outcome") || "").trim();
      const k = (q.get("kind") || "").trim();
      setFilter(o ? { kind: "outcome", value: o } : (k ? { kind: "kind", value: k } : null));
    };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  // Catalogue is needed to translate a `kind` filter into the set of outcome
  // ids that belong to it. Fetched once per agent; tiny payload.
  const [catalogue, setCatalogue] = useState([]);
  useEffect(() => {
    if (!agent?.id) return;
    fetch(`/api/agents/${agent.id}/outcomes/catalogue`)
      .then((r) => r.ok ? r.json() : null).then((d) => setCatalogue(d?.outcomes || [])).catch(() => {});
  }, [agent?.id]);
  useEffect(() => {
    if (!agent?.id) return;
    setLoading(true);
    fetch(`/api/agents/${agent.id}/calls?limit=500`)
      .then((r) => r.json())
      .then((arr) => { setAllCalls(Array.isArray(arr) ? arr : []); setLoading(false); })
      .catch(() => setLoading(false));
  }, [agent?.id]);

  // Apply the URL filter to the call list. The stats tiles + table + CSV
  // export all run off the FILTERED list so the page reads consistently.
  const calls = (() => {
    if (!filter) return allCalls;
    if (filter.kind === "outcome") {
      return allCalls.filter((c) => (c.outcome || "") === filter.value);
    }
    if (filter.kind === "kind") {
      const ids = new Set(catalogue.filter((o) => o.kind === filter.value).map((o) => o.id));
      return allCalls.filter((c) => ids.has(c.outcome));
    }
    return allCalls;
  })();
  const clearFilter = () => {
    setFilter(null);
    try { window.history.pushState({}, "", location.pathname); } catch {}
  };
  const filterLabel = (() => {
    if (!filter) return "";
    if (filter.kind === "outcome") {
      const o = catalogue.find((c) => c.id === filter.value);
      return o ? o.label : filter.value.replace(/_/g, " ");
    }
    const map = { success: "Success", qualified: "Qualified", info: "Info-only", failure: "Failure" };
    return map[filter.value] || filter.value;
  })();

  // Stat aggregations
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
  const callsToday = calls.filter((c) => new Date(c.started_at || c.ended_at) >= today).length;
  const callsMonth = calls.filter((c) => new Date(c.started_at || c.ended_at) >= monthStart).length;
  const callsTotal = calls.length;
  const avgDur = calls.length
    ? (calls.reduce((s, c) => s + Number(c.duration_s || 0), 0) / calls.length)
    : 0;
  // Peak hour: bucket by hour, pick the bucket with the most calls
  const hourBuckets = Array(24).fill(0);
  calls.forEach((c) => {
    const h = new Date(c.started_at || c.ended_at).getHours();
    if (!Number.isNaN(h)) hourBuckets[h]++;
  });
  const peakHour = calls.length
    ? hourBuckets.indexOf(Math.max(...hourBuckets))
    : null;
  const fmt12 = (h) => `${((h + 11) % 12) + 1} ${h < 12 ? "AM" : "PM"}`;
  const peakLabel = peakHour === null ? "—" : `${fmt12(peakHour)}–${fmt12((peakHour + 1) % 24)}`;

  const tagColor = (outcome) => {
    const o = (outcome || "").toLowerCase();
    if (o === "booked")     return "db-tag-green";
    if (o === "lead")       return "db-tag-blue";
    if (o === "escalated")  return "db-tag-purple";
    if (o === "no_answer")  return "db-tag-grey";
    if (o === "abuse" || o === "rejected") return "db-tag-red";
    return "db-tag-grey";
  };

  const fmtTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  };

  const exportCSV = () => {
    const header = ["id", "started_at", "duration_s", "outcome", "reason", "summary"];
    const rows = calls.map((c) => header.map((k) => JSON.stringify(c[k] ?? "")).join(","));
    const blob = new Blob([header.join(",") + "\n" + rows.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${agent.slug || agent.id}-calls.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const actions = html`
    <button class="db-btn-ghost" type="button" onClick=${exportCSV} disabled=${!calls.length}>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>
      <span>Export CSV</span>
    </button>
  `;

  const body = html`
    <div class="db-overview">
      <section class="db-stats db-stats-5">
        <div class="db-stat db-stat-blue">
          <div class="db-stat-label">Calls today</div>
          <div class="db-stat-value">${callsToday}</div>
        </div>
        <div class="db-stat db-stat-green">
          <div class="db-stat-label">Calls this month</div>
          <div class="db-stat-value">${callsMonth}</div>
        </div>
        <div class="db-stat db-stat-pink">
          <div class="db-stat-label">Calls to date</div>
          <div class="db-stat-value">${callsTotal}</div>
        </div>
        <div class="db-stat db-stat-yellow">
          <div class="db-stat-label">Avg duration</div>
          <div class="db-stat-value">${avgDur ? avgDur.toFixed(1) + "s" : "—"}</div>
        </div>
        <div class="db-stat db-stat-purple">
          <div class="db-stat-label">Peak time</div>
          <div class="db-stat-value db-stat-value-sm">${peakLabel}</div>
        </div>
      </section>

      ${filter ? html`
        <div class="db-filter-banner">
          <span class="db-filter-banner-icon" aria-hidden="true">${filter.kind === "kind" ? "🎯" : "🔍"}</span>
          <span>Showing ${calls.length} call${calls.length === 1 ? "" : "s"} where <b>${filter.kind === "kind" ? `kind = ${filterLabel.toLowerCase()}` : `outcome = ${filterLabel}`}</b></span>
          <button class="db-filter-clear" type="button" onClick=${clearFilter}>Clear filter ×</button>
        </div>
      ` : ""}
      ${loading ? html`<div class="db-empty"><div class="db-empty-sub">Loading…</div></div>` :
        calls.length === 0 ? html`
          <div class="db-panel">
            <div class="db-empty" style=${{ margin: "16px auto" }}>
              <div class="db-empty-icon"></div>
              <div class="db-empty-title">${filter ? "No calls match this filter" : "No calls yet"}</div>
              <div class="db-empty-sub">${filter
                ? html`No calls in this window had outcome <b>${filterLabel}</b>. <button class="db-btn-ghost db-btn-sm" type="button" onClick=${clearFilter} style=${{ marginLeft: "8px" }}>Show all calls</button>`
                : html`Once ${agent.name} answers ${pronouns(agent).poss} first call, every call lands here with full transcript, outcome and summary.`}
              ${!filter ? html`<button class="db-btn-primary" onClick=${onEdit}>Send a test call →</button>` : ""}
              </div>
            </div>
          </div>
        ` : html`
          <div class="db-table-wrap">
            <table class="db-table">
              <thead>
                <tr>
                  <th>Date / Time</th>
                  <th>Duration</th>
                  <th>Outcome</th>
                  <th>
                    Lead
                    <${InfoDot}>
                      <strong>Hot</strong> — ready to act now (book / buy / escalate). <strong>Warm</strong> — clear interest, needs follow-up. <strong>Cold</strong> — info-only, no buying signal. <strong>N/A</strong> — wasn't a buying call (support, status check). ${agent.name} assesses honestly at the end of every call.
                    </${InfoDot}>
                  </th>
                  <th>
                    Mood
                    <${InfoDot}>
                      How the caller sounded overall — <strong>positive</strong>, <strong>neutral</strong>, <strong>negative</strong>, or <strong>mixed</strong>. A frustrated caller whose problem got solved is <em>mixed</em>, not <em>positive</em>.
                    </${InfoDot}>
                  </th>
                  <th>Summary</th>
                  <th class="db-table-th-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                ${calls.map((c) => html`
                  <tr key=${c.id}>
                    <td>${fmtTime(c.started_at)}</td>
                    <td>${c.duration_s ? Number(c.duration_s).toFixed(1) + "s" : "—"}</td>
                    <td><span class=${"db-tag " + tagColor(c.outcome)}>${(c.outcome || "unknown").replace(/_/g, " ")}</span></td>
                    <td>
                      ${c.lead_quality
                        ? html`<span class=${"db-lead db-lead-" + c.lead_quality} title=${c.lead_signals || ""}>${c.lead_quality}</span>`
                        : html`<span class="db-muted">—</span>`}
                    </td>
                    <td>
                      ${c.sentiment
                        ? html`<span class=${"db-mood db-mood-" + c.sentiment}>${c.sentiment}</span>`
                        : html`<span class="db-muted">—</span>`}
                    </td>
                    <td class="db-table-summary">${c.summary || c.reason || "—"}</td>
                    <td class="db-table-td-right">
                      <button class="db-btn-ghost db-btn-sm" type="button"
                              onClick=${() => setDetailId(c.id)}>Details</button>
                    </td>
                  </tr>
                `)}
              </tbody>
            </table>
          </div>
        `}

      ${detailId ? html`
        <${CallDetailModal}
          loading=${detailLoading}
          data=${detail}
          agent=${agent}
          onClose=${() => { setDetailId(null); setDetail(null); }} />
      ` : ""}
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="calls"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Call log"
      subtitle=${`${agent.name} · ${(presets?.locales || []).find((l) => l.id === agent.locale)?.label || agent.locale || ""}`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// Per-agent focused pages — each one does ONE thing well and saves to the
// existing PATCH /api/agents/{id} endpoint. Shared draft helpers live below.
// ─────────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────────
// Locale detection — runs once at boot, persisted to localStorage so the
// hero CTA + Go-live country pre-fill stay stable across reloads even if
// the user later resolves into a corporate VPN.
// ─────────────────────────────────────────────────────────────────────────
const COUNTRY_DICT = {
  IN: { label: "India",          dial: "+91",  flag: "🇮🇳" },
  US: { label: "United States",  dial: "+1",   flag: "🇺🇸" },
  GB: { label: "United Kingdom", dial: "+44",  flag: "🇬🇧" },
  SG: { label: "Singapore",      dial: "+65",  flag: "🇸🇬" },
  AE: { label: "UAE",            dial: "+971", flag: "🇦🇪" },
  AU: { label: "Australia",      dial: "+61",  flag: "🇦🇺" },
  CA: { label: "Canada",         dial: "+1",   flag: "🇨🇦" },
  DE: { label: "Germany",        dial: "+49",  flag: "🇩🇪" },
  FR: { label: "France",         dial: "+33",  flag: "🇫🇷" },
  ES: { label: "Spain",          dial: "+34",  flag: "🇪🇸" },
  JP: { label: "Japan",          dial: "+81",  flag: "🇯🇵" },
  // Indian-language hints — even if region is unknown, language signals India
  HI: { label: "India",          dial: "+91",  flag: "🇮🇳" },
  BN: { label: "India",          dial: "+91",  flag: "🇮🇳" },
  TA: { label: "India",          dial: "+91",  flag: "🇮🇳" },
};
const LANG_LABELS = {
  hi: "Hindi", en: "English", bn: "Bengali", ta: "Tamil", te: "Telugu",
  kn: "Kannada", ml: "Malayalam", mr: "Marathi", gu: "Gujarati",
  es: "Spanish", fr: "French", de: "German", ja: "Japanese", ar: "Arabic",
};

function detectLocale() {
  // Prefer cached value to avoid layout shift after IP resolution.
  try {
    const cached = JSON.parse(localStorage.getItem("sxai.locale") || "null");
    if (cached?.country) return cached;
  } catch {}
  // navigator.language gives e.g. "en-IN", "hi-IN", "en-US"
  const tag = (navigator.language || "en-US").trim();
  const [langRaw, regionRaw] = tag.split("-");
  const lang = (langRaw || "en").toLowerCase();
  let region = (regionRaw || "").toUpperCase();
  // If region missing, infer from language (Hindi → India, etc.)
  if (!region) {
    const langKey = lang.toUpperCase();
    if (COUNTRY_DICT[langKey]) region = "IN";
    else region = "US";
  }
  // Timezone fallback — e.g. "Asia/Kolkata" → IN, "Europe/London" → GB
  if (!COUNTRY_DICT[region]) {
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      if (tz.startsWith("Asia/Kolkata") || tz.startsWith("Asia/Calcutta")) region = "IN";
      else if (tz.startsWith("Asia/Singapore")) region = "SG";
      else if (tz.startsWith("Asia/Dubai")) region = "AE";
      else if (tz.startsWith("Europe/London")) region = "GB";
      else if (tz.startsWith("Australia/")) region = "AU";
      else if (tz.startsWith("America/")) region = "US";
    } catch {}
  }
  if (!COUNTRY_DICT[region]) region = "US";
  const country = COUNTRY_DICT[region];
  const result = {
    country: region,
    countryLabel: country.label,
    countryDial: country.dial,
    countryFlag: country.flag,
    language: lang,
    languageLabel: LANG_LABELS[lang] || "English",
    bcp47: tag,
  };
  try { localStorage.setItem("sxai.locale", JSON.stringify(result)); } catch {}
  return result;
}

// Save state pill shown next to the Save button.
function SaveStatePill({ state }) {
  if (!state) return null;
  const cls = state.cls === "ok" ? "db-save-ok" : state.cls === "err" ? "db-save-err" : "db-save-dim";
  return html`<span class=${"db-save-pill " + cls}>${state.msg}</span>`;
}

// Persona & tone — name + persona one-liner + greeting + free-form prompt.
function AgentPersonaPage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  const [draft, setDraft] = useState({
    name: agent.name || "",
    persona: agent.persona || "",
    greeting: agent.greeting || "",
    system_prompt: agent.system_prompt || "",
  });
  const [state, setState] = useState({ msg: "", cls: "" });
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };
  const deleteAgent = async () => {
    if (confirmName.trim() !== agent.name) return;
    setDeleting(true);
    try {
      await fetch(`/api/agents/${agent.id}`, { method: "DELETE" });
      onNav && onNav("/agents");
    } catch {
      setDeleting(false);
    }
  };

  const body = html`
    <div class="db-overview">
      <!-- Identity + First Message — paired side-by-side because they're
           short and conceptually linked (the agent's persona feeds into
           her first line). Stacks back to one column on phones via the
           shared .db-twocol rule. -->
      <div class="db-twocol persona-twocol">
        <!-- Identity: the agent's name + one-line persona. Caller never
             sees these labels; the name flows into the greeting + prompt. -->
        <section class="db-panel">
          <h3 class="db-panel-title">Identity</h3>
          <p class="db-panel-sub">Who the agent is. The caller never sees these — they shape the greeting and how the agent refers to herself.</p>
          <div class="db-form">
            <label class="db-form-field">
              <span class="db-form-label">Agent name</span>
              <input class="db-input" value=${draft.name} onInput=${(e) => set("name", e.target.value)} placeholder="e.g. Maya" />
            </label>
            <label class="db-form-field">
              <span class="db-form-label">Persona <span class="db-form-opt">(one sentence)</span></span>
              <input class="db-input" value=${draft.persona} onInput=${(e) => set("persona", e.target.value)} placeholder="Warm, efficient receptionist at a dental clinic." />
            </label>
          </div>
        </section>

        <!-- First Message — the greeting the agent opens every call with.
             Renamed from "Greeting" + given an InfoDot so a brand-new
             operator immediately knows this is the literal first line the
             caller hears (Vapi-style clarity). -->
        <section class="db-panel">
          <h3 class="db-panel-title">
            First Message
            <${InfoDot}>
              The exact first line ${agent.name || "your agent"} speaks when a call connects,
              before the caller says anything. Keep it short and natural — name +
              business + a "how can I help?". Leave it blank to let the agent
              improvise an opening.
            </${InfoDot}>
          </h3>
          <${MarkdownEditor}
            value=${draft.greeting}
            onChange=${(v) => set("greeting", v)}
            rows=${2}
            compact=${true}
            placeholder="Hello, this is Maya at BrightSmile Dental. How can I help you today?" />
          <span class="db-form-help">The very first thing the caller hears.</span>
        </section>
      </div>

      <!-- System Prompt — the master prompt. This is the answer to
           "where do I change how the agent behaves?". InfoDot explains
           it + the auto-prepended safety floor so operators don't waste
           lines re-stating basics. -->
      <section class="db-panel">
        <h3 class="db-panel-title">
          System Prompt
          <${InfoDot}>
            The master instructions that control everything ${agent.name || "your agent"}
            says and does — her job, what she answers about, how she handles
            bookings, her tone, edge-cases, and escalation. Write it in plain
            English. This is the main place to change the agent's behaviour.
          </${InfoDot}>
        </h3>
        <p class="db-panel-sub">
          Edit freely — this is the agent's brain. A universal safety floor
          (no medical/legal advice, never read full card numbers, hand off to
          a human on request) is always applied on top automatically, so you
          don't need to re-state the basics here.
        </p>
        <${MarkdownEditor}
          value=${draft.system_prompt}
          onChange=${(v) => set("system_prompt", v)}
          rows=${16}
          className="md-editor-prompt"
          placeholder=${"You are Maya, the receptionist for BrightSmile Dental…\n\n## What callers want\n- Book a check-up\n- Reschedule\n- Ask about hours and pricing\n\n## How to handle bookings\n1. Check the calendar\n2. Propose 2 nearby slots\n3. Confirm and send an SMS recap\n\n## Tone\nWarm, calm, unhurried. Switch to Hindi if the caller does.\n\n## Edge cases\nIf asked about pain or symptoms, don't advise — offer the soonest appointment."} />
        <span class="db-form-help">Tip: structure it as Who she is → What callers want → How to handle each → Tone → Edge-cases → Close. Markdown headings, lists and bold help her parse it.</span>
      </section>

      <section class="db-panel db-danger">
        <h3 class="db-panel-title">Danger zone</h3>
        <p class="db-panel-sub">Deleting ${agent.name} permanently removes the agent, its call history, and any phone-number requests. This cannot be undone.</p>
        <div class="db-danger-row">
          <input class="db-input db-danger-input" placeholder=${`Type "${agent.name}" to confirm`}
                 value=${confirmName} onInput=${(e) => setConfirmName(e.target.value)} />
          <button class="db-btn-danger" type="button"
                  disabled=${confirmName.trim() !== agent.name || deleting}
                  onClick=${deleteAgent}>
            ${deleting ? "Deleting…" : "Delete agent"}
          </button>
        </div>
      </section>
    </div>
  `;

  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save changes</button>
  `;

  return html`
    <${DashboardShell}
      activeKey="persona"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Persona & tone"
      subtitle="How ${agent.name} introduces herself and handles the conversation."
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Small talk — short rapport openers the agent leans on when a caller
// starts with chitchat ("hi, how are you?"). Distinct from the task-specific
// "Sample phrases" baked into the system prompt: those are business-facing
// ("Let me check that for you, one moment"), these are pure warmth.
//
// Eva pre-fills 2-4 phrases during build based on sector + region; the
// operator can add / edit / remove freely. The runtime prompt builder reads
// the column straight from agents.small_talk on every call.
function AgentSmallTalkPage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  // One phrase per line — simplest possible editor that matches how an
  // operator actually thinks about these ("here are the things she might
  // say to warm up the caller"). On save we split, trim, dedupe, drop
  // blanks; the server has the same hygiene in silent_defaults so we're
  // forgiving on both sides.
  const initial = (agent.small_talk || []).filter((s) => typeof s === "string").join("\n");
  const [text, setText] = useState(initial);
  const [state, setState] = useState({ msg: "", cls: "" });

  const phrases = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const raw of text.split("\n")) {
      const s = raw.trim();
      if (!s || seen.has(s)) continue;
      seen.add(s);
      out.push(s.slice(0, 120));
      if (out.length >= 8) break;
    }
    return out;
  }, [text]);

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ small_talk: phrases }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  const placeholder = [
    "Hope you're keeping well.",
    "How's your day going?",
    "Glad you called — how can I help?",
  ].join("\n");

  const body = html`
    <div class="db-overview">
      <section class="db-panel">
        <h3 class="db-panel-title">Rapport openers</h3>
        <p class="db-panel-sub">
          One short phrase per line — ${agent.name || "your agent"} picks the
          one that fits when a caller opens with chitchat. Keep them under
          eight words, no business name or booking talk. Eva pre-filled a
          few based on your sector; edit, reorder, or add freely.
        </p>
        <textarea class="db-textarea" rows="8"
                  value=${text}
                  onInput=${(e) => setText(e.target.value)}
                  placeholder=${placeholder}></textarea>
        <p class="db-form-help">
          ${phrases.length} phrase${phrases.length === 1 ? "" : "s"} ready
          ${phrases.length >= 8 ? " · capped at 8" : ""}
          ${phrases.length === 0 ? " · falls back to a generic warm opener" : ""}
        </p>
      </section>

      <section class="db-panel">
        <h3 class="db-panel-title">Preview</h3>
        <p class="db-panel-sub">How they'll appear in ${agent.name || "your agent"}'s runtime prompt.</p>
        ${phrases.length === 0 ? html`
          <p class="db-form-help">No phrases yet — the agent will fall back to "How can I help?".</p>
        ` : html`
          <ul style=${{ margin: "8px 0 0", paddingLeft: "20px", lineHeight: "1.7" }}>
            ${phrases.map((p) => html`<li>“${p}”</li>`)}
          </ul>
        `}
      </section>
    </div>
  `;

  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save changes</button>
  `;

  return html`
    <${DashboardShell}
      activeKey="small-talk"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Small talk"
      subtitle="Short rapport openers ${agent.name || "your agent"} uses when callers warm up first."
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Knowledge base — for now, edits the system prompt's "facts" section.
// File upload + RAG comes later; this page intentionally only does ONE thing:
// "here's what the agent knows about your business."
function AgentKnowledgePage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  const [text, setText] = useState(agent.system_prompt || "");
  // URLs the user wants the agent to "know about" — stored as a newline-list
  // under variables.knowledge_urls so it round-trips through the existing
  // variables JSON column. Today these are documentation references the
  // model sees in the system prompt; tomorrow they'll be RAG ingestion
  // inputs. Either way the agent NEVER fetches them at runtime.
  const [urls, setUrls] = useState(() => (agent?.variables?.knowledge_urls || "").trim());
  const [allowList, setAllowList] = useState(() => (agent?.variables?.knowledge_allow_domains || "").trim());
  const [state, setState] = useState({ msg: "", cls: "" });
  // Tab strip — organise the knowledge surface by SOURCE TYPE so each one
  // gets a focused view (manual notes vs. URL imports vs. file uploads).
  const [kbTab, setKbTab] = useState("notes");
  // ── Import (URL via Firecrawl + file upload) preview/apply state ────────
  // mode: null | "url" | "file"
  const [importMode, setImportMode] = useState(null);
  const [importUrl, setImportUrl] = useState("");
  const [importFileMeta, setImportFileMeta] = useState(null);   // {name, size}
  const [importPreview, setImportPreview] = useState(null);     // {yaml, source}
  const [importYaml, setImportYaml] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [importErr, setImportErr] = useState("");
  const fileInputRef = useRef(null);
  const sources = Array.isArray(agent?.variables?.knowledge_sources)
    ? agent.variables.knowledge_sources : [];

  const openImportUrl = () => {
    setImportMode("url"); setImportUrl(""); setImportFileMeta(null);
    setImportPreview(null); setImportYaml(""); setImportErr("");
  };
  const closeImport = () => {
    if (importBusy) return;
    setImportMode(null); setImportPreview(null); setImportYaml("");
    setImportUrl(""); setImportFileMeta(null); setImportErr("");
  };
  const fetchUrlPreview = async () => {
    const u = (importUrl || "").trim();
    if (!u) return;
    setImportBusy(true); setImportErr("");
    try {
      const r = await fetch(`/api/agents/${agent.id}/knowledge/import-url`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: u }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error((d && (d.detail?.message || d.detail)) || ("Import failed (" + r.status + ")"));
      }
      const data = await r.json();
      if (!data || !data.yaml) throw new Error("Nothing useful was found on that URL.");
      setImportPreview({ yaml: data.yaml, source: data.source || { kind: "url", url: u } });
      setImportYaml(data.yaml);
    } catch (e) {
      setImportErr(String(e.message || e));
    } finally { setImportBusy(false); }
  };
  const onFilePicked = async (file) => {
    if (!file) return;
    setImportMode("file"); setImportFileMeta({ name: file.name, size: file.size });
    setImportPreview(null); setImportYaml(""); setImportErr("");
    setImportBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("filename", file.name);
      const r = await fetch(`/api/agents/${agent.id}/knowledge/upload`, {
        method: "POST", body: form,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error((d && (d.detail?.message || d.detail)) || ("Upload failed (" + r.status + ")"));
      }
      const data = await r.json();
      if (!data || !data.yaml) throw new Error("That file had nothing usable.");
      setImportPreview({ yaml: data.yaml, source: data.source || { kind: "file", filename: file.name } });
      setImportYaml(data.yaml);
    } catch (e) {
      setImportErr(String(e.message || e));
    } finally { setImportBusy(false); }
  };
  const applyImport = async () => {
    if (!importPreview) return;
    setImportBusy(true); setImportErr("");
    try {
      const src = importPreview.source || {};
      let r;
      if (src.kind === "file") {
        const form = new FormData();
        form.append("yaml", importYaml || "");
        form.append("filename", src.filename || importFileMeta?.name || "uploaded.txt");
        form.append("apply", "true");
        r = await fetch(`/api/agents/${agent.id}/knowledge/upload`, { method: "POST", body: form });
      } else {
        r = await fetch(`/api/agents/${agent.id}/knowledge/import-url`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: src.url || importUrl, yaml: importYaml, apply: true, title: src.title || "" }),
        });
      }
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error((d && (d.detail?.message || d.detail)) || ("Apply failed (" + r.status + ")"));
      }
      const data = await r.json();
      // Refresh local system_prompt + agent in the parent.
      if (data && data.agent && data.agent.system_prompt != null) setText(data.agent.system_prompt);
      refreshAgent && refreshAgent();
      setState({ msg: "Knowledge added ✓", cls: "ok" });
      setTimeout(() => setState({ msg: "", cls: "" }), 2400);
      closeImport();
    } catch (e) {
      setImportErr(String(e.message || e));
    } finally { setImportBusy(false); }
  };

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      // Merge URLs + allow-list into variables, leaving everything else alone.
      const variables = { ...(agent.variables || {}) };
      if (urls.trim()) variables.knowledge_urls = urls.trim();
      else delete variables.knowledge_urls;
      if (allowList.trim()) variables.knowledge_allow_domains = allowList.trim();
      else delete variables.knowledge_allow_domains;
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: text, variables }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };
  // Counts shown on each tab pill so the operator can see at a glance
  // what each source-type contains.
  const urlSources = sources.filter((s) => (s.kind || "url") !== "file");
  const fileSources = sources.filter((s) => s.kind === "file");
  const refUrlCount = (urls || "").split(/\n+/).map((x) => x.trim()).filter(Boolean).length;

  const SourceList = (items, kind) => items.length === 0 ? html`
    <p class="db-kb-empty">${kind === "url"
      ? "No URL imports yet — click “Import from URL” to pull facts from a page."
      : "No files uploaded yet — pick a file above to add one."}</p>
  ` : html`
    <ul class="db-kb-sources">
      ${items.slice().reverse().map((s, i) => html`
        <li key=${i} class="db-kb-source">
          <span class="db-kb-source-icon" aria-hidden="true">${kind === "file" ? "📄" : "🌐"}</span>
          <div class="db-kb-source-meta">
            <div class="db-kb-source-label">
              ${kind === "file"
                ? html`<span>${s.filename || "Uploaded file"}</span>`
                : html`<a href=${s.url || "#"} target="_blank" rel="noopener">${s.title || s.url || "URL"}</a>`}
            </div>
            <div class="db-kb-source-when">${(s.added_at || "").slice(0, 10)}</div>
          </div>
          <button class="db-kb-source-view" type="button" title="Find this in the Notes editor"
                  onClick=${() => setKbTab("notes")}>View in notes →</button>
        </li>
      `)}
    </ul>
  `;

  const body = html`
    <div class="db-overview">
      <!-- Source-restriction explainer — sets expectations up-front. -->
      <section class="db-publish-banner is-gated">
        <div class="db-publish-left">
          <div class="db-publish-dot is-gated" aria-hidden="true"></div>
          <div>
            <div class="db-publish-status">Closed-source knowledge</div>
            <div class="db-publish-copy">
              ${agent.name || "Your agent"} answers <strong>only</strong> from this page and the business profile — no browsing, no Google, no Wikipedia mid-call. That means: no hallucinated prices, no invented hours, no "let me check online for you". If a fact isn't here, ${pronouns(agent).subj} politely says so or hands the caller off.
            </div>
          </div>
        </div>
      </section>

      <!-- Tabs — organise the surface by source type. -->
      <div class="kb-tabs" role="tablist" aria-label="Knowledge sources">
        ${[
          { id: "notes", icon: "✏️", label: "Notes",         count: null },
          { id: "urls",  icon: "🌐", label: "URL imports",   count: urlSources.length + (refUrlCount > 0 ? 0 : 0) },
          { id: "files", icon: "📄", label: "Uploaded files", count: fileSources.length },
        ].map((t) => html`
          <button key=${t.id} type="button" role="tab"
                  aria-selected=${kbTab === t.id}
                  class=${"kb-tab" + (kbTab === t.id ? " is-active" : "")}
                  onClick=${() => setKbTab(t.id)}>
            <span class="kb-tab-icon" aria-hidden="true">${t.icon}</span>
            <span class="kb-tab-label">${t.label}</span>
            ${(t.count != null && t.count > 0) ? html`<span class="kb-tab-count">${t.count}</span>` : ""}
          </button>
        `)}
      </div>

      <!-- Notes — the manual "what she knows" editor + the allow-list. -->
      ${kbTab === "notes" ? html`
        <section class="db-panel">
          <h3 class="db-panel-title">What ${agent.name || "your agent"} knows</h3>
          <p class="db-panel-sub">Hours, prices, FAQs, policies — anything ${pronouns(agent).subj} should be able to answer. Paste freely; ${pronouns(agent).subj} cites only what's here. Anything imported from URLs or uploaded files lands here too, in a clearly-bounded KNOWLEDGE block — edit it like any other text.</p>
          <${MarkdownEditor}
            value=${text}
            onChange=${(v) => setText(v)}
            rows=${18}
            className="md-editor-prompt"
            placeholder=${"## Hours\nMon–Sun, 9 AM – 9 PM\n\n## Address\n123 MG Road, Bengaluru\n\n## Pricing\n- Consultation ₹500\n- Root canal ₹4500\n\n## FAQs\n**Do you accept insurance?** Yes — Star Health, ICICI Lombard, HDFC ERGO."} />
        </section>

        <section class="db-panel">
          <h3 class="db-panel-title">Allowed domains <span class="db-panel-pill">Optional</span></h3>
          <p class="db-panel-sub">If you ever wire ${agent.name || "your agent"} to a tool that does live retrieval (search, web fetch, link previews) — we lock it to these domains only. Leave blank to allow everything we connect (default). Today this is forward-looking.</p>
          <label class="db-form-field">
            <span class="db-form-label">Allow-list <span class="db-form-opt">(comma or newline)</span></span>
            <input class="db-input" type="text" value=${allowList}
                   placeholder="yourdomain.com, helpcenter.yourdomain.com"
                   onInput=${(e) => setAllowList(e.target.value)} />
          </label>
        </section>
      ` : ""}

      <!-- URL imports — reference list + Firecrawl-backed imports. -->
      ${kbTab === "urls" ? html`
        <section class="db-panel">
          <h3 class="db-panel-title">Import from a URL <span class="db-panel-pill">Firecrawl</span></h3>
          <p class="db-panel-sub">Paste a website, Google Maps listing, or local-listing URL. Eva pulls the facts and shows you a YAML preview to edit before adding them to ${agent.name || "your agent"}'s knowledge.</p>
          <div class="db-kb-actions">
            <button class="db-btn-primary" type="button" onClick=${openImportUrl}>
              <span aria-hidden="true">🌐</span> Import from URL
            </button>
          </div>
        </section>

        <section class="db-panel">
          <h3 class="db-panel-title">Imported URLs <span class="db-panel-pill">${urlSources.length}</span></h3>
          <p class="db-panel-sub">Pages whose facts you've folded into the knowledge base.</p>
          ${SourceList(urlSources, "url")}
        </section>

        <section class="db-panel">
          <h3 class="db-panel-title">Reference URLs <span class="db-panel-pill">Reference only</span></h3>
          <p class="db-panel-sub">Plain documentation links — pasted so a teammate reviewing the agent knows where facts came from. No live fetching at call time.</p>
          <label class="db-form-field">
            <span class="db-form-label">URLs <span class="db-form-opt">(one per line)</span></span>
            <textarea class="db-textarea" rows="4"
                      style=${{ fontFamily: "ui-monospace, SF Mono, Menlo, monospace", fontSize: "12.5px" }}
                      value=${urls}
                      placeholder="https://yourdomain.com/pricing${"\n"}https://yourdomain.com/hours${"\n"}https://yourdomain.com/faq"
                      onInput=${(e) => setUrls(e.target.value)}></textarea>
          </label>
        </section>
      ` : ""}

      <!-- Uploaded files — the dropzone + file-source history. -->
      ${kbTab === "files" ? html`
        <section class="db-panel">
          <h3 class="db-panel-title">Upload a document <span class="db-panel-pill">.txt · .docx</span></h3>
          <p class="db-panel-sub">Drop in a menu, FAQ sheet, or policy doc. Eva condenses it to a YAML brief; you review and approve before it lands in ${agent.name || "your agent"}'s knowledge. <span class="db-form-opt">PDF and XLS coming soon.</span></p>
          <input ref=${fileInputRef} type="file" accept=".txt,.md,.markdown,.docx"
                 style=${{ display: "none" }}
                 onChange=${(e) => { const f = e.target.files?.[0]; if (f) onFilePicked(f); e.target.value = ""; }} />
          <button type="button" class="db-dropzone is-clickable" onClick=${() => fileInputRef.current?.click()}>
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            <span>Click to pick a file — .txt, .md, or .docx (≤ 8 MB)</span>
          </button>
        </section>

        <section class="db-panel">
          <h3 class="db-panel-title">Uploaded files <span class="db-panel-pill">${fileSources.length}</span></h3>
          <p class="db-panel-sub">Documents you've folded into the knowledge base.</p>
          ${SourceList(fileSources, "file")}
        </section>
      ` : ""}
    </div>
  `;

  // Preview / apply modal — operator reviews the YAML before it's appended.
  const importModal = (importMode && (importBusy || importPreview || importErr || importMode === "url")) ? html`
    <div class="db-modal-backdrop" onClick=${closeImport}>
      <div class="db-modal db-modal-wide" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>${importMode === "file"
              ? (importFileMeta ? `Review: ${importFileMeta.name}` : "Review uploaded knowledge")
              : (importPreview ? `Review: ${importPreview.source?.title || importPreview.source?.url || "URL"}` : "Import from URL")}</h2>
          <button class="db-modal-close" type="button" onClick=${closeImport} aria-label="Close" disabled=${importBusy}>×</button>
        </header>
        <div class="db-modal-body">
          ${importMode === "url" && !importPreview && !importBusy ? html`
            <p class="db-modal-lead">Paste a website, Google Maps listing, or local-listing URL. Eva pulls the facts and shows you a YAML preview to edit before it's added.</p>
            <div class="db-modal-row">
              <input class="db-modal-input" type="url"
                     placeholder="https://your-business.com or a Google Maps link"
                     value=${importUrl}
                     onInput=${(e) => setImportUrl(e.target.value)}
                     onKeyDown=${(e) => { if (e.key === "Enter" && importUrl.trim()) fetchUrlPreview(); }} />
            </div>
          ` : ""}
          ${importBusy ? html`
            <div class="db-kb-loading">
              <span class="wiz-spinner-sm" aria-hidden="true"></span>
              <span>${importMode === "file" ? "Reading your file and condensing it…" : "Pulling and condensing the page…"}</span>
            </div>
          ` : ""}
          ${importPreview ? html`
            <p class="db-modal-lead">Reviewed YAML below — edit anything you like. On <b>Apply</b>, it's appended to ${agent.name || "your agent"}'s knowledge (a single KNOWLEDGE block on the system prompt; never replaces what's already there).</p>
            <textarea class="wiz-knowledge-yaml" rows="14" spellCheck="false"
                      value=${importYaml}
                      onInput=${(e) => setImportYaml(e.target.value)}></textarea>
          ` : ""}
          ${importErr ? html`<div class="db-modal-err">${importErr}</div>` : ""}
        </div>
        <div class="db-modal-foot">
          <button class="db-modal-btn ghost" type="button" onClick=${closeImport} disabled=${importBusy}>Cancel</button>
          ${importPreview
            ? html`<button class="db-modal-btn primary" type="button" onClick=${applyImport} disabled=${importBusy || !importYaml.trim()}>${importBusy ? "Applying…" : "Apply to knowledge"}</button>`
            : (importMode === "url"
                ? html`<button class="db-modal-btn primary" type="button" onClick=${fetchUrlPreview} disabled=${importBusy || !importUrl.trim()}>${importBusy ? "Pulling…" : "Pull facts"}</button>`
                : "")}
        </div>
      </div>
    </div>
  ` : "";
  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save knowledge</button>
  `;
  return html`
    <${DashboardShell}
      activeKey="knowledge"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Knowledge base"
      subtitle=${`Facts and policies ${agent.name} can rely on.`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
    ${importModal}
  `;
}

// Voice settings — voice picker + locale + the few sliders that matter.
// What Gemini Live's voices actually speak. All 8 voices use the same speech
// model and cover the same locales — the differentiator is timbre / character,
// not language. We list this explicitly so users don't have to start a call
// to find out whether "Charon" can handle Hindi (yes), and don't end up
// picking voices by guessing at language icons. Grouped for legibility.
const VOICE_SUPPORTED_LANGUAGES = [
  {
    group: "English variants",
    items: [
      { id: "en-US", label: "English (US)" },
      { id: "en-GB", label: "English (UK)" },
      { id: "en-IN", label: "English (India)" },
      { id: "en-AU", label: "English (Australia)" },
    ],
  },
  {
    group: "Indian languages",
    items: [
      { id: "hi-IN", label: "Hindi" },
      { id: "bn-IN", label: "Bengali" },
      { id: "ta-IN", label: "Tamil" },
      { id: "te-IN", label: "Telugu" },
      { id: "kn-IN", label: "Kannada" },
      { id: "ml-IN", label: "Malayalam" },
      { id: "mr-IN", label: "Marathi" },
      { id: "gu-IN", label: "Gujarati" },
    ],
  },
  {
    group: "European",
    items: [
      { id: "es-ES", label: "Spanish (Spain)" },
      { id: "es-MX", label: "Spanish (Mexico)" },
      { id: "fr-FR", label: "French" },
      { id: "de-DE", label: "German" },
      { id: "it-IT", label: "Italian" },
      { id: "pt-BR", label: "Portuguese (Brazil)" },
      { id: "nl-NL", label: "Dutch" },
      { id: "pl-PL", label: "Polish" },
      { id: "ru-RU", label: "Russian" },
      { id: "tr-TR", label: "Turkish" },
    ],
  },
  {
    group: "East Asia + Middle East",
    items: [
      { id: "ja-JP", label: "Japanese" },
      { id: "ko-KR", label: "Korean" },
      { id: "zh-CN", label: "Mandarin" },
      { id: "id-ID", label: "Indonesian" },
      { id: "th-TH", label: "Thai" },
      { id: "vi-VN", label: "Vietnamese" },
      { id: "ar-XA", label: "Arabic" },
    ],
  },
];
const VOICE_TOTAL_LANGS = VOICE_SUPPORTED_LANGUAGES.reduce((n, g) => n + g.items.length, 0);

// Tone descriptors per voice — Gemini Live's 8 voices are language-agnostic,
// so the only thing differentiating them in practice is timbre. We surface
// that explicitly here: the "tone" is the headline, the voice name is the
// subtitle, and the user picks by feeling instead of by recognising obscure
// Greek names. Eva auto-picks one during the build interview based on the
// agent's persona — this page exposes the picker so the user can override.
// Human-readable label for each sector's default ambience — shown on the
// Voice settings page so users know what "Default" actually picks. Keys
// must mirror SECTOR_AMBIENCE in startAmbienceFor below.
const SECTOR_AMBIENCE_LABEL = {
  saas_support: "office",
  banking: "office",
  insurance: "office",
  education: "office",
  legal: "quiet",
  real_estate: "office",
  retail: "café",
  restaurant: "café",
  events: "café",
  travel: "café",
  healthcare: "clinic",
  dental: "clinic",
  automotive: "workshop",
  logistics: "workshop",
  generic: "office",
};

const VOICE_TONES = {
  Aoede:  { tone: "Warm & friendly",       vibe: "Welcomes the caller like a familiar receptionist." },
  Puck:   { tone: "Bright & upbeat",       vibe: "Energy in every sentence — good for sales, hospitality." },
  Charon: { tone: "Deep & calm",           vibe: "Composed, low-pitched — reassuring on heavy calls." },
  Kore:   { tone: "Clear & neutral",       vibe: "Crisp diction, no edge — defaults are safe here." },
  Fenrir: { tone: "Energetic & rugged",    vibe: "Confident, slightly gruff — fits trades, workshops." },
  Leda:   { tone: "Soft & conversational", vibe: "Easy on the ear over long calls. Therapeutic vibe." },
  Orus:   { tone: "Measured & formal",     vibe: "Precise, polite — legal, healthcare, premium hotels." },
  Zephyr: { tone: "Light & breezy",        vibe: "Casual, modern. Good for D2C, lifestyle, beauty." },
};

// Render a voice as "Tone (Name)" — the attribute leads, the Greek name
// trails in brackets. Falls back to "{name} voice" if we don't have a tone
// entry yet (custom voices, unknown ids), so the UI never shows a bare ID.
function voiceTag(id) {
  if (!id) return "";
  const t = VOICE_TONES[id]?.tone;
  return t ? `${t} (${id})` : `${id} voice`;
}

// Single restrained audio motif used in both the hero and the tone cards.
// A minimal sound-wave glyph — animates only when the corresponding voice is
// playing. Keeps the page monochrome / professional instead of a paint set.
function AudioMark({ playing }) {
  return html`
    <svg class=${"db-audio-mark" + (playing ? " is-playing" : "")} viewBox="0 0 28 28" aria-hidden="true">
      <rect x="3"  y="11" width="2.4" height="6"  rx="1.2"/>
      <rect x="8"  y="8"  width="2.4" height="12" rx="1.2"/>
      <rect x="13" y="5"  width="2.4" height="18" rx="1.2"/>
      <rect x="18" y="8"  width="2.4" height="12" rx="1.2"/>
      <rect x="23" y="11" width="2.4" height="6"  rx="1.2"/>
    </svg>
  `;
}

function AgentVoicePage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  const voices = presets?.voices || [];
  const locales = presets?.locales || [];
  const [draft, setDraft] = useState({
    voice: agent.voice || "Aoede",
    locale: agent.locale || "en-IN",
    greeting: agent.greeting || "",   // build 195: greeting editable here too
    voice_tweaks: {
      temperature: agent.voice_tweaks?.temperature ?? 0.7,
      top_p: agent.voice_tweaks?.top_p ?? 0.9,
      sensitivity: agent.voice_tweaks?.sensitivity ?? "balanced",
      ...(agent.voice_tweaks || {}),
    },
  });
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const [state, setState] = useState({ msg: "", cls: "" });
  const [showAdvanced, setShowAdvanced] = useState(false);
  // Live audio preview — single shared <audio> element so playing one voice
  // automatically stops the previous. Samples live at /static/voice-samples/
  // {voice}.mp3; we treat a 404 / network error as "uploading soon" so the
  // page degrades gracefully when the asset isn't there yet.
  const audioRef = useRef(null);
  const [playing, setPlaying] = useState(null);   // voice id currently playing
  const [previewError, setPreviewError] = useState(null);

  // Eva's pick is whatever the agent currently has. When the user picks a
  // different one, we remember Eva's original so we can show "back to Eva's
  // pick" affordance — small but meaningful for trust.
  const evaPick = agent.voice || "Aoede";
  const setTweak = (k, v) => setDraft((d) => ({ ...d, voice_tweaks: { ...d.voice_tweaks, [k]: v } }));

  const stopPreview = () => {
    setPlaying(null);
    if (audioRef.current) {
      try { audioRef.current.pause(); audioRef.current.currentTime = 0; } catch {}
    }
  };
  const playPreview = (voiceId) => {
    setPreviewError(null);
    if (playing === voiceId) { stopPreview(); return; }
    if (!audioRef.current) audioRef.current = new Audio();
    const a = audioRef.current;
    a.onended = () => setPlaying(null);
    a.onerror = () => {
      setPlaying(null);
      setPreviewError(`Sample for ${voiceId} isn't available yet — try "Hear in a real call" below.`);
    };
    a.src = `/static/voice-samples/${voiceId}.wav`;
    a.currentTime = 0;
    setPlaying(voiceId);
    a.play().catch(() => {
      setPlaying(null);
      setPreviewError(`Couldn't play ${voiceId}'s sample — try "Hear in a real call" below.`);
    });
  };
  // Stop audio on unmount + when the user navigates away mid-preview.
  useEffect(() => () => stopPreview(), []);

  const selectVoice = (voiceId) => {
    // Preview and pick are intentionally separate actions: the ▶ button on
    // each card auditions a voice without committing, the Pick button
    // commits without auto-playing. So you can sample several voices, decide
    // which feels right, then apply — no surprise audio when you click Pick.
    setDraft((d) => ({ ...d, voice: voiceId }));
  };

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  const selectedTone = VOICE_TONES[draft.voice]?.tone || "—";
  const selectedVibe = VOICE_TONES[draft.voice]?.vibe || "";

  // Build 195: reference-design adaptation. Avatar initial = first letter
  // of the voice name; colour derived by hashing the name to one of the
  // dashboard's pastels. Tags = locale + tone descriptor split + perceived
  // gender (which the voice-set lookup gives us for free since build 187).
  const VOICE_AVATAR_PALETTE = [
    "#3b82f6", "#a855f7", "#10b981", "#f59e0b",
    "#ec4899", "#06b6d4", "#ef4444", "#6366f1",
  ];
  const avatarColor = (name) => {
    let h = 0;
    const s = String(name || "");
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return VOICE_AVATAR_PALETTE[h % VOICE_AVATAR_PALETTE.length];
  };
  const voiceGender = (id) => {
    if (["Aoede", "Leda", "Kore", "Zephyr"].includes(id)) return "Female voice";
    if (["Charon", "Fenrir", "Puck", "Orus"].includes(id)) return "Male voice";
    return "Voice";
  };
  // Tone like "Warm & friendly" → ["Warm", "friendly"]. Together with the
  // locale label + perceived gender that gives us the same 3-4 chip strip
  // the reference shows next to its voice avatar.
  const toneTags = (id) => {
    const t = VOICE_TONES[id]?.tone || "";
    return t.split(/&|,/).map((s) => s.trim()).filter(Boolean);
  };
  const currentLocaleLabel = (locales.find((l) => l.id === draft.locale)?.label) || draft.locale;
  const currentVoiceTags = [currentLocaleLabel, ...toneTags(draft.voice)];
  const greetingLimit = 240;
  const greetingLen = (draft.greeting || "").length;

  const body = html`
    <div class="db-overview">
      <!-- Voice picker — adapted from the CEO's reference design.
           Two dropdowns up top (language / voice), preview card with
           avatar + tags + Play Sample, then the greeting message with
           a character counter. Replaces the build-178 hero + 8-card
           grid which were over-busy for what's really a 2-decision
           page: which language, which voice. The 8-card grid lives
           below as an optional "Explore all voices" expander. -->
      <section class="db-panel vs-panel">
        <div class="vs-twocol">
          <label class="db-form-field">
            <span class="db-form-label">Language</span>
            <select class="db-input vs-select" value=${draft.locale}
                    onChange=${(e) => { stopPreview(); set("locale", e.target.value); }}>
              ${locales.map((l) => html`<option key=${l.id} value=${l.id}>${l.label}</option>`)}
            </select>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Voice</span>
            <select class="db-input vs-select" value=${draft.voice}
                    onChange=${(e) => { stopPreview(); selectVoice(e.target.value); }}>
              ${voices.map((v) => {
                const meta = VOICE_TONES[v.id] || {};
                return html`<option key=${v.id} value=${v.id}>${v.id} — ${meta.tone || "neutral"}</option>`;
              })}
            </select>
          </label>
        </div>

        <div class="vs-card">
          <div class="vs-card-left">
            <div class="vs-avatar" style=${{ background: avatarColor(draft.voice) }}>
              ${(draft.voice || "?").charAt(0).toUpperCase()}
            </div>
            <div class="vs-info">
              <div class="vs-name">${draft.voice}</div>
              <div class="vs-meta">${voiceGender(draft.voice)} · ${selectedTone}</div>
              <div class="vs-tags">
                ${currentVoiceTags.map((t, i) => html`<span key=${i} class="vs-tag">${t}</span>`)}
              </div>
              ${selectedVibe ? html`<p class="vs-vibe">${selectedVibe}</p>` : ""}
            </div>
          </div>
          <button class="vs-play db-btn-primary" type="button" onClick=${() => playPreview(draft.voice)}>
            ${playing === draft.voice
              ? html`<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg><span>Stop</span>`
              : html`<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><polygon points="6,4 20,12 6,20"/></svg><span>Play Sample</span>`}
          </button>
        </div>
        ${previewError ? html`<div class="vs-warn">${previewError}</div>` : ""}
        ${draft.voice !== evaPick ? html`
          <button class="db-btn-ghost db-btn-sm vs-reset" type="button"
                  onClick=${() => { set("voice", evaPick); stopPreview(); }}>
            Back to Eva's pick (${evaPick})
          </button>
        ` : ""}

        <label class="db-form-field vs-greeting-field">
          <span class="db-form-label">Greeting Message</span>
          <textarea class="db-input vs-greeting" rows="3"
                    maxlength=${greetingLimit}
                    value=${draft.greeting}
                    onInput=${(e) => set("greeting", e.target.value)}
                    placeholder="Welcome to ${agent.variables?.business_name || agent.name}. I am ${agent.name}, how can I help you today?"></textarea>
          <span class="vs-counter">${greetingLen}/${greetingLimit} characters</span>
        </label>
      </section>

      <!-- Tone explorer kept but folded into an optional expander so the
           page stays clean for the common "pick from dropdown + listen"
           flow. Operators who want to compare the 8 voices side-by-side
           open this — same picker mechanics as before. -->
      <details class="db-panel vs-explore">
        <summary>
          <span>Explore all 8 voices</span>
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
        </summary>
        <p class="db-panel-sub">Every voice speaks the same ${VOICE_TOTAL_LANGS} languages — this is purely about how ${agent.name} sounds. Click any tone to hear it. Click again to stop.</p>
        <div class="db-tone-grid">
          ${voices.map((v) => {
            const meta = VOICE_TONES[v.id] || {};
            const isActive = draft.voice === v.id;
            const isPlaying = playing === v.id;
            return html`
              <div key=${v.id} class=${"db-tone-card" + (isActive ? " active" : "") + (isPlaying ? " playing" : "")}>
                <div class="db-tone-body">
                  <div class="db-tone-headline">${meta.tone || v.id}</div>
                  <div class="db-tone-voice">${v.id}</div>
                  <div class="db-tone-vibe">${meta.vibe || ""}</div>
                </div>
                <div class="db-tone-actions">
                  <button type="button" class="db-tone-listen" aria-label=${`Listen to ${v.id}`}
                          onClick=${(e) => { e.stopPropagation(); playPreview(v.id); }}>
                    ${isPlaying
                      ? html`<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>`
                      : html`<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><polygon points="6,4 20,12 6,20"/></svg>`}
                  </button>
                  <button type="button" class=${"db-tone-pick" + (isActive ? " active" : "")}
                          onClick=${() => selectVoice(v.id)}>
                    ${isActive ? "Selected" : "Pick"}
                  </button>
                </div>
              </div>
            `;
          })}
        </div>
        <p class="db-voice-hint">
          Want to hear ${draft.voice} in a real conversation? <button class="db-link" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/test-call`)}>Send yourself a test call →</button>
        </p>
      </details>

      <!-- Background ambience (Beta) — plays a low-volume loop behind the
           voice so the call doesn't sound like Eva's in a vacuum. Choice
           defaults to whatever fits the agent's sector. Quality and stability
           are explicitly Beta — synthesised loops, not licensed recordings. -->
      <section class="db-panel">
        <h3 class="db-panel-title">
          Background ambience
          <span class="db-beta-tag">Beta</span>
        </h3>
        <p class="db-panel-sub">A subtle ambient loop plays underneath ${agent.name}'s voice so callers don't feel like they're talking to nothing. Picking the right vibe ("office" for support, "café" for a restaurant) buys a surprising amount of presence. Synthesised loops in this release — sourced studio recordings come in v2.</p>
        <div class="db-form-grid-2">
          <label class="db-form-field">
            <span class="db-form-label">Ambience</span>
            <select class="db-input"
                    value=${draft.voice_tweaks.ambience ?? "default"}
                    onChange=${(e) => setTweak("ambience", e.target.value === "default" ? null : e.target.value)}>
              <option value="default">Default for ${(presets?.sectors || []).find((s) => s.id === agent.sector)?.label || "this agent"}</option>
              <option value="off">Off — silence</option>
              <option value="office">Office — light chatter + soft keys</option>
              <option value="busy_office">Busy office — high chatter / call-centre</option>
              <option value="clinic">Clinic — quiet medical office</option>
              <option value="cafe">Café — restaurant / hotel buzz</option>
              <option value="workshop">Workshop — garage / industrial</option>
              <option value="quiet">Quiet — minimal HVAC only</option>
            </select>
            <span class="db-form-help">Default for ${agent.name}: ${(SECTOR_AMBIENCE_LABEL[(agent.sector || "")] || "office")} (based on her industry).</span>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Volume <span class="db-form-opt">${Math.round(((draft.voice_tweaks.ambience_volume ?? 0.18) * 100))}%</span></span>
            <input class="db-range" type="range" min="0" max="0.5" step="0.01"
                   value=${draft.voice_tweaks.ambience_volume ?? 0.18}
                   onInput=${(e) => setTweak("ambience_volume", parseFloat(e.target.value))} />
            <span class="db-form-help">Anything above ~25% will compete with the voice. Keep it subtle.</span>
          </label>
        </div>
      </section>

      <!-- Advanced collapsed by default — these knobs rarely matter for end
           users and they're easy to break a call with. Behind a single click. -->
      <details class="db-panel db-voice-advanced" open=${showAdvanced}
               onToggle=${(e) => setShowAdvanced(e.target.open)}>
        <summary>
          <span>Advanced — conversation feel</span>
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
        </summary>
        <p class="db-panel-sub">Higher temperature = more creative phrasing. Lower = more on-script. Defaults work for most cases.</p>
        <div class="db-form">
          <label class="db-form-field">
            <span class="db-form-label">Temperature <span class="db-form-opt">${draft.voice_tweaks.temperature.toFixed(2)}</span></span>
            <input class="db-range" type="range" min="0" max="1.5" step="0.05"
                   value=${draft.voice_tweaks.temperature}
                   onInput=${(e) => setTweak("temperature", parseFloat(e.target.value))} />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Top-p <span class="db-form-opt">${draft.voice_tweaks.top_p.toFixed(2)}</span></span>
            <input class="db-range" type="range" min="0" max="1" step="0.05"
                   value=${draft.voice_tweaks.top_p}
                   onInput=${(e) => setTweak("top_p", parseFloat(e.target.value))} />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Interrupt sensitivity</span>
            <select class="db-input" value=${draft.voice_tweaks.sensitivity}
                    onChange=${(e) => setTweak("sensitivity", e.target.value)}>
              <option value="low">Low — let the caller finish</option>
              <option value="balanced">Balanced (default)</option>
              <option value="high">High — react quickly</option>
            </select>
          </label>
        </div>
      </details>
    </div>
  `;
  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save voice settings</button>
  `;
  return html`
    <${DashboardShell}
      activeKey="voice-settings"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Voice settings"
      subtitle=${`Eva picked a voice for ${agent.name} — adjust the tone if you'd prefer something else.`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Guardrails — single-focus page. Three blocks:
//   (1) Defaults always-on   — brand-safety floor, not configurable
//   (2) Do's     (toggles)   — behaviours the agent should adopt
//   (3) Don'ts   (toggles)   — extra hard limits
// State persists into `agent.policy` (JSON) via the existing PATCH endpoint.
// PhoneAIConventionsPanel — read-only transparency for the systemic
// speech / silence / sector playbook that EVERY Eva-built agent inherits
// at runtime. Backed by /api/agents/{id}/conventions; collapsed by default
// so it doesn't crowd the editable Guardrails above.
function PhoneAIConventionsPanel({ agent }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/agents/${agent.id}/conventions`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [agent?.id]);
  if (err) return html`<section class="db-panel"><p class="db-form-help">Couldn't load conventions: ${err}</p></section>`;
  if (!data) return "";
  return html`
    <section class="db-panel">
      <button class="db-conv-head" type="button" onClick=${() => setOpen((v) => !v)}>
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
          <path d="M3 12h18M3 6h18M3 18h12"/>
        </svg>
        <div class="db-conv-head-meta">
          <h3 class="db-panel-title">Phone AI conventions <span class="db-panel-pill">Systemic</span></h3>
          <p class="db-panel-sub">Speech, silence, and a ${(data.sector_playbook?.length || 0) > 0 ? agent.sector || "sector" : "generic"} playbook auto-applied to every call — locale ${data.locale}, currency ${data.currency_spoken}, ${data.timezone}. <span class="db-conv-toggle">${open ? "Hide" : "Show details"}</span></p>
        </div>
      </button>
      ${open ? html`
        <div class="db-conv-body">
          <div class="db-conv-grid">
            <div class="db-conv-card">
              <div class="db-conv-card-title"><span aria-hidden="true">🌐</span> Locale defaults</div>
              <ul class="db-conv-meta">
                <li><b>Currency spoken as:</b> ${data.currency_spoken}</li>
                <li><b>Date pattern:</b> ${data.date_pattern}</li>
                <li><b>Time zone:</b> ${data.timezone}</li>
              </ul>
            </div>
            <div class="db-conv-card">
              <div class="db-conv-card-title"><span aria-hidden="true">🗣️</span> Speech & formatting</div>
              <ul class="db-conv-list">
                ${(data.speech_rules || []).map((r, i) => html`<li key=${i}>${r}</li>`)}
              </ul>
            </div>
            <div class="db-conv-card">
              <div class="db-conv-card-title"><span aria-hidden="true">⏸️</span> Silence & turn-taking</div>
              <ul class="db-conv-list">
                ${(data.silence_rules || []).map((r, i) => html`<li key=${i}>${r}</li>`)}
              </ul>
            </div>
            <div class="db-conv-card db-conv-card-wide">
              <div class="db-conv-card-title"><span aria-hidden="true">🎯</span> Sector playbook — ${agent.sector || "generic"}</div>
              ${(data.sector_playbook || []).length > 0
                ? html`<ul class="db-conv-list">
                    ${data.sector_playbook.map((r, i) => html`<li key=${i}>${r}</li>`)}
                  </ul>`
                : html`<p class="db-form-help">No sector-specific playbook for this agent. Standard conventions apply.</p>`}
            </div>
          </div>
          <p class="db-conv-foot">These conventions are baked into the runtime system prompt automatically — you don't need to copy them into the agent's persona or knowledge. To override for a specific case, edit the agent's <b>System prompt</b> on the Persona & tone page.</p>
        </div>
      ` : ""}
    </section>
  `;
}

function AgentGuardrailsPage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  // Catalogue — single source of truth. Each item has a stable id, a short
  // label, an explanation, and a per-section bucket.
  // Build 210 — gender-neutral phrasing in the static catalogue. The
  // catalogue is module-scope (no `agent` in scope to feed to
  // `pronouns()`), so we lean on "the agent" / passive voice so the
  // copy reads correctly for every agent regardless of name/voice.
  const DEFAULTS = [
    { id: "no_med_legal_fin",  label: "No medical, legal, or financial advice", help: "Defers to a clinician / lawyer / advisor instead of guessing." },
    { id: "no_pii_aloud",      label: "Won't read card numbers, OTPs or passwords aloud", help: "Hard rule. Also won't ask the caller to read theirs back." },
    { id: "human_handoff",     label: "Hands off to a human if the caller asks twice", help: "Offers to text you on WhatsApp / leave a callback." },
  ];
  const DOS = [
    { id: "confirm_booking",   label: "Repeat back the booking time before confirming",     help: "Tightens accuracy — \"so that's Friday 3 PM, is that right?\"" },
    { id: "sms_recap",         label: "Send an SMS recap after every booking",              help: "Caller gets a thread to refer back to." },
    { id: "language_match",    label: "Switch to the caller's language if detected",        help: "Hindi caller on a Hindi-English agent? The agent switches mid-call." },
    { id: "offer_transcript",  label: "Offer to email a transcript at end of call",         help: "Especially useful for support / intake calls." },
    { id: "name_caller",       label: "Use the caller's name once captured",                help: "Small thing, big warmth boost." },
  ];
  const DONTS = [
    { id: "no_price_promise",  label: "Don't quote prices that aren't in the knowledge base", help: "She'll say \"let me have someone get back to you\" instead." },
    { id: "no_delivery_eta",   label: "Don't promise specific delivery / arrival times",      help: "Avoids one of the top reasons callers get angry later." },
    { id: "no_competitors",    label: "Don't discuss competitors by name",                    help: "Neutral redirect to your own services." },
    { id: "no_after_hours",    label: "Don't accept bookings outside business hours",         help: "She'll offer the next available slot in-hours." },
    { id: "no_phone_payment",  label: "Don't process payments over the phone",                help: "She'll text a secure payment link instead." },
  ];

  // Initial state — pulled off `agent.policy` (existing JSON column). Defaults
  // are always on regardless of stored state.
  const stored = (typeof agent.policy === "object" && agent.policy) ? agent.policy : {};
  const initial = {
    dos: Object.fromEntries(DOS.map((d) => [d.id, stored?.dos?.[d.id] ?? ["confirm_booking", "sms_recap", "language_match"].includes(d.id)])),
    donts: Object.fromEntries(DONTS.map((d) => [d.id, stored?.donts?.[d.id] ?? ["no_price_promise", "no_phone_payment"].includes(d.id)])),
    custom_dos:   stored?.custom_dos   || "",
    custom_donts: stored?.custom_donts || "",
  };
  const [policy, setPolicy] = useState(initial);
  const [saveState, setSaveState] = useState({ msg: "", cls: "" });

  const toggle = (bucket, id) => setPolicy((p) => ({
    ...p,
    [bucket]: { ...p[bucket], [id]: !p[bucket][id] },
  }));

  const save = async () => {
    setSaveState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setSaveState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setSaveState({ msg: "", cls: "" }), 2200);
    } catch {
      setSaveState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  const actions = html`
    <${SaveStatePill} state=${saveState.msg ? saveState : null} />
    <button class="db-btn-primary" type="button" onClick=${save}>Save guardrails</button>
  `;

  const body = html`
    <div class="db-overview">
      <!-- Top row: Do's and Don'ts side-by-side. These are the two columns
           the user actually edits; keeping them at eye level (and in opposing
           positions, like a checklist) makes the binary "what she does / what
           she doesn't" trade-off feel immediate. -->
      <div class="db-guardrails-cols">
        <section class="db-panel">
          <div class="db-channel-head">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>
            <div>
              <h3 class="db-panel-title">Do's — habits ${agent.name} adopts</h3>
              <p class="db-panel-sub">Behaviours that make calls feel polished. Toggle the ones you want.</p>
            </div>
          </div>
          <ul class="db-rules">
            ${DOS.map((d) => html`
              <li key=${d.id} class=${policy.dos[d.id] ? "is-on" : ""}>
                <button class="db-rule-toggle" type="button" role="switch" aria-checked=${policy.dos[d.id]} aria-label=${d.label} onClick=${() => toggle("dos", d.id)}>
                  <span class="db-rule-toggle-thumb"></span>
                </button>
                <div class="db-rule-body">
                  <div class="db-rule-label">${d.label}</div>
                  <div class="db-rule-help">${d.help}</div>
                </div>
              </li>
            `)}
          </ul>
          <label class="db-form-field" style=${{ marginTop: 12 }}>
            <span class="db-form-label">Add your own <span class="db-form-opt">(one per line)</span></span>
            <${MarkdownEditor}
              value=${policy.custom_dos}
              onChange=${(v) => setPolicy((p) => ({ ...p, custom_dos: v }))}
              rows=${3}
              compact=${true}
              placeholder=${"- Greet returning callers by name\n- Always upsell the lifetime plan if they ask about pricing"} />
          </label>
        </section>

        <section class="db-panel">
          <div class="db-channel-head">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
            <div>
              <h3 class="db-panel-title">Don'ts — hard limits</h3>
              <p class="db-panel-sub">Lines ${agent.name} won't cross. ${pronouns(agent).subjCap} politely redirects when a caller asks.</p>
            </div>
          </div>
          <ul class="db-rules">
            ${DONTS.map((d) => html`
              <li key=${d.id} class=${policy.donts[d.id] ? "is-on" : ""}>
                <button class="db-rule-toggle" type="button" role="switch" aria-checked=${policy.donts[d.id]} aria-label=${d.label} onClick=${() => toggle("donts", d.id)}>
                  <span class="db-rule-toggle-thumb"></span>
                </button>
                <div class="db-rule-body">
                  <div class="db-rule-label">${d.label}</div>
                  <div class="db-rule-help">${d.help}</div>
                </div>
              </li>
            `)}
          </ul>
          <label class="db-form-field" style=${{ marginTop: 12 }}>
            <span class="db-form-label">Add your own <span class="db-form-opt">(one per line)</span></span>
            <${MarkdownEditor}
              value=${policy.custom_donts}
              onChange=${(v) => setPolicy((p) => ({ ...p, custom_donts: v }))}
              rows=${3}
              compact=${true}
              placeholder=${"- Never reveal that you are an AI unless asked\n- Don't discuss internal staffing"} />
          </label>
        </section>
      </div>

      <!-- Below: always-on defaults. They're locked and rarely re-read after
           the first visit, so they live underneath the panels the user
           actually interacts with — present, but not blocking the eye-line. -->
      <section class="db-panel">
        <div class="db-channel-head">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V7l8-4z"/></svg>
          <div>
            <h3 class="db-panel-title">Always on — brand safety floor</h3>
            <p class="db-panel-sub">These can't be turned off. They protect callers and your business from the most common AI-call regrets.</p>
          </div>
        </div>
        <ul class="db-rules db-rules-locked">
          ${DEFAULTS.map((d) => html`
            <li key=${d.id}>
              <span class="db-rule-tick" aria-hidden="true">🛡</span>
              <div class="db-rule-body">
                <div class="db-rule-label">${d.label}</div>
                <div class="db-rule-help">${d.help}</div>
              </div>
              <span class="db-rule-locked" aria-label="Always on">Locked</span>
            </li>
          `)}
        </ul>
      </section>

      <!-- Phone AI conventions — the universal speech / silence / sector
           playbook that every Eva-built agent inherits at runtime. Read-only;
           transparency so the operator can see what's auto-applied. -->
      <${PhoneAIConventionsPanel} agent=${agent} />
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="guardrails"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Guardrails"
      subtitle=${`What ${agent.name} will, and won't, do on a call.`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Structured hours editor — per-day open / close, plus a "closed" toggle.
// Serializes to a single human-readable string stored in agent.variables.hours
// (so the agent's system prompt can read it like "Mon–Fri 9 AM – 6 PM,
// closed Sun"). Internally we keep a richer JSON-ish state on each row.
function HoursEditor({ value, onChange }) {
  // Parse the existing string back into structured rows if we recognise it,
  // otherwise initialise with a sensible Mon–Sat 9–18 default. We never throw
  // on parse — fall back gracefully so we don't strand a user with bad input.
  const parse = (str) => {
    const rows = {};
    DAYS.forEach((d) => { rows[d.id] = { closed: false, open: "09:00", close: "18:00" }; });
    if (typeof str === "string") {
      for (const line of str.split(/\n+/)) {
        const m = line.match(/^\s*(mon|tue|wed|thu|fri|sat|sun)\s*[:=]\s*(closed|(\d{2}:\d{2})\s*[-–]\s*(\d{2}:\d{2}))\s*$/i);
        if (m) {
          const id = m[1].toLowerCase();
          if (m[2].toLowerCase() === "closed") rows[id] = { closed: true, open: "", close: "" };
          else rows[id] = { closed: false, open: m[3], close: m[4] };
        }
      }
    }
    return rows;
  };
  const [rows, setRows] = useState(() => parse(value));

  // Serialise back to the simple "mon: 09:00-18:00\nsun: closed" string the
  // backend already stores, so the system prompt rendering doesn't change.
  const serialize = (next) => DAYS.map((d) => {
    const r = next[d.id];
    if (r.closed) return `${d.id}: closed`;
    return `${d.id}: ${r.open}-${r.close}`;
  }).join("\n");

  const update = (id, patch) => {
    const next = { ...rows, [id]: { ...rows[id], ...patch } };
    setRows(next);
    onChange && onChange(serialize(next));
  };

  return html`
    <div class="db-hours-grid">
      ${DAYS.map((d) => {
        const r = rows[d.id];
        return html`
          <div class="db-hours-row" key=${d.id}>
            <span class="db-hours-day">${d.label}</span>
            <label class="db-hours-closed">
              <input type="checkbox" checked=${r.closed} onChange=${(e) => update(d.id, { closed: e.target.checked })} />
              <span>Closed</span>
            </label>
            <input class="db-input db-hours-time" type="time" disabled=${r.closed}
                   value=${r.open || "09:00"} onInput=${(e) => update(d.id, { open: e.target.value })} />
            <span class="db-hours-sep">–</span>
            <input class="db-input db-hours-time" type="time" disabled=${r.closed}
                   value=${r.close || "18:00"} onInput=${(e) => update(d.id, { close: e.target.value })} />
          </div>
        `;
      })}
    </div>
  `;
}

// Business profile — the canonical place for everything Eva (or you) tells
// the agent about the underlying business. Common fields up top, location +
// hours, sector-adaptive fields based on agent.sector (e.g. cuisine for a
// restaurant, neighborhoods for real-estate), and a current-offers section
// with sector-aware placeholders. Replaces the old "Business profile"
// section that lived inside the Overview edit drawer.
function AgentProfilePage({ agent, agents, presets, plan, onNav, refreshAgent, org }) {
  const [vars, setVars] = useState(() => ({ ...(agent.variables || {}) }));
  const [sector, setSector] = useState(agent.sector || "");
  const [locale, setLocale] = useState(agent.locale || "");
  const [state, setState] = useState({ msg: "", cls: "" });
  const sectors = presets?.sectors || [];
  const locales = presets?.locales || [];

  const setVar = (k, v) => setVars((p) => {
    const next = { ...p };
    if (v === "" || v == null) delete next[k];
    else next[k] = v;
    return next;
  });

  // Default the country from the org if the agent doesn't have one set yet —
  // saves the user from typing the same country into every new agent.
  const country = vars.country || (org?.country || "");
  useEffect(() => {
    if (!vars.country && org?.country) setVar("country", org.country);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org?.country]);

  const sectorSchema = SECTOR_PROFILE_SCHEMA[sector];

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sector, locale, variables: vars }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  // Per-section completeness — shown as "N/M filled" next to each accordion
  // header so the user can scan which sections still want attention without
  // expanding them.
  const filled = (v) => !!(v != null && String(v).trim());
  const aboutFilled = [vars.business_name, sector, vars.industry, vars.languages, locale].filter(filled).length;
  const locationFilled = [country, vars.city, vars.address, vars.timezone, vars.hours].filter(filled).length;
  const channelsFilled = [vars.website, vars.phone, vars.email, vars.services].filter(filled).length;
  const sectorFilled = sectorSchema
    ? sectorSchema.fields.filter((f) => filled(vars[`${sector}_${f.key}`])).length
    : 0;
  const sectorTotal = sectorSchema?.fields.length || 0;
  const offersFilled = filled(vars.offers) ? 1 : 0;

  // Render an accordion with a pill, title, sub copy. Open=true keeps it
  // expanded; the first one is open by default so the user lands on something.
  const Accord = (key, title, pillText, sub, content, defaultOpen = false) => html`
    <details class="db-accord" key=${key} open=${defaultOpen}>
      <summary class="db-accord-head">
        <span class="db-accord-title">${title}</span>
        ${pillText ? html`<span class="db-accord-pill">${pillText}</span>` : ""}
        <svg class="db-accord-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
      </summary>
      <div class="db-accord-body">
        ${sub ? html`<p class="db-accord-sub">${sub}</p>` : ""}
        ${content}
      </div>
    </details>
  `;

  const aboutSection = Accord("about", "About the business", `${aboutFilled}/5`,
    html`${agent.name} is an <strong>inbound</strong> phone agent — these details are what ${pronouns(agent).subj}'ll lean on when callers ask.`,
    html`
      <div class="db-form-grid-2">
        <label class="db-form-field">
          <span class="db-form-label">Business name</span>
          <input class="db-input" type="text" value=${vars.business_name || ""}
                 placeholder="e.g. BrightSmile Dental"
                 onInput=${(e) => setVar("business_name", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Industry</span>
          <select class="db-input" value=${sector} onChange=${(e) => setSector(e.target.value)}>
            <option value="">—</option>
            ${sectors.map((s) => html`<option key=${s.id} value=${s.id}>${s.label}</option>`)}
          </select>
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Specialty <span class="db-form-opt">(narrower than industry)</span></span>
          <input class="db-input" type="text" value=${vars.industry || ""}
                 placeholder="e.g. Family dentistry, B2B SaaS"
                 onInput=${(e) => setVar("industry", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Languages spoken</span>
          <input class="db-input" type="text" value=${vars.languages || ""}
                 placeholder="English, Hindi, Kannada"
                 onInput=${(e) => setVar("languages", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Accent / locale</span>
          <select class="db-input" value=${locale} onChange=${(e) => setLocale(e.target.value)}>
            <option value="">—</option>
            ${locales.map((l) => html`<option key=${l.id} value=${l.id}>${l.label}</option>`)}
          </select>
        </label>
      </div>
    `,
    true,   // open by default
  );

  const locationSection = Accord("location", "Location & hours", `${locationFilled}/5`,
    html`Where ${agent.name} answers from. The country also drives invoices + integration filtering — change your <button class="db-link-btn" type="button" onClick=${() => onNav && onNav("/account/org")}>org's country</button> if it's different.`,
    html`
      <div class="db-form-grid-2">
        <label class="db-form-field">
          <span class="db-form-label">Country</span>
          <select class="db-input" value=${country}
                  onChange=${(e) => setVar("country", e.target.value)}>
            <option value="">—</option>
            ${COUNTRIES.map((c) => html`<option key=${c.id} value=${c.id}>${c.label}</option>`)}
          </select>
        </label>
        <label class="db-form-field">
          <span class="db-form-label">City</span>
          <input class="db-input" type="text" value=${vars.city || ""}
                 placeholder="e.g. Bengaluru"
                 onInput=${(e) => setVar("city", e.target.value)} />
        </label>
        <label class="db-form-field db-form-span-2">
          <span class="db-form-label">Address</span>
          <input class="db-input" type="text" value=${vars.address || ""}
                 placeholder="Street, area, pin"
                 onInput=${(e) => setVar("address", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Timezone</span>
          <input class="db-input" type="text" value=${vars.timezone || ""}
                 placeholder="e.g. Asia/Kolkata"
                 onInput=${(e) => setVar("timezone", e.target.value)} />
        </label>
      </div>
      <div class="db-form-field" style=${{ marginTop: 16 }}>
        <span class="db-form-label">Business hours</span>
        <${HoursEditor} value=${vars.hours || ""} onChange=${(s) => setVar("hours", s)} />
        <span class="db-form-help">${agent.name} uses these to give "we're open until 9pm" / "we're closed today" answers. Toggle Closed for the days you're shut.</span>
      </div>
    `,
  );

  const channelsSection = Accord("channels", "Channels & contact", `${channelsFilled}/4`,
    html`What to share when callers ask how else they can reach you.`,
    html`
      <div class="db-form-grid-2">
        <label class="db-form-field">
          <span class="db-form-label">Website</span>
          <input class="db-input" type="url" value=${vars.website || ""}
                 placeholder="https://example.com"
                 onInput=${(e) => setVar("website", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Business phone <span class="db-form-opt">(for escalations)</span></span>
          <input class="db-input" type="tel" value=${vars.phone || ""}
                 placeholder="+91 80 1234 5678"
                 onInput=${(e) => setVar("phone", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Contact email</span>
          <input class="db-input" type="email" value=${vars.email || ""}
                 placeholder="hello@example.com"
                 onInput=${(e) => setVar("email", e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Services offered <span class="db-form-opt">(short list)</span></span>
          <input class="db-input" type="text" value=${vars.services || ""}
                 placeholder="Brief list — full menu can live in Knowledge base"
                 onInput=${(e) => setVar("services", e.target.value)} />
        </label>
      </div>
    `,
  );

  const industrySection = sectorSchema ? Accord(`industry-${sector}`, `${sectorSchema.label} details`, `${sectorFilled}/${sectorTotal}`,
    html`Sector-specific context. ${agent.name} uses these to answer questions only a ${sectorSchema.label.toLowerCase()} business would get.`,
    html`
      <div class="db-form-grid-2">
        ${sectorSchema.fields.map((f) => {
          const storeKey = `${sector}_${f.key}`;
          const v = vars[storeKey] || "";
          return html`
            <label class=${"db-form-field" + (f.type === "textarea" ? " db-form-span-2" : "")} key=${storeKey}>
              <span class="db-form-label">${f.label}</span>
              ${f.type === "textarea" ? html`
                <textarea class="db-textarea" rows="2" placeholder=${f.placeholder}
                          value=${v} onInput=${(e) => setVar(storeKey, e.target.value)}></textarea>
              ` : html`
                <input class="db-input" type="text" placeholder=${f.placeholder}
                       value=${v} onInput=${(e) => setVar(storeKey, e.target.value)} />
              `}
            </label>
          `;
        })}
      </div>
    `,
  ) : "";

  const offersSection = Accord("offers", "Current offers & specials", offersFilled ? "Set" : "Empty",
    html`${agent.name} mentions these when a caller asks "anything on right now?" or as a soft upsell.`,
    html`
      <label class="db-form-field">
        <${MarkdownEditor}
          value=${vars.offers || ""}
          onChange=${(v) => setVar("offers", v)}
          rows=${4}
          compact=${true}
          placeholder=${sectorSchema?.offers_examples || "- 20% off first booking\n- Free consultation through Friday"} />
        <span class="db-form-help">Update this as your promotions change — the agent reads the latest text on every call.</span>
      </label>
    `,
  );

  const body = html`
    <div class="db-profile-accords">
      ${aboutSection}
      ${locationSection}
      ${channelsSection}
      ${industrySection}
      ${offersSection}
    </div>
  `;

  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save profile</button>
  `;

  return html`
    <${DashboardShell}
      activeKey="profile"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Business profile"
      subtitle=${`Everything ${agent.name} should know about the business behind the phone.`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Organisation settings — billing + tax entity for the user. One org per
// account today (1:N when team support lands). Country drives invoicing,
// default agent country, and the Integrations filter — so it's worth
// surfacing it as a real first-class page.
function AccountOrgPage({ agents, plan, onNav, org, onOrgChanged }) {
  const [draft, setDraft] = useState(() => ({
    name: org?.name || "",
    country: org?.country || "",
    tax_id: org?.tax_id || "",
    billing_address: org?.billing_address || "",
    currency: org?.currency || "",
    timezone: org?.timezone || "",
  }));
  const [state, setState] = useState({ msg: "", cls: "" });
  useEffect(() => {
    if (org) setDraft({
      name: org.name || "",
      country: org.country || "",
      tax_id: org.tax_id || "",
      billing_address: org.billing_address || "",
      currency: org.currency || "",
      timezone: org.timezone || "",
    });
  }, [org?.id]);

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch("/api/me/org", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      if (!r.ok) throw new Error("server " + r.status);
      const updated = await r.json();
      onOrgChanged && onOrgChanged(updated);
      setState({ msg: "Saved ✓", cls: "ok" });
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  // Hint the country-specific tax-id label. Helps users not stare at a blank
  // "Tax ID" field and wonder which of their three numbers we want.
  const TAX_LABELS = {
    IN: "GSTIN", GB: "VAT number", DE: "USt-IdNr",
    FR: "SIRET / TVA", ES: "NIF", US: "EIN", CA: "BN",
    SG: "GST Reg.", AE: "TRN", AU: "ABN",
  };
  const taxLabel = TAX_LABELS[draft.country] || "Tax ID";

  const body = html`
    <div class="db-overview">
      <section class="db-panel">
        <h3 class="db-panel-title">Workspace</h3>
        <p class="db-panel-sub">Everything in SpiderX.AI lives inside your workspace — its agents, its calls, its team, its billing. Rename it to whatever your customers know you by.</p>
        <label class="db-form-field">
          <span class="db-form-label">Workspace name</span>
          <input class="db-input" type="text" value=${draft.name}
                 placeholder="e.g. Anchor SaaS Pvt Ltd"
                 onInput=${(e) => set("name", e.target.value)} />
        </label>
      </section>

      <section class="db-panel">
        <h3 class="db-panel-title">Tax & billing</h3>
        <p class="db-panel-sub">This is the formal organisation we invoice. Country drives the invoice format, the default for new agent profiles, and which integrations get recommended on the Integrations page. ${taxLabel} is what appears on your invoice header.</p>
        <div class="db-form-grid-2">
          <label class="db-form-field">
            <span class="db-form-label">Country</span>
            <select class="db-input" value=${draft.country} onChange=${(e) => set("country", e.target.value)}>
              <option value="">—</option>
              ${COUNTRIES.map((c) => html`<option key=${c.id} value=${c.id}>${c.label}</option>`)}
            </select>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">${taxLabel}</span>
            <input class="db-input" type="text" value=${draft.tax_id}
                   placeholder=${draft.country === "IN" ? "29ABCDE1234F1Z5" : draft.country === "GB" ? "GB123456789" : "Tax registration number"}
                   onInput=${(e) => set("tax_id", e.target.value)} />
          </label>
          <label class="db-form-field db-form-span-2">
            <span class="db-form-label">Billing address</span>
            <textarea class="db-textarea" rows="3" value=${draft.billing_address}
                      placeholder="Street, area, city, state, postal code"
                      onInput=${(e) => set("billing_address", e.target.value)}></textarea>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Currency <span class="db-form-opt">(invoice display)</span></span>
            <input class="db-input" type="text" value=${draft.currency}
                   placeholder=${draft.country === "IN" ? "INR" : draft.country === "US" ? "USD" : draft.country === "GB" ? "GBP" : "ISO code"}
                   onInput=${(e) => set("currency", e.target.value)} />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Timezone</span>
            <input class="db-input" type="text" value=${draft.timezone}
                   placeholder=${draft.country === "IN" ? "Asia/Kolkata" : "IANA tz name"}
                   onInput=${(e) => set("timezone", e.target.value)} />
          </label>
        </div>
      </section>

      <section class="db-panel">
        <h3 class="db-panel-title">Team</h3>
        <p class="db-panel-sub">
          Invite teammates to share agents under this workspace.
          <button class="db-link-btn" type="button" onClick=${() => onNav && onNav("/account/team")}>
            Manage your team →
          </button>
        </p>
      </section>
    </div>
  `;

  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save workspace</button>
  `;

  return html`
    <${DashboardShell}
      activeKey="org"
      agents=${agents}
      plan=${plan}
      title="Workspace"
      subtitle="Name, billing entity, country defaults."
      actions=${actions}
      onNav=${onNav}
      body=${body}
      hideSidebar=${true}
    />
  `;
}

// Developer page — outcomes, free-form variables, and call-ended webhook live
// here so the Overview edit drawer can stay focused on the business profile.
// One place to wire the agent into engineering plumbing.
function AgentDeveloperPage({ agent, agents, presets, plan, onNav, refreshAgent }) {
  const [draft, setDraft] = useState({
    outcomes: Array.isArray(agent.outcomes) ? agent.outcomes : [],
    variables: (agent.variables && typeof agent.variables === "object") ? agent.variables : {},
    webhook_url: agent.webhook_url || "",
    // Headers stored as rows on the client (preserves order + lets users
    // have an empty pair while typing). Flattened back to an object on save.
    webhook_header_rows: (() => {
      const h = (agent.webhook_headers && typeof agent.webhook_headers === "object") ? agent.webhook_headers : {};
      return Object.entries(h).map(([k, v]) => ({ k, v }));
    })(),
  });
  const [state, setState] = useState({ msg: "", cls: "" });
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));

  // Test-webhook state — fired by the "Send test payload" button.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);   // { ok, status, body, error, url } | null

  // Outcomes chip-editor state — small input you type into and hit enter.
  const [outcomeDraft, setOutcomeDraft] = useState("");
  const addOutcome = () => {
    const v = outcomeDraft.trim().toLowerCase().replace(/[^a-z0-9_]/g, "_");
    if (!v) return;
    if ((draft.outcomes || []).includes(v)) { setOutcomeDraft(""); return; }
    set("outcomes", [...(draft.outcomes || []), v]);
    setOutcomeDraft("");
  };
  const removeOutcome = (v) => set("outcomes", (draft.outcomes || []).filter((x) => x !== v));

  // Non-canonical "custom" variables — kept as rows for the same reason as
  // headers. Canonical (Business-profile) vars are managed elsewhere and
  // merged back on save.
  const [customVarRows, setCustomVarRows] = useState(() => Object.entries(
    (agent.variables && typeof agent.variables === "object") ? agent.variables : {}
  ).filter(([k]) => !CANONICAL_KEYS.has(k)).map(([k, v]) => ({ k, v: String(v) })));
  const addCustomVar = () => setCustomVarRows((rows) => [...rows, { k: "", v: "" }]);
  const updateCustomVar = (i, patch) => setCustomVarRows((rows) => rows.map((r, idx) => idx === i ? { ...r, ...patch } : r));
  const removeCustomVar = (i) => setCustomVarRows((rows) => rows.filter((_, idx) => idx !== i));

  // Header row helpers — same shape as custom vars.
  const addHeader = () => set("webhook_header_rows", [...(draft.webhook_header_rows || []), { k: "", v: "" }]);
  const updateHeader = (i, patch) => set("webhook_header_rows", (draft.webhook_header_rows || []).map((r, idx) => idx === i ? { ...r, ...patch } : r));
  const removeHeader = (i) => set("webhook_header_rows", (draft.webhook_header_rows || []).filter((_, idx) => idx !== i));
  const applyAuthPreset = (preset) => {
    const existing = (draft.webhook_header_rows || []).filter((r) => r.k.toLowerCase() !== "authorization" && r.k.toLowerCase() !== "x-api-key");
    if (preset === "bearer") set("webhook_header_rows", [...existing, { k: "Authorization", v: "Bearer YOUR_TOKEN_HERE" }]);
    else if (preset === "apikey") set("webhook_header_rows", [...existing, { k: "X-API-Key", v: "YOUR_KEY_HERE" }]);
    else if (preset === "basic") set("webhook_header_rows", [...existing, { k: "Authorization", v: "Basic base64(user:pass)" }]);
  };

  // The sample payload — exactly what the server fires in a real call. Kept
  // in sync with /api/agents/{id}/webhook/test on the backend.
  const samplePayload = {
    event: "call.ended",
    agent: { id: agent.id, name: agent.name, slug: agent.slug },
    call: {
      id: "call_2026-05-13_abc123",
      started_at: "2026-05-13T14:22:08+05:30",
      ended_at: "2026-05-13T14:22:55+05:30",
      duration_s: 47.3,
      from: "+91 98XXXXXXXX",
      to: agent?.variables?.phone || "+91 80 1234 5678",
    },
    outcome: (draft.outcomes && draft.outcomes[0]) || "booked",
    reason: "CONVERSATION_COMPLETE",
    summary: "Caller booked a check-up for Friday 3 PM. Confirmed by SMS.",
    extracted: { customer_name: "Test Caller", appointment_at: "2026-05-15T15:00" },
  };
  const samplePayloadJson = JSON.stringify(samplePayload, null, 2);
  const [payloadCopied, setPayloadCopied] = useState(false);
  const copyPayload = async () => {
    try { await navigator.clipboard.writeText(samplePayloadJson); setPayloadCopied(true); setTimeout(() => setPayloadCopied(false), 1800); } catch {}
  };

  const flattenHeaders = () => {
    const out = {};
    for (const r of (draft.webhook_header_rows || [])) {
      const k = (r.k || "").trim();
      const v = (r.v || "").trim();
      if (k) out[k] = v;
    }
    return out;
  };
  const flattenVars = () => {
    const canonical = {};
    for (const [k, v] of Object.entries(agent.variables || {})) {
      if (CANONICAL_KEYS.has(k)) canonical[k] = v;
    }
    const customs = {};
    for (const r of customVarRows) {
      const k = (r.k || "").trim();
      const v = (r.v || "").trim();
      if (k && !CANONICAL_KEYS.has(k)) customs[k] = v;
    }
    return { ...canonical, ...customs };
  };

  const save = async () => {
    setState({ msg: "Saving…", cls: "dim" });
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          outcomes: draft.outcomes || [],
          variables: flattenVars(),
          webhook_url: (draft.webhook_url || "").trim(),
          webhook_headers: flattenHeaders(),
        }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      setState({ msg: "Saved ✓", cls: "ok" });
      refreshAgent && refreshAgent();
      setTimeout(() => setState({ msg: "", cls: "" }), 2200);
    } catch {
      setState({ msg: "Couldn't save — try again", cls: "err" });
    }
  };

  const sendTestPayload = async () => {
    setTesting(true); setTestResult(null);
    try {
      // Save first — the server fires from agent.webhook_url, so we need
      // the current draft persisted. One save + one test is cleaner than
      // sending the draft inline.
      await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          webhook_url: (draft.webhook_url || "").trim(),
          webhook_headers: flattenHeaders(),
        }),
      });
      const r = await fetch(`/api/agents/${agent.id}/webhook/test`, { method: "POST" });
      const data = await r.json();
      if (!r.ok) {
        setTestResult({ ok: false, status: r.status, error: data?.detail?.message || "Test failed" });
      } else {
        setTestResult(data);
      }
    } catch (e) {
      setTestResult({ ok: false, status: 0, error: "Couldn't reach our server — check your connection." });
    } finally {
      setTesting(false);
    }
  };

  const body = html`
    <div class="db-overview">
      <!-- API access — at-a-glance info every integration needs. Lets the
           dev grab the agent ID + a starter curl without leaving the page. -->
      <section class="db-panel">
        <h3 class="db-panel-title">API access</h3>
        <p class="db-panel-sub">Reference info for any integration that talks to ${agent.name}. The agent ID is the canonical handle; the slug is human-friendly and stable across renames.</p>
        <div class="db-dev-kv">
          <div class="db-dev-kv-row">
            <span class="db-dev-kv-key">Agent ID</span>
            <code class="db-dev-kv-val">${agent.id}</code>
            <button class="db-dev-kv-copy" type="button" onClick=${async () => { try { await navigator.clipboard.writeText(String(agent.id)); } catch {} }}>Copy</button>
          </div>
          <div class="db-dev-kv-row">
            <span class="db-dev-kv-key">Slug</span>
            <code class="db-dev-kv-val">${agent.slug || "—"}</code>
            <button class="db-dev-kv-copy" type="button" onClick=${async () => { try { await navigator.clipboard.writeText(agent.slug || ""); } catch {} }}>Copy</button>
          </div>
          <div class="db-dev-kv-row">
            <span class="db-dev-kv-key">Fetch via REST</span>
            <code class="db-dev-kv-val db-dev-kv-mono">curl ${typeof window !== "undefined" ? window.location.origin : "https://app.spiderx.ai"}/api/agents/${agent.id}</code>
            <button class="db-dev-kv-copy" type="button" onClick=${async () => { try { await navigator.clipboard.writeText(`curl ${typeof window !== "undefined" ? window.location.origin : "https://app.spiderx.ai"}/api/agents/${agent.id}`); } catch {} }}>Copy</button>
          </div>
        </div>
      </section>

      <!-- Outcomes — chip editor. Each chip removable; type-and-enter adds. -->
      <section class="db-panel">
        <h3 class="db-panel-title">Outcomes <span class="db-panel-pill">${(draft.outcomes || []).length}</span></h3>
        <p class="db-panel-sub">Buckets the agent picks from at the end of every call. These IDs show up unchanged in the Call logs filter and on the webhook payload. Lowercase, <code>snake_case</code>.</p>
        <div class="db-chip-editor">
          ${(draft.outcomes || []).map((o) => html`
            <span class="db-chip" key=${o}>
              <span>${o}</span>
              <button type="button" class="db-chip-x" aria-label=${`Remove ${o}`} onClick=${() => removeOutcome(o)}>×</button>
            </span>
          `)}
          <input class="db-chip-input" type="text"
                 value=${outcomeDraft}
                 placeholder=${(draft.outcomes || []).length === 0 ? "booked, rescheduled, escalated…" : "+ add"}
                 onInput=${(e) => setOutcomeDraft(e.target.value)}
                 onKeyDown=${(e) => {
                   if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addOutcome(); }
                   else if (e.key === "Backspace" && !outcomeDraft && (draft.outcomes || []).length) {
                     removeOutcome((draft.outcomes || [])[(draft.outcomes || []).length - 1]);
                   }
                 }}
                 onBlur=${() => outcomeDraft.trim() && addOutcome()} />
        </div>
      </section>

      <!-- Call-ended webhook — row-style headers + auth shortcuts + test button. -->
      <section class="db-panel">
        <h3 class="db-panel-title">Call-ended webhook</h3>
        <p class="db-panel-sub">We POST a JSON payload to this URL each time ${agent.name} ends a call. Body includes outcome, summary, extracted fields, and a stable call ID. Retries up to 3× with exponential backoff on 5xx responses.</p>
        <label class="db-form-field">
          <span class="db-form-label">Endpoint URL</span>
          <input class="db-input" type="url"
                 placeholder="https://your-server.com/webhook/call-ended"
                 value=${draft.webhook_url}
                 onInput=${(e) => set("webhook_url", e.target.value)} />
          <span class="db-form-help">HTTPS in production. <code>http://</code> works for local dev only.</span>
        </label>

        <div class="db-form-field">
          <div class="db-dev-headers-head">
            <span class="db-form-label" style=${{ margin: 0 }}>Headers</span>
            <div class="db-dev-auth-presets">
              <span class="db-form-help" style=${{ margin: 0 }}>Quick auth:</span>
              <button type="button" class="db-dev-auth-btn" onClick=${() => applyAuthPreset("bearer")}>Bearer token</button>
              <button type="button" class="db-dev-auth-btn" onClick=${() => applyAuthPreset("apikey")}>API key</button>
              <button type="button" class="db-dev-auth-btn" onClick=${() => applyAuthPreset("basic")}>Basic auth</button>
            </div>
          </div>
          ${(draft.webhook_header_rows || []).length === 0 ? html`
            <div class="db-dev-empty">No headers yet. Use a quick-auth preset above, or add one manually.</div>
          ` : ""}
          ${(draft.webhook_header_rows || []).map((row, i) => html`
            <div class="db-dev-row" key=${i}>
              <input class="db-input db-dev-row-key" type="text" placeholder="Header-Name" value=${row.k}
                     onInput=${(e) => updateHeader(i, { k: e.target.value })} />
              <input class="db-input db-dev-row-val" type="text" placeholder="value" value=${row.v}
                     onInput=${(e) => updateHeader(i, { v: e.target.value })} />
              <button type="button" class="db-dev-row-x" aria-label="Remove header"
                      onClick=${() => removeHeader(i)}>×</button>
            </div>
          `)}
          <button type="button" class="db-dev-add" onClick=${addHeader}>+ Add header</button>
        </div>

        <!-- Sample payload + send-test action — the self-service moment. -->
        <div class="db-form-field">
          <span class="db-form-label">Sample payload</span>
          <pre class="db-dev-payload"><code>${samplePayloadJson}</code></pre>
          <div class="db-dev-test-row">
            <button type="button" class=${"db-btn-ghost " + (payloadCopied ? "is-copied" : "")} onClick=${copyPayload}>
              ${payloadCopied ? "Copied!" : "Copy sample"}
            </button>
            <button type="button" class="db-btn-primary"
                    disabled=${testing || !draft.webhook_url.trim()}
                    onClick=${sendTestPayload}>
              ${testing ? "Sending…" : "Send test payload →"}
            </button>
          </div>
          ${testResult ? html`
            <div class=${"db-dev-test-result " + (testResult.ok ? "is-ok" : "is-err")}>
              <div class="db-dev-test-head">
                <span class="db-dev-test-icon" aria-hidden="true">${testResult.ok ? "✓" : "!"}</span>
                <span class="db-dev-test-title">
                  ${testResult.ok ? `Endpoint responded ${testResult.status}` : (testResult.error || `Failed${testResult.status ? ` (${testResult.status})` : ""}`)}
                </span>
                <button type="button" class="db-dev-test-close" aria-label="Dismiss" onClick=${() => setTestResult(null)}>×</button>
              </div>
              ${testResult.body ? html`<pre class="db-dev-test-body"><code>${testResult.body.slice(0, 600)}</code></pre>` : ""}
            </div>
          ` : ""}
        </div>
      </section>

      <!-- Custom variables — row editor for non-canonical {{key}} substitutions. -->
      <section class="db-panel">
        <h3 class="db-panel-title">Custom variables ${customVarRows.length > 0 ? html`<span class="db-panel-pill">${customVarRows.length}</span>` : ""}</h3>
        <p class="db-panel-sub">Anything you want substituted into the prompt as <code>{{key}}</code>. The Business-profile fields (business name, hours, etc.) are managed in the Overview edit panel — this is for everything else: promo codes, mascot names, internal IDs.</p>
        ${customVarRows.length === 0 ? html`
          <div class="db-dev-empty">No custom variables yet. Add one to substitute into ${agent.name}'s prompt.</div>
        ` : ""}
        ${customVarRows.map((row, i) => html`
          <div class="db-dev-row" key=${i}>
            <input class="db-input db-dev-row-key" type="text" placeholder="key" value=${row.k}
                   onInput=${(e) => updateCustomVar(i, { k: e.target.value })} />
            <input class="db-input db-dev-row-val" type="text" placeholder="value" value=${row.v}
                   onInput=${(e) => updateCustomVar(i, { v: e.target.value })} />
            <button type="button" class="db-dev-row-x" aria-label="Remove variable"
                    onClick=${() => removeCustomVar(i)}>×</button>
          </div>
        `)}
        <button type="button" class="db-dev-add" onClick=${addCustomVar}>+ Add variable</button>
      </section>

      <section class="db-panel">
        <h3 class="db-panel-title">Function calls <span class="db-panel-pill">Coming soon</span></h3>
        <p class="db-panel-sub">Let ${agent.name} call your own functions mid-conversation — look up a record, book a slot, charge a card. We're wiring up an OpenAPI / JSON-Schema flow; ping us if you'd like to be in the first batch.</p>
      </section>
    </div>
  `;

  const actions = html`
    <${SaveStatePill} state=${state.msg ? state : null} />
    <button class="db-btn-primary" onClick=${save}>Save settings</button>
  `;

  return html`
    <${DashboardShell}
      activeKey="developer"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Developer"
      subtitle=${`Webhooks, variables and API access for ${agent.name}.`}
      actions=${actions}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Test call — single-focus page: trigger a web call OR ring the user's phone.
function AgentTestCallPage({ agent, agents, presets, plan, onNav, onTest, onTestPhone }) {
  // Phone-callback state lives here directly so the form has room to breathe
  // alongside the web-call side. We drop the legacy PhoneTestForm chrome —
  // it carries its own heading + sub that compete with the page layout.
  const [num, setNum] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const submitPhone = async (e) => {
    e?.preventDefault();
    if (!num.trim()) return;
    setSubmitting(true);
    try {
      await Promise.resolve(onTestPhone?.(num.trim()));
      setDone(true);
      setTimeout(() => setDone(false), 4000);
    } finally {
      setSubmitting(false);
    }
  };

  const body = html`
    <div class="db-testcall">
      <section class="db-testcall-card db-testcall-web">
        <div class="db-testcall-head">
          <div class="db-testcall-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
          </div>
          <span class="db-testcall-tag">Web call</span>
        </div>
        <h3 class="db-testcall-title">Talk in your browser</h3>
        <p class="db-testcall-sub">Use your mic right now. The fastest way to hear ${agent.name} say ${pronouns(agent).poss} greeting and answer a quick question.</p>
        <ul class="db-testcall-bullets">
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> No phone number needed</li>
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> Instant — works on any device</li>
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> Free, doesn't burn voice minutes</li>
        </ul>
        <div class="db-testcall-cta">
          <button class="db-btn-primary" onClick=${onTest}>
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>
            <span>Start web call</span>
          </button>
          <span class="db-testcall-hint">Allow microphone access when asked.</span>
        </div>
      </section>

      <div class="db-testcall-divider" aria-hidden="true"><span>or</span></div>

      <section class="db-testcall-card db-testcall-phone">
        <div class="db-testcall-head">
          <div class="db-testcall-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.89.33 1.77.62 2.61a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.47-1.18a2 2 0 0 1 2.11-.45c.84.29 1.72.5 2.61.62A2 2 0 0 1 22 16.92z"/></svg>
          </div>
          <span class="db-testcall-tag">Phone callback</span>
        </div>
        <h3 class="db-testcall-title">Get a real call on your phone</h3>
        <p class="db-testcall-sub">${agent.name} rings you within a few seconds. The honest test — proves the carrier route, DTMF, and IVR setup all work end-to-end.</p>
        <ul class="db-testcall-bullets">
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> Tests the live carrier path</li>
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> Works on the move</li>
          <li><span class="db-testcall-bullet-tick" aria-hidden="true">✓</span> Counts against your monthly minutes</li>
        </ul>
        <form class="db-testcall-form" onSubmit=${submitPhone}>
          <label class="db-form-field">
            <span class="db-form-label">Your phone number</span>
            <input class="db-input" type="tel" inputmode="tel"
                   placeholder="+91 98XXXXXXXX"
                   value=${num} onInput=${(e) => setNum(e.target.value)} />
          </label>
          <button class="db-btn-primary" type="submit" disabled=${!num.trim() || submitting}>
            ${done
              ? html`<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12l5 5L20 7"/></svg><span>Calling you now</span>`
              : html`<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.89.33 1.77.62 2.61a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.47-1.18a2 2 0 0 1 2.11-.45c.84.29 1.72.5 2.61.62A2 2 0 0 1 22 16.92z"/></svg><span>${submitting ? "Connecting…" : "Call me back"}</span>`}
          </button>
        </form>
        <span class="db-testcall-hint">We use your number once for this test — never stored, never SMS'd.</span>
      </section>
    </div>
  `;
  return html`
    <${DashboardShell}
      activeKey="test-call"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Get a test call"
      subtitle=${`Two ways to hear ${agent.name} live — pick whichever's closer to hand.`}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Go live — the friendly number-request flow as its own page.
function AgentGoLivePage({ agent, agents, presets, plan, onNav, refreshAgent, org }) {
  // History of past number-request submissions for this agent — folded in
  // here (used to be its own /numbers page) so the whole phone-number
  // workflow lives on a single screen.
  const [numberRequests, setNumberRequests] = useState([]);
  const [numberRequestsLoading, setNumberRequestsLoading] = useState(true);

  // ─── Self-service SIP-connect (Voniz) ───────────────────────────────────
  // Fetches the saved sip_config + the inbound URI the operator pastes
  // into their Voniz Application field. Form-state is independent of
  // sipConfig so editing-then-discarding doesn't blow away the saved
  // config until the operator hits Save.
  const [sipConfig, setSipConfig] = useState(null);          // server's redacted view
  const [sipInboundUri, setSipInboundUri] = useState("");    // computed by server
  // Self-service SIP providers come from presets (server-owned). Filter
  // to the self_service ones for the dropdown; the FIRST is the default
  // (Plivo). Each carries registrar + console_url + blurb so the form
  // and setup steps adapt to whichever provider the operator picks.
  const sipProviders = (presets?.sip_providers || []).filter((p) => p.self_service);
  const defaultSipProviderId = sipProviders[0]?.id || "plivo";
  const [sipForm, setSipForm] = useState({
    provider: defaultSipProviderId,
    did: "",            // E.164 phone number callers dial — the "live number"
    alias: "",
    username: "",
    registrar: sipProviders[0]?.registrar || "sip.plivo.com",
    remote_uri: "",
    password: "",
  });
  // Metadata for the currently-selected provider (label, registrar,
  // console_url, blurb). Drives all the provider-specific copy below.
  const sipProviderMeta = (sipProviders.find((p) => p.id === sipForm.provider))
    || (presets?.sip_providers || []).find((p) => p.id === sipForm.provider)
    || { id: sipForm.provider, label: "SIP provider", registrar: "", console_url: "" };
  const sipProviderLabel = sipProviderMeta.label || "SIP provider";
  const [sipSaving, setSipSaving] = useState(false);
  const [sipError, setSipError] = useState("");
  const [sipDirty, setSipDirty] = useState(false);
  const [sipUriCopied, setSipUriCopied] = useState(false);

  // Fetch on mount: server returns inbound_uri (always derivable from
  // agent_id), the current config (redacted), and the inbound_host so the
  // operator sees the exact value they'll paste into Voniz.
  useEffect(() => {
    if (!agent?.id) return;
    fetch(`/api/agents/${agent.id}/sip-config`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        setSipInboundUri(data.inbound_uri || "");
        const cfg = data.config;
        if (cfg) {
          setSipConfig(cfg);
          // Seed the form with what's stored so the operator can edit
          // in place. Password is intentionally NOT seeded — the
          // backend's redacted_view sends password=null + password_set=true.
          setSipForm({
            provider: cfg.provider || defaultSipProviderId,
            did: cfg.did || "",
            alias: cfg.alias || "",
            username: cfg.username || "",
            registrar: cfg.registrar || sipProviders[0]?.registrar || "sip.plivo.com",
            remote_uri: cfg.remote_uri || "",
            password: "",   // empty by default; preserved server-side on save
          });
          setSipDirty(false);
        }
      })
      .catch(() => {});
  }, [agent?.id]);

  const setSipField = useCallback((key, val) => {
    setSipForm((f) => ({ ...f, [key]: val }));
    setSipDirty(true);
    setSipError("");
  }, []);

  // Switching provider re-defaults the registrar to that provider's SIP
  // server — UNLESS the operator has typed a custom registrar that
  // doesn't match any known provider default (then we leave it alone).
  const onSipProviderChange = useCallback((nextId) => {
    setSipForm((f) => {
      const allProviders = presets?.sip_providers || [];
      const prevMeta = allProviders.find((p) => p.id === f.provider);
      const nextMeta = allProviders.find((p) => p.id === nextId);
      // Was the current registrar the previous provider's default (or
      // empty)? If so, swap it to the new default. Otherwise keep the
      // operator's custom value.
      const registrarIsDefault = !f.registrar.trim()
        || (prevMeta && f.registrar.trim() === (prevMeta.registrar || ""));
      return {
        ...f,
        provider: nextId,
        registrar: registrarIsDefault ? (nextMeta?.registrar || f.registrar) : f.registrar,
      };
    });
    setSipDirty(true);
    setSipError("");
  }, [presets]);

  const sipCanSave = !sipSaving && (
    sipForm.alias.trim().length > 0
    && (sipForm.username.trim().length > 0 || sipForm.remote_uri.trim().length > 0)
  );

  const saveSipConfig = useCallback(async () => {
    if (!agent?.id) return;
    setSipSaving(true);
    setSipError("");
    try {
      const r = await fetch(`/api/agents/${agent.id}/sip-config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: sipForm.provider || defaultSipProviderId,
          did: sipForm.did.trim(),       // the live number callers dial
          alias: sipForm.alias.trim(),
          username: sipForm.username.trim(),
          registrar: sipForm.registrar.trim() || sipProviderMeta.registrar || "sip.plivo.com",
          remote_uri: sipForm.remote_uri.trim(),
          // Empty string means "don't update" server-side — preserves
          // existing password. Set value means "use this new password".
          password: sipForm.password,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setSipError(body.detail || `Save failed (HTTP ${r.status}).`);
        setSipSaving(false);
        return;
      }
      const data = await r.json();
      if (data?.config) setSipConfig(data.config);
      if (data?.inbound_uri) setSipInboundUri(data.inbound_uri);
      // Wipe the password field — server has it stored now.
      setSipForm((f) => ({ ...f, password: "" }));
      setSipDirty(false);
      if (refreshAgent) refreshAgent();
    } catch (e) {
      setSipError("Network error — couldn't save.");
    } finally {
      setSipSaving(false);
    }
  }, [agent?.id, sipForm, refreshAgent]);

  const disconnectSipConfig = useCallback(async () => {
    if (!agent?.id) return;
    if (!confirm("Disconnect this SIP provider? Your endpoint credentials will be cleared. You can reconfigure later.")) return;
    setSipSaving(true);
    setSipError("");
    try {
      const r = await fetch(`/api/agents/${agent.id}/sip-config`, { method: "DELETE" });
      if (!r.ok) { setSipError("Disconnect failed."); return; }
      setSipConfig(null);
      setSipForm({
        provider: defaultSipProviderId, did: "", alias: "", username: "",
        registrar: sipProviders[0]?.registrar || "sip.plivo.com",
        remote_uri: "", password: "",
      });
      setSipDirty(false);
      if (refreshAgent) refreshAgent();
    } catch {
      setSipError("Network error.");
    } finally {
      setSipSaving(false);
    }
  }, [agent?.id, refreshAgent]);

  const copySipInboundUri = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(sipInboundUri);
      setSipUriCopied(true);
      setTimeout(() => setSipUriCopied(false), 1500);
    } catch {}
  }, [sipInboundUri]);
  useEffect(() => {
    if (!agent?.id) return;
    setNumberRequestsLoading(true);
    fetch(`/api/agents/${agent.id}/number-requests`)
      .then((r) => r.ok ? r.json() : [])
      .then((arr) => {
        const list = Array.isArray(arr) ? arr : [];
        setNumberRequests(list);
        setNumberRequestsLoading(false);
        // Auto-open the managed-number panel if there's an active
        // request in flight — operator deserves to see its status
        // without hunting for the collapsed section.
        if (list.some((r) => r.status === "pending" || r.status === "in_progress")) {
          setManagedOpen(true);
        }
      })
      .catch(() => setNumberRequestsLoading(false));
  }, [agent?.id]);
  const fmtRequestTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
  const requestStatusClass = (s) => s === "fulfilled" ? "db-tag-green" : s === "in_progress" ? "db-tag-blue" : "db-tag-yellow";

  // Resolve the country whose providers we should surface — agent's profile
  // country wins, otherwise the org's default. Mirrors the logic from the
  // old standalone Numbers page.
  const goLiveAgentCountry = (agent?.variables?.country || "").trim().toUpperCase();
  const goLiveCountryCode = (COUNTRIES.find((c) => c.id === goLiveAgentCountry)?.id) || org?.country || "";
  const goLiveCountryLabel = (COUNTRIES.find((c) => c.id === goLiveCountryCode)?.label) || "this region";
  const goLiveProviders = NUMBER_PROVIDERS[goLiveCountryCode] || NUMBER_PROVIDERS_DEFAULT;

  // Phone-request country list — carries `placeholder` (different shape from
  // the top-level `COUNTRIES` constant which only has id/label). Renamed
  // away from `COUNTRIES` so it doesn't shadow + TDZ the module-level one
  // we use a few lines above to resolve providers.
  const PHONE_COUNTRIES = [
    { id: "IN", label: "India", placeholder: "+91 98XXXXXXXX" },
    { id: "US", label: "United States", placeholder: "+1 555 0100" },
    { id: "GB", label: "United Kingdom", placeholder: "+44 7700 900000" },
    { id: "SG", label: "Singapore", placeholder: "+65 8000 0000" },
    { id: "AE", label: "United Arab Emirates", placeholder: "+971 50 000 0000" },
    { id: "AU", label: "Australia", placeholder: "+61 400 000 000" },
    { id: "other", label: "Somewhere else (tell us)", placeholder: "+ country code & number" },
  ];
  const defaultCountry = (() => {
    // Prefer the agent's saved locale, then the user's browser-detected
    // locale, then fall back to India (our biggest market).
    const loc = (agent?.locale || "").toUpperCase();
    const m = PHONE_COUNTRIES.find((c) => loc.endsWith("-" + c.id));
    if (m) return m.id;
    try {
      const cached = JSON.parse(localStorage.getItem("sxai.locale") || "null");
      if (cached?.country && PHONE_COUNTRIES.find((c) => c.id === cached.country)) return cached.country;
    } catch {}
    return "IN";
  })();
  const [country, setCountry] = useState(defaultCountry);
  const [city, setCity] = useState("");
  const [handle, setHandle] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(null);
  const [error, setError] = useState("");
  // Managed-number panel is now a SECONDARY fallback (Voniz self-service
  // is the primary path). Folded by default — operator clicks the
  // teaser link to expand the form. We auto-expand if there's an
  // already-submitted request in flight so the operator sees the
  // confirmation immediately on refresh.
  const [managedOpen, setManagedOpen] = useState(false);
  const countryObj = PHONE_COUNTRIES.find((c) => c.id === country) || PHONE_COUNTRIES[0];
  const canSubmit = handle.trim().length > 4 && !submitting;
  const submit = async (e) => {
    e?.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true); setError("");
    try {
      const r = await fetch("/api/number-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: agent.id,
          country: countryObj.label,
          city: city.trim() || null,
          delivery_handle: handle.trim(),
        }),
      });
      if (!r.ok) throw new Error("server " + r.status);
      const data = await r.json();
      setDone({ id: data.id });
    } catch {
      setError("Couldn't submit — check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  };
  // Embed snippet for the agent — copyable, points at our own host.
  const embedOrigin = (typeof window !== "undefined") ? window.location.origin : "https://app.spiderx.ai";

  // Customisation controls — these line up 1:1 with embed.js data-* attrs.
  // Defaults match what the script itself uses when an attr is omitted, so
  // the snippet stays minimal until the user actually changes something.
  const COLOR_PRESETS = [
    { id: "violet", label: "Violet", value: "linear-gradient(135deg,#a855f7 0%,#ec4899 100%)" },
    { id: "ocean",  label: "Ocean",  value: "linear-gradient(135deg,#0ea5e9 0%,#2563eb 100%)" },
    { id: "forest", label: "Forest", value: "linear-gradient(135deg,#10b981 0%,#047857 100%)" },
    { id: "sunset", label: "Sunset", value: "linear-gradient(135deg,#fb923c 0%,#dc2626 100%)" },
    { id: "ink",    label: "Ink",    value: "#1a1d2b" },
  ];
  const [embedPosition, setEmbedPosition] = useState("bottom-right");
  const [embedMode,     setEmbedMode]     = useState("popover");
  const [embedLabel,    setEmbedLabel]    = useState(`Talk to ${agent.name}`);
  const [embedColor,    setEmbedColor]    = useState(COLOR_PRESETS[0].value);

  // Build the snippet — only emit data-* attrs that differ from defaults so
  // the line stays scannable when the user hasn't customised anything.
  const embedAttrs = [`data-agent="${agent.slug || agent.id}"`];
  if (embedPosition !== "bottom-right") embedAttrs.push(`data-position="${embedPosition}"`);
  if (embedMode !== "popover") embedAttrs.push(`data-mode="${embedMode}"`);
  if (embedLabel && embedLabel !== `Talk to ${agent.name}`) embedAttrs.push(`data-label="${embedLabel.replace(/"/g, "'")}"`);
  if (embedColor !== COLOR_PRESETS[0].value) embedAttrs.push(`data-color="${embedColor.replace(/"/g, "'")}"`);
  const embedSnippet = `<script src="${embedOrigin}/static/embed.js" ${embedAttrs.join(" ")}></script>`;

  const [copied, setCopied] = useState(false);
  const copySnippet = async () => {
    try { await navigator.clipboard.writeText(embedSnippet); setCopied(true); setTimeout(() => setCopied(false), 1800); }
    catch {}
  };

  // Publish state — flips agent.published via PATCH. Status banner up top
  // reflects whatever the server returned last. We pessimistically toggle so
  // the UI feels instant; on error we roll back.
  const published = !!agent.published;
  // Plan gating: publishing requires a paid plan. The server enforces this
  // (returns 402); we mirror the rule client-side so the CTA is honest
  // before the user clicks. `plan` here is the user's plan_state payload
  // from /api/me, threaded through DashboardShell.
  const planSlug = (plan?.plan?.slug || "free").toLowerCase();
  const isPaid = planSlug && planSlug !== "free";
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const togglePublish = async () => {
    // Belt + braces: don't even hit the server if we know the plan blocks it.
    // Pass the agent slug as `?return=…` so the Billing page can route us
    // straight back here once the upgrade lands, with the Publish CTA armed.
    const returnSlug = encodeURIComponent(agent.slug || agent.id);
    if (!published && !isPaid) {
      onNav && onNav(`/account/billing?return=${returnSlug}`);
      return;
    }
    setPublishing(true); setPublishError("");
    try {
      const r = await fetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ published: !published }),
      });
      if (r.status === 402) {
        // Race: plan flipped to free between page load and click. Route to
        // billing rather than showing a generic error.
        onNav && onNav(`/account/billing?return=${returnSlug}`);
        return;
      }
      if (!r.ok) throw new Error("server " + r.status);
      // Source of truth = server. Tell the parent to re-fetch — that pulls
      // the new agent payload (with published=true) and refreshes the agent
      // list so the sidebar badge flips to "Live" too.
      refreshAgent && refreshAgent();
    } catch {
      setPublishError("Couldn't publish — give it another tap.");
    } finally {
      setPublishing(false);
    }
  };

  // Faux-third-party preview state — the FAB is the closed state, the panel
  // expands when clicked, just like the real embed.js. We hand-rolled the
  // visuals here (rather than running the actual embed.js inside the page)
  // because embed.js mounts at fixed/2147483646 on the viewport; we want it
  // contained inside the preview frame for an honest in-context demo.
  const [embedOpen, setEmbedOpen] = useState(false);
  const businessName = (agent?.variables?.business_name || agent.name);
  const fabLabel = embedLabel || `Talk to ${agent.name}`;

  const numberPanel = done ? html`
    <section class="db-panel db-panel-tall">
      <h3 class="db-panel-title">You're all set — number on the way</h3>
      <p class="db-panel-sub">
        We're picking a ${countryObj.label} number for ${agent.name} and pointing it at ${pronouns(agent).obj} now.
        You'll get the number on ${handle.trim()} — usually within 1 working hour.
      </p>
      <div class="golive-success" style=${{ marginTop: 12 }}>
        <div class="golive-success-row"><span class="golive-success-icon">✓</span><span>Request <b>#${done.id}</b> received</span></div>
        <div class="golive-success-row"><span class="golive-success-icon">⏱</span><span>Avg. turnaround: under 1 working hour</span></div>
        <div class="golive-success-row"><span class="golive-success-icon">↳</span><span>Need it sooner? WhatsApp us on <a class="golive-link" href="https://wa.me/918100000000" target="_blank" rel="noopener">+91 81000 00000</a>.</span></div>
      </div>
    </section>
  ` : html`
    <section class="db-panel db-panel-tall">
      <div class="db-channel-head">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M2 13a14 14 0 0 1 20 0l-2.4 2.4a2 2 0 0 1-2.6.2l-2-1.5a2 2 0 0 1-.7-2.1l.6-2a10 10 0 0 0-5.8 0l.6 2a2 2 0 0 1-.7 2.1l-2 1.5a2 2 0 0 1-2.6-.2L2 13z"/></svg>
        <div>
          <h3 class="db-panel-title">Get a phone number</h3>
          <p class="db-panel-sub">Callers ring a real number, ${agent.name} answers. We provision it on our side.</p>
        </div>
      </div>
      <form class="db-form" onSubmit=${submit}>
        <label class="db-form-field">
          <span class="db-form-label">Where are most callers?</span>
          <select class="db-input" value=${country} onChange=${(e) => setCountry(e.target.value)}>
            ${PHONE_COUNTRIES.map((c) => html`<option key=${c.id} value=${c.id}>${c.label}</option>`)}
          </select>
        </label>
        <label class="db-form-field">
          <span class="db-form-label">City or area <span class="db-form-opt">(optional)</span></span>
          <input class="db-input" type="text" placeholder="e.g. Bengaluru, NYC, Greater London"
                 value=${city} onInput=${(e) => setCity(e.target.value)} />
        </label>
        <label class="db-form-field">
          <span class="db-form-label">Where should we send the number?</span>
          <input class="db-input" type="tel" placeholder=${countryObj.placeholder}
                 value=${handle} onInput=${(e) => setHandle(e.target.value)} required />
          <span class="db-form-help">We'll WhatsApp / SMS the number here once it's live.</span>
        </label>
        ${error ? html`<div class="golive-error">${error}</div>` : ""}
        <div class="db-actions-row">
          <button type="submit" class="db-btn-primary" disabled=${!canSubmit}>
            ${submitting ? "Submitting…" : "Get my number"}
          </button>
        </div>
      </form>
    </section>
  `;

  // Publish banner — sits above the channels. The "Publish" CTA is the
  // commitment moment ("this agent is ready for real callers / visitors").
  // It's a separate concept from having a phone number: the embed widget can
  // be on a page in draft mode (handy for previewing), but it'll show a
  // "draft mode" notice instead of accepting real calls. Once published,
  // the same snippet just works.
  //
  // Free-plan users see an "Upgrade to publish" gate instead of the
  // regular CTA — the same banner copy clarifies what publishing unlocks,
  // and the button routes to /account/billing.
  const planLabel = plan?.plan?.label || "Free";
  const bannerClass = published ? "is-live" : (isPaid ? "is-draft" : "is-gated");
  const publishBanner = html`
    <section class=${"db-publish-banner " + bannerClass}>
      <div class="db-publish-left">
        <div class=${"db-publish-dot " + (published ? "is-live" : isPaid ? "is-draft" : "is-gated")} aria-hidden="true"></div>
        <div>
          <div class="db-publish-status">
            ${published ? "Live" : isPaid ? "Draft" : `Locked · ${planLabel} plan`}
          </div>
          <div class="db-publish-copy">
            ${published
              ? html`${agent.name} is published. Visitors using the embed snippet and callers on your number reach ${pronouns(agent).obj} immediately.`
              : isPaid
                ? html`${agent.name} is in draft. Hit Publish when you're happy — the embed widget goes live the moment you do. Add a phone number now or later — both channels unlock with the same publish.`
                : html`Publishing unlocks the channels below. You can build, edit, and test ${agent.name} freely on the free plan — once you upgrade, just paste the web snippet to start taking real calls on day one. A phone number's a click away whenever you're ready.`}
          </div>
        </div>
      </div>
      <div class="db-publish-actions">
        ${publishError ? html`<span class="db-publish-error">${publishError}</span>` : ""}
        <button type="button"
                class=${published ? "db-btn-ghost" : "db-btn-primary"}
                disabled=${publishing} onClick=${togglePublish}>
          ${publishing ? "…" : (published ? "Unpublish" : isPaid ? "Publish & Go-live →" : "Upgrade to publish →")}
        </button>
      </div>
    </section>
  `;

  // Faux third-party embed preview — a browser-window chrome around dummy
  // page content with the actual embed bubble pinned to bottom-right of the
  // preview frame. Click the FAB to expand the panel; the panel embeds the
  // real /embed/<slug> page, so the live agent really does answer.
  const embedPanel = html`
    <section class="db-panel db-panel-tall golive-channel-card">
      <div class="db-channel-head">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 8h18"/></svg>
        <div style=${{ flex: 1 }}>
          <h3 class="db-panel-title">
            Web widget
            <span class="golive-channel-pill golive-channel-pill-default">Fastest go-live</span>
          </h3>
          <p class="db-panel-sub">
            Drop one line of JavaScript on any site — ${agent.name} starts taking calls
            from visitors today. No phone provider needed.
          </p>
        </div>
      </div>

      <!-- Faux browser frame containing a sample page + the embed bubble.
           Click the bubble to see the exact popover behaviour visitors get. -->
      <div class="db-faux-browser" aria-label="Preview as it appears on your website">
        <div class="db-faux-chrome">
          <span class="db-faux-dot"></span>
          <span class="db-faux-dot"></span>
          <span class="db-faux-dot"></span>
          <div class="db-faux-url">https://${(agent?.variables?.website || "yourdomain.example").replace(/^https?:\/\//, "").split("/")[0]}</div>
        </div>
        <div class="db-faux-body">
          <div class="db-faux-page">
            <div class="db-faux-h1">${businessName}</div>
            <div class="db-faux-line"></div>
            <div class="db-faux-line short"></div>
            <div class="db-faux-line"></div>
            <div class="db-faux-line shorter"></div>
            <div class="db-faux-line short"></div>
          </div>

          <!-- The embed widget pinned bottom-right of the FAUX frame. Mirrors
               embed.js's CSS using the .sxai-* classes so it looks identical
               to what visitors see. -->
          <div class=${"sxai-root sxai-preview" + (embedOpen ? " is-open" : "")}
               data-pos=${embedPosition} data-mode=${embedMode}>
            <button type="button" class="sxai-fab" aria-label=${fabLabel}
                    style=${{ background: embedColor }} onClick=${() => setEmbedOpen(true)}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13a14 14 0 0 1 20 0l-2.4 2.4a2 2 0 0 1-2.6.2l-2-1.5a2 2 0 0 1-.7-2.1l.6-2a10 10 0 0 0-5.8 0l.6 2a2 2 0 0 1-.7 2.1l-2 1.5a2 2 0 0 1-2.6-.2L2 13z"/></svg>
            </button>
            <div class="sxai-tip">${fabLabel}</div>
            <div class=${"sxai-panel" + (embedOpen ? " open" : "")}>
              <iframe class="sxai-frame" src=${`/embed/${agent.slug || agent.id}`} title=${`SpiderX.AI — ${fabLabel}`} allow="microphone; autoplay; clipboard-read; clipboard-write" loading="lazy" />
              <button type="button" class="sxai-close" aria-label="Close" onClick=${() => setEmbedOpen(false)}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
              </button>
            </div>
          </div>
        </div>
      </div>
      <div class="db-embed-caption">
        ${embedOpen ? "Click the × to close — same gesture your visitors use." : "Click the bubble to open it, just like a real visitor would."}
      </div>

      <!-- Customisation controls — each maps 1:1 to a data-* attr on the
           embed.js <script> tag. The snippet below regenerates as the user
           tweaks, so they can scan-and-copy when it looks right. -->
      <div class="db-embed-controls">
        <div class="db-embed-control">
          <span class="db-embed-control-label">Position</span>
          <div class="db-embed-segment">
            <button type="button" class=${"db-embed-seg-btn" + (embedPosition === "bottom-right" ? " active" : "")}
                    onClick=${() => setEmbedPosition("bottom-right")}>Bottom right</button>
            <button type="button" class=${"db-embed-seg-btn" + (embedPosition === "bottom-left" ? " active" : "")}
                    onClick=${() => setEmbedPosition("bottom-left")}>Bottom left</button>
          </div>
        </div>

        <div class="db-embed-control">
          <span class="db-embed-control-label">Mode</span>
          <div class="db-embed-segment">
            <button type="button" class=${"db-embed-seg-btn" + (embedMode === "popover" ? " active" : "")}
                    onClick=${() => setEmbedMode("popover")}>Popover</button>
            <button type="button" class=${"db-embed-seg-btn" + (embedMode === "fullscreen" ? " active" : "")}
                    onClick=${() => setEmbedMode("fullscreen")}>Fullscreen</button>
          </div>
        </div>

        <div class="db-embed-control">
          <span class="db-embed-control-label">Bubble label</span>
          <input class="db-embed-input" type="text" maxlength="40"
                 value=${embedLabel} placeholder=${`Talk to ${agent.name}`}
                 onInput=${(e) => setEmbedLabel(e.target.value)} />
        </div>

        <div class="db-embed-control">
          <span class="db-embed-control-label">Bubble colour</span>
          <div class="db-embed-swatches">
            ${COLOR_PRESETS.map((c) => html`
              <button key=${c.id} type="button"
                      class=${"db-embed-swatch" + (embedColor === c.value ? " active" : "")}
                      style=${{ background: c.value }}
                      aria-label=${c.label}
                      title=${c.label}
                      onClick=${() => setEmbedColor(c.value)}></button>
            `)}
            <label class="db-embed-swatch-custom" title="Pick a custom colour">
              <input type="color" value=${typeof embedColor === "string" && embedColor.startsWith("#") ? embedColor : "#a855f7"}
                     onInput=${(e) => setEmbedColor(e.target.value)} />
              <span aria-hidden="true">+</span>
            </label>
          </div>
        </div>
      </div>

      <div class="db-embed-snippet">
        <code>${embedSnippet}</code>
      </div>
      <div class="db-actions-row">
        <button type="button" class=${"db-btn-primary " + (copied ? "is-copied" : "")} onClick=${copySnippet}>
          ${copied ? html`<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg><span>Copied!</span>` : html`<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg><span>Copy snippet</span>`}
        </button>
        <a class="db-btn-ghost" href=${`/embed/${agent.slug || agent.id}`} target="_blank" rel="noopener">Open standalone →</a>
      </div>
    </section>
  `;

  // Providers panel — moved here from the old standalone Numbers page so
  // the user can see WHO will provision their number (and the preferred
  // partner) right next to the request form.
  const providersPanel = html`
    <section class="db-panel">
      <h3 class="db-panel-title">
        Providers for ${goLiveCountryLabel}
        ${goLiveCountryCode ? html`<span class="db-panel-pill">${goLiveCountryCode}</span>` : ""}
      </h3>
      <p class="db-panel-sub">
        ${goLiveCountryCode
          ? html`When you submit a number request for ${agent.name}, we provision it through one of these carriers — our preferred partner first, others on request. Change the country on the <button class="db-link-btn" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/profile`)}>Business profile</button> if this isn't right.`
          : html`Set the country on the <button class="db-link-btn" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/profile`)}>Business profile</button> to see your local providers. Until then, here are our global defaults.`}
      </p>
      <ul class="db-providers">
        ${goLiveProviders.map((p) => html`
          <li class=${"db-provider" + (p.partner ? " is-partner" : "")} key=${p.id}>
            <div class="db-provider-head">
              <div class="db-provider-name">${p.name}</div>
              ${p.partner ? html`<span class="db-provider-pill">Preferred partner</span>` : ""}
            </div>
            <div class="db-provider-desc">${p.desc}</div>
            <div class="db-provider-coverage">${p.coverage}</div>
          </li>
        `)}
      </ul>
    </section>
  `;

  // History — only renders if any request has been submitted. Empty state
  // doesn't get its own block; the request form above is the call-to-action.
  const requestHistoryPanel = (!numberRequestsLoading && numberRequests.length > 0) ? html`
    <section class="db-panel">
      <h3 class="db-panel-title">Your number requests <span class="db-panel-pill">${numberRequests.length}</span></h3>
      <p class="db-panel-sub">Status of every phone-number request you've submitted for ${agent.name}.</p>
      <div class="db-table-wrap">
        <table class="db-table">
          <thead><tr><th>Submitted</th><th>Country</th><th>City</th><th>Send to</th><th>Status</th></tr></thead>
          <tbody>
            ${numberRequests.map((r) => html`
              <tr key=${r.id}>
                <td>${fmtRequestTime(r.created_at)}</td>
                <td>${r.country || "—"}</td>
                <td>${r.city || "—"}</td>
                <td>${r.delivery_handle}</td>
                <td><span class=${"db-tag " + requestStatusClass(r.status)}>${(r.status || "pending").replace(/_/g, " ")}</span></td>
              </tr>
            `)}
          </tbody>
        </table>
      </div>
    </section>
  ` : "";

  // ─── Bring-your-own-SIP card (Voniz, self-service) ────────────────────
  // Two-column layout:
  //   Left  — form: alias / username / registrar / password
  //   Right — setup steps: copyable inbound SIP URI + numbered checklist
  // Status pill at top reflects whether the agent has saved a config yet
  // and notes that Phase 2 (actual SIP termination) is pending.
  // Three SIP-card states:
  //   no config saved        → "Not connected" (yellow)
  //   config saved, no DID   → "Credentials saved · add a phone number" (yellow)
  //                            — the operator needs to buy a DID in Voniz
  //                              and paste it here before calls can flow.
  //   config saved + DID set → "Live on <did> · waiting on first call" (blue)
  //                            — fully configured pending the SIP
  //                              terminator going live.
  const sipHasDid = !!(sipConfig && sipConfig.did);
  const sipFmtDid = (raw) => {
    // Country-aware E.164 grouping for readability. The naive single
    // regex couldn't pick the right country-code length (was eating
    // 3 digits for India's '+91' giving '+918 0456 789 01'). This
    // per-country approach gets the canonical visual format right.
    // Anything outside this list falls back to the raw E.164 string.
    if (!raw) return "";
    // US / Canada → +1 NNN NNN NNNN
    let m = raw.match(/^(\+1)(\d{3})(\d{3})(\d{4})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]} ${m[4]}`;
    // India → +91 NN NNNN NNNN
    m = raw.match(/^(\+91)(\d{2})(\d{4})(\d{4})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]} ${m[4]}`;
    // UK mobile → +44 NNNN NNNNNN
    m = raw.match(/^(\+44)(\d{4})(\d{6})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]}`;
    // Singapore → +65 NNNN NNNN
    m = raw.match(/^(\+65)(\d{4})(\d{4})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]}`;
    // UAE → +971 N(N) NNN NNNN
    m = raw.match(/^(\+971)(\d{1,2})(\d{3})(\d{4})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]} ${m[4]}`;
    // Australia → +61 N NNNN NNNN
    m = raw.match(/^(\+61)(\d{1})(\d{4})(\d{4})$/);
    if (m) return `${m[1]} ${m[2]} ${m[3]} ${m[4]}`;
    return raw;
  };
  const sipStatusLabel = !sipConfig
    ? "Not connected"
    : sipHasDid
      ? `Live on ${sipFmtDid(sipConfig.did)} · waiting on first call`
      : "Credentials saved · add a phone number to go live";
  const sipStatusPillClass = !sipConfig
    ? "db-tag-yellow"
    : sipHasDid ? "db-tag-blue" : "db-tag-yellow";
  const sipPanel = html`
    <section class="db-panel db-panel-tall sip-card golive-channel-card">
      <div class="db-channel-head">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
          <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.89.33 1.77.62 2.61a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.47-1.18a2 2 0 0 1 2.11-.45c.84.29 1.72.5 2.61.62A2 2 0 0 1 22 16.92z"/>
        </svg>
        <div style=${{ flex: 1 }}>
          <h3 class="db-panel-title">
            Phone number <span class="sip-pill">${sipProviderLabel} SIP</span>
            <span class=${"sip-status-pill " + sipStatusPillClass}>${sipStatusLabel}</span>
          </h3>
          <p class="db-panel-sub">
            Already have a ${sipProviderLabel} endpoint? Paste its details, point your
            provider's <b>Application</b> at our SIP URI, and inbound phone calls route to ${agent.name}.
          </p>
        </div>
      </div>

      <!-- Phone AI provider selector — defaults to Plivo, with Exotel,
           Voniz and others. Changing it re-defaults the registrar below
           and adapts the setup steps + console link to the chosen
           provider. -->
      <label class="db-form-field sip-provider-select">
        <span class="db-form-label">Phone AI provider</span>
        <select class="db-input db-select"
                value=${sipForm.provider}
                onChange=${(e) => onSipProviderChange(e.target.value)}>
          ${sipProviders.map((p) => html`
            <option value=${p.id} key=${p.id}>${p.label}${p.id === defaultSipProviderId ? " (default)" : ""}</option>
          `)}
        </select>
        ${sipProviderMeta.blurb ? html`<span class="db-form-help">${sipProviderMeta.blurb}</span>` : ""}
      </label>

      <!-- Live number callout — surfaces the DID prominently above the
           form once the operator has saved one. This is the answer to
           the operator's "what number will customers actually dial?"
           question. -->
      ${sipHasDid ? html`
        <div class="sip-live-number-card">
          <div class="sip-live-number-label">Live phone number</div>
          <div class="sip-live-number-value">${sipFmtDid(sipConfig.did)}</div>
          <div class="sip-live-number-sub">
            Callers dialling this number reach ${agent.name} via your Voniz endpoint.
          </div>
        </div>
      ` : ""}

      <div class="sip-grid">
        <!-- LEFT: Credentials form -->
        <div class="sip-col">
          <!-- DID first — it's the most important field; the rest is
               routing plumbing. -->
          <label class="db-form-field">
            <span class="db-form-label">
              Phone number <span class="db-form-opt">(DID from ${sipProviderLabel})</span>
            </span>
            <input class="db-input" type="tel"
                   placeholder="+91 80 4567 8901"
                   value=${sipForm.did}
                   onInput=${(e) => setSipField("did", e.target.value)} />
            <span class="db-form-help">
              The actual number callers dial. Buy it in ${sipProviderLabel}, attach it to
              this endpoint, then paste it here. We'll format it for you.
            </span>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Endpoint alias <span class="db-form-opt">(your name for it)</span></span>
            <input class="db-input" type="text" placeholder="e.g. front-desk"
                   value=${sipForm.alias}
                   onInput=${(e) => setSipField("alias", e.target.value)} />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">${sipProviderLabel} username</span>
            <input class="db-input" type="text"
                   placeholder="SIP username at your provider"
                   value=${sipForm.username}
                   onInput=${(e) => setSipField("username", e.target.value)} />
            <span class="db-form-help">Or paste the full SIP URI into the field below — we'll split it.</span>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Or paste full SIP URI <span class="db-form-opt">(optional)</span></span>
            <input class="db-input" type="text"
                   placeholder=${"sip:username@" + (sipProviderMeta.registrar || "sip.example.com")}
                   value=${sipForm.remote_uri}
                   onInput=${(e) => setSipField("remote_uri", e.target.value)} />
          </label>
          <label class="db-form-field">
            <span class="db-form-label">Registrar</span>
            <input class="db-input" type="text"
                   placeholder=${sipProviderMeta.registrar || "sip.example.com"}
                   value=${sipForm.registrar}
                   onInput=${(e) => setSipField("registrar", e.target.value)} />
            <span class="db-form-help">${sipProviderLabel}'s SIP server. Leave as default unless you're on a regional registrar.</span>
          </label>
          <label class="db-form-field">
            <span class="db-form-label">
              Password
              <span class="db-form-opt">(optional · for future outbound)</span>
            </span>
            <input class="db-input" type="password"
                   placeholder=${sipConfig?.password_set ? "Stored securely · re-enter to change" : "From your " + sipProviderLabel + " endpoint"}
                   value=${sipForm.password}
                   onInput=${(e) => setSipField("password", e.target.value)} />
            <span class="db-form-help">
              We only need this for outbound calls (Phase 2). Inbound works without it.
            </span>
          </label>
          ${sipError ? html`<div class="sip-error">${sipError}</div>` : ""}
          <div class="sip-actions">
            ${sipConfig ? html`
              <button class="db-btn-secondary" type="button"
                      disabled=${sipSaving}
                      onClick=${disconnectSipConfig}>Disconnect</button>
            ` : ""}
            <button class="db-btn-primary" type="button"
                    disabled=${!sipCanSave || (!sipDirty && !!sipConfig)}
                    onClick=${saveSipConfig}>
              ${sipSaving ? "Saving…" : sipConfig ? (sipDirty ? "Save changes" : "Saved ✓") : "Save & generate inbound URI"}
            </button>
          </div>
        </div>

        <!-- RIGHT: Setup steps + copyable inbound URI -->
        <div class="sip-col sip-col-steps">
          <div class="sip-step">
            <div class="sip-step-num">1</div>
            <div class="sip-step-body">
              <div class="sip-step-title">Buy a number + endpoint in ${sipProviderLabel}</div>
              <div class="sip-step-sub">
                In your ${sipProviderMeta.console_url ? html`<a href=${sipProviderMeta.console_url} target="_blank" rel="noopener" class="sip-link">${sipProviderLabel} console</a>` : html`${sipProviderLabel} console`},
                pick a DID (the phone number callers will dial) and attach it
                to a SIP endpoint. ${sipProviderLabel} gives you a username + password.
              </div>
            </div>
          </div>
          <div class="sip-step">
            <div class="sip-step-num">2</div>
            <div class="sip-step-body">
              <div class="sip-step-title">Paste credentials on the left</div>
              <div class="sip-step-sub">
                Phone number (DID), alias, username, registrar — paste them all
                into the form. Hit Save.
              </div>
            </div>
          </div>
          <div class=${"sip-step" + (sipConfig ? "" : " is-pending")}>
            <div class="sip-step-num">3</div>
            <div class="sip-step-body">
              <div class="sip-step-title">Copy this inbound SIP URI</div>
              <div class="sip-uri-row">
                <code class="sip-uri-code">${sipInboundUri || "(saving will reveal it)"}</code>
                <button class="sip-uri-copy" type="button"
                        onClick=${copySipInboundUri}
                        disabled=${!sipInboundUri}>
                  ${sipUriCopied ? "Copied ✓" : "Copy"}
                </button>
              </div>
              <div class="sip-step-sub">
                The user-part (<code>agent-${agent.id}</code>) tells our SIP
                terminator which agent picks up. Don't edit it.
              </div>
            </div>
          </div>
          <div class=${"sip-step" + (sipConfig ? "" : " is-pending")}>
            <div class="sip-step-num">4</div>
            <div class="sip-step-body">
              <div class="sip-step-title">Paste it into ${sipProviderLabel}</div>
              <div class="sip-step-sub">
                In the same ${sipProviderLabel} endpoint, edit its <b>Application</b> field
                and set the destination SIP URI to the one above. Save.
              </div>
            </div>
          </div>
          <div class=${"sip-step" + (sipHasDid ? "" : " is-pending")}>
            <div class="sip-step-num">5</div>
            <div class="sip-step-body">
              <div class="sip-step-title">Make a test call</div>
              <div class="sip-step-sub">
                ${sipHasDid
                  ? html`Dial <b>${sipFmtDid(sipConfig.did)}</b> from any phone. ${agent.name} should pick up.`
                  : html`Dial your ${sipProviderLabel} DID from any phone. ${agent.name} should pick up.`}
                <span class="sip-betatag">Beta</span> SIP termination is rolling
                out — your config is saved either way; calls start flowing the
                moment the terminator is enabled for your workspace.
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  `;

  // ── Managed-number fallback (secondary path) ─────────────────────────
  // Voniz self-service is the primary phone path now. For operators who
  // DON'T have any SIP provider yet, we keep the legacy "we'll provision
  // a number for you" flow available — but folded behind a one-line
  // teaser so it doesn't visually compete with the Voniz card.
  //
  // Auto-expanded when there's already a pending/in-progress request
  // (handled in the numberRequests fetch effect above) so a returning
  // operator sees their status immediately. Otherwise: collapsed.
  const managedHasActive = numberRequests.some((r) => r.status === "pending" || r.status === "in_progress");
  const managedFallback = html`
    <section class="db-panel managed-fallback">
      ${managedOpen ? html`
        <div class="managed-fallback-head">
          <div>
            <h3 class="db-panel-title">Don't have a SIP provider yet?</h3>
            <p class="db-panel-sub">
              We'll provision a regional number for ${agent.name} on our side and WhatsApp it to you.
              Manual fulfilment — usually within 1 working hour. Most operators prefer the Voniz path
              above; this is the white-glove fallback.
            </p>
          </div>
          ${!managedHasActive ? html`
            <button class="managed-fallback-collapse" type="button"
                    onClick=${() => setManagedOpen(false)}
                    aria-label="Hide">×</button>
          ` : ""}
        </div>
        ${numberPanel}
      ` : html`
        <button class="managed-fallback-teaser" type="button"
                onClick=${() => setManagedOpen(true)}>
          <span class="managed-fallback-icon" aria-hidden="true">✉</span>
          <span class="managed-fallback-copy">
            <strong>Don't have a SIP provider?</strong>
            <span class="managed-fallback-sub">We'll provision a managed number for you instead. ~1 working hour, ops-fulfilled.</span>
          </span>
          <span class="managed-fallback-arrow" aria-hidden="true">→</span>
        </button>
      `}
    </section>
  `;

  const body = html`
    <div class="db-overview">
      ${publishBanner}
      <!-- Two channels to go live, side-by-side equal weight:
           LEFT  — Web embed widget. The fast-path default. No phone
                   provider needed; paste a script tag on any site and
                   ${agent.name} is taking calls. This is what unlocks
                   plan commercialization from zero — operators can
                   ship to real users today.
           RIGHT — SIP (Voniz). For operators who want a real phone
                   number routed in. Same go-live status; phone
                   channel instead of web. -->
      <div class="db-channels golive-channels">
        ${embedPanel}
        ${sipPanel}
      </div>
      <!-- Secondary fallback for operators who don't have a SIP
           provider yet — we provision a managed number for them. -->
      ${managedFallback}
      ${providersPanel}
      ${requestHistoryPanel}
    </div>
  `;
  return html`
    <${DashboardShell}
      activeKey="live"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Go live"
      subtitle=${`Two channels to choose from. Ship ${agent.name} on the web today, add a phone number whenever you're ready.`}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// Number requests — list of past submissions for this agent.
// ─────────────────────────────────────────────────────────────────────────
// TeamPage — /account/team. Phase 2 team management.
//   Top half: members table (avatar, name, email, role, joined, actions).
//   Bottom half: pending invites + new-invite form.
// Permissions matter — only owner can change roles; admin+ can invite or
// remove; member can only see + leave. The page derives the caller's role
// from the members list so it doesn't need another /me fetch.
// ─────────────────────────────────────────────────────────────────────────
function TeamPage({ onNav, org, currentUser }) {
  const [members, setMembers] = useState([]);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(true);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [error, setError] = useState(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const [m, i] = await Promise.all([
        fetch("/api/org/members").then((r) => r.json()),
        fetch("/api/org/invites").then((r) => r.json()),
      ]);
      setMembers(Array.isArray(m) ? m : []);
      setInvites(Array.isArray(i) ? i : []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const myRole = (currentUser && members.find((m) => m.user_id === currentUser.id)?.role) || "member";
  const canManage = myRole === "owner" || myRole === "admin";
  const canChangeRoles = myRole === "owner";

  const onChangeRole = async (userId, role) => {
    const r = await fetch(`/api/org/members/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      alert(e.detail?.message || e.detail || `Failed (${r.status})`);
      return;
    }
    await refresh();
  };

  // Destructive actions queued through a confirm modal instead of the
  // native browser confirm() — see Audit F.3.
  const [confirmAction, setConfirmAction] = useState(null);
  const onRemoveMember = (userId, name) => {
    setConfirmAction({
      kind: "remove_member",
      title: userId === currentUser?.id ? "Leave the team?" : `Remove ${name || "this member"}?`,
      body: userId === currentUser?.id
        ? html`You'll lose access to this workspace's agents. Your own workspace and any agents you've created elsewhere are unaffected.`
        : html`<strong>${name || "This member"}</strong> will lose access to this workspace's agents. They can be re-invited later.`,
      confirmLabel: userId === currentUser?.id ? "Leave team" : "Remove",
      onConfirm: async () => {
        const r = await fetch(`/api/org/members/${userId}`, { method: "DELETE" });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          throw new Error(e.detail?.message || e.detail || `Failed (${r.status})`);
        }
        setConfirmAction(null);
        await refresh();
      },
    });
  };
  const onRevokeInvite = (id, email) => {
    setConfirmAction({
      kind: "revoke_invite",
      title: "Revoke invite?",
      body: html`The link sent to <strong>${email || "this teammate"}</strong> will stop working. They won't be notified — you can send a fresh invite anytime.`,
      confirmLabel: "Revoke",
      onConfirm: async () => {
        const r = await fetch(`/api/org/invites/${id}`, { method: "DELETE" });
        if (!r.ok) throw new Error(`Failed (${r.status})`);
        setConfirmAction(null);
        await refresh();
      },
    });
  };

  const body = html`
      <div class="db-page">
        <div class="db-page-header">
          <h1>Team & invites</h1>
          <p class="db-muted">
            ${org?.name || "Your workspace"} · everyone listed here can see and
            build agents in this org. Roles control who can invite, remove
            members, and delete agents.
          </p>
        </div>

        ${error ? html`<div class="db-error">${error}</div>` : null}
        ${loading ? html`<div class="db-loading">Loading team…</div>` : html`
          <section class="db-card">
            <header class="db-card-head">
              <h2>Members <span class="db-pill-soft">${members.length}</span></h2>
              ${canManage ? html`
                <button class="db-btn-primary" onClick=${() => setInviteOpen(true)}>
                  Invite member
                </button>
              ` : null}
            </header>
            <table class="db-table">
              <thead>
                <tr>
                  <th>Member</th>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Joined</th>
                  ${canManage ? html`<th></th>` : null}
                </tr>
              </thead>
              <tbody>
                ${members.map((m) => html`
                  <tr key=${m.user_id}>
                    <td>
                      <div class="db-member-name">
                        ${m.name || m.email.split("@")[0]}
                        ${m.user_id === currentUser?.id ? html`<span class="db-pill-tiny">you</span>` : null}
                      </div>
                    </td>
                    <td class="db-muted">${m.email}</td>
                    <td>
                      ${canChangeRoles && m.user_id !== currentUser?.id ? html`
                        <select
                          class="db-select-inline"
                          value=${m.role}
                          onChange=${(e) => onChangeRole(m.user_id, e.target.value)}
                        >
                          <option value="owner">Owner</option>
                          <option value="admin">Admin</option>
                          <option value="member">Member</option>
                        </select>
                      ` : html`<span class="db-role-tag db-role-${m.role}">${m.role}</span>`}
                    </td>
                    <td class="db-muted">${new Date(m.joined_at).toLocaleDateString()}</td>
                    ${canManage ? html`
                      <td class="db-row-actions">
                        ${m.user_id === currentUser?.id ? html`
                          <button class="db-btn-ghost-danger" onClick=${() => onRemoveMember(m.user_id, "yourself")}>
                            Leave team
                          </button>
                        ` : html`
                          <button class="db-btn-ghost-danger" onClick=${() => onRemoveMember(m.user_id, m.name)}>
                            Remove
                          </button>
                        `}
                      </td>
                    ` : null}
                  </tr>
                `)}
              </tbody>
            </table>
          </section>

          ${invites.length > 0 || canManage ? html`
            <section class="db-card">
              <header class="db-card-head">
                <h2>Pending invites <span class="db-pill-soft">${invites.length}</span></h2>
              </header>
              ${invites.length === 0 ? html`
                <p class="db-muted db-pad">No pending invites.</p>
              ` : html`
                <table class="db-table">
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Role</th>
                      <th>Expires</th>
                      ${canManage ? html`<th></th>` : null}
                    </tr>
                  </thead>
                  <tbody>
                    ${invites.map((i) => html`
                      <tr key=${i.id}>
                        <td>${i.email}</td>
                        <td><span class="db-role-tag db-role-${i.role}">${i.role}</span></td>
                        <td class="db-muted">${new Date(i.expires_at).toLocaleDateString()}</td>
                        ${canManage ? html`
                          <td class="db-row-actions">
                            <button class="db-btn-ghost-danger" onClick=${() => onRevokeInvite(i.id, i.email)}>
                              Revoke
                            </button>
                          </td>
                        ` : null}
                      </tr>
                    `)}
                  </tbody>
                </table>
              `}
            </section>
          ` : null}
        `}

        ${inviteOpen ? html`
          <${InviteModal}
            onClose=${() => setInviteOpen(false)}
            onCreated=${async () => { setInviteOpen(false); await refresh(); }}
          />
        ` : null}
        ${confirmAction ? html`
          <${DestructiveConfirmModal}
            title=${confirmAction.title}
            body=${confirmAction.body}
            confirmLabel=${confirmAction.confirmLabel}
            onClose=${() => setConfirmAction(null)}
            onConfirm=${confirmAction.onConfirm}
          />
        ` : null}
      </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="team"
      plan=${null}
      agents=${[]}
      onNav=${onNav}
      title="Team"
      subtitle=${org?.name || "Workspace members + invites"}
      body=${body}
    />
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// InviteModal — overlay form. Two fields, one button. Returns the token so
// the inviter can copy + paste into Slack/SMS while the email also fires.
// ─────────────────────────────────────────────────────────────────────────
function InviteModal({ onClose, onCreated }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [link, setLink] = useState(null);

  const submit = async () => {
    setSaving(true); setError(null);
    try {
      const r = await fetch("/api/org/invites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), role }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        setError(e.detail?.message || e.detail || `Failed (${r.status})`);
        return;
      }
      const inv = await r.json();
      setLink(`${location.origin}/invite/${inv.token}`);
    } finally {
      setSaving(false);
    }
  };

  return html`
    <div class="db-modal-backdrop" onClick=${onClose}>
      <div class="db-modal" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>Invite a teammate</h2>
          <button class="db-modal-close" onClick=${onClose}>×</button>
        </header>
        ${link ? html`
          <div class="db-modal-body">
            <p>Invite sent. Copy and share this link too — it works for 7 days.</p>
            <input class="db-input db-mono" readonly value=${link}
              onClick=${(e) => e.target.select()} />
            <div class="db-modal-actions">
              <button class="db-btn-primary" onClick=${() => { onCreated && onCreated(); }}>
                Done
              </button>
            </div>
          </div>
        ` : html`
          <div class="db-modal-body">
            <label>
              Email
              <input
                class="db-input"
                type="email"
                placeholder="teammate@yourdomain.com"
                value=${email}
                onInput=${(e) => setEmail(e.target.value)}
                autoFocus
              />
            </label>
            <label>
              Role
              <select class="db-input" value=${role} onChange=${(e) => setRole(e.target.value)}>
                <option value="member">Member — build + edit agents</option>
                <option value="admin">Admin — also invite + manage members</option>
              </select>
            </label>
            ${error ? html`<div class="db-error">${error}</div>` : null}
            <div class="db-modal-actions">
              <button class="db-btn-ghost" onClick=${onClose}>Cancel</button>
              <button class="db-btn-primary" disabled=${!email.trim() || saving} onClick=${submit}>
                ${saving ? "Sending…" : "Send invite"}
              </button>
            </div>
          </div>
        `}
      </div>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AcceptInvitePage — /invite/:token. Public landing. Fetches the invite
// preview (org name + inviter), then either:
//   - if not logged in → routes to /login with returnTo back here
//   - if logged in → POST /accept and route into /agents
// Decline button is always available, doesn't require auth.
// ─────────────────────────────────────────────────────────────────────────
function AcceptInvitePage({ token, currentUser, onAccepted }) {
  const [invite, setInvite] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Reset both error + invite when the token changes so a previous failure
    // doesn't bleed into a new render. setLoading=true so the spinner shows
    // until the fresh fetch resolves.
    setError(null); setInvite(null); setLoading(true);
    fetch(`/api/invites/${token}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(r.status === 404 ? "Invite not found" : "Failed to load invite");
        return r.json();
      })
      .then((d) => { if (!cancelled) setInvite(d); })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [token]);

  const accept = async () => {
    if (!currentUser) {
      // Stash return path then hop to login.
      sessionStorage.setItem("sxai_invite_token", token);
      history.pushState({}, "", "/login");
      window.dispatchEvent(new PopStateEvent("popstate"));
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(`/api/invites/${token}/accept`, { method: "POST" });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        setError(e.detail?.message || "Couldn't accept this invite.");
        return;
      }
      onAccepted && onAccepted();
    } finally {
      setBusy(false);
    }
  };

  const decline = async () => {
    setBusy(true);
    await fetch(`/api/invites/${token}/decline`, { method: "POST" });
    setBusy(false);
    setInvite((i) => i ? { ...i, status: "declined" } : i);
  };

  return html`
    <div class="sxai-invite-shell">
      <div class="sxai-invite-card">
        <h1>SpiderX.AI</h1>
        ${loading ? html`<p>Loading invite…</p>` : null}
        ${error ? html`<div class="db-error">${error}</div>` : null}
        ${invite && !loading ? html`
          ${invite.status === "pending" ? html`
            <p class="db-muted">${invite.inviter_name || invite.inviter_email || "Someone"} invited you</p>
            <h2>${invite.org_name}</h2>
            <p>as a <strong>${invite.role}</strong> · ${invite.email}</p>
            <div class="db-modal-actions">
              <button class="db-btn-ghost" disabled=${busy} onClick=${decline}>Decline</button>
              <button class="db-btn-primary" disabled=${busy} onClick=${accept}>
                ${currentUser ? "Accept invite" : "Log in to accept"}
              </button>
            </div>
          ` : invite.status === "accepted" ? html`
            <p>This invite was already accepted.</p>
            <button class="db-btn-primary" onClick=${() => location.href = "/agents"}>Go to dashboard</button>
          ` : invite.status === "declined" ? html`
            <p>You've declined this invite. No further action needed.</p>
          ` : invite.status === "revoked" ? html`
            <p>This invite was revoked by the inviter.</p>
          ` : invite.status === "expired" ? html`
            <p>This invite expired. Ask the inviter to send a fresh one.</p>
          ` : null}
        ` : null}
      </div>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
// AdminFilterBar (build 202) — unified filter row for every super-admin
// page. Declared by the host page via the `kinds` prop, e.g.
// `["org","agent","phone","daterange"]`. Renders only the requested
// controls; emits a single `onChange(value)` whenever Apply is hit (not
// on every keystroke — the table reloads are expensive enough that we
// don't want them on a dropdown jitter).
//
// URL-synced via `useAdminFilters(kinds)` so refresh + back/forward
// stays in the same filtered view. The hook is the single source of
// truth — the bar itself is presentational.
//
// Date range default: last 7 days. Presets collapse to the closest
// `days=N` for endpoints that don't accept ISO start/end (e.g.
// /admin/agent-pnl); see `value.days` for that convenience.
// ─────────────────────────────────────────────────────────────────────────

// Date presets — labels are imperative and explicit. "Last 7 days" is
// the default because it covers the common "what happened this week"
// question without dragging months of stale rows into the table.
const ADMIN_DATE_PRESETS = [
  { key: "today",        label: "Today",        days: 1   },
  { key: "yesterday",    label: "Yesterday",    days: 1   },  // start/end offset handles the back-1
  { key: "last_7_days",  label: "Last 7 days",  days: 7   },
  { key: "last_30_days", label: "Last 30 days", days: 30  },
  { key: "last_60_days", label: "Last 60 days", days: 60  },
  { key: "last_90_days", label: "Last 90 days", days: 90  },
  { key: "custom",       label: "Custom range", days: null },
];

// Resolve a preset key → {start, end} ISO strings + days hint. Returns
// nulls for "custom" so the bar knows to render date inputs.
function _adminPresetRange(presetKey) {
  const now = new Date();
  const endOfToday = new Date(now); endOfToday.setHours(23,59,59,999);
  const startOfToday = new Date(now); startOfToday.setHours(0,0,0,0);
  switch (presetKey) {
    case "today": {
      return { start: startOfToday.toISOString(), end: endOfToday.toISOString(), days: 1 };
    }
    case "yesterday": {
      const s = new Date(startOfToday); s.setDate(s.getDate() - 1);
      const e = new Date(endOfToday);  e.setDate(e.getDate() - 1);
      return { start: s.toISOString(), end: e.toISOString(), days: 1 };
    }
    case "last_7_days": {
      const s = new Date(startOfToday); s.setDate(s.getDate() - 6);
      return { start: s.toISOString(), end: endOfToday.toISOString(), days: 7 };
    }
    case "last_30_days": {
      const s = new Date(startOfToday); s.setDate(s.getDate() - 29);
      return { start: s.toISOString(), end: endOfToday.toISOString(), days: 30 };
    }
    case "last_60_days": {
      const s = new Date(startOfToday); s.setDate(s.getDate() - 59);
      return { start: s.toISOString(), end: endOfToday.toISOString(), days: 60 };
    }
    case "last_90_days": {
      const s = new Date(startOfToday); s.setDate(s.getDate() - 89);
      return { start: s.toISOString(), end: endOfToday.toISOString(), days: 90 };
    }
    case "custom":
      return { start: null, end: null, days: null };
    default:
      // Unknown preset falls back to the safe default
      return _adminPresetRange("last_7_days");
  }
}

// Format an ISO timestamp as YYYY-MM-DD for the <input type=date>.
// Empty inputs yield an empty string (browser-friendly) rather than
// "Invalid Date" which the date picker won't accept.
function _adminIsoToInputDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// useAdminFilters — central state hook for the filter bar. Reads
// initial values from `window.location.search` so refresh + back keeps
// you in the same view. `setValue` writes back to the URL via
// `history.replaceState` (replace, not push, so the back button still
// goes to the previous *page*, not the previous *filter state* — that
// would be infuriating).
//
// `signature` is a stable string the host page can stick in a useEffect
// dep array so it reloads exactly when filters change.
function useAdminFilters(kinds) {
  const has = (k) => kinds.includes(k);
  const defaultPreset = "last_7_days";

  const initial = useMemo(() => {
    const qs = new URLSearchParams(window.location.search);
    const presetKey = qs.get("preset") || defaultPreset;
    const range = _adminPresetRange(presetKey);
    return {
      preset: presetKey,
      org_id: qs.get("org_id") || "",
      agent_id: qs.get("agent_id") || "",
      phone: qs.get("phone") || "",
      start: qs.get("start") || range.start || "",
      end:   qs.get("end")   || range.end   || "",
      days: qs.get("days") ? Number(qs.get("days")) : (range.days || 7),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);  // initial only — we don't want URL to spam-update on every render

  const [value, setValueRaw] = useState(initial);

  const writeUrl = (v) => {
    const qs = new URLSearchParams(window.location.search);
    // Only persist the keys this bar exposes; leave unrelated params
    // (?tab= etc.) untouched so other state owners stay in control.
    const keys = ["org_id", "agent_id", "phone", "preset", "start", "end", "days"];
    for (const k of keys) {
      const val = v[k];
      if (val === "" || val == null) qs.delete(k);
      else qs.set(k, String(val));
    }
    const next = qs.toString();
    const url = window.location.pathname + (next ? `?${next}` : "");
    try { window.history.replaceState({}, "", url); } catch {}
  };

  const setValue = (next) => {
    setValueRaw(next);
    writeUrl(next);
  };

  const reset = () => {
    const r = _adminPresetRange(defaultPreset);
    const next = {
      preset: defaultPreset,
      org_id: "", agent_id: "", phone: "",
      start: r.start || "", end: r.end || "", days: r.days || 7,
    };
    setValue(next);
  };

  // Build a URLSearchParams the host page appends to its fetch URL.
  // Only the kinds the host bar advertises get serialised — a page
  // that doesn't include "phone" in its kinds list won't send a stale
  // phone filter even if one was URL-pinned by a previous page.
  const toQuery = () => {
    const qs = new URLSearchParams();
    if (has("org")   && value.org_id)   qs.set("org_id",   value.org_id);
    if (has("agent") && value.agent_id) qs.set("agent_id", value.agent_id);
    if (has("phone") && value.phone)    qs.set("phone",    value.phone);
    if (has("daterange")) {
      if (value.start) qs.set("start", value.start);
      if (value.end)   qs.set("end",   value.end);
      // days is a convenience for endpoints that don't take start/end
      if (value.days)  qs.set("days",  String(value.days));
    }
    return qs;
  };

  // Stable signature for useEffect deps — order MATTERS for a stable
  // dep string, hence the explicit fields rather than JSON.stringify.
  const signature = [
    has("org")   ? value.org_id   : "",
    has("agent") ? value.agent_id : "",
    has("phone") ? value.phone    : "",
    has("daterange") ? (value.preset === "custom"
      ? `c:${value.start || ""}:${value.end || ""}`
      : value.preset) : "",
  ].join("|");

  return { value, setValue, reset, toQuery, signature, kinds };
}

// Controlled filter bar. `state` is the object returned by
// `useAdminFilters`. The bar holds *draft* edits internally and only
// pushes them up via state.setValue on Apply — that's what makes a
// dropdown change cheap (no reload until Apply).
function AdminFilterBar({ state, orgs, agents }) {
  const has = (k) => state.kinds.includes(k);
  // Draft state — committed to URL/state only on Apply
  const [draft, setDraft] = useState(state.value);
  // Keep draft synced if state.value changes from URL (e.g. browser
  // back). useMemo cheaper than useEffect for this — but useEffect is
  // semantically clearer and we already pay one render either way.
  useEffect(() => { setDraft(state.value); }, [state.signature]);

  // When org changes, scope the agent dropdown to that org so the user
  // doesn't have to scroll through 100 agents from other orgs.
  const visibleAgents = (agents || []).filter((a) =>
    !draft.org_id || String(a.org_id) === String(draft.org_id)
  );

  const presetChange = (key) => {
    const r = _adminPresetRange(key);
    setDraft((d) => ({
      ...d,
      preset: key,
      start: r.start || d.start,
      end:   r.end   || d.end,
      days:  r.days  || d.days,
    }));
  };

  const apply = () => state.setValue(draft);
  const resetAll = () => state.reset();

  // Active-filter count for the badge — what the user perceives as
  // "I have 3 filters applied" excluding the always-on date range.
  const activeCount = [
    has("org")   && state.value.org_id,
    has("agent") && state.value.agent_id,
    has("phone") && state.value.phone,
    has("daterange") && state.value.preset !== "last_7_days",
  ].filter(Boolean).length;

  return html`
    <div class="ax-filterbar">
      ${has("org") ? html`
        <label class="ax-fb-field">
          <span class="ax-fb-label">Org</span>
          <select class="ax-fb-select" value=${draft.org_id}
                  onChange=${(e) => setDraft((d) => ({ ...d, org_id: e.target.value, agent_id: "" }))}>
            <option value="">All orgs</option>
            ${(orgs || []).map((o) => html`
              <option key=${o.id} value=${String(o.id)}>${o.name}</option>
            `)}
          </select>
        </label>
      ` : ""}

      ${has("agent") ? html`
        <label class="ax-fb-field">
          <span class="ax-fb-label">Agent</span>
          <select class="ax-fb-select" value=${draft.agent_id}
                  onChange=${(e) => setDraft((d) => ({ ...d, agent_id: e.target.value }))}>
            <option value="">All agents</option>
            ${visibleAgents.map((a) => html`
              <option key=${a.id} value=${String(a.id)}>
                ${a.name}${a.org_name ? ` · ${a.org_name}` : ""}
              </option>
            `)}
          </select>
        </label>
      ` : ""}

      ${has("phone") ? html`
        <label class="ax-fb-field ax-fb-field-grow">
          <span class="ax-fb-label">Phone</span>
          <input class="ax-fb-input" type="search"
                 placeholder="Search caller phone…"
                 value=${draft.phone}
                 onInput=${(e) => setDraft((d) => ({ ...d, phone: e.target.value }))} />
        </label>
      ` : ""}

      ${has("daterange") ? html`
        <label class="ax-fb-field">
          <span class="ax-fb-label">Range</span>
          <select class="ax-fb-select" value=${draft.preset}
                  onChange=${(e) => presetChange(e.target.value)}>
            ${ADMIN_DATE_PRESETS.map((p) => html`
              <option key=${p.key} value=${p.key}>${p.label}</option>
            `)}
          </select>
        </label>
        ${draft.preset === "custom" ? html`
          <label class="ax-fb-field">
            <span class="ax-fb-label">From</span>
            <input class="ax-fb-input ax-fb-input-date" type="date"
                   value=${_adminIsoToInputDate(draft.start)}
                   onChange=${(e) => {
                     const d = e.target.value ? new Date(e.target.value + "T00:00:00") : null;
                     setDraft((dr) => ({ ...dr, start: d ? d.toISOString() : "" }));
                   }} />
          </label>
          <label class="ax-fb-field">
            <span class="ax-fb-label">To</span>
            <input class="ax-fb-input ax-fb-input-date" type="date"
                   value=${_adminIsoToInputDate(draft.end)}
                   onChange=${(e) => {
                     const d = e.target.value ? new Date(e.target.value + "T23:59:59") : null;
                     setDraft((dr) => ({ ...dr, end: d ? d.toISOString() : "" }));
                   }} />
          </label>
        ` : ""}
      ` : ""}

      <div class="ax-fb-actions">
        <button class="ax-fb-apply" type="button" onClick=${apply}>Apply</button>
        <button class="ax-fb-reset" type="button" onClick=${resetAll}>Reset</button>
        ${activeCount > 0 ? html`
          <span class="ax-fb-badge" title="Active filters beyond the default 7-day window">
            ${activeCount} active
          </span>
        ` : ""}
      </div>
    </div>
  `;
}

// useAdminLookups — single fetch of the org + agent dropdown data,
// cached for the lifetime of the SPA so navigating between admin
// pages doesn't re-fetch. Returns `{orgs, agents}` plus `reload()`.
// Module-level cache because hooks state is per-component instance.
let _ADMIN_LOOKUPS_CACHE = null;
function useAdminLookups() {
  const [data, setData] = useState(_ADMIN_LOOKUPS_CACHE || { orgs: null, agents: null });
  useEffect(() => {
    if (_ADMIN_LOOKUPS_CACHE) return;
    Promise.all([
      fetch("/api/admin/orgs-lookup").then((r) => r.ok ? r.json() : []),
      fetch("/api/admin/agents-lookup").then((r) => r.ok ? r.json() : []),
    ]).then(([orgs, agents]) => {
      _ADMIN_LOOKUPS_CACHE = { orgs, agents };
      setData(_ADMIN_LOOKUPS_CACHE);
    }).catch(() => setData({ orgs: [], agents: [] }));
  }, []);
  return {
    orgs: data.orgs || [],
    agents: data.agents || [],
    loaded: data.orgs !== null,
    reload: () => { _ADMIN_LOOKUPS_CACHE = null; setData({ orgs: null, agents: null }); },
  };
}


// AdminShell — /admin and /admin/<section>. Phase 3 super-admin surface.
// UI-gates on me.is_super_admin AND every API call is independently gated,
// so a non-admin who guesses the URL sees a clear "no access" instead of
// a half-loaded shell that 403s on each fetch.
//
// Sections: summary | orgs | users | calls | audit | super-admins
// Each tab is a paginated table; admin actions (grant, revoke, plan
// override) audit-log automatically on the backend.
// ─────────────────────────────────────────────────────────────────────────
function AdminShell({ section, currentUser, onNav }) {
  const sec = section || "summary";

  if (!currentUser?.is_super_admin) {
    return html`
      <div class="sxai-invite-shell">
        <div class="sxai-invite-card">
          <h1>SpiderX.AI</h1>
          <h2>Restricted</h2>
          <p>The admin surface is only available to platform super-admins.</p>
          <div class="db-modal-actions">
            <button class="db-btn-primary" onClick=${() => onNav("/agents")}>Back to dashboard</button>
          </div>
        </div>
      </div>
    `;
  }

  // Build 200: sidebar redesigned to match the CEO's reference — a
  // pinned "Dashboard" item at the top, then collapsible category
  // sections in uppercase tracking-wider type (DAILY OPS / SYSTEM /
  // ...), each with icon + label rows. The active item gets a
  // pink/purple gradient highlight. SessionStorage persists open
  // groups under `sxai.admin_nav_open`.
  //
  // Internal section keys are PRESERVED from build 198 so every
  // existing /admin/<section> URL still resolves; only the chrome
  // and grouping change.
  const Icon = {
    dashboard: html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>`,
    orgs:      html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 21V9l9-6 9 6v12"/><path d="M9 21V12h6v9"/></svg>`,
    users:     html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`,
    shield:    html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
    calls:     html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M22 16.92v3a2 2 0 0 1-2.18 2A19.79 19.79 0 0 1 2.08 4.18 2 2 0 0 1 4 2h3a2 2 0 0 1 2 1.72 13 13 0 0 0 .67 2.81 2 2 0 0 1-.45 2.11L8 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 13 13 0 0 0 2.81.67A2 2 0 0 1 22 16.92z"/></svg>`,
    pnl:       html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 2v20M5 9h14M5 15h14"/></svg>`,
    obs:       html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-7"/></svg>`,
    audit:     html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h6"/></svg>`,
    chart:     html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`,
    ledger:    html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>`,
    cog:       html`<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
  };

  // Pinned items (above the group sections, no header)
  const pinned = [
    { key: "summary", label: "Dashboard", icon: Icon.dashboard },
  ];
  // Grouped sections (collapsible). Visual order matches the reference.
  const groups = [
    {
      key: "daily-ops", label: "Daily ops",
      items: [
        { key: "orgs",           label: "Organisations", icon: Icon.orgs },
        { key: "users",          label: "Users",         icon: Icon.users },
        { key: "calls",          label: "Calls",         icon: Icon.calls },
        { key: "agent-pnl",      label: "Agent P&L",     icon: Icon.pnl },
        { key: "observability",  label: "Observability", icon: Icon.obs },
      ],
    },
    {
      key: "platform-config", label: "Platform",
      items: [
        { key: "analytics",     label: "Analytics",      icon: Icon.chart },
        { key: "llm",           label: "LLM ledger",     icon: Icon.ledger },
        { key: "audit",         label: "Audit log",      icon: Icon.audit },
      ],
    },
    {
      key: "system", label: "System",
      items: [
        { key: "super-admins",  label: "Super-admins",   icon: Icon.shield },
        { key: "settings",      label: "Platform settings", icon: Icon.cog },
      ],
    },
  ];

  // Which group is the active section under? Auto-open it on mount;
  // persist any operator-driven open/closed toggles in sessionStorage
  // keyed `sxai.admin_nav_open` so a fresh tab doesn't lose your state.
  const NAV_KEY = "sxai.admin_nav_open";
  const initialOpen = (() => {
    try {
      const stored = JSON.parse(sessionStorage.getItem(NAV_KEY) || "{}");
      // Always open the group containing the current section
      const activeGroup = groups.find((g) => g.items.some((i) => i.key === sec));
      if (activeGroup) stored[activeGroup.key] = true;
      return stored;
    } catch { return {}; }
  })();
  const [openGroups, setOpenGroups] = useState(initialOpen);
  const toggleGroup = (gk) => {
    setOpenGroups((prev) => {
      const next = { ...prev, [gk]: !prev[gk] };
      try { sessionStorage.setItem(NAV_KEY, JSON.stringify(next)); } catch {}
      return next;
    });
  };

  const goSection = (sectionKey) => {
    onNav(`/admin/${sectionKey === "summary" ? "" : sectionKey}`);
  };

  return html`
    <div class="db-admin-shell db-admin-shell-vsplit ax-shell">
      <header class="ax-topbar">
        <a class="ax-brand ax-brand-logo" href="/agents" onClick=${(e) => { e.preventDefault(); onNav("/agents"); }}>
          <${SpiderXLogo} height=${22} />
        </a>
        <span class="ax-topbar-label">Platform admin</span>
        <span class="ax-pill ax-pill-internal">
          <span class="ax-pill-dot"></span>
          INTERNAL
        </span>
        <div class="ax-topbar-spacer"></div>
        <button class="ax-exit" onClick=${() => onNav("/agents")}>Exit admin</button>
      </header>
      <div class="db-admin-body ax-body">
        <aside class="ax-side">
          <div class="ax-side-scroll">
            <!-- Pinned items (no section header) -->
            <div class="ax-pinned">
              ${pinned.map((it) => html`
                <button key=${it.key}
                        class=${"ax-nav-item" + (it.key === sec ? " is-active" : "")}
                        onClick=${() => goSection(it.key)}>
                  <span class="ax-nav-icon">${it.icon}</span>
                  <span class="ax-nav-label">${it.label}</span>
                </button>
              `)}
            </div>
            <!-- Grouped sections -->
            ${groups.map((g) => html`
              <div key=${g.key} class=${"ax-group" + (openGroups[g.key] ? " is-open" : "")}>
                <button class="ax-group-head" onClick=${() => toggleGroup(g.key)}>
                  <span class="ax-group-label">${g.label.toUpperCase()}</span>
                  <svg class="ax-group-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
                </button>
                ${openGroups[g.key] ? html`
                  <div class="ax-group-items">
                    ${g.items.map((it) => html`
                      <button key=${it.key}
                              class=${"ax-nav-item" + (it.key === sec ? " is-active" : "")}
                              onClick=${() => goSection(it.key)}>
                        <span class="ax-nav-icon">${it.icon}</span>
                        <span class="ax-nav-label">${it.label}</span>
                      </button>
                    `)}
                  </div>
                ` : ""}
              </div>
            `)}
          </div>
          <!-- Footer: access status + user -->
          <div class="ax-side-foot">
            <div class="ax-access">
              <span class="ax-access-dot"></span>
              <span>ACCESS · OPEN</span>
            </div>
            <div class="ax-side-user">${currentUser?.email || "—"}</div>
          </div>
        </aside>
        <main class="db-admin-main db-admin-main-vsplit ax-main">
          ${sec === "summary" ? html`<${AdminSummary} />`
            : sec === "analytics" ? html`<${AdminAnalytics} />`
            : sec === "llm" ? html`<${AdminLlmLedger} />`
            : sec === "orgs" || sec === "organisations" ? html`<${AdminOrgs} />`
            : sec === "users" ? html`<${AdminUsers} />`
            : sec === "calls" ? html`<${AdminCalls} />`
            : sec === "agent-pnl" ? html`<${AdminAgentPnl} />`
            : sec === "audit" ? html`<${AdminAudit} />`
            : sec === "observability" ? html`<${AdminObservability} />`
            : sec === "settings" ? html`<${AdminSettings} />`
            : sec === "super-admins" ? html`<${AdminSuperAdmins} currentUser=${currentUser} />`
            : html`<p>Unknown section.</p>`
          }
        </main>
      </div>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AdminObservability — /admin/observability. Build 200 redesign.
// Reference-design adaptation: title + status pill row, search + 2
// dropdowns, data-table with KIND chip + SEVERITY pill + View → action.
// Clicking any row opens a detail drawer with the full JSON payload.
// ─────────────────────────────────────────────────────────────────────────
function AdminObservability() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState({ severity: "", kind_prefix: "", q: "" });
  const [schedulers, setSchedulers] = useState([]);
  const [tab, setTab] = useState("feed");  // feed | schedulers | pricing
  const [detailEvent, setDetailEvent] = useState(null);
  // Build 202: unified AdminFilterBar — org, agent, daterange (events
  // can be attributed to either; phone isn't meaningful here since
  // events don't carry a caller number).
  const fb = useAdminFilters(["org", "agent", "daterange"]);
  const lookups = useAdminLookups();

  const load = () => {
    const qs = fb.toQuery();
    if (filter.severity) qs.set("severity", filter.severity);
    if (filter.kind_prefix) qs.set("kind_prefix", filter.kind_prefix);
    qs.set("limit", "200");
    fetch(`/api/admin/events?${qs}`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then((d) => { setData(d); setErr(""); })
      .catch((e) => setErr(String(e.message || e)));
    fetch("/api/admin/schedulers")
      .then((r) => r.ok ? r.json() : [])
      .then((d) => setSchedulers(Array.isArray(d) ? d : []))
      .catch(() => {});
  };
  useEffect(() => {
    load();
    // Auto-refresh while the page is open. 15s is short enough to feel
    // alive without hammering the DB on a quiet platform.
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [filter.severity, filter.kind_prefix, fb.signature]);

  const resolveEvent = async (id) => {
    try {
      await fetch(`/api/admin/events/${id}/resolve`, { method: "POST" });
      load();
    } catch {}
  };
  const runJob = async (name) => {
    try {
      await fetch(`/api/admin/schedulers/${encodeURIComponent(name)}/run`, { method: "POST" });
      setTimeout(load, 800);  // give the job a beat to write events
    } catch {}
  };

  const fmtAgo = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s/60)}m ago`;
    if (s < 86400) return `${Math.round(s/3600)}h ago`;
    return `${Math.round(s/86400)}d ago`;
  };
  // fmtAbs — short absolute timestamp for the WHEN column. Matches the
  // reference's "Jun 4, 2026 7 AM" style.
  const fmtAbs = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  };
  const sevDot = (sev) => {
    const map = {
      info: "#94a3b8", warning: "#f59e0b",
      error: "#ef4444", critical: "#dc2626",
    };
    return map[sev] || "#94a3b8";
  };

  const counts = data?.counts || {};
  const items = data?.items || [];

  // Local search filter (client-side over the already-loaded set). We
  // filter on title + kind + message so a quick "rohan" or "drift"
  // narrows the list without round-tripping the server.
  const q = filter.q.trim().toLowerCase();
  const visibleItems = !q ? items : items.filter((e) =>
    (e.title || "").toLowerCase().includes(q)
    || (e.kind || "").toLowerCase().includes(q)
    || (e.message || "").toLowerCase().includes(q)
  );

  // Status pill — maps severity → bucket label matching the reference's
  // "sent / deduped / failed" affordance. We use sev as the visible
  // status so the page reads like the design.
  const statusOf = (e) => {
    if (e.severity === "error" || e.severity === "critical") return "failed";
    if (e.resolved_at) return "resolved";
    return "sent";
  };

  // Counts for the chip row across the FULL items set (not the search
  // filter — those numbers are about platform health, not the current
  // text query).
  const sentN = items.filter((e) => statusOf(e) === "sent" || statusOf(e) === "resolved").length;
  const failedN = items.filter((e) => statusOf(e) === "failed").length;
  const dedupedN = 0;  // future: count rows with conflict (dedupe_key match)
  const totalN = items.length;

  return html`
    <h1>Observability events</h1>
    <p class="ax-sub">Every noteworthy thing the platform does writes a row here — sent, deduped or failed, newest first. Click any row to see the full JSON payload.</p>

    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />

    <!-- Status pills — top of the page, reference-style -->
    <div class="ax-statusrow">
      <span class="ax-stat-pill ax-stat-total">${totalN} total</span>
      <span class="ax-stat-pill"><span class="ax-stat-dot stat-sent"></span>${sentN} sent</span>
      <span class="ax-stat-pill"><span class="ax-stat-dot stat-deduped"></span>${dedupedN} deduped</span>
      <span class="ax-stat-pill"><span class="ax-stat-dot stat-failed"></span>${failedN} failed</span>
    </div>

    <!-- Search + filter dropdowns + count badge -->
    <div class="ax-toolbar">
      <input class="ax-search" type="search"
             placeholder="Search by kind, title, message id, recipient…"
             value=${filter.q}
             onInput=${(e) => setFilter((f) => ({ ...f, q: e.target.value }))} />
      <select class="ax-select" value=${filter.kind_prefix} onChange=${(e) => setFilter((f) => ({ ...f, kind_prefix: e.target.value }))}>
        <option value="">All kinds</option>
        <option value="agent">agent.*</option>
        <option value="call">call.*</option>
        <option value="cost">cost.*</option>
        <option value="pricing">pricing.*</option>
        <option value="notify">notify.*</option>
        <option value="quality">quality.*</option>
        <option value="system">system.*</option>
      </select>
      <select class="ax-select" value=${filter.severity} onChange=${(e) => setFilter((f) => ({ ...f, severity: e.target.value }))}>
        <option value="">All severities</option>
        <option value="info">info</option>
        <option value="warning">warning</option>
        <option value="error">error</option>
        <option value="critical">critical</option>
      </select>
      <span class="ax-toolbar-count">${visibleItems.length} event${visibleItems.length === 1 ? "" : "s"}</span>
    </div>

    <!-- Sub-tab bar — Live feed is the default; Schedulers + Pricing kept -->
    <div class="ax-subtabs">
      <button class=${"ax-subtab" + (tab === "feed" ? " is-active" : "")} onClick=${() => setTab("feed")}>Live feed</button>
      <button class=${"ax-subtab" + (tab === "schedulers" ? " is-active" : "")} onClick=${() => setTab("schedulers")}>Schedulers</button>
      <button class=${"ax-subtab" + (tab === "pricing" ? " is-active" : "")} onClick=${() => setTab("pricing")}>Pricing</button>
    </div>

    ${tab === "feed" ? html`
      ${err ? html`<div class="db-form-help" style=${{ color: "#b91c1c", marginBottom: "10px" }}>Couldn't load: ${err}</div>` : ""}
      <!-- Data table — left-bordered row per severity, KIND chip + STATUS
           pill + View → action. Click anywhere on the row to open the
           detail drawer. -->
      <div class="ax-table-wrap">
        <table class="ax-table">
          <thead>
            <tr>
              <th>WHEN</th>
              <th>KIND</th>
              <th>TITLE</th>
              <th>STATUS</th>
              <th class="ax-th-right"></th>
            </tr>
          </thead>
          <tbody>
            ${visibleItems.length === 0 ? html`
              <tr><td colspan="5" class="ax-empty-cell">No events match this filter.</td></tr>
            ` : visibleItems.map((e) => {
              const status = statusOf(e);
              return html`
                <tr key=${e.id} class=${"ax-row ax-row-" + e.severity + (e.resolved_at ? " is-resolved" : "")}
                    onClick=${() => setDetailEvent(e)}>
                  <td class="ax-cell-when">${fmtAbs(e.created_at)}</td>
                  <td><span class=${"ax-kind-chip ax-kind-" + (e.kind.split(".")[0] || "x")}>${e.kind}</span></td>
                  <td class="ax-cell-title">${e.title}</td>
                  <td>
                    <span class=${"ax-status-pill ax-status-" + status}>
                      ${status === "failed" ? "✗" : status === "resolved" ? "✓" : "✓"} ${status.toUpperCase()}
                    </span>
                  </td>
                  <td class="ax-th-right">
                    <button class="ax-view" type="button"
                            onClick=${(ev) => { ev.stopPropagation(); setDetailEvent(e); }}>
                      View →
                    </button>
                  </td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </div>

      ${detailEvent ? html`
        <${EventDetailDrawer}
          event=${detailEvent}
          onClose=${() => setDetailEvent(null)}
          onResolve=${async () => { await resolveEvent(detailEvent.id); setDetailEvent(null); }} />
      ` : ""}
    ` : ""}

    ${tab === "schedulers" ? html`
      <div class="db-obs-schedulers">
        <table class="db-table">
          <thead>
            <tr><th>Job</th><th>Cron</th><th>Timezone</th><th>Last run</th><th></th></tr>
          </thead>
          <tbody>
            ${schedulers.length === 0 ? html`
              <tr><td colspan="5" class="db-muted" style=${{ textAlign: "center", padding: "24px" }}>No jobs registered.</td></tr>
            ` : schedulers.map((j) => html`
              <tr key=${j.name}>
                <td><code>${j.name}</code></td>
                <td><code>${j.cron}</code></td>
                <td>${j.tz}</td>
                <td>${j.last_run ? fmtAgo(j.last_run) : "—"}</td>
                <td class="db-table-td-right">
                  <button class="db-btn-ghost db-btn-sm" type="button" onClick=${() => runJob(j.name)}>Run now</button>
                </td>
              </tr>
            `)}
          </tbody>
        </table>
        <p class="db-form-help" style=${{ marginTop: "16px" }}>
          Jobs run in the FastAPI process. Failed runs emit <code>system.scheduler.run.missed</code> events visible in the live feed.
        </p>
      </div>
    ` : ""}

    ${tab === "pricing" ? html`<${AdminPricingTab} />` : ""}
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// EventDetailDrawer — slides in from the right when a row's "View →"
// is clicked. Shows the event header, message, payload as pretty JSON,
// and a Resolve action for actionable severities. Click outside or the
// × closes it.
// ─────────────────────────────────────────────────────────────────────────
function EventDetailDrawer({ event, onClose, onResolve }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const created = event.created_at ? new Date(event.created_at).toLocaleString() : "—";
  const canResolve = !event.resolved_at && (event.severity === "warning" || event.severity === "error" || event.severity === "critical");
  return html`
    <div class="ax-drawer-backdrop" onClick=${onClose}>
      <aside class="ax-drawer" onClick=${(e) => e.stopPropagation()}>
        <header class="ax-drawer-head">
          <div>
            <div class="ax-drawer-eyebrow">Event #${event.id}</div>
            <h3 class="ax-drawer-title">${event.title}</h3>
          </div>
          <button class="ax-drawer-close" type="button" aria-label="Close" onClick=${onClose}>×</button>
        </header>
        <div class="ax-drawer-body">
          <dl class="ax-kv">
            <dt>Kind</dt>
            <dd><code>${event.kind}</code></dd>
            <dt>Severity</dt>
            <dd><span class=${"ax-status-pill ax-status-" + (event.severity === "error" || event.severity === "critical" ? "failed" : "sent")}>${event.severity}</span></dd>
            <dt>Source</dt>
            <dd>${event.source}</dd>
            <dt>When</dt>
            <dd>${created}</dd>
            ${event.agent_id ? html`<dt>Agent ID</dt><dd>${event.agent_id}</dd>` : ""}
            ${event.org_id ? html`<dt>Org ID</dt><dd>${event.org_id}</dd>` : ""}
            ${event.user_id ? html`<dt>User ID</dt><dd>${event.user_id}</dd>` : ""}
            ${event.dedupe_key ? html`<dt>Dedupe key</dt><dd><code>${event.dedupe_key}</code></dd>` : ""}
            ${event.resolved_at ? html`<dt>Resolved</dt><dd>${new Date(event.resolved_at).toLocaleString()} (by user ${event.resolved_by || "?"})</dd>` : ""}
          </dl>
          ${event.message ? html`
            <div class="ax-drawer-section">
              <div class="ax-drawer-label">Message</div>
              <div class="ax-drawer-msg">${event.message}</div>
            </div>
          ` : ""}
          ${event.payload && Object.keys(event.payload).length > 0 ? html`
            <div class="ax-drawer-section">
              <div class="ax-drawer-label">Payload</div>
              <pre class="ax-drawer-json">${JSON.stringify(event.payload, null, 2)}</pre>
            </div>
          ` : ""}
        </div>
        ${canResolve ? html`
          <footer class="ax-drawer-foot">
            <button class="db-btn-primary" type="button" onClick=${onResolve}>Resolve event</button>
            <button class="db-btn-ghost" type="button" onClick=${onClose}>Close</button>
          </footer>
        ` : html`
          <footer class="ax-drawer-foot">
            <button class="db-btn-ghost" type="button" onClick=${onClose}>Close</button>
          </footer>
        `}
      </aside>
    </div>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AdminAgentPnl — /admin/agent-pnl. Build 199.
// Per-agent COGS roll-up. Hits /api/admin/agent-pnl?days=N and renders
// a table sorted by total cost descending. Each row drills through to
// the agent's overview page. Future build: revenue + margin columns
// once plans.monthly_inr exists.
// ─────────────────────────────────────────────────────────────────────────
function AdminAgentPnl() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  // Build 202: AdminFilterBar — org, agent, daterange. The bar's
  // `days` field is what /admin/agent-pnl actually consumes; ISO
  // start/end are ignored by this endpoint but stay in the URL so
  // when the user switches to another admin page the range carries.
  const fb = useAdminFilters(["org", "agent", "daterange"]);
  const lookups = useAdminLookups();
  const days = fb.value.days || 30;
  useEffect(() => {
    setData(null); setErr("");
    const qs = fb.toQuery();
    qs.set("days", String(days));
    fetch(`/api/admin/agent-pnl?${qs}`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then(setData).catch((e) => setErr(String(e.message || e)));
  }, [fb.signature]);
  if (err) return html`<div class="db-form-help" style=${{ color: "#b91c1c" }}>Couldn't load: ${err}</div>`;
  if (!data) return html`<div class="db-loading">Loading P&L…</div>`;
  const agents = data.agents || [];
  const totals = agents.reduce((acc, a) => {
    acc.calls += a.calls_n;
    acc.minutes += a.minutes;
    acc.cost_llm += a.cost_paise_llm;
    acc.telephony += a.telephony_paise_estimate;
    acc.cogs += a.total_cogs_paise;
    return acc;
  }, { calls: 0, minutes: 0, cost_llm: 0, telephony: 0, cogs: 0 });

  return html`
    <h1>Agent P&L <span class="db-pill-soft">last ${days}d</span></h1>
    <p class="db-admin-sub">Per-agent COGS for the period — LLM cost (frozen at call time) + telephony estimate (today's Plivo rate × minutes). Revenue / margin will land once <code>plans</code> carries rate fields.</p>

    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />

    <div class="db-admin-grid db-pnl-totals">
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Agents with traffic</div>
        <div class="db-admin-tile-value">${agents.filter((a) => a.calls_n > 0).length}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Calls</div>
        <div class="db-admin-tile-value">${totals.calls}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Minutes</div>
        <div class="db-admin-tile-value">${totals.minutes.toFixed(1)}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">LLM cost</div>
        <div class="db-admin-tile-value">₹${(totals.cost_llm/100).toFixed(2)}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Telephony est.</div>
        <div class="db-admin-tile-value">₹${(totals.telephony/100).toFixed(2)}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Total COGS</div>
        <div class="db-admin-tile-value">₹${(totals.cogs/100).toFixed(2)}</div>
      </div>
    </div>

    <table class="db-table db-pnl-table">
      <thead>
        <tr>
          <th>Agent</th>
          <th>Org</th>
          <th>Status</th>
          <th class="db-table-th-right">Calls</th>
          <th class="db-table-th-right">Minutes</th>
          <th class="db-table-th-right">LLM ₹</th>
          <th class="db-table-th-right">Telephony ₹</th>
          <th class="db-table-th-right"><b>COGS ₹</b></th>
          <th class="db-table-th-right">COGS / min</th>
        </tr>
      </thead>
      <tbody>
        ${agents.length === 0 ? html`
          <tr><td colspan="9" class="db-muted" style=${{ textAlign: "center", padding: "24px" }}>No agents yet.</td></tr>
        ` : agents.map((a) => html`
          <tr key=${a.agent_id}>
            <td>
              <a href=${`/agent/${a.slug || a.agent_id}`}
                 class="db-link" style=${{ fontWeight: 500 }}>${a.name}</a>
              <div style=${{ fontSize: "11.5px", color: "#9095a3" }}>${a.sector || "—"} · ${a.locale || "—"}</div>
            </td>
            <td>${a.org_name || html`<span class="db-muted">—</span>`}</td>
            <td>
              ${a.published
                ? html`<span class="db-tag db-tag-green">live</span>`
                : html`<span class="db-tag db-tag-grey">draft</span>`}
            </td>
            <td class="db-table-td-right">${a.calls_n}</td>
            <td class="db-table-td-right">${a.minutes.toFixed(1)}</td>
            <td class="db-table-td-right">₹${(a.cost_paise_llm/100).toFixed(2)}</td>
            <td class="db-table-td-right">₹${(a.telephony_paise_estimate/100).toFixed(2)}</td>
            <td class="db-table-td-right"><b>₹${(a.total_cogs_paise/100).toFixed(2)}</b></td>
            <td class="db-table-td-right db-muted">
              ${a.cogs_per_min_paise > 0 ? html`₹${(a.cogs_per_min_paise/100).toFixed(2)}` : "—"}
            </td>
          </tr>
        `)}
      </tbody>
    </table>

    <p class="db-form-help" style=${{ marginTop: "12px" }}>
      Telephony estimate uses today's Plivo per-min rate × minutes. Web/test
      calls don't actually touch PSTN, so the real number is somewhere
      between LLM-only and this estimate — build 200 will stamp the true
      telephony cost on every call row.
    </p>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AdminPricingTab — /admin/observability (Pricing tab). Build 199.
// Shows: current effective rates (from pricing_versions) joined with the
// latest observed-rate event per (provider, rate_kind) so the operator
// can see "what we charge against" vs "what wholesale really is right
// now". An open `pricing.drift.detected` event surfaces as a yellow/red
// row with a "Roll forward to observed" action that closes the current
// version + writes a new one + resolves the drift event in one txn.
// ─────────────────────────────────────────────────────────────────────────
function AdminPricingTab() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  // Build 203: manual price-check. The daily_price_check scheduler
  // runs at 05:00 IST automatically, but ops needs an on-demand
  // path — "I just rolled forward Plivo's INR rate, let me re-check
  // before EOD." We re-use the existing scheduler run-now endpoint
  // (POST /admin/schedulers/{name}/run) so there's one canonical
  // trigger path; the button is just a UX shortcut on this page.
  const [checking, setChecking] = useState(false);
  const [lastCheck, setLastCheck] = useState(null);
  const load = () => {
    fetch("/api/admin/pricing/current")
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("status " + r.status)))
      .then(setData).catch((e) => setMsg(String(e.message || e)));
    // Pull the scheduler's last-run so the header can show a fresh
    // "Last checked Xm ago" badge that matches the Schedulers tab.
    fetch("/api/admin/schedulers")
      .then((r) => r.ok ? r.json() : [])
      .then((rows) => {
        const job = (Array.isArray(rows) ? rows : []).find((j) => j.name === "daily_price_check");
        setLastCheck(job?.last_run || null);
      })
      .catch(() => {});
  };
  useEffect(() => { load(); }, []);

  // Trigger the price-monitor scrape on-demand. The job runs Gemini +
  // Twilio + Plivo scrapes serially, which can take 5-15s, so we
  // show a spinner and disable the button until the POST returns.
  // When it finishes, `pricing.observed` + `pricing.drift.detected`
  // events have been emitted, so reloading the table immediately
  // surfaces the new observed values and any new drifts.
  const checkRatesNow = async () => {
    if (checking) return;
    setChecking(true);
    setMsg("Checking wholesale rates from Gemini, Twilio and Plivo…");
    try {
      const r = await fetch("/api/admin/schedulers/daily_price_check/run", { method: "POST" });
      if (!r.ok) throw new Error("status " + r.status);
      setMsg("Checked ✓ — observed rates refreshed.");
      load();
    } catch (e) {
      setMsg(`Failed: ${e.message || e}`);
    } finally {
      setChecking(false);
    }
  };

  // "12m ago" / "3h ago" — same helper shape as the Observability
  // page's fmtAgo but local to keep the component self-contained.
  const fmtAgo = (iso) => {
    if (!iso) return "never";
    const d = new Date(iso);
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s/60)}m ago`;
    if (s < 86400) return `${Math.round(s/3600)}h ago`;
    return `${Math.round(s/86400)}d ago`;
  };
  if (!data) return html`<div class="db-loading">Loading pricing…</div>`;
  const rates = data.rates || [];
  const observed = data.observed || [];
  const drifts = data.drifts || [];

  // Build a lookup: for each (provider+rate_kind+model_id), what was the
  // most-recent observed value? Walks observed events newest-first and
  // keeps the first hit per key (since the events were already sorted
  // DESC by id from the API).
  const obsByKey = {};
  for (const ev of observed) {
    const p = ev.payload || {};
    const provider = p.provider;
    const model = p.model || null;
    if (!provider) continue;
    // The price-monitor emits separate rows per (in/out) for LLM and a
    // single row per (in/out) for telephony. Detect by payload shape.
    if (p.observed_usd_per_1m) {
      const inKey = `${provider}|llm.audio.in|${model || ""}`;
      const outKey = `${provider}|llm.audio.out|${model || ""}`;
      if (!obsByKey[inKey]) obsByKey[inKey] = { usd: p.observed_usd_per_1m.in, at: ev.created_at, ev };
      if (!obsByKey[outKey]) obsByKey[outKey] = { usd: p.observed_usd_per_1m.out, at: ev.created_at, ev };
    } else if (p.observed_usd_per_min || p.outbound_mobile_usd_per_min) {
      // Twilio's snapshot path uses outbound_mobile_usd_per_min; the
      // live API path uses observed_usd_per_min. Accept both.
      const k = `${provider}|pstn.outbound.mobile|`;
      if (!obsByKey[k]) obsByKey[k] = {
        usd: p.observed_usd_per_min || p.outbound_mobile_usd_per_min,
        at: ev.created_at, ev,
      };
    } else if (p.observed_inr_per_min) {
      const k = `${provider}|pstn.outbound.mobile|`;
      if (!obsByKey[k]) obsByKey[k] = { inr: p.observed_inr_per_min, at: ev.created_at, ev };
    }
  }

  const driftByKey = {};
  for (const ev of drifts) {
    const p = ev.payload || {};
    const provider = p.provider;
    if (!provider) continue;
    if (p.model) {
      driftByKey[`${provider}|llm.audio.in|${p.model}`] = ev;
      driftByKey[`${provider}|llm.audio.out|${p.model}`] = ev;
    } else {
      driftByKey[`${provider}|pstn.outbound.mobile|`] = ev;
    }
  }

  const rollForward = async (rate, obs) => {
    if (!obs) return;
    if (busy) return;
    setBusy(true);
    setMsg("");
    try {
      const body = {
        provider: rate.provider,
        rate_kind: rate.rate_kind,
        model_id: rate.model_id,
        unit: rate.unit,
        usd_per_unit: obs.usd ?? null,
        inr_per_unit: obs.inr ?? null,
        note: `Promoted observed rate (${obs.at})`,
        observed_event_id: obs.ev?.id,
        resolve_drift_event_id: driftByKey[`${rate.provider}|${rate.rate_kind}|${rate.model_id || ""}`]?.id,
      };
      const r = await fetch("/api/admin/pricing/roll-forward", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error("status " + r.status);
      setMsg(`Rolled forward ✓ — new version id ${(await r.json()).new_version_id}`);
      load();
    } catch (e) {
      setMsg(`Failed: ${e.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  return html`
    <!-- Header row: title-side blurb + on-demand price-check button -->
    <div class="ax-pricing-head">
      <p class="db-form-help" style=${{ margin: 0, flex: 1 }}>
        Currently-in-force wholesale rates. Compare against the latest observed
        rate from the daily price-check; "Roll forward" closes the old version
        and writes a new one (audit-tracked, one button).
      </p>
      <div class="ax-pricing-check">
        <span class="ax-pricing-check-meta">
          Last checked <b>${fmtAgo(lastCheck)}</b>
          <span class="ax-pricing-check-cron"> · auto-runs 05:00 IST</span>
        </span>
        <button class="ax-pricing-check-btn" type="button"
                disabled=${checking}
                onClick=${checkRatesNow}
                title="Run the price-monitor scrape now (Gemini + Twilio + Plivo)">
          ${checking ? html`
            <span class="ax-pricing-spin" aria-hidden="true"></span>
            <span>Checking…</span>
          ` : html`
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M21 12a9 9 0 1 1-3.36-7" />
              <path d="M21 4v6h-6" />
            </svg>
            <span>Check rates now</span>
          `}
        </button>
      </div>
    </div>
    ${msg ? html`<div class="db-form-help" style=${{
      color: msg.startsWith("Failed") ? "#b91c1c" : "#166534",
      marginBottom: "10px",
    }}>${msg}</div>` : ""}
    <table class="db-table db-pricing-table">
      <thead>
        <tr>
          <th>Provider</th>
          <th>Rate</th>
          <th>Model</th>
          <th>Unit</th>
          <th>Effective</th>
          <th>Observed</th>
          <th>Drift</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${rates.map((r) => {
          const key = `${r.provider}|${r.rate_kind}|${r.model_id || ""}`;
          const obs = obsByKey[key];
          const drift = driftByKey[key];
          const effective = r.usd_per_unit ? `$${Number(r.usd_per_unit).toFixed(4)}` : `₹${Number(r.inr_per_unit).toFixed(2)}`;
          const observedDisp = !obs ? "—" :
            obs.usd != null ? `$${Number(obs.usd).toFixed(4)}` :
            obs.inr != null ? `₹${Number(obs.inr).toFixed(2)}` : "—";
          let driftPct = null;
          if (obs && (obs.usd != null) && Number(r.usd_per_unit) > 0) {
            driftPct = (obs.usd - Number(r.usd_per_unit)) / Number(r.usd_per_unit) * 100;
          } else if (obs && (obs.inr != null) && Number(r.inr_per_unit) > 0) {
            driftPct = (obs.inr - Number(r.inr_per_unit)) / Number(r.inr_per_unit) * 100;
          }
          const rowClass = !drift ? "" : (drift.severity === "critical" ? "row-critical" : "row-warning");
          return html`
            <tr key=${r.id} class=${rowClass}>
              <td><span class="db-pricing-provider db-pricing-provider-${r.provider}">${r.provider}</span></td>
              <td><code>${r.rate_kind}</code></td>
              <td>${r.model_id ? html`<code>${r.model_id}</code>` : "—"}</td>
              <td class="db-muted">${r.unit}</td>
              <td><b>${effective}</b></td>
              <td>${observedDisp}</td>
              <td>
                ${driftPct == null ? "—"
                  : html`<span class=${"db-pricing-drift" + (Math.abs(driftPct) > 1 ? " is-drifty" : "")}>${driftPct.toFixed(2)}%</span>`}
              </td>
              <td class="db-table-td-right">
                ${obs && drift ? html`
                  <button class="db-btn-primary db-btn-sm" type="button"
                          disabled=${busy}
                          onClick=${() => rollForward(r, obs)}>
                    Roll forward
                  </button>
                ` : ""}
              </td>
            </tr>
          `;
        })}
      </tbody>
    </table>
    <p class="db-form-help" style=${{ marginTop: "16px" }}>
      Roll-forward never re-prices historical calls. Old call rows keep
      their <code>cost_paise</code> frozen at the rate that was in force
      when the call landed.
    </p>
  `;
}

function AdminSummary() {
  const [s, setS] = useState(null);
  useEffect(() => { fetch("/api/admin/summary").then(r => r.json()).then(setS); }, []);
  if (!s) return html`<div class="db-loading">Loading…</div>`;
  const tiles = [
    { k: "users_count",         label: "Users" },
    { k: "orgs_count",          label: "Organisations" },
    { k: "agents_count",        label: "Agents" },
    { k: "published_count",     label: "Published",
      info: html`Agents whose owner has tapped <strong>Publish & Go-live</strong>. Free-plan agents can be tested in the browser but won't be marked published until the org upgrades.` },
    { k: "calls_count",         label: "Calls" },
    { k: "minutes_total",       label: "Minutes", fmt: (v) => Math.round(Number(v)) },
    { k: "input_tokens_total",  label: "Tokens in", fmt: (v) => Number(v).toLocaleString(),
      info: html`What the model <em>heard</em> across all sessions — caller speech + system prompt + tool results. Out is usually 30–40% of in.` },
    { k: "output_tokens_total", label: "Tokens out", fmt: (v) => Number(v).toLocaleString(),
      info: html`What the agent <em>said</em> — model-generated tokens. Pricing weights out 4× higher than in, so trimming the agent's verbosity (short sentences, 1–2 max) pays off here.` },
    { k: "cost_paise_total",    label: "Cost (₹)", fmt: (v) => (Number(v)/100).toFixed(2),
      info: html`Total Gemini Live spend across all calls + builder + post-call summaries in this period. Sourced from the <code>llm_calls</code> ledger; rebuilt daily from <code>org_daily_stats</code>.` },
  ];
  return html`
    <h1>Platform overview</h1>
    <div class="db-admin-grid">
      ${tiles.map((t) => html`
        <div class="db-admin-tile">
          <div class="db-admin-tile-label">
            ${t.label}
            ${t.info ? html`<${InfoDot} position="bottom">${t.info}</${InfoDot}>` : null}
          </div>
          <div class="db-admin-tile-value">${t.fmt ? t.fmt(s[t.k]) : s[t.k]}</div>
        </div>
      `)}
    </div>
  `;
}

function AdminOrgs() {
  const [rows, setRows] = useState(null);
  const [planFor, setPlanFor] = useState(null);
  useEffect(() => { fetch("/api/admin/orgs").then(r => r.json()).then(setRows); }, []);
  if (!rows) return html`<div class="db-loading">Loading…</div>`;
  const refresh = () => fetch("/api/admin/orgs").then(r => r.json()).then(setRows);
  return html`
    <h1>Organisations <span class="db-pill-soft">${rows.length}</span></h1>
    <table class="db-table">
      <thead>
        <tr><th>ID</th><th>Name</th><th>Country</th><th>Members</th><th>Agents</th><th>Minutes</th><th>Plan</th><th></th></tr>
      </thead>
      <tbody>
        ${rows.map((o) => html`
          <tr key=${o.id}>
            <td class="db-muted">${o.id}</td>
            <td>${o.name}</td>
            <td>${o.country || "—"}</td>
            <td>${o.members_count}</td>
            <td>${o.agents_count}</td>
            <td>${Math.round(Number(o.minutes_used || 0))}</td>
            <td><span class="db-role-tag db-role-${o.primary_plan || "free"}">${o.primary_plan || "—"}</span></td>
            <td><button class="db-btn-ghost" onClick=${() => setPlanFor(o)}>Set plan</button></td>
          </tr>
        `)}
      </tbody>
    </table>
    ${planFor ? html`<${PlanOverrideModal} org=${planFor} onClose=${() => setPlanFor(null)} onDone=${() => { setPlanFor(null); refresh(); }} />` : null}
  `;
}

function PlanOverrideModal({ org, onClose, onDone }) {
  const [plan, setPlan] = useState("free");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`/api/admin/orgs/${org.id}/plan`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        setErr(e.detail?.message || e.detail || `Failed (${r.status})`);
        return;
      }
      onDone();
    } finally { setBusy(false); }
  };
  return html`
    <div class="db-modal-backdrop" onClick=${onClose}>
      <div class="db-modal" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>Override plan</h2>
          <button class="db-modal-close" onClick=${onClose}>×</button>
        </header>
        <div class="db-modal-body">
          <p class="db-muted">Force-set the plan for every member of <strong>${org.name}</strong>. Bypasses Razorpay; audit-logged.</p>
          <label>Plan
            <select class="db-input" value=${plan} onChange=${(e) => setPlan(e.target.value)}>
              <option value="free">Free</option>
              <option value="starter">Starter</option>
              <option value="pro">Pro</option>
              <option value="business">Business</option>
            </select>
          </label>
          ${err ? html`<div class="db-error">${err}</div>` : null}
          <div class="db-modal-actions">
            <button class="db-btn-ghost" onClick=${onClose}>Cancel</button>
            <button class="db-btn-primary" disabled=${busy} onClick=${submit}>${busy ? "Applying…" : "Apply"}</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

function AdminUsers() {
  const [rows, setRows] = useState(null);
  const [q, setQ] = useState("");
  const load = (term = "") => fetch(`/api/admin/users?q=${encodeURIComponent(term)}`).then(r => r.json()).then(setRows);
  useEffect(() => { load(""); }, []);
  return html`
    <h1>Users</h1>
    <input
      class="db-input db-admin-search"
      placeholder="Search email or name…"
      value=${q}
      onInput=${(e) => { setQ(e.target.value); load(e.target.value.trim()); }}
    />
    ${!rows ? html`<div class="db-loading">Loading…</div>` : html`
      <table class="db-table">
        <thead><tr><th>ID</th><th>Email</th><th>Name</th><th>Plan</th><th>Org</th><th>Super-admin</th><th>Joined</th></tr></thead>
        <tbody>
          ${rows.map((u) => html`
            <tr key=${u.id}>
              <td class="db-muted">${u.id}</td>
              <td>${u.email}</td>
              <td>${u.name || "—"}</td>
              <td>${u.plan_label || "—"}</td>
              <td>${u.org_id || "—"}</td>
              <td>${u.is_super_admin ? html`<span class="db-role-tag db-role-owner">Super-admin</span>` : html`<span class="db-muted">—</span>`}</td>
              <td class="db-muted">${new Date(u.created_at).toLocaleDateString()}</td>
            </tr>
          `)}
        </tbody>
      </table>
    `}
  `;
}

function AdminCalls() {
  const [rows, setRows] = useState(null);
  // Build 202: full filter row — org, agent, phone, daterange.
  const fb = useAdminFilters(["org", "agent", "phone", "daterange"]);
  const lookups = useAdminLookups();
  useEffect(() => {
    setRows(null);
    const qs = fb.toQuery();
    qs.set("limit", "200");
    fetch(`/api/admin/calls?${qs}`)
      .then((r) => r.ok ? r.json() : [])
      .then(setRows)
      .catch(() => setRows([]));
  }, [fb.signature]);
  if (!rows) return html`
    <h1>Recent calls</h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <div class="db-loading">Loading…</div>
  `;
  if (rows.length === 0) return html`
    <h1>Recent calls</h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <p class="db-muted">No calls match these filters.</p>
  `;
  return html`
    <h1>Recent calls <span class="db-pill-soft">${rows.length}</span></h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <table class="db-table">
      <thead>
        <tr><th>When</th><th>Org</th><th>Agent</th><th>Duration</th><th>Outcome</th><th>Tokens (in/out)</th><th>Cost (₹)</th></tr>
      </thead>
      <tbody>
        ${rows.map((c) => html`
          <tr key=${c.id}>
            <td class="db-muted">${new Date(c.started_at).toLocaleString()}</td>
            <td>${c.org_name}</td>
            <td>${c.agent_name}</td>
            <td>${Math.round(c.duration_s)}s</td>
            <td>${c.outcome || "—"}</td>
            <td class="db-muted db-mono">${c.input_tokens || 0} / ${c.output_tokens || 0}</td>
            <td class="db-muted">${((c.cost_paise || 0)/100).toFixed(2)}</td>
          </tr>
        `)}
      </tbody>
    </table>
  `;
}

function AdminAudit() {
  const [rows, setRows] = useState(null);
  // Build 202: audit feed scoped by date range (no org/agent — audit
  // entries are actor-keyed, not target-keyed at the org level here).
  const fb = useAdminFilters(["daterange"]);
  const lookups = useAdminLookups();
  useEffect(() => {
    setRows(null);
    const qs = fb.toQuery();
    qs.set("limit", "200");
    fetch(`/api/admin/audit?${qs}`)
      .then((r) => r.ok ? r.json() : [])
      .then(setRows)
      .catch(() => setRows([]));
  }, [fb.signature]);
  if (!rows) return html`
    <h1>Audit log</h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <div class="db-loading">Loading…</div>
  `;
  if (rows.length === 0) return html`
    <h1>Audit log</h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <p class="db-muted">No admin actions in this window.</p>
  `;
  return html`
    <h1>Audit log <span class="db-pill-soft">${rows.length}</span></h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <table class="db-table">
      <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Diff</th><th>IP</th></tr></thead>
      <tbody>
        ${rows.map((a) => html`
          <tr key=${a.id}>
            <td class="db-muted">${new Date(a.created_at).toLocaleString()}</td>
            <td>${a.actor_email}</td>
            <td><code>${a.action}</code></td>
            <td class="db-muted db-mono">${a.target_kind ? `${a.target_kind}:${a.target_id || ""}` : "—"}</td>
            <td class="db-muted db-mono" style="max-width:380px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
              ${a.diff ? JSON.stringify(a.diff) : "—"}
            </td>
            <td class="db-muted">${a.ip || "—"}</td>
          </tr>
        `)}
      </tbody>
    </table>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// Sparkline — inline SVG, no chart lib. Renders a values[] array as a
// stroked line within the given width × height. We pad missing days
// with zero so the line always spans the full window — that way the
// admin grid's last-7-day sparkline doesn't shrink horizontally when an
// agent had no calls on Tuesday. Single-purpose, intentionally tiny.
// ─────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
// InfoDot — small "(i)" affordance for dashboard columns/tiles. Click
// reveals a 1-2 sentence popover. Single-purpose: explains a column
// header or metric that's been earned its place in the UI but whose
// meaning isn't self-evident (lead_quality, sentiment, tokens in/out,
// cost-per-minute, plan-gated features).
//
// Doctrine note: the build orb deliberately has NO tooltips (see
// northstar Part I). This component is for the dashboard surface that
// grew around the orb — different product, different rules. Audit F.2.
// ─────────────────────────────────────────────────────────────────────────
function InfoDot({ children, position = "bottom" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  // Outside click + Escape close.
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return html`
    <span class="db-infodot-wrap" ref=${ref}>
      <button
        type="button"
        class=${"db-infodot" + (open ? " is-open" : "")}
        aria-label="More info"
        onClick=${(e) => { e.stopPropagation(); setOpen((o) => !o); }}
      >i</button>
      ${open ? html`
        <div class=${"db-infodot-pop db-infodot-pop-" + position}>${children}</div>
      ` : null}
    </span>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// DestructiveConfirmModal — replaces the four native browser confirm()
// dialogs. Names the thing being destroyed, explains the consequence in
// one short line, and offers cancel/confirm. For hard deletes (Delete
// agent), pass `typedName` — the operator must type it before confirm
// enables. GitHub's "type the repo name" pattern; doctrine permits this
// because it's about preventing tragedy, not "are you sure" theatre.
// Audit F.3.
// ─────────────────────────────────────────────────────────────────────────
function DestructiveConfirmModal({
  title, body, confirmLabel = "Delete", typedName = null,
  onClose, onConfirm,
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const ready = typedName == null || typed.trim() === typedName;
  const submit = async () => {
    if (!ready || busy) return;
    setBusy(true); setErr(null);
    try {
      await onConfirm();
    } catch (e) {
      setErr(String(e?.message || e) || "Didn't go through — try again.");
    } finally {
      setBusy(false);
    }
  };
  return html`
    <div class="db-modal-backdrop" onClick=${onClose}>
      <div class="db-modal db-modal-destructive" onClick=${(e) => e.stopPropagation()}>
        <header class="db-modal-head">
          <h2>${title}</h2>
          <button class="db-modal-close" onClick=${onClose}>×</button>
        </header>
        <div class="db-modal-body">
          <p class="db-destructive-body">${body}</p>
          ${typedName ? html`
            <label class="db-destructive-confirm-label">
              <span>Type <strong>${typedName}</strong> to confirm:</span>
              <input
                class="db-input"
                value=${typed}
                onInput=${(e) => setTyped(e.target.value)}
                placeholder=${typedName}
                autoFocus
              />
            </label>
          ` : null}
          ${err ? html`<div class="db-error">${err}</div>` : null}
          <div class="db-modal-actions">
            <button class="db-btn-ghost" onClick=${onClose}>Cancel</button>
            <button
              class="db-btn-destructive"
              disabled=${!ready || busy}
              onClick=${submit}
            >${busy ? "Working…" : confirmLabel}</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

function Sparkline({ values, width = 120, height = 32, stroke = "#a78bfa", fill = "rgba(167,139,250,0.12)" }) {
  if (!values || values.length === 0) {
    return html`<svg width=${width} height=${height} class="db-sparkline" aria-hidden="true"></svg>`;
  }
  const max = Math.max(1, ...values);
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = height - (v / max) * (height - 4) - 2;
    return [x, y];
  });
  const path = points.map(([x, y], i) => (i === 0 ? `M${x},${y}` : `L${x},${y}`)).join(" ");
  const fillPath = `${path} L${width},${height} L0,${height} Z`;
  return html`
    <svg width=${width} height=${height} class="db-sparkline" aria-hidden="true">
      <path d=${fillPath} fill=${fill} />
      <path d=${path} stroke=${stroke} stroke-width="1.5" fill="none" stroke-linecap="round" />
    </svg>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AdminAnalytics — /admin/analytics. Platform-wide time series + per-org
// ranking. Reuses the totals tile pattern from the summary tab, with a
// big sparkline on top for the calls-per-day curve.
// Range selector defaults to 30d; max 365 (clamped server-side).
// ─────────────────────────────────────────────────────────────────────────
function AdminAnalytics() {
  const [data, setData] = useState(null);
  const [days, setDays] = useState(30);
  const load = (d) => fetch(`/api/admin/analytics?days=${d}`).then(r => r.json()).then(setData);
  useEffect(() => { load(days); }, [days]);
  if (!data) return html`<div class="db-loading">Loading…</div>`;
  const t = data.totals || {};
  const calls = (data.series || []).map((d) => Number(d.calls || 0));
  const cost = (data.series || []).map((d) => Number(d.cost_paise || 0));
  return html`
    <h1>
      Platform analytics
      <span class="db-pill-soft">${days}d</span>
      <span class="db-admin-range-spacer"></span>
      ${[7, 30, 90].map((d) => html`
        <button
          class=${"db-admin-range-btn" + (days === d ? " is-active" : "")}
          onClick=${() => setDays(d)}
        >${d}d</button>
      `)}
    </h1>
    <div class="db-admin-grid">
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Calls</div>
        <div class="db-admin-tile-value">${Number(t.calls || 0).toLocaleString()}</div>
        <div class="db-admin-tile-spark"><${Sparkline} values=${calls} width=${180} height=${36} /></div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Minutes</div>
        <div class="db-admin-tile-value">${Math.round(Number(t.minutes || 0)).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Tokens in</div>
        <div class="db-admin-tile-value">${Number(t.input_tokens || 0).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Tokens out</div>
        <div class="db-admin-tile-value">${Number(t.output_tokens || 0).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Cost (₹)</div>
        <div class="db-admin-tile-value">${(Number(t.cost_paise || 0)/100).toFixed(2)}</div>
        <div class="db-admin-tile-spark"><${Sparkline} values=${cost} width=${180} height=${36} stroke="#2563eb" fill="rgba(37,99,235,0.10)" /></div>
      </div>
    </div>

    <section class="db-card db-admin-by-org">
      <header class="db-card-head"><h2>By organisation</h2></header>
      <table class="db-table">
        <thead><tr><th>Org</th><th>Calls</th><th>Minutes</th><th>Cost (₹)</th></tr></thead>
        <tbody>
          ${(data.by_org || []).map((o) => html`
            <tr key=${o.id}>
              <td>${o.name}</td>
              <td>${o.calls}</td>
              <td>${Math.round(Number(o.minutes || 0))}</td>
              <td class="db-muted">${(Number(o.cost_paise || 0)/100).toFixed(2)}</td>
            </tr>
          `)}
        </tbody>
      </table>
    </section>
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// AdminLlmLedger — /admin/llm. Phase 7 universal LLM-cost view.
// Reads /api/admin/analytics/llm which sums llm_calls across every kind
// (builder, agent, tts). The customer-call analytics tab focuses on
// outcomes + minutes; this tab focuses on token cost, including the
// Eva-builder time that doesn't show up there.
// ─────────────────────────────────────────────────────────────────────────
function AdminLlmLedger() {
  const [data, setData] = useState(null);
  // Build 202: org + agent + daterange. Days come from the bar's
  // value (preset → days; custom → 30 fallback).
  const fb = useAdminFilters(["org", "agent", "daterange"]);
  const lookups = useAdminLookups();
  const days = fb.value.days || 30;
  useEffect(() => {
    setData(null);
    const qs = fb.toQuery();
    qs.set("days", String(days));
    fetch(`/api/admin/analytics/llm?${qs}`)
      .then((r) => r.ok ? r.json() : null)
      .then(setData)
      .catch(() => setData(null));
  }, [fb.signature]);
  if (!data) return html`
    <h1>LLM ledger</h1>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <div class="db-loading">Loading…</div>
  `;
  const t = data.totals || {};
  const cpm = t.cost_per_minute_paise == null ? null : Number(t.cost_per_minute_paise) / 100;
  return html`
    <h1>
      LLM ledger
      <span class="db-pill-soft">${days}d</span>
    </h1>
    <p class="db-muted db-admin-settings-blurb">
      Universal ledger of every LLM session — including Eva-builder
      conversations and TTS previews. Customer calls are the analytics
      tab; this is the full token+cost picture.
    </p>
    <${AdminFilterBar} state=${fb} orgs=${lookups.orgs} agents=${lookups.agents} />
    <div class="db-admin-grid">
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Sessions</div>
        <div class="db-admin-tile-value">${Number(t.sessions || 0).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Minutes</div>
        <div class="db-admin-tile-value">${Math.round(Number(t.minutes || 0)).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Cost (₹)</div>
        <div class="db-admin-tile-value">${(Number(t.cost_paise || 0)/100).toFixed(2)}</div>
      </div>
      <div class="db-admin-tile db-admin-tile-cpm">
        <div class="db-admin-tile-label">
          Cost per minute
          <${InfoDot} position="bottom">
            <strong>Weighted</strong> — sum of cost ÷ sum of minutes across the window. Not the average of per-call <code>cost_per_minute</code>, which would weight a 5-second test the same as a 90-second customer call.
          </${InfoDot}>
        </div>
        <div class="db-admin-tile-value">
          ${cpm == null ? "—" : html`₹${cpm.toFixed(4)}`}
        </div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Tokens in</div>
        <div class="db-admin-tile-value">${Number(t.input_tokens || 0).toLocaleString()}</div>
      </div>
      <div class="db-admin-tile">
        <div class="db-admin-tile-label">Tokens out</div>
        <div class="db-admin-tile-value">${Number(t.output_tokens || 0).toLocaleString()}</div>
      </div>
    </div>

    <section class="db-card db-admin-by-org">
      <header class="db-card-head"><h2>By kind</h2></header>
      ${(data.by_kind || []).length === 0 ? html`
        <p class="db-muted db-pad">No sessions in the last ${days} days.</p>
      ` : html`
        <table class="db-table">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Sessions</th>
              <th>Minutes</th>
              <th>Tokens (in / out)</th>
              <th>Cost (₹)</th>
              <th>Cost / min (₹)</th>
            </tr>
          </thead>
          <tbody>
            ${data.by_kind.map((k) => {
              const kcpm = k.cost_per_minute_paise == null ? null : Number(k.cost_per_minute_paise) / 100;
              return html`
                <tr key=${k.kind}>
                  <td><span class=${"db-role-tag db-llm-kind-" + k.kind}>${k.kind}</span></td>
                  <td>${k.sessions}</td>
                  <td>${Math.round(Number(k.minutes || 0))}</td>
                  <td class="db-muted db-mono">
                    ${Number(k.input_tokens || 0).toLocaleString()}
                    / ${Number(k.output_tokens || 0).toLocaleString()}
                  </td>
                  <td>${(Number(k.cost_paise || 0)/100).toFixed(2)}</td>
                  <td class="db-muted">${kcpm == null ? "—" : kcpm.toFixed(4)}</td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      `}
    </section>
  `;
}

function AdminSettings() {
  // Read-through cache lives server-side; this component reloads on every
  // mount and after every save so the panel always reflects the truth.
  const [rows, setRows] = useState(null);
  const [editing, setEditing] = useState(null);   // { key, json: string }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => fetch("/api/admin/settings").then(r => r.json()).then(setRows);
  useEffect(() => { load(); }, []);

  // Save handler — accepts the raw textarea string, parses it as JSONB-
  // shaped JSON so 'true', '42', '"hello"', '{...}' all flow through
  // unchanged. PATCH expects {value: <parsed>}.
  const save = async () => {
    if (!editing) return;
    setBusy(true); setErr(null);
    let parsed;
    try { parsed = JSON.parse(editing.json); }
    catch (e) { setErr(`Not valid JSON: ${e.message}`); setBusy(false); return; }
    const r = await fetch(`/api/admin/settings/${encodeURIComponent(editing.key)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: parsed }),
    });
    setBusy(false);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      setErr(e.detail?.message || e.detail || `Failed (${r.status})`);
      return;
    }
    setEditing(null);
    await load();
  };

  if (!rows) return html`<div class="db-loading">Loading…</div>`;

  // Group rows by category so the UI mirrors the table's organisation.
  const groups = {};
  for (const r of rows) {
    (groups[r.category] ||= []).push(r);
  }
  const groupOrder = ["models", "limits", "features", "branding"];
  const sortedKeys = [...new Set([...groupOrder, ...Object.keys(groups)])].filter((k) => groups[k]);

  return html`
    <h1>Platform settings <span class="db-pill-soft">${rows.length}</span></h1>
    <p class="db-muted db-admin-settings-blurb">
      Edit any value below to retune the platform without a deploy. JSON-shaped
      values: <code>"strings"</code>, <code>42</code>, <code>true</code>,
      <code>["arrays"]</code>, <code>{"objects": true}</code>. Every change
      writes to the audit log.
    </p>

    ${sortedKeys.map((cat) => html`
      <section class="db-card db-admin-settings-group" key=${cat}>
        <header class="db-card-head"><h2>${cat}</h2></header>
        <table class="db-table">
          <thead>
            <tr><th>Setting</th><th>Value</th><th>Description</th><th></th></tr>
          </thead>
          <tbody>
            ${groups[cat].map((s) => html`
              <tr key=${s.key}>
                <td>
                  <div class="db-member-name">${s.label}</div>
                  <div class="db-muted db-mono db-admin-setting-key">${s.key}</div>
                </td>
                <td class="db-mono db-admin-setting-value">${JSON.stringify(s.value)}</td>
                <td class="db-muted">${s.description || "—"}</td>
                <td class="db-row-actions">
                  <button class="db-btn-ghost" onClick=${() => setEditing({ key: s.key, json: JSON.stringify(s.value, null, 2) })}>
                    Edit
                  </button>
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `)}

    ${editing ? html`
      <div class="db-modal-backdrop" onClick=${() => setEditing(null)}>
        <div class="db-modal" onClick=${(e) => e.stopPropagation()}>
          <header class="db-modal-head">
            <h2>Edit setting</h2>
            <button class="db-modal-close" onClick=${() => setEditing(null)}>×</button>
          </header>
          <div class="db-modal-body">
            <p class="db-muted db-mono">${editing.key}</p>
            <label>
              JSON value
              <textarea
                class="db-input db-admin-json-edit"
                rows="6"
                value=${editing.json}
                onInput=${(e) => setEditing({ ...editing, json: e.target.value })}
              />
            </label>
            ${err ? html`<div class="db-error">${err}</div>` : null}
            <div class="db-modal-actions">
              <button class="db-btn-ghost" onClick=${() => setEditing(null)}>Cancel</button>
              <button class="db-btn-primary" disabled=${busy} onClick=${save}>
                ${busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      </div>
    ` : null}
  `;
}

function AdminSuperAdmins({ currentUser }) {
  const [rows, setRows] = useState(null);
  const [grantEmail, setGrantEmail] = useState("");
  const [err, setErr] = useState(null);
  const load = () => fetch("/api/admin/super-admins").then(r => r.json()).then(setRows);
  useEffect(() => { load(); }, []);
  const grant = async () => {
    setErr(null);
    const email = grantEmail.trim();
    if (!email) return;
    const search = await fetch(`/api/admin/users?q=${encodeURIComponent(email)}`).then(r => r.json());
    const target = search.find((u) => u.email.toLowerCase() === email.toLowerCase());
    if (!target) { setErr(`No user with email ${email}.`); return; }
    const r = await fetch("/api/admin/super-admins", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: target.id }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      setErr(e.detail?.message || e.detail || `Failed (${r.status})`);
      return;
    }
    setGrantEmail("");
    await load();
  };
  const [confirmAction, setConfirmAction] = useState(null);
  const revoke = (userId, email) => {
    setConfirmAction({
      title: "Revoke super-admin?",
      body: html`<strong>${email}</strong> will lose platform-admin access on their next request. They keep their normal account and any orgs they're a member of.`,
      confirmLabel: "Revoke super-admin",
      onConfirm: async () => {
        const r = await fetch(`/api/admin/super-admins/${userId}`, { method: "DELETE" });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          throw new Error(e.detail?.message || e.detail || `Failed (${r.status})`);
        }
        setConfirmAction(null);
        await load();
      },
    });
  };
  if (!rows) return html`<div class="db-loading">Loading…</div>`;
  // htm is quirky about inline `style="…"` containing semicolons; using a
  // CSS class for spacing (db-admin-grant-card) avoids the parser tripping
  // and silently swallowing the whole tab render.
  return html`
    <h1>Super-admins <span class="db-pill-soft">${rows.length}</span></h1>
    <table class="db-table">
      <thead><tr><th>Email</th><th>Name</th><th>Granted by</th><th>When</th><th></th></tr></thead>
      <tbody>
        ${rows.map((s) => html`
          <tr key=${s.user_id}>
            <td>${s.email}</td>
            <td>${s.name || "—"}</td>
            <td class="db-muted">${s.granted_by_email || "system"}</td>
            <td class="db-muted">${new Date(s.granted_at).toLocaleString()}</td>
            <td class="db-row-actions">
              ${rows.length > 1
                ? html`<button class="db-btn-ghost-danger" onClick=${() => revoke(s.user_id, s.email)}>Revoke</button>`
                : html`<span class="db-muted db-mono">last admin</span>`}
            </td>
          </tr>
        `)}
      </tbody>
    </table>
    <section class="db-card db-admin-grant-card">
      <header class="db-card-head"><h2>Grant super-admin</h2></header>
      <div class="db-pad">
        <p class="db-muted">The user must already exist (have signed up).</p>
        <div class="db-admin-grant-row">
          <input
            class="db-input"
            type="email"
            placeholder="email@spiderx.ai"
            value=${grantEmail}
            onInput=${(e) => setGrantEmail(e.target.value)}
          />
          <button class="db-btn-primary" onClick=${grant} disabled=${!grantEmail.trim()}>Grant</button>
        </div>
        ${err ? html`<div class="db-error">${err}</div>` : null}
      </div>
    </section>
    ${confirmAction ? html`
      <${DestructiveConfirmModal}
        title=${confirmAction.title}
        body=${confirmAction.body}
        confirmLabel=${confirmAction.confirmLabel}
        onClose=${() => setConfirmAction(null)}
        onConfirm=${confirmAction.onConfirm}
      />
    ` : null}
  `;
}

// ─────────────────────────────────────────────────────────────────────────
// BillingPage — /account/billing. Shows the user's current plan + the
// upgrade tier ladder. Click an upgrade tier → POST /api/razorpay/order →
// Razorpay Checkout (real, if RAZORPAY_KEY_ID env is set) or demo confirm
// (if not). On success, POST /api/me/upgrade to flip the user's plan.
// ─────────────────────────────────────────────────────────────────────────
// IntegrationsPage — /account/integrations. Lists the connectors we support
// today + a "request another" form. Live wiring (OAuth, webhook provisioning)
// is per-connector work; this page makes the catalogue visible so the user
// knows what's in flight + what's coming.
function IntegrationsPage({ agents, plan, onNav, org, presets }) {
  // Catalogue is intentionally honest:
  //   live    — fully wired today (Twilio for phone numbers, Custom webhook
  //             for the call-ended POST). One-click reachable per agent.
  //   request — we can stand it up on demand via the webhook + a thin glue
  //             layer; ops takes the request, customer fast-tracks.
  //   roadmap — scheduled but not built yet.
  //
  // Each connector also carries `countries` (ISO codes the connector is
  // relevant for, or ["GLOBAL"]) and `industries` (sector ids it pairs with,
  // empty if cross-industry). The country filter at the top defaults to the
  // org's country so a restaurant in India sees Chope + Razorpay first, not
  // OpenTable + Stripe — and vice versa.
  const CONNECTORS = [
    // Live — globally relevant per-agent wiring
    { id: "twilio",  name: "Twilio",         desc: "Phone-number provisioning, inbound + outbound routing. The default carrier behind every Go-live number.", bucket: "live", countries: ["GLOBAL"], industries: [] },
    { id: "webhook", name: "Custom webhook", desc: "JSON POST to your own endpoint at the end of every call — outcome, summary, extracted fields.",            bucket: "live", countries: ["GLOBAL"], industries: [] },

    // Cross-industry CRM + scheduling — on request, globally relevant
    { id: "hubspot",         name: "HubSpot CRM",     desc: "Create / update contacts + log every call as an activity.",            bucket: "request", countries: ["GLOBAL"], industries: [] },
    { id: "salesforce",      name: "Salesforce",      desc: "Lead create + opportunity update from call outcomes.",                  bucket: "request", countries: ["GLOBAL"], industries: [] },
    { id: "zoho",            name: "Zoho CRM",        desc: "Lead + ticket creation, Indian-market favourite.",                       bucket: "request", countries: ["IN", "GLOBAL"], industries: [] },
    { id: "google_calendar", name: "Google Calendar", desc: "Book and reschedule directly into the user's primary calendar.",        bucket: "request", countries: ["GLOBAL"], industries: [] },
    { id: "calendly",        name: "Calendly",        desc: "Hand callers a Calendly slot picker over SMS.",                          bucket: "request", countries: ["GLOBAL"], industries: [] },
    { id: "sheets",          name: "Google Sheets",   desc: "Stream call summaries into a row-per-call spreadsheet.",                 bucket: "request", countries: ["GLOBAL"], industries: [] },
    { id: "slack",           name: "Slack",           desc: "Real-time call alerts in any channel — escalations + bookings.",          bucket: "request", countries: ["GLOBAL"], industries: [] },

    // Restaurant-side
    { id: "chope",     name: "Chope",      desc: "SEA-favourite table reservations — direct slot grab from the call.",                bucket: "request", countries: ["SG", "OTHER"], industries: ["restaurant"] },
    { id: "opentable", name: "OpenTable",  desc: "Table reservations for US / UK / AU restaurants.",                                    bucket: "request", countries: ["US", "GB", "AU"], industries: ["restaurant"] },
    { id: "resy",      name: "Resy",       desc: "Reservation management for higher-end restaurants in the US.",                       bucket: "request", countries: ["US"], industries: ["restaurant"] },
    { id: "tock",      name: "Tock",       desc: "Tasting menus + experience bookings.",                                                bucket: "request", countries: ["US", "GLOBAL"], industries: ["restaurant"] },
    { id: "toast",     name: "Toast POS",  desc: "Sync menu, hours and out-of-stock into ${agent} so she doesn't promise the unavailable.", bucket: "request", countries: ["US"], industries: ["restaurant"] },
    { id: "zomato",    name: "Zomato",     desc: "Reservation + listing sync for India / SEA.",                                          bucket: "roadmap", countries: ["IN", "OTHER"], industries: ["restaurant"] },
    { id: "swiggy",    name: "Swiggy / Dineout", desc: "Indian table-booking + offers feed from Dineout, deferred to agent during calls.", bucket: "roadmap", countries: ["IN"], industries: ["restaurant"] },

    // Salon / spa
    { id: "vagaro",   name: "Vagaro",            desc: "Stylist calendars + service catalog for salons / spas.",                          bucket: "request", countries: ["US", "GB", "CA", "AU"], industries: ["salon"] },
    { id: "mindbody", name: "Mindbody",          desc: "Class + appointment booking for fitness / wellness.",                              bucket: "request", countries: ["US", "GLOBAL"], industries: ["salon"] },
    { id: "fresha",   name: "Fresha",            desc: "Free booking platform popular in EU + India.",                                     bucket: "request", countries: ["IN", "GB", "GLOBAL"], industries: ["salon"] },
    { id: "square_appts", name: "Square Appointments", desc: "Appointment management + payments for small salons.",                        bucket: "request", countries: ["US", "GB", "CA", "AU"], industries: ["salon"] },

    // Dental / healthcare
    { id: "nexhealth", name: "NexHealth",   desc: "Patient scheduling + reminders for US dental & medical practices.",                    bucket: "request", countries: ["US"], industries: ["dental", "healthcare"] },
    { id: "dentrix",   name: "Dentrix",     desc: "Schedule + chart write-back for US dental practices.",                                  bucket: "request", countries: ["US"], industries: ["dental"] },
    { id: "practo",    name: "Practo",      desc: "Practice-management + patient acquisition stack used across India.",                    bucket: "request", countries: ["IN"], industries: ["dental", "healthcare"] },

    // Real estate
    { id: "magicbricks", name: "MagicBricks", desc: "Lead-routing + listing sync for Indian real-estate brokerages.",                     bucket: "request", countries: ["IN"], industries: ["real_estate"] },
    { id: "99acres",     name: "99acres",     desc: "India's other big real-estate listing portal.",                                       bucket: "request", countries: ["IN"], industries: ["real_estate"] },
    { id: "nobroker",    name: "NoBroker",    desc: "Rental-heavy marketplace, lead lookup + transfer.",                                   bucket: "request", countries: ["IN"], industries: ["real_estate"] },
    { id: "bold_trail",  name: "BoldTrail",   desc: "US real-estate CRM (formerly KvCORE).",                                                bucket: "request", countries: ["US"], industries: ["real_estate"] },
    { id: "follow_up_boss", name: "Follow Up Boss", desc: "Lead nurturing for US realtors.",                                                bucket: "request", countries: ["US"], industries: ["real_estate"] },

    // Hotel / travel
    { id: "cloudbeds", name: "Cloudbeds",     desc: "Property management + reservation sync for independent hotels.",                     bucket: "request", countries: ["GLOBAL"], industries: ["travel"] },
    { id: "booking",   name: "Booking.com",   desc: "Channel manager + availability sync.",                                                bucket: "request", countries: ["GLOBAL"], industries: ["travel"] },
    { id: "mmt",       name: "MakeMyTrip / Goibibo", desc: "Indian OTA channel-manager hookup.",                                            bucket: "roadmap", countries: ["IN"], industries: ["travel"] },
    { id: "oyo",       name: "OYO",           desc: "Inventory + lead sync for OYO-affiliated rooms.",                                      bucket: "roadmap", countries: ["IN"], industries: ["travel"] },

    // Automotive
    { id: "shopmonkey",  name: "Shopmonkey",   desc: "Shop management + scheduling for US repair shops.",                                  bucket: "request", countries: ["US"], industries: ["automotive"] },
    { id: "gomechanic",  name: "GoMechanic",   desc: "Service-network booking for India.",                                                  bucket: "roadmap", countries: ["IN"], industries: ["automotive"] },

    // Retail / e-com
    { id: "shopify",     name: "Shopify",      desc: "Order lookup + status updates over the phone.",                                       bucket: "request", countries: ["GLOBAL"], industries: ["retail"] },
    { id: "magento",     name: "Magento",      desc: "Order lookup for enterprise retail stacks.",                                          bucket: "request", countries: ["GLOBAL"], industries: ["retail"] },

    // Legal / accounting
    { id: "clio",        name: "Clio",         desc: "Intake + matter-management for US / Commonwealth law firms.",                        bucket: "request", countries: ["US", "GB", "CA", "AU"], industries: ["legal"] },

    // Cleaning / home services (sector not in presets yet — surface anyway under "Other")
    { id: "servicetitan", name: "ServiceTitan", desc: "Dispatch + job-management for US home-service providers.",                          bucket: "request", countries: ["US"], industries: ["logistics"] },
    { id: "jobber",       name: "Jobber",       desc: "Lighter-weight scheduling + invoicing for small service businesses.",                bucket: "request", countries: ["US", "CA", "GB", "AU"], industries: ["logistics"] },
    { id: "housecall_pro", name: "Housecall Pro", desc: "Bookings + dispatch for cleaning / HVAC / plumbing.",                              bucket: "request", countries: ["US", "CA"], industries: ["logistics"] },

    // Payments
    { id: "razorpay",  name: "Razorpay payment links", desc: "Generate + SMS pay-on-call links to Indian customers.", bucket: "roadmap", countries: ["IN"], industries: [] },
    { id: "stripe",    name: "Stripe",                  desc: "Card payments + checkout sessions, US / EU / SG.",      bucket: "roadmap", countries: ["US", "GB", "DE", "FR", "SG", "AU", "GLOBAL"], industries: [] },
    { id: "whatsapp",  name: "WhatsApp Business",       desc: "Send call recap + payment link via WhatsApp.",          bucket: "roadmap", countries: ["GLOBAL"], industries: [] },
  ];
  const STATUS_LABEL = { live: "Live", request: "On request", roadmap: "Roadmap" };
  const STATUS_CLASS = { live: "db-tag-green", request: "db-tag-yellow", roadmap: "db-tag-grey" };
  const ACTION_LABEL = { live: "Configure per agent →", request: "Request →", roadmap: "On the roadmap" };

  // Country filter — defaults to the org's country (if set), else "ALL".
  // GLOBAL connectors are always included; everything else is filtered by
  // country tag intersection.
  const [country, setCountry] = useState(() => org?.country || "ALL");
  useEffect(() => {
    if (org?.country && country === "ALL") setCountry(org.country);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org?.country]);

  // Industry filter — derived from agents present in the workspace. Adds
  // "All industries" + each sector with at least one agent.
  const agentSectors = Array.from(new Set((agents || []).map((a) => a.sector).filter(Boolean)));
  const [industry, setIndustry] = useState("ALL");

  const matchesCountry = (c) => country === "ALL"
    || c.countries.includes("GLOBAL")
    || c.countries.includes(country);
  const matchesIndustry = (c) => industry === "ALL"
    || c.industries.length === 0
    || c.industries.includes(industry);
  const filtered = CONNECTORS.filter((c) => matchesCountry(c) && matchesIndustry(c));

  const [request, setRequest] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const requestRef = useRef(null);
  const submitRequest = (e) => {
    e?.preventDefault();
    if (!request.trim()) return;
    // No backend yet — store locally + show a confirmation. Ops will reach
    // out to power users via support; the form is the visible promise.
    try { localStorage.setItem(`sxai.integration_request.${Date.now()}`, request.trim()); } catch {}
    setSubmitted(true);
    setRequest("");
    setTimeout(() => setSubmitted(false), 3500);
  };

  const handleAction = (c) => {
    if (c.bucket === "live") {
      // Both live connectors are per-agent: Twilio is set up in the Go-live
      // flow, Webhook in the agent's Developer page. We send the user to the
      // agent list so they can pick one — they'll continue from there.
      onNav && onNav("/agents");
      return;
    }
    if (c.bucket === "request") {
      setRequest(c.name);
      // Defer to next tick so the controlled input has the new value before
      // we scroll, and the focus highlight reads as a clear handoff.
      setTimeout(() => {
        requestRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
        requestRef.current?.querySelector("input")?.focus();
      }, 30);
    }
    // roadmap → no-op (button is disabled anyway)
  };

  const countryLabel = (id) => COUNTRIES.find((x) => x.id === id)?.label || id;
  const sectorLabel = (id) => (presets?.sectors || []).find((x) => x.id === id)?.label || id;

  const renderGroup = (label, sub, bucket) => {
    const items = filtered.filter((c) => c.bucket === bucket);
    if (items.length === 0) return "";
    return html`
      <section class="db-panel" key=${bucket}>
        <h3 class="db-panel-title">${label} <span class="db-panel-pill">${items.length}</span></h3>
        <p class="db-panel-sub">${sub}</p>
        <div class="db-integrations-grid">
          ${items.map((c) => html`
            <div class="db-integration-card" key=${c.id}>
              <div class="db-integration-head">
                <div class="db-integration-name">${c.name}</div>
                <span class=${"db-tag " + STATUS_CLASS[c.bucket]}>${STATUS_LABEL[c.bucket]}</span>
              </div>
              <div class="db-integration-desc">${c.desc}</div>
              <div class="db-integration-tags">
                ${c.industries.length > 0 ? c.industries.map((i) => html`<span class="db-tag db-tag-purple" key=${i}>${sectorLabel(i)}</span>`) : ""}
                ${!c.countries.includes("GLOBAL") ? c.countries.filter((cc) => cc !== "GLOBAL").map((cc) => html`<span class="db-tag db-tag-blue db-tag-flag" key=${cc}><span class="db-flag" aria-hidden="true">${flagFor(cc)}</span><span>${countryLabel(cc)}</span></span>`) : html`<span class="db-tag db-tag-grey db-tag-flag"><span class="db-flag" aria-hidden="true">🌐</span><span>Global</span></span>`}
              </div>
              <button class="db-btn-ghost db-btn-sm" type="button"
                      disabled=${c.bucket === "roadmap"}
                      onClick=${() => handleAction(c)}>
                ${ACTION_LABEL[c.bucket]}
              </button>
            </div>
          `)}
        </div>
      </section>
    `;
  };

  const body = html`
    <div class="db-overview">
      <!-- Filters — country defaults to the org's, industry to "All". Cards
           that are GLOBAL or industry-agnostic always show through. -->
      <section class="db-filters">
        <div class="db-filter-group">
          <span class="db-filter-label">Country</span>
          <div class="db-filter-pills">
            <button class=${"db-filter-pill" + (country === "ALL" ? " active" : "")} type="button" onClick=${() => setCountry("ALL")}>
              <span class="db-flag" aria-hidden="true">🌐</span><span>All</span>
            </button>
            ${COUNTRIES.filter((c) => c.id !== "OTHER").slice(0, 8).map((c) => html`
              <button key=${c.id} class=${"db-filter-pill" + (country === c.id ? " active" : "")} type="button" onClick=${() => setCountry(c.id)}>
                <span class="db-flag" aria-hidden="true">${c.flag}</span><span>${c.label}</span>
              </button>
            `)}
          </div>
        </div>
        ${agentSectors.length > 0 ? html`
          <div class="db-filter-group">
            <span class="db-filter-label">Industry</span>
            <div class="db-filter-pills">
              <button class=${"db-filter-pill" + (industry === "ALL" ? " active" : "")} type="button" onClick=${() => setIndustry("ALL")}>All</button>
              ${agentSectors.map((s) => html`
                <button key=${s} class=${"db-filter-pill" + (industry === s ? " active" : "")} type="button" onClick=${() => setIndustry(s)}>${sectorLabel(s)}</button>
              `)}
            </div>
          </div>
        ` : ""}
      </section>

      ${renderGroup(
        "Active integrations",
        "Wired into the platform today. Set up the specifics per agent on its Go-live or Developer page.",
        "live",
      )}
      ${renderGroup(
        "Available on request",
        "We can stand any of these up through the webhook + a thin glue layer. Tell us which one and we'll fast-track it for your account.",
        "request",
      )}
      ${renderGroup(
        "Roadmap",
        "Scheduled but not built yet — watching customer demand decide the order.",
        "roadmap",
      )}

      <section class="db-panel" ref=${requestRef}>
        <h3 class="db-panel-title">Need something else?</h3>
        <p class="db-panel-sub">Tell us which tool, and we'll fast-track it. (Active customers' requests jump the queue.)</p>
        <form class="db-form" onSubmit=${submitRequest}>
          <label class="db-form-field">
            <span class="db-form-label">Tool or service you need</span>
            <input class="db-input" type="text" placeholder="e.g. Pipedrive, Notion, Freshdesk, Telegram…"
                   value=${request} onInput=${(e) => setRequest(e.target.value)} />
          </label>
          <div class="db-actions-row">
            <button type="submit" class="db-btn-primary" disabled=${!request.trim()}>Request integration</button>
            ${submitted ? html`<span class="db-save-pill db-save-ok">Got it — we'll be in touch.</span>` : ""}
          </div>
        </form>
      </section>
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="integrations"
      agents=${agents}
      plan=${plan}
      title="Integrations"
      subtitle="Connect SpiderX.AI agents to the tools you already use."
      onNav=${onNav}
      body=${body}
    />
  `;
}

function BillingPage({ agents, plan, onNav, onPlanChanged }) {
  const [plans, setPlans] = useState([]);
  const [me, setMe] = useState(null);   // plan_state
  const [busy, setBusy] = useState(null);   // plan slug currently checking out
  const [toast, setToast] = useState(null);

  // Optional contextual return flow — when the user lands here from the
  // Go-live Publish gate, the path is /account/billing?return=<agent-slug>.
  // We surface a banner ("you're upgrading to publish {agent}") and snap
  // them back to that agent's Go-live page once the upgrade succeeds.
  const returnSlug = (() => {
    try { return new URLSearchParams(window.location.search).get("return"); }
    catch { return null; }
  })();
  const returnAgent = returnSlug
    ? (agents || []).find((a) => (a.slug || String(a.id)) === returnSlug)
    : null;

  useEffect(() => {
    fetch("/api/plans").then((r) => r.json()).then((arr) => setPlans(Array.isArray(arr) ? arr : []));
    fetch("/api/me/plan").then((r) => r.json()).then(setMe).catch(() => {});
  }, []);

  // Load the Razorpay Checkout script lazily — only when the user clicks an
  // upgrade tier. No-op if already loaded.
  const ensureRzpScript = () => new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve();
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Razorpay script failed to load"));
    document.head.appendChild(s);
  });

  const handleUpgrade = async (plan) => {
    setBusy(plan.slug);
    try {
      const oRes = await fetch("/api/razorpay/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: plan.id }),
      });
      if (!oRes.ok) throw new Error("order failed");
      const order = await oRes.json();

      if (order.demo) {
        // No Razorpay keys configured — mark paid immediately, demo-mode.
        const u = await fetch("/api/me/upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plan_id: plan.id }),
        });
        if (!u.ok) throw new Error("upgrade failed");
        const fresh = await u.json();
        setMe(fresh);
        setToast({ kind: "ok", msg: `Upgraded to ${plan.label} (demo mode — no real payment).` });
        onPlanChanged && onPlanChanged(fresh);
        // If the user came from the Go-live publish gate, send them back so
        // they can finish what they started.
        if (returnSlug) {
          setTimeout(() => onNav && onNav(`/agent/${returnSlug}/go-live`), 900);
        }
        return;
      }

      // Real Razorpay flow.
      await ensureRzpScript();
      const rzp = new window.Razorpay({
        key: order.key,
        order_id: order.order_id,
        amount: order.amount_paise,
        currency: order.currency,
        name: "SpiderX.AI",
        description: `${order.plan_label} plan`,
        prefill: { name: order.name, email: order.email },
        theme: { color: "#6366f1" },
        handler: async (response) => {
          const u = await fetch("/api/me/upgrade", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              plan_id: plan.id,
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
            }),
          });
          if (!u.ok) {
            setToast({ kind: "err", msg: "Payment didn't verify — drop us a line at support@spiderx.ai." });
            return;
          }
          const fresh = await u.json();
          setMe(fresh);
          setToast({ kind: "ok", msg: `Welcome to ${plan.label}.` });
          onPlanChanged && onPlanChanged(fresh);
          // Same return-flow as demo mode: bounce back to Go-live if that's
          // where the user started.
          if (returnSlug) {
            setTimeout(() => onNav && onNav(`/agent/${returnSlug}/go-live`), 1200);
          }
        },
        modal: { ondismiss: () => setBusy(null) },
      });
      rzp.open();
    } catch (err) {
      setToast({ kind: "err", msg: "Couldn't start checkout — try again." });
    } finally {
      setBusy(null);
      setTimeout(() => setToast(null), 4500);
    }
  };

  const fmtPrice = (paise, currency) => {
    if (paise === 0) return "Free";
    const rupees = Math.round(paise / 100);
    return `₹${rupees.toLocaleString("en-IN")}`;
  };

  const currentPlanId = me?.plan?.id;
  const minutesPct = me ? Math.min(100, Math.round((me.minutes_used / Math.max(1, me.minutes_total)) * 100)) : 0;

  const body = html`
    <div class="db-overview">
      ${returnAgent ? html`
        <section class="db-publish-banner is-gated">
          <div class="db-publish-left">
            <div class="db-publish-dot is-gated" aria-hidden="true"></div>
            <div>
              <div class="db-publish-status">Unlock publishing</div>
              <div class="db-publish-copy">
                You're upgrading so <b>${returnAgent.name}</b> can go live to real callers and visitors. Pick a plan — we'll bring you straight back to Go-live to finish.
              </div>
            </div>
          </div>
          <div class="db-publish-actions">
            <button type="button" class="db-btn-ghost" onClick=${() => onNav && onNav(`/agent/${returnSlug}/go-live`)}>← Back to ${returnAgent.name}</button>
          </div>
        </section>
      ` : ""}

      <section class="db-panel">
        <div class="db-panel-head">
          <div>
            <h3 class="db-panel-title">Current plan</h3>
            <div class="db-panel-sub">
              ${me ? html`<b>${me.plan.label}</b> · ${me.minutes_left} of ${me.minutes_total} minutes left` : "Loading…"}
            </div>
          </div>
          ${me ? html`
            <div class="db-progress" title=${`${me.minutes_used} minutes used`}>
              <div class="db-progress-fill" style=${{ width: `${minutesPct}%` }}></div>
            </div>
          ` : ""}
        </div>
        ${me && me.plan_started_at ? html`
          <div class="db-form-help">Renewed / started ${me.plan_started_at.slice(0, 10)}.</div>
        ` : ""}
      </section>

      ${toast ? html`<div class=${"billing-toast billing-toast-" + toast.kind}>${toast.msg}</div>` : ""}

      <section>
        <h3 class="db-panel-title" style=${{ marginBottom: "8px" }}>Pick a plan</h3>
        <p class="db-panel-sub" style=${{ marginBottom: "16px" }}>Monthly billing. Cancel any time. INR pricing, ₹ via Razorpay.</p>
        <div class="billing-grid">
          ${plans.map((p) => {
            const isCurrent = p.id === currentPlanId;
            const isUpgrade = (me?.plan?.price_paise || 0) < p.price_paise;
            const ctaLabel = isCurrent ? "Current plan" :
              isUpgrade ? (p.price_paise === 0 ? "Switch to Free" : `Upgrade to ${p.label}`) :
              `Switch to ${p.label}`;
            return html`
              <div key=${p.id} class=${"billing-card" + (isCurrent ? " billing-card-current" : "") + (p.slug === "pro" ? " billing-card-featured" : "")}>
                ${p.slug === "pro" ? html`<div class="billing-card-flag">Most popular</div>` : ""}
                <div class="billing-card-label">${p.label}</div>
                <div class="billing-card-price">
                  <span class="billing-card-amount">${fmtPrice(p.price_paise, p.currency)}</span>
                  ${p.price_paise > 0 ? html`<span class="billing-card-period">/ month</span>` : ""}
                </div>
                <div class="billing-card-tagline">${p.tagline}</div>
                <ul class="billing-card-features">
                  ${(p.features || []).map((f) => html`<li key=${f}><span class="billing-card-tick" aria-hidden="true">✓</span><span>${f}</span></li>`)}
                </ul>
                <button
                  type="button"
                  class=${"db-btn-primary billing-card-cta" + (isCurrent ? " billing-card-cta-current" : "")}
                  disabled=${isCurrent || busy === p.slug}
                  onClick=${() => !isCurrent && handleUpgrade(p)}>
                  ${busy === p.slug ? "Loading…" : ctaLabel}
                </button>
              </div>
            `;
          })}
        </div>
      </section>
    </div>
  `;

  return html`
    <${DashboardShell}
      activeKey="billing"
      agents=${agents}
      plan=${plan}
      title="Billing & plan"
      subtitle="Manage your subscription and minutes balance."
      onNav=${onNav}
      body=${body}
      hideSidebar=${true}
    />
  `;
}

// Phone-number providers we route through, keyed by ISO country code. Each
// country gets the providers we've actually contracted with for that region
// — order = our preference order. `partner: true` flags the one we have a
// direct commercial relationship with (preferred billing + faster
// provisioning); others are courtesy-listed so the customer can ask for them
// if they already have an account or volume rates. India's preferred is GTS
// (Global Telco Solutions) — less well-known internationally but it's our
// partner there, so it leads.
const NUMBER_PROVIDERS = {
  IN: [
    { id: "gts",       name: "GTS (Global Telco Solutions)", desc: "Our India partner — fast number provisioning, GST-billed in INR, full DLT compliance handled.", coverage: "Pan-India · landline + mobile · toll-free", partner: true },
    { id: "exotel",    name: "Exotel",                       desc: "Indian cloud-comms operator. Familiar to most BPOs.",                                          coverage: "All India circles · landline + mobile" },
    { id: "knowlarity", name: "Knowlarity",                  desc: "Long-standing India provider, voice + SMS.",                                                    coverage: "All India · landline + virtual numbers" },
    { id: "plivo",     name: "Plivo",                        desc: "India-founded, now global. Good fallback if you also need US numbers.",                         coverage: "India + 220 countries" },
  ],
  US: [
    { id: "twilio",    name: "Twilio",                       desc: "Default for US numbers — local + toll-free + SMS-enabled.",                                     coverage: "All 50 states · local + 800 toll-free", partner: true },
    { id: "bandwidth", name: "Bandwidth",                    desc: "Tier-1 US carrier, used when enterprise volume needs direct interconnect.",                     coverage: "All 50 states · long-codes + toll-free" },
    { id: "telnyx",    name: "Telnyx",                       desc: "Developer-friendly, cheaper at scale.",                                                          coverage: "All 50 states · SIP + numbers" },
    { id: "vonage",    name: "Vonage",                       desc: "Enterprise contact-centre stack.",                                                                coverage: "Global · API-driven provisioning" },
  ],
  GB: [
    { id: "vonage",    name: "Vonage",                       desc: "Our preferred UK provider — geographic + non-geographic.",                                       coverage: "All UK area codes · 03 + 084x", partner: true },
    { id: "twilio",    name: "Twilio",                       desc: "Familiar for international teams.",                                                              coverage: "UK + 100+ countries" },
    { id: "gamma",     name: "Gamma",                        desc: "UK-domestic carrier, good for businesses wanting BT-interconnect numbers.",                     coverage: "All UK exchanges · BT-interconnect" },
  ],
  SG: [
    { id: "twilio",    name: "Twilio",                       desc: "Best Singapore coverage for cross-border calling.",                                              coverage: "Singapore + global SIP", partner: true },
    { id: "vonage",    name: "Vonage",                       desc: "Strong APAC presence.",                                                                          coverage: "SG · regional toll-free" },
    { id: "plivo",     name: "Plivo",                        desc: "Singapore numbers via the same account as India.",                                               coverage: "Singapore + SEA" },
  ],
  AE: [
    { id: "etisalat",  name: "Etisalat Business",            desc: "UAE-domestic carrier — required for landline-grade numbers + TRA compliance.",                  coverage: "All UAE emirates · landline + toll-free", partner: true },
    { id: "twilio",    name: "Twilio",                       desc: "International routing into UAE.",                                                                coverage: "UAE + global" },
  ],
  AU: [
    { id: "twilio",    name: "Twilio",                       desc: "Default AU provider, ACMA-compliant.",                                                            coverage: "All states · local + 1300 / 1800", partner: true },
    { id: "mnf",       name: "MNF Group",                    desc: "AU-domestic carrier with strong wholesale rates.",                                                coverage: "All Australian states" },
  ],
  CA: [
    { id: "twilio",    name: "Twilio",                       desc: "Pan-Canada provisioning, CRTC-compliant.",                                                        coverage: "All provinces · local + 8xx toll-free", partner: true },
    { id: "telnyx",    name: "Telnyx",                       desc: "Lower price-point at volume.",                                                                   coverage: "Canada + US" },
  ],
  DE: [
    { id: "vonage",    name: "Vonage",                       desc: "Our preferred DACH provider — handles BNetzA registration.",                                     coverage: "All German area codes", partner: true },
    { id: "twilio",    name: "Twilio",                       desc: "Wider EU coverage if you have offices outside Germany.",                                          coverage: "DE + EU" },
  ],
  FR: [
    { id: "vonage",    name: "Vonage",                       desc: "French numbers with ARCEP-compliant provisioning.",                                              coverage: "All French départements", partner: true },
    { id: "twilio",    name: "Twilio",                       desc: "Wider EU coverage.",                                                                              coverage: "FR + EU" },
  ],
};
// Fallback bucket for any country not specifically listed — global carriers
// only. We don't pretend to have local coverage where we don't.
const NUMBER_PROVIDERS_DEFAULT = [
  { id: "twilio", name: "Twilio", desc: "Our global default — 100+ countries, including most of LATAM, Africa, and APAC.", coverage: "Global · varies by country", partner: true },
  { id: "vonage", name: "Vonage", desc: "Strong enterprise coverage where Twilio is light.",                                coverage: "Global · API-driven" },
  { id: "plivo",  name: "Plivo",  desc: "Cost-effective for high-volume routes.",                                            coverage: "Global · 220 countries" },
];

function AgentNumbersPage({ agent, agents, presets, plan, onNav, org }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    if (!agent?.id) return;
    setLoading(true);
    fetch(`/api/agents/${agent.id}/number-requests`)
      .then((r) => r.ok ? r.json() : [])
      .then((arr) => { setRows(Array.isArray(arr) ? arr : []); setLoading(false); })
      .catch(() => setLoading(false));
  }, [agent?.id]);
  const fmt = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
  const statusTag = (s) => s === "fulfilled" ? "db-tag-green" : s === "in_progress" ? "db-tag-blue" : "db-tag-yellow";

  // Resolve which country's providers to show: agent's variables.country
  // first (set on the Business profile), org country second, India only as a
  // last resort so we don't accidentally surface DLT compliance copy to US
  // customers. The country label is whatever the user picked on the profile;
  // we map back to ISO via the COUNTRIES catalogue.
  const agentCountry = (agent?.variables?.country || "").trim().toUpperCase();
  const isoFromAgent = COUNTRIES.find((c) => c.id === agentCountry)?.id;
  const countryCode = isoFromAgent || org?.country || "";
  const countryLabel = (COUNTRIES.find((c) => c.id === countryCode)?.label) || "this region";
  const providers = NUMBER_PROVIDERS[countryCode] || NUMBER_PROVIDERS_DEFAULT;

  const providersPanel = html`
    <section class="db-panel">
      <h3 class="db-panel-title">
        Providers for ${countryLabel}
        ${countryCode ? html`<span class="db-panel-pill">${countryCode}</span>` : ""}
      </h3>
      <p class="db-panel-sub">
        ${countryCode
          ? html`When you submit a number request for ${agent.name}, we provision it through one of these carriers — our preferred partner first, others on request. Change the country on the <button class="db-link" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/profile`)}>Business profile</button> if this isn't right.`
          : html`Set the country on the <button class="db-link" type="button" onClick=${() => onNav && onNav(`/agent/${agent.slug || agent.id}/profile`)}>Business profile</button> to see your local providers. Until then, here are our global defaults.`}
      </p>
      <ul class="db-providers">
        ${providers.map((p) => html`
          <li class=${"db-provider" + (p.partner ? " is-partner" : "")} key=${p.id}>
            <div class="db-provider-head">
              <div class="db-provider-name">${p.name}</div>
              ${p.partner ? html`<span class="db-provider-pill">Preferred partner</span>` : ""}
            </div>
            <div class="db-provider-desc">${p.desc}</div>
            <div class="db-provider-coverage">${p.coverage}</div>
          </li>
        `)}
      </ul>
    </section>
  `;

  const body = loading ? html`<div class="db-empty"><div class="db-empty-sub">Loading…</div></div>` :
    rows.length === 0 ? html`
      <div class="db-overview">
        ${providersPanel}
        <section class="db-panel">
          <div class="db-empty" style=${{ margin: "16px auto" }}>
            <div class="db-empty-icon"></div>
            <div class="db-empty-title">No number requests yet</div>
            <div class="db-empty-sub">Submit one from the Go live page and we'll provision a number for ${agent.name} through one of the providers above.</div>
            <button class="db-btn-primary" onClick=${() => onNav(`/agent/${agent.slug || agent.id}/go-live`)}>Go live →</button>
          </div>
        </section>
      </div>
    ` : html`
      <div class="db-overview">
        ${providersPanel}
        <section class="db-panel">
          <h3 class="db-panel-title">Your requests</h3>
          <div class="db-table-wrap">
            <table class="db-table">
              <thead><tr><th>Submitted</th><th>Country</th><th>City</th><th>Send to</th><th>Status</th></tr></thead>
              <tbody>
                ${rows.map((r) => html`
                  <tr key=${r.id}>
                    <td>${fmt(r.created_at)}</td>
                    <td>${r.country || "—"}</td>
                    <td>${r.city || "—"}</td>
                    <td>${r.delivery_handle}</td>
                    <td><span class=${"db-tag " + statusTag(r.status)}>${(r.status || "pending").replace(/_/g, " ")}</span></td>
                  </tr>
                `)}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    `;
  return html`
    <${DashboardShell}
      activeKey="numbers"
      agent=${agent}
      agents=${agents}
      plan=${plan}
      title="Number requests"
      subtitle=${`Phone numbers ${agent.name} has been provisioned (or queued for).`}
      onNav=${onNav}
      body=${body}
    />
  `;
}

// New light-theme agents list — uses dashboard shell + .db-card primitives.
function DashboardAgentsList({ agents, presets, plan, onBuildNew, onOpen, onDelete, onNav }) {
  const [q, setQ] = useState("");
  const labelFor = (list, id) => (list || []).find((x) => x.id === id)?.label || _prettifyEnumId(id);
  const filter = q.trim().toLowerCase();
  // Searching now also matches against the business name we surface in the
  // header — a user typing "BrightSmile" should find the agent regardless of
  // its display name.
  const filtered = filter
    ? agents.filter((a) => [
        a.name, a.sector, a.locale, a.persona,
        a?.variables?.business_name, a?.variables?.industry, a?.variables?.city,
      ].some((s) => (s || "").toLowerCase().includes(filter)))
    : agents;

  // Quick humanised "how long ago" formatter for the activity footer. Goes
  // from minutes → hours → days → date string so a glance gives you the
  // right resolution without overloading any single card.
  const formatAgo = (iso) => {
    if (!iso) return null;
    const norm = iso.includes("T") ? iso : (iso.replace(" ", "T") + (iso.match(/[Z+-]\d{2}:?\d{2}$/) ? "" : "Z"));
    const ms = Date.now() - new Date(norm).getTime();
    if (Number.isNaN(ms) || ms < 0) return null;
    const m = Math.round(ms / 60000);
    if (m < 1)   return "just now";
    if (m < 60)  return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24)  return `${h}h ago`;
    const d = Math.round(h / 24);
    if (d < 7)   return `${d}d ago`;
    return new Date(norm).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };

  const actions = html`
    ${agents.length > 5 ? html`
      <input class="db-search" type="search" placeholder="Search by name, business, sector, language…"
             value=${q} onInput=${(e) => setQ(e.target.value)} />
    ` : ""}
    <button class="db-btn-primary" onClick=${onBuildNew}>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
      <span>Build new</span>
    </button>
  `;

  const body = filtered.length === 0 ? html`
    <div class="db-empty">
      <div class="db-empty-icon"></div>
      <div class="db-empty-title">${agents.length === 0 ? "No agents yet" : "No matches"}</div>
      <div class="db-empty-sub">
        ${agents.length === 0
          ? "Talk to Eva for a minute — she'll build your first agent and you'll see it here."
          : "Try a different search term, or build a new agent."}
      </div>
      ${agents.length === 0 ? html`
        <button class="db-btn-primary" onClick=${onBuildNew}>
          <span>Build your first agent</span>
        </button>
      ` : ""}
    </div>
  ` : html`
    <ul class="db-grid">
      ${filtered.map((a) => {
        const business = (a?.variables?.business_name || "").trim();
        const sectorLabel = labelFor(presets?.sectors, a.sector);
        const localeLabel = labelFor(presets?.locales, a.locale);
        const callsCount = a.calls_count ?? 0;
        const lastCallAt = formatAgo(a.last_call_at);
        // "New" stays as a freshness marker for agents built in the last hour
        // — surfaces alongside the publish state so a brand-new draft is
        // visibly distinct from an old one nobody touched.
        const isFresh = (() => {
          if (!a.created_at) return false;
          const iso = a.created_at.includes("T") ? a.created_at :
                      (a.created_at.replace(" ", "T") + (a.created_at.match(/[Z+-]\d{2}:?\d{2}$/) ? "" : "Z"));
          const age = Date.now() - new Date(iso).getTime();
          return age >= 0 && age < 60 * 60 * 1000;
        })();
        const isLive = !!a.published;

        return html`
          <li class="db-card" key=${a.id}>
            <button class="db-card-tap" onClick=${() => onOpen(a)}>
              <div class="db-card-row">
                <div class="db-card-thumb"></div>
                <div class="db-card-headblock">
                  <div class="db-card-name">
                    <span class="db-card-agent-name">${a.name}</span>
                    ${business ? html`<span class="db-card-name-sep">·</span><span class="db-card-business">${business}</span>` : ""}
                  </div>
                  <div class="db-card-sub">${sectorLabel} · ${localeLabel} · ${voiceTag(a.voice || "Aoede")}</div>
                </div>
                <div class="db-card-pills">
                  ${isLive
                    ? html`<span class="db-card-pill is-live">Live</span>`
                    : html`<span class="db-card-pill is-draft">Draft</span>`}
                  ${isFresh ? html`<span class="db-card-pill is-fresh" title="Built in the last hour">New</span>` : ""}
                </div>
              </div>
              ${a.greeting ? html`<div class="db-card-greet">"${a.greeting}"</div>` : ""}
              <div class="db-card-foot">
                <span class="db-card-stat">
                  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.89.33 1.77.62 2.61a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.47-1.18a2 2 0 0 1 2.11-.45c.84.29 1.72.5 2.61.62A2 2 0 0 1 22 16.92z"/></svg>
                  <span>${callsCount} ${callsCount === 1 ? "call" : "calls"}</span>
                </span>
                ${lastCallAt ? html`
                  <span class="db-card-stat">
                    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
                    <span>Last ${lastCallAt}</span>
                  </span>
                ` : html`<span class="db-card-stat db-card-stat-quiet">No calls yet</span>`}
                <span class="db-card-foot-spacer"></span>
                <span class="db-card-open">Open <span aria-hidden="true">→</span></span>
              </div>
            </button>
            <button class="db-card-del" onClick=${(e) => { e.stopPropagation(); onDelete(a.id); }} aria-label="Delete">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
            </button>
          </li>
        `;
      })}
    </ul>
  `;

  return html`
    <${DashboardShell}
      activeKey="agents"
      plan=${plan}
      agents=${agents}
      title=${`${agents.length} ${agents.length === 1 ? "Phone AI agent" : "Phone AI agents"}`}
      subtitle="Open one to manage call logs, knowledge and settings."
      actions=${actions}
      onNav=${onNav}
      body=${body}
      hideSidebar=${true}
    />
  `;
}

function AgentsListPage({ agents, presets, onBuildNew, onOpen, onDelete }) {
  const [q, setQ] = useState("");
  const labelFor = (list, id) => (list || []).find((x) => x.id === id)?.label || _prettifyEnumId(id);
  const filter = q.trim().toLowerCase();
  const filtered = filter
    ? agents.filter((a) => [a.name, a.sector, a.locale, a.persona].some((s) => (s || "").toLowerCase().includes(filter)))
    : agents;

  return html`
    <div class="agents-page">
      <header class="agents-head">
        <div>
          <div class="agents-eyebrow">Your agents</div>
          <h1 class="agents-title">${agents.length} ${agents.length === 1 ? "Phone AI agent" : "Phone AI agents"}</h1>
        </div>
        <div class="agents-actions">
          ${agents.length > 5 ? html`
            <input
              class="agents-search"
              type="search"
              placeholder="Search by name, sector, language…"
              value=${q}
              onInput=${(e) => setQ(e.target.value)}
            />
          ` : ""}
          <button class="agents-new" onClick=${onBuildNew}>
            <span class="agents-new-spark" aria-hidden="true">✨</span>
            <span>Build new</span>
          </button>
        </div>
      </header>

      ${filtered.length === 0 ? html`
        <div class="agents-empty">
          <div class="agents-empty-orb"></div>
          <div class="agents-empty-title">${agents.length === 0 ? "No agents yet" : "No matches"}</div>
          <div class="agents-empty-sub">
            ${agents.length === 0
              ? html`<span>Talk to Eva for a minute — she'll build your first agent and you'll see it here.</span>`
              : html`<span>Try a different search term, or build a new agent.</span>`}
          </div>
          ${agents.length === 0 ? html`
            <button class="agents-empty-cta" onClick=${onBuildNew}>
              <span class="agents-new-spark">✨</span>
              <span>Build your first agent</span>
            </button>
          ` : ""}
        </div>
      ` : html`
        <ul class="agents-grid">
          ${filtered.map((a) => html`
            <li class="agent-card" key=${a.id}>
              <button class="agent-card-tap" onClick=${() => onOpen(a)}>
                <div class="agent-card-thumb"></div>
                <div class="agent-card-meta">
                  <div class="agent-card-name">${a.name}</div>
                  <div class="agent-card-sub">${labelFor(presets?.sectors, a.sector)} · ${labelFor(presets?.locales, a.locale)} · ${a.voice || "Aoede"}</div>
                  ${a.greeting ? html`<div class="agent-card-greet">"${a.greeting}"</div>` : ""}
                </div>
              </button>
              <button class="agent-card-del" onClick=${(e) => { e.stopPropagation(); onDelete(a.id); }} aria-label="Delete">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
              </button>
            </li>
          `)}
        </ul>
      `}
    </div>
  `;
}

function TweaksDrawer({ open, onClose, agents, refreshAgents, onTest, onDelete, tweaks, setTweaks, schema, buildSnapshot, presets, initialEditId, clearInitialEditId }) {
  const [tab, setTab] = useState("studio");
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState(null);
  const [saveState, setSaveState] = useState({ msg: "", cls: "" });

  // Open an agent's edit view
  const startEdit = async (id) => {
    setSaveState({ msg: "", cls: "" });
    try {
      const r = await fetch(`/api/agents/${id}`);
      if (!r.ok) throw new Error("not found");
      const full = await r.json();
      setDraft({
        ...full,
        guardrails: Array.isArray(full.guardrails) ? full.guardrails : [],
        connectors: Array.isArray(full.connectors) ? full.connectors : [],
        voice_tweaks: full.voice_tweaks || {},
        outcomes: Array.isArray(full.outcomes) ? full.outcomes : [],
        variables: (full.variables && typeof full.variables === "object") ? full.variables : {},
        webhook_url: full.webhook_url || "",
        webhook_headers: (full.webhook_headers && typeof full.webhook_headers === "object") ? full.webhook_headers : {},
      });
      setEditingId(id);
    } catch (e) {
      setSaveState({ msg: "Could not load agent.", cls: "err" });
    }
  };

  const closeEdit = () => { setEditingId(null); setDraft(null); setSaveState({ msg: "", cls: "" }); };

  // If the drawer was opened with a specific agent to edit (e.g. from the
  // reveal card's "Edit" action), auto-load its editor view.
  useEffect(() => {
    if (open && initialEditId && editingId !== initialEditId) {
      setTab("studio");
      startEdit(initialEditId);
      clearInitialEditId && clearInitialEditId();
    }
  }, [open, initialEditId]);

  const updateDraft = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const updateTweak = (k, v) => setDraft((d) => ({ ...d, voice_tweaks: { ...(d.voice_tweaks || {}), [k]: v } }));
  const toggleArr = (key, id) => {
    const cur = new Set(draft[key] || []);
    cur.has(id) ? cur.delete(id) : cur.add(id);
    updateDraft(key, Array.from(cur));
  };

  const saveEdit = async () => {
    if (!draft) return;
    setSaveState({ msg: "Saving…", cls: "" });
    try {
      const payload = {
        name: draft.name,
        sector: draft.sector,
        locale: draft.locale,
        persona: draft.persona,
        greeting: draft.greeting,
        voice: draft.voice,
        system_prompt: draft.system_prompt,
        guardrails: draft.guardrails || [],
        connectors: draft.connectors || [],
        voice_tweaks: draft.voice_tweaks || {},
        // Tier 2: advanced fields (silently seeded by Eva, editable here)
        outcomes: draft.outcomes || [],
        variables: draft.variables || {},
        webhook_url: draft.webhook_url || "",
        webhook_headers: draft.webhook_headers || {},
      };
      const r = await fetch(`/api/agents/${editingId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setSaveState({ msg: "Saved. Applies on next call.", cls: "ok" });
      refreshAgents && refreshAgents();
      setTimeout(() => setSaveState({ msg: "", cls: "" }), 2200);
    } catch (e) {
      setSaveState({ msg: "Could not save: " + (e?.message || ""), cls: "err" });
    }
  };

  if (!presets) presets = {};

  const update = (k, v) => {
    const next = { ...tweaks, [k]: v };
    setTweaks(next);
    saveTweaks(next);
  };

  const renderField = (key, def) => {
    const current = tweaks[key] !== undefined ? tweaks[key] : def.default;
    if (def.type === "select") {
      return html`
        <div class="tw-field" key=${key}>
          <div class="label-row">
            <span class="name">${def.label}</span>
          </div>
          <div class="help">${def.help}</div>
          <select class="tw-select" value=${current ?? ""} onChange=${(e) => update(key, e.target.value)}>
            ${def.options.map((o) => html`<option key=${o.id} value=${o.id}>${o.label}</option>`)}
          </select>
        </div>
      `;
    }
    if (def.type === "range") {
      return html`
        <div class="tw-field" key=${key}>
          <div class="label-row">
            <span class="name">${def.label}</span>
            <span class="val">${current}</span>
          </div>
          <div class="help">${def.help}</div>
          <input
            class="tw-range"
            type="range"
            min=${def.min}
            max=${def.max}
            step=${def.step}
            value=${current}
            onInput=${(e) => update(key, def.step < 1 ? parseFloat(e.target.value) : parseInt(e.target.value, 10))}
          />
        </div>
      `;
    }
    if (def.type === "bool") {
      const on = !!current;
      return html`
        <div class="tw-field" key=${key}>
          <div class="label-row">
            <span class="name">${def.label}</span>
            <span class=${"tw-toggle " + (on ? "on" : "")} onClick=${() => update(key, !on)}></span>
          </div>
          <div class="help">${def.help}</div>
        </div>
      `;
    }
    return null;
  };

  // Editor-only drawer. The tweaks-panel's "Your agents" tab has been
  // replaced by the /agents page; its Voice / Advanced tabs were redundant
  // with per-agent settings now living inside the editor itself. The drawer
  // now opens only when the cockpit's "Open advanced settings" button fires
  // (or via the ?drawer=<id> debug URL).

  return html`
    <div class=${"tweaks-scrim " + (open ? "open" : "")} onClick=${onClose}></div>
    <aside class=${"tweaks-drawer " + (open ? "open" : "")}>
      <header>
        <h2>${draft?.name ? `Edit ${draft.name}` : "Edit agent"}</h2>
        <button class="x" onClick=${onClose} aria-label="Close">${Icons.close}</button>
      </header>
      <div class="body">
        ${editingId !== null && draft ? html`
          <${AgentEditor}
            draft=${draft}
            updateDraft=${updateDraft}
            updateTweak=${updateTweak}
            toggleArr=${toggleArr}
            presets=${presets}
            schema=${schema}
            saveEdit=${saveEdit}
            closeEdit=${closeEdit}
            saveState=${saveState}
            onTest=${onTest}
          />
        ` : html`
          <div class="tw-empty">
            ${saveState?.msg ? saveState.msg : "Loading…"}
          </div>
        `}
      </div>
    </aside>
  `;
}

// ─────────────────────── App ─────────────────────────────────────────────

// ─────────────────────── BuildRecovery banner ─────────────────────────────
// Shown when a voice-build call ended abnormally with savable info but
// no `agent_saved` event arrived. Two recovery paths:
//   1. Server already silently committed on WS close → we show
//      "Eva saved {name} in the background — open her now?"
//   2. Server didn't commit (slots above floor but Eva never even fired
//      save_agent, or finalize race) → we show
//      "Save what we have as {name}? Eva captured: …"
// Either way, the operator's voice work is not lost. Dismissing the
// banner explicitly abandons the build_session row.
function BuildRecovery({ sid, onCommitted, onAbandoned }) {
  const [state, setState] = useState(null);   // server's /state payload
  const [phase, setPhase] = useState("checking"); // checking | offer | committing | error | dismissed
  const [error, setError] = useState("");

  // On mount: fetch the build_session state. Three branches:
  //   committed_already → "Eva already saved Maya in the background"
  //                       → show 'Open Maya' button (no finalize needed)
  //   in_progress + finalizable → show 'Save & test Maya' button
  //   anything else (never existed, abandoned, not finalizable) → dismiss
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/build-sessions/${encodeURIComponent(sid)}/state`);
        if (cancelled) return;
        if (!r.ok) {
          setPhase("dismissed");
          return;
        }
        const data = await r.json();
        setState(data);
        if (!data.exists) {
          // Never started or belongs to another user. Clear the
          // stale sid + dismiss.
          setPhase("dismissed");
          try { sessionStorage.removeItem("eva_build_sid"); } catch {}
          if (onAbandoned) onAbandoned();
          return;
        }
        // L4 silent-commit path already shipped the agent — surface it.
        if (data.status === "committed" && data.committed_agent) {
          setPhase("committed_already");
          return;
        }
        if (data.status !== "in_progress") {
          // Abandoned (operator explicitly dismissed earlier) or
          // committed without a linked agent (race? data corruption).
          // Either way, nothing useful to do — clear + dismiss.
          setPhase("dismissed");
          try { sessionStorage.removeItem("eva_build_sid"); } catch {}
          if (onAbandoned) onAbandoned();
          return;
        }
        if (!data.finalizable) {
          // Not enough captured to save — silently drop without nagging.
          setPhase("dismissed");
          try { sessionStorage.removeItem("eva_build_sid"); } catch {}
          // We don't formally abandon the row here — the daily TTL
          // sweeper picks it up eventually. The operator might also
          // resume the build via the same sid if they reopen the tab.
          if (onAbandoned) onAbandoned();
          return;
        }
        setPhase("offer");
      } catch (e) {
        console.warn("[BuildRecovery] state fetch failed:", e);
        if (!cancelled) setPhase("dismissed");
      }
    })();
    return () => { cancelled = true; };
  }, [sid, onAbandoned]);

  const onSave = useCallback(async () => {
    setPhase("committing");
    setError("");
    try {
      const r = await fetch(`/api/build-sessions/${encodeURIComponent(sid)}/finalize`, {
        method: "POST",
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(body.detail || `Couldn't save (HTTP ${r.status}).`);
        setPhase("error");
        return;
      }
      const data = await r.json();
      try { sessionStorage.removeItem("eva_build_sid"); } catch {}
      if (data?.agent && onCommitted) onCommitted(data.agent);
    } catch (e) {
      setError(String(e?.message || e || "Network error"));
      setPhase("error");
    }
  }, [sid, onCommitted]);

  const onDiscard = useCallback(async () => {
    try {
      await fetch(`/api/build-sessions/${encodeURIComponent(sid)}/abandon`, {
        method: "POST",
      });
    } catch {}
    try { sessionStorage.removeItem("eva_build_sid"); } catch {}
    setPhase("dismissed");
    if (onAbandoned) onAbandoned();
  }, [sid, onAbandoned]);

  if (phase === "checking" || phase === "dismissed" || !state) return null;

  // ── Branch A: silently auto-committed by the WS-close path ──
  // Operator hung up; server finished the save in the background.
  // Show a friendly "already saved" panel with an Open button.
  if (phase === "committed_already" && state.committed_agent) {
    const ag = state.committed_agent;
    const nm = ag.name || state.agent_name || "your agent";
    const onOpen = () => {
      try { sessionStorage.removeItem("eva_build_sid"); } catch {}
      if (onCommitted) onCommitted(ag);
    };
    const onDismiss = () => {
      try { sessionStorage.removeItem("eva_build_sid"); } catch {}
      if (onAbandoned) onAbandoned();
    };
    return html`
      <div class="br-backdrop" onClick=${onDismiss}></div>
      <div class="br-card" role="dialog" aria-label="Agent saved in background">
        <div class="br-head">
          <span class="br-icon" style=${{ background: "linear-gradient(135deg, #34d399, #10b981)" }}>✓</span>
          <span class="br-title">${nm} is ready</span>
        </div>
        <p class="br-body">
          Your call ended before the reveal, but Eva had enough to
          finish — ${nm} is saved and waiting in your dashboard.
        </p>
        <div class="br-actions">
          <button class="br-btn-secondary" type="button" onClick=${onDismiss}>Not now</button>
          <button class="br-btn-primary" type="button" onClick=${onOpen}>
            Open ${nm}'s dashboard
          </button>
        </div>
      </div>
    `;
  }

  // ── Branch B: still in_progress + finalizable — offer Save & test ──
  if (!state.finalizable) return null;

  const proposedName = (state.agent_name || "your agent").trim();
  const sectorLabel = (state.sector_kind || "").trim();
  const businessLabel = (state.business_name || "").trim();
  const jobLabel = (state.primary_job || "").trim();

  return html`
    <div class="br-backdrop" onClick=${onDiscard}></div>
    <div class="br-card" role="dialog" aria-label="Recover unsaved build">
      <div class="br-head">
        <span class="br-icon">!</span>
        <span class="br-title">
          Eva almost finished — want to save ${proposedName}?
        </span>
      </div>
      <p class="br-body">
        The build call ended before we could spin her up, but Eva captured
        enough to ship a working draft. Save it now and you can
        fine-tune in the dashboard.
      </p>
      <div class="br-summary">
        ${businessLabel ? html`<div class="br-summary-row"><span class="br-summary-label">Business</span><strong>${businessLabel}</strong></div>` : ""}
        ${sectorLabel ? html`<div class="br-summary-row"><span class="br-summary-label">Sector</span>${sectorLabel}</div>` : ""}
        ${jobLabel ? html`<div class="br-summary-row"><span class="br-summary-label">Job</span>${jobLabel}</div>` : ""}
        <div class="br-summary-row"><span class="br-summary-label">Agent</span><strong>${proposedName}</strong></div>
      </div>
      <div class="br-actions">
        <button class="br-btn-secondary" type="button"
                onClick=${onDiscard}
                disabled=${phase === "committing"}>Discard</button>
        <button class="br-btn-primary" type="button"
                onClick=${onSave}
                disabled=${phase === "committing"}>
          ${phase === "committing" ? "Saving…" : `Save & test ${proposedName}`}
        </button>
      </div>
      ${phase === "error" ? html`<div class="br-body" style=${{ color: "#b91c1c", paddingTop: 0 }}>${error}</div>` : ""}
    </div>
  `;
}


// ──────────────────────────── FloatingEva ─────────────────────────────────
// Persistent helper Eva in the bottom-right of every dashboard page.
// Two visual states: collapsed bubble → tap → expanded card with a small
// audio-reactive blob, a caption rail, a text input, and a mic/mute toggle.
//
// Lifecycle: lazy — engine + WS only spin up when the user taps the bubble.
// Closing the card tears both down. Push-to-talk via the mute toggle; the
// engine streams mic chunks whenever it's NOT muted.
//
// Context: every render checks (currentRoute, agentSummary, pageLabel) and
// sends a `{type:"context"}` message over the WS whenever those change. The
// server uses the latest context to compose the next reply.
function FloatingEva({
  visible,
  user,
  currentRoute,
  pageLabel,
  contextAgent,
  refreshAgent,
  refreshAgents,
  onNavigate,
}) {
  const [open, setOpen] = useState(false);
  const [convState, setConvState] = useState("idle"); // idle | connecting | ready | listening | speaking | error
  const [errorMsg, setErrorMsg] = useState(null);
  const [micMuted, setMicMuted] = useState(false);
  const [textValue, setTextValue] = useState("");
  const [evaCaption, setEvaCaption] = useState("");
  const [userCaption, setUserCaption] = useState("");

  const wsRef = useRef(null);
  const engineRef = useRef(null);
  const lastContextSentRef = useRef("");
  const evaSegRef = useRef("");
  const userSegRef = useRef("");

  // Compose the context payload from props. Re-derived every render; the
  // effect below diffs against last-sent and only emits when something
  // material changed. Keep this shape in sync with _format_helper_context
  // server-side.
  const contextPayload = useMemo(() => {
    const agentSummary = contextAgent
      ? `${contextAgent.name} · ${contextAgent.sector || "?"} · ${contextAgent.locale || "?"} · ${contextAgent.published ? "live" : "draft"}`
      : null;
    return {
      type: "context",
      page: currentRoute || "/",
      page_label: pageLabel || null,
      agent_id: contextAgent ? contextAgent.id : null,
      agent_summary: agentSummary,
    };
  }, [currentRoute, pageLabel, contextAgent]);

  // Send context updates whenever the payload changes AND the WS is open.
  useEffect(() => {
    if (!open) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const key = JSON.stringify(contextPayload);
    if (key === lastContextSentRef.current) return;
    lastContextSentRef.current = key;
    try { ws.send(JSON.stringify(contextPayload)); } catch {}
  }, [open, contextPayload]);

  const teardown = useCallback(() => {
    try { wsRef.current?.close(); } catch {}
    wsRef.current = null;
    try { engineRef.current?.stop(); } catch {}
    engineRef.current = null;
    lastContextSentRef.current = "";
    evaSegRef.current = "";
    userSegRef.current = "";
    setEvaCaption("");
    setUserCaption("");
    setMicMuted(false);
    setTextValue("");
    setConvState("idle");
    setErrorMsg(null);
  }, []);

  // When the widget closes, cleanly drop the engine + WS so the mic stops.
  useEffect(() => {
    if (!open) teardown();
    return () => { if (!open) teardown(); };
  }, [open, teardown]);

  // If the parent hides us mid-conversation (e.g. operator starts a build
  // on the main blob → view flips to "call") force-close so we release
  // the mic instead of fighting the main engine for it.
  useEffect(() => {
    if (!visible && open) setOpen(false);
  }, [visible, open]);

  // Engine + WS startup on first open. Re-runs only when `open` flips true.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    let localWs = null;
    let localEngine = null;
    (async () => {
      setConvState("connecting");
      setErrorMsg(null);
      const engine = new AudioEngine();
      localEngine = engine;
      try {
        await engine.start({
          onMicChunk: (buf) => {
            const ws = wsRef.current;
            if (ws && ws.readyState === WebSocket.OPEN) ws.send(buf);
          },
        });
      } catch (e) {
        console.error("[FloatingEva] engine.start failed:", e);
        if (cancelled) return;
        const msg = String(e?.message || e || "");
        setErrorMsg(/permission|denied/i.test(msg)
          ? "Mic permission was denied — allow it to talk to Eva."
          : "Couldn't start the mic. Try again in a moment.");
        setConvState("error");
        return;
      }
      if (cancelled) { try { engine.stop(); } catch {} return; }
      engineRef.current = engine;

      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const qsObj = {
        locale: navigator.language || "en-US",
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      };
      const uid = user?.id || currentUserId();
      if (uid) qsObj.user_id = String(uid);
      const qs = new URLSearchParams(qsObj).toString();
      const ws = new WebSocket(`${proto}//${location.host}/ws/helper?${qs}`);
      localWs = ws;
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        console.info("[FloatingEva] WS open");
      };
      ws.onerror = (e) => {
        console.error("[FloatingEva] WS error:", e);
        if (cancelled) return;
        setErrorMsg("Couldn't reach Eva. Is the server running with /ws/helper? Check the server log.");
        setConvState("error");
      };
      ws.onclose = (ev) => {
        console.warn("[FloatingEva] WS close", ev && { code: ev.code, reason: ev.reason });
        if (cancelled) return;
        // If we close BEFORE getting a "ready" event, the endpoint either
        // isn't reachable or the server rejected us. Surface a clear
        // error instead of silently dropping back to "Idle" (which made
        // the bug look like 'Eva is just lazy').
        setConvState((cur) => {
          if (cur === "error") return cur;
          const preReady = cur === "connecting";
          if (preReady) {
            const code = ev && ev.code;
            setErrorMsg(
              code === 1006
                ? "Couldn't reach /ws/helper. Restart the server (uvicorn --reload sometimes misses new routes — Ctrl-C and re-run ./run.sh)."
                : `Eva disconnected (code ${code ?? "?"}). Tap to retry.`,
            );
            return "error";
          }
          // Post-ready close — connection dropped after a healthy start.
          setErrorMsg("Connection dropped. Close and reopen to retry.");
          return "error";
        });
      };

      ws.onmessage = (ev) => {
        if (typeof ev.data !== "string") {
          // Binary = Eva's PCM audio chunks → speaker.
          engineRef.current?.playPcm(ev.data);
          // Audio playback = speaking state. We clear it on turn_complete.
          setConvState((s) => (s === "speaking" ? s : "speaking"));
          return;
        }
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "ready") {
          setConvState("ready");
          // Push the initial context immediately so Eva's first reply is
          // aware. The diff-on-payload effect would also catch this, but
          // sending here too is harmless (the server dedupes).
          try {
            ws.send(JSON.stringify(contextPayload));
            lastContextSentRef.current = JSON.stringify(contextPayload);
          } catch {}
        } else if (msg.type === "reconnected") {
          setConvState("ready");
        } else if (msg.type === "transcript") {
          if (msg.role === "user") {
            userSegRef.current += msg.text || "";
            setUserCaption(userSegRef.current);
          } else if (msg.role === "model") {
            evaSegRef.current += msg.text || "";
            setEvaCaption(evaSegRef.current);
          }
        } else if (msg.type === "turn_complete") {
          // Snapshot + reset the per-turn segments. Captions linger on
          // screen until the NEXT turn overwrites them.
          evaSegRef.current = "";
          userSegRef.current = "";
          setConvState((s) => (s === "error" ? s : "ready"));
        } else if (msg.type === "navigate") {
          // Server-driven navigation — Eva did an edit and wants the
          // operator to see the page that owns it.
          if (msg.route && onNavigate) onNavigate(msg.route);
        } else if (msg.type === "agent_updated") {
          // Eva patched the current agent. Re-fetch so the open page
          // reflects the new values immediately.
          if (refreshAgent) refreshAgent();
          if (refreshAgents) refreshAgents();
        } else if (msg.type === "error") {
          setErrorMsg(msg.message || "Eva ran into an error.");
          setConvState("error");
        }
      };
    })();
    return () => {
      cancelled = true;
      try { localWs?.close(); } catch {}
      try { localEngine?.stop(); } catch {}
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Toggle the engine's mic mute. Engine stays alive; just stops streaming.
  const toggleMic = useCallback(() => {
    const next = !micMuted;
    setMicMuted(next);
    engineRef.current?.setMuted(next);
  }, [micMuted]);

  // Submit the typed message over the WS as a {type:"text"} payload.
  // If the WS isn't open yet, surface that loudly instead of returning
  // silently — the user's biggest source of "nothing happened" confusion
  // is hitting Enter before the connection finished establishing.
  const sendText = useCallback(() => {
    const txt = textValue.trim();
    if (!txt) return;
    const ws = wsRef.current;
    const rs = ws?.readyState;
    if (!ws) {
      setErrorMsg("Not connected to Eva yet. Reopening the helper…");
      setConvState("error");
      console.warn("[FloatingEva] sendText with no WS");
      return;
    }
    if (rs !== WebSocket.OPEN) {
      const stateName = rs === 0 ? "still connecting" : rs === 2 ? "closing" : rs === 3 ? "closed" : `state ${rs}`;
      setErrorMsg(`Can't send — Eva is ${stateName}. Try again in a sec.`);
      console.warn("[FloatingEva] sendText with WS readyState=", rs);
      return;
    }
    try {
      ws.send(JSON.stringify({ type: "text", text: txt }));
      console.info("[FloatingEva] sent text:", txt);
      setTextValue("");
      // Echo locally so the operator sees their question appear.
      userSegRef.current = txt;
      setUserCaption(txt);
    } catch (e) {
      console.error("[FloatingEva] sendText threw:", e);
      setErrorMsg("Couldn't send. Try again.");
    }
  }, [textValue]);

  if (!visible) return null;

  // ── Idle bubble ───────────────────────────────────────────────────────
  if (!open) {
    return html`
      <div class="feva-root">
        <button class="feva-bubble" type="button" onClick=${() => setOpen(true)} aria-label="Open Eva helper">
          <div class="feva-bubble-blob">
            <${VoiceBlob} engineRef=${engineRef} mode=${"idle"} size=${36} />
          </div>
          <span class="feva-bubble-label">Ask Eva</span>
        </button>
      </div>
    `;
  }

  // ── Expanded card ─────────────────────────────────────────────────────
  const blobMode = convState === "speaking" ? "speak"
    : convState === "listening" ? "listen"
    : convState === "ready" ? "listen"
    : convState === "connecting" ? "calling"
    : convState === "error" ? "error"
    : "idle";

  const stateText =
    convState === "connecting" ? "Connecting…" :
    convState === "ready"      ? (micMuted ? "Mic muted · type or unmute" : "Listening — start talking") :
    convState === "speaking"   ? "Eva is talking" :
    convState === "listening"  ? "Listening…" :
    convState === "error"      ? (errorMsg || "Couldn't reach Eva") :
    "Idle";

  const contextLabel = pageLabel
    || (contextAgent ? `${contextAgent.name} · ${currentRoute || ""}` : currentRoute || "Dashboard");

  return html`
    <div class="feva-root">
      <div class="feva-card" role="dialog" aria-label="Eva helper">
        <div class="feva-card-head">
          <div class="feva-card-blob">
            <${VoiceBlob} engineRef=${engineRef} mode=${blobMode} size=${44} />
          </div>
          <div class="feva-card-head-text">
            <div class="feva-card-title">Eva</div>
            <div class=${"feva-card-state" + (convState === "error" ? " is-error" : "")}>
              ${stateText}
            </div>
          </div>
          <div class="feva-card-actions">
            <button class="feva-card-endcall" type="button"
                    onClick=${() => setOpen(false)}
                    title="End the call with Eva (your conversation is remembered)"
                    aria-label="End call">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M22 12.5c-3.5-3.5-9.5-3.5-13 0l-1 1c-1 1-2.5 1-3.5 0L2 11c-.6-.6-.6-1.5 0-2 4-3.5 12-5 18 0 .6.5.6 1.5 0 2l-2.5 1.5c-1 1-2.5 1-3.5 0z" transform="rotate(135 12 12)"/>
              </svg>
              <span>End call</span>
            </button>
            <button class="feva-card-close" type="button" onClick=${() => setOpen(false)} aria-label="Minimise — your conversation is remembered" title="Minimise">×</button>
          </div>
        </div>

        <div class="feva-context" title="What Eva can see right now">
          <span class="feva-context-dot"></span>
          <span>Eva can see: ${contextLabel}</span>
        </div>

        <div class="feva-captions">
          ${userCaption ? html`<div class="feva-cap feva-cap-user">“${userCaption}”</div>` : ""}
          ${evaCaption ? html`<div class="feva-cap">${evaCaption}</div>` : ""}
          ${!userCaption && !evaCaption ? html`
            <div class="feva-cap-empty">
              Talk or type — try “rename her to Priya” or “what plan am I on?”
            </div>
          ` : ""}
        </div>

        <div class="feva-input-row">
          <button
            class=${"feva-mic-btn" + (micMuted ? " is-muted" : convState === "listening" || convState === "ready" ? " is-listening" : "")}
            type="button"
            onClick=${toggleMic}
            aria-label=${micMuted ? "Unmute mic" : "Mute mic"}
            title=${micMuted ? "Mic muted — tap to unmute" : "Mic hot — tap to mute"}
          >
            ${micMuted ? "✕" : "🎙"}
          </button>
          <input
            class="feva-input"
            type="text"
            placeholder="Or type a message…"
            value=${textValue}
            onInput=${(e) => setTextValue(e.target.value)}
            onKeyDown=${(e) => { if (e.key === "Enter") sendText(); }}
          />
          <button
            class="feva-send-btn"
            type="button"
            onClick=${sendText}
            disabled=${!textValue.trim()}
            aria-label="Send"
          >↑</button>
        </div>
      </div>
    </div>
  `;
}


function App() {
  // view ∈ {"landing", "call", "chat"}.
  //   landing → splash + LandingHero (or cockpit if revealAgent set)
  //   call    → voice-first session view (audio engine + orb + captions)
  //   chat    → text-first build chat (LandingChatView, no audio engine)
  const [view, setView] = useState("landing");
  // The typed prompt that initiated a chat session. Lives at App level
  // (not inside LandingChatView) so a switch-to-voice re-mount doesn't
  // lose what the operator already said.
  const [chatInitialText, setChatInitialText] = useState("");
  // Industry preset for the current build (from the landing dropdown or a
  // /for-<industry> deep-link). `landingIndustry` is the canonical
  // selection the LandingHero reflects (kept in sync with the URL so
  // back/forward + reload land on the right per-industry page).
  // `chatIndustry` snapshots it at build-start so the chat WS can thread
  // it to the server even after the operator navigates away.
  const [landingIndustry, setLandingIndustry] = useState(
    typeof location !== "undefined" ? industryFromSlug((location.pathname.match(/^\/for-([\w-]+)/) || [])[1]) : null
  );
  const [chatIndustry, setChatIndustry] = useState(null);
  // Voice-mode mirror of LandingChatView's pendingQuestion / questionError.
  // Populated by openSession's WS onmessage handler. Rendered as an
  // overlay <QuestionCard /> inside the call chrome so flipping from
  // chat → voice keeps the structured Q&A visible.
  const [callQuestion, setCallQuestion] = useState(null);
  const [callQuestionError, setCallQuestionError] = useState(null);
  const [blobMode, setBlobMode] = useState("idle");
  const [splashGone, setSplashGone] = useState(false);
  const [hint, setHint] = useState(null);
  const [callState, setCallState] = useState("idle");
  const [agent, setAgent] = useState(null);
  const [muted, setMuted] = useState(false);
  const [timer, setTimer] = useState(0);

  const [tweaks, setTweaks] = useState(loadTweaks);
  const [schema, setSchema] = useState(null);
  const [presets, setPresets] = useState(null);
  const [agents, setAgents] = useState([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [buildSnapshot, setBuildSnapshot] = useState(null);
  // Recovery-banner state. `recoverSid` is set when the landing screen
  // detects an orphaned eva_build_sid in sessionStorage (the build call
  // ended without an agent_saved event firing). The banner component
  // then checks the server for finalizability and offers a manual
  // commit button. Also re-checked after every call that closes
  // abnormally.
  const [recoverSid, setRecoverSid] = useState(null);
  // Track whether the in-flight session ever produced an `agent_saved`
  // event. Reset on every openSession. closeSession reads it to decide
  // whether to surface the recovery banner.
  const agentSavedRef = useRef(false);
  const [revealAgent, setRevealAgent] = useState(null);   // post-build reveal card
  const [revealSection, setRevealSection] = useState("overview");   // overview | calls | settings | numbers
  const [embedSlug, setEmbedSlug] = useState(null);                  // /embed/<slug> minimal iframe surface
  const [accountPage, setAccountPage] = useState(null);               // null | "billing" | "integrations"
  // Two-stage reveal: "unveal" plays a 3-second theatrical curtain, then
  // transitions to "cockpit" where the singular setup wizard lives. The
  // unveal is reserved for fresh-build moments (Eva just created the
  // agent) — re-entering a saved agent from the list or a deep link goes
  // straight to "cockpit" so the animation isn't a tax on every visit.
  const [revealStage, setRevealStage] = useState("cockpit");
  const [cockpitStats, setCockpitStats] = useState(null);
  // Plan info. Hard-coded for now (free / 300 min); will swap to an API.
  // `plan` is computed AFTER `user` is declared (see below) — the useMemo
  // would TDZ-error if placed up here. Search for `const plan = useMemo`.
  // Detected once at mount; used by the landing CTA, Go-live country pre-fill,
  // and (soon) as the initial locale hint to Eva when building a new agent.
  const [locale] = useState(() => detectLocale());
  // Theme — default light to match the builder/dashboard.
  const [theme, setTheme] = useState(() => loadTheme());
  const toggleTheme = useCallback(() => {
    setTheme((t) => {
      const next = t === "light" ? "dark" : "light";
      applyTheme(next);
      return next;
    });
  }, []);
  // Auth state — null = signed out, otherwise the user row from /api/me.
  // Source of truth is localStorage; we re-validate against /api/me on boot
  // so a server-side wipe doesn't leave a stale session sitting around.
  const [user, setUser] = useState(() => loadAuth());
  // Route override for /login and /signup — top-level so the shell can swap
  // the entire surface when not authed.
  const [authPage, setAuthPage] = useState(null);   // null | "login" | "signup"

  // Live plan state, derived from /api/me's `plan_state` pin (boot effect
  // fetches it; user-state refresh on plan change updates it). Falls back to
  // free defaults until /api/me lands so the topbar never reads "—".
  const plan = useMemo(() => {
    const ps = user?.plan_state;
    if (!ps) return { label: "Free", minutesLeft: 300, minutesTotal: 300, plan: { slug: "free", label: "Free" } };
    return {
      label: ps.plan?.label || "Free",
      minutesLeft: ps.minutes_left,
      minutesTotal: ps.minutes_total,
      // Pin the raw plan record so child components can gate features by
      // slug (e.g. Publish only works on paid plans). Without this they'd
      // have to fetch /api/me/plan again.
      plan: ps.plan || { slug: "free", label: "Free" },
    };
  }, [user?.plan_state]);
  // /agents full-page list. The old tweaks-drawer "Your agents" tab is gone;
  // this is its replacement — a real page with its own URL.
  const [agentsListOpen, setAgentsListOpen] = useState(false);
  const [goLiveAgent, setGoLiveAgent] = useState(null);   // open Twilio modal for this agent
  const [editAgentId, setEditAgentId] = useState(null);   // drawer opens this agent's editor

  // Boot: re-validate an EXISTING session only. Per PO direction, a fresh
  // visitor (no stored auth) stays logged-OUT — the build flow gates on
  // login so we get the 'not logged in → login → resume' experience. We no
  // longer silently auto-sign-in as the founder. (Dashboard API calls still
  // fall back to the founder server-side, so browsing isn't broken — only
  // the in-app `user` is null until they actually sign in.)
  useEffect(() => {
    if (isSignedOut()) return;            // honour explicit sign-out
    if (!loadAuth()) return;              // fresh visitor → logged out until sign-in
    fetch("/api/me").then((r) => r.ok ? r.json() : null).then((me) => {
      if (!me) return;
      // Refresh the cached user with fresh fields (org, plan_state).
      saveAuth(me);
      setUser(me);
    }).catch(() => {});
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // Mount-time recovery check. Covers the case where the operator
  // closed the tab mid-build and reopened it: sessionStorage retains
  // the eva_build_sid, but there's no in-memory state proving the
  // session ever ended cleanly. Hand the sid to <BuildRecovery /> and
  // let it decide (via /state) whether to show the banner or quietly
  // drop the stale id.
  useEffect(() => {
    try {
      const sid = sessionStorage.getItem("eva_build_sid");
      if (sid) setRecoverSid(sid);
    } catch {}
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const handleSignOut = useCallback(() => {
    clearAuth();
    setUser(null);
    setRevealAgent(null);
    setAgentsListOpen(false);
    setAuthPage("login");
    try { window.history.replaceState({}, "", "/login"); } catch {}
  }, []);

  // When a new agent enters the reveal, refresh stats so the cockpit's
  // call-count pill stays accurate. Stage is owned by the caller —
  // post-build paths opt into "unveal" explicitly; everything else
  // (list-page open, deep link, debug hook) lands directly in "cockpit".
  useEffect(() => {
    if (!revealAgent?.id) { setCockpitStats(null); return; }
    fetch(`/api/agents/${revealAgent.id}/stats`)
      .then((r) => r.json()).then(setCockpitStats).catch(() => {});
  }, [revealAgent?.id]);

  // Live captions — the latest user + agent line, each fading after its turn.
  const [userCaption, setUserCaption] = useState("");
  const [agentCaption, setAgentCaption] = useState("");
  // accumulators for the current turn; flushed on turn_complete
  const userSegRef = useRef("");
  const agentSegRef = useRef("");
  // fade-out timers, so captions linger then disappear
  const userFadeRef = useRef(null);
  const agentFadeRef = useRef(null);
  // Full conversation history — every completed turn, in order. Captions are
  // the "what's on screen right now" view; this is the "scroll back through
  // everything" view, openable via the chat-toggle button.
  const [transcript, setTranscript] = useState([]);   // [{role, text, ts}]
  const [chatOpen, setChatOpen] = useState(false);

  const engineRef = useRef(null);
  const wsRef = useRef(null);
  const stateRef = useRef({ view: "landing", muted: false });
  stateRef.current.view = view;
  stateRef.current.muted = muted;
  const callStartRef = useRef(0);
  // Forward-ref so /build deep-links and the splash CTA can trigger
  // openSession from places defined before it.
  const openSessionRef = useRef(null);

  // boot
  useEffect(() => {
    const t = setTimeout(() => setSplashGone(true), 1700);

    // Path-based routing — read location.pathname on mount, then again on
    // every popstate (browser back/forward). Patterns:
    //   /                  → landing splash (orb + hero + CTA)
    //   /agent/<slug>      → deep-link into a saved agent's cockpit; tap
    //                        the CTA there to start a test call
    //   /build             → kick off Eva directly, skipping the splash
    const applyRoute = (path) => {
      // /embed/<slug>           → minimal iframe-embed surface
      const eMatch = path.match(/^\/embed\/([\w-]+)/);
      if (eMatch) {
        setAgentsListOpen(false);
        setRevealAgent(null);
        setAuthPage(null);
        setEmbedSlug(eMatch[1]);
        return;
      }
      // /agent/<slug>           → overview page
      // /agent/<slug>/calls     → call log page
      // /agent/<slug>/settings  → knowledge & settings (Phase 4)
      // /agent/<slug>/numbers   → folded into /go-live; redirect transparently.
      setEmbedSlug(null);
      const m = path.match(/^\/agent\/([\w-]+)(?:\/(calls|outcomes|persona|small-talk|knowledge|guardrails|voice|test-call|go-live|numbers|settings|developer|profile|purpose|extra-info))?/);
      if (m) {
        setAgentsListOpen(false);
        // Legacy /numbers links + bookmarks land on Go live now. Soft replace
        // so the back-button still works sensibly.
        let section = m[2] || "overview";
        if (section === "numbers") {
          section = "go-live";
          try { window.history.replaceState({}, "", `/agent/${m[1]}/go-live`); } catch {}
        }
        setRevealSection(section);
        fetch(`/api/agents/by-slug/${encodeURIComponent(m[1])}`)
          .then((r) => r.ok ? r.json() : null)
          .then((a) => {
            if (a && a.id) {
              setRevealAgent(a);
            } else {
              flashHint("Agent not found.", 2500);
              window.history.replaceState({}, "", "/");
            }
          })
          .catch(() => {});
      } else if (path === "/login" || path === "/signup") {
        setAgentsListOpen(false);
        setRevealAgent(null);
        setAuthPage(path === "/signup" ? "signup" : "login");
        return;   // skip the default `setAuthPage(null)` below
      } else if (path === "/account/billing" || path === "/account/integrations" || path === "/account/org" || path === "/account/team") {
        // Account-scoped pages — no agent context.
        setAgentsListOpen(false);
        setRevealAgent(null);
        setAuthPage(null);
        setAccountPage(
          path === "/account/billing" ? "billing"
          : path === "/account/integrations" ? "integrations"
          : path === "/account/team" ? "team"
          : "org",
        );
        return;
      } else if (path.startsWith("/invite/")) {
        // Accept-invite landing — public, doesn't need auth to preview.
        const tok = path.slice("/invite/".length);
        setAgentsListOpen(false);
        setRevealAgent(null);
        setAuthPage(null);
        setAccountPage({ kind: "invite", token: tok });
        return;
      } else if (path === "/admin" || path.startsWith("/admin/")) {
        // Super-admin shell. UI gates on me.is_super_admin; the API gates
        // independently so a non-admin who guesses the URL gets the shell
        // but every fetch 403s — no data leak.
        const section = path.split("/")[2] || "summary";
        setAgentsListOpen(false);
        setRevealAgent(null);
        setAuthPage(null);
        setAccountPage({ kind: "admin", section });
        return;
      } else if (path === "/agents") {
        // Full-page agents list — closes any open cockpit/drawer.
        setRevealAgent(null);
        setAgentsListOpen(true);
        refreshAgents();
      } else if (path === "/build") {
        setAgentsListOpen(false);
        setTimeout(() => { openSessionRef.current && openSessionRef.current(); }, 50);
      } else if (/^\/for-[\w-]+/.test(path)) {
        // Per-industry landing page — /for-automobile, /for-dental, etc.
        // Resolve the slug to an industry id and reskin the homepage to
        // that industry. Unknown slugs resolve to null → plain landing.
        const slug = (path.match(/^\/for-([\w-]+)/) || [])[1];
        setAgentsListOpen(false);
        setRevealAgent(null);
        setLandingIndustry(industryFromSlug(slug));
      } else {
        // / or anything else → landing
        setLandingIndustry(null);
        setAgentsListOpen(false);
      }
      // Any path that didn't early-return clears the auth-page + account-page
      // overrides so we don't get stuck on /login or /account/* after navigating.
      setAuthPage(null);
      setAccountPage(null);
    };
    applyRoute(location.pathname);
    const onPop = () => applyRoute(location.pathname);
    window.addEventListener("popstate", onPop);

    // Debug URL-driven state for headless browser tests:
    //   ?reveal=<id>   → show that agent's reveal card
    //   ?golive=<id>   → open the Twilio go-live modal for that agent
    //   ?drawer=1      → open the Studio drawer
    //   ?drawer=<id>   → open drawer + editor for that agent
    const usp = new URLSearchParams(location.search);
    const debugReveal = usp.get("reveal");
    const debugGoLive = usp.get("golive");
    const debugDrawer = usp.get("drawer");
    if (debugReveal) {
      fetch(`/api/agents/${debugReveal}`).then((r) => r.json()).then((a) => {
        if (a && a.id) { setRevealStage("unveal"); setRevealAgent(a); }
      });
    }
    if (debugGoLive) {
      fetch(`/api/agents/${debugGoLive}`).then((r) => r.json()).then((a) => {
        if (a && a.id) setGoLiveAgent(a);
      });
    }
    if (debugDrawer) {
      setDrawerOpen(true);
      if (debugDrawer !== "1") setEditAgentId(parseInt(debugDrawer, 10));
    }

    // Lightweight test hooks so we can render the post-build reveal in
    // automated browser tests without needing a real mic / WS roundtrip.
    if (typeof window !== "undefined") {
      window.__sxAI = window.__sxAI || {};
      window.__sxAI.showReveal = (agent) => { setRevealStage("unveal"); setRevealAgent(agent); };
      window.__sxAI.showGoLive = (agent) => setGoLiveAgent(agent);
      window.__sxAI.openDrawer = () => setDrawerOpen(true);
      // Visual-only call view (no WS) for screenshot / preview purposes
      window.__sxAI.fakeCallView = (agent) => {
        setView("call");
        setBlobMode("listen");
        setCallState("connected");
        callStartRef.current = Date.now();
        if (agent) setAgent(agent);
      };
      window.__sxAI.fakeCaptions = (u, a) => {
        if (u !== undefined) setUserCaption(u);
        if (a !== undefined) setAgentCaption(a);
      };
      window.__sxAI.fakeTranscript = (turns) => {
        // Seed the conversation history for preview/screenshots.
        const now = Date.now();
        setTranscript(turns.map((t, i) => ({ ...t, ts: now - (turns.length - i) * 8000 })));
      };
      window.__sxAI.openChat = () => setChatOpen(true);
    }
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    fetch("/api/tweaks/schema").then((r) => r.json()).then(setSchema).catch(() => {});
    fetch("/api/presets").then((r) => r.json()).then(setPresets).catch(() => {});
    refreshAgents();
  }, []);

  const refreshAgents = useCallback(async () => {
    try { setAgents(await (await fetch("/api/agents")).json()); } catch {}
  }, []);

  // call timer
  useEffect(() => {
    if (view !== "call" || callState === "idle" || callState === "dialling") return;
    const id = setInterval(() => {
      setTimer(callStartRef.current ? (Date.now() - callStartRef.current) / 1000 : 0);
    }, 1000);
    return () => clearInterval(id);
  }, [view, callState]);

  const flashHint = useCallback((msg, ms = 2400) => {
    setHint(msg);
    if (msg) setTimeout(() => setHint((cur) => (cur === msg ? null : cur)), ms);
  }, []);

  // History helper — pushes the SPA into a new URL without reloading. Used
  // when we enter the cockpit (`/agent/<slug>`) or kick off Eva (`/build`).
  const goRoute = useCallback((path, replace = false) => {
    try {
      if (replace) window.history.replaceState({}, "", path);
      else window.history.pushState({}, "", path);
      // pushState/replaceState don't fire popstate, so the route handler
      // (bound to popstate) wouldn't run on in-app navigation. Dispatch a
      // synthetic event so applyRoute picks up the new path — this is what
      // makes "click an agent in the list" actually open its cockpit.
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch {}
  }, []);

  const closeSession = useCallback(async () => {
    setCallState("ending");
    setBlobMode("thinking");
    try { wsRef.current?.close(); } catch {}
    wsRef.current = null;
    try { await engineRef.current?.stop(); } catch {}
    engineRef.current = null;
    // Recovery check: if this session was a build (had a sid) AND the
    // server never emitted agent_saved, surface the BuildRecovery banner
    // so the operator can finalize manually. The banner queries the
    // backend to confirm there's enough data to save; if not, it
    // silently dismisses.
    try {
      const orphanSid = sessionStorage.getItem("eva_build_sid");
      if (orphanSid && !agentSavedRef.current) {
        setRecoverSid(orphanSid);
      }
    } catch {}
    setView("landing");
    setBlobMode("idle");
    setCallState("idle");
    setAgent(null);
    setMuted(false);
    setTimer(0);
    callStartRef.current = 0;
    setHint(null);
    setBuildSnapshot(null);
    setCallQuestion(null);
    setCallQuestionError(null);
    // Captions are call-scoped — clear them so the next call starts fresh.
    setUserCaption(""); setAgentCaption("");
    userSegRef.current = ""; agentSegRef.current = "";
    if (userFadeRef.current) { clearTimeout(userFadeRef.current); userFadeRef.current = null; }
    if (agentFadeRef.current) { clearTimeout(agentFadeRef.current); agentFadeRef.current = null; }
    setTranscript([]); setChatOpen(false);
    // Drop back to / when a session ends, unless we're showing a cockpit.
    if (location.pathname !== "/" && !revealAgent) goRoute("/");
    refreshAgents();
  }, [refreshAgents, revealAgent, goRoute]);

  // Ambience helper — pick the right loop for this agent and start it on
  // the live audio engine. Per-sector defaults make a SaaS support agent
  // sound like an office and a hotel concierge sound like a quiet lobby.
  // The user can override on the Voice settings page.
  const startAmbienceFor = useCallback((agent) => {
    if (!agent || !engineRef.current) return;
    const tweaks = agent.voice_tweaks || {};
    const SECTOR_AMBIENCE = {
      saas_support: "office",
      banking: "office",
      insurance: "office",
      education: "office",
      legal: "quiet",
      real_estate: "office",
      retail: "cafe",
      restaurant: "cafe",
      events: "cafe",
      travel: "cafe",
      healthcare: "clinic",
      dental: "clinic",
      automotive: "workshop",
      logistics: "workshop",
      generic: "office",
    };
    // Explicit user choice wins; sector default is the safety net; "off"
    // disables. Volume defaults to 18%, conservative for laptop speakers.
    const choice = tweaks.ambience ?? SECTOR_AMBIENCE[agent.sector] ?? "off";
    const vol = typeof tweaks.ambience_volume === "number" ? tweaks.ambience_volume : 0.18;
    if (!choice || choice === "off") {
      engineRef.current.setAmbience(null);
      return;
    }
    engineRef.current.setAmbience(`/static/voice-samples/ambience/${choice}.wav`, vol);
  }, []);

  const openSession = useCallback(async (testAgentId, opts) => {
    // opts = { initialText?: string, startMuted?: boolean }
    //   initialText  — a typed message to send as the FIRST user turn
    //                  once the WS is ready. Used by the landing page's
    //                  prompt composer to skip "Eva says her opener →
    //                  user has to talk" and jump straight to the
    //                  substantive conversation.
    //   startMuted   — start with the mic muted, so a text-first build
    //                  doesn't accidentally pick up ambient room noise.
    //                  Operator can unmute from the call chrome.
    const initialText = (opts && opts.initialText) || "";
    const startMuted  = !!(opts && opts.startMuted);
    const presetIndustry = (opts && opts.industry) || null;
    setView("call");
    // Reset the "did this session commit a new agent?" flag. closeSession
    // reads it to decide whether to surface the recovery banner.
    agentSavedRef.current = false;
    // Hide any leftover recovery banner — the operator is starting a new
    // session intentionally.
    setRecoverSid(null);
    // Pre-connection: particles WANDER in their idle Lissajous orbits with a
    // warm amber tint — visually says "ringing / gathering". The wave-line
    // snap only kicks in when we transition to listen / speak / thinking
    // after the `ready` event arrives.
    setBlobMode("calling");
    setCallState("dialling");
    setAgent(null);
    // Text-first builds open muted so the operator's typed prompt is
    // the only input the model sees on turn 1 — no chance of an
    // accidentally-captured "uhh" or background noise getting mixed
    // in. The mute toggle in the call chrome flips it on when they
    // want to speak.
    setMuted(startMuted);
    setTimer(0);
    setHint(null);
    setBuildSnapshot(null);
    // Fresh call session = fresh template-question state. Carry-over
    // from a previous build would render a stale card.
    setCallQuestion(null);
    setCallQuestionError(null);
    // Test-mode invocations clear any stale reveal card.
    if (testAgentId) setRevealAgent(null);
    // URL reflects intent — `/build` for Eva-fresh, `/agent/<slug>` is set
    // separately by the cockpit-open / agent-loaded events.
    if (!testAgentId && location.pathname !== "/build") goRoute("/build");

    const engine = new AudioEngine();
    engineRef.current = engine;
    try {
      await engine.start({
        onMicChunk: (buf) => {
          const ws = wsRef.current;
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(buf);
        },
      });
    } catch (e) {
      // Surface a precise reason instead of always blaming the mic. The
      // pre-WS failure surface includes: getUserMedia denied (NotAllowedError),
      // mic in use elsewhere (NotReadableError), no input device
      // (NotFoundError), AudioContext locked by autoplay policy, or the
      // worklet module failing to load. Each gets a different one-line hint;
      // the original Error is also logged so DevTools shows the stack.
      console.error("[openSession] engine.start() failed:", e);
      const name = e?.name || "";
      const msg = String(e?.message || e || "");
      let hint;
      if (name === "NotAllowedError" || /denied|permission/i.test(msg)) {
        hint = "Mic permission was denied. Allow it in your browser's URL bar to try again.";
      } else if (name === "NotFoundError" || /no.*microphone|no.*device/i.test(msg)) {
        hint = "No microphone found. Plug one in and try again.";
      } else if (name === "NotReadableError" || /in use|busy/i.test(msg)) {
        hint = "Mic is in use by another app. Close the other app and try again.";
      } else if (/audioworklet|worklet|AbortError/i.test(msg)) {
        hint = "Audio engine didn't load — refresh once and try again.";
      } else if (/autoplay|user gesture/i.test(msg)) {
        hint = "Browser blocked audio start — tap the orb once more.";
      } else {
        hint = msg.slice(0, 80) || "Couldn't start — refresh and try again.";
      }
      flashHint(hint, 4000);
      setBlobMode("error");
      // Shorter dismissal so the user can re-tap without waiting.
      setTimeout(closeSession, 600);
      return;
    }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const qsObj = {
      locale: navigator.language || "en-US",
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      ...tweaksQuery(tweaks),
    };
    if (testAgentId) qsObj.agent_id = String(testAgentId);
    // Industry preset from the landing page (voice path) — server locks
    // that industry's template so the voice build skips triage too.
    if (presetIndustry && !testAgentId) qsObj.industry = String(presetIndustry);
    // Stamp the WS so any agent Eva builds in this session inherits the
    // current user's id. Server falls back to founder if missing.
    const _uid = currentUserId();
    if (_uid) qsObj.user_id = String(_uid);
    // Stable build session id: one UUID per builder build, persisted in
    // sessionStorage so a WS-level reconnect within the same tab reuses
    // the same row in the server's build_sessions table — meaning Eva
    // re-loads the captured facts (sector, business_name, etc.) and
    // CAN'T re-ask them. Cleared on agent_saved so the next build gets
    // a fresh sid. Test-mode connections (testAgentId set) don't need
    // a sid — they're not in builder kind.
    if (!testAgentId) {
      let buildSid = null;
      try { buildSid = sessionStorage.getItem("eva_build_sid"); } catch {}
      if (!buildSid) {
        // crypto.randomUUID is standard in all browsers we target, but
        // fall back to a Math.random-based UUIDv4 if it's missing (e.g.
        // an old in-app webview). Either way the sid is opaque to the
        // server; only collision-resistance matters.
        buildSid = (crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          : "fb-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
        try { sessionStorage.setItem("eva_build_sid", buildSid); } catch {}
      }
      qsObj.sid = buildSid;
    }
    const qs = new URLSearchParams(qsObj).toString();
    const ws = new WebSocket(`${proto}//${location.host}/ws/session?${qs}`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onerror = (e) => {
      console.error("[openSession] WebSocket error:", e);
      setBlobMode("error");
      flashHint("Couldn't connect — check your network and try again.", 3000);
    };
    ws.onclose = (e) => {
      // Distinguish a clean handoff close (code 1000 / 1005) from a
      // pre-ready failure (anything else). The pre-ready failures
      // surface as a hint; clean closes just unwind quietly.
      if (engineRef.current) {
        if (e && e.code && e.code !== 1000 && e.code !== 1005 && !callStartRef.current) {
          console.error("[openSession] WS closed before ready:", e.code, e.reason);
          flashHint(
            e.reason
              ? `Connection dropped: ${e.reason.slice(0, 70)}`
              : "Connection dropped before Eva picked up. Try again in a moment.",
            3500,
          );
        }
        closeSession();
      }
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "ready") {
          setBlobMode("listen");
          setCallState("connected");
          callStartRef.current = Date.now();
          setHint(null);
          // Text-first entry from the landing composer: ship the typed
          // prompt as the first user turn. The server's gemini_bridge
          // already handles {type:"text", text:…} during a live session
          // (same code path TypeRail uses mid-call). Eva responds to
          // this as if the operator had spoken it — the rest of the
          // build flow proceeds unchanged.
          if (initialText) {
            try { ws.send(JSON.stringify({ type: "text", text: initialText })); }
            catch (e) { console.warn("[openSession] couldn't send initialText:", e); }
          }
        } else if (msg.type === "session_starting") {
          if (msg.agent) setAgent(msg.agent);
          // Kick off the ambience layer once we know which agent is on the
          // line. The chosen bed is stored on agent.voice_tweaks.ambience;
          // we default to the sector's recommended bed if unset.
          startAmbienceFor(msg.agent);
        } else if (msg.type === "template_question") {
          // Server pushed a new template question. Mirror it into call-view
          // state so the QuestionCard overlay updates with progress meter +
          // chips, exactly like in the chat view.
          if (msg.question) {
            setCallQuestion(msg.question);
            setCallQuestionError(null);
          }
        } else if (msg.type === "template_question_error") {
          // Last answer (typed via mic or chip) failed validation —
          // attach to the current card so the operator gets a clear
          // "didn't work, try again" cue instead of wondering.
          setCallQuestionError({
            question_id: msg.question_id,
            error: msg.error,
            retry_prompt: msg.retry_prompt,
          });
        } else if (msg.type === "template_complete") {
          // Interview's done — clear the card so the wrap-up beat owns
          // the screen unobstructed.
          setCallQuestion(null);
          setCallQuestionError(null);
        } else if (msg.type === "agent_loaded" || msg.type === "agent_saved") {
          setAgent(msg.agent);
          startAmbienceFor(msg.agent);
          if (msg.type === "agent_saved") {
            // Mark this session as saved so the recovery-banner logic
            // at closeSession knows NOT to nag the operator.
            agentSavedRef.current = true;
            // Build committed server-side. Drop the build sid so a
            // follow-up build opens a fresh build_sessions row instead
            // of resurrecting facts from this (now-committed) one.
            try { sessionStorage.removeItem("eva_build_sid"); } catch {}
            setBuildSnapshot({
              name: msg.agent.name,
              sector: msg.agent.sector,
              locale: msg.agent.locale,
              voice: msg.agent.voice,
              greeting: msg.agent.greeting,
            });
            // CRITICAL: lock the reveal state SYNCHRONOUSLY from msg.agent
            // BEFORE the async /api/agents fetch. msg.agent already carries
            // id + name + slug + sector + locale from the server (it's the
            // dict create_agent returned). If we waited for the by-id
            // fetch to resolve, there'd be a 200-500ms window where
            // build_complete arrives → view flips to "landing" → revealAgent
            // is still null → the LandingHero renders with its "Open your
            // agents" CTA. A quick operator who clicks it during that flash
            // gets routed to /agents (the LIST), not the new agent's
            // dashboard. That was the "popup that doesn't go to the agent
            // I just built" bug.
            //
            // Sequence locked here:
            //   1. setRevealStage("unveal")  — UI knows what to render
            //   2. setRevealAgent(msg.agent) — has slug, enough to render
            //   3. goRoute(/agent/<slug>)    — URL matches, applyRoute fires
            //   4. fetch upgrades revealAgent to the FULL dict (with
            //      connectors / persona / etc.) once it returns. The full
            //      dict and msg.agent share the same id+slug so no second
            //      navigation is needed.
            setRevealStage("unveal");
            setRevealAgent(msg.agent);
            if (msg.agent?.slug) goRoute(`/agent/${msg.agent.slug}`);
            else if (msg.agent?.id) goRoute(`/agent/${msg.agent.id}`);
            // Fetch the full agent (with connectors / persona / etc.) and
            // upgrade revealAgent in place. The reveal card was already
            // queued above so this is purely additive — no race, no flash.
            fetch(`/api/agents/${msg.agent.id}`)
              .then((r) => r.ok ? r.json() : null)
              .then((full) => {
                if (full && full.id) setRevealAgent(full);
              })
              .catch(() => { /* msg.agent is already in place — no rollback needed */ });
            refreshAgents();
          }
        } else if (msg.type === "build_complete") {
          // Builder session is done — gracefully exit call view, the reveal
          // card will already be queued and will take over the screen.
          closeSession();
        } else if (msg.type === "transferring") {
          setBlobMode("thinking");
          setCallState("connected");
          engineRef.current?.flushPlayback();
          flashHint("Putting you through…", 1600);
        } else if (msg.type === "reconnected") {
          setCallState("connected");
          setBlobMode("listen");
        } else if (msg.type === "interrupted") {
          // We configured Gemini with activity_handling=NO_INTERRUPTION on
          // the server, which means the model is supposed to finish its turn
          // before yielding. But the SDK sometimes still emits `interrupted`
          // when its VAD merely *detects* user audio (without acting on it).
          // If we flush playback here, Eva's voice cuts mid-sentence even
          // though the model keeps generating. That's the bug users report
          // ("audio stops after first sentence, but the chat shows the full
          //  text"). So: do NOT flush playback on this event anymore. Just
          // log and let Eva finish; real barge-in is handled client-side
          // by the audio engine's tightened threshold + grace window.
          console.debug("[ws] interrupted event received — ignoring (server NO_INTERRUPTION)");
        } else if (msg.type === "transcript") {
          // Live captions — accumulate per turn, render as it streams so the
          // viewer sees words as they're spoken. We display BOTH roles so the
          // user can verify what the mic captured (a huge debug aid) and
          // follow along with what the agent says.
          if (msg.role === "user") {
            userSegRef.current += msg.text || "";
            setUserCaption(userSegRef.current);
            if (userFadeRef.current) { clearTimeout(userFadeRef.current); userFadeRef.current = null; }
          } else if (msg.role === "model") {
            agentSegRef.current += msg.text || "";
            setAgentCaption(agentSegRef.current);
            if (agentFadeRef.current) { clearTimeout(agentFadeRef.current); agentFadeRef.current = null; }
          }
        } else if (msg.type === "turn_complete") {
          setBlobMode("listen");
          // Snapshot the segments — we need them both for the transcript
          // log AND for the fade timer (which uses .current).
          const finalUser = userSegRef.current.trim();
          const finalAgent = agentSegRef.current.trim();
          // Persist the completed turn(s) into the full conversation history.
          // We push BOTH roles if both have content this turn, in order, with
          // the same wall-clock ts so they group naturally in the panel.
          if (finalUser || finalAgent) {
            const now = Date.now();
            setTranscript((prev) => {
              const next = prev.slice();
              if (finalUser) next.push({ role: "user", text: finalUser, ts: now });
              if (finalAgent) next.push({ role: "model", text: finalAgent, ts: now });
              return next;
            });
          }
          // Whichever side just finished — start its fade. Captions linger
          // ~6s so a viewer can read them, then disappear so the screen
          // breathes between exchanges.
          if (finalUser) {
            userFadeRef.current = setTimeout(() => { setUserCaption(""); }, 6000);
          }
          if (finalAgent) {
            agentFadeRef.current = setTimeout(() => { setAgentCaption(""); }, 6000);
          }
          // Reset accumulators so the next turn starts clean (the displayed
          // text persists until the fade timer fires).
          userSegRef.current = "";
          agentSegRef.current = "";
        } else if (msg.type === "error") {
          setBlobMode("error");
          flashHint(msg.message?.slice(0, 80) || "Something went wrong.", 3000);
        } else if (msg.type === "go_away") {
          setCallState("reconnecting");
        }
      } else {
        engineRef.current?.playPcm(ev.data);
        setBlobMode("speak");
      }
    };
  }, [closeSession, flashHint, tweaks, goRoute]);

  // Keep the forward-ref pointing at the latest openSession so /build
  // deep-links and the splash CTA can invoke it.
  useEffect(() => { openSessionRef.current = openSession; }, [openSession]);

  // ── Shared post-build reveal (used by wizard, chat, and voice) ──
  // Locks the reveal SYNCHRONOUSLY from the just-saved agent (avoids the
  // brief LandingHero flash the agent_saved race used to cause), then
  // upgrades the dict via the by-id fetch.
  const revealSavedAgent = useCallback((agentPayload) => {
    if (!agentPayload) return;
    agentSavedRef.current = true;
    try { sessionStorage.removeItem("eva_build_sid"); } catch {}
    setView("landing");
    setRevealStage("unveal");
    setRevealAgent(agentPayload);
    if (agentPayload.slug) goRoute(`/agent/${agentPayload.slug}`);
    else if (agentPayload.id) goRoute(`/agent/${agentPayload.id}`);
    if (agentPayload.id) {
      fetch(`/api/agents/${agentPayload.id}`)
        .then((r) => r.ok ? r.json() : null)
        .then((full) => { if (full && full.id) setRevealAgent(full); })
        .catch(() => {});
    }
    refreshAgents && refreshAgents();
  }, [goRoute, refreshAgents]);

  // ── Central build dispatcher with the login gate ──
  // PO direction: the WIZARD is the default build surface; chat & voice are
  // alternates. Starting any build requires login — if the visitor isn't
  // signed in we stash their intent, send them to the login screen, and
  // resume the exact same build after auth (see AuthPage onAuthed).
  //   opts = { mode?: "wizard"|"chat"|"voice", initialText?, industry?, voice? }
  const startBuild = useCallback((opts = {}) => {
    const text = opts.initialText ? String(opts.initialText).trim() : "";
    const ind = opts.industry || null;
    const mode = opts.voice ? "voice" : (opts.mode || "wizard");
    // Auth gate.
    if (!loadAuth()) {
      try {
        sessionStorage.setItem("sxai.pending_build", JSON.stringify({ mode, initialText: text, industry: ind }));
      } catch {}
      setAuthPage("login");
      try { window.history.pushState({}, "", "/login"); } catch {}
      return;
    }
    // Dispatch to the chosen surface.
    setChatInitialText(text);
    setChatIndustry(ind);
    if (mode === "voice") {
      const o = { industry: ind };
      if (text) { o.initialText = text; o.startMuted = true; }
      openSession(undefined, o);
    } else if (mode === "chat") {
      setView("chat");
    } else {
      setView("wizard");
    }
  }, [openSession]);

  // After a successful sign-in, resume any build the visitor started before
  // the gate bounced them to login.
  const resumePendingBuild = useCallback(() => {
    let intent = null;
    try {
      const raw = sessionStorage.getItem("sxai.pending_build");
      if (raw) intent = JSON.parse(raw);
      sessionStorage.removeItem("sxai.pending_build");
    } catch {}
    if (intent && typeof intent === "object") {
      startBuild(intent);
      return true;
    }
    return false;
  }, [startBuild]);

  // Wizard → chat/voice handoff. Persists the wizard's current answers onto
  // the shared build_session (same sid the chat/voice WS will use) so Eva
  // resumes the SAME build and never re-asks anything already filled, then
  // flips to the chosen surface.
  const syncWizardThenSwitch = useCallback(async (answers, mode, meta) => {
    const loc = (locale && locale.bcp47) || navigator.language || "en-IN";
    if (meta && meta.dynamic) {
      // Catch-all builds have no static template for the chat to resume, so
      // we fold the use case + everything filled into ONE opening message —
      // Eva gets the full picture up front and won't re-ask it.
      const parts = [];
      if (meta.useCase) parts.push(meta.useCase.trim());
      if (meta.summary) parts.push("Details so far:\n" + meta.summary);
      const combined = parts.join("\n\n");
      setChatInitialText(combined);
      if (mode === "voice") {
        setView("landing");
        setTimeout(() => openSession(undefined, combined ? { initialText: combined, startMuted: true } : {}), 0);
      } else {
        setView("chat");
      }
      return;
    }
    const sid = ensureBuildSid();
    try {
      await fetch("/api/build/wizard/sync", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid, industry: chatIndustry || "", locale: loc, answers: answers || {} }),
      });
    } catch (e) { console.warn("[wizard] sync failed:", e); }
    if (mode === "voice") {
      setView("landing");
      setTimeout(() => openSession(undefined, { industry: chatIndustry }), 0);
    } else {
      setView("chat");
    }
  }, [chatIndustry, locale, openSession]);

  // press — long-press is only meaningful in a call (hang-up gesture). On
  // the landing the blob acts like a regular button: any tap, no matter how
  // long, kicks off a session. Earlier we returned early when `held` was
  // true, which made trackpad / hesitant clicks feel broken.
  const pressRef = useRef({ longTimer: null, held: false });
  const onPressStart = useCallback(() => {
    pressRef.current.held = false;
    pressRef.current.longTimer = setTimeout(() => {
      pressRef.current.held = true;
      if (stateRef.current.view === "call") closeSession();
    }, LONG_PRESS_MS);
  }, [closeSession]);
  const onPressEnd = useCallback(() => {
    clearTimeout(pressRef.current.longTimer);
    if (stateRef.current.view === "landing") {
      // Any release on landing → start session, regardless of hold duration.
      pressRef.current.held = false;
      openSession();
      return;
    }
    if (pressRef.current.held) return;   // long-press already triggered closeSession
    const next = !stateRef.current.muted;
    setMuted(next);
    engineRef.current?.setMuted(next);
    flashHint(next ? "Mic muted" : "Mic on", 1400);
  }, [openSession, flashHint]);
  const onPressCancel = useCallback(() => {
    clearTimeout(pressRef.current.longTimer);
    pressRef.current.held = false;
  }, []);

  const toggleMute = useCallback(() => {
    const next = !muted;
    setMuted(next);
    engineRef.current?.setMuted(next);
    flashHint(next ? "Mic muted" : "Mic on", 1400);
  }, [muted, flashHint]);

  // Agent delete uses a typed-name confirm modal — the agent's calls,
  // config, and transcripts go too. Audit F.3: doctrine bans "Are you
  // sure?" modals; the typed-name pattern is a respected exception
  // because it prevents tragedy rather than gating intent.
  const [deleteAgent, setDeleteAgent] = useState(null);
  const onDelete = useCallback(async (id) => {
    const a = agents.find((x) => x.id === id);
    if (!a) return;
    setDeleteAgent(a);
  }, [agents]);
  const confirmDelete = useCallback(async () => {
    if (!deleteAgent) return;
    const id = deleteAgent.id;
    const r = await fetch(`/api/agents/${id}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`Failed (${r.status})`);
    setDeleteAgent(null);
    refreshAgents();
  }, [deleteAgent, refreshAgents]);

  const onTest = useCallback(async (id) => {
    setDrawerOpen(false);
    setRevealAgent(null);
    // Set the URL to the agent's slug so the call view has a meaningful URL.
    try {
      const a = await (await fetch(`/api/agents/${id}`)).json();
      if (a && a.slug) goRoute(`/agent/${a.slug}`);
    } catch {}
    openSession(id);   // start the WS directly in test mode for this agent
  }, [openSession, goRoute]);

  // The blob is the protagonist on every screen, so its physical footprint
  // shouldn't jump when a call starts. We keep the diameter near-identical
  // between landing and call (just a hair larger in call, to honour the
  // emphasis without being a startle). Audio-driven scale inside the blob
  // already gives a real sense of "coming alive". The viewport size is held
  // in state so we react to resize and to the post-mount layout (some headless
  // browsers report 0×0 at React mount time, only settling on the real
  // dimensions a beat later).
  const [vp, setVp] = useState(() => ({
    w: typeof window !== "undefined" ? window.innerWidth : 1024,
    h: typeof window !== "undefined" ? window.innerHeight : 768,
  }));
  useEffect(() => {
    const update = () => setVp({ w: window.innerWidth, h: window.innerHeight });
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  const blobSize = useMemo(() => {
    const v = Math.min(vp.w, vp.h);
    if (v < 100) return 320; // guard against degenerate viewports
    // Landing: blob sits in the lower-middle so the hero copy at the top
    //   reads cleanly without ever overlapping. Capped at 260 and biased by
    //   38% of the smaller viewport edge so it scales but never crowds out
    //   the headline. Call: blob can swell larger since the hero is gone.
    return view === "call" ? Math.min(480, v * 0.58) : Math.min(320, v * 0.30);
  }, [view, vp.w, vp.h]);

  const stateLabel =
    callState === "dialling" ? "calling…"
    : callState === "reconnecting" ? "reconnecting…"
    : callState === "ending" ? "ending"
    : callState === "connected" ? "on the line"
    : "";

  const pulseClass =
    callState === "dialling" || callState === "reconnecting" ? "calling"
    : callState === "ending" ? "ending" : "";

  // Pre-render gate: /embed/<slug> takes over the whole surface — no
  // brandbar, no landing chrome, just the orb + CTA. Loaded inside the
  // floating iframe that /static/embed.js injects on third-party sites.
  if (embedSlug) {
    return html`
      ${view === "call" ? html`
        <div class="stage" data-view=${view}>
          <button
            class="blob-tap"
            onPointerDown=${onPressStart}
            onPointerUp=${onPressEnd}
            onPointerLeave=${onPressCancel}
            onPointerCancel=${onPressCancel}
            aria-label="Tap to mute, hold to end call"
          >
            <${VoiceBlob} engineRef=${engineRef} mode=${blobMode} size=${blobSize} />
          </button>
        </div>
      ` : html`
        <${EmbedView}
          slug=${embedSlug}
          blobSize=${blobSize}
          blobMode=${blobMode}
          engineRef=${engineRef}
          onPressStart=${onPressStart}
          onPressEnd=${onPressEnd}
          onPressCancel=${onPressCancel}
          onStart=${(id) => openSession(id)}
        />
      `}
    `;
  }

  // Pre-render gate: account-scoped pages (billing / integrations).
  if (accountPage === "billing") {
    return html`
      <${BillingPage}
        agents=${agents}
        plan=${plan}
        onNav=${(r) => goRoute(r)}
        onPlanChanged=${(state) => {
          // Refresh /api/me so the topbar minutes counter updates immediately.
          fetch("/api/me").then((r) => r.json()).then(setUser).catch(() => {});
        }}
      />
    `;
  }
  if (accountPage === "integrations") {
    return html`
      <${IntegrationsPage}
        agents=${agents}
        plan=${plan}
        presets=${presets}
        onNav=${(r) => goRoute(r)}
        org=${user?.org || null}
      />
    `;
  }
  if (accountPage === "org") {
    return html`
      <${AccountOrgPage}
        agents=${agents}
        plan=${plan}
        onNav=${(r) => goRoute(r)}
        org=${user?.org || null}
        onOrgChanged=${(o) => setUser((u) => ({ ...(u || {}), org: o }))}
      />
    `;
  }
  if (accountPage === "team") {
    return html`
      <${TeamPage}
        agents=${agents}
        plan=${plan}
        onNav=${(r) => goRoute(r)}
        org=${user?.org || null}
        currentUser=${user || null}
      />
    `;
  }
  if (accountPage && accountPage.kind === "invite") {
    return html`
      <${AcceptInvitePage}
        token=${accountPage.token}
        currentUser=${user || null}
        onAccepted=${async () => {
          // After accept, refresh /api/me + agents list to pick up new org membership.
          try {
            const me = await fetch("/api/me").then((r) => r.json());
            setUser(me);
          } catch {}
          goRoute("/agents");
        }}
      />
    `;
  }
  if (accountPage && accountPage.kind === "admin") {
    return html`
      <${AdminShell}
        section=${accountPage.section}
        currentUser=${user || null}
        onNav=${(r) => goRoute(r)}
      />
    `;
  }

  // Pre-render gate: auth pages take over the whole surface.
  if (authPage) {
    return html`
      <${AuthPage}
        mode=${authPage}
        defaults=${{ email: "dipesh.majumder@webspiders.com", name: "Dipesh" }}
        onAuthed=${async (u) => {
          setUser(u);
          setAuthPage(null);
          // If the visitor was mid-build when the login gate bounced them,
          // resume that exact build (wizard / chat / voice) now.
          if (location.pathname.startsWith("/login") || location.pathname.startsWith("/signup")) {
            try { window.history.replaceState({}, "", "/"); } catch {}
          }
          if (resumePendingBuild()) return;
          // Otherwise route by agent count: fresh accounts skip the empty
          // /agents page and land in the build flow; returning users go to
          // their list.
          try {
            const r = await fetch("/api/agents");
            const arr = r.ok ? await r.json() : [];
            goRoute(Array.isArray(arr) && arr.length > 0 ? "/agents" : "/");
          } catch {
            goRoute("/agents");
          }
        }}
        onSwitch=${(next) => goRoute(next === "signup" ? "/signup" : "/login")}
      />
    `;
  }

  return html`
    <div>
      <div class=${"brandsplash " + (splashGone ? "gone" : "")}>
        <img class="brandmark" src="/static/assets/spiderx-logo.svg" alt="SpiderX AI" />
      </div>

      <!-- .stage is the floating-orb layer. It's only used in CALL view —
           the landing renders the orb inline inside <LandingHero> so it
           can't crash into the hero or CTA. -->
      ${view === "call" ? html`
        <div class="stage" data-view=${view}>
          <button
            class="blob-tap"
            onPointerDown=${onPressStart}
            onPointerUp=${onPressEnd}
            onPointerLeave=${onPressCancel}
            onPointerCancel=${onPressCancel}
            aria-label="Tap to mute, hold to end call"
          >
            <${VoiceBlob} engineRef=${engineRef} mode=${blobMode} size=${blobSize} />
          </button>
        </div>
      ` : ""}

      ${splashGone && view === "landing" && !revealAgent && !agentsListOpen ? html`
        <${LandingArt} />
        <${CookieNotice} locale=${locale} />
      ` : ""}

      ${splashGone ? html`
        <a class="brandbar" href="/" aria-label="SpiderX.AI · home">
          <${SpiderXLogo} height=${20} />
        </a>
        <div class="landing-theme">
          <${ThemeToggle} theme=${theme} onToggle=${toggleTheme} />
        </div>
      ` : ""}

      ${deleteAgent ? html`
        <${DestructiveConfirmModal}
          title="Delete this agent?"
          body=${html`
            <strong>${deleteAgent.name}</strong> and everything tied to her go too —
            saved config, every call's transcript, the call log, the analytics rollups.
            This can't be undone.
          `}
          typedName=${deleteAgent.name}
          confirmLabel="Delete ${deleteAgent.name}"
          onClose=${() => setDeleteAgent(null)}
          onConfirm=${confirmDelete}
        />
      ` : ""}

      <!--
        Apple-styled landing — hero copy, large headline, primary CTA button
        that kicks off Eva. The orb is the visual centerpiece + a secondary
        tap target; the button is the obvious affordance.
      -->
      ${splashGone && view === "landing" && !revealAgent && !agentsListOpen ? html`
        <${LandingHero}
          agents=${agents}
          locale=${locale}
          onBuild=${(opts) => {
            // PO direction: prompt box → WIZARD by default (chat & voice are
            // alternates offered inside the wizard header / the mic button).
            // startBuild applies the login gate first: a logged-out visitor
            // is sent to sign-in, then resumed into the same surface.
            // "Talk instead" passes voice:true → voice flow.
            startBuild(opts);
          }}
          onOpenAgents=${() => goRoute("/agents")}
          initialIndustry=${landingIndustry}
          onIndustryChange=${(id) => {
            // Reflect the dropdown choice into App-level state + the URL
            // so it behaves like a real per-industry landing page.
            setLandingIndustry(id);
            const p = industryToPath(id);
            try { window.history.pushState({}, "", p); } catch {}
          }}
          blobSize=${blobSize}
          blobMode=${blobMode}
          engineRef=${engineRef}
          onPressStart=${onPressStart}
          onPressEnd=${onPressEnd}
          onPressCancel=${onPressCancel}
        />
      ` : ""}

      <!--
        Text-first chat surface. Owns its own WS (no audio engine) and
        renders Eva's responses as streaming chat bubbles. Mounts when
        the operator submits a prompt on the landing composer; unmounts
        on close / agent_saved / switch-to-voice.
      -->
      ${splashGone && view === "chat" && !revealAgent ? html`
        <${LandingArt} />
        <${LandingChatView}
          initialText=${chatInitialText}
          industry=${chatIndustry}
          refreshAgents=${refreshAgents}
          onClose=${() => {
            // Operator hit the × — drop back to landing.
            setView("landing");
            setChatInitialText("");
            if (location.pathname !== "/") goRoute("/");
          }}
          onSwitchToVoice=${() => {
            // Flip to voice mode. The build_session row + sid in
            // sessionStorage are shared, so the voice session resumes
            // with all the facts the chat already captured. Carry the
            // industry preset across so the resumed voice session keeps
            // the same locked template.
            setView("landing");                        // unmount the chat view
            setTimeout(() => openSession(undefined, { industry: chatIndustry }), 0);   // open voice
          }}
          onAgentSaved=${(agentPayload) => revealSavedAgent(agentPayload)}
        />
      ` : ""}

      <!--
        Wizard — the DEFAULT build surface. A deterministic multi-step form
        driven by the industry template's question list. Chat & voice are
        offered as alternates in its header.
      -->
      ${splashGone && view === "wizard" && !revealAgent ? html`
        <${LandingArt} />
        <${WizardView}
          industry=${chatIndustry}
          locale=${locale?.bcp47 || navigator.language || "en-IN"}
          initialText=${chatInitialText}
          presets=${presets}
          onClose=${() => {
            setView("landing");
            setChatInitialText("");
            if (location.pathname !== "/") goRoute("/");
          }}
          onSwitchToChat=${(answers, meta) => {
            // Seamless handoff: persist what the operator filled so chat
            // (Eva) resumes the same build and never re-asks answered Qs.
            syncWizardThenSwitch(answers, "chat", meta);
          }}
          onSwitchToVoice=${(answers, meta) => {
            syncWizardThenSwitch(answers, "voice", meta);
          }}
          onAgentSaved=${(agentPayload) => revealSavedAgent(agentPayload)}
        />
      ` : ""}

      <!-- Removed: legacy .tweaks-toggle ("dots → /agents") was pinned to the
           same top-right corner as the theme toggle with z-index 7 (higher than
           the landing-theme wrap's z-index 5), so every click on the moon
           icon got eaten by it. The primary "Open your agents" CTA + the
           top-bar agent selector now cover that affordance. -->

      ${view === "call"
        ? html`
            <div class="callchrome-top">
              <span class=${"pulse " + pulseClass}></span>
              <span class="who">${agent ? agent.name : "Eva"}</span>
              <span class="state">${stateLabel}</span>
              ${callState === "connected" ? html`<span class="timer">${fmtTimer(timer)}</span>` : ""}
            </div>
            <${CaptionRail}
              userLine=${userCaption}
              agentLine=${agentCaption}
              agentName=${agent ? agent.name : "Eva"}
              transcriptLen=${transcript.length}
              onOpenChat=${() => setChatOpen(true)}
            />
            <${ChatPanel}
              open=${chatOpen}
              transcript=${transcript}
              agentName=${agent ? agent.name : "Eva"}
              onClose=${() => setChatOpen(false)}
            />
            <!-- Structured question card overlay during a build call.
                 Same component the chat view uses; positioned above
                 the TypeRail so the chips + progress meter are in
                 the operator's eye-line while the orb / captions
                 carry the conversational beat. Chip clicks reuse
                 the same WS as the TypeRail (text input). Skip
                 emits a template_skip event over the WS. -->
            ${callQuestion ? html`
              <div class="callchrome-qcard">
                <${QuestionCard}
                  question=${callQuestion}
                  error=${callQuestionError}
                  compact=${true}
                  showWaiting=${false}
                  onAnswer=${(text) => {
                    const ws = wsRef.current;
                    if (ws && ws.readyState === WebSocket.OPEN) {
                      try { ws.send(JSON.stringify({ type: "text", text })); } catch {}
                    }
                    setCallQuestion(null);
                    setCallQuestionError(null);
                  }}
                  onSkip=${(qid) => {
                    const ws = wsRef.current;
                    if (ws && ws.readyState === WebSocket.OPEN) {
                      try { ws.send(JSON.stringify({ type: "template_skip", question_id: qid })); } catch {}
                    }
                    setCallQuestion(null);
                    setCallQuestionError(null);
                  }}
                />
              </div>
            ` : ""}
            <${TypeRail}
              wsRef=${wsRef}
              placeholder=${agent ? `Type to ${agent.name}…` : "Or type to Eva…"}
            />
            <div class="callchrome-bottom">
              <button class=${"pill " + (muted ? "muted" : "")} onClick=${toggleMute}>
                ${muted ? Icons.micOff : Icons.mic}
                <span>${muted ? "Muted" : "Mute"}</span>
              </button>
              <button class="pill hangup" onClick=${closeSession}>
                ${Icons.hang}
                <span>End call</span>
              </button>
            </div>
          `
        : ""}

      ${hint ? html`<div class="hint">${hint}</div>` : ""}

      ${revealAgent && view === "landing" && revealStage === "unveal" ? html`
        <${TheatricalUnveal}
          agent=${revealAgent}
          presets=${presets}
          onDone=${() => setRevealStage("cockpit")}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "overview" ? html`
        <${AgentOverviewPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          stats=${cockpitStats}
          onTest=${() => { onTest(revealAgent.id); }}
          onTestPhone=${(num) => flashHint(`Phone-test stub: ${revealAgent.name} would call ${num}. Wire Twilio/GTS to enable outbound.`, 4500)}
          onEdit=${() => {
            setEditAgentId(revealAgent.id);
            setDrawerOpen(true);
          }}
          onGoLive=${() => setGoLiveAgent(revealAgent)}
          onNav=${(r) => {
            // Same-agent sub-route → just switch section, keep revealAgent so we don't
            // re-fetch and re-mount. Different route → drop the agent and navigate.
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "calls" ? html`
        <${AgentCallsPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          onEdit=${() => { onTest(revealAgent.id); }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "outcomes" ? html`
        <${AgentCallOutcomesPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "purpose" ? html`
        <${AgentPurposePage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "extra-info" ? html`
        <${AgentExtraInfoPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "persona" ? html`
        <${AgentPersonaPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "small-talk" ? html`
        <${AgentSmallTalkPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "knowledge" ? html`
        <${AgentKnowledgePage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "voice" ? html`
        <${AgentVoicePage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "guardrails" ? html`
        <${AgentGuardrailsPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "test-call" ? html`
        <${AgentTestCallPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          onTest=${() => { onTest(revealAgent.id); }}
          onTestPhone=${(num) => flashHint(`Phone-test stub: ${revealAgent.name} would call ${num}. Wire Twilio/GTS to enable outbound.`, 4500)}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "go-live" ? html`
        <${AgentGoLivePage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          org=${user?.org || null}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "numbers" ? html`
        <${AgentNumbersPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          org=${user?.org || null}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "developer" ? html`
        <${AgentDeveloperPage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${revealAgent && view === "landing" && revealStage === "cockpit" && revealSection === "profile" ? html`
        <${AgentProfilePage}
          agent=${revealAgent}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          org=${user?.org || null}
          refreshAgent=${() => {
            fetch(`/api/agents/${revealAgent.id}`).then((r) => r.json()).then((a) => a?.id && setRevealAgent(a)).catch(() => {});
            refreshAgents && refreshAgents();
          }}
          onNav=${(r) => {
            const sameAgent = r.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
            if (!sameAgent) setRevealAgent(null);
            goRoute(r);
          }}
        />` : ""}

      ${agentsListOpen && view === "landing" ? html`
        <${DashboardAgentsList}
          agents=${agents}
          presets=${presets}
          plan=${plan}
          onBuildNew=${() => {
            // "Build new" → land on the homepage prompt composer, NOT
            // the voice flow. /build auto-fires openSession() (voice
            // mic mode) via applyRoute; the prompt-first composer is
            // the default entry now. Routing to "/" shows LandingHero
            // with the textarea + starter chips; the operator types or
            // taps "Talk instead" if they want voice.
            setAgentsListOpen(false);
            setRevealAgent(null);
            goRoute("/");
          }}
          onOpen=${(a) => { setAgentsListOpen(false); goRoute(`/agent/${a.slug || a.id}`); }}
          onDelete=${onDelete}
          onNav=${(r) => goRoute(r)}
        />
      ` : ""}

      ${goLiveAgent ? html`
        <${GoLiveModal}
          agent=${goLiveAgent}
          onClose=${() => setGoLiveAgent(null)}
        />` : ""}

      <${TweaksDrawer}
        open=${drawerOpen}
        onClose=${() => { setDrawerOpen(false); setEditAgentId(null); }}
        agents=${agents}
        refreshAgents=${refreshAgents}
        onTest=${onTest}
        onDelete=${onDelete}
        tweaks=${tweaks}
        setTweaks=${setTweaks}
        schema=${schema}
        presets=${presets}
        buildSnapshot=${buildSnapshot}
        initialEditId=${editAgentId}
        clearInitialEditId=${() => setEditAgentId(null)}
      />

      ${(() => {
        // Persistent Eva helper — bottom-right of every dashboard page once
        // the operator has built (or imported) their first agent.
        // Visibility rules:
        //   • At least one saved agent (Eva-as-helper only makes sense
        //     after the build flow has been used at least once).
        //   • Splash has faded in (don't fight the brand reveal).
        //   • Not in a call (call view owns the mic; helper would conflict).
        //   • Not on the auth screens or the public /embed view.
        //   • Not during the unveal animation.
        //   • Not on the homepage. The LandingHero already carries the
        //     "Talk to Eva" CTA + the audio-reactive orb — having the
        //     FloatingEva bubble simultaneously hover in the corner is
        //     redundant and visually noisy. The bubble's purpose is to
        //     follow the operator INTO a dashboard page when they need
        //     contextual help; on the homepage the hero IS Eva's surface.
        //
        // Homepage = view==='landing' with no agent dashboard / list /
        // account page open — exactly the same condition the LandingHero
        // uses to render itself (see line ~9090).
        const onHomepage = (
          view === "landing"
          && !revealAgent
          && !agentsListOpen
          && !accountPage
        );
        const fevaVisible = (
          agents.length > 0 &&
          splashGone &&
          view === "landing" &&
          !authPage &&
          !embedSlug &&
          revealStage !== "unveal" &&
          !onHomepage
        );
        // Derive a clean route + a human label for the context chip from
        // existing route-driving state. Keeps a single source of truth.
        const fevaRoute = (() => {
          if (embedSlug) return `/embed/${embedSlug}`;
          if (authPage) return `/${authPage}`;
          if (accountPage) return `/account/${accountPage}`;
          if (agentsListOpen) return "/agents";
          if (revealAgent && revealSection) {
            const slug = revealAgent.slug || revealAgent.id;
            return revealSection === "overview"
              ? `/agent/${slug}`
              : `/agent/${slug}/${revealSection}`;
          }
          return "/";
        })();
        const fevaPageLabel = (() => {
          if (revealAgent && revealSection) {
            const labels = {
              "overview": "Overview",
              "profile": "Business profile",
              "persona": "Persona & tone",
              "small-talk": "Small talk",
              "knowledge": "Knowledge base",
              "guardrails": "Guardrails",
              "voice": "Voice settings",
              "developer": "Webhooks & data",
              "go-live": "Go live",
              "calls": "Call logs",
              "test-call": "Get a test call",
            };
            return `${labels[revealSection] || "Dashboard"} — ${revealAgent.name}`;
          }
          if (agentsListOpen) return "All agents";
          if (accountPage) return `Account · ${accountPage}`;
          return null;
        })();
        return html`
          <${FloatingEva}
            visible=${fevaVisible}
            user=${user}
            currentRoute=${fevaRoute}
            pageLabel=${fevaPageLabel}
            contextAgent=${revealAgent}
            refreshAgent=${() => {
              if (!revealAgent?.id) { refreshAgents && refreshAgents(); return; }
              fetch(`/api/agents/${revealAgent.id}`)
                .then((r) => r.json())
                .then((a) => { if (a?.id) setRevealAgent(a); })
                .catch(() => {});
              refreshAgents && refreshAgents();
            }}
            refreshAgents=${refreshAgents}
            onNavigate=${(route) => {
              // Server told us to navigate (after an edit). Mirror the
              // same path-leaves-current-agent logic used elsewhere.
              const sameAgent = revealAgent && route.startsWith(`/agent/${revealAgent.slug || revealAgent.id}`);
              if (!sameAgent) setRevealAgent(null);
              goRoute(route);
            }}
          />
        `;
      })()}

      ${recoverSid && view === "landing" && splashGone && !embedSlug && !authPage ? html`
        <${BuildRecovery}
          sid=${recoverSid}
          onCommitted=${(agent) => {
            // Finalize succeeded. Mirror the agent_saved code path:
            // route to the agent's dashboard with the reveal animation,
            // refresh the agents list, drop the recovery banner.
            //
            // Lock the reveal SYNCHRONOUSLY before unmounting the banner.
            // setRecoverSid(null) causes BuildRecovery to disappear; if
            // revealAgent is also null at that instant (because the
            // /api/agents fetch hasn't resolved), the user sees the
            // LandingHero — clicking its "Open your agents" CTA goes to
            // /agents (the LIST), NOT to the agent that was just saved.
            // Routing first via the `agent` payload (which already has
            // id+slug from the server's /state response) eliminates the
            // flash. The async fetch below just upgrades the dict.
            agentSavedRef.current = true;
            setRevealStage("unveal");
            setRevealAgent(agent);
            if (agent?.slug) goRoute(`/agent/${agent.slug}`);
            else if (agent?.id) goRoute(`/agent/${agent.id}`);
            setRecoverSid(null);
            fetch(`/api/agents/${agent.id}`)
              .then((r) => r.ok ? r.json() : null)
              .then((full) => {
                if (full && full.id) setRevealAgent(full);
              })
              .catch(() => { /* agent is already in place */ });
            refreshAgents && refreshAgents();
          }}
          onAbandoned=${() => setRecoverSid(null)}
        />
      ` : ""}
    </div>
  `;
}

createRoot(document.getElementById("root")).render(html`<${App} />`);
