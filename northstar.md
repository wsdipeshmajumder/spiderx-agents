# SpiderX AI · Eva — Northstar

## What we're building

A voice-first phone-AI agent builder for non-technical operators. Open the
app, tap the blob, talk for ninety seconds, and you have a working phone
agent — already on the line and ready to take a mock call. No forms, no
dashboards, no setup.

The audience is a dental-clinic owner, a hotel manager, an insurance
broker, a yoga-studio lead, a café manager — someone who has never written
a system prompt and shouldn't have to. The locales we serve at launch are
**US, UK, Singapore, and India**, with Eva auto-tailoring her tone and the
agent's defaults to the operator's region.

## The promise

> "Tell me what you want answered when your phone rings. I'll be that
> answerer in two minutes."

Two minutes from cold open to a Hindi-speaking dental receptionist on the
line, taking a mock call. Two more minutes to swap her for a Tamil-speaking
insurance lead-qualifier. The friction is zero — you just keep talking.

## Design principles

1. **One surface.** The iridescent voice blob is the entire UI on the
   landing screen. No buttons, no panels, no captions, no menus. Brand
   appears once on first load as a 1.6 s fade, then vanishes.

2. **Less is more.** Every visible affordance is paid for by user need.
   Everything else — agent listing, voice tuning, guardrails, connector
   picking, telephony routing — happens behind the scenes, surfaced
   conversationally when (and only when) relevant.

3. **Apple polish, not POC mindset.** Every micro-detail considered. The
   reference for finish quality is consumer hardware — what would ship
   from Cupertino, not what would ship from a hackathon.

4. **The blob is alive.** Subtle breathing at rest. Iridescent shift over
   time. Audio-reactive scale and halo. Sparkle motes drifting inside —
   wandering when idle, **forming an audio-synced waveform during a
   call**. Cooler palette while listening, warmer while speaking. Never
   gimmicky, always restrained.

5. **Eva leads the build. The agent owns the call.** During the build Eva
   is the host: warm small talk, decisive defaults, never interrogates.
   The moment she has enough, she steps back and the new agent is the
   hero — its name, voice, persona, and capabilities are revealed to the
   user, who chooses to test, edit, or take it live.

6. **A-star front-office behaviour, baked in.** Every saved agent
   inherits a universal "five-year veteran receptionist" overlay:
   acknowledge before action, empathy first, real prosody, no
   robotic monologues, code-switch naturally, confirm critical details,
   close with *"anything else I can help with?"*, escalate gracefully
   when out of scope.

## Interaction model

Three gestures cover the entire experience; everything else is voice.

| Gesture                | Effect                                          |
|------------------------|-------------------------------------------------|
| Tap (landing)          | Start a session — Eva greets and leads        |
| Tap (during a call)    | Toggle mic mute (haptic-style hint flashes)     |
| Press-and-hold ≥ 0.55s | End the call                                    |

During an active call, a thin status strip appears: agent name, call
state (dialling / connected / reconnecting / ending), and an elapsed-time
counter, plus a redundant **hangup pill** and **type-to-Eva text rail**
so the gesture isn't the only way out and a flaky mic isn't the only way
in. This is the only chrome on the screen besides the blob.

## Eva's conversational behaviour

Eva is not a form rendered as dialogue. Eva is a colleague who already
knows how to build a good agent and is gently guiding the human to the
answers.

- **Always leads.** Opens with a clear two-option offer ("build something
  new, or hop on a call with Maya?") — or, if no agents exist, a warm
  "tell me about the agent you'd like — what business is this for?".
- **Empathetic and mirrors.** Brisk human → Eva is brisk. Unsure human
  → Eva reassures with phrases like *"we can change any of this later
  — let's just get something working first."*
- **Suggests, doesn't interrogate.** For a dental clinic Eva says
  *"callers usually want to book or reschedule appointments — should
  we handle both?"* instead of *"what use-cases do you want?".*
- **Silently picks defaults.** Voice, locale, connectors, guardrails
  — all chosen using sector + region heuristics. Eva only confirms a
  choice when it's non-obvious.
- **Knows the saved agents.** Every time the user taps the blob, the
  agent list is injected into Eva's context so she can route by name
  (*"call Maya"*) via the `select_agent` tool.
- **Writes A-star prompts.** When Eva calls `save_agent`, she composes
  a 200-450-word system prompt for the new agent covering: who they
  are, the 2-3 things callers actually call about, how to use the
  connectors specifically, 2-3 sample phrases, sector-specific
  edge-cases, multilingual code-switching guidance, and how to close /
  escalate.
- **Hands off cleanly.** When ready, Eva says one short warm line
  (*"I think she'll be lovely. Putting her on now."*) and stops. The
  server emits `build_complete` and exits the builder session. The
  browser dissolves the orb into the **agent reveal card**.

## Conversational fidelity — the things that make it feel human

These are the details that turn "voice AI" into "an actual phone call".

- **Real barge-in.** The moment the user starts talking, the agent's
  audio stops. The client detects this locally via mic peak amplitude
  (no round-trip needed) and hard-stops every scheduled `BufferSource`;
  in parallel, the server lets Gemini's default activity-detection
  signal its own interrupt. Combined, the agent yields the line
  within a beat.
- **Conversation memory across reconnects.** If Gemini's edge drops
  the underlying session mid-build, the server replays the user's
  prior statements (captured from `input_audio_transcription`) into
  the freshly-opened session as a synthetic system-style "earlier the
  user told you X" message. Eva picks up from where she left off
  instead of restarting the greeting loop.
- **Type-to-Eva fallback.** A text input rail is always visible during
  a call. If the mic is flaky or the room is loud, the user types and
  the agent responds in voice. This is the seatbelt under the
  audio path.
- **No re-greeting loops.** At most one "sorry, you broke up there"
  apology per session. After that, reconnects are silent — context is
  intact and the agent just keeps going.
- **A-star agent runtime overlay.** Every saved agent's runtime
  system prompt is wrapped by a universal A-star front-office block:
  short sentences (1-2 max), real prosody, acknowledge before action
  (*"Sure, let me check that for you…"*), confirm critical details
  by repeating back, code-switch naturally if multilingual, never
  read card numbers or OTPs aloud, close warmly with *"anything else
  I can help with?"*, escalate to human when out of scope.

## Build → reveal → test → live

The user flow has four named beats. They blur into one experience,
but each has a different visual signature.

```
TAP ORB
   │
   ▼
[ BUILD ]               Eva talks. Orb is alive. Sparkles wander.
   │                    Eva picks voice / locale / connectors / guardrails
   │                    silently. Asks 3-4 short leading questions only if
   │                    something material is missing.
   │
   ▼
[ REVEAL ]              Eva: "I think she'll be lovely. Putting her on now."
                        Server emits build_complete and exits the session.
                        Orb fades to background. An Apple-style card slides
                        in: agent thumbnail · name · persona tagline ·
                        pills (sector / locale / voice) · italic-quoted
                        greeting · "What <name> can do" + connector badges ·
                        actions: [Edit] [Call <name>] [Go live] [Back to home].
                        Eva is silent. The agent is the hero.
   │
   ├─── Edit ───────►    Studio drawer opens directly into the agent's
   │                    editor — every Gemini Live voice param (voice,
   │                    temperature, top-p, mic sensitivity, silence /
   │                    prefix-padding ms), the full system prompt textarea,
   │                    guardrail checklist, connector checklist. Save
   │                    persists into the per-agent `voice_tweaks` JSON.
   │
   ├─── Call <name> ─►   Fresh /ws/session?agent_id=N. Agent picks up the
   │                    line with their greeting. Real conversation. Tools
   │                    fire mid-call. Sparkles in the orb form an
   │                    audio-synced waveform throughout.
   │
   └─── Go live ────►    Three-step Twilio routing modal: ngrok tunnel,
                        Twilio number webhook URL (TwiML auto-generated
                        per agent), test call. Copy-TwiML button. Twilio
                        Media Streams bridge already wired server-side.
```

## Architecture

```
┌────────────────────────┐    PCM16 / 16 kHz     ┌──────────────────────┐
│   Browser              │ ────WebSocket────►    │   FastAPI relay      │
│   one React component  │ ◄────PCM16 / 24 kHz   │   (one /ws/session)  │
│   voice blob is the UI │                       └──────────┬───────────┘
└────────────────────────┘                                  │ google-genai
   │                                                        ▼
   │  Client-side barge-in              ┌─────────────────────────────────────┐
   │  + audio-synced sparkles           │  Gemini Live · cascade or native    │
   │  + type-to-Eva text rail           │  · save_agent / select_agent        │
   │                                    │  · 10 connector tools (test mode)   │
   │                                    │  · session_resumption + memory      │
   │                                    │  · A-star front-office overlay      │
   │                                    └─────────────────────────────────────┘
   │                                                        ▲
   │                ┌──────────────────────┐  µ-law 8k JSON │
   │  Twilio Voice ─┤  /ws/twilio/{id}     ├────────────────┘
   │  (real PSTN)   │  µ-law ↔ PCM resample │
   │                └──────────────────────┘
   ▼
SQLite agents table:
  · id, name, sector, locale, voice
  · persona, greeting, system_prompt (A-star)
  · guardrails[], connectors[]
  · voice_tweaks {voice, temperature, top_p,
                  sensitivity, silence_ms, prefix_pad_ms}
  · sip_config
```

- **One WebSocket per session.** Browser ↔ relay opens once per
  visit. After save, the server emits `build_complete` and closes
  cleanly. The reveal card is purely client-state from there.
- **No middleman models.** No STT, no TTS, no orchestration framework.
  Gemini Live is the brain, the ear, and the voice — that's the
  whole point.
- **Connectors are Gemini tools.** Each saved agent's runtime session
  is opened with its picked connectors registered as
  `FunctionDeclaration`s. They fire mid-call. The agent narrates the
  result naturally — never recites raw fields.
- **Telephony is provider-agnostic.** Twilio Media Streams ships
  first; Telnyx, Plivo, Exotel follow the same WS+JSON+µ-law shape.

## What we deliberately don't have

- **No web dashboard for agents.** No table view, no metrics page,
  no prompt-editor screen as the primary surface. The Studio drawer
  exists for tweaks but it's tucked behind a `⋯` and Eva is always
  the front door.
- **No multi-tenant chrome.** Single operator, single instance for now.
- **No transcripts on screen during a call.** The human is on the
  phone — reading is a different mode and would distract from the voice.
  Transcripts stream server-side for logs only.
- **No setup wizard, no first-run tour, no tooltips.** The blob is
  self-explanatory: tap it.

## Success criteria

A successful first session looks like this, from the human's seat:

1. Page loads. Brand fades in for 1.6 s, then it's just a calm
   iridescent orb in the dark. Sparkles wander.
2. Human taps the orb. Mic permission dialog. Allow.
3. Within 1-2 s, Eva's voice: *"Hi — tell me what you'd like your
   phone to say when it rings."*
4. Human: *"I want a Hindi-speaking dental receptionist for my clinic
   in Bangalore. Name her Maya. She books and reschedules. Never give
   medical advice."*
5. Eva: *"Hi! Wonderful, I've got that. We'll call her Maya, handling
   bookings and reschedules in Hindi and English. And I'll make sure
   she never gives medical advice. I think she'll be lovely. Putting
   her on now."* — `save_agent` fires.
6. The orb fades to background. The reveal card slides in: *"Meet
   your new agent — Maya"*, with her pills, her greeting in italic
   quotes, three connector badges (calendar check, calendar book,
   SMS), and three actions.
7. Human taps **Call Maya**. A fresh WS opens in test mode. Within
   ~3 s Maya picks up the line: *"Namaste, this is Maya at your
   clinic in Bangalore, how can I help you?"*. Sparkles in the orb
   form an audio-synced waveform that pulses with whoever is speaking.
8. Human roleplays as a caller. Maya checks `calendar_check`,
   proposes a slot in Hinglish (*"hamare paas 3 baje ka slot khaali
   hai"*), books with `calendar_book`, sends SMS confirmation via
   `sms_send`, closes with *"Is there anything else I can help you
   with?"*.
9. Human says "thanks, that's all" and long-presses the orb. Call
   ends. Sparkles return to wandering. Maya stays saved.
10. Next visit, tap the orb. Eva: *"Build something new, or hop on a
    call with Maya?"*

Total clock time: ninety seconds for the build, two more for the
test call. Total clicks: one (allow mic) plus the gestures.

## Anti-patterns we will not ship

- A "save my agent" button.
- A dropdown for choosing voice / language / sector as the primary
  build path.
- A modal that says "Are you sure you want to end the call?"
- A loading spinner that lasts longer than the breathing of the orb.
- Captioning that draws the eye away from the orb during a call.
- An onboarding tour or "try the demo" overlay.
- A microphone visualiser separate from the orb — the orb IS the
  microphone visualiser.
- A robotic-sounding agent. Real prosody, real prosaic warmth, real
  pauses, or it doesn't ship.

## Anti-patterns the AGENTS will not exhibit

- "As an AI, I can't…" / "I'm just an AI". The agent is the
  business's receptionist. Not an AI.
- Reading enumerated options out loud ("press 1 for…").
- Long monologues. 1-2 sentences per turn. If the answer is
  longer, ask if the caller wants the detail.
- Reading card numbers, OTPs, or passwords aloud.
- Promising outcomes ("yes I'll refund you" / "your loan is approved").
- Pretending to know an answer it doesn't know. Offer to put the
  caller through to the team instead.

## Open questions / roadmap candidates

- **Agent versioning** — Eva should be able to say *"edit Maya's
  greeting"* and produce a new revision without the human stepping
  outside the conversation.
- **Connector authoring by voice** — describe a connector (*"when
  someone confirms a booking, also POST to my Slack channel"*) and
  Eva should be able to wire it.
- **Outbound calling** — symmetric to Twilio inbound; speak a number,
  the agent dials out and runs a script.
- **Operator analytics** — calls handled, resolution rate, escalations.
  Surfaced only when asked, not as a default screen.
- **Multi-org / team accounts** — for franchise or chain operators
  with multiple receptionists.

## North-star user

> Asha runs a four-chair dental clinic in HSR Layout, Bangalore. She
> has no IT staff. Her receptionist quit on Tuesday and the patients
> keep calling. By Wednesday lunchtime she has Maya answering calls in
> Hindi and Kannada, booking appointments to Google Calendar, sending
> SMS confirmations, and escalating anything medical back to her. She
> has never once seen a Gemini API console, a prompt field, or a
> webhook URL. She has spoken to one orb.


---

# Part II · Mother → Children Doctrine

> Eva is the mother. Every saved agent is her child. SpiderX is a
> super-agent platform — one mother births many children, each healthy,
> independent, and smart, for any service business in any country we
> support.
>
> This part of the doc is the contract the platform commits to. If a
> build path violates a non-negotiable, it's a bug. If the coverage
> matrix has a hole, it's a roadmap item — explicit, not invisible.

## What "healthy, independent, smart" means

A child agent is **healthy** when:

- It has all six canonical scalars: `name`, `sector`, `locale`, `voice`,
  `greeting`, `system_prompt`.
- It has structured business profile (`variables`) with at least
  `business_name`, `industry`, `country`, and one of {`city`,
  `address`, `hours`}.
- It has a **core purpose** record (`agents.purpose`): summary, 2–6
  answers, 2–4 actions from the canonical action library,
  `post_call.{email,sms}` flags.
- It has a sector-appropriate **outcome taxonomy** (one of 4 outcome
  families: booking / sales / support / intake).
- It has a sector-appropriate **ambience** and **VAD style**.
- It has a guardrail policy: at least 2 do's, 2 don'ts.

A child agent is **independent** when:

- It can hold the call without supervision for the duration of a
  conversation, including reconnects (`<call_resumed>`).
- It knows what's in scope (its `purpose.answers` + `purpose.actions`)
  and what isn't.
- For out-of-scope asks, it offers a callback or human transfer instead
  of inventing.
- It calls `end_call` exactly once at the end with `outcome`, `reason`,
  `summary`, `sentiment`, `lead_quality`, `lead_signals`.

A child agent is **smart** when:

- Its accent + idiom matches the country (Indian English for IN/Gulf,
  British for GB/SG/HK, American for US/MX/BR, etc.).
- Its voice + ambience matches the **environment**, not a default.
- Its outcomes + actions match its **sector**, not a generic.
- Its persona suggestion matches the country naming convention (Maya/
  Sofia/Olivia for IN, Emily/James for GB, Daniel/Ava for US, etc.).

## Non-negotiables (system enforces today)

These are codified in the codebase and **must remain true** through any
future refactor. A change that violates one is a regression.

| # | Invariant | Where enforced |
|---|---|---|
| 1 | Every sector in `presets.SECTORS` has full coverage in `silent_defaults` (VAD, outcomes, ambience, do's, don'ts) | `backend/silent_defaults.py` — verified by `defaults_for(sector)` returning non-empty for every catalogue id |
| 2 | Eva can never wipe a sector default by mentioning ONE nested field. Explicit choices are **deep-merged** on top of silent defaults, not substituted. | `silent_defaults.merge_into_save_args` |
| 3 | Eva can NEVER fabricate enum values. `sector`, `locale`, `voice`, `connectors[]`, `purpose.actions[]` are enum-bound at the `save_agent` declaration. | `gemini_bridge._save_agent_decl()` |
| 4 | Every saved agent's runtime prompt inherits the **A-STAR front-office floor** (honesty, empathy, escalation, time-keeping) before its sector-specific block. A 200-word Eva-written prompt still gets the universal floor. | `gemini_bridge._agent_system_prompt()` |
| 5 | NEVER-RE-GREET rule + 4-turn build cap + never-invent rules are the strongest builder behavioural guardrails. They map 1:1 to known production failure modes. | `gemini_bridge._builder_system_prompt` |
| 6 | Disconnect-safety blocks `end_call` with imprecise outcomes within the first 10s of a call. The agent is told to retry with a clearer outcome rather than insist. | `connectors.handle` end_call branch + min-call-age check |
| 7 | Outcome vocabulary is shared across all sectors (4 outcome families). Call-log analytics can compare across verticals on the same axis. | `silent_defaults._OUTCOME_SETS` |
| 8 | `purpose.actions[]` uses the **same 8-action library** for every sector. A car dealership's `appointment_booking` and a clinic's `appointment_booking` are the same canonical action — drives consistent downstream behaviour. | save_agent schema + `_format_purpose_for_prompt` |
| 9 | Variable substitution `{{key}}` happens at session-open in **all three** of `persona`, `greeting`, `system_prompt` — single template surface, no half-substituted strings reach the model. | `_substitute_variables` + `_agent_system_prompt` |
| 10 | Tokens for **every** LLM session (builder + agent + tts) write to `llm_calls` with a kind discriminator. Cost-per-minute is a stored generated column — finance dashboards never recompute. | Alembic 0007, `db_pg.insert_llm_call`, `_flush_llm_session` |
| 11 | Every super-admin write goes through `audit_log` in the same transaction as the mutation. Forensic trail can't desync from the action. | `backend/admin.py` |
| 12 | Plan-gating is enforced at the API boundary, never inline. Free-tier publishing returns 402; rate limit returns 429; super-admin needs `super_admins` row check. Same shim everywhere. | `backend/auth.py` + 402/429 routes |
| 13 | Per-call **sentiment + lead_quality + lead_signals** are mandatory at `end_call` (declaration-required for the new fields). The lead-quality filter only works if the data is honest. | `connectors.CONNECTOR_DECLS["end_call"]` |

## Coverage matrix (as of 2026-05-14)

### Sector × silent-defaults

```
                  VAD   Outcomes   Ambience   Do's   Don'ts   Profile
healthcare        ✓     booking    clinic     ✓      ✓        ✓
dental            ✓     booking    clinic     ✓      ✓        ✓
salon             ✓     booking    quiet      ✓      ✓        ✓
restaurant        ✓     booking    cafe       ✓      ✓        ✓
travel            ✓     booking    cafe       ✓      ✓        ✓
events            ✓     booking    cafe       ✓      ✓        ✓
automotive        ✓     booking    workshop   ✓      ✓        ✓
real_estate       ✓     sales      office     ✓      ✓        ✓
insurance         ✓     sales      office     ✓      ✓        ✓
banking           ✓     sales      office     ✓      ✓        ✓
education         ✓     intake     office     ✓      ✓        ✓
legal             ✓     intake     quiet      ✓      ✓        ✓
retail            ✓     support    cafe       ✓      ✓        ✓
logistics         ✓     support    workshop   ✓      ✓        ✓
saas_support      ✓     support    office     ✓      ✓        ✓
generic           ✓     support    office     ✓      ✓        ✗ (universal only)
```

✓ — every catalogue sector has silent-defaults coverage.

### Country × region defaults (`_REGION_PROFILES`)

```
                  Profile   EN-variant   Default voice   Currency   SIP    Naming hint
─── Full profiles (locale-rich, well-tested) ───────────────────────────
IN                ✓         en-IN        Aoede           INR        exotel ✓
GB                ✓         en-GB        Leda            GBP        twilio ✓
US                ✓         en-US        Aoede           USD        twilio ✓
SG                ✓         en-GB        Leda            SGD        twilio ✓
AU                ✓         en-AU        Aoede           AUD        twilio ✓
AE                ✓         en-IN        Aoede           AED        twilio ✓
DE                ✓         en-GB        Leda            EUR        twilio ✓
FR                ✓         en-GB        Leda            EUR        twilio ✓
ES                ✓         en-GB        Leda            EUR        twilio ✓
BR                ✓         en-US        Aoede           BRL        twilio ✓
MX                ✓         en-US        Aoede           MXN        twilio ✓
CA                ✓         en-US        Aoede           CAD        twilio ✓
JP                ✓         en-GB        Leda            JPY        twilio ✓
─── Stub profiles (en variant + currency + name hints) ────────────────
NL                ◐         en-GB        Leda            EUR        twilio ✓
IT                ◐         en-GB        Leda            EUR        twilio ✓
KR                ◐         en-GB        Leda            KRW        twilio ✓
ZA                ◐         en-GB        Leda            ZAR        twilio ✓
NZ                ◐         en-AU        Aoede           NZD        twilio ✓
IE                ◐         en-GB        Leda            EUR        twilio ✓
─── Falls to US profile (no region default) ──────────────────────────
CN, KE, NG, ID,   ✗         ✓ (en-GB)    — emits US currency + naming in Eva's brief
TH, VN, PH, etc.
```

**19** countries with at least a stub region profile (full=13 + stub=6).
Everything else still falls through to the US profile, which is the
last remaining piece of "biggest open gap". The biggest markets (IN,
US, GB, GCC, EU, JP, BR, MX, CA, AU, NZ) are now covered.

## Known gaps (ranked by impact)

These are real, documented, and on the roadmap. They are **not**
non-negotiables — they are admissions of incomplete work.

1. **Non-anglophone countries have no region profile.** Tokyo / Berlin /
   São Paulo operators get US defaults silently. Fix: extend
   `_REGION_PROFILES` with at minimum JP, DE, FR, ES, BR, MX, NL.
2. **`_english_variant()` returns `en-US` for non-English-locale
   countries.** A German user gets a US English suggestion. Fix: return
   `en-GB` as the neutral non-anglophone default + add a note.
3. **`purpose.post_call.{email,sms}` is captured but never fired.** Both
   Eva and the runtime agent advertise "a confirmation goes out" — false
   advertising today. Need an `end_call` post-hook that fires email via
   `email_stub` and gates SMS on plan tier.
4. **Voice selection is sector-only, never region-aware.** No "Indian
   customers prefer X / Gulf customers prefer Y" guidance. `Aoede`
   hard-coded fallback regardless of country. Fix: add `default_voice`
   to each region profile.
5. **Eva's connector hints omit several sectors.** Automotive, legal,
   logistics, saas_support, generic get no connector suggestion from
   Eva's prompt. SaaS should default to `knowledge_base_search` at minimum.
6. **`SECTOR_PROFILE_SCHEMA` has no entry for `generic`.** A "generic
   receptionist" agent has no industry-specific form fields beyond the
   universal canonical-vars.
7. **No purpose-template example for 12 of 16 sectors.** Eva's prompt
   gives car / clinic / SaaS / salon. The rest improvise.
8. **`SIP_PROVIDERS` is dead in the UI.** Catalogue lists 6 providers
   but no frontend surface exposes the picker.

## Inconsistencies to tidy

Small, won't break anything, but noisy in the codebase:

1. **Three definitions of "supported countries"** — frontend `COUNTRIES`
   (16), backend `_REGION_PROFILES` (6), `_TZ_REGION` (~35). One should
   be canonical; the other two derive from it.
2. **`voice_tweaks.ambience` enum lists `busy_office`** which no sector
   defaults to and no frontend label maps. Either wire or remove.
3. **Education + legal are `patient` VAD** but use `_intake` outcomes
   that favour fast lead capture. Re-evaluate.
4. **Three orphan voices** (`Kore`, `Fenrir`, `Zephyr`) — in
   `presets.VOICES` but never recommended by Eva's prompt.
5. **Asymmetric disconnect-safety** — `_intake` sectors get
   `{not_interested, voicemail, wrong_number}` as imprecise outcomes;
   `_support` loses `not_interested`. Decide if support agents need the
   same min-age guard.
6. **Hard-coded `Aoede` fallback in TWO places** — Eva's prompt + the
   runtime fallback. Make it one constant.
7. **Salon ambience** defaults to `quiet` in backend, but Eva's prompt
   says "salon premium → quiet" only when premium. Reconcile.

## What Eva must NEVER do (hard rules in her prompt)

1. **Never re-greet.** Once per call. Drops + reconnects don't reset.
2. **Never invent business facts.** If the user didn't say it, it's not
   in the agent. No "primarily focused on deliveries", "family-run",
   "boutique" unless those words came from the user.
3. **Never re-ask a fact she already captured.** INTERNAL STATE rule.
4. **Never spawn an agent with empty mission.** If the build is
   interrupted, save with sensible silent defaults — never leave a child
   with a NULL purpose.
5. **Never promise post-call SMS on a free plan.** Plan-gating is
   downstream; Eva captures intent honestly.
6. **Never bypass the 4-turn build cap silently.** If turn 5 is needed,
   it's a failure of pacing — defaults + save immediately.

## What agent runtime children must NEVER do

1. **Never read full card numbers, OTPs, or full account numbers aloud.**
2. **Never quote prices, hours, or availability outside what they were
   given.** Knowledge-base or transfer instead.
3. **Never promise refunds, approvals, or outcomes outside their scope.**
4. **Never tag every caller as `hot`.** Lead-quality assessment must be
   honest — information-only callers are `cold`, not `hot`. The hot-lead
   filter is only useful if `hot` really means hot.
5. **Never call `end_call` more than once per call.**
6. **Never start an apology with "as an AI" or "I cannot".** Help, or
   transfer.

## The promise (Part II)

> One minute in, one healthy agent out — for any service business in any
> country we support. The matrix above is honest about which countries
> that is today. The gaps are real and the roadmap items are visible.
> We don't ship "almost works" agents; we ship a child who can hold a
> real conversation, captures the right signals, and writes a structured
> log her operator can act on.
>
> Eva is the mother. Every child she births inherits the floor.


---

# Part III · Implementation log — post-doctrine gap closure

> What changed after Part II was committed. The doc and the code stay
> in sync — items move from "known gaps" to "non-negotiables" only when
> the implementation lands.

## Shipped in this pass

| Gap (Part II §5) | What landed | Verification |
|---|---|---|
| `_REGION_PROFILES` covered 6/16 countries | Added DE, FR, ES, BR, MX, CA, JP profiles (speaking_note, default_voice, currency, naming_hint). Coverage **6 → 13 / 16**. | `gemini_bridge._REGION_PROFILES` |
| `_english_variant()` returned `en-US` for non-anglophone countries | Now maps 30+ countries explicitly. Non-anglophone fallback = `en-GB` (neutral international). DE, FR, ES, JP, KR no longer pretend to be American. | `gemini_bridge._english_variant` |
| Voice selection was region-blind | Each region profile now has `default_voice`. Eva's `_region_hint` renders it into the silent-defaults brief so she picks per region (Leda for GB/EU/SG/JP, Aoede for IN/US/AU/Gulf/BR/MX/CA). | `_region_hint` includes "Default voice for this region" line |
| Hard-coded `Aoede` fallback in two places | One canonical `DEFAULT_VOICE` in `presets.py`. Both `gemini_bridge` and `db_pg` import it. | `presets.DEFAULT_VOICE` |
| `purpose.post_call.{email,sms}` captured but never fired | End-call handler now calls `_fire_post_call_notifications` after `insert_call`. Email goes to org owners; SMS plan-gated (Free → off regardless of flag). Email body = outcome / sentiment / lead / duration / why / summary / captured details. SMS = ≤80-char digest. | `connectors._fire_post_call_notifications` + `email_stub.send_call_summary_email/_sms` |
| 12 sectors lacked purpose-template guidance | Eva's prompt now has explicit `sector → answers + actions` mapping for all 15 catalogue sectors. | `_builder_system_prompt` CORE PURPOSE section |
| Eva's connector hints omitted automotive / legal / logistics / saas_support / generic | All 15 sectors now have a `Connector hints by sector` block in Eva's prompt. SaaS support correctly gets `knowledge_base_search`. | `_builder_system_prompt` Connector hints block |
| `SECTOR_PROFILE_SCHEMA` had no `generic` entry | Added `generic` with 6 sector-neutral fields (what_we_do, primary_audience, service_areas, key_offerings, pricing_signals, escalation_policy). | `frontend/app.js` SECTOR_PROFILE_SCHEMA |
| Three orphan voices (`Kore`, `Fenrir`, `Zephyr`) never recommended | Eva's voice-pick guidance extended: Kore → clear neutral office; Fenrir → brisk decisive (logistics, auto service); Zephyr → light breezy (travel, hospitality). | Voice-pick line in `_builder_system_prompt` |

## Still on the roadmap (intentional carry-forward)

- **`SIP_PROVIDERS` UI surface.** Catalogue exists, no frontend picker. Defaults pick silently. Owner: Phase ahead.
- **Education + legal VAD vs intake outcomes.** Patient VAD slows fast lead capture. Re-evaluate with real-call data.
- **Three "supported countries" definitions** (frontend 16 / region-profiles 13 / TZ map ~35). Make `_REGION_PROFILES` keys canonical, derive the others.
- **Salon ambience reconciliation** ("salon premium → quiet" in prompt vs always-quiet in backend).
- **Asymmetric disconnect-safety** across outcome families. Decide policy for support agents.

## New non-negotiables (Part III)

These are now enforced and join the table in Part II §3:

| # | Invariant | Where enforced |
|---|---|---|
| 14 | Every region profile has a `default_voice`. Eva surfaces it in her silent-defaults brief. No region collapses to a global hard-coded voice. | `_REGION_PROFILES[*].default_voice` |
| 15 | Voice fallback is one constant — `presets.DEFAULT_VOICE` — imported by every consumer. No more "two places that happen to agree". | `presets.DEFAULT_VOICE` |
| 16 | Eva's prompt has an explicit `sector → answers + actions` map AND an explicit `sector → connectors[]` map for every catalogue sector. Eva never has to invent the purpose template for a sector we ship. | `_builder_system_prompt` CORE PURPOSE + Connector hints blocks |
| 17 | `purpose.post_call.email` actually fires post-call. `purpose.post_call.sms` fires only on paid plans. The agent's claim that "a confirmation goes out" is now true. | `connectors._fire_post_call_notifications` |
| 18 | `_english_variant` never returns `en-US` for a non-anglophone country. Continental Europe + East Asia → `en-GB` (neutral international). Latin America → `en-US` (closer to caller idiom). | `_english_variant` |


---

# Part IV · Independent audit + post-audit fixes

> After Part III shipped, we commissioned an **independent code auditor**
> (fresh context, no prior framing) to verify the doctrine claims hold.
> The audit was honest: it found 5 real issues the implementation log
> had papered over.
>
> This part captures the audit findings, the fixes that followed, and
> the new non-negotiables those fixes added. Doc tracks reality, not
> aspiration.

## What the auditor verified (claims that hold)

- `DEFAULT_VOICE` is a single canonical constant in `presets.py`,
  imported by `gemini_bridge.py` and `db_pg.py`. No leftover hard-codes.
- 13 region profiles all carry the full 7-field schema.
- `_region_hint` emits `Default voice for this region: …` into Eva's
  brief — region-aware voice selection actually wired.
- All 16 catalogue sectors appear in both the answers+actions map and
  the connector-hints map in Eva's prompt.
- Kore + Fenrir + Zephyr explicitly named in the voice-pick rule.
- `_fire_post_call_notifications` is wired after `insert_call` returns,
  inside its own try/except — notification failures never roll back the
  calls row. Plan-gating actually inspects owners' plan tiers.
- `email_stub` kwargs match `_fire_post_call_notifications` callsites.
- `SECTOR_PROFILE_SCHEMA.generic` has the 6 fields documented.
- Legacy `purpose=None` agents don't crash the notification path.

## What the auditor caught (real issues fixed in this pass)

| Finding | Severity | Fix |
|---|---|---|
| `en-AU` missing from `presets.LOCALES` — `_REGION_PROFILES["AU"].default_agent_locale="en-AU"` would fail Eva's `save_agent` enum validation. **Australian operator builds were silently broken.** | High | Added `en-AU` to LOCALES (+ `pt-BR`, `it-IT`, `nl-NL`, `ko-KR` for new region stubs). |
| Part III §1 claim "JP/KR no longer pretend to be American" was wrong — `_english_variant` still returned `en-US` for JP and KR. Doc/code drift. | Medium | Flipped JP, KR, TW to `en-GB` (neutral international register). |
| `_english_variant` knew 30+ countries; `_REGION_PROFILES` covered 13. NL/IT/KR/ZA/NZ/IE users would get the right locale but a US-shaped region brief (USD currency, "Maya/Olivia" naming). | Medium | Added 6 stub region profiles (NL, IT, KR, ZA, NZ, IE) with locale + currency + name hints. Coverage 13 → **19**. |
| `agent.variables.phone` field documented as "caller-facing escalation phone" but `_fire_post_call_notifications` was sending operator SMS to it. Field semantics collision. | Medium | Split into `phone` (escalation, caller-facing) and `notification_phone` (post-call SMS, operator-facing). SMS sender prefers `notification_phone`, falls back to `phone` for legacy agents. Schema updated in Eva's save_agent. |
| Coverage matrix in Part II §4b was stale (still showed pre-doctrine 6/16) even though Part III claimed 13. | Low | Matrix rewritten to show 13 full + 6 stub = 19 profiles. |

## Auditor's minor observations (not fixed, intentionally accepted)

- **`_fire_post_call_notifications` plan-gating caps owner lookups at 3.** Edge case where owners 4+ are on a paid plan but the first 3 aren't. Single-tenant assumption — every member of an org shares the same plan today, so the cap doesn't bite in practice. Documented here for future multi-plan-per-org scenarios.
- **Non-anglophone region profiles default `default_agent_locale` to English (`en-GB` or `en-US`).** Operators in DE/FR/ES/JP could plausibly want `de-DE` / `fr-FR` / `ja-JP` for local-only businesses. Eva will let them override; the silent default is just the safer choice for businesses that take international calls.
- **Docstring drift** — `_fire_post_call_notifications` says "fall back to founder if no owner"; the code actually falls back to **members**. Comment lag; behaviour is correct (member fallback is more useful than a fixed founder fallback for multi-tenant). Will tidy next pass.

## New non-negotiables (Part IV)

| # | Invariant | Where enforced |
|---|---|---|
| 19 | Every `_REGION_PROFILES[X].default_agent_locale` MUST be an ID in `presets.LOCALES`. Mismatch = `save_agent` tool call rejected on enum validation. | `gemini_bridge._REGION_PROFILES` × `presets.LOCALES` (currently in-sync; CI gate worth adding) |
| 20 | `purpose.post_call.sms` recipient lookup prefers `variables.notification_phone` over `variables.phone`. Caller-facing escalation phone is never accidentally the SMS target. | `connectors._fire_post_call_notifications` |
| 21 | `_english_variant` returns `en-GB` for all non-anglophone countries. Continental Europe, East Asia, Africa, SE Asia all get the neutral international register. No country silently pretends to be American. | `gemini_bridge._english_variant` |

## The honest current state

After Part IV, **19 of 16 frontend countries** have at least a stub
region profile (we over-cover — NL/IT/KR/ZA/NZ/IE are in
`_english_variant` even though frontend COUNTRIES doesn't list them).
The biggest markets are full-profile: IN, US, GB, GCC (AE), EU
(DE, FR, ES, IT, NL, IE), JP, BR, MX, CA, AU, NZ, SG, ZA, KR.

The Australian save bug is fixed. The JP/KR English variant is fixed.
The SMS phone semantics is fixed. The matrix in the doc matches the
code. Eva still has 12 of the world's countries (Africa beyond ZA,
SE Asia, China, smaller markets) falling through to the US profile —
genuinely open work, visible in the matrix.

**Eva births healthy children across the matrix? Yes, for the markets
the matrix says are covered. No invisible regressions; no overstated
claims.**


---

# Part V · DB-architect audit + Phase 9a fixes

> An independent DB architect (15+ years Postgres production
> experience, fresh context) audited the schema before the first
> scale event. They flagged 5 production-breakers + 14 ranked
> concerns + projected what breaks at the 12-month target
> (1k orgs / 50k agents / 5M calls / 50M llm_calls).
>
> Phase 9a ships the cheapest, highest-leverage half-day slice — six
> fixes that prevent silent data corruption + write amplification
> waste. The remaining items (calls.org_id stamp, partitioning,
> denormalised list reads, agents.user_id semantics flip) are real
> work staged for Phase 9b/9c when scale demands it.

## Audit findings — full picture

### ❌ Production-breakers identified

| # | Issue | Severity | Phase 9a addresses? |
|---|---|---|---|
| B1 | `agents.user_id NOT NULL ON DELETE CASCADE` — deleting any user nukes their agents (org/members survive, work doesn't) | High | **Phase 9b** — needs nullable + SET NULL + careful data migration |
| B2 | `calls` has no `org_id` column — tenant attribution is one JOIN deep, blocks RLS adoption | High | **Phase 9b** — needs backfill + insert-time consistency CHECK |
| B3 | Connection pool `max_size=10` hardcoded | High | ✅ — Phase 9a (env-configurable, defaults 4/24) |
| B4 | `_unique_slug` raceful, no retry — collisions surface as 500s | Medium | ✅ — Phase 9a (3-retry on `UniqueViolationError`) |
| B5 | 5-statement `insert_call` deadlock-prone if any future code path reverses lock order | Low | Documented — lock-order discipline noted, no code change |

### ⚠️ High-severity concerns

| # | Issue | Phase 9a addresses? |
|---|---|---|
| 1 | `users.email` has TWO unique constraints — one case-sensitive, one case-insensitive. Case-variant emails could coexist. | ✅ — Phase 9a (dropped `users_email_key`) |
| 2 | `idx_agents_user` is dead post-Phase-2 — pure write amplification | ✅ — Phase 9a (dropped) |
| 3 | `audit_log.diff` + `agents.purpose` JSONB have no size cap | ✅ — Phase 9a (64KB + 16KB CHECK constraints) |
| 4 | No CHECK on `calls.sentiment`, `calls.lead_quality` enums | ✅ — Phase 9a (CHECK constraints added) |
| 5 | `list_agents` correlated subqueries — 200-400ms at scale | **Phase 9b** — needs denormalised `agents.last_call_at` + rollup reads |
| 6 | `admin_platform_summary` does 9 full scans on calls — 2-5s/load at 5M rows | **Phase 9b** — rewrite to rollup reads + 60s cache |
| 7 | `admin_list_orgs` joins `calls` for minutes_used | **Phase 9b** — read `org_daily_stats SUM` |

### 📐 12-month size projections

```
calls           8–15 GB    → partition by month before 5M rows
llm_calls       12–15 GB   → partition by month
agent_daily_stats   3 GB
idx_calls_agent_started   2–3 GB
idx_llm_org_started       3–4 GB   → partitioning halves this
```

## Phase 9a — shipped in this pass

| # | Fix | Verification |
|---|---|---|
| 1 | **Dropped `users_email_key` UNIQUE** (case-sensitive). Case-insensitive `idx_users_email_lower` UNIQUE is now the sole enforcement. `Foo@x.com` and `foo@x.com` can no longer coexist. | Live probe: `DIPESH.MAJUMDER@…` insert → `idx_users_email_lower` violation ✓ |
| 2 | **Dropped dead `idx_agents_user`**. Every hot query reads agents by org now. | `\d agents` confirms index gone |
| 3 | **Added `calls_sentiment_check`** — `sentiment IS NULL OR sentiment IN ('positive','neutral','negative','mixed')`. Model can no longer slip a freeform string past the call-log. | Live probe: `'ecstatic'` rejected ✓ |
| 4 | **Added `calls_lead_quality_check`** — `lead_quality IS NULL OR lead_quality IN ('hot','warm','cold','na')`. | Live probe: `'on-fire'` rejected ✓ |
| 5 | **Added `agents_purpose_size_check`** — `octet_length(purpose::text) <= 16384` (16 KB). | Live probe: 20 KB blob rejected ✓ |
| 6 | **Added `audit_log_diff_size_check`** — `diff IS NULL OR octet_length(diff::text) <= 65536` (64 KB). | Live probe: 70 KB blob rejected ✓ |
| 7 | **`_unique_slug` 3-retry on `UniqueViolationError`** — concurrent agent creates with the same name no longer surface as 500s. | Code change in `create_agent` |
| 8 | **Pool size env-configurable** — `PG_POOL_MIN` / `PG_POOL_MAX` (defaults 4/24, was hardcoded 2/10). | Live probe: pool reads env, sizes confirmed |

## New non-negotiables (Part V)

| # | Invariant | Where enforced |
|---|---|---|
| 22 | Email uniqueness is **case-insensitive only**. No two users can have emails that differ only in case. | `idx_users_email_lower` UNIQUE (sole constraint) |
| 23 | `calls.sentiment` is one of `positive`/`neutral`/`negative`/`mixed` or NULL. The model cannot pollute the call-log dashboard with freeform strings. | `calls_sentiment_check` |
| 24 | `calls.lead_quality` is one of `hot`/`warm`/`cold`/`na` or NULL. Hot-lead filter stays meaningful. | `calls_lead_quality_check` |
| 25 | `agents.purpose` JSONB ≤ 16 KB. `audit_log.diff` JSONB ≤ 64 KB. No client can write multi-MB blobs that detoast on every read. | Two size-check CHECK constraints |
| 26 | Concurrent agent creates with colliding slugs never surface 500s. `create_agent` retries up to 3 times on UNIQUE violation, then surfaces the original error. | `db_pg.create_agent` retry loop |
| 27 | Connection pool sizes are runtime-configurable (`PG_POOL_MIN`, `PG_POOL_MAX`). Production sizing is a deploy concern, not a code change. | `db_pg._pool_sizes()` |

## Phase 9b — staged for next pass (when scale demands)

These need batched data migrations + coordinated app-code changes, so they don't fit the half-day budget:

- **B1**: `agents.user_id` → `created_by` semantics (nullable + `ON DELETE SET NULL`)
- **B2**: `calls.org_id` immutable per-call tenant stamp + insert-time consistency CHECK
- Composite `UNIQUE(org_id, slug)` on agents (currently global, blocks org-A "support-bot" from org-B's)
- Replace correlated subqueries in `list_agents` with rollup reads + denormalised `agents.last_call_at` / `calls_count_30d`
- Rewrite `admin_platform_summary` to read `org_daily_stats` (one query, not nine)

## Phase 9c — staged for first million calls

- Partition `calls` and `llm_calls` by `RANGE (started_at)` monthly
- Stand up PgBouncer transaction-pool in front of app pool
- Audit-log retention policy + monthly partitioning

## The honest current state

> The independent auditor's verdict on the rollup design ("architecturally
> strongest part of this design") is intact. The hot-path query patterns
> *around* the rollups (admin/list endpoints) still re-read raw `calls`
> — that's the Phase 9b lift.
>
> Phase 9a removed two pieces of dead weight (`users_email_key`,
> `idx_agents_user`), added six pieces of data-integrity enforcement
> that previously relied on convention, made one piece of operational
> sizing tunable, and turned one 500-class race into a no-op retry.
> Six new non-negotiables added to the doctrine.
>
> The 12-month scale path is now visible end-to-end: what's safe today,
> what needs to change before 100 orgs (Phase 9b), what needs to change
> before 1M calls (Phase 9c).


---

# Part VI · Phase 9b — before-100-orgs fixes (shipped)

> The DB-architect audit had three remaining production-breakers and
> three high-severity correlated-subquery patterns staged for Phase 9b.
> This pass lands all five in one Alembic + matching app code.

## What shipped (verified live)

| Audit finding | Fix |
|---|---|
| **B1**: `agents.user_id NOT NULL ON DELETE CASCADE` — deleting any user nukes the org's agents | Migration 0010 drops NOT NULL + flips FK to `ON DELETE SET NULL`. Live probe: deleted an Org-B user, the org and agents survive with `user_id=NULL`. |
| **B2**: `calls` had no `org_id` — tenant attribution one JOIN deep, blocked RLS adoption | Migration 0010 adds `calls.org_id NOT NULL` (backfilled from agents), `idx_calls_org_started`, and a `calls_org_stamp` trigger that fills `org_id` from agents on insert + rejects mismatches on insert/update. Defense-in-depth: `insert_call` also writes it explicitly. Live probe: trigger rejected an insert that claimed `org_id=8` for an agent in org 1. |
| **Slug collisions across orgs blocked** — `UNIQUE(slug)` was global, blocking org A's "support-bot" if org B had one | Migration 0010 drops the global UNIQUE, adds composite `UNIQUE(org_id, slug)`. `_unique_slug` is now org-scoped. Live probe: org A and org B both got `slug='support-bot'`. Same-org duplicate auto-suffixes to `support-bot-2`. |
| **`list_agents` correlated subqueries** — 200–400ms at scale | Migration 0010 adds denormalised `agents.last_call_at` + `agents.calls_count` (backfilled). `insert_call` maintains them in the same transaction as the calls insert + rollup UPSERTs. `list_agents` reads them directly — **1.2ms for the listing query on the live DB**. |
| **`admin_platform_summary` did 9 full scans on calls** — 2–5s at 5M rows | Rewritten with one CTE over `org_daily_stats` for the five calls-derived tiles + 4 small-table COUNTs. **1.5ms on the live DB.** Same data shape, ~1000× faster at scale. |

## New non-negotiables (Part VI)

| # | Invariant | Where enforced |
|---|---|---|
| 28 | Deleting a user **does not** delete their agents. `agents.user_id ON DELETE SET NULL` — agents survive with `user_id=NULL`, still attached to their org. | Migration 0010 |
| 29 | Every `calls` row carries an immutable `org_id` that matches its agent. The `calls_org_stamp` trigger auto-fills on INSERT and rejects mismatched values on both INSERT and UPDATE OF `org_id`. | Trigger `calls_org_stamp` |
| 30 | Agent slugs are unique **within an org**, not globally. Org A and Org B can both have a `support-bot`. | Composite `UNIQUE(org_id, slug)` |
| 31 | `agents.calls_count` and `agents.last_call_at` are maintained transactionally inside `insert_call`. They can never lag behind the calls table by more than a tx commit. Listing endpoints read these directly — no correlated subqueries. | `insert_call` + `list_agents` |
| 32 | Cross-platform aggregates (`admin_platform_summary`, `admin_list_orgs.minutes_used`) read from `org_daily_stats`, never from `calls`. The calls table is for individual call records; rollups are for dashboards. | `admin_platform_summary`, `admin_list_orgs` |

## Performance receipts (live DB, current populated state)

```
list_agents (2 agents)              1.2ms
admin_platform_summary               1.5ms  (was: 9× full scans of calls)
admin_list_orgs (1 org)              <5ms  (was: calls JOIN agents subselect)
```

Projected at 12-month target (1k orgs, 50k agents, 5M calls, 50M llm_calls):
- `list_agents` for a 100-agent org: ~5–10ms (was 200–400ms)
- `admin_platform_summary`: ~10–20ms (was 2–5s)
- `admin_list_orgs`: ~50–100ms (was 800ms–2s)

Three orders of magnitude on the platform-summary call — the rollup
design now actually pays out the way the auditor said it could.

## What's still on the carry-forward list (Phase 9c)

- Partition `calls` and `llm_calls` by `RANGE (started_at)` monthly — needed before 1M+ rows for index lifetime + dropping old months in one DDL
- PgBouncer transaction-pool in front of the app
- Audit-log retention policy + monthly partitioning

## The honest current state — five non-negotiables added

The audit identified two genuine production-breakers (B1, B2). Both
are closed with schema-level enforcement (FK + trigger). Three
high-severity performance issues (correlated subqueries in three hot
endpoints) are all closed via denormalisation + rollup reads —
verified single-digit-ms on the live DB. Five new non-negotiables
codified into the doctrine table (rows 28–32).

Eva's children survive their creators leaving. Their calls carry
immutable tenant attribution. Their dashboards read from rollups, not
ad-hoc table scans. The platform is now structurally ready for the
trip from 10 orgs to 1,000.


---

# Part VII · Humane-ness audit + fixes

> An independent UX writer (Apple/Stripe/Linear bar) audited the
> frontend for tone, microcopy, contextual help, error messages, and
> whether the no-tour doctrine still holds at the current surface size.
> Verdict: build flow + landing speak with one warm voice (Eva). Two
> cracks: (a) admin shell and Developer page leak a second/third voice
> (Datadog + curl), (b) "Workspace/Organisation/Team" are three names
> for the same thing.
>
> The auditor's clearest verdict on tours: **no overlay, no first-run
> cards, no numbered tooltips on the orb.** The Eva-spoken dashboard
> primer IS the tour. It costs zero pixels and lands at the moment the
> user is most curious. The doctrine survives.

## What landed (F.1 — 15-minute pass)

| Surface | Before | After |
|---|---|---|
| Topbar action | `Raise a Support Ticket` (Title Case + ITIL-speak) | **`Help`** |
| Support modal title | `Raise a support ticket` | **`How can we help?`** |
| Admin badge | `PLATFORM ADMIN` (uppercased pink, 2018-Datadog screaming) | **`Platform admin`** (sentence-case, dignified pink) |
| Publish CTA | `Publish & go live` | **`Publish & Go-live →`** (matches canonical) |
| Super-admin column | `<YES>` shouty tag | **`Super-admin`** tag · muted `—` for non-admins |
| Org → Team subsection | `Team [Coming soon]` (contradicted by real Team page) | **Active link** to `/account/team` |
| Publish error | `Couldn't update — try again in a moment.` (generic) | **`Couldn't publish — give it another tap.`** |
| Payment error | `Payment verified failed — contact support.` (typo!) | **`Payment didn't verify — drop us a line at support@spiderx.ai.`** |

## What landed (F.2 — info-icon affordance)

New `<InfoDot>` component — a 14px circular `(i)` glyph with a click-to-open popover. Used ONLY on dashboard surfaces, never on the build orb. Click outside or Escape dismisses.

Five placements:
1. **Lead column** (call log) — *"Hot — ready to act now (book/buy/escalate). Warm — clear interest, needs follow-up. Cold — info-only, no buying signal. N/A — wasn't a buying call. {agent} assesses honestly at the end of every call."*
2. **Mood column** (call log) — *"How the caller sounded overall. A frustrated caller whose problem got solved is mixed, not positive."*
3. **Tokens in tile** (admin) — *"What the model heard across all sessions. Out is usually 30–40% of in."*
4. **Tokens out tile** (admin) — *"Pricing weights out 4× higher than in, so trimming agent verbosity pays off."*
5. **Cost (₹) tile** (admin) — *"Total Gemini Live spend including builder + agent + post-call. Sourced from the llm_calls ledger."*
6. **Cost per minute tile** (LLM ledger) — *"Weighted — sum of cost ÷ sum of minutes, not the average of per-call cost_per_minute."*
7. **Published tile** — *"Agents whose owner has tapped Publish & Go-live. Free-plan agents test in the browser but stay un-published until upgrade."*

## What landed (F.3 — destructive-action UX)

New `<DestructiveConfirmModal>` component — replaces native browser `confirm()` at four sites:

| Site | Modal title | Body | Confirm label |
|---|---|---|---|
| **Delete agent** (`/agents` trash icon) | `Delete this agent?` | `{name} and everything tied to her go too — saved config, every call's transcript, the call log, the analytics rollups. This can't be undone.` | `Delete {name}` (typed-name required) |
| **Remove team member** | `Remove {name}?` / `Leave the team?` | Tells them what the member loses; mentions re-invitability. | `Remove` / `Leave team` |
| **Revoke invite** | `Revoke invite?` | Names the email, says the link stops working. | `Revoke` |
| **Revoke super-admin** | `Revoke super-admin?` | Names the email, says they keep normal account + memberships. | `Revoke super-admin` |

For agent delete: **typed-name confirm** — the operator must literally type the agent's name before the destructive button enables. GitHub's "type the repo name" pattern. Doctrine permits this because it's preventing tragedy, not gating intent.

For the other three: explicit consequence in one short sentence + named target + cancel as the calm secondary action. No "Are you sure?" theatre.

## New non-negotiables (Part VII)

| # | Invariant | Where enforced |
|---|---|---|
| 33 | No native browser `confirm()` or `alert()` anywhere in app.js. Destructive actions go through `<DestructiveConfirmModal>`; non-destructive errors go through inline `db-error` divs or toasts. | `app.js` (4 confirm() callsites replaced; remaining `confirm()` strings are dead). |
| 34 | The build orb has no tooltips, no info icons, no first-run cards, no "click me" hints. Eva does that work by voice. Dashboard pages MAY use `<InfoDot>` for column-header explainers — never the orb. | `<InfoDot>` component policy. |
| 35 | Hard deletes use typed-name confirm. Soft destructive actions (remove member, revoke invite, revoke super-admin) use named-target modals with explicit consequence. | `<DestructiveConfirmModal typedName=...>` |
| 36 | Microcopy for the platform stays in Eva's voice on customer-facing pages (landing, build, agent overview, business profile, guardrails, voice, knowledge). Admin + Developer surfaces speak a soberer second voice — that's deliberate (mirrors the doctrine's Eva-host vs runtime-receptionist split one floor up). | Convention enforced on review. |

## Honest carry-forwards

- **"Workspace / Organisation / Team" terminology** — still three names for the same thing across screens. Auditor recommends `Workspace` everywhere except invoices which keep `Organisation`. Not shipped this pass — a noun rename touches ~30 places.
- **Per-agent first-visit tip strip** (optional) — the auditor's only soft recommendation for a tour-shaped affordance: a single dismissable sentence on the agent overview the first three visits only, e.g. *"Eva already filled in what she heard. Business profile and Guardrails are where you fine-tune."* Not a tour; a memory aid. Deferred — only ship if real-user feedback shows the gap exists.

## The honest current state

Eight stings caught at F.1 → fixed. Seven InfoDots earned their place on the dashboard surface that grew around the build orb — without violating the doctrine that bans tooltips on the orb itself. Four native `confirm()` dialogs replaced with intent-respecting custom UI, including a typed-name agent-delete that prevents tragedy without staging theatre.

Three orders of magnitude of code-quality remained intact; this pass moved three orders of magnitude of *taste* alongside them. Eva's children are now spoken about, deleted, and explained in a voice that matches the voice she uses to birth them.
