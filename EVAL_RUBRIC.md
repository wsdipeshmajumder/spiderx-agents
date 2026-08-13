# Eval Rubric — Tester Feedback

> **Maintenance rule (hard):** update this file on **every push** that changes
> behaviour. Per affected item record: acceptance criterion, **verdict**
> (PASS / PARTIAL / OPEN), **evidence tier**, and the **build** it shipped in.
> Bump "Last updated" below. See `CLAUDE.md` → Hard rules.

**Last updated: build 418**

**Build 408 (Hours editor — mobile-width fix):** proactive follow-up
after shipping build 406's multi-window hours editors — tester asked to
"test hours editor on mobile width too". Resized the sandboxed browser to
375px (iPhone-class width) and drove both editors live:

- `WizardHoursEditor` (onboarding): a real bug. `.wiz-hours-row` had no
  `flex-wrap`, so day(42px) + toggle(78px) + the ranges block couldn't
  drop to their own line — the "to" time input got squeezed off the edge
  of the card and the WHOLE PAGE gained horizontal scroll (confirmed via
  screenshot: the topbar logo was visibly clipped/shifted). Fixed by
  adding `flex-wrap: wrap` to `.wiz-hours-row` plus a `flex-basis: 100%`
  on `.wiz-hours-ranges`/`.wiz-hours-closedlabel` inside the existing
  600px mobile media query (same pattern already used for the settings
  page's `.db-hours-row`), so day+toggle sit on one line and the time
  ranges get the full card width on the line below.
- That fix alone left a second, subtler bug once a day had 2+ windows:
  the remove "×" button plus default 10px gaps left each
  `<input type="time">` at only 101px measured width — Chrome's native
  time control starts silently dropping its last character below ~117px
  ("09:00 AM" → "09:00 AI"), which isn't visible as HTML-level overflow
  (`scrollWidth === clientWidth` on the input) so it wouldn't have been
  caught by a layout-overflow check alone, only by actually reading the
  rendered text. Fixed by tightening `.wiz-hours-range`'s gap (10px→4px)
  and shrinking `.wiz-hours-range-remove` (22px→18px) inside the same
  mobile query, handing ~16px back to the two inputs — remeasured at
  114px, confirmed rendering "09:00 AM"/"06:00 PM" etc. in full.
- Settings-page `HoursEditor` (`.db-hours-*`) needed no changes — measured
  128px per time input even with a 2-then-3-window Wed row, well above
  the clipping threshold, and `document.documentElement.scrollWidth`
  matched `window.innerWidth` throughout (no page-level overflow).
- Re-verified desktop (1280px) afterward for both editors — visually
  unchanged, confirming the fix is mobile-only via the existing
  `@media (max-width: 600px)` block.

Verdict: **PASS**. Evidence tier: Behavioral (live browser at 375px:
found the overflow bug, fixed it, measured the residual character-clipping
bug via `getBoundingClientRect()`, fixed that, then re-verified both
editors — add/remove/toggle interactions — render correctly at both
375px and 1280px).

**Build 407 (Skip the mount-time /api/agents call when logged out —
no more cosmetic 401 in the Network tab):** tester report (relayed):
"was logged in, went to lunch, came back, refreshed — saw an error, seemed
like a refresh-token issue." Investigated the app's actual auth model
first rather than assuming: there are no JWTs or refresh tokens anywhere —
login is a stored `{id, email}` object in `localStorage`, stamped as
`X-User-Id` on every request by a patched `window.fetch`. No code path
clears it automatically (`clearAuth()` only fires from the two manual
sign-out buttons), and the backend does a plain unconditional row lookup
by id — no TTL, no session table. So a "refresh token" expiring isn't
literally possible here, and nothing in the app's own logic silently logs
anyone out.

Traced the actual mechanism: the top-level `App()`'s boot `useEffect`
called `refreshAgents()` (→ `GET /api/agents`) unconditionally on every
mount, before login state was known — logged in or not. Logged out, this
401s every time; build 403 already made that harmless (empty list instead
of a crash, via an `Array.isArray` guard), but the red
`{"detail":"authentication required"}` row it leaves in DevTools Network
still reads exactly like a broken session to anyone reasonably
troubleshooting a fresh page load, which is what the tester saw and
reported. Confirmed with the reporting user directly: the page had fully
recovered after the refresh (normal dashboard, no broken state) — the
session was never actually lost, only the Network tab looked alarming.

Fix: gate that boot-time call on `user` being truthy — `user`'s
`useState` initializer already reads `loadAuth()` synchronously, so this
is not a race, just a straight skip when there's no session yet. A fresh
login's own `onAuthed` handler already does its own `/api/agents` fetch
and `setAgents()` once auth succeeds (unaffected by this change), so no
case is left without agents loaded.

**Verdict: PASS** — code-verified: `user` is available synchronously at
the point this effect runs (declared via `useState(() => loadAuth())`
earlier in the same component, mount effects execute after the full
render pass), and the post-login path is untouched. Not independently
re-reproduced against a live "logged out → refresh → check Network tab"
session (the original repro required a multi-hour idle gap to describe,
not something feasible to fully replay); the underlying root cause
(unconditional pre-auth fetch) is directly confirmed via source
inspection instead. Evidence: **Code** + **Behavioral** (the recovered
page was independently confirmed by the reporting user).

**Build 406 (Multi-window business hours — split lunch/dinner service):**
tester feedback, prompted by a Google Business Profile hours screenshot next
to our own onboarding "Hours" field: "for restaurant use cases and similar,
there could be multiple open and closing in same day" (e.g. Wed 11:30
AM–3:00 PM lunch + 6:00 PM–1:00 AM dinner). The Hours field (both the build
wizard's `WizardHoursEditor` and the post-build settings `HoursEditor`) only
supported ONE open/close pair per day — a real gap for restaurants, salons,
and any business running split shifts.

- `_parseHours`/`_serializeHours` (`frontend/app.js`) — the shared helpers
  both editors already reused — now model each day as `{ open, ranges: […] }`
  instead of a single `{ open, from, to }`. A single-range day still
  serializes byte-identical to before ("Mon–Sat 9:30 AM–8:00 PM, Sun
  closed"), so every existing stored `hours` string keeps parsing exactly as
  it did — this is additive, not a breaking format change. A day with 2+
  ranges joins them with " & " ("Wed 11:30 AM–3:00 PM & 6:00 PM–1:00 AM").
  Parsing also handles the Google-style raw text where the second window has
  no day prefix ("Wed 11:30am-3pm, 6pm-1am") by attaching a bare trailing
  time-range clause to the most recently-seen day.
- Both `WizardHoursEditor` (onboarding) and `HoursEditor` (Business profile
  → Location & hours) got a "+ Add hours" link per day and a "×" remove
  button once a day has 2+ ranges (hidden at exactly 1, since a day must
  always keep at least one window). A new range defaults to starting an hour
  after the previous one's close, clamped to 23:59 so it can never compute
  an invalid wrap-past-midnight default.
- `HoursEditor`'s CSS (`frontend/styles.css`) moved off a rigid 5-column
  `grid-template-columns` keyed to nth-child position — that layout had no
  room for a variable number of ranges — to a flex row with a
  `.db-hours-ranges` stack, matching the pattern already used by
  `WizardHoursEditor`. The old nth-child-based mobile media-query override
  is gone with it (replaced by a plain `flex-basis: 100%` on the ranges
  block).
- Verified live end-to-end in the sandboxed browser: built a restaurant
  agent through the wizard, added a second Wed window (11:30 AM–3:00 PM +
  6:00 PM–1:00 AM), confirmed the "Saved as" preview matched exactly;
  removed the second range and confirmed it cleanly collapsed back to a
  single-range day with no stray "&"; separately, on an existing agent's
  Business profile → Location & hours, added a split Wed, hit **Save
  profile**, confirmed via the PATCH response that `variables.hours`
  persisted as `"wed: 10:00-20:00 & 21:00-23:59"`, then did a full page
  reload (fresh mount, not client nav) and confirmed the editor re-parsed
  it back into two visible Wed rows with working remove buttons.

Verdict: **PASS**. Evidence tier: Behavioral (live browser, both editors,
full save → reload round-trip verified via network response + fresh mount).

**Build 405 (Agent-switcher gradient + topbar logo size):** tester feedback
from a screenshot of the topbar: "the drop down use a gradient color" +
"spiderx logo reduce by 10%, use ur judgement."

- `.db-switcher-trigger` (the "Rohan"/agent-name pill at the top of the
  sidebar, `AgentSwitcher` component) now uses `linear-gradient(135deg,
  #4f46e5 0%, #6366f1 100%)` — the same indigo brand gradient already used
  elsewhere for primary actions — instead of the flat `#f4f5f9` surface
  from build 242. Deliberately NOT the pink→purple gradient reserved for
  the active sidebar nav item (`.db-nav-item.active`, build 230): that
  gradient means "you're on this page," and reusing it on the switcher
  would blur that signal. Text/icon flip to white for contrast; hover
  state darkens to `#4338ca → #4f46e5`; dark-theme override kept identical
  (indigo reads fine on both `#f7f8fa` and `#0b0d14` canvases, needed no
  separate palette).
- Topbar `SpiderXLogo` height reduced from 44 to 40 (44 × 0.9 = 39.6,
  rounded) — the one dashboard-shell usage (`DashboardShell`, matches the
  tester's screenshot); the three other `height=` call sites elsewhere in
  the app (homepage header, a different page header, footer) were left
  untouched as out of scope.
- Verified live in the sandboxed browser against `/agents/<id>` (Ria):
  gradient pill renders correctly and stays legible in both light and
  dark theme (`data-theme="dark"` toggle); logo visibly smaller relative
  to the workspace-switcher pill beside it, no layout shift.

Verdict: **PASS**. Evidence tier: Behavioral (live browser screenshot,
both themes).

**Build 404 (Background image: true CSS background on `.db-main`, not a
layout element):** tester correction: "u r pushing the elements on screen
for the background images, as it means, this is a background image, so
all elements would be in front of it." Builds 397–401's `<aside
class="db-bgrail">` — a real flex/absolute sibling of `<main>` — was
fundamentally the wrong model: a background never resizes or displaces
content by definition, and a separate layout box competing for space (or
overlaying with its own stacking context) does exactly that. Reworked from
scratch:

- Removed the `<aside class="db-bgrail">` DOM element entirely (reverted
  the build-398 `DashboardShell` change) and every `.db-bgrail`/flex/
  `max-width` rule from builds 397–401.
- `.db-main` keeps its original, completely unmodified `flex: 1` — zero
  width or layout impact on any page, sparse or dense, matching "all
  elements in front of it": a CSS `background-image` always paints behind
  an element's own children, so content needs no repositioning at all to
  sit "in front" — it already does, structurally, by definition.
- Sizing iterated twice in this same build. First tried
  `background-size: auto calc(100vh - 56px)` (height-locked to `.db-main`'s
  real box height, width auto) on the theory that the resulting width
  would be much wider than any reasonable content box, so
  `background-position: right` + `no-repeat` would clip to just a
  right-hand slice sized to whatever's genuinely empty. Measured live and
  it didn't hold: at 1600×900 the computed size was `auto 844px` → ≈1501px
  wide against a 1600px-wide `.db-main` box — nearly the *entire* photo
  rendered (both its left and right tree clusters visible at once), and on
  a 27-card dense grid page it showed through every inter-card gutter, not
  just the true right edge — busy rather than a clean accent. Switched to
  `background-size: cover` (with `background-position: center`) instead:
  deterministically fills `.db-main`'s entire box in both dimensions
  regardless of viewport or content density, cropping rather than leaving
  any edge unfilled — the only sizing mode that reliably delivers "entire
  screen except the left menu" on every page, not just favorable ones.
- Kept from build 400: `@media (min-width: 1440px)` gate (hidden below
  that — no page's content is ever displaced either way, so this is purely
  "don't bother painting it where there's no room to appreciate it", not a
  squeeze-avoidance measure like it was for the old rail model). Kept dark
  theme's existing `:root[data-theme="dark"] .db-main { background:
  #0b0d14; }` full-shorthand override, which already resets
  `background-image` to `none` via the cascade — removed the now-orphaned
  `.db-bgrail` dark rule (the class no longer exists).

**Verdict: PASS** — browser-verified at 1600×900 on the Chat widget Home
tab (moderate content: two populated cards + a tab row): image fills
`.db-main` edge-to-edge top-to-bottom, every card and every tab label
fully legible on top of it, dark theme fully clean (image absent, pure
`#0b0d14`), and 1280px width (below the gate) renders identically to
before this feature existed. Also checked the 27-agent dense grid page at
1600px to confirm `cover` deterministically fills the box there too (the
image shows through the grid's own inter-card gutters on that specific
page, which is expected and correct for a true background under a
full-width grid — not a bug in this fix). Evidence: **Behavioral**
(logged-in browser, both pages, light + dark) + **Code**.

**Build 403 (Fix "Something went wrong" crash right after login):** tester
report: "logout and then after login this is coming" (the AppErrorBoundary
card added in build 397). Reproduced on a completely fresh login too — no
prior logout needed. Root-caused with a real OTP login against a local
server (`EMAIL_PROVIDER=log` override so the code prints to the server log
instead of a real inbox — no email sent) and read the actual console error
instead of guessing from the screenshot: `TypeError: filtered.map is not a
function` inside `DashboardAgentsList`.

`App`'s `refreshAgents()` fires unconditionally on mount, before login
completes on a cold load. An unauthenticated `/api/agents` 401s with an
error-detail **object** (`{"detail": "authentication required"}`), not a
list — and `refreshAgents` did `setAgents(await res.json())` with no
`response.ok` check and no shape validation, storing that object as-is.
Nothing re-fetches `agents` after login succeeds, so `DashboardAgentsList`
later renders with that poisoned object; `agents.filter/.map(...)` throws,
and `AppErrorBoundary` (build 397) catches it as a blank "Something went
wrong" card instead of the crash being silent — an improvement over before,
but the underlying crash itself was still unfixed until now.

Fixed both ends: `refreshAgents` now checks `r.ok` and guards with
`Array.isArray(data) ? data : []` (matches the safe pattern already used
elsewhere, e.g. the admin healthcheck page). `onAuthed`'s own post-login
`/api/agents` fetch (previously used only to decide the post-login route)
now also feeds `setAgents(list)` directly — the freshly-authenticated
fetch replaces whatever `refreshAgents` left behind while still logged
out, rather than leaving that stale/poisoned state in place.

**Verdict: PASS** — browser-verified end-to-end: cleared all local storage,
loaded `/login` fresh, requested + entered a real OTP for the account with
saved agents, landed on `/agents` with `agents` populated correctly and no
crash (previously crashed 100% of the time on this exact path, verified
before the fix with the same steps). Evidence: **Behavioral** (real login,
console error captured before + confirmed clean after) + **Code**.

**Build 402 (Native audio-player popup was clipped by the Call-log table's
rounded-corner wrapper):** tester screenshot: a red box around the
recording player's controls on the Call log page, with "ui getting
hidden...when clicked on the hover and then on the speaker." The recording
player is a *native* `<audio controls>` element (`app.js` — no custom
playback UI), and its speaker icon shows a volume-slider popup on
hover/click that Chromium renders inline in normal document flow (not as
browser chrome escaping layout) — so it's clipped by any ancestor with
`overflow: hidden`. `.db-table-wrap` had exactly that, used only to force
rounded corners on the table's square cells. For a row near the top of the
table the popup has nowhere to render but above the row, i.e. outside the
wrapper's box — clipped to invisible, reading as "the UI got hidden."

Fix: dropped `overflow: hidden` from `.db-table-wrap` and rounded the
table's own outer cells directly instead
(`thead tr:first-child th:first-child` etc., 12px matching the wrapper) —
the standard way to get a rounded table wrapper without `overflow:hidden`
clipping anything a child needs to escape the box for (this same fix also
covers a native `<select>` dropdown or a title tooltip near the table
edges, not just the recording popup). `.db-table-wrap` is shared by 3
tables in the app; all three get the fix since none had a competing
`border-radius` override.

**Verdict: PASS** — browser-verified on `rohan`'s Call log: computed style
confirms `overflow: visible` on `.db-table-wrap` post-fix, with
`border-top-left-radius`/`border-bottom-left-radius` both resolving to
12px on the outer cells (rounding preserved, screenshot-compared against
the pre-fix table — no visible corner-squaring regression). Not yet
reproduced with the *actual* native volume-popup rendering pixel-for-pixel
(automated browser screenshots don't reliably capture that specific native
control state) — the fix is verified via the CSS mechanism directly
(overflow was provably the only thing capable of clipping it, and it's now
provably gone), not a literal before/after popup screenshot. Evidence:
**Behavioral** (computed styles + visual regression check) + **Code**.

**Build 401 (Background rail: give it the majority of the row instead of a
fixed strip):** tester screenshot of the Agents list (1 agent — sparse
content) showed the rail as a bounded box with a big white gap of unused
`.db-main` between the card and the image; "shud have been entire screen
except left menu."

First attempt (within this same fix) added `max-width: 1600px` to
`.db-main` in the `min-width: 1920px` tier, expecting the rail to absorb
whatever `.db-main` didn't use. Measured live and it did nothing:
`.db-main` was still `flex: 1` (grow: 1) and the rail was `flex: 1 1 auto`
(also grow: 1) — **equal** grow factors split the row 50/50 during flex
free-space distribution regardless of either side's `max-width` (a
`max-width` only engages if a box's *assigned* share would exceed it; at
1920px a 50% share is 960px, nowhere near the 1600px cap, so it never
triggered). Confirmed via `getBoundingClientRect()`: `mainWidth: 960,
railWidth: 960` on a 1920px viewport.

Real fix: `.db-main { flex: 0 1 auto; max-width: 1600px; }` in the
`min-width: 1920px` tier — `flex-grow: 0` stops it competing for free
space at all, so it sizes to its own content's natural width (shrink-wrap,
capped at 1600px as a safety ceiling — no page's internal wrapper caps
wider than 1500px, checked in build 401's first pass). `.db-bgrail`
(`flex: 1 1 auto`, the only remaining grower) then claims 100% of
whatever's left. Re-measured on the exact reported page (Agents list,
1920px): `mainWidth: 816, railWidth: 1104` — the rail now gets the
majority (57%) of the row, and the ratio is content-driven per page
instead of a fixed split, so a denser page (more cards, a filled Chat
overview) naturally leaves proportionally less for the rail without any
per-page changes. The 1440–1919px tier (build 400) is untouched — this
change is scoped to the `min-width: 1920px` block only.

**Verdict: PASS** — browser-verified at 1920×1080 on both the reported
page (`/agents`, sparse — rail now dominant, no dead gap) and a
content-heavy page (Chat widget Home, two populated cards — rail still
generous, no overlap or squeeze); re-verified the 1600px `/agent/.../chat`
width (1440–1919px tier) renders identically to build 400, confirming no
regression to the lower tier. Evidence: **Behavioral** (logged-in browser,
`getBoundingClientRect()` measurements + screenshots, 3 viewport/page
combinations) + **Code**.

**Build 400 (Background rail: lower the visibility gate 1920px → 1440px):**
tester boxed a completely different page (`Get a test call`) at their own,
normal working window — no rail visible at all — and asked again for the
background to fill the space. Root cause: 399's rail only widened what was
already there past 1920px; the *entire feature* was still gated behind
`min-width: 1920px`, which almost no real browser window (laptops included)
actually reaches — the tester was never going to see it at their normal
size. Verified the risk before lowering: `.db-table` is `width: 100%` with
no fixed pixel width, so narrowing `.db-main`'s available space just
reflows table columns, it doesn't clip or break them. Split into two
tiers: 320px rail from 1440px (a genuinely common laptop width) up, growing
to the full 560px only past 1920px where there's room to spare — instead
of a single all-or-nothing breakpoint. **Verdict: PASS** — browser-verified
at 1440×900 logged in: rail renders on the exact `Get a test call` page
from the report, no card/content squeeze; `Call logs` (the widest
table-heavy page in the app) reflows its columns cleanly at the same width
with nothing clipped or broken; dark theme still hides the rail entirely
(unchanged, verified on the same page). Evidence: **Behavioral**
(logged-in browser, 1440×900, both the reported page and a stress-test
table page) + **Code**.

**Build 399 (Background rail widened 320px → 560px):** tester, after 398's
non-overlapping rail shipped, boxed the *entire* dashboard area (cards
included) in feedback and said the image "shud occupy this whole space."
Ambiguous whether that meant "make the rail column itself bigger" or "go
back to a shared background behind the cards" (which 393/394/396 already
tried and reverted, each time because the image showed through behind real
content). Asked directly rather than guess a 5th time — tester confirmed:
widen the rail, keep the non-overlapping architecture (398's whole point
was making overlap structurally impossible; that stays).
`.db-bgrail`'s `flex: 0 0 320px` → `flex: 0 0 560px` (75% wider). Raised
the visibility gate `@media (min-width: 1680px)` → `1920px` in lockstep —
at the old gate width, sidebar + a reasonably wide page's content + a
560px rail would genuinely compete for space; 1920px (a common real-world
monitor width, not just a rare ultra-wide) leaves `.db-main` room for its
widest pages (1440px-capped content + padding) alongside the bigger rail.
**Verdict: PASS** — browser-verified at 1920×1080 logged in: rail renders
visibly wider (~560px, roughly 30% of the viewport) with no content
squeeze or overlap; dark theme and sub-1920px widths unaffected (both
untouched by this change). Evidence: **Behavioral** (logged-in browser,
1920×1080) + **Code**.

**Build 398 (Background image as a genuine full-height right rail, not a
shared background):** tester, after the build-396 size/position fix: "shud
occupy full right side. Try again." Builds 393/394/396 all tried to paint
the lake/forest image as `.db-main`'s own CSS `background` — which is
fundamentally the wrong shape for "occupy the full right side": either it
sits behind/under the tab row and page content (393, the original bug) or
it has to shrink to a small corner watermark to avoid that (394/396,
correctly avoided overlap but could never fill the side). A shared
background can't both be full-height AND never overlap content, because
it's literally in the same box as the content.

Replaced it with a genuine layout element: `<aside class="db-bgrail">`
added as a flex sibling of `<main class="db-main">` inside `.db-body`
(`display: flex`), right after `</main>`. `.db-body` has no explicit
`align-items` (defaults to `stretch`), so the new rail automatically
matches the full height of the row for free — no explicit height needed.
`flex: 0 0 320px` gives it a fixed width; `background-size: cover` fills
that column edge-to-edge (cropping the image rather than letterboxing it,
since the source is a wide landscape and the rail is narrow). Because it's
a sibling column, not a background layer, it structurally cannot overlap
`.db-main`'s content — there's no shared box for it to sit behind.

Kept from build 396: hidden below `@media (min-width: 1680px)` (a rail
stealing 320px would squeeze real content on ordinary laptop widths — the
1440px-content-column math from build 396 still applies), and hidden
outright in dark theme (`:root[data-theme="dark"] .db-bgrail { display:
none; }` — a pastel watercolor doesn't belong in a dark UI regardless of
where it sits). Removed the old `.db-main` background-image rule entirely
(build 396) rather than layering the two approaches.

**Verdict: PASS** — browser-verified on `rohan` at three states: 1920×1080
light — the rail fills the full height of the right side, distinct from
and non-overlapping with the two-pane Home content; 1920×1080 dark — rail
fully hidden, pure dark canvas; 1440×900 light — rail hidden (below the
1680px gate), content lays out exactly as it did before this feature
existed, no squeeze. Evidence: **Behavioral** (logged-in browser, all
three states) + **Code**.

**Build 397 (Persistent, retryable error on a mid-call drop instead of
silently bouncing to the dashboard; error boundary for the Google-login
blank screen):** two tester reports, investigated with the codebase before
touching anything (no guessing).

- **"automatically the test call page got removed during testing in a
  call... is there a timer set?"** — clarified: this was testing an
  already-*saved* agent (not the Eva-builder voice conversation, which does
  have a legitimate 90s/150s/240s wrap-up watchdog by design). Traced the
  actual mechanism: every server-sent mid-call `type:"error"` frame
  (reconnects exhausted — `gemini_bridge.py:5386,5411`; save failed; no
  usable model) is immediately followed by the server closing the WS.
  `ws.onclose` only had a persistent-error path for *pre-ready* failures —
  anything after `callStartRef.current` was set fell straight into
  `closeSession()`, which silently `setView("landing")` + routes to `/`.
  The only feedback was a 3s toast fired moments before the page vanished —
  easy to miss, reads exactly as "the test call page got removed" with no
  visible cause. Added `midCallErrorRef` (set in the `onmessage` error
  handler, alongside a `setCallError` with a retry callback identical to
  the existing pre-ready-failure pattern) so `ws.onclose` now recognizes
  this case too and keeps the call surface up with a persistent, retryable
  error card instead of navigating away.
- **Blank screen after Google login in Edge** — no error boundary existed
  anywhere in the app; a post-login render crash unmounts the whole React
  tree, leaving only the background gradient with zero explanation —
  browser- and root-cause-agnostic, but matches the symptom exactly. Added
  `AppErrorBoundary` (inline-styled only, so it can't depend on a possibly-
  unloaded stylesheet) wrapping the root render. Also addressed the likely
  Edge-specific contributor: `googleSignIn()` awaited the Firebase SDK's
  dynamic `import()` *before* calling `signInWithPopup()`, moving the popup
  call outside the synchronous click handler — Chromium popup heuristics
  (Edge's stricter tracking-prevention especially) are more likely to
  block/wedge a popup opened outside that gesture window. `AuthPage` now
  prefetches `_getFirebaseAuth()` on mount so the cached promise is
  normally already resolved by the time the button is clicked.

**Verdict: PASS (mid-call error UX) / PARTIAL (Edge blank screen)** — the
mid-call fix is code-verified against the exact server code paths that
produce it (every `type:"error"` send in `gemini_bridge.py` during an
active session is followed by `return`, confirmed by direct inspection);
not yet reproduced live end-to-end (would need to force a Gemini drop
past `MAX_RECONNECTS` on demand). The Edge fix is a defense-in-depth pair
(error boundary + popup-timing) addressing the most plausible root causes
identified from investigation, but the user hadn't captured a console
error at the time of the report, so the *actual* trigger is still
unconfirmed — flagged as the natural follow-up if it recurs (check
DevTools Console next time). Evidence: **Code** (both) + **Behavioral**
pending reproduction.

**Build 396 (Fix the build-393 background image — too big, overlapping the
tab row):** tester screenshot showed the lake/forest background dominating
the whole right third of the viewport, its treetops washing out the
"System prompt" / "Go live" / "Conversations" tab labels where the image
overlapped them — the 55%/820px sizing from build 393 was sized for a wide
open panel, not for sitting directly behind live text. Fixed on three axes:
(1) size — 380px fixed width, not up to 820px; (2) position — pushed down
170px from the top of `.db-main` (clears the pageheader + tab row, which
sit in that first ~150px) instead of flush top; (3) scope — wrapped in
`@media (min-width: 1680px)`, since `.db-main` only has genuine spare
gutter beyond the 1440px-capped content column on wide monitors — on a
standard laptop width the image would sit on top of real content instead
of beside it, which is exactly what the tester's ultra-wide screenshot
exposed. **Verdict: PASS** — browser-verified at 1920×1080 on `rohan`:
background renders small and clearly clear of the tab row and both panel
cards; tab labels fully legible. Evidence: **Behavioral** (logged-in
browser, live screenshot) + **Code**.

**Build 395 (Fix call-recording audio drift — bot audio front-loaded ahead — bot audio front-loaded ahead
of the caller's):** tester reported a live call recording where the voices
sounded mixed up — the bot's answer played before the user's question that
prompted it. Downloaded the recording and confirmed it empirically before
touching code: per-second RMS energy showed the agent (right) channel
packed solid for the first ~57s of a 91s call with almost no gaps, then
completely silent for the remaining ~34s — while the caller (left) channel
kept its normal on/off speech pattern the entire way through.

Root cause: `RecordingWriter.write_agent()` (`backend/recordings.py`) only
writes bytes while Gemini is actually emitting TTS audio, with no silence
written during the gaps while Eva isn't speaking. `write_caller()` gets a
continuous mic tap (every inbound WS chunk, silence included), so
`caller.wav` stays wall-clock aligned — but `agent.wav` is a "compressed"
track: all of a call's TTS back-to-back with the dead air squeezed out.
`mix_to_stereo()`'s sample-index interleave assumes both streams start
aligned at frame 0, so a call with real gaps drifts the agent channel
earlier and earlier as it goes on, exactly matching the empirical shape
above (and the tester's report — a later answer ending up mixed before an
earlier question).

Fix: added `RecordingWriter._pad_to_now()`, called from both `write_caller`
and `write_agent` before every real chunk — inserts silence so each
stream's cumulative duration catches up to real elapsed time since
`self._started_at`, keeping both channels wall-clock aligned regardless of
how bursty either one is. `mix_to_stereo`'s existing end-of-stream padding
(for whichever side is shorter overall) is untouched and still correct.

**Verdict: PASS** — `tests/test_offline.py::TestRecordingWriterAlignment`
(4 new tests): `_pad_to_now` inserts the correct silence for elapsed time
and is a no-op when a stream is already caught up; `write_agent` pads
through a simulated silent gap (reproduces the reported bug shape) with the
padding verified as genuine silence, not garbage; an end-to-end check that
after caller-continuous + agent-bursty writes, both streams' durations land
within 0.2s of true elapsed time despite wildly different active-audio
totals. Evidence: **Behavioral** (RMS analysis of the real reported
recording, offline) + **Unit** (4 tests) + **Code**. Not yet re-verified
against a fresh live call recording end-to-end (existing bad recordings
predate the fix and can't be repaired retroactively) — flagged as the
natural follow-up.


**Build 394 (Recent-chats toolbar: 2 compact rows, not 3):** screenshot
showed FROM/TO on one line, the All/Widget/Test filter dropped to its own
second line, and the transcript-checkbox+Export row as a third — tester:
"instead of 3 rows, make it 2 rows compact." Root cause was physical, not a
CSS bug: in the narrow `chatconv-list` pane (~311–353px per live
measurement) the date range alone needs ~270px, leaving nowhere near enough
room for the segmented filter (180px) on the same line as build 390 had
grouped them — no amount of flex tuning fits 132+135+52+180px of controls
into a 311px row. Regrouped instead of just shrinking: row 1 is now
FROM/TO/Clear only (fits standalone); row 2 is the traffic filter + the
transcript toggle + Export, all three compacted to fit together — Widget/
Test go icon-only (🧩/🧪, `title=` tooltip carries the label, kept in the
segment's accessible name so screen readers still hear "Widget traffic" /
"Test traffic"), the transcript checkbox drops its text label for a 📄 icon
(tooltip explains it), and "Export report" shortens to "Export". Date
input `min-width` trimmed 132px → 116px for a little extra breathing room.
**Verdict: PASS** — browser-verified on `rohan`: date range renders on one
line, filter+checkbox+Export render together on a second line with no
wrap; clicking the icon-only Test filter still correctly narrows the list
to the 1 test conversation. Evidence: **Behavioral** (logged-in browser,
measured pane width) + **Code**.

**Build 393 (Ambient lake/forest background, right side of the dashboard
shell):** tester supplied a watercolor lake/forest image and asked to "use
this background image as a background on the right side" to make the UI
feel more alive; follow-up clarified the target as the agent dashboard
shell (not the marketing homepage or login page). Saved the asset to
`frontend/bg-lake.jpg` (served at `/static/bg-lake.jpg` via the existing
`app.mount("/static", ...)`) and applied it to `.db-main` — the persistent
main-content box behind every internal dashboard page (Home, Chat widget,
Call Analytics, etc.) — anchored `right top`, `no-repeat`,
`background-size: min(55%, 820px) auto`. Deliberately left
`background-attachment` at its default (`scroll`, tied to the element's own
box): `.db-main` is itself the scrolling container, so the image stays
pinned top-right as page content scrolls inside it, rather than scrolling
away immediately. No overlay/gradient needed — the image's own top half is
already a soft near-white fog that blends into `.db-main`'s `#f7f8fa`
background, and opaque `.db-panel` cards naturally occlude it wherever
real content sits, so it only shows through empty margin. Dark theme is
untouched: `:root[data-theme="dark"] .db-main`'s existing `background:
#0b0d14` shorthand already resets `background-image` to `none` via normal
cascade (higher-specificity full shorthand beats the light-mode
longhand), so the pastel watercolor never appears against the dark UI.
**Verdict: PASS** — code-level: confirmed `/static/bg-lake.jpg` resolves
under the existing static mount with no new route needed; confirmed the
dark-theme override's shorthand-reset behavior against the CSS cascade
rules (no explicit `:root[data-theme="dark"] .db-main { background-image:
none; }` required). Evidence: **Code** (asset placement, mount reuse,
cascade correctness) — a full logged-in-browser screenshot pass at both
themes is the natural next-session follow-up, not yet captured here.

**Build 392 also carries a trailing build-390 fix:** `.chatconv-list` (the
Recent-chats left pane) had no height cap, so once build 390 split its
toolbar into two rows the taller pane could scroll the whole page instead
of itself, drifting out of sync with `.chatconv-detail`'s already-capped
`max-height: calc(100vh - 150px)`. Gave `.chatconv-list` the same cap +
`overflow-y: auto` — both panes now scroll independently at equal height.
Verified: `.chatconv-list` computed `max-height: 570px`, `overflow-y: auto`,
`scrollHeight` (3932px of rows) > `clientHeight` (568px) — scrolls in its
own region as intended.

**Build 392 (City/country in the chat-detail provenance chips):** tester ask:
"show the city, country also" (on the existing device/browser/OS/source/
locale chip row). No IP-geolocation capability existed anywhere in the repo
before this. Added `_client_ip(ws)` (mirrors `admin.py`'s `_ip_ua` — trusts
`X-Forwarded-For`'s first hop, matches Railway's single-proxy deployment)
and `_geoip_lookup(ip)` to `chat_bridge.py`: skips private/loopback/
reserved/link-local IPs outright (zero-cost for local dev and the
operator's own preview links — precisely the case in the tester's
screenshot), else a keyless `ipwho.is` call (switched from `ipapi.co`
after hitting its free-tier 429 from this very host mid-build — verified
live before committing to it) capped at 1.5s, results cached by IP for the
process lifetime. Awaited synchronously into `_provenance` at session
start rather than deferred to persist-time — a deliberate simplicity
tradeoff: private/self-test traffic (the common case) pays zero latency,
and a genuine new visitor IP pays a bounded, one-time (then-cached) cost,
same "best-effort, never breaks the chat" contract `_chat_provenance`
already made for UA parsing. Rendered as a 📍 chip in `chatDetailBody`
between OS and Source. **Verdict: PASS** — unit-tested `_geoip_lookup`
directly (private IP → `{}` instantly; `8.8.8.8` → `{city: "San Jose",
country: "United States"}`), then end-to-end over a real WebSocket with a
spoofed `X-Forwarded-For` header (Python `websockets` client, bypassing
the browser's inability to set WS handshake headers) — confirmed the
persisted `calls.extracted._provenance` row and the rendered "📍 San Jose,
United States" chip in the dashboard. Evidence: **Behavioral** (unit +
live WS session + browser) + **Code**.

**Build 390 (Drop the selected-row left border; reorganise the Recent-chats
toolbar into two rows):** two tester items, both UI polish on the
Conversations tab shipped in the last few builds.

- **No left border** — `.call-row.is-selected` used an inset box-shadow to
  fake a 3px indigo left border on the selected row; tester: "dont use a
  left border." Dropped the `box-shadow`, kept the tint background as the
  only selected-state indicator.
- **Toolbar reorganised** — screenshot showed the "Include full transcripts"
  checkbox stranded far right with a large empty gap, and "Export report"
  alone on its own wrapped line. Cause: adding the build-388 traffic-filter
  segment to the single flex-wrap toolbar row overflowed it, so the
  `flex:1 1 auto` spacer + wrap pushed later items around unpredictably.
  Split into two explicit `.chatconv-toolbar-row`s: row 1 = date range +
  Clear + traffic filter (the "narrow down what you see" controls, spacer
  pushes the filter to the right); row 2 = the transcript checkbox + Export
  report (the "do something with it" controls, spacer pushes Export right).
  Grouped by function instead of leaving flex-wrap to sort seven inline
  items into arbitrary lines.

**Verdict: PASS** — browser-verified on `rohan`: selecting a row shows only
the tint, no border; the toolbar now renders as two clean rows with no
stray gaps at any tested width. Evidence: **Behavioral** (logged-in
browser) + **Code**.

**Build 389 (Rename "Real" → "Widget" — don't overclaim verified traffic):**
tester pushback: "'real' may be not real, so use some other word for it."
Fair — the label only means "not tagged `_is_test`"; absence of the test
flag isn't proof a visit came through embed.js on a real customer site
(e.g. someone could open `/embed/<slug>?channel=chat` directly with no
`is_test` param and it'd still read as the non-test bucket). Renamed
"🌎 Real" → "🧩 Widget" everywhere it appeared: the Conversations traffic
filter (`trafficFilter` state value `real` → `widget`), the chat-detail
provenance chip (CSS class `chatdrawer-provchip-real` →
`-provchip-widget`), and its tooltip — now "Not tagged as an operator
preview — presumed to be a visitor via the embed widget" instead of
claiming it "came through the real embed.js widget." Picked 🧩 (not 🌐,
already used for the Browser chip in the same row) to avoid emoji
collision. **Verdict: PASS** — browser-verified on `rohan`: filter button
and detail chip both read "🧩 Widget"; clicking it still filters correctly
(same underlying `!c.extracted?._is_test` logic, untouched). Evidence:
**Behavioral** (logged-in browser) + **Code**.

**Build 388 (Traffic filter for Conversations; fix ghost/primary buttons
wrapping to two lines):** two tester items.

- **Test/Real traffic filter** — "have a filter for test and real traffic."
  Added an All / 🌎 Real / 🧪 Test segmented control (reusing the
  `.db-embed-segment` pattern) to the Conversations tab's Recent-chats
  toolbar, filtering the already-fetched `chatLogs` window client-side on
  `extracted._is_test` (build 387). Empty state distinguishes "no chats at
  all in this window" from "no chats match this filter."
- **Button text wrapping** — screenshot showed the "↻ Refresh" button's icon
  and label stacked on two lines instead of one. Root cause: the same class
  of bug as build 382's tab fix — `.db-btn-ghost`/`.db-btn-primary` are flex
  children of `.db-panel-head` (`justify-content: space-between`) with
  neither `white-space: nowrap` nor `flex-shrink: 0`, so in the narrower
  `chatconv-list` pane (post build-383's two-pane layout) the title/subtitle
  squeezed the button below its content's natural width and its text
  wrapped at the space. Added `white-space: nowrap; flex-shrink: 0;` to both
  base classes — applies to every button using them, not just Refresh.

**Verdict: PASS** — browser-verified on `rohan`: Refresh renders on one line
in the (still-narrow) Recent-chats header; the traffic filter's Test/Real/All
states each rendered the correct subset live (Test → 1 row, Real → the rest,
All → everything). Evidence: **Behavioral** (logged-in browser) + **Code**.

**Build 387 (Label preview-link visits "Test" vs real embed.js traffic
"Real"):** tester ask: "when u see visits are from the preview link, show
them as test, when visitors come from embed widget, show them as real
traffic, use diff label." The 4 operator self-test surfaces (embed-flyout
"Open preview", Go-live's "Open a live preview", the Go-Live page's FAB-mock
iframe + "Open standalone") now append `?is_test=1` to the `/embed/<slug>`
URL. `AgentChatEmbed` reads it and forwards `&is_test=1` on the chat WS;
`run_agent_chat_session` stamps `extracted._is_test=true` (added to both
`chat_report.py` and `app.js`'s meta-key exclusion lists so it doesn't leak
as a fake "captured field"). `_LiveChat` gained an `is_test` slot (threaded
through `live_chats_for_agent` and the `chat_observe` "hello" frame) so the
label is live, not just post-hoc. Real embed.js visits are untouched — no
flag, and `_chat_provenance`'s existing `source`/host tracking already
distinguishes them by construction (embed.js sets `?host=<page domain>` on
the iframe; these operator links never do). Labelled in 5 places: Home's +
Conversations' live-visitor list rows, `LiveChatModal`'s header (both
live-watch entry points), the historical Conversations list rows, and the
chat-detail provenance chips (🧪 amber "Test" / 🌎 green "Real"). **Verdict:
PASS** — browser-verified with two simultaneous live sessions on `rohan`:
one opened via `/embed/rohan?channel=chat&is_test=1` (the real "Open a live
preview" link's URL shape), one via a raw WS mimicking embed.js (no
`is_test`). Confirmed correct labelling live (both live-list locations +
the watch modal header) and post-hoc (Conversations list row + detail
chip) — test visit tagged everywhere, real visit untagged/labelled 🌎 Real
everywhere. Evidence: **Behavioral** (two concurrent live sessions,
compared side by side) + **Code**.

**Build 386 (Live now moves to Home's right pane; clicking opens it in
Conversations):** tester follow-up: "the live now need not be in the left,
make it a list on right side, clicking which take them to conversations
tab." Reworked build 383's layout — left pane (`chatconv-list`) is back to
just Chat overview (stats + quick links); the **Live now** list moved to the
right pane (`chatconv-detail`), replacing the inline `LiveChatModal` Home
used to render there. Row click no longer opens the chat inline on Home at
all — it does `setLiveSid(lc.sid); setChatTab("conversations")`, so the
visitor opens in the Conversations tab's existing detail pane (same shared
`liveSid` state — Conversations already renders `LiveChatModal` there when
it's set). Row CTA copy changed from "Watch / join →" to "Open in
Conversations →" to match. **Verdict: PASS** — browser-verified with a real
live visitor session: Home's right pane lists the live visitor with the new
copy; clicking it switches the active tab to Conversations AND the live
chat is already open and "watching" there (transcript visible, Join as
human available) — no extra click needed. Evidence: **Behavioral** (live WS
session) + **Code**.

**Build 383–385 (Home tab goes wide: live-visitor list + fix the watch/join
auth bug it exposed):** tester ask: "the home tab also shud have the wide
view" + "have live visitor list, to click and watch or join a conversation."

- **383** — Home's configured state now uses the same two-pane
  `.chatconv-layout` grid as Conversations (list pane + sticky detail pane,
  filling the full 1440px width) instead of a single narrow card. Left pane:
  stats + quick links + a new **Live now** section listing live visitors
  (reusing the exact row markup/classes from the Conversations tab's live
  list). Right pane: clicking a visitor renders `<LiveChatModal inline>` —
  the same watch/join component Conversations already used — with an empty
  state ("click a live visitor…") when nothing's selected. Also fixed
  `.chathome-stats` (`repeat(4,1fr)`) overflowing the now-narrower list pane
  by 127px — switched to a fixed `repeat(2,1fr)` 2x2 grid.
- **384/385 — the watch/join feature itself was broken, pre-existing, on
  BOTH Home and Conversations** (not introduced by 383 — verified identical
  failure from the original Conversations tab first). `LiveChatModal`'s
  WebSocket never authenticated: it connected bare, and the backend's
  `chat_observe` mode requires an operator id (build 366 closed the
  founder-fallback hole) — every watch/join attempt hit
  `{"type":"error","message":"Not authorised."}`. **384** tried `withUser()`
  (appends `?u=`) — still failed, because `ws_session`'s own query parsing
  only reads `?user_id=` (see its docstring), a different alias than the
  shared REST `current_user()` accepts. Root-caused with a raw WS test
  against a real live session (`&u=1` → rejected, `&user_id=1` → connected
  + got the `hello` transcript) before touching code. **385** fixes it:
  `LiveChatModal` now builds `&user_id=${currentUserId()}` directly.

**Verdict: PASS** — browser-verified the full loop end-to-end on `rohan`
(org 1) using a real live visitor session (raw WS `mode=chat` connection):
clicked a live visitor from the new Home list → `LiveChatModal` connected
("watching", live transcript) → clicked **Join as human** → AI paused,
"Dipesh is now in control." system message, sent an operator message that
appeared as "You" → visitor row updated to a live "Dipesh in control" pill
+ watcher count. Re-verified from the original Conversations tab too — same
fix, same component. `.chathome-stats` re-measured at 0px overflow post-fix.
Evidence: **Behavioral** (live WS session, both entry points) + **Code**.

**Build 382 (Chat-panel tabs: fix equal-width regression on narrower
windows):** build 381's `flex: 1 1 0` made the 5 tabs equal-width, but only
verified at a wide (1280px) viewport. Tester report from production
(agents.spiderx.ai) at a normal desktop window: "Home and Go live are not
the same width as the others." Root cause: flex items default to
`min-width: auto`, which floors each button at its own text's intrinsic
width — `flex: 1 1 0` can grow a short label's box but can't shrink a long
label's box below its content. Once the row gets narrow enough that
"System prompt"/"Conversations" (13 chars) hit that floor, they're pushed
past their equal 1/5 share and squeeze "Home" (4 chars) and "Go live"
(7 chars) below theirs. Added `min-width: 0` (overrides the default) +
`white-space: nowrap` to `.chatpage-tabs .db-tab`. **Verdict: PASS** —
browser-verified: confirmed the CSS from build 381 was genuinely live on
`agents.spiderx.ai` (`?v=381` cache-busted `styles.css` matched the local
source exactly, no CDN staleness), so the bug was real, not a cache issue.
Re-measured all 5 tab widths via `getBoundingClientRect()` after the
build-382 fix at both 900px and 1280px viewports — identical widths at
both. At a true mobile width (480px) the row hits a separate, pre-existing
overflow (no wrap/scroll on the tab bar) — out of scope for this fix, not
introduced by it, and not what was reported. Evidence: **Behavioral**
(measured on `rohan`, both viewport widths) + **Code**.

**Build 381 (Chat-panel tabs: equal width):** the 5 chat-widget tabs (Home /
Settings / System prompt / Go live / Conversations) previously sized to their
own label (`flex: 0 0 auto`, left-packed — a deliberate build-364 choice), which
read as uneven since the labels are different lengths. Changed
`.chatpage-tabs .db-tab` to `flex: 1 1 0` + centered content so all 5 tabs
split the bar evenly, same balanced-control look regardless of label length.
Scoped to `.chatpage-tabs` only — the shared `.db-tabs`/`.db-tab` primitive
used by other tab bars elsewhere in the app is untouched. **Verdict: PASS** —
browser-verified on `rohan` (org 1): all 5 tabs render equal-width across the
full bar; clicking through (Home → Conversations) still switches panels
correctly, including the tab whose label pairs with a live-count pill.
Evidence: **Behavioral** (logged-in browser) + **Code**.

**Build 380 (Chat panel: Home tab + layman Go-live tab):** the chat-widget
panel (`AgentChatPage`) gains a **Home** tab (default) and a **Go live** tab,
inserted before **Conversations** — tab order is now Home / Settings / System
prompt / Go live / Conversations. Home shows a getting-started welcome screen
(3-step "Style it → Write its system prompt → Go live" cards) until the chat
is configured (`chatConfigured` = has logs, or a `welcome_message`/
`instructions` set), then flips to an overview: conversations / live-now /
positive-rating / human-handoffs stat tiles + quick links to Settings, System
prompt, and Conversations. Go live is a non-technical 3-step embed flow (copy
snippet → paste before `</body>` or into a site builder's custom-code box →
open a live preview) — no code editing, no jargon. **Verdict: PASS** —
browser-verified both Home variants live: `maya` (unconfigured, 0 chats) shows
the welcome screen with working step-card + "Get started" nav to Settings/
System prompt/Go live; `rohan` (org 1, 38 real `web_chat` conversations) shows
the overview stats (38 conversations, 0 live, 0 handoffs) + working quick
links. Go-live tab renders the real embed snippet and the paste-target step
correctly as literal `</body>` (not an escaped `&lt;/body&gt;` entity — htm
does not decode HTML entities in template strings, so the tag is interpolated
as a JS string). Evidence: **Behavioral** (logged-in browser, both agents) +
**Code**. Housekeeping: this shipped in the same commit as the build-379
engine-aware-ledger work but the rubric entry was missed at push time,
which also let `EVAL_RUBRIC.md`'s "Last updated" line drift behind
`APP_BUILD` (380 vs 379) — `tests/test_offline.py`'s
`test_rubric_last_updated_matches_build` catches exactly this and was
failing; fixed by this entry.

**Build 379 (Engine-aware cost ledger — Standard vs Pro):** the super-admin LLM
ledger can now segment spend by voice engine, and the user picker states both
tiers are included at no extra cost (per the product decision: Pro is a free
quality upgrade). Migration `0034_voice_provider` adds a point-in-time
`voice_provider` stamp to `calls` + `llm_calls` (was only in mutable
`agents.voice_tweaks`, unrecoverable after the fact) and seeds a **₹0 Fish
`tts.pro.voice` pricing dimension** (free `s2.1-pro-free` tier; a real rate rolls
forward via the audited Pricing tab when the free window ends 2026-08-31 —
detect-only, historical `cost_paise` never re-priced). Threaded through
`_persist_call` + the `end_call` connector → `insert_call` (both the `calls` and
`llm_calls` writes). `llm_analytics_platform` gains a `by_engine` breakdown
(kind='agent', grouped by provider; fish→Pro, gemini→Standard, NULL→Legacy);
super-admin `AdminLlmLedger` renders a "By voice tier" table. Cost stays
engine-independent (metered off `model_id`+tokens); Pro's full Gemini compute was
already counted, so this only *labels* it. **Verdict: PASS** — migration applied
locally; verified `voice_provider` on both tables, Fish ₹0 row seeded, and
`by_engine` segments distinct Pro/Standard/Legacy buckets (ephemeral rows,
cleaned up). Evidence: **Behavioral** (live DB query) + **Code**. No historical
re-pricing; no user-facing price delta (both included).

**Build 378 (Voice engine UI → provider-agnostic Standard / Pro tiers):** the
Voice & behaviour engine picker no longer names the underlying providers. The
`<select>` (Gemini / Fish) is replaced by two selectable cards — **Standard**
(Real-time) and **Pro** (Most natural, Recommended, default) — each with a
one-line "clear diff" so an operator can choose on merit without seeing vendor
names. Sub-panel copy scrubbed ("Voice style" not "Fish voice"; fallback
described as Standard, not Gemini); the preview's default line and its error
message are now generic (raw backend detail → console only, never surfaced —
it can name the engine). No behaviour change: the cards still write
`voice_tweaks.voice_provider` = `"fish"`/`"gemini"`, so the build-377 live
pipeline is untouched; only the presentation layer changed. New CSS
`.vs-tiers`/`.vs-tier` (accent ring on active, radio affordance, dark theme,
mobile 1-col). **Verdict: PASS** — offline suite green (build lockstep bumped to
378 in all four files); provider strings absent from the rendered picker.
Evidence: **Code** + **Behavioral** (dashboard render check).

**Regression gate added (no build bump — tests/CI/docs only):** the evals now run
themselves so regressions can't reach prod. New `tests/test_offline.py` — a
27-check, stdlib-only **offline** unit suite (no server/DB/keys; all fixtures
synthesized in-process) covering the code with no HTTP surface that `eval_suite.py`
can't see: the Fish voice pipeline (audio codecs µ-law↔PCM 8/16/24 kHz, WAV→mono
decode, sentence flushing, the live-call `_bridge` branch **and its
degrade-to-Gemini safety net** via fake carrier/session objects), the `fish_audio`
client shape, build-number lockstep (`APP_BUILD`==`SXAI_BUILD`==CLAUDE.md==this
line), and import sanity. Wired into the pipeline two ways: a committed **pre-push
hook** (`.githooks/pre-push`, `make install-hooks`) that blocks the push/deploy on
any offline failure — and runs the online suite too when a dev server is up — and
a **GitHub Actions** workflow (`.github/workflows/evals.yml`) running the offline
suite on every push + PR on Python 3.13 (matches Railway; no secrets). `Makefile`
targets: `make eval` / `eval-offline` / `eval-online` / `eval-scenario` /
`install-hooks`. First runs: offline **27 PASS**, online **43 PASS** (47 with
`--scenario`), pre-push gate green end-to-end. Evidence: **Behavioral** (both
suites run green) + the hook's exit-code plumbing unit-checked (fails block even
when output is warning-only). This closes the Build-377 rubric gap: the Fish
live-call path now has an automated **Unit** gate (Behavioral on a real call still
pending).

**Build 377 (Fish Audio drives LIVE CALLS — Phase 2, Fish is default):** the
voice-engine preference now applies to live phone calls, not just the preview
button. Architecture: Gemini Live stays the brain (STT + reasoning + tools +
barge-in) but its own voice is **suppressed** when `voice_provider == "fish"`
(now the platform default); the agent's words — Gemini's `output_transcription`
— are spoken through Fish, sentence-by-sentence, via a new `fish_player` task in
`telephony/base.py::_bridge`. New audio helpers in `telephony/audio.py`
(`wav_to_pcm16_mono`, `pcm16_to_ulaw8k`, `pcm16_resample`) decode Fish's 44.1 kHz
WAV → 8 kHz µ-law through the existing `audioop` chain (no MP3 dep). Safety (hard
rule — never break a call): because Gemini's audio is still arriving in parallel,
ANY Fish failure (synth error, WAV decode error, or a spoken turn with no
transcription) flips `fx['active']=False` and the call **degrades to Gemini's own
voice mid-call**; if Fish isn't configured at all, the call silently stays on
Gemini. Barge-in flushes the Fish queue + bumps a generation counter so
in-flight/queued audio is dropped. Recording agent-channel now captures the Fish
audio the caller actually hears (resampled to 24 kHz). Frontend default flipped
to Fish; labels updated. **Scope:** carrier WebSocket path (Twilio/Plivo) — the
SIP-native handler still uses Gemini voice (separate, unverified transport).
**Verdict: PARTIAL** — offline-verified (WAV decode 44.1k→8k µ-law 144×20 ms
frames; sentence flushing emits complete sentences + buffers the tail; barge-in
drain empties the queue; module imports clean). **Not yet verified on a real
carrier call** (needs a live inbound call to confirm end-to-end audio + latency).
Evidence: **Unit** (audio + flushing tests) + **Code** (branch/fallback review);
Behavioral pending a live call. Phase 2 follow-ups: Fish streaming-TTS WS for
lower latency; wire the SIP-native path; stereo-mix alignment with Fish's
synth-delayed agent channel.


**Build 376 (Fish Audio default → free `s2.1-pro-free` backbone):** Phase 1's
default TTS backbone was `s1`, which requires paid Fish *API credit* and returned
**402** in prod (verified live: 503→502→402 as the key rolled out). Fish offers a
free tier — model id **`s2.1-pro-free`** (state-of-the-art, no credit, free
through 2026-08-31; same `/v1/tts` endpoint). Changed `backend/fish_audio.py`
`FISH_TTS_MODEL` default `s1` → `s2.1-pro-free` (still overridable via env to a
paid backbone). Verified with the live key: `model: s2.1-pro-free` → **200
audio/mpeg, 57677 bytes** (valid 128kbps MP3). **Verdict: PASS** — Fish preview
now produces real audio in prod with zero cost and no top-up. Evidence:
**Behavioral** (live 200 + valid MP3 bytes). Supersedes the build-375 PARTIAL.


**Build 375 (Fish Audio — selectable voice engine, Phase 1):** added Fish Audio
as a second, selectable voice engine alongside the default **Gemini native
audio** — via a "Voice engine" dropdown in Voice & behaviour. Non-breaking: the
live Gemini phone pipeline stays the default and is untouched (verified Gemini
`voice` unchanged after saving a Fish selection). New `backend/fish_audio.py`
(async client over Fish's `POST /v1/tts`, `GET /model` voice catalogue with a
curated fallback); two endpoints — `GET /api/tts/fish/voices` (authed) and
`POST /api/tts/preview` (authed, returns `audio/mpeg`). Frontend: engine
`<select>` + Fish voice picker + "Preview voice" button in `VoiceSettings`,
persisting `voice_provider` / `fish_voice_id` into `voice_tweaks`. **Verdict:
PARTIAL** — UI, persistence, and synthesis wiring all work end-to-end; the live
preview surfaces Fish's error cleanly. Actual audio playback is blocked on the
Fish account having **API credit** (calls currently return **402** — API credit
is separate from platform credit) and on `FISH_AUDIO_API_KEY` being set in the
Railway (prod) env. Phase 2 (route live calls through STT→LLM→Fish-TTS) deferred.
Evidence: **Behavioral** (dropdown renders both engines, 13 voices load, preview
POSTs and returns the 402 gracefully, save round-trips `voice_provider=fish`).


**Eval suite: `--scenario` live-chat mode added (tests/docs only):** the harness
now optionally drives a real customer chat over `/ws/session?mode=chat` (opt-in,
gated on `GEMINI_API_KEY`) — asserting session-ready, a model reply, follow-up
chips (build 350; SKIP if none in-window since they're a background LLM call),
and visitor provenance capture (build 351 — mobile UA → device, `?host=` Referer
→ source, read back off the persisted chat). Verified: `--scenario` alone → 4/4;
full run `--scenario` → 47 checks green. Provenance rows in EVAL_SUITE.md
promoted from Scenario → Automated. Evidence: **Behavioral**.

**Feature eval + rubric suite added (no build bump — tests/docs only):** new
`tests/eval_suite.py` — a standalone, stdlib-only harness that hits the live API
(stub `X-User-Id` auth, snapshot+restore on every mutation) and scores each
feature area PASS/FAIL/SKIP, plus `EVAL_SUITE.md`, the breadth rubric catalogue
(feature → acceptance criterion → evidence tier). Covers 13 areas / 43 automated
checks: platform, auth & security (anon-401 + stripped embed read), agents
read/update, chat branding round-trip, guardrails/policy, knowledge, calls +
date filter, XLSX export (2 vs 3 sheets), entitlements/add-ons, plans/billing,
super-admin subscription table + plan override, embed/public, provenance
columns. Out-of-band features (voice/chat WS, Eva build, telephony, Razorpay,
UI) are catalogued as Scenario/Manual with pointers to the existing scenario
scripts. First run: **43 PASS · 0 FAIL**. Evidence: **Behavioral** (suite runs
green vs the live server).

**Build 374 (System prompt loads instantly once drafted — no repeat LLM fetch):**
the System prompt tab auto-drafts via `/chat-instructions/suggest` (an LLM call
that measures **~14s** locally) whenever `chat_settings.instructions` is empty —
but the draft was never saved, so it re-fetched on EVERY open (the slow load).
Fix: the auto-draft now **persists** its result to `chat_settings.instructions`
(a `PATCH` right after the draft returns), so subsequent opens load the saved
prompt straight from chat_settings — no fetch, no spinner. A saved prompt was
already loaded directly; this closes the never-saved case. Manual "Regenerate"
still leaves saving to the operator (fixed the handler so the click event isn't
mistaken for the auto=true flag). Verified live (temp entitlement, Rohan
cleared): first open drafted 806 chars and persisted them; reload loaded 808
chars at t=0 with no "Drafting" state across 4 samples. Evidence: **Behavioral**.

**Build 373 (smooth page/tab transition — no more content "break dance"):**
navigating hard-swapped the main content with no transition, so each page/tab
load popped/jumped. The `.db-content` container is now keyed by `activeKey`, so
it remounts per navigation and replays a short fade + 7px lift
(`@keyframes db-content-in`, 0.26s ease-out) instead of a hard cut. Honours
`prefers-reduced-motion` (animation disabled). Covers all sidebar-route
navigations (the agent sub-pages that read as tabs are routes → covered).
Verified live: computed `animation-name db-content-in`, 0.26s, keyframes
present; the content node is a fresh element on each nav (remount confirmed) and
renders correctly at rest. Evidence: **Behavioral**.

**Build 372 (split Add-ons vs Workspace sections + stylized paid footer):**
- The single "Workspace & add-ons" header is replaced by a **per-group section
  header** — Add-ons / Developer / Account each get their own labelled
  `.db-nav-divider` (the first, `.db-nav-divider-lead`, keeps the 28px gap from
  the agent-config groups above). Verified live: 3 headers — Add-ons (lead,
  margin-top 28px) · Developer · Account.
- The paid-plan nav footer is stylized: instead of plain grey "Starter plan" +
  a text "Manage" link, it's now a **brand-tinted plan chip** ("● Starter",
  gradient dot, `.db-nav-foot-plan-paid`) + a **bordered pill "Manage" button**
  (`.db-nav-foot-manage`). Reads the plan label from `plan.plan.label`. Verified
  live on a temp Starter plan: chip "Starter" with gradient bg, Manage button
  border `rgba(124,108,246,0.32)`, pill radius; free plan still shows
  Free + Upgrade. Evidence: **Behavioral**.

**Build 371 (division = flat direct items + more separation):** two follow-ups
to 370's nav division:
- **More space** between the voice-agent groups and the division — the
  `.db-nav-divider` top margin went 14→28px (plus a short leading tick).
- The three division entries (Add-ons / Developer / Account) no longer render as
  collapsible groups; the division renders as **flat direct menu items**
  (`.db-nav-flat`, no group head / chevron / expand). Because Account carries 4
  pages, the flat list is 6 items: Chat widget [Add-on] · Webhooks & data ·
  Workspace · Team & invites · Billing & plan · Integrations — nothing orphaned
  (collapsing Account to one link would have lost Team/Billing/Integrations,
  which have no in-page nav). Non-division groups stay collapsible. Verified
  live: no Add-ons/Developer/Account group heads remain, all 6 items are direct,
  divider margin-top 28px, label renders "Workspace & add-ons". Evidence:
  **Behavioral**.

**Build 370 (nav division: Add-ons / Developer / Account set apart):** the
Add-ons (build 367), Developer, and Account groups are now a distinct nav
division — separated from the agent-config groups above by a labelled separator
("WORKSPACE & ADD-ONS", `.db-nav-divider`). Each of the three groups carries a
`division: true` flag; the render inserts the divider before the FIRST division
group only (guarded so it never leads the nav when Account is the sole group,
e.g. no agent open). Verified live: one divider, label "Workspace & add-ons",
immediately precedes the Add-ons group. Evidence: **Behavioral**.

**Security: anonymous `chat_observe` can no longer watch live customer chats.**
The `/ws/session` handler defaults `user_id` to the founder for header-less
connections (needed so the anonymous embed chat/voice + Eva build flow work).
But the `mode=chat_observe` branch (operator watching a live chat) ran
`require_agent_member(user_id, agent)` against that founder default — so an
anonymous caller who knew a live `sid` could watch the founder's live customer
conversations. Fix: track `user_id_authed` (True only when `?user_id=` resolves
to a real user) and reject `chat_observe` when it's false. The customer
chat/voice + builder branches are untouched (they legitimately run anonymously).
Evidence: **Code** (guard is a contained early-return; the leak needs a guessed
live sid so wasn't reproduced end-to-end).

**Known residual (lower severity, flagged not fixed):** the same founder-default
also lives in `_agents_brief` (gemini_bridge) and the builder agent-save
(`owner_id` in gemini_bridge:4517 / chat_bridge:1063). Effect: anonymous Eva can
be prompted to list the founder's agent *names*, and an anonymous direct-WS
`save_agent` writes a *draft* (unpublishable without paid+auth) into the
founder's org — MEDIUM/LOW (names + draft spam, not transcripts/recordings/
secrets/money, which are all closed). These sit inside the intentionally-
anonymous Eva build flow, so removing them needs a coordinated change + voice/
build-flow testing; deferred to avoid breaking anonymous Eva.

**Build 368 (loading spinner on the system-prompt editor):** the System prompt
tab auto-drafts the prompt via an LLM call (`/chat-instructions/suggest`) on
first open when empty — during which the textarea sat empty with only a
placeholder, reading as an empty/broken box. Now while `suggesting` is true the
textarea is covered by a centered overlay — `.db-spin` spinner + "Drafting your
system prompt…" (`.chatcfg-instr-loading`) — so the loading state is explicit.
Verified live: clicking Regenerate showed the spinner overlay over the editor.
Evidence: **Behavioral**.

**Build 367 (Chat widget presented as a paid add-on in the nav):** the
"Chat widget" nav item was inside the voice agent's "Test & launch" group. Moved
it out into its own top-level **"Add-ons"** group (icon `extension`, positioned
after Voice & behaviour), with a purple **"Add-on" badge** on the item
(`.db-nav-item-badge.is-addon`) so it reads as a separate paid channel, not a
core voice feature. Verified live: Chat widget renders under "Add-ons" with the
badge and is no longer in "Test & launch". (Part 2 — recording the add-on in the
admin subscription table — pending a clarification.) Evidence: **Behavioral**.

**Security follow-up: `/api/admin/storage-health` now super-admin gated.** The
one `/api/admin/*` route that wasn't super-admin gated (it exposed server paths
+ recording totals to any signed-in user). Now `current_user()` (401 for anon,
from the build-365 fix) + `db.is_super_admin(user["id"])` → 403 otherwise.
Verified on a fresh local server: anon → 401, founder (super-admin) → 200.
Evidence: **Behavioral**.

**SECURITY (critical): closed the systemic anonymous-founder auth bypass.**
`current_user()` fell back to `db.get_founder()` for any header-less request, so
an UNAUTHENTICATED caller was treated as a fully-privileged founder across ~55
endpoints — confirmed live on prod: anon `GET /api/me` returned the founder
(incl. `is_super_admin`), `GET /api/agents` enumerated their agents, and
`GET /api/agents/{id}` leaked `system_prompt`; destructive routes (delete agent,
provision numbers, place outbound calls, rewrite prompts, flip plan/add-ons)
were all reachable as the founder. Fix: `current_user()` now **raises 401** when
no real user resolves; a narrow `allow_anonymous=True` (returns `None`) is used
only by the two genuinely-public endpoints — the `by-slug` embed read and the
support-ticket intake. The `?u=`/`?user_id=` media flow and the `/ws/session`
handler (resolves the founder id directly, not via `current_user`) are
unaffected. Verified on an isolated instance: anon → 401 on every protected +
destructive endpoint; embed `by-slug` anon → 200 stripped shape; authed
(`X-User-Id`) → 200 full; support anon → 200; SPA logged-out landing renders
(no crash, benign 401s the SPA already tolerates); SPA logged-in dashboard loads
all 27 agents. Evidence: **Behavioral**.

**Build 365 (chat tabs left-aligned + system-prompt editor to the top):** two
tweaks reversing part of 363's layout per feedback:
- The three chat tabs (Settings / System prompt / Conversations) are now
  **left-aligned** at natural width (`.chatpage-tabs .db-tab{flex:0 0 auto}`)
  instead of stretched equal-width. Verified live: widths 84/131/125px, packed
  left in a 952px bar.
- On the **System prompt** tab, the system-prompt **text area moved to the top**
  (directly under the panel header), with the knowledge cards + Do's & Don'ts
  below it. Verified: `.chatknow-instr` precedes `.chatkb-grid` in the DOM.
Frontend-only; `APP_BUILD` staged in isolation (a concurrent session's
`current_user` auth-hardening in app.py was left untouched/unstaged). Lands as
365 to avoid colliding with that pending build. Evidence: **Behavioral**.

**Build 363 ("What it knows" tab → "System prompt", polished):** four changes to
the Chat widget tab (renamed from build 358's "What it knows"):
- Tab **renamed to "System prompt"** and the three chat tabs (Settings / System
  prompt / Conversations) are now **equal width** (`.chatpage-tabs .db-tab{flex:1}`).
  Verified: all three 315px.
- The **"what it knows" summary cards moved to the top** of the tab (above the
  system-prompt editor, which now sits at the bottom). Verified: grid precedes
  `.chatknow-instr` in the DOM.
- The info-card grid is now **5 columns in one row** (`repeat(5,minmax(0,1fr))`,
  wraps below 1080px); the Do's & Don'ts card still spans full width. Verified:
  5 columns, 5 info cards.
- **Emoji → professional mono line icons** in every card header (book / wrench /
  target / clipboard / globe; shield + check/x on Do's & Don'ts). Verified: 6
  header SVGs, no emoji in headers (per-group data emoji in the Knowledge-topics
  list are content, left as-is). Evidence: **Behavioral**.

**Build 362 (multi-instance guardrail sync via Postgres LISTEN/NOTIFY):**
build 361's SSE fan-out was in-process only — a policy save on instance A
wouldn't reach SSE clients on instance B. Now `policy_stream.publish()` fires
`pg_notify('sxai_policy', <json>)` (reaches every instance), and each process
runs one dedicated, auto-reconnecting `LISTEN` connection (started as a
background task at app startup, closed on shutdown) whose callback fans out to
that process's SSE subscribers. Payloads over ~7.5 KB (custom-rule blobs) fall
back to a `refetch` notify + DB re-read to stay under Postgres' 8000-byte NOTIFY
cap; a NOTIFY failure falls back to same-process fan-out. SSE endpoint contract
unchanged. Verified against the live DB: (1) a clean boot logs
`LISTEN sxai_policy established`; (2) a policy PATCH delivered `event: policy`
to an API SSE client AND a raw NOTIFY to a **separate standalone LISTEN
process** — proving cross-process delivery. (Single-worker dev-server needed a
manual restart mid-test: an open SSE stream had blocked uvicorn --reload's
graceful shutdown; unrelated to the code, which boots clean.) Evidence:
**Behavioral**.

**Build 361 (server push for cross-device guardrail sync):** build 360's live
sync was same-browser only (BroadcastChannel). Now it also crosses devices/users
via SSE: new `backend/policy_stream.py` (in-process pub/sub) + a
`GET /api/agents/{id}/policy-stream` endpoint (auth via `?u=<uid>` since
EventSource can't set headers; 25s heartbeats). `patch_agent` publishes the new
`agent.policy` on any policy PATCH, tagging the saver's `origin` (a per-tab
`X-Client-Id` stamped by the global fetch patch). `usePolicySync` now also opens
an EventSource and applies incoming policies whose `origin` isn't this tab's, so
the saver never echoes to itself. Single-process fan-out (current deployment);
noted to swap for Redis/LISTEN-NOTIFY if scaled to multiple instances. Verified:
(1) curl SSE stream received `event: policy` after a PATCH; (2) an external curl
PATCH — NOT a BroadcastChannel participant — live-updated the open dashboard tab
via SSE alone (`name_caller` false→true, URL unchanged). Evidence: **Behavioral**.

**Build 360 (live cross-tab sync of guardrails — no reload):** saving Do's &
Don'ts on either surface (the Guardrails page OR the chat "What it knows" tab)
now pushes the new `agent.policy` to every OTHER open tab of the same agent via
a module-scope `BroadcastChannel("sxai-agent-policy")` + a `usePolicySync`
hook; the receiving surface calls `setPolicy`/`setGpolicy` (+ `reloadKb`) so it
updates instantly with no reload/navigation. Graceful no-op where
BroadcastChannel is absent (remount-on-nav sync still applies). The chat tab
shows a brief "⟳ updated from another tab" flash. Verified live with TWO tabs
(Guardrails page + chat tab): toggling `name_caller` on the Guardrails page
flipped it ON in the chat tab with the URL unchanged; toggling `no_competitors`
on the chat tab flipped it ON on the Guardrails page — both directions, no
reload, changes coexisting. Evidence: **Behavioral**.

**Build 359 (edit Do's & Don'ts inline on the "What it knows" tab):** the
read-only guardrails card is now a full inline editor — the same 5 Do's / 5
Don'ts preset toggles + custom-rule textareas as the Guardrails page, saving to
the same `agent.policy` via `PATCH {policy}`. Extracted the preset catalogue to
module scope (`GUARDRAIL_DOS`/`GUARDRAIL_DONTS` + `guardrailPolicyFrom`) so the
Guardrails page and this tab share ONE source of truth (no drift). Always-on
safety rules stay implicit with a link to the full Guardrails page. Verified
live (temp entitlement): 5+5 toggles reflect Rohan's saved policy; toggling
`offer_transcript` + Save round-tripped to `agent.policy` (false→true→restored),
"✓ Saved"; the standalone Guardrails page still renders 10 toggles + saves
(refactor intact). Evidence: **Behavioral**.

**Security: public by-slug endpoint no longer leaks operator IP (backend-only,
no build bump):** `GET /api/agents/by-slug/<slug>` is the UNAUTHENTICATED read
the embed uses, but `_public_agent()` only stripped carrier secrets — so it
returned the agent's full `system_prompt`, `guardrails`, and `variables`
(transfer numbers, prices, addresses) to anyone who knew a slug (confirmed live
on prod: Kavya's 987-char prompt was readable). Root cause: `current_user()`
falls back to the founder for header-less requests, so an anonymous read looked
authenticated and, if the agent sat in the founder's org, got the full shape.
Fix: gate the full shape on a real client-supplied identity (`X-User-Id` header
or `?u=`); everyone else gets a new allowlist-only `_public_agent_embed()` —
`id/slug/name/persona/locale/chat_settings` (minus `chat_settings.instructions`
and `allowed_domains`). Verified locally: anon read → 6 keys, no
`system_prompt`/`guardrails`/`variables`, `chat_settings` keeps starters+colours
(embed still works); `X-User-Id:1` owner read → full shape (dashboard editor
still works). Evidence: **Behavioral**.

**Build 358 (chat instructions move to the "What it knows" tab):** the prompt /
instructions editor was extracted from the Chat → Settings accordion and placed
on the **"What it knows"** tab, which now frames the agent's brain as three
things: what it's **pre-trained on** (existing knowledge/tools/goals/captures
cards) · what **you instruct** it (the moved editor, with ✨Suggest/Regenerate,
⤢Expand, and its own Save) · the **Do's & Don'ts** guardrails. The old
read-only "Guardrails" card became a full-width **Do's & Don'ts** card (✅ dos /
⛔ don'ts from the knowledge endpoint) with an **Edit →** link to the Guardrails
page. Settings' "Conversation & behaviour" section keeps starters/language/
handoff and shows a pointer note. Verified live (temp entitlement): editor
renders + Save PATCHes chat_settings ("✓ Saved"); Settings no longer holds the
textarea; dos/don'ts render (3 dos / 2 don'ts for Rohan). Evidence: **Behavioral**.

**Voice agents: stop repeating the previous topic in the next answer (prompt
fix, no build bump — backend-only):** reported on the prod Gajraj/Kavya agent
(ID 2) — caller asked the showroom address (agent offered to text it), then
switched to the Verna, and the Verna answer *re-raised* the "text you the
address" offer. Root cause: the shared phone-agent conventions
(`phone_ai_conventions._silence_and_turn_taking`, injected into every call
agent's prompt via `_agent_system_prompt`) had turn-taking rules but no
"stay on the current question / say each thing once / don't re-raise an
unanswered offer" rule (the text-chat path already had one). Added a
"One topic at a time — don't drag the last one forward" block. Platform-wide
for all voice agents; takes effect on the next call. Verified: composes into
the conventions block and is wired into the customer call prompt (line 1309).
Evidence: **Code** (prompt-only — not call-tested; requires a live call).

**Mobile end-to-end flow — verification (build 357, no code change):** drove the
whole chat flow in the real embed.js widget at 375px on a host page: open →
home + 4 starters (header fully visible, not clipped); send "What are the ticket
prices?" → user + model bubbles render correctly; **3 grounded follow-up chips**
appeared (in-language) and tapping one continued the conversation; **New chat**
cleared the transcript and returned to the fresh home. Combined with the
separately-verified connection-issue recovery (auto-reconnect + 36px Reconnect
bar) and return-to-home on end, the mobile experience is confirmed working
end-to-end across builds 351/354/356/357. Evidence: **Behavioral**.

**Build 357 (mobile tap target for the Reconnect button):** the connection-issue
Reconnect button was 28px tall — under the ~44px mobile ideal. At `≤560px` it now
gets `min-height:36px` + roomier padding. Verified in 375px emulation: rendered
112×36 (was 112×28). Evidence: **Behavioral**.


**Build 356 (graceful "connection issue" recovery):** a dropped socket no longer
leaves the visitor on a dead "connection issue" screen. Two-part handling:
- **Silent auto-reconnect once** per error episode (2.5s) via a new `reconnect()`
  that keeps the transcript (it's still in sessionStorage on error — only cleared
  on a clean end — so the fresh WS *resumes* the conversation, doesn't wipe it).
  An `errRetryRef` guard spends exactly one retry and only re-arms on a healthy
  `ready`, so a genuinely-down server can't cause a reconnect loop. Skipped in
  the operator preview.
- **Manual Reconnect bar** (`.chatembed-reconnect`) shown on the error state with
  the message + a Reconnect button, so if the auto-retry doesn't fix it the
  visitor has a clear one-tap recovery instead of a dead screen.
Verified in a live embed: a transient socket error auto-reconnected (socket count
1→2, status back to "online", no loop); a persistent error (chat-not-entitled)
showed a stable Reconnect bar with no reconnect loop. Evidence: **Behavioral**.

**Build 354 (mobile: chat panel header no longer clipped):** on mobile the
popover panel used the desktop `bottom:74px` + `height:min(600px,100vh-100px)`,
so on a phone (esp. an in-app browser like Instagram, where `100vh` spans behind
the address bar) it extended above the visible viewport and the **header was
clipped behind the browser chrome**; there was no mobile handling for popover
(only drawer). Fix: at `≤560px` the popover is now `position:fixed`, anchored to
the real viewport (`top`/`bottom` with `env(safe-area-inset-*)`), so the header
is always visible; the "×" close moves INSIDE the header top-right (no room for
the outside-corner button on a full-height panel), and the iframe header's
mobile right inset grows to 46px so "New chat" clears it. Desktop/drawer/
fullscreen untouched. Verified in 375px emulation: panel `position:fixed`, full
header (avatar/name/New chat/×) visible, close inside + clear of New chat. Also
strengthened build-351's return-to-home so an ended chat with an un-answered
rating prompt still falls back to home after 15s. Evidence: **Behavioral**.

**Build 355 ("Include full transcripts" export checkbox):** the Chat log
transcript sheet (build 353) is now opt-in. A checkbox in the Conversations
export toolbar adds `transcript=1` to the export URL; the endpoint only pulls
the heavy `transcript` column (`include_transcript`) and only appends the
"Chat log" sheet when set — so the default export stays light (Summary +
Conversations). Verified over HTTP: no param → 2 sheets, `transcript=1` → 3
sheets; and in the dashboard the checkbox toggles the URL param. (Build 354 was
taken by a concurrent session's mobile close-button fix; this lands as 355 to
avoid a number collision.) Evidence: **Behavioral**.

**Build 353 (legible links on branded response bubbles + chat returns to home on end):**
two embed polish items:
- Link legibility: links inside a bot bubble were the bright accent on a pale
  brand background (Moments: `#e45dbf` on `#f4e6fb` ≈ 2.6:1, fails WCAG). They
  now use a DEEPER shade of the SAME accent via `color-mix(... 55%, #150512)` +
  `font-weight:600` + clearer underline — brand hue preserved, contrast ≈ 6.3:1
  (AA). Plain-accent fallback for no-`color-mix` browsers; dark-theme bubbles
  keep the bright accent. Black body text unchanged (already high-contrast);
  the lever for that is a lighter `bot_bubble_color` tint. Verified in-browser:
  computed link `≈#873571`, contrast 2.6→6.3. Evidence: **Behavioral**.
- On chat end the widget now returns to the fresh home (starter questions)
  instead of a dead "chat ended" screen — a `useEffect` calls `newChat()` once
  any post-chat rating is resolved (short delay for the goodbye). Verified: a
  simulated `call_ended` reconnected and showed the home + 4 starters. (Shipped
  in the build-351 sweep; recorded here.) Evidence: **Behavioral**.

**Build 353 (full chat log sheet in the report):** the XLSX now has a third
sheet, **Chat log**, with every message of every conversation in the window —
grouped chat-by-chat under an accent header row (Chat N · date · outcome ·
captured info), Visitor vs agent speaker labels, agent turns zebra-shaded.
`list_calls_for_agent(include_transcript=True)` pulls the `transcript` column
only for the export (UI list still omits it); `_parse_transcript` handles the
JSON-string turns + legacy plain text. Verified over HTTP: 3 sheets
(Summary / Conversations / Chat log), transcripts render with full turns.
Evidence: **Behavioral**.

**Build 352 (provenance analytics on the report Summary):** the XLSX Summary
sheet now carries two more breakdown tables below Outcome breakdown — **Visitor
device** (Mobile/Desktop/Tablet split) and **Top sources** (source domain,
top 8) — each with count + share, built from `extracted._provenance`. Refactored
the three tables through one `_table()` helper. Verified: device split
Mobile 57% / Desktop 29% / Tablet 14%, sources led by moments-shop.com 57%.
Evidence: **Behavioral** (openpyxl round-trip).

**Build 351 (visitor provenance labels + Conversations UX polish):** three
changes to the Chat → Conversations tab:
- Visitor provenance: the chat detail now shows where the visitor came from as
  nice chips (device / browser / OS / source domain / locale). Captured
  backend-only from the WS handshake — `_parse_ua()` reads the User-Agent, and
  the source domain comes from embed.js's `?host=` on the iframe URL (the WS
  `referer`); stored under `extracted._provenance` (`_chat_provenance`). No
  frontend WS plumbing. Verified: parser handles iOS/Android/Windows/macOS/iPad
  correctly; label chips render 📱 Mobile · Safari · iOS · 🔗 moments-shop.com ·
  en-GB. Excluded from lead "captured info"; surfaced as **Device** + **Source**
  columns in the XLSX Conversations sheet for analytics.
- Date filter now defaults to the **last 30 days** (From/To pre-filled).
- The From/To toolbar is **sticky** (position:sticky; top:0) so it stays put
  when the list scrolls. Verified live in the dashboard (temp chat entitlement).
Evidence: **Behavioral**.

**Build 350 (grounded follow-up question chips after every reply):** the
customer chat now suggests 2-3 natural next questions as tappable chips after
each answer so the conversation flow is never lost. New `_generate_followups`
(fast flash model, JSON out) is grounded in (a) the running conversation, (b)
the agent's persona/scope — its goal, and (c) its guardrails: it is explicitly
told never to lead the visitor toward anything the agent refuses/can't do, and
already-asked questions are filtered out. Emitted as the existing
`quick_replies` frame (reuses the `.chatembed-chips` render — tap sends the
question), fired as a background task after `turn_complete` with a `turn_seq`
guard that drops stale chips if the visitor types first, and **suppressed when
the agent already showed its own widget** (quick_replies / form / cards /
handoff) that turn. Operator toggle `chat_settings.followup_suggestions`
(default on) in the Conversation & behaviour accordion. Verified end-to-end via
an in-process WS client (temp local chat entitlement): "What is the ticket
price?" → "Rs 500" → chips ['Tickets kaise khareedun?', 'Payment kaise hoga?',
'Yeh event kab hai?'] — natural, in the conversation's language, within scope.
Evidence: **Behavioral**.

**Build 349 (Conversations: live chat in the right pane + date filter + XLSX export):**
three changes to the Chat → Conversations tab:
- The live-chat watch/join view now opens INLINE in the right pane (a new
  `LiveChatModal({inline})` variant / `.livechat-inline`) instead of a modal
  overlay; recent chats already used the pane. Clicking a live row clears the
  recorded selection and vice-versa. Verified live: clicking a live chat shows
  it in `.chatconv-detail` with no `.db-modal-backdrop`; clicking a recent chat
  swaps the pane to its transcript.
- Date-range filter (From/To) above the list → `date_from`/`date_to`
  (YYYY-MM-DD, inclusive) on `GET /api/agents/{id}/calls` and the export.
  Verified: full window 26 rows, one-day filter 11 rows.
- Client-ready XLSX export (`GET /api/agents/{id}/chat/export.xlsx`, new
  `backend/chat_report.py` via openpyxl): Sheet 1 branded KPI summary (period,
  total, resolved-by-AI, handoffs, positive-rating %, leads captured, avg
  length, outcome breakdown), Sheet 2 one styled row per chat. Verified: valid
  .xlsx, 2 sheets, correct KPIs (80% = 4/5 rated, avg 2m30s), honours the date
  window. Evidence: **Behavioral** (dashboard render-tested with a temp local
  chat entitlement; export validated over HTTP + reopened with openpyxl).

**Build 348 (professional mono icons on the settings accordions):** replaced the
emoji in the six chat-settings accordion headers with monochrome line icons
(stroke `currentColor`, 1.8px, shared with the chevron style): palette, layout,
tag, chat-bubble, bell, shield. Muted grey by default, darkening on hover/open
(`.chatcfg-acc-icon`). Verified live in the dashboard: 6 SVG icons render with
full shapes, titles emoji-free. Evidence: **Behavioral**.

**Build 347 (chat-settings form accordion):** re-lands the collapsible-section
refactor of the Chat → Appearance/behaviour settings form as its own complete
commit (JS + CSS together, fixing the 345 split where the CSS was uncommitted).
Six sections — Brand & colours, Layout & text, Name/launcher/welcome,
Conversation & behaviour, Proactive nudge, Trust & privacy — each showing a
one-line hint while collapsed; Brand opens first. Operator-facing only (no embed
change). Evidence: **Code** — app.js passes ES-module syntax check, CSS braces
balanced. NOT render-verified in the dashboard: the chat-settings form is gated
behind chat entitlement and the local dev user is on the free plan (paywall),
so the accordion UI needs a manual check on a chat-enabled account.

**Build 346 (isolate the chat-settings accordion out of 345):** a chat-settings
form accordion refactor (not part of the mobile fix) had landed on disk and got
bundled into build 345's app.js. This reverts app.js to the flat form so 345
stands as the mobile fix alone; the accordion re-lands complete (JS + CSS) in
build 347. No behavioural change vs the pre-accordion form. Evidence: **Code**.

**Build 345 (fix: "New chat" un-tappable on mobile):** regression from build 343
— removing the header's right-padding reservation and floating the "×" close at
the panel's top-right corner left the "New chat" control (which collapses to a
bare ~26px icon when the panel iframe is <360px, i.e. essentially every phone)
jammed into the rounded corner directly under the close button, so it was very
hard to tap and mis-taps hit the close. Diagnosis: the `newChat()` handler
itself works (native click fires it; the button centre hit-tests to the iframe)
— it was purely a tap-target/geometry problem. Fix: (1) restore a 30px right
inset on the real-embed header so the actions stay clear of the corner + close
(`.chatembed:not(.chatembed-contained) .chatembed-head`), and (2) enlarge the
icon-only New chat to a full 38×38 target (+ 38px min on the human button) in
the `max-width:360px` query. Verified in the real embed.js widget at 375px: New
chat 38×38, 14px clear of the close, centre hit-tests to the iframe. Evidence:
**Behavioral**.

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

## Round 5 (VAPI head-to-head, Gajraj Hyundai replica agent) — systemic fixes

Operator ran a same-prompt/same-KB quality comparison of production agent id 6 (Gajraj Hyundai / "Kavya", automotive) against a VAPI replica handling the identical script, and supplied 10 VAPI call recordings as the target quality bar. Transcribing + diffing both sides found Kavya's own system_prompt (RULE 1–9, a very detailed hand-authored voice-agent spec) already covers or beats VAPI on the structural issues found in VAPI's own "good" calls — single clean close (vs. VAPI repeating its goodbye 2–4x in 3/10 calls), no dead air (RULE 1's filler-token discipline vs. 9–15s silences in 5/10 VAPI calls), a mandatory name/phone capture gate. The real gap wasn't prompt quality — it was two silent config-integrity failures that inspecting agent 6's actual production row (Railway Postgres) surfaced directly: `connectors` never included `email_send` despite RULE 4 mandating "call send_email ONCE" for every lead, and the RULE 4b sales-SMS body's `{{GAJRAJ_SALES_SMS}}` was never set under Variables. Neither fails loudly — prompt composition never raises on either — so a tester call sounds fine while every captured lead is silently dropped before it reaches the dealership. Gajraj is one example tenant; the fix generalizes to a platform-level static check, not a data patch to this one agent's row.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U27 | Config-rot (unresolved deploy-time variables, a prompt instructing a connector call that isn't provisioned) is surfaced to the operator, not silently swallowed | PASS | Unit+Code | 409 | New `gemini_bridge.agent_config_warnings(agent)` — pure-text, no model call — flags (a) `{{VAR}}` left unresolved in persona/greeting/chat-instructions (any name), plus in the freeform system_prompt but ONLY `{{ALL_CAPS}}`-style names, (b) system_prompt telling the model to call a connector (`send_email`/`email_send`, `send_sms`/`sms_send`, …) absent from `agent.connectors` (`end_call` excluded — always force-included at session-open regardless of the operator's list). Reproduces the exact agent-6 shape in a unit test: `email_send` instructed but not provisioned, `{{GAJRAJ_SALES_SMS}}` unset. Wired into `_public_agent()` (so `GET`/`PATCH /api/agents/{id}` return `config_warnings` immediately) and into the existing hourly `agent_healthcheck` scheduler (emits `agent.config.warning`, kept outside the `agent.healthcheck.*` prefix so it can't shadow the WS-probe connectivity dot) — the healthcheck module's own docstring already promised catching "a broke template variable reference" (build 231-era) but never implemented it; this closes that gap for every published agent, not just Gajraj. Verified against agent 6's real production row: 7 warnings (4 genuine ALL_CAPS gaps + `email_send` + `fish_voice_not_selected`, see U30, + one accepted false positive on `ACTIVE_LANGUAGE`). The scheduler's Level-2 query previously selected only `id, name, slug, org_id` — extended to also pull `system_prompt, persona, greeting, connectors, variables, voice_tweaks, chat_settings`, or `_probe_one`'s new check would have silently run against an all-empty agent dict and never fired |
| U28 | `{{variable}}` detection must not flag or corrupt intentional in-prompt state notation | PASS | Unit | 409 | **Caught before shipping**, not a hypothetical: an earlier version of this fix (a) made `_substitute_variables` blank any unresolved `{{key}}` at runtime and (b) scanned system_prompt for every `{{key}}` unconditionally. Sanity-checking against agent 6's real row surfaced **27** "warnings" — Kavya's system_prompt has a whole "STATE VARIABLES" section using `{{lower_snake_case}}` as intentional notation for conversation state the MODEL fills in as it talks (`{{caller_name}}`, `{{price_quoted}}`, 20+ more), never meant to be resolved by config. Blanking those at runtime would have corrupted lines like `Namaste {{caller_name}}, thank you for calling` → `Namaste , thank you for calling` on every real call. Fixed by (a) reverting `_substitute_variables` to its original leave-literal-when-unresolved behaviour (unchanged from pre-409) and (b) restricting the system_prompt scan to `{{ALL_CAPS}}` names only (the deploy-time-constant convention) — cuts the false-positive count from 27 to 7 against the same real row. Regression-tested directly against that shape (`test_agent_config_warnings_ignores_state_variable_notation_in_system_prompt`) |
| U29 | Dashboard surfaces config-rot on the agent the operator is looking at, not just in the events feed | PASS | Code | 409 | `AgentOverviewPage` renders a `.db-config-warning` banner (light/dark themed) listing each `config_warnings[].detail` when non-empty. Computed server-side per request, no extra query |
| U30 | Fish Audio provider status + voice-selection gap (raised by "across both Gemini and Fish Audio" ask) | PASS (warning added) | Code+Unit | 409 | **Correction to an earlier read of this codebase**: Fish Audio is NOT preview-only. The real production call path is `backend/twilio_bridge.py` → `backend/telephony/base.py::run_call`/`_bridge`, which imports `_agent_system_prompt` from `gemini_bridge` directly (so U27/U28 already cover it) and defaults `voice_tweaks.voice_provider` to `"fish"` platform-wide when unset — Fish is the *default* live engine, not a Phase-2 stub (`backend/sip/gemini_handler.py` is a separate LAN-only native-SIP path per the "Out-of-band tooling" note below, not what agent 6 runs on). Confirmed live on agent 6: `voice_provider="fish"`, `fish_voice_id=""`. Blank `fish_voice_id` → `fish_audio.synthesize()` omits `reference_id` from the request entirely, so Fish speaks in its own generic default voice — never one picked for this agent's locale/persona (hi-IN automotive receptionist). `agent_config_warnings()` gains a third kind, `fish_voice_not_selected`, for this shape. Deliberately a warning, not an automatic reroute to Gemini — silently changing which engine speaks on every live call is an operator product decision, not something a config-lint should make unilaterally |

## Round 6 (critical bug report — agent "keeps talking on its own" in the web demo)

Operator: "If I test a call using the web demo, and remain silent, the AI keeps talking on its own." Root-caused against production `calls` rows (Railway Postgres, read-only) rather than guessing — pulled every non-`web_chat` (i.e. voice) call from the last 48h across agents 6 and 8 and read the stored `transcript` JSON directly.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U31 | Diagnose: is the agent actually generating unprompted turns, or is this a UI/logging illusion? | CONFIRMED real | Behavioral | — | Every one of the last several voice-test calls (ids 331–334 checked in full) has **100% `role: "model"` turns and zero `role: "user"` turns**, yet the agent visibly progresses through a multi-step dialogue as if answering someone — e.g. call 333: asks "personal or commercial?" then, with no intervening turn, moves straight to "would you like a test drive?", and includes a bare "Umm, yes, I understand." with nothing preceding it to understand. `_ConversationMemory`/`sc.input_transcription` in `gemini_bridge.py` does capture real user turns when they occur (confirmed by reading the code), so this isn't a transcript-logging gap — the model genuinely had no real caller input driving these turns |
| U32 | Root-cause: why does the model generate turns with no real input? | CONFIRMED — speaker-bleed echo | Code | — | `frontend/audio-engine.js`'s own long-standing comment (present since the initial commit, build 177) already names this exact failure mode: without echo cancellation, "Eva's audio bleeds back through laptop speakers into the mic, Gemini's input transcription catches phrases Eva just said, the model generates a parallel response to its own voice." `echoCancellation: true` was already on, but the SAME file's `_checkBargeIn` documents that bleed still reaches the mic at peaks of 5000–8000 (measured empirically, hence its barge-in threshold being set to 12000) — and **every mic chunk was forwarded to the server unconditionally**, `_checkBargeIn` only decided whether to interrupt local playback, never whether to transmit. So bleed under the local 12000 barge-in bar still reached Gemini's server-side VAD, got transcribed as real speech, and the model replied to itself — a self-sustaining loop with the caller completely silent, matching the operator's report exactly |
| U33 | Fix: stop echo bleed from reaching the server as fake user speech | PASS | Code | 410 | `_checkBargeIn` now returns whether the chunk should be forwarded at all, not just whether to flush playback; the caller in `start()` only calls `onMicChunk` when it returns true. Gate: while Eva is audibly playing (`EVA_TALKING`) and the chunk does NOT clear the existing sustained-loud bar (`USER_TALKING`, peak > 12000 for 4 consecutive chunks — the exact same threshold already tuned against measured bleed), the chunk is dropped, not sent. Deliberately reuses the existing threshold rather than inventing a new one — it was already proven to sit above bleed and below real speech. Costs nothing for genuine interrupts: `NO_INTERRUPTION` on the server means Eva finishes her sentence regardless of a mid-turn interrupt, and the code's own prior comment already documented that real interrupts land "once the model turn completes" — i.e. after `EVA_TALKING` goes false, where forwarding is unconditional exactly as before. No Python-testable surface (pure client-side WebAudio logic, and this repo has no JS test harness); verification is: re-pull production call transcripts after deploy and confirm no further calls with 100% model-role turns and zero user turns |

## Round 7 (feature ask: "based on the agent voice model selected, the web demo shud work for both providers")

Follow-up to Round 6's echo-bleed fix. Operator asked what happens on Fish Audio; investigation found the browser voice-test path (`gemini_bridge.run_session`, reached via `/ws/session` with no `mode=` param) had ZERO Fish Audio integration — it always played Gemini's own native voice regardless of a saved agent's `voice_tweaks.voice_provider`, unlike the real Twilio/Plivo call path (`telephony/base.py::_bridge`) where Fish is the platform-wide default. Confirmed no "browser" entry exists in the telephony provider registry (only `twilio`/`plivo`), so this genuinely couldn't be reached any other way. Operator then explicitly asked for this to be closed.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U34 | The browser voice-test path plays through the SAME voice engine (Fish vs Gemini) a real phone call for that agent would use | PASS | Unit | 412 | Ported telephony.base's Fish integration into `gemini_bridge.py`: `_pump_gemini_to_client` gains optional `fx`/`fish_q` params (default `None` → fully inactive, so `run_helper_session`'s existing call site is untouched) that mirror `_bridge`'s protocol — accumulate `output_transcription` into sentence-boundary segments, synthesize via Fish, suppress Gemini's own `inline_data` audio for that turn, bump `fx["gen"]` + drain the queue on `sc.interrupted` (barge-in). New `_fish_player_for_ws` is the browser-wire-format counterpart to telephony's `fish_player()` — decodes Fish's WAV, resamples to PCM16@24kHz (matching `audio-engine.js`'s `outCtx` sample rate, so the client needs zero changes — the browser already treats Fish-origin and Gemini-origin bytes identically via `_send_bytes`/`playPcm`), and streams it chunked so a barge-in can abort mid-clip. `run_session` activates it in the non-builder branch using the exact same resolution telephony uses (`voice_tweaks.voice_provider` or "fish" default, gated on `fish_audio.is_configured()`) |
| U35 | Shared sentence-flush logic (`_fish_flush`/`_drain_queue`) has one implementation, not two copies to drift | PASS | Code | 412 | Moved from `telephony/base.py` into `fish_audio.py` (a leaf module neither `gemini_bridge.py` nor `telephony/base.py` needs to import from each other to reach — avoids a circular import, since `telephony.base` already imports FROM `gemini_bridge`). Re-exported from `telephony/base.py` under the same names so existing imports/tests (`from backend.telephony.base import _fish_flush, _drain_queue`) needed no changes |
| U36 | A leaked/orphaned Fish player task (WS closed without reaching an explicit stop signal) doesn't run forever | PASS | Unit | 412 | `run_session` has several early-`return` exit paths inside its inner reconnect loop that an explicit stop signal can't reach (same accepted shape as this file's pre-existing `_wrap_up_watchdog`, whose own cleanup comment documents the identical trade-off). `_fish_player_for_ws` self-bounds instead: checks `ws.client_state != CONNECTED` at the top of every loop iteration, and wraps its queue wait in a 30s timeout so it re-checks even with no new segments arriving — worst-case leak is bounded to ~30s, not permanent. An explicit `fish_q.put_nowait(None)` stop signal is still sent at the one hot-path transition that reliably reaches it (builder→agent handoff) for the fast path |
| — | Test-harness note (not a product bug) | — | — | — | Building the test coverage for U34 surfaced a Python 3.9-only asyncio quirk: constructing `asyncio.Queue()`/`asyncio.Event()` in a test's synchronous setup code (outside the `async def go(): ...; asyncio.run(go())` coroutine) can bind to a stale loop left behind by an earlier test in the same process, raising "Future attached to a different loop" — only when the queue is shared across two independently-scheduled tasks. Fixed by constructing them inside `go()`, matching how `run_session` already does it in production (already running inside FastAPI's one long-lived loop, so it was never actually at risk) |

## Round 8 (critical production incident — build 412's Fish browser feature regressed live calls)

Operator, testing agent 5 ("Mira", Dipesh workspace) shortly after build 412 shipped: "It kept talking on its own, didnt let me talk, this time it was 2 way AI talking, and every time the voice changed. This is not right." Root-caused against the same production call-log method as Round 6 — call 335 (agent 5, timestamped right at the report) showed the identical 100%-`role:"model"`/zero-`role:"user"` shape as the Round 6 incident, i.e. the self-talk symptom was back, now compounded by "voice changed every time."

Root cause: agent 5's `voice_tweaks` has no `voice_provider`/`fish_voice_id` keys at all (never touched by the operator) — `voice_provider` resolves to the platform default, `"fish"`, and build 412 activated Fish for the browser test path under that exact condition, same as telephony already did. With `fish_voice_id` unset, `fish_audio.synthesize()` omits `reference_id` entirely — Fish's free backbone does not reliably pick the same voice per request without one, AND any single intermittent synth failure flips the existing (correct, intentional) degrade-to-Gemini safety net mid-call. Both together: the caller hears the voice change mid-conversation, and the transition (Fish audio still queued/playing while Gemini's own audio starts) reads as two AIs talking over each other. This was a **latent bug in the existing telephony path too** (agents 6/8 have the identical unset shape) — build 412 didn't introduce the underlying flaw, it just gave the browser path a second way to reach it, and this was the first time it hit a real conversation instead of only fake-WS unit tests.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U37 | Fish Audio never activates on a live call (browser OR phone) without an explicitly chosen voice | PASS | Unit | 416 | New `fish_audio.resolve_voice_engine(voice_tweaks) -> (active, voice_id)` is now the ONE place both `telephony/base.py::_bridge` and `gemini_bridge.py::run_session` resolve the voice engine — requires `fish_voice_id` truthy (in addition to the existing `voice_provider == "fish"` and `is_configured()` checks) before `active` is `True`. Unifying into one function closes the drift risk that let telephony and the browser path silently share the same gap. Falls back to Gemini's own already-reliable voice whenever no voice was picked — the exact safe default the incident showed was missing. 6 new direct unit tests (`TestResolveVoiceEngine`) cover the agent-5 shape specifically (`voice_tweaks` present but with neither key touched), plus explicit-gemini, not-configured, and None-tweaks edge cases. `agent_config_warnings`'s `fish_voice_not_selected` message updated to describe the new (safe) behavior — "calls currently use Gemini's own voice" instead of the old "Fish's generic default voice", since that's no longer what happens |
| — | Should Fish have been disabled platform-wide instead of gated per-agent? | Considered, rejected | — | — | A blanket "disable Fish entirely until every agent has a voice picked" would have silently regressed any agent an operator DID deliberately configure with Fish + a real voice_id (none exist in production today, but the mechanism should stay correct for when one does). Gating on the presence of an explicit `fish_voice_id` — the one operator action that was actually missing — fixes the reported bug without disabling a feature that's correct once configured |

## Round 9 (Round 8's fix didn't land — a second live silent-test still failed)

Operator, deliberately staying silent to verify Round 8's fix: pasted a live Call Details transcript (agent "Mira", 13 Aug 2026 13:11, "Web / test call", 9 turns, all `Mira:`, zero caller turns) — the exact same self-talk shape, reproduced AFTER build 416 was live. Since build 416 disabled Fish for Mira (no fish_voice_id set), this call should have been Gemini-only and should have benefited from build 410's mic-echo-gate fix — but didn't.

Root cause: `frontend/app.js` imports `audio-engine.js` via a STATIC ES `import` with its own manually-maintained cache-bust query param — `/static/audio-engine.js?v=23` — entirely separate from, and not covered by, the `SXAI_BUILD`/`APP_BUILD` lockstep (a static `import` specifier must be a string literal, so it structurally can't reference the `SXAI_BUILD` constant). Build 410 edited `audio-engine.js` (the mic-echo-gate fix) and correctly bumped `SXAI_BUILD` — but never touched this second, independent version counter. `StaticFiles` doesn't set aggressive cache headers, but browsers still don't re-fetch an unchanged URL within a session/cache lifetime — so any browser (including the operator's, testing live) that already had `audio-engine.js?v=23` cached from before build 410 kept running the PRE-FIX file indefinitely. Build 410's fix may never have reached a real browser until now.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U38 | audio-engine.js's cache-bust actually changes when the file changes | PASS | Code | 417 | Bumped `/static/audio-engine.js?v=23` → `?v=24` in `frontend/app.js`'s import line — the actual, minimal fix. This is the second time in this incident chain a fix "shipped" without reaching a real browser; unlike builds 409-416 this one has NOTHING to do with backend logic, so no amount of backend testing (unit tests, live DB queries, even a correct root-cause diagnosis) would have caught it |
| U39 | This class of bug (edit a statically-imported file, forget its independent cache-bust) can't silently recur | PASS | Unit | 417 | New `TestStaticImportCacheBust` hashes `audio-engine.js`'s content against a checked-in snapshot — if the file changes, the hash mismatches, forcing whoever edits it to explicitly bump `?v=N` in `app.js` AND update the test's expected hash. A forcing function, not a comment someone can miss. Scoped to `audio-engine.js` only (the file actually involved); `voice-blob.js?v=34` has the identical structural risk but wasn't implicated in this incident and wasn't touched — flagged here, not fixed, to avoid scope creep on an urgent live fix |
| — | Should the whole manual-per-file-version scheme be replaced? | Deferred, flagged | — | — | The robust fix is routing static JS imports through something SXAI_BUILD-aware (e.g. dynamic `import()` with a template literal, or server-side templating of `app.js` the way `index.html` already gets `{BUILD}` substituted) — a real refactor, not an urgent hotfix. Recorder-worklet.js (loaded via `audioWorklet.addModule()`) has NO cache-bust at all, an even sharper version of the same gap. Worth a dedicated follow-up, not bundled into this incident's fix |

## Round 10 (the real root cause — production Railway logs, not guessing)

Operator called Mira again after build 417 and got the same result. Rather than theorize further, pulled Railway application logs (`railway logs --since/--until`) bracketing the exact call timestamp from the DB row (call 337). This is what finally showed the actual mechanism, and it has nothing to do with echo, Fish, or browser caching — all three prior fixes (410, 416, 417) were real, correctly-targeted fixes for real problems they found, just not THIS one.

Log evidence, a single 25.75s silent test call: `Live session opened` → `mic stats: chunks=16 last3s_peak=203` → `gemini stream ended cleanly after turns=1 ... reason='gemini stream ended (no exception)'` → `reconnecting to Gemini (attempt 1, with handle)` → `RECONNECTED` → `reconnect: resume-handle steer (no-ack, answer-pending)` → repeat two more times (`chunks=25 peak=1276`, `chunks=30 peak=2785`) — **3 reconnects in 25 seconds**, all clean (no exception, no `go_away`), all with mic peaks far below the 12000 echo/barge-in threshold (ruling out echo bleed as the mechanism this time — genuinely quiet mic input). 8 turns landed in the transcript, matching roughly 2 turns per connection episode.

Root cause: `session.receive()`'s generator ending after ~1 turn cycle is apparently normal, not a real drop — but the reconnect handler's `resume_handle` branch unconditionally sent a `turn_complete=True` kickoff instructing: "...if the caller's most recent question is still unanswered, answer it now, directly. Otherwise stay silent...". Mira's last utterance before each reconnect was typically HER OWN half-finished question ("what date works for you?") — read by the model as "unanswered", which it then answered itself, live, with the caller having said nothing. A caller who stays genuinely silent (exactly what every test so far has deliberately done) hits this on every single cycle, producing a self-sustaining conversation with no real input driving it at all — the precise symptom reported four times running.

| Item | Acceptance criterion | Verdict | Tier | Build | Notes |
|---|---|---|---|---|---|
| U40 | Resuming a Gemini Live session never gives the model discretion to answer its own dangling question | PASS | Code | 418 | Removed the "if unanswered, answer it now, directly" escape hatch from the `resume_handle` reconnect-kickoff branch in `run_session` (`backend/gemini_bridge.py`). Replacement is unconditional: "do NOT continue or answer your own last question. Stay COMPLETELY SILENT right now — produce no audio at all. Wait for the caller to actually speak." A genuinely unanswered caller question still gets answered — once the caller actually speaks again and triggers a real VAD-detected turn, untouched by this change. Can't unit-test live Gemini's actual instruction-following, so `TestReconnectSteerNeverInvitesSelfAnswer` guards the source text directly (same style as `TestBuildLockstep`): the old loophole phrase must be absent from the kickoff string specifically (isolated from the explanatory comment, which deliberately quotes it), and the mandatory-silence replacement must be present |
| — | Why did builds 410/416/417 not catch this? | — | — | — | Each fixed something real that DID need fixing (build 410: genuine mic-echo bleed risk once loud enough; 416: Fish's free-backbone voice inconsistency without a chosen voice_id; 417: a real stale-cache gap for audio-engine.js) — none of them were wrong, they just weren't the mechanism actually firing in the reported calls. This round only found the real cause by reading production Railway logs bracketing the exact call timestamp instead of continuing to reason from the transcript alone, which looks identical (100% agent-role turns) regardless of WHICH of these four mechanisms produces it |
| — | The other two reconnect branches (`memory.turns` replay, no-handle/no-memory) | Not touched, flagged | — | — | Both already lean toward "stay silent" but the `memory.turns` branch still has a "continue the conversation forward" discretion clause with the same shape as the bug just fixed. Every reconnect in the two reported incidents had `handle=yes`, so this branch was never actually exercised — left alone to keep this fix narrowly scoped to what the evidence supports, not touched blind. Worth the same tightening as a fast follow-up if a `resume_handle`-absent reconnect ever reproduces a similar symptom |

## Out-of-band tooling (not a tester item, no build number)
- **`backend/sip/` (`sipd`)** — native SIP UAS that accepts inbound INVITEs straight from a Grandstream UCM and bridges call audio to a Gemini agent (no Twilio/Plivo). Run as a **separate LAN process** (`python -m backend.sip`), NOT part of the Railway web app — `backend/app.py` does not import it, so it's inert for the deploy. Committed to the repo so it can be pulled onto a LAN box. Transport (SIP/RTP/G.711/digest) is unit- + loopback-proven; the live Gemini audio bridge (`gemini_handler.py`) is pending a first live call. Reuses `gemini_bridge._agent_system_prompt`/`_live_config`/connectors + `db.get_agent`; no new deps.
