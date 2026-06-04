"""Per-agent call-outcome taxonomy + report assembly.

Two surfaces in the dashboard:
  • Call logs        — WHAT happened on the call (transcript, duration, etc.)
  • Call outcomes    — WHAT was the RESULT (booking, lead, info, dropped),
                       aggregated as a performance report.

The taxonomy is a matrix of (industry × locale × the operator's own wizard
answers). A restaurant agent's outcomes are different from a dental clinic's,
and within "restaurant" the available outcomes adapt to THIS restaurant's
config — if delivery is disabled we don't list `delivery_arranged`, etc.

Outcomes are bucketed by KIND so the report can show "success rate" meaningfully:
  • success    — primary KPI (booking made, order taken, emergency routed)
  • qualified  — useful but not a primary success (lead captured, callback)
  • info       — informational (price/hours given, FAQ answered)
  • failure    — unwanted (voicemail, abandoned, complaint not resolved)

Each outcome carries a `success_weight` (0–1) used to roll up an overall
weighted success rate. The catalogue is also unioned into `agent.outcomes` at
build time so the agent's end_call tool has a complete vocabulary.
"""
from __future__ import annotations

from typing import Any


# ─── Outcome kinds + their weights (for the success-rate report) ────────────


_KIND_WEIGHT = {
    "success": 1.0,
    "qualified": 0.5,
    "info": 0.2,
    "failure": 0.0,
}
# Default weights ARE exposed on the catalogue payload + report so the
# weights editor on the dashboard can show "default 1.0 / your 0.7" side by
# side without re-encoding the numbers in two places.
DEFAULT_KIND_WEIGHTS: dict[str, float] = dict(_KIND_WEIGHT)


def _resolve_kind_weights(agent: dict[str, Any]) -> dict[str, float]:
    """Per-agent override (agents.outcome_weights JSONB) wins over the
    catalogue defaults. Any missing kind falls back to the default — so an
    operator can override JUST `qualified` without re-typing the others."""
    raw = agent.get("outcome_weights")
    if not isinstance(raw, dict):
        return dict(DEFAULT_KIND_WEIGHTS)
    out: dict[str, float] = dict(DEFAULT_KIND_WEIGHTS)
    for k in ("success", "qualified", "info", "failure"):
        v = raw.get(k)
        try:
            if v is not None:
                # Clamp to [0, 1] — sliders elsewhere already enforce this,
                # but defense-in-depth in case a hand-PATCH sneaks past.
                out[k] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _o(oid: str, label: str, kind: str, desc: str) -> dict[str, Any]:
    return {
        "id": oid, "label": label, "kind": kind,
        "description": desc, "success_weight": _KIND_WEIGHT.get(kind, 0.0),
    }


# ─── Per-sector base catalogues ─────────────────────────────────────────────


def _generic_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("resolved",              "Resolved",              "success",   "Caller's reason for calling was handled."),
        _o("info_only",             "Info only",             "info",      "Caller got the information they wanted, no action."),
        _o("callback_requested",    "Callback requested",    "qualified", "Caller asked someone to call them back."),
        _o("transferred_human",     "Transferred to human",  "info",      "Agent transferred the call to a person."),
        _o("not_interested",        "Not interested",        "failure",   "Caller decided not to proceed."),
        _o("voicemail",             "Voicemail",             "failure",   "Caller didn't engage / left voicemail."),
        _o("abandoned",             "Abandoned",             "failure",   "Caller disconnected before the agent could finish wrapping up."),
        _o("complaint_logged",      "Complaint logged",      "failure",   "Caller registered a complaint — needs follow-up."),
    ]


def _restaurant_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        _o("reservation_made",       "Reservation made",          "success",   "A table reservation was created."),
        _o("reservation_cancelled",  "Reservation cancelled",     "info",      "Existing reservation cancelled per caller's request."),
        _o("reservation_modified",   "Reservation modified",      "info",      "Caller changed time / party size / area."),
        _o("menu_info_given",        "Menu info given",           "info",      "Walked through menu / dish details."),
        _o("price_info_sms_sent",    "Price info SMS sent",       "info",      "Detailed prices sent via SMS rather than read aloud."),
        _o("birthday_acknowledged",  "Birthday acknowledged",     "info",      "Caller's birthday flagged for the team."),
        _o("walk_in_directed",       "Walk-in directed",          "info",      "Caller advised to walk in; expectations set."),
        _o("large_group_transferred","Large-group transferred",   "info",      "10+ guests → handed to the team."),
        _o("callback_requested",     "Callback requested",        "qualified", "Caller asked the team to call back."),
        _o("complaint_logged",       "Complaint logged",          "failure",   "Caller registered a complaint."),
        _o("voicemail",              "Voicemail",                 "failure",   "Caller left without engaging."),
    ]
    # Operator-config conditional outcomes.
    delivery = str(v.get("delivery", "")).lower()
    takeaway = str(v.get("takeaway", "")).lower()
    if delivery in ("true", "yes", "1"):
        out.insert(3, _o("delivery_arranged", "Delivery arranged", "success", "Delivery order details captured."))
    if takeaway not in ("dine_in_only", "no", "false"):
        out.insert(3, _o("takeaway_ordered",  "Takeaway ordered",  "success", "Takeaway / pickup order taken."))
    return out


def _dental_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("appointment_booked",       "Appointment booked",       "success",   "New appointment created."),
        _o("appointment_rescheduled",  "Appointment rescheduled",  "info",      "Existing appointment moved."),
        _o("appointment_cancelled",    "Appointment cancelled",    "info",      "Caller cancelled their booking."),
        _o("emergency_routed",         "Emergency routed",         "success",   "Urgent dental case routed to on-call / triage."),
        _o("treatment_info_given",     "Treatment info given",     "info",      "Walked through a procedure or treatment."),
        _o("price_info_callback",      "Price info via callback",  "qualified", "Exact pricing handed to counsellor for callback."),
        _o("insurance_info_given",     "Insurance info given",     "info",      "Insurance / payment info provided."),
        _o("callback_requested",       "Callback requested",       "qualified", "Caller asked someone to call them back."),
        _o("not_interested",           "Not interested",           "failure",   "Caller decided not to proceed."),
        _o("voicemail",                "Voicemail",                "failure",   "Caller didn't engage."),
    ]


def _automotive_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        _o("test_drive_booked",   "Test drive booked",      "success",   "A test drive was scheduled."),
        _o("quote_requested",     "Quote requested",        "qualified", "Caller asked for a tailored quote (sales callback)."),
        _o("lead_captured",       "Lead captured",          "qualified", "New sales lead recorded."),
        _o("model_info_given",    "Model / inventory info", "info",      "Walked through models, variants, colours."),
        _o("finance_info_routed", "Finance info routed",    "info",      "Finance / EMI specifics handed to the team."),
        _o("callback_requested",  "Callback requested",     "qualified", "Caller asked the team to call them back."),
        _o("not_interested",      "Not interested",         "failure",   "Caller decided not to proceed."),
        _o("voicemail",           "Voicemail",              "failure",   "Caller didn't engage."),
    ]
    if str(v.get("has_service_centre", "")).lower() in ("true", "yes", "1"):
        out.insert(1, _o("service_booked", "Service booked", "success", "Service / maintenance appointment created."))
    return out


def _salon_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("appointment_booked",  "Appointment booked",  "success",   "Salon appointment created."),
        _o("stylist_swap_booked", "Stylist swap booked", "success",   "Alternate stylist accepted, slot booked."),
        _o("service_info_given",  "Service info given",  "info",      "Walked through services."),
        _o("price_info_given",    "Price info given",    "info",      "Indicative pricing shared."),
        _o("walk_in_directed",    "Walk-in directed",    "info",      "Caller advised to walk in."),
        _o("callback_requested",  "Callback requested",  "qualified", "Caller asked the team to call them back."),
        _o("not_interested",      "Not interested",      "failure",   "Caller decided not to proceed."),
        _o("voicemail",           "Voicemail",           "failure",   "Caller didn't engage."),
    ]


def _real_estate_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        _o("site_visit_booked",      "Site visit booked",     "success",   "Site visit slot scheduled."),
        _o("lead_captured",          "Lead captured",         "qualified", "Caller's requirements + contact recorded."),
        _o("project_info_given",     "Project info given",    "info",      "Walked through projects / locations / BHK options."),
        _o("price_info_routed",      "Price info routed",     "info",      "Final pricing handed to sales team."),
        _o("loan_info_routed",       "Loan info routed",      "info",      "Loan / EMI specifics handed to the team."),
        _o("callback_requested",     "Callback requested",    "qualified", "Caller asked the team to call back."),
        _o("not_interested",         "Not interested",        "failure",   "Caller decided not to proceed."),
        _o("voicemail",              "Voicemail",             "failure",   "Caller didn't engage."),
    ]
    if locale.endswith("-IN"):
        out.append(_o("rera_disclosure_sent", "RERA disclosure sent", "info", "RERA registration / project details handed to the team."))
    return out


def _healthcare_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("appointment_booked",  "Appointment booked",  "success",   "New patient appointment created."),
        _o("emergency_routed",    "Emergency routed",    "success",   "Caller directed to emergency services / on-call."),
        _o("test_booked",         "Test / scan booked",  "success",   "Diagnostic / lab test scheduled."),
        _o("report_route_given",  "Report routing given","info",      "Caller routed for reports / records."),
        _o("triage_info_given",   "Triage info given",   "info",      "General info given without a booking."),
        _o("callback_requested",  "Callback requested",  "qualified", "Caller asked the team to call back."),
        _o("voicemail",           "Voicemail",           "failure",   "Caller didn't engage."),
    ]


def _retail_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("order_status_given",  "Order status given",  "success",   "Order status looked up + summarised."),
        _o("return_initiated",    "Return initiated",    "success",   "Return / exchange ticket raised."),
        _o("availability_given",  "Availability info",   "info",      "Stock / pricing info provided."),
        _o("callback_requested",  "Callback requested",  "qualified", "Caller asked the team to call back."),
        _o("complaint_logged",    "Complaint logged",    "failure",   "Caller registered a complaint."),
        _o("not_interested",      "Not interested",      "failure",   "Caller decided not to proceed."),
        _o("voicemail",           "Voicemail",           "failure",   "Caller didn't engage."),
    ]


def _education_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("counselling_booked",  "Counselling booked",  "success",   "Counselling slot scheduled."),
        _o("demo_booked",         "Demo class booked",   "success",   "Free demo class scheduled."),
        _o("lead_captured",       "Lead captured",       "qualified", "Caller's interest + contact recorded."),
        _o("fee_info_routed",     "Fee info routed",     "info",      "Exact fees handed to counsellor."),
        _o("course_info_given",   "Course info given",   "info",      "Walked through course / batch options."),
        _o("callback_requested",  "Callback requested",  "qualified", "Caller asked the team to call back."),
        _o("not_interested",      "Not interested",      "failure",   "Caller decided not to proceed."),
        _o("voicemail",           "Voicemail",           "failure",   "Caller didn't engage."),
    ]


def _travel_catalogue(locale: str, v: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _o("booking_started",     "Booking started",     "success",   "Tentative booking / hold placed."),
        _o("quote_requested",     "Quote requested",     "qualified", "Tailored quote queued for callback."),
        _o("lead_captured",       "Lead captured",       "qualified", "Caller's interest + dates + contact recorded."),
        _o("itinerary_info_given","Itinerary info given","info",      "Walked through package options."),
        _o("visa_info_routed",    "Visa info routed",    "info",      "Visa specifics handed to the team."),
        _o("callback_requested",  "Callback requested",  "qualified", "Caller asked the team to call back."),
        _o("not_interested",      "Not interested",      "failure",   "Caller decided not to proceed."),
        _o("voicemail",           "Voicemail",           "failure",   "Caller didn't engage."),
    ]


_CATALOGUES = {
    "restaurant":  _restaurant_catalogue,
    "dental":      _dental_catalogue,
    "automotive":  _automotive_catalogue,
    "salon":       _salon_catalogue,
    "real_estate": _real_estate_catalogue,
    "healthcare":  _healthcare_catalogue,
    "retail":      _retail_catalogue,
    "education":   _education_catalogue,
    "travel":      _travel_catalogue,
}


# ─── Core purpose ↔ outcomes mapping (sector-aware) ──────────────────────
#
# Each action the operator picks on the Core-purpose page maps to a SET of
# outcome ids that count as "fulfilling" that purpose for THIS sector. The
# Call outcomes page uses this to derive a `purpose_conversion_rate` and to
# tag primary outcomes — turning the page into "is this agent doing what
# I built it for?", not just "did the call land somewhere".

_ACTION_OUTCOMES: dict[str, dict[str, list[str]]] = {
    # Universal: every sector logs callback the same way.
    "callback_request": {
        "_default": ["callback_requested"],
    },
    # Sector-specific bookings.
    "appointment_booking": {
        "restaurant":   ["reservation_made"],
        "dental":       ["appointment_booked"],
        "automotive":   ["test_drive_booked", "service_booked"],
        "salon":        ["appointment_booked", "stylist_swap_booked"],
        "real_estate":  ["site_visit_booked"],
        "healthcare":   ["appointment_booked", "test_booked"],
        "education":    ["counselling_booked", "demo_booked"],
        "travel":       ["booking_started"],
        "_default":     ["resolved"],
    },
    "quote_request": {
        "automotive":   ["quote_requested"],
        "real_estate":  ["quote_requested"],
        "travel":       ["quote_requested"],
        "_default":     ["callback_requested"],
    },
    "inquiry_capture": {
        "automotive":   ["lead_captured"],
        "real_estate":  ["lead_captured"],
        "education":    ["lead_captured"],
        "travel":       ["lead_captured"],
        "_default":     ["info_only", "resolved"],
    },
    "complaint_intake": {
        "_default":     ["complaint_logged"],
    },
    "order_status": {
        "retail":       ["order_status_given"],
        "restaurant":   ["reservation_modified"],
        "_default":     ["info_only"],
    },
    "support_ticket": {
        "retail":       ["return_initiated", "complaint_logged"],
        "_default":     ["complaint_logged"],
    },
    "emergency_routing": {
        "healthcare":   ["emergency_routed"],
        "dental":       ["emergency_routed"],
        "_default":     ["transferred_human"],
    },
}


def purpose_aligned_outcome_ids(agent: dict[str, Any]) -> list[str]:
    """The set of outcome ids that fulfil THIS agent's stated purpose
    actions, given its sector. Returns an ORDERED list (preserves the
    operator's action selection order) with duplicates removed."""
    purpose = agent.get("purpose") if isinstance(agent.get("purpose"), dict) else {}
    actions = purpose.get("actions") if isinstance(purpose.get("actions"), list) else []
    sector = (agent.get("sector") or "generic").strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for a in actions:
        if not isinstance(a, str):
            continue
        per_sector = _ACTION_OUTCOMES.get(a) or {}
        ids = per_sector.get(sector) or per_sector.get("_default") or []
        for oid in ids:
            if oid in seen:
                continue
            seen.add(oid)
            out.append(oid)
    return out


# ─── Public API ────────────────────────────────────────────────────────────


def catalogue_for(agent: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the outcome catalogue for THIS agent: industry × locale ×
    saved variables × operator overrides.

    Build 213 — `agent.outcome_overrides` lets the business user edit
    labels / kinds, hide irrelevant outcomes, and add custom ones.
    Resolution order:
      1. Start with the sector-resolved catalogue (industry × locale ×
         variables — the original auto-detected set).
      2. Drop any id listed in `outcome_overrides.removed`.
      3. Overlay per-field edits from `outcome_overrides.edited`
         (label / kind / description; `id` and `success_weight` stay
         catalogue-managed so the rollup math doesn't drift).
      4. Append `outcome_overrides.added` — each gets `is_custom: true`
         so the UI can render an "operator-added" affordance.
    """
    sector = (agent.get("sector") or "generic").strip().lower()
    locale = (agent.get("locale") or "en-IN")
    variables = agent.get("variables") if isinstance(agent.get("variables"), dict) else {}
    fn = _CATALOGUES.get(sector, _generic_catalogue)
    items = fn(locale, variables or {})
    overrides = _normalize_overrides(agent.get("outcome_overrides"))
    removed = overrides["removed"]
    edited = overrides["edited"]
    # Build 209 — custom-kind weight resolver. Built-ins use the static
    # `_KIND_WEIGHT` map; customs alias one of those, so we resolve via
    # the alias_kind. Closes over `overrides` so the catalogue resolver
    # doesn't have to refetch.
    custom_kind_alias = {ck["id"]: ck["alias_kind"] for ck in overrides["custom_kinds"]}
    def _weight_for_kind(k: str) -> float:
        if k in _KIND_WEIGHT:
            return _KIND_WEIGHT[k]
        return _KIND_WEIGHT.get(custom_kind_alias.get(k, ""), 0.0)
    # Dedup by id (keep first occurrence) — preserves order. Also drops
    # `removed` ids in the same pass to keep the catalogue tight.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        if it["id"] in seen or it["id"] in removed:
            continue
        seen.add(it["id"])
        # Apply per-field overlay if the operator edited this row.
        ed = edited.get(it["id"])
        if ed:
            merged = dict(it)
            for k in ("label", "kind", "description"):
                if k in ed and ed[k] is not None:
                    merged[k] = ed[k]
            # Re-stamp the success_weight from the (possibly edited) kind so
            # a "Lead captured" reclassified to `success` rolls into the
            # right bucket on the report. Build 209 — custom kinds also
            # resolve here via their alias_kind.
            merged["success_weight"] = _weight_for_kind(merged["kind"])
            merged["is_edited"] = True
            it = merged
        out.append(it)
    # Append operator-added customs — `is_custom: true` so the UI can
    # render the "added by you" badge + a different delete affordance
    # (custom rows truly delete; built-in rows are hidden via `removed`).
    for added in overrides["added"]:
        if added["id"] in seen:
            continue
        seen.add(added["id"])
        out.append({
            "id":             added["id"],
            "label":          added["label"],
            "kind":           added["kind"],
            "description":    added.get("description") or "",
            "success_weight": _weight_for_kind(added["kind"]),
            "is_custom":      True,
        })
    return out


_BUILTIN_KINDS = {"success", "qualified", "info", "failure"}


def _normalize_overrides(raw: Any) -> dict[str, Any]:
    """Clean + type-coerce an `outcome_overrides` blob to a known shape.

    Defensive — operators can't directly write this blob but a future
    bulk-import / API path might. Bad fields are dropped silently so a
    malformed override row never crashes the catalogue resolution path.

    Build 209 — additionally accepts `custom_kinds` (a list of per-agent
    kind definitions). Each custom kind has:
      id          slug, max 40 chars (the value stored on outcomes)
      label       what the operator + the dashboard show, max 60 chars
      alias_kind  one of the 4 built-ins — defines the downstream
                  bucket (Wins counter, success_weight, dashboard
                  grouping). The OPERATOR sees their custom label
                  everywhere; the BACKEND uses the alias for math.
      emoji       optional UI prefix, max 8 chars
    """
    out = {"edited": {}, "added": [], "removed": set(), "custom_kinds": []}
    if not isinstance(raw, dict):
        return out

    # ─── custom_kinds (build 209) ────────────────────────────────────────
    # Resolve these FIRST so the set of allowed kinds includes them
    # when we validate edited / added rows below.
    ck = raw.get("custom_kinds")
    if isinstance(ck, list):
        seen_ids: set[str] = set()
        for row in ck:
            if not isinstance(row, dict):
                continue
            cid = row.get("id")
            label = row.get("label")
            alias = row.get("alias_kind")
            if not (isinstance(cid, str) and cid.strip()):
                continue
            if not (isinstance(label, str) and label.strip()):
                continue
            if alias not in _BUILTIN_KINDS:
                continue
            slug = cid.strip().lower().replace(" ", "_")[:40]
            if not slug or slug in seen_ids or slug in _BUILTIN_KINDS:
                # Don't let a custom kind shadow a built-in — that
                # would silently change the meaning of "success" etc.
                continue
            seen_ids.add(slug)
            out["custom_kinds"].append({
                "id":         slug,
                "label":      label.strip()[:60],
                "alias_kind": alias,
                "emoji":      (row.get("emoji") or "").strip()[:8] if isinstance(row.get("emoji"), str) else "",
            })

    allowed_kinds = _BUILTIN_KINDS | {k["id"] for k in out["custom_kinds"]}

    ed = raw.get("edited")
    if isinstance(ed, dict):
        for oid, fields in ed.items():
            if not isinstance(oid, str) or not isinstance(fields, dict):
                continue
            clean: dict[str, Any] = {}
            label = fields.get("label")
            if isinstance(label, str) and label.strip():
                clean["label"] = label.strip()[:80]
            kind = fields.get("kind")
            if isinstance(kind, str) and kind in allowed_kinds:
                clean["kind"] = kind
            desc = fields.get("description")
            if isinstance(desc, str):
                clean["description"] = desc.strip()[:280]
            if clean:
                out["edited"][oid] = clean
    added = raw.get("added")
    if isinstance(added, list):
        for row in added:
            if not isinstance(row, dict):
                continue
            oid = row.get("id")
            label = row.get("label")
            kind = row.get("kind")
            if not (isinstance(oid, str) and oid.strip()): continue
            if not (isinstance(label, str) and label.strip()): continue
            if kind not in allowed_kinds: continue
            out["added"].append({
                "id":          oid.strip().lower().replace(" ", "_")[:60],
                "label":       label.strip()[:80],
                "kind":        kind,
                "description": (row.get("description") or "").strip()[:280] if isinstance(row.get("description"), str) else "",
            })
    rem = raw.get("removed")
    if isinstance(rem, list):
        out["removed"] = {x for x in rem if isinstance(x, str)}
    return out


# Back-compat alias — older internal call sites referenced _ALLOWED_KINDS
# as a constant. Today it's the *built-in* set; per-agent custom kinds
# are added on top during `_normalize_overrides`. Kept so anything
# importing this name continues to resolve.
_ALLOWED_KINDS = _BUILTIN_KINDS


def kind_weight_for(agent: dict[str, Any], kind: str) -> float:
    """Build 209 — resolve the success_weight for a kind on an agent.

    Built-in kinds pull from `_KIND_WEIGHT` directly. Custom kinds
    fall through to their declared `alias_kind` — so "Demo booked"
    aliased to `success` carries the success weight 1.0 in every
    downstream rollup without the dashboard having to know about
    every custom kind a customer invented."""
    if kind in _KIND_WEIGHT:
        return _KIND_WEIGHT[kind]
    overrides = _normalize_overrides(agent.get("outcome_overrides"))
    for ck in overrides["custom_kinds"]:
        if ck["id"] == kind:
            return _KIND_WEIGHT.get(ck["alias_kind"], 0.0)
    return 0.0


def merge_with_agent_outcomes(agent: dict[str, Any]) -> list[str]:
    """Union the catalogue's outcome ids with the agent's saved vocabulary.
    The agent's existing `outcomes` list wins for ordering / extras the
    operator hand-added; catalogue ids fill the rest."""
    existing = list(agent.get("outcomes") or [])
    have = {o for o in existing if isinstance(o, str)}
    for c in catalogue_for(agent):
        if c["id"] not in have:
            existing.append(c["id"])
            have.add(c["id"])
    return existing


def assemble_report(agent: dict[str, Any], analytics: dict[str, Any]) -> dict[str, Any]:
    """Join the agent's catalogue with the rollup data from db.agent_analytics
    to produce a single performance-report payload the dashboard renders.

    Adds:
      • per-outcome counts (from `analytics.by_outcome`), enriched with the
        catalogue label / description / kind / weight,
      • per-KIND totals (success / qualified / info / failure),
      • overall weighted success rate (0–100),
      • orphan outcomes (something the agent logged that isn't in the
        catalogue — surfaced so the operator notices)."""
    # Resolve effective weights (operator override > catalogue defaults).
    weights = _resolve_kind_weights(agent)
    cat = catalogue_for(agent)
    # Re-stamp each catalogue row with the EFFECTIVE weight so the page
    # shows the operator's customised numbers — not the static defaults.
    cat = [{**c, "success_weight": weights.get(c["kind"], 0.0)} for c in cat]
    by_outcome_raw = analytics.get("by_outcome") or []
    counts: dict[str, int] = {row.get("outcome") or "unknown": int(row.get("count") or 0)
                              for row in by_outcome_raw}
    total = sum(counts.values())

    # Build 209 — custom kinds alias one of the 4 built-ins for rollup
    # math. Build a kind → bucket map so an outcome tagged with a
    # custom kind "demo_booked" (aliased to success) still rolls into
    # the success row in by_kind. Lookup falls through to the kind
    # itself if unknown — keeps legacy / orphan rows visible.
    overrides_for_agent = _normalize_overrides(agent.get("outcome_overrides"))
    custom_kind_to_bucket = {ck["id"]: ck["alias_kind"] for ck in overrides_for_agent["custom_kinds"]}
    def _bucket_for(kind: str) -> str:
        if kind in _BUILTIN_KINDS:
            return kind
        return custom_kind_to_bucket.get(kind, kind)

    rows: list[dict[str, Any]] = []
    weighted = 0.0
    by_kind: dict[str, int] = {"success": 0, "qualified": 0, "info": 0, "failure": 0}
    seen: set[str] = set()
    for c in cat:
        n = counts.get(c["id"], 0)
        share = (n / total * 100.0) if total else 0.0
        weighted += n * float(c.get("success_weight", 0.0))
        # Bucket into the 4 default columns via alias resolution so the
        # dashboard's Wins/Qualified/Info/Failure counters stay accurate
        # even when the operator has added custom kinds.
        bucket = _bucket_for(c["kind"])
        by_kind[bucket] = by_kind.get(bucket, 0) + n
        rows.append({**c, "count": n, "share": round(share, 1)})
        seen.add(c["id"])

    # Anything the agent logged that ISN'T in the catalogue — keep it visible.
    orphan: list[dict[str, Any]] = []
    for k, n in counts.items():
        if k in seen:
            continue
        orphan.append({
            "id": k, "label": k.replace("_", " ").title(),
            "kind": "info", "count": n,
            "share": round((n / total * 100.0) if total else 0.0, 1),
            "description": "Logged by the agent but not in the sector catalogue.",
        })

    # ── Core-purpose link: which outcomes count as "the job done" ─────────
    primary_ids = purpose_aligned_outcome_ids(agent)
    primary_set = set(primary_ids)
    primary_rows: list[dict[str, Any]] = []
    primary_count = 0
    for row in rows:
        if row["id"] in primary_set:
            row["is_primary"] = True
            primary_rows.append(row)
            primary_count += int(row.get("count", 0))
        else:
            row["is_primary"] = False
    purpose_blob = agent.get("purpose") if isinstance(agent.get("purpose"), dict) else {}
    purpose_conversion = round((primary_count / total * 100.0) if total else 0.0, 1)

    success_rate = round((weighted / total * 100.0) if total else 0.0, 1)
    return {
        "range_days": int(analytics.get("range_days") or 0),
        "totals": dict(analytics.get("totals") or {}),
        "series": list(analytics.get("series") or []),
        "outcomes": rows,
        "by_kind": by_kind,
        "orphan_outcomes": orphan,
        # Build 209 — per-agent custom kinds. The dashboard renders the
        # operator's labels everywhere; downstream rollups use the
        # alias_kind via `by_kind` above. Empty list when none defined.
        "custom_kinds": list(overrides_for_agent["custom_kinds"]),
        "success_rate": success_rate,
        "total_calls": total,
        # Surface the effective weights + defaults so the page can render
        # the editor without a second round-trip.
        "weights": weights,
        "default_weights": dict(DEFAULT_KIND_WEIGHTS),
        "weights_overridden": isinstance(agent.get("outcome_weights"), dict),
        # ── Core-purpose alignment ──
        # The page renders this as a "Is the agent doing what we built it
        # for?" hero KPI + tags each catalogue row with `is_primary` so the
        # operator can see goal-aligned outcomes at a glance.
        "purpose": {
            "summary":   (purpose_blob.get("summary") or "").strip(),
            "actions":   list(purpose_blob.get("actions") or []) if isinstance(purpose_blob.get("actions"), list) else [],
            "primary_outcome_ids": list(primary_ids),
            "primary_outcomes":    list(primary_rows),
            "primary_count":       primary_count,
            "conversion_rate":     purpose_conversion,
            "has_purpose":         bool(primary_ids),
        },
    }
