# Eval Rubric — Tester Feedback

> **Maintenance rule (hard):** update this file on **every push** that changes
> behaviour. Per affected item record: acceptance criterion, **verdict**
> (PASS / PARTIAL / OPEN), **evidence tier**, and the **build** it shipped in.
> Bump "Last updated" below. See `CLAUDE.md` → Hard rules.

**Last updated: build 306**

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
| 9 | en-IN voice previews sound Indian | PARTIAL (needs audition) | Asset | 305 | 8 samples re-recorded w/ Indian-accent instruction + Hinglish; `?v=BUILD` cache-bust. **Not auditioned — needs a human to listen** |
| 10 | Embed widget shows the agent, not the landing page | PASS (pre-call) | Behavioral | 304 | standalone `/embed/<slug>` renders the widget correctly. **In-call distortion NOT re-tested** (publish gate blocks a draft-agent embed call) |
| 11 | "No calls" empty state looks intentional | PASS | Code | 303 | Call-logs empty got a real glyph; not seen rendering (Tara has calls) |
| 12 | Bot holds context; doesn't repeat the caller's last question | OPEN | — | — | conversation-bridge logic; too risky to patch blind. **Needs a failing-call transcript** |
| 13 | Recording plays back (not a dead 0:00 player) | PARTIAL (mitigated, not root-caused) | Instrumented | 305 | near-empty captures dropped → "captured almost no audio"; `finalize` logs `caller/agent/total` bytes. **Root cause needs a real call's Railway log line** (likely volume not mounted) |
| 14 | CSV export opens cleanly in Excel | PASS | Unit | 303 | RFC-4180 escaping + BOM + CRLF + more columns |
| 15 | No duplicate "Close" controls in the outcomes editor | PASS | Behavioral | 304 | live: "− Hide outcome form" / "− Hide kind form", no double Close |

---

## Outstanding (blocked on a real call I can't generate — no mic in automation)
- **#12** — paste the transcript turn where she repeats the caller's question.
- **#13** — run one real call, then share the `recordings.finalize … caller=N agent=N total=N` Railway log line + confirm `RAILWAY_VOLUME_MOUNT_PATH`/`RECORDING_DIR` and that `/files` is a mounted volume.

## Score
13 of 15 closed (PASS). 1 PARTIAL pending audition (#9). 1 OPEN (#12). #13 mitigated.
