# Eval Rubric — Tester Feedback

> **Maintenance rule (hard):** update this file on **every push** that changes
> behaviour. Per affected item record: acceptance criterion, **verdict**
> (PASS / PARTIAL / OPEN), **evidence tier**, and the **build** it shipped in.
> Bump "Last updated" below. See `CLAUDE.md` → Hard rules.

**Last updated: build 309**

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
| 12 | Bot holds context; doesn't repeat the caller's last question | OPEN | — | — | conversation-bridge logic; too risky to patch blind. **Needs a failing-call transcript** |
| 13 | Recording plays back (not a dead 0:00 player) | PARTIAL (code root-caused; prod fix is infra) | Behavioral+Instrumented | 305, 307 | **Code path proven correct** — a real local call writes healthy WAVs (caller ~180 KB, agent ~440 KB, mixed ~890 KB) that play back. So the prod 0:00 player is a **storage-persistence gap**, not a capture bug. 307: detail endpoint now gates `recording_available` on the file *actually on disk* (`recordings.usable_capture_bytes`), not the DB size column that outlives a wiped file → a missing recording shows "Recording file is missing from storage — it may not have been persisted on this deployment" instead of a dead player; **loud boot warning** `recordings.EPHEMERAL_STORAGE` when on Railway but resolved to the ephemeral `data/recordings`. **Remaining for playback in prod: mount a persistent volume / set `RECORDING_DIR`** (infra, not code) |
| 14 | CSV export opens cleanly in Excel | PASS | Unit | 303 | RFC-4180 escaping + BOM + CRLF + more columns |
| 15 | No duplicate "Close" controls in the outcomes editor | PASS | Behavioral | 304 | live: "− Hide outcome form" / "− Hide kind form", no double Close |

---

## Outstanding
- **#12** — paste the transcript turn where she repeats the caller's question.
- **#13 (prod infra, not code)** — recordings persist correctly in code (verified by a real local call). For playback to work in prod, confirm the deploy's recordings root is a **persistent volume**: check the boot log for `recordings.root resolved to …` (and the new `recordings.EPHEMERAL_STORAGE` warning), then set `RECORDING_DIR` to a mounted path (or attach a volume so `RAILWAY_VOLUME_MOUNT_PATH` resolves). Recordings written before the volume existed are unrecoverable.

## Score
13 of 15 closed (PASS). 1 PARTIAL pending audition (#9). 1 OPEN (#12). #13 code-complete; prod playback pending a persistent-volume config.
