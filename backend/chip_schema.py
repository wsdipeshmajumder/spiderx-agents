"""Centralised tag-chip schema for the call log + Eva's extraction prompt.

Build 217 shipped chips with a frontend-hardcoded SECTOR_CHIP_SCHEMA.
Build 218 added value-based inference. This module (build 219) makes
the system truly systemic:

  • One source of truth for which `extracted.<field>` keys belong to
    each sector, and how each renders in the dashboard.
  • Eva's runtime end_call prompt INJECTS this schema as a hint so
    the LLM consistently captures the same vocabulary across calls —
    no more drift between `party_size` and `guests`.
  • Per-agent `chip_overrides` (migration 0025) layers on top: edit
    the label / category, hide irrelevant fields, add custom slots.
    Whatever the operator declares ALSO flows into Eva's prompt, so
    a "loyalty_tier" custom slot becomes part of Eva's vocabulary
    AND renders as a chip.

The CATEGORY palette below is the contract between this module and
the frontend's CHIP_CATS object — colour pairs MUST match across so
the chip a user sees on the dashboard matches the semantic category
the backend declared. The frontend pulls both via the
/api/agents/{id}/chip-schema endpoint, so changes here propagate
without code edits on the JS side.
"""
from __future__ import annotations

from typing import Any


# ─── Semantic categories ──────────────────────────────────────────────────


# (bg, fg) pastel pairs. Frontend reads these via the API endpoint and
# applies them inline on each chip — no theme synchronisation needed.
SEMANTIC_CATEGORIES: dict[str, dict[str, str]] = {
    "outcome": {"bg": "#dbeafe", "fg": "#1e3a8a",
                "label": "Outcome",  "hint": "What kind of call (reservation / inquiry / complaint)."},
    "topic":   {"bg": "#d1fae5", "fg": "#065f46",
                "label": "Topic",    "hint": "Sub-topic or service area the caller asked about."},
    "count":   {"bg": "#fce7f3", "fg": "#9f1239",
                "label": "Count",    "hint": "Quantity (e.g. 4 guests, 10 adults, 3 bedrooms)."},
    "date":    {"bg": "#fef3c7", "fg": "#92400e",
                "label": "Date",     "hint": "A calendar date — when something will happen."},
    "time":    {"bg": "#ede9fe", "fg": "#5b21b6",
                "label": "Time",     "hint": "A clock time."},
    "place":   {"bg": "#d9f99d", "fg": "#3f6212",
                "label": "Place",    "hint": "Location, seating, room, branch."},
    "detail":  {"bg": "#cffafe", "fg": "#155e75",
                "label": "Detail",   "hint": "Money, accessories, dietary, ID, free-form notes."},
    "person":  {"bg": "#e0e7ff", "fg": "#3730a3",
                "label": "Person",   "hint": "Who's involved — family, manager, stylist, doctor."},
    "emotion": {"bg": "#fee2e2", "fg": "#991b1b",
                "label": "Emotion",  "hint": "Caller's emotional state — urgency, frustration."},
    "lead":    {"bg": "#fed7aa", "fg": "#9a3412",
                "label": "Lead",     "hint": "Lead temperature (hot / warm)."},
}


ALLOWED_CATEGORIES = set(SEMANTIC_CATEGORIES.keys())


# ─── Per-sector default schema ────────────────────────────────────────────
#
# Each entry: (field_name, category, human_label).
# Field name is what Eva should populate in `extracted.<field>`.
# Category drives the chip's colour.
# Human label is what the operator sees in the override editor.
#
# Add a new sector here → it ships everywhere (chip rendering AND
# Eva's extraction prompt) on the next deploy.


SECTOR_CHIP_SCHEMA: dict[str, list[tuple[str, str, str]]] = {
    "restaurant": [
        ("reservation_type", "topic",  "Reservation type"),    # reservation / cancellation / inquiry
        ("service_type",     "topic",  "Service type"),         # dine-in / takeaway / delivery
        ("party_size",       "count",  "Party size"),
        ("guest_type",       "person", "Guest type"),           # family / group / couple
        ("occasion",         "detail", "Occasion"),             # birthday / anniversary
        ("date",             "date",   "Date"),
        ("time",             "time",   "Time"),
        ("seating_pref",     "place",  "Seating preference"),
        ("dietary",          "detail", "Dietary"),
        ("special_requests", "detail", "Special requests"),
        ("menu_item",        "topic",  "Menu item"),
    ],
    "dental": [
        ("procedure",         "topic",   "Procedure"),
        ("urgency",           "emotion", "Urgency"),
        ("appointment_date",  "date",    "Appointment date"),
        ("appointment_time",  "time",    "Appointment time"),
        ("preferred_dentist", "person",  "Preferred dentist"),
        ("insurance",         "detail",  "Insurance"),
    ],
    "healthcare": [
        ("specialty",        "topic",   "Specialty"),
        ("complaint",        "topic",   "Complaint"),
        ("urgency",          "emotion", "Urgency"),
        ("appointment_date", "date",    "Appointment date"),
        ("appointment_time", "time",    "Appointment time"),
        ("preferred_doctor", "person",  "Preferred doctor"),
        ("insurance",        "detail",  "Insurance"),
    ],
    "automotive": [
        ("vehicle_model",   "topic",  "Vehicle / model"),
        ("brand",           "topic",  "Brand"),
        ("variant",         "topic",  "Variant"),
        ("budget",          "detail", "Budget"),
        ("test_drive_date", "date",   "Test drive date"),
        ("test_drive_time", "time",   "Test drive time"),
        ("trade_in",        "detail", "Trade-in"),
        ("service_type",    "topic",  "Service type"),     # sales / service / parts
    ],
    "salon": [
        ("service",          "topic",  "Service"),
        ("stylist",          "person", "Stylist"),
        ("appointment_date", "date",   "Appointment date"),
        ("appointment_time", "time",   "Appointment time"),
    ],
    "real_estate": [
        ("property_type", "topic",  "Property type"),
        ("bedrooms",      "count",  "Bedrooms"),
        ("budget",        "detail", "Budget"),
        ("location",      "place",  "Location"),
        ("viewing_date",  "date",   "Viewing date"),
        ("viewing_time",  "time",   "Viewing time"),
    ],
    "legal": [
        ("case_type",         "topic",   "Case type"),
        ("urgency",           "emotion", "Urgency"),
        ("consultation_date", "date",    "Consultation date"),
        ("consultation_time", "time",    "Consultation time"),
        ("preferred_lawyer",  "person",  "Preferred lawyer"),
    ],
    "education": [
        ("program",      "topic",  "Program"),
        ("intake",       "topic",  "Intake"),
        ("enquiry_type", "topic",  "Enquiry type"),
        ("session_date", "date",   "Session date"),
        ("session_time", "time",   "Session time"),
        ("fee_bucket",   "detail", "Fee bucket"),
    ],
    "fitness": [
        ("service",      "topic",  "Service"),
        ("trainer",      "person", "Trainer"),
        ("session_date", "date",   "Session date"),
        ("session_time", "time",   "Session time"),
        ("plan",         "detail", "Plan"),
    ],
    "veterinary": [
        ("species",          "topic",   "Species"),
        ("complaint",        "topic",   "Complaint"),
        ("urgency",          "emotion", "Urgency"),
        ("appointment_date", "date",    "Appointment date"),
        ("appointment_time", "time",    "Appointment time"),
        ("preferred_vet",    "person",  "Preferred vet"),
    ],
    "hospitality": [
        ("room_type",        "topic",  "Room type"),
        ("check_in",         "date",   "Check-in"),
        ("check_out",        "date",   "Check-out"),
        ("nights",           "count",  "Nights"),
        ("adults",           "count",  "Adults"),
        ("children",         "count",  "Children"),
        ("special_requests", "detail", "Special requests"),
    ],
    "logistics": [
        ("service_type", "topic",  "Service type"),    # pickup / delivery / tracking
        ("pickup_date",  "date",   "Pickup date"),
        ("pickup_time",  "time",   "Pickup time"),
        ("destination",  "place",  "Destination"),
        ("order_id",     "detail", "Order ID"),
    ],
    # Generic baseline — anything not in a sector list above falls
    # through to this. Keeps date / time / count / location / name
    # visible by default for novel verticals.
    "_generic": [
        ("topic",    "topic",  "Topic"),
        ("category", "topic",  "Category"),
        ("date",     "date",   "Date"),
        ("time",     "time",   "Time"),
        ("count",    "count",  "Count"),
        ("location", "place",  "Location"),
        ("name",     "person", "Name"),
    ],
}


# ─── Override normalisation ───────────────────────────────────────────────


def _normalize_overrides(raw: Any) -> dict[str, Any]:
    """Coerce + clean the operator-supplied chip_overrides blob to a
    known shape. Defensive — bad fields are dropped silently so a
    malformed override can never crash schema resolution."""
    out: dict[str, Any] = {"edited": {}, "added": [], "removed": set()}
    if not isinstance(raw, dict):
        return out
    ed = raw.get("edited")
    if isinstance(ed, dict):
        for field, fields in ed.items():
            if not isinstance(field, str) or not isinstance(fields, dict):
                continue
            clean: dict[str, Any] = {}
            cat = fields.get("category")
            if cat in ALLOWED_CATEGORIES:
                clean["category"] = cat
            label = fields.get("label")
            if isinstance(label, str) and label.strip():
                clean["label"] = label.strip()[:80]
            desc = fields.get("description")
            if isinstance(desc, str):
                clean["description"] = desc.strip()[:280]
            if clean:
                out["edited"][field] = clean
    added = raw.get("added")
    if isinstance(added, list):
        for row in added:
            if not isinstance(row, dict):
                continue
            f = row.get("field")
            cat = row.get("category")
            label = row.get("label")
            if not (isinstance(f, str) and f.strip()):
                continue
            if cat not in ALLOWED_CATEGORIES:
                continue
            if not (isinstance(label, str) and label.strip()):
                continue
            out["added"].append({
                "field":       f.strip().lower().replace(" ", "_")[:60],
                "category":    cat,
                "label":       label.strip()[:80],
                "description": (row.get("description") or "").strip()[:280] if isinstance(row.get("description"), str) else "",
            })
    rem = raw.get("removed")
    if isinstance(rem, list):
        out["removed"] = {x for x in rem if isinstance(x, str)}
    return out


# ─── Public API ───────────────────────────────────────────────────────────


def effective_schema(agent: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the final chip schema for THIS agent: sector defaults
    layered with the operator's chip_overrides.

    Resolution order:
      1. Sector defaults (SECTOR_CHIP_SCHEMA[sector] or _generic).
      2. Drop any field in `removed`.
      3. Overlay per-field edits from `edited` (label / category).
      4. Append `added` rows — each is `is_custom: True` so the UI
         can render an "added by you" badge.

    Returns: [{field, category, label, is_custom?, is_edited?}, ...]
    """
    sector = (agent.get("sector") or "_generic").strip().lower()
    base = SECTOR_CHIP_SCHEMA.get(sector) or SECTOR_CHIP_SCHEMA["_generic"]
    overrides = _normalize_overrides(agent.get("chip_overrides"))
    removed = overrides["removed"]
    edited = overrides["edited"]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field, cat, label in base:
        if field in seen or field in removed:
            continue
        seen.add(field)
        row = {"field": field, "category": cat, "label": label}
        ed = edited.get(field)
        if ed:
            if "category" in ed: row["category"] = ed["category"]
            if "label" in ed:    row["label"]    = ed["label"]
            if "description" in ed: row["description"] = ed["description"]
            row["is_edited"] = True
        out.append(row)
    for added in overrides["added"]:
        if added["field"] in seen:
            continue
        seen.add(added["field"])
        out.append({
            "field": added["field"],
            "category": added["category"],
            "label": added["label"],
            "description": added.get("description", ""),
            "is_custom": True,
        })
    return out


def extraction_hints_for_prompt(agent: dict[str, Any]) -> str:
    """Return a system-prompt block that Eva injects into the end_call
    instructions. Tells the LLM EXACTLY which `extracted.<field>` keys
    to populate (with human labels) so the vocabulary the dashboard
    chips render against matches the vocabulary the LLM produces.

    Empty string if the resolved schema is empty — keeps the prompt
    tight when an operator has chip_overrides removing everything.
    """
    schema = effective_schema(agent)
    if not schema:
        return ""
    lines = [
        "━━━━━━━━━━━━━ EXTRACTED-FIELDS VOCABULARY ━━━━━━━━━━━━━",
        "When you call end_call, populate `extracted` with these fields",
        "WHENEVER the caller mentions the relevant information. Use",
        "these exact field names — the dashboard expects them. Skip any",
        "field the caller didn't mention; don't invent values to fill",
        "slots.",
        "",
    ]
    for row in schema:
        cat_hint = SEMANTIC_CATEGORIES.get(row["category"], {}).get("hint", "")
        custom = "  (custom — operator-added)" if row.get("is_custom") else ""
        lines.append(f"  • {row['field']}  →  {row['label']}{custom}")
        if cat_hint:
            lines.append(f"      kind: {cat_hint}")
    return "\n".join(lines)
