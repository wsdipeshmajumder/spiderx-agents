# SpiderX.AI — Project Memory

Persistent context Claude reads at the start of every session. Keep terse;
this is operational reference, not narrative documentation.

---

## Acceptance criteria — Reservation/Booking transparency

Adopted as the platform-wide quality bar (Vincenzo Capuano Transparency
Report framework, June 2026). Every reservation-flow feature should be
audited against this list before it ships.

### Section 1 · Business Impact tiles

- [x] Reservations generated count (success-kind outcomes)
- [ ] **Covers KPI tile** — sum `extracted.party_size` over window; surface on dashboard + EOD digest
- [x] 24×7 availability (architectural)
- [x] Processing success rate (`success_rate` on Outcomes page)

### Section 2 · Phone-AI vs human reception

All six rows currently compliant (24×7 / never-miss / concurrent /
consistent policy / same experience / full logs+audit). Re-verify on
every change to the call-handling path or audit logging.

### Section 3 · Exception transparency

- [x] Failure-kind outcome counting (`by_kind.failure`)
- [ ] **Sync-related exception category** (e.g. `booking.sync.failed`)
- [ ] **Retrieval exception category** (e.g. `booking.retrieve.failed`)
- [ ] **No-show tracking** — needs post-reservation webhook + outcome kind

### Section 4 · Actions taken (controls)

Telecom Reliability:
- [x] Additional monitoring (events ledger + Observability page)
- [ ] **Call-path redundancy** (second telephony provider, auto-fallback)
- [ ] **Real-time escalation alerts** (Slack/SMS pager on error/critical events)

Booking-system Synchronization:
- [ ] **Automated sync validation** (post-booking re-read against partner system)
- [x] Exception reporting dashboard (Observability page)

Guest Confirmation:
- [x] Improved confirmation messaging (prompt + connector return text)
- [x] Explicit reservation reference (in `extracted` + read back by agent)
- [ ] **Day-of reminder / follow-up validation workflow**

### Section 5 · Transparency measures (most important section)

Daily Operations Report (largely shipped via Build 201 EOD digest):
- [x] Total calls received
- [x] Calls answered
- [x] Reservations created (success-kind aggregation)
- [x] Failed reservations (`by_kind.failure`)
- [ ] **Sync exceptions** — events exist; pull them into the digest email
- [x] Escalations (`transferred_human` outcome)

Weekly Quality Review:
- [x] Random call audits (full stereo recordings + Call Details modal, builds 206–208)
- [x] Reservation accuracy checks (`extracted` + transcript + recording side-by-side)
- [x] Telecom uptime review (Schedulers tab + event severity)
- [ ] **Weekly review job** — Monday 09:00 IST scheduler that mails "5 random calls + last week's exceptions"

Executive Dashboard Access:
- [x] Call volumes
- [x] Booking conversion (`success_rate` + `purpose.conversion_rate`)
- [x] Booking failures
- [x] Sync status (events feed)
- [x] Exception resolution (Resolve button + `resolved_at`/`resolved_by`)

### Prioritised work list to clear the unchecked boxes

| # | Work | Effort | Section |
|---|---|---|---|
| 1 | Covers KPI tile | ~2h | §1 |
| 2 | No-show outcome + post-reservation webhook | ~4h | §3, §4 |
| 3 | Weekly Quality Review email scheduler | ~3h | §5 |
| 4 | Real-time escalation pager (Slack/SMS on error/critical events) | ~3h | §4 |
| 5 | `booking.sync.failed` / `booking.retrieve.failed` event kinds + sync-validation connector | ~6h | §3, §4 |
| 6 | Telephony provider failover (per-agent, auto-fallback) | ~1d | §4 |

Items 1–4 deliver the most checkbox movement per hour.

---

## Build numbering convention

`backend/app.py:APP_BUILD` and `frontend/app.js:SXAI_BUILD` must stay
in lockstep. Bump on EVERY frontend change. `index.html` substitutes
`{BUILD}` at request time so the `?v=` cache pin can never drift.
Current build: **292**.

## Environment

- **Local Postgres** — connection via `DATABASE_URL` / `PG_URL` in `.env`
- **Local recordings** — `data/recordings/<agent_id>/<call_id>/`
- **Production recordings** — Railway volume mounted at `/files`,
  recordings land at `/files/recordings/<agent_id>/<call_id>/`.
  Resolved by `backend/recordings._resolve_recording_root()` —
  order: `RECORDING_DIR` env var → `RAILWAY_VOLUME_MOUNT_PATH` env
  var → `/files` autodetect → `data/recordings` dev fallback.
- **Server** — `uvicorn backend.app:app` on `localhost:8765`
- **Production** — Railway (`spiderx-agents` service, root `requirements.txt`
  + `railway.json` with start command `alembic upgrade head && uvicorn …`)

## Hard rules

- Recording write failures must NEVER break a call.
- `.env` is gitignored; never echo secrets into chat output.
- Roll-forward of pricing never re-prices historical calls (frozen
  `cost_paise` per row).
- Scrapers / watchdogs are detect-only — human approval required for
  rate changes via the Pricing tab.
- One canonical event-write path (`events.emit`), one canonical read path
  (`/api/admin/events`).
