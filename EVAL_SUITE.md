# SpiderX.AI — Feature Eval & Rubric Suite

A living catalogue of the app's features, each with an **acceptance criterion**,
an **evidence tier**, and a **verdict**. Most rows are checked automatically by
`tests/eval_suite.py`; the rest are driven by the scenario scripts already in
`tests/`, or are manual UI checks.

This is the *breadth* rubric (does every feature work?). It complements
`EVAL_RUBRIC.md`, which is the *per-build* changelog rubric (what changed and how
it was verified).

## Running it

```bash
# 1. start the server (dev DB, stub auth)
.venv/bin/uvicorn backend.app:app --port 8765

# 2. run the whole suite (exits non-zero on any FAIL)
.venv/bin/python tests/eval_suite.py

# a single area (substring match):
.venv/bin/python tests/eval_suite.py --only security
BASE=http://localhost:8765 UID=1 .venv/bin/python tests/eval_suite.py

# ALSO drive a real chat WebSocket end-to-end (needs GEMINI_API_KEY in the env):
.venv/bin/python tests/eval_suite.py --scenario
```

`--scenario` opens `/ws/session?mode=chat` exactly as the embed does, sends a
question, and asserts: session **ready**, a **model reply**, **follow-up chips**
(build 350 — SKIPped if none arrive in the window, since they're a background
LLM call), and **visitor provenance captured** (build 351 — a mobile User-Agent
→ device, a `?host=` Referer → source, read back off the persisted chat). It
creates one real chat-log row for the agent (expected test data).

The harness authenticates with the passwordless **`X-User-Id`** stub header
(user 1 = the founder / super-admin in dev). Every mutation snapshots the prior
value and restores it, so it is safe to run repeatedly against the dev DB.
Last full run: **43 PASS · 0 FAIL** (13 areas); **+4 with `--scenario`** (live
chat WS) → 47.

## Evidence tiers

- **Automated** — asserted by `tests/eval_suite.py` against the live API.
- **Scenario** — driven by a WebSocket/LLM scenario script in `tests/`
  (`northstar_test.py`, `test_industries.py`, `human_test.py`,
  `interrupt_test.py`, `reconnect_memory_test.py`, `talk_to_agent.py`,
  `talk_to_eva.py`, `roleplay.py`). These need a live Gemini key + a running
  server and are not part of the fast API pass.
- **Manual** — a human checks it in the browser (pixel/interaction detail).

---

## 1 · Platform & meta — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Health | `GET /api/health` → 200 | PASS |
| Build pin | `GET /api/build` returns an integer build (drives the `?v=` cache pin) | PASS |
| Version | `GET /api/version` → 200 | PASS |
| Plan catalogue | `GET /api/plans` → non-empty list | PASS |
| Voice/industry presets | `GET /api/presets` → 200 | PASS |

## 2 · Auth & security — **Automated** (critical)

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| No anonymous-founder fallback | anon `GET /api/me`, `/api/agents`, `/api/admin/storage-health` → **401** | PASS |
| Authed identity | `X-User-Id` → `/api/me` returns the real user | PASS |
| Public embed read allowed | anon `GET /api/agents/by-slug/<slug>` → 200 | PASS |
| Embed read is stripped | anon by-slug omits `system_prompt` / `guardrails` / `variables` and `chat_settings.instructions` | PASS |
| Owner read is full | authed by-slug returns the full agent shape | PASS |

## 3 · Agents (read + update) — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Get agent | `GET /api/agents/{id}` → 200 with a name | PASS |
| Stats / analytics | `GET .../stats` + `/analytics` → 200 | PASS |
| Update round-trip | `PATCH /api/agents/{id}` persona persists (snapshot+restore) | PASS |
| Create / delete | agent lifecycle via the Eva build flow | **Scenario** (`test_industries.py`, `northstar_test.py`) |

## 4 · Chat branding (`chat_settings`) — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Whole-object persistence | `chat_settings` PATCH round-trips as an **object** (no jsonb double-encode) | PASS |
| Brand kit | `accent_color(_2)`, `card_bg_color`, `card_text_color`, `display_name` persist | PASS |
| Sizing/layout flags | `bubble_size:"xs"`, `full_width_responses` persist | PASS |
| Live preview + widget render | brand kit visibly applied in the embed | **Manual** |

## 5 · Guardrails / policy — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Policy round-trip | `PATCH {policy}` persists dos/donts | PASS |
| Live policy stream | `GET /api/agents/{id}/policy-stream` opens (SSE) | PASS |
| Cross-tab / cross-device sync | a PATCH fans out to other tabs via BroadcastChannel + SSE + PG LISTEN/NOTIFY | **Manual / Scenario** |

## 6 · Knowledge — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Chat brain summary | `GET /chat/knowledge` → 200 with `agent_name` | PASS |
| Knowledge gaps | `GET /knowledge/gaps` → 200 gaps list | PASS |
| Import URL / upload / Shopify sync | ingestion endpoints | **Manual** (needs real docs/creds) |

## 7 · Calls / analytics / outcomes — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Call log | `GET /calls` (+ `?channel=web_chat`) → 200 list | PASS |
| Date filter | `date_from`/`date_to` narrow the window | PASS |
| Outcomes report | `GET /outcomes/report` → 200 | PASS |
| Recording playback | per-call `recording.wav` streams | **Manual** (needs a captured recording) |

## 8 · Chat export — the client-ready XLSX report — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Valid file | `GET /chat/export.xlsx` → 200, PK/zip signature, spreadsheet MIME | PASS |
| Opt-in transcripts | default = 2 sheets; `?transcript=1` adds the **Chat log** sheet (3) | PASS |
| Date-scoped | export honours the same `date_from`/`date_to` | PASS (via §7) |

## 9 · Entitlements / add-ons — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Add-on catalogue | `GET /api/addons` → 200 with add-ons | PASS |
| Plan + entitlements | `GET /api/me/plan` exposes `plan` + `entitlements` (e.g. `chat_channel`) | PASS |
| Purchase flow | Razorpay order → activate | **Manual** (needs live keys) |

## 10 · Plans / billing / org — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| My orgs | `GET /api/me/orgs` → 200 list | PASS |
| Org detail | `GET /api/me/org` → 200 | PASS |
| Org analytics | `GET /api/org/analytics` → 200 | PASS |
| Team invites | invite create/accept/decline | **Manual** |

## 11 · Super-admin (the subscription table) — **Automated** (super-admin only)

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Orgs table | `GET /api/admin/orgs` → 200 list | PASS |
| Storage health gated | super-admin `GET /api/admin/storage-health` → 200 (anon 401, non-admin 403) | PASS |
| Plan override | `POST /api/admin/orgs/{id}/plan` round-trips (snapshot+restore) | PASS |

## 12 · Embed / public surface — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Embed page | anon `GET /embed/<slug>` → 200 HTML | PASS |
| Loader script | `GET /static/embed.js` → 200 (namespaced `sxai`) | PASS |
| Floating widget behaviour | FAB → panel, close button, brand gradient, mobile taps | **Manual** |

## 13 · Visitor provenance (report proxy) — **Automated**

| Feature | Acceptance criterion | Verdict |
|---|---|---|
| Conversations columns | export Conversations sheet has **Device** + **Source** | PASS |
| Summary analytics | Summary sheet has **Visitor device** + **Top sources** breakdowns | PASS |
| Capture pipeline | UA/referer → `extracted._provenance` on a live chat | **Automated (`--scenario`)** |

---

## Out-of-band (not in the fast API pass — by design)

These need a live Gemini key and/or a real browser, so they live as scenario
scripts or manual checks rather than in `eval_suite.py`:

- **Text chat (one turn) — now automated** by `eval_suite.py --scenario`: ready
  → model reply → follow-up chips → provenance capture. Deeper multi-turn / VOICE
  conversations, interrupts, tool/connector calls, handoff, CSAT stay in the
  scenario scripts → `northstar_test.py`, `human_test.py`, `interrupt_test.py`,
  `talk_to_agent.py`.
- **Eva build flow** — describe → build → save → test an agent → `test_industries.py`,
  `talk_to_eva.py`, `build_one_agent.py`.
- **Session resume / memory** → `reconnect_memory_test.py`.
- **Live chat watch/join, recording capture, telephony provisioning + outbound
  calls, Razorpay purchase** — need real providers/creds; verify in staging.
- **UI/pixel** — nav divisions, page-transition smoothness, mobile embed,
  dashboards — manual, tracked per-build in `EVAL_RUBRIC.md`.

## Extending the suite

Add a `s_<area>()` function in `tests/eval_suite.py`, register it in `main()`'s
list, and add a row here. Keep mutations snapshot+restore. Prefer asserting
observable outcomes (status + shape + persisted value) over implementation.
