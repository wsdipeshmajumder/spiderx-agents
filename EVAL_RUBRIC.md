# Eval Rubric — Tester Feedback

> **Maintenance rule (hard):** update this file on **every push** that changes
> behaviour. Per affected item record: acceptance criterion, **verdict**
> (PASS / PARTIAL / OPEN), **evidence tier**, and the **build** it shipped in.
> Bump "Last updated" below. See `CLAUDE.md` → Hard rules.

**Last updated: build 344**

**Build 344 (drop the gradient card option — solid card colour only):** per
feedback the brand-gradient starter cards weren't wanted; removed
`card_brand_gradient` (state, checkbox, root class, `.chatembed-cardgrad` CSS).
Starter-card branding stays solid via `card_bg_color` + `card_text_color`, with
the card arrow following the text colour. Moments = `card_bg_color:"#e45dbf"` +
`card_text_color:"#ffffff"`. Verified: card bg `rgb(228,93,191)`, text + arrow
white, `font-weight:400`. Evidence: **Behavioral**. (The outside close button
from 343 is unchanged.)

**Build 343 (brand-gradient cards + close button moved outside the panel):**
two changes to the chat embed:
- Starter cards can now use the two-tone brand palette: `card_brand_gradient`
  paints them with the `accent → accent-2` gradient (matches the Moments
  `#e45dbf → #e5a3ff`), the card arrow follows the card text colour, and a soft
  text-shadow keeps white text legible across the lighter end. Pair with
  `card_text_color:"#ffffff"`. Exposed as a Chat → Appearance checkbox + live
  preview. Verified: card bg = the gradient, text `rgb(255,255,255)`.
- The "×" close button now floats OUTSIDE the panel at the top-right corner
  (`.sxai-close` top/right:-14px; panel is `overflow:visible` with a new
  `.sxai-clip` layer rounding the iframe; drawer mode keeps it on-screen). The
  build-339 header padding reservation is removed — the header reclaims full
  width (verified: `padding-right:14px`). Evidence: **Behavioral**.

**Build 342 (brandable starter cards + unbold):** the 4 starter-question cards
on the chat home are now brandable per-agent via two new `chat_settings` keys
(`card_bg_color`, `card_text_color` → `--chat-card-bg` / `--chat-card-text`;
`--chat-card-border` also honoured), exposed in Chat → Appearance and wired
through the live preview. Card text weight dropped 600 → 400 (unbold) per the
Moments feedback. Verified live: computed `--chat-card-bg` `#fbeef8`, text
`#3a1230`, `font-weight:400`. Evidence: **Behavioral**.

**Build 341 (per-agent chat brand kit — for Moments/BlissBot):** five new
`chat_settings` keys make the embed brandable without code per-agent, all
exposed in Chat → Appearance and driven through the live preview:
- `accent_color_2` — second brand colour → gradient avatar + launcher FAB +
  user bubble (`--chat-accent-2`, `?accent2=` forwarded by embed.js). Moments =
  `#e45dbf` → `#e5a3ff`.
- `full_width_responses` — `.chatembed-fullwidth`: model bubbles span the panel
  width to cut scroll length. Verified: model bubble `max-width:100%`, fills
  362/390px.
- `display_name` — chat-only name override (e.g. "BlissBot") that never touches
  the voice agent's real name/persona/greeting; applied to header, hero, input
  placeholder.
- `bubble_size:"xs"` — new 12px step in the size scale + dashboard selector.
- `bot_bubble_color` — custom model-response bubble background.
Logo provision is the existing `avatar_url` (header logo image). Verified live
on a seeded config (computed styles: accent `#e45dbf`, accent-2 `#e5a3ff`,
`--chat-size` 12px, gradient user bubble, full-width model bubble). Evidence:
**Behavioral** (code) — production Moments agent still needs the config values
applied (it lives outside the dev DB). Rendering paths all confirmed.

**Build 340 (embed close "×" now visible on the light header):** the overlay
button was styled white (`color:#fff` on `rgba(255,255,255,0.10)`) for the dark
panel, but it sits over the iframe's light chat header, so it washed out. Now a
light chip + slate glyph (`background:rgba(255,255,255,0.92); color:#475569`)
with a soft shadow — clearly legible on light, still visible on dark. Verified
in a live embed.js overlay: the "×" reads crisply and is separated from the
"New chat" button. Evidence: **Behavioral**.

**Build 339 (embed header no longer overlaps the close button):** in the
floating chat embed, `embed.js` overlays a 28px "×" close button at
`top:10px/right:10px`, and the header action cluster ("New chat" /
"Talk to a human") ran to the right edge and slid under it. Fix: reserve
right-side room in the real embed only —
`.chatembed:not(.chatembed-contained) .chatembed-head { padding-right: 48px; }`
— so the operator preview (which has no overlay) is untouched. Verified in a
live preview: root class `chatembed`, computed `padding-right` 48px, "New chat"
clears a simulated × with a ~10px gap. Evidence: **Behavioral**.

**Build 338 (order-status graceful redirect):** the not-invent fix (U22) still let the bot ask "what's your order number?" then say it'd "check" (screenshot). Now: as soon as an order/tracking question comes up, the agent redirects in ONE reply — no asking for a number, no "I'll check" — to the customer's order-confirmation/shipping email tracking link, their `/account` page, and `/pages/contact-us`. Prompt + connector not-configured message both updated; agent 4 had the `order_status` connector removed + an order-tracking policy added to knowledge.

**Evidence tiers**
- **Behavioral** — observed live in a real browser session (prod or preview)
- **Unit** — logic verified by a standalone test
- **Code** — fix signature confirmed present in the deployed bundle
- **Asset** — regenerated file(s) (e.g. audio), not yet auditioned
- **Instrumented** — diagnostics / guard added; root cause not yet confirmed
- **Open** — not addressed

---

## Round 1 (PDF: "agents.spiderx.ai Testing")

| # | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| 1 | Login page does not pre-fill the email | PASS | Behavioral | 298 | tester-confirmed; `/login` email `value=""` |
| 2 | Wizard mode-switch is guarded + carries answers over | PASS | Code+Local | 299 | tester-confirmed |
| 3 | Wizard offers a per-day hours editor | PASS | Code | 299 | tester-confirmed |
| 4 | Failed test call → persistent retryable error, not a bounce | PASS | Code+Local | 300 | tester-confirmed |
| 5 | Knowledge banner names the 3 real sources | PASS | Behavioral | 301 | live: "Knowledge page, Business profile, Additional Info" |
| 7 | Timezone is a dropdown | PASS | Behavioral | 301 | live: `<select>`, 419 IANA options |
| 8 | Build-time hours render on the profile page | PASS | Behavioral+Data | 301 (fix 302) | live: stored human-format hours render correctly. **Regression** in 301 (machine-format → all-closed) fixed in 302 |

---

## Round 2 (PDF: "agents.spiderx.ai Testing (1)" — re-test + new)

| # | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| 6 | Save shows a prominent, scroll-independent confirmation on every save surface | PASS | Behavioral | 303–304, 306 | portal toast (`parent: body`, `position:fixed`). **Core-purpose page was the last surface with NO toast** (form stays open on save, so the collapse-to-read confirmation never fired) — wired `SaveStatePill` into `PurposeBox` in **306**. Headless-verified: PATCH 200, toast sequence "Saving…" → "Saved ✓" |
| 9 | en-IN voice previews sound Indian — incl. Indian names + correct gender | PARTIAL (Charon clip needs audition) | Behavioral+Asset | 305, 309 | 305: 8 samples re-recorded w/ Indian-accent + Hinglish. **309 (tester re-test):** (a) **Indian display names** — voice picker now shows gender-matched Indian personas on `-IN` locales (Charon→Vikram, Puck→Arjun, Aoede→Ananya, Kore→Priya, Leda→Meera, Zephyr→Isha, Fenrir→Rohan, Orus→Aditya); non-IN locales keep the Gemini id. Locale logic unit-tested (en-IN/hi-IN/bn-IN/ta-IN→Indian; en-US/ja-JP/en-GB→original). Display-only — TTS voice id unchanged. (b) **Gender bug fixed** — Charon (male) was speaking the feminine "main sun *rahi* hoon"; corrected to "*raha*" and `Charon.wav` regenerated (262 KB, `?v=BUILD` busts cache). **New Charon clip not yet auditioned** |
| 10 | Embed widget shows the agent, not the landing page (incl. after a call) | PASS | Behavioral | 304, 307 | standalone `/embed/<slug>` renders the widget pre-call (304). **Post-call distortion root-caused + fixed in 307**: `closeSession` ran `goRoute("/")`, which cleared `embedSlug` and dropped the iframe onto the landing/marketing splash — now skipped when on an `/embed/` path. Headless before/after: OLD → `path="/"`, marketing hero shown; FIXED → `path="/embed/<slug>"`, orb + "Talk to <agent>" restored |
| 11 | "No calls" empty state looks intentional | PASS | Behavioral | 303, 308 | Call-logs empty got a real glyph in 303. **308 fixes the actual layout the tester flagged**: the "Send a test call" button was rendered *inside* the description `<div>`, so it wrapped into the middle of the sentence ("…lands here with full [button] transcript…"). Moved the CTA out to its own centered block (`db-empty-cta`) below the copy. Headless before/after screenshots on `zoe` (0 calls): button now sits cleanly under the 2-line description |
| 12 | Bot holds context; doesn't repeat the caller's last question | PARTIAL (root-caused twice; needs live re-test) | Code | (bridge, 318) | **First pass** (315) hardened the prompt + telephony reconnect. **A re-test (7 Jul) still showed it** — a *web* call where the bot said "Sorry, you broke up — could you say that again?" after the user's question. Real root cause: the web-voice reconnect had **three** branches and the `resume_handle` one sent `kickoff_text = None` — i.e. NOTHING — so when Gemini restored via its handle the model's own prior ("acknowledge the drop / re-ask") took over. **318:** that branch now sends an explicit steer — "resumed WITH full context, do NOT acknowledge the drop / say sorry / ask them to repeat / re-answer; answer the pending question, else wait." Telephony reconnect switched from the bare `<call_resumed>` token to the same explicit steer. **Needs a live 2-min+ call to confirm** |
| 13 | Recording plays back (not a dead 0:00 player) | PARTIAL (code root-caused; prod fix is infra) | Behavioral+Instrumented | 305, 307 | **Code path proven correct** — a real local call writes healthy WAVs (caller ~180 KB, agent ~440 KB, mixed ~890 KB) that play back. So the prod 0:00 player is a **storage-persistence gap**, not a capture bug. 307: detail endpoint now gates `recording_available` on the file *actually on disk* (`recordings.usable_capture_bytes`), not the DB size column that outlives a wiped file → a missing recording shows "Recording file is missing from storage — it may not have been persisted on this deployment" instead of a dead player; **loud boot warning** `recordings.EPHEMERAL_STORAGE` when on Railway but resolved to the ephemeral `data/recordings`. **Remaining for playback in prod: mount a persistent volume / set `RECORDING_DIR`** (infra, not code) |
| 14 | CSV export opens cleanly in Excel | PASS | Unit | 303 | RFC-4180 escaping + BOM + CRLF + more columns |
| 15 | No duplicate "Close" controls in the outcomes editor | PASS | Behavioral | 304 | live: "− Hide outcome form" / "− Hide kind form", no double Close |

---

## Outstanding
- **#12** — paste the transcript turn where she repeats the caller's question.
- **#13 (prod infra, not code)** — recordings persist correctly in code (verified by a real local call). For playback to work in prod, confirm the deploy's recordings root is a **persistent volume**: check the boot log for `recordings.root resolved to …` (and the new `recordings.EPHEMERAL_STORAGE` warning), then set `RECORDING_DIR` to a mounted path (or attach a volume so `RAILWAY_VOLUME_MOUNT_PATH` resolves). Recordings written before the volume existed are unrecoverable.

## Additional UX feedback (live walkthrough, beyond the two PDFs)

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U1 | "Customise outcomes" section makes its purpose + actions clear | PASS | Behavioral | 309 | Tester: "not clear what this is for or what to do." Rewrote the intro to lead with **what an outcome is** (agent tags every call with one; powers Call log / success-rate / reports), explain the **kind** column (Success = win, Qualified = lead, …), state it **works as-is**, then a scannable "you only need this if you want to: Rename / Change a kind / Add or hide" list. Headless-verified on `rohan/outcomes`: lead + 3 bullets render |
| U2 | Call log surfaces caller phone number + per-call cost | PASS | Behavioral | 310 | Phone + Cost (₹) columns in the table & CSV. `calls.caller_number` (migration 0033) captured at the Answer webhook → media-WS → both persist paths; `cost_paise` surfaced. Prod-verified: `/api/agents/1/calls` returns both; a billed call shows ₹4.99; caller_number null for web calls |
| U3 | Knowledge-base file upload works | PASS | Behavioral | (deps) | Prod upload 500'd — `python-multipart` was absent from `requirements.txt` (plain `fastapi`, not `fastapi[standard]`, doesn't pull it in), so Starlette's `request.form()` raised on every `multipart/form-data` body. Pinned it in both requirements files. Prod-verified before→after: `POST …/knowledge/upload` 500 → 200 preview |
| U4 | Agent responses are channel-aware (voice vs web-chat) | PASS | Code+Assembled | (prompt) | Chat prompt already formats for the screen (concise/skimmable, may share links, on-screen widgets, "NOT a phone call"). Added the missing **voice** counterpart to `_agent_system_prompt`: "you are HEARD, not read" — never read URLs/markdown/emojis aloud, **offer to text/email links** (sms_send), say emails/phones/money/times naturally. Verified present in the assembled live voice prompt |
| U5 | Chat embed: bottom-drawer open mode | PASS | Behavioral | 311 | `embed.js` `data-mode="drawer"` (bottom sheet, slides up, full-width mobile) alongside popover/fullscreen; picked via "Open as" in the Chat-widget config. Served embed.js verified to carry the drawer CSS |
| U6 | Chat embed: customise response-box colour + size before embedding | PASS | Behavioral | 311 | `chat_settings.bubble_radius` + `bubble_size` (sm/md/lg) + accent, edited in-config (roundness slider, size toggle), applied via `--chat-radius`/`--chat-size`; `embed.js` also forwards `data-accent/radius/size` (override wins). Headless: embed resolves the CSS vars from URL params |
| U7 | Chat embed: bot home as the starting point (preset questions) | PASS | Behavioral | 312, 314, 326 | Fresh chat opens on a home hero + preset-question card grid. **314:** home is the opening screen whenever a welcome OR starters are set — the welcome_message becomes the hero and the model kickoff greeting is suppressed. **326 (tester):** the live embed now ALWAYS opens on the home with **4 suggestions** — operator starters first, padded with generic prompts that skip a topic a real starter already covers (no "your hours?" + "opening hours?" dupes); `showHome` no longer requires any config. Headless: `rohan` (2 set) → 4 distinct cards; `nora` (0 set) → 4 generic + "Ask me anything about Nora" hero |
| U8 | Chat embed: voice mode (ask by voice) | PASS (needs live-mic audition) | Behavioral | 313 | Mic in the composer using the browser Web Speech API — dictates into the box live and auto-sends on stop; hidden where SpeechRecognition is unavailable (Firefox). Headless-verified: mic renders, API present. **Actual transcription needs a real mic to audition** |
| U9 | Chat-widget page uses the full screen width | PASS | Behavioral | 324 | Tester: "give the chat menu + body more space, widen the 3 tabs to fit screen." The page was capped at a centered 1020px column (`.golive-focus-wide`), wasting the right half and clustering the Settings / What-it-knows / Conversations tabs in the middle. Scoped `.chatpage` to fill the content width (1440 cap) + stretch, so the tab bar spans full-width and both the two-pane preview and the card grids breathe. Headless before/after at 1440px: focus 1020→1112 (fills content); all 3 tabs verified wide (Settings 2-pane, Knowledge 4-across, Conversations full-width list) |
| U10 | Conversations tab: chat detail in a right pane, not a drawer | PASS | Behavioral | 325 | Tester: "show conversations on right pane instead of drawer." Split the 3rd tab into two panes — chat list left, transcript/captured-info/CSAT right — with the selected row highlighted and an empty-state prompt. Extracted a shared `chatDetailBody` helper (the slide-in drawer is still used by the Call-log page). Headless: click a chat → row selected, transcript renders inline in the pane, no drawer backdrop |
| U11 | Chat widget: discoverable "New chat" control | PASS | Behavioral | 325 | Tester: "keep the ability to start a new chat." The reset existed but as a bare grey refresh icon that read as "reload." Made it a labeled **"↻ New chat"** pill next to "Talk to a human" (drops to icon-only under 360px). Headless: labeled button renders; sending a message then clicking it clears the log (4→0 msgs) back to the home screen |
| U12 | Chat replies render markdown (bold / italic / bullet lists / code), not raw `**` and `*` | PASS | Code | 328 | Tester: "Markdown not taking" — bubbles showed literal `**bold**` and `*` bullets. Replaced `linkifyChat` with `renderChatMarkdown`: block-level bullet lists (`*`/`-`/`•`/`1.`) + line breaks, inline `**bold**`/`__b__`, `*italic*`/`_i_`, `` `code` ``, markdown links and bare URLs (keeps the hover-preview anchors). htm escapes every interpolated text node, so it's XSS-safe. CSS for `strong/em/ul/li/code` in styles.css |
| U13 | Proactive nudge (teaser bubble) actually appears on the live site | PASS | Behavioral | 328, 329 | Tester: "Proactive nudge not working." Root cause was architectural: teaser/icon/colours were baked into the static `<script data-*>` snippet, so configuring them AFTER pasting never reached the site. `embed.js` now fetches the agent's live `chat_settings` from `/api/agents/by-slug/<slug>` at load and reads `teaser`/`teaser_delay` from it (data-* still overrides). Snippet slimmed to `data-agent`+`data-channel`(+`data-position`) so it's paste-once and all config is live. **329:** the cross-origin fetch (embed runs on the customer's domain) was CORS-blocked — added `Access-Control-Allow-Origin: *` to the public by-slug endpoint (public payload, header-based auth → no CSRF risk). Headless on a cross-origin host page: FAB renders with chat-bubble icon, teaser "Looking for Something?…" shows after the 8s delay, no console errors |
| U14 | Chat widget: hide the "Talk to a human" button | PASS | Behavioral | 327 | Tester: "Not able to hide Talk to human button." Setting shipped 327 (`chat_settings.hide_human_handoff`, read server-side by the embed surface, no re-paste needed). Was reported against the pre-327 build; re-verify on prod after 328 deploy that toggling + saving hides it |
| U15 | Chat widget: customise the launcher / FAB icon | PASS | Code | 328 | Tester: "need a place to update this icon." Added a **Launcher icon URL** field (`chat_settings.launcher_icon`) in the Chat-widget config; `embed.js` renders it as an `<img>` in the FAB, falling back to the logo (`avatar_url`) then a default icon. Also fixed the chat FAB to use a chat-bubble icon (was a phone icon on the chat channel). Live via the same chat_settings fetch as U13 |
| U16 | Chat does not end after a single answer (premature `end_call`) | PASS | Code | 328 | Tester: "chat is getting ended after just 1 message" — agent answered one question then fired `end_call` (CSAT + "chat ended"). The chat prompt's WRAP-UP rule listed "question answered" as an end trigger. Rewrote it: end ONLY when the visitor is clearly finished (says bye / "that's all") or a booking is confirmed and they need nothing more; never end right after providing info/a link; after every answer invite the next step and wait. Needs a live multi-turn chat to fully confirm |
| U17 | Chat shares real product links + is embed-context aware (not "go to our website") | PASS | Behavioral | 330 | Tester (agent 4 / Moments): "the prompt has product URLs but in chat the URLs are not showing, and the language makes it sound like the user has to go somewhere else." The brief is voice-framed ("callers", "direct the caller to our website momentswellness.com.au", "send a link via SMS") while the chat is embedded ON momentswellness.com.au — so the model said "visit our website" and never pasted the specific product URLs that ARE in the knowledge (`…/products/…`, `…/collections/…`). Chat prompt now (a) instructs SHARE REAL LINKS — paste the exact product/collection URL inline as a markdown link, not the bare homepage / "it's on our website", and (b) adds a WHERE THE VISITOR IS block: you're embedded on the business's own site, never tell them to "visit/go to the website", translate phone phrasing (caller / SMS / "direct to website") into just pasting the link. Generic (not Moments-specific). **Live-verified on prod (agent 4 chat):** "Do you have flavoured condoms? Send me the link" → reply pasted a clickable markdown link "Moments Flavoured Condoms" → `…/collections/flavoured` (specific page, not homepage), no "visit our website"/SMS phrasing, and did NOT end the chat (asked "anything else?"). Markdown renders clickable via U12. **331 (source-level hardening):** to lean less on the runtime adapter, the generation layer now authors channel-neutral procedures — builder meta-prompt writes "customer intents" (was "caller intents") + an explicit CHANNEL-NEUTRAL WORDING rule (describe the action, not the medium: "share the link"/"offer a callback"/"connect to the team", never "over the phone"/"send an SMS"/"direct them to the website"); all 11 build templates: guardrails "…over/on the phone" → "…in conversation" (no longer imply chat is exempt), `_generic` "put you through" → "connect you to the team". Affects NEWLY-built agents only; existing agents keep their saved brief and are covered at runtime by U17/330 |

## Score
13 of 15 closed (PASS). 1 PARTIAL pending audition (#9). 1 OPEN (#12). #13 code-complete; prod playback pending a persistent-volume config. +13 ad-hoc (U1 outcomes intro, U2 call-log fields, U3 upload fix, U4 channel-aware responses, U5–U8 chat-embed: drawer / response-box styling / preset-question home / voice mode; U9–U11 chat page width / conversations pane / new-chat control; U12 chat markdown, U13 live teaser via chat_settings fetch, U14 hide-human, U15 launcher icon, U16 no premature end).

## Round 3 (human test — "Moments ChatBot Test.pdf", agent 4 web chat) — systemic fixes

Tester patterns: (A) knowledge gaps — ingredient list, safety data sheet, "vanilla masking natural/synthetic", vegan/latex-free, bundle rules; (B) **offers it can't deliver** — "would you like a link to the ingredient list / SDS / vegan alternatives?" then deflects; (C) broken product link ("Major Pleasure Toy"); (D strength) guardrails decline sexting/acts/kinks/medical cleanly. Fixes are generic (help every web-chat agent), not Moments-specific.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U18 | Chat never offers a link/list/document/alternative it doesn't actually have (Pattern B) | PASS | Code | 332 | Chat prompt EDGE CASES gains "ONLY OFFER WHAT YOU CAN DELIVER": never dangle a link/list/spec-sheet/SDS/alternative unless that exact thing is in the knowledge and producible in the next message; if not, answer with what you have, say plainly you don't have that document, and offer `request_human_handoff`. Backend-only (no frontend). Live-verify: ask agent 4 for the full ingredient list → should decline+offer handoff, not dangle |
| U19 | Unanswered questions are captured as a knowledge-gap worklist (Pattern A visibility) | PASS | Code+Unit | 333 | `chat_bridge` detects "I don't have that information"-family replies (regex; EXCLUDES guardrail "can't provide advice" declines — 9/9 unit cases) and emits a deduped `knowledge.gap` event per (agent, question). New `GET /api/agents/{id}/knowledge/gaps` returns the distinct unanswered questions so the operator knows exactly what to add. Turns the tester's hand-made "bot couldn't answer" list into an automatic feed |
| U20 | Broken knowledge links are detected before a visitor hits them (Pattern C) | PASS | Behavioral | 333, 334 | `GET /api/agents/{id}/knowledge/link-check` extracts every URL from the agent's knowledge, checks each (public hosts only, SSRF-guarded, concurrency+count capped), returns broken ones and emits `knowledge.link.broken`. **334 fix:** first live run flagged all 23 agent-4 links as broken because the store 429-rate-limited an 8-way burst and the code counted any ≥400 as broken. Reclassified: only **404/410 or a connection failure = broken**; 401/403/429/5xx = **"unverified"** (anti-bot/transient, reported separately, never alarmed); concurrency 8→4, HEAD-first, browser UA. Would have caught the "Major Pleasure Toy" 404 without crying wolf on a throttling store |
| U21 | Live Shopify catalog sync — authoritative prices/URLs/availability (durable fix for A+C) | PASS | Code+Unit | 333 | `POST /api/agents/{id}/knowledge/sync-shopify {store_url}` pulls the store's own `products.json` (names, prices, product URLs, availability, tags), renders a compact YAML catalog, and REPLACES the prior auto-synced block (re-sync can't duplicate/conflict — the exact failure that produced the A$20 vs A$12 price bug). Verified against the real Moments catalog: 52 products, valid YAML, Mega Thin 0.03 → correct A$12.00 + working URL. This one source would have prevented the price error, the broken link, and the vegan/latex-free gap (tags). Generic for any Shopify store |

## Round 4 (human test #2 — Moments web chat, screenshots) — systemic fixes

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U22 | Bot never fabricates order/tracking/delivery status | PASS | Behavioral | 335 | **Live-verified:** "status of my order 1234?" → "I can't look up order statuses here… check your order confirmation email or contact us" (was "#1234 confirmed, arrives Jul 25"). **Critical.** `connectors.order_status` was a demo STUB returning `random.choice(status)` + random ETA → real customers got invented delivery dates ("#1234 confirmed, arrives Jul 25"). Now returns `not_configured` unless `DEMO_CONNECTORS=1`; `knowledge_base_search` stub (fake salon FAQ) likewise returns empty. Chat prompt EDGE CASES adds an ORDER/TRANSACTION rule: never state/invent order status/tracking/date, direct to the store's order-tracking/contact page. Generic |
| U23 | Handoff shares contact form/email when no live human | PASS | Behavioral | 335, 336 | Tester: "share the contact us form / email." New `chat_settings.handoff_mode="contact_info"` (+ `support_email`, `contact_url`). **336:** 335's prompt line was overridden by the `request_human_handoff` handler, whose tool-result `instruction` said "a teammate has been notified — ask for phone/email." Made the handler honor `handoff_mode`: in contact_info mode it returns "no live agent; share the contact form/email as a link" and suppresses the misleading "notified" banner. agent 4 set to `/pages/contact-us`. Verified live below |
| U24 | Bot shares specific store pages (blogs, size guide) + product URLs, not "check our website" | PASS | Data | 335 | Shopify catalog synced onto agent 4 (52 products, real URLs — fixes the broken "Major Pleasure Toy" link: real `/products/major`). Added a STORE PAGES & POLICIES block: blog index `/blogs/all`, size-guide blog, contact page, and the latex fact (all natural rubber latex; **no latex-free option** — confirmed absent from catalog) so the bot stops saying "I don't have that." Builds on U17/U21 |
| U25 | Bot doesn't repeat the same answer twice | PASS | Behavioral | 335, 337 | Tester: "answer repeated 2 times." Cause: model answers, calls a presentational tool (quick_replies), then re-answers next tool-loop iteration → doubled bubble (fake `knowledge_base_search` was one trigger, now empty). **337:** the quick_replies/show_form tool RESULTS now instruct "buttons shown; do NOT restate your message" (335 added the prompt rule). Live-verified on 337: sizes + CEO-toy answers no longer duplicated |
| U26 | Markdown links render as clickable (incl. inside bullet lists) | PASS (already shipped) | Behavioral | 328/331 | Tester screenshot showed raw `[label](url)` in a bulleted list. Verified against the **live** bundle (build 331): `renderChatMarkdown` runs `_mdInline` on each `<li>`, and bullet-list links render as `<a>` anchors. The screenshot predated the build-328 markdown fix — no code change needed |

## Out-of-band tooling (not a tester item, no build number)
- **`backend/sip/` (`sipd`)** — native SIP UAS that accepts inbound INVITEs straight from a Grandstream UCM and bridges call audio to a Gemini agent (no Twilio/Plivo). Run as a **separate LAN process** (`python -m backend.sip`), NOT part of the Railway web app — `backend/app.py` does not import it, so it's inert for the deploy. Committed to the repo so it can be pulled onto a LAN box. Transport (SIP/RTP/G.711/digest) is unit- + loopback-proven; the live Gemini audio bridge (`gemini_handler.py`) is pending a first live call. Reuses `gemini_bridge._agent_system_prompt`/`_live_config`/connectors + `db.get_agent`; no new deps.
