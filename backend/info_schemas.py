"""Industry-adaptive "Additional Info" field-group schemas.

Each agent carries an `extra_info` JSONB map of {group_id: free_text}.
WHICH groups an operator sees depends on the agent's sector (auto-set by
Eva during the build via the matched template's agent_profile.sector).
A dental clinic gets Treatments / Doctors / Insurance; a restaurant gets
Menu Highlights / Daily Specials / Seating; a dealership gets Inventory /
Finance / Test-drive policy; etc.

The same schema is consumed in three places:
  • the dashboard's Additional Info page (renders the accordion groups)
  • the per-agent save/load (validates group ids)
  • the LIVE CALL prompt builder (renders a "REFERENCE INFO" section from
    the filled groups so the agent answers callers with this knowledge)

Keeping it server-side (not just in the frontend) means the call-time
prompt and the dashboard editor agree on labels without duplication.

Shape of a group:
  { "id": "menu_highlights",
    "label": "Menu Highlights",
    "emoji": "🍽️",
    "desc": "Signature dishes, must-try items, chef's recommendations",
    "info_only": False }   # info_only groups are reference-only, never
                           # drive an action like booking — surfaced as a
                           # gentle note in the UI.
"""
from __future__ import annotations

from typing import Any


def _g(gid: str, label: str, emoji: str, desc: str, info_only: bool = False) -> dict[str, Any]:
    return {"id": gid, "label": label, "emoji": emoji, "desc": desc, "info_only": info_only}


# Per-industry group lists. Keys are the canonical sector ids the
# templates assign (restaurant / dental / automotive / salon / generic).
INFO_GROUPS_BY_SECTOR: dict[str, list[dict[str, Any]]] = {
    "restaurant": [
        _g("menu_highlights",  "Menu Highlights",   "🍽️", "Signature dishes, must-try items, chef's recommendations"),
        _g("daily_specials",   "Daily Specials",    "🏷️", "Seasonal ingredients, chef specials, limited items"),
        _g("dietary_options",  "Dietary Options",   "🥗", "Vegan, gluten-free, allergen info, customizations"),
        _g("promotions",       "Promotions & Offers","🎁", "Discounts, happy hours, special deals"),
        _g("seating_ambiance", "Seating & Ambiance","🛋️", "Seating arrangements, capacity, ambiance, private dining", info_only=True),
        _g("holidays_events",  "Holidays & Events", "📅", "Special menus, seasonal events, celebrations", info_only=True),
        _g("payment_modes",    "Payment Modes",     "💳", "Accepted payment methods, policies, and procedures"),
    ],
    "dental": [
        _g("treatments",        "Treatments & Procedures", "🦷", "Procedures offered, what each involves, typical duration"),
        _g("doctors",           "Doctors & Specialists",   "🩺", "Dentists on staff, their specialties, who to ask for"),
        _g("insurance_payment", "Insurance & Payment",     "💳", "Insurance accepted, payment plans, financing"),
        _g("appointment_policy","Appointment Policy",      "🗓️", "Booking, cancellation, late arrivals, new-patient steps"),
        _g("promotions",        "Promotions & Offers",     "🎁", "First-visit offers, package deals, discounts"),
        _g("emergency",         "Emergency Protocol",      "🚑", "After-hours guidance, dental emergencies, what to do"),
        _g("facilities",        "Facilities & Amenities",  "🏥", "Parking, accessibility, languages, amenities", info_only=True),
    ],
    "automotive": [
        _g("inventory",       "Models & Inventory",  "🚗", "Brands & models in stock, variants, colours"),
        _g("pricing_finance", "Pricing & Finance",   "💰", "On-road price guidance, EMI, financing partners"),
        _g("test_drive",      "Test Drive Policy",   "🔑", "How test drives work, documents needed, booking"),
        _g("service_warranty","Service & Warranty",  "🔧", "Service centre, warranty, AMC packages"),
        _g("trade_in",        "Trade-in / Exchange", "🔁", "Exchange process, valuation, paperwork"),
        _g("promotions",      "Promotions & Offers", "🎁", "Seasonal offers, exchange bonuses, discounts"),
        _g("payment_modes",   "Payment Modes",       "💳", "Accepted payment methods, booking amount"),
    ],
    "salon": [
        _g("services_pricing",   "Services & Pricing",     "✂️", "Service menu with indicative pricing"),
        _g("stylists",           "Stylists & Specialists", "💇", "Stylists/groomers and their specialties"),
        _g("products_used",      "Products Used",          "🧴", "Brands/products used, retail products sold"),
        _g("booking_policy",     "Booking Policy",         "🗓️", "Appointment vs walk-in, cancellation, deposits"),
        _g("promotions",         "Promotions & Offers",    "🎁", "First-visit offers, packages, membership deals"),
        _g("facilities",         "Facilities & Amenities", "🏠", "Parking, accessibility, amenities", info_only=True),
        _g("payment_modes",      "Payment Modes",          "💳", "Accepted payment methods, policies"),
    ],
    "healthcare": [
        _g("services",          "Departments & Services",  "🏥", "Departments, specialties, services offered"),
        _g("doctors",           "Doctors & Specialists",   "🩺", "Doctors on staff, specialties, consulting hours"),
        _g("insurance_payment", "Insurance & Payment",     "💳", "Insurance/TPA accepted, cashless, payment plans"),
        _g("appointment_policy","Appointment Policy",      "🗓️", "Booking, walk-in, cancellation, new-patient steps"),
        _g("diagnostics",       "Diagnostics & Labs",      "🔬", "Lab tests, imaging, report turnaround times"),
        _g("emergency",         "Emergency Protocol",      "🚑", "Emergency/ambulance, after-hours, what callers do"),
        _g("facilities",        "Facilities & Amenities",  "🛎️", "Parking, pharmacy, accessibility, languages", info_only=True),
    ],
    "real_estate": [
        _g("listings",       "Listings & Properties", "🏘️", "Properties available — types, locations, BHK/size"),
        _g("pricing",        "Pricing",               "💰", "Price ranges, price per sq ft, negotiation policy"),
        _g("site_visits",    "Site Visits",           "🚶", "How site visits work, scheduling, documents needed"),
        _g("documentation",  "Documentation & Loans", "📑", "Paperwork, home loans, registration assistance"),
        _g("promotions",     "Promotions & Offers",   "🎁", "Launch offers, payment plans, discounts"),
        _g("amenities",      "Amenities & Possession", "🏗️", "Project amenities, possession timeline", info_only=True),
        _g("payment_modes",  "Payment Modes",         "💳", "Booking amount, payment schedule, methods"),
    ],
    "retail": [
        _g("products",       "Products & Catalog",   "🛍️", "Product categories, bestsellers, availability"),
        _g("pricing_offers", "Pricing & Offers",     "🏷️", "Pricing, current sales, coupons, loyalty"),
        _g("order_status",   "Orders & Tracking",    "📦", "How to check orders, tracking, timelines"),
        _g("returns_refunds","Returns & Refunds",    "↩️", "Return window, refund policy, exchanges"),
        _g("shipping",       "Shipping & Delivery",  "🚚", "Delivery areas, charges, timelines"),
        _g("store_info",     "Store Info",           "🏬", "Store locations, hours, in-store services", info_only=True),
        _g("payment_modes",  "Payment Modes",        "💳", "Accepted methods, COD, EMI"),
    ],
    "logistics": [
        _g("services",          "Services & Coverage",  "🚛", "Services offered, coverage areas, vehicle types"),
        _g("pricing",           "Pricing",              "💰", "Rate guidance, surcharges, minimums"),
        _g("tracking",          "Tracking",             "📍", "How tracking works, status definitions"),
        _g("delivery_timelines","Delivery Timelines",   "⏱️", "SLAs, express vs standard, cut-off times"),
        _g("claims",            "Claims",               "📋", "Damage/loss claims process and timelines"),
        _g("service_areas",     "Service Areas",        "🗺️", "Pin codes / regions served", info_only=True),
        _g("payment_modes",     "Billing & Payment",    "💳", "Invoicing, COD remittance, accepted methods"),
    ],
    "banking": [
        _g("products",           "Products",             "🏦", "Accounts, loans, cards offered"),
        _g("rates_fees",         "Rates & Fees",         "📊", "Indicative rates & charges (never promise firm numbers)"),
        _g("eligibility_docs",   "Eligibility & KYC",    "📑", "Eligibility criteria, KYC documents needed"),
        _g("application_process","Application Process",  "🗂️", "How to apply, steps, timelines"),
        _g("support_help",       "Support & Safety",     "🔒", "Card block, disputes, fraud — what callers should do"),
        _g("branch_info",        "Branch & ATM Info",    "🏧", "Branch locations, hours, ATMs", info_only=True),
    ],
    "insurance": [
        _g("products",            "Policies Offered",     "🛡️", "Life, health, motor, etc. — what's available"),
        _g("coverage",            "Coverage & Exclusions","📄", "What's covered, exclusions, sum-assured ranges"),
        _g("premium_eligibility", "Premium & Eligibility","💰", "Premium guidance, eligibility, documents"),
        _g("claims_process",      "Claims Process",       "📋", "How to file a claim, documents, timelines"),
        _g("renewals",            "Renewals",             "🔁", "Renewal process, grace period, lapse policy"),
        _g("promotions",          "Promotions & Riders",  "🎁", "Offers, discounts, add-on riders"),
        _g("support_help",        "Policy Servicing",     "🛎️", "Endorsements, nominee changes, contact"),
    ],
    "education": [
        _g("courses",     "Courses & Programs",  "📚", "Courses/programs offered, levels, duration"),
        _g("fees",        "Fees & Scholarships", "💰", "Fee structure, payment plans, scholarships"),
        _g("admissions",  "Admissions",          "📝", "Admission process, eligibility, deadlines"),
        _g("schedule",    "Batches & Schedule",  "🗓️", "Batch timings, online/offline, start dates"),
        _g("faculty",     "Faculty",             "👩‍🏫", "Instructors and their specialties"),
        _g("promotions",  "Promotions & Offers", "🎁", "Early-bird, referral, discount offers"),
        _g("facilities",  "Facilities",          "🏫", "Campus, labs, hostel, transport", info_only=True),
    ],
    "events": [
        _g("events_lineup", "Events Lineup",       "🎤", "Upcoming events, dates, venues"),
        _g("ticketing",     "Ticketing",           "🎟️", "Ticket types, pricing tiers, availability"),
        _g("venue_info",    "Venue Info",          "📍", "Venue, seating, capacity, directions", info_only=True),
        _g("policies",      "Policies",            "📋", "Refund, transfer, entry rules, age limits"),
        _g("promotions",    "Promotions & Offers", "🎁", "Early-bird, group discounts, offers"),
        _g("accessibility", "Accessibility",       "♿", "Parking, wheelchair access, facilities", info_only=True),
        _g("payment_modes", "Payment Modes",       "💳", "Accepted payment methods"),
    ],
    "travel": [
        _g("packages",       "Packages & Destinations","🌍", "Packages/destinations offered, durations"),
        _g("pricing",        "Pricing",                "💰", "Price ranges, what's included/excluded"),
        _g("booking_policy", "Booking Policy",         "🗓️", "Booking, deposits, cancellation, rescheduling"),
        _g("documentation",  "Travel Documents",       "🛂", "Visa, passport, insurance requirements"),
        _g("promotions",     "Promotions & Offers",    "🎁", "Seasonal deals, group rates, offers"),
        _g("inclusions",     "Inclusions",             "🧳", "Meals, transfers, guides, what's bundled", info_only=True),
        _g("payment_modes",  "Payment Modes",          "💳", "Payment schedule, accepted methods"),
    ],
    "legal": [
        _g("practice_areas",  "Practice Areas",     "⚖️", "Areas of law the firm handles"),
        _g("consultation",    "Consultation",       "🗓️", "Consultation fee, duration, how to book"),
        _g("intake_process",  "Intake Process",     "📝", "What info is needed to open a new matter"),
        _g("fees_billing",    "Fees & Billing",     "💰", "Fee structure, retainer, billing model"),
        _g("attorneys",       "Attorneys",          "👔", "Attorneys/specialists on staff"),
        _g("confidentiality", "Confidentiality",    "🔒", "What the agent must NOT advise on — intake only", info_only=True),
        _g("office_info",     "Office Info",        "🏢", "Office locations, hours", info_only=True),
    ],
    "saas_support": [
        _g("products",        "Products & Plans",    "💻", "Products/plans supported, feature overview"),
        _g("common_issues",   "Common Issues",       "🛠️", "Top issues + first-line fixes / workarounds"),
        _g("account_billing", "Account & Billing",   "💳", "Billing, plan changes, refunds"),
        _g("integrations",    "Integrations",        "🔌", "Supported integrations, setup help"),
        _g("escalation",      "Escalation",          "📈", "When/how to escalate, SLA tiers"),
        _g("known_issues",    "Known Issues",        "⚠️", "Current outages / known bugs", info_only=True),
        _g("resources",       "Resources",           "🔗", "Docs, status page, help-center links", info_only=True),
    ],
    "generic": [
        _g("products_services", "Products & Services",  "📦", "What you offer — key products and services"),
        _g("pricing",           "Pricing",              "💰", "Pricing guidance, packages, quotes"),
        _g("promotions",        "Promotions & Offers",  "🎁", "Current offers, discounts, deals"),
        _g("policies",          "Policies",             "📋", "Cancellation, returns, guarantees, hours nuances"),
        _g("faqs",              "FAQs",                 "❓", "Common questions callers ask + the answers"),
        _g("payment_modes",     "Payment Modes",        "💳", "Accepted payment methods"),
    ],
}

# Sector-id aliases → canonical key above. Agents built via the
# probabilistic (non-template) flow can carry sector values like
# "healthcare" or "hospitality"; map them so they still get a sensible
# group set instead of the bare generic fallback.
_SECTOR_ALIASES = {
    # Every canonical SECTORS id now has its OWN group set above; these
    # aliases only catch free-text / synonym sector values that the
    # probabilistic build flow might assign.
    "clinic": "healthcare",
    "hospital": "healthcare",
    "medical": "healthcare",
    "hospitality": "restaurant",
    "cafe": "restaurant",
    "food": "restaurant",
    "auto": "automotive",
    "dealership": "automotive",
    "car": "automotive",
    "spa": "salon",
    "beauty": "salon",
    "grooming": "salon",
    "realty": "real_estate",
    "property": "real_estate",
    "ecommerce": "retail",
    "e-commerce": "retail",
    "shop": "retail",
    "delivery": "logistics",
    "shipping": "logistics",
    "finance": "banking",
    "fintech": "banking",
    "coaching": "education",
    "edtech": "education",
    "school": "education",
    "ticketing": "events",
    "hotel": "travel",
    "tourism": "travel",
    "law": "legal",
    "saas": "saas_support",
    "it_support": "saas_support",
    "helpdesk": "saas_support",
}


def groups_for(sector: str | None) -> list[dict[str, Any]]:
    """Resolve the field-group list for an agent's sector. Falls back to
    the generic set for anything we don't recognise."""
    s = (sector or "").strip().lower()
    if s in INFO_GROUPS_BY_SECTOR:
        return INFO_GROUPS_BY_SECTOR[s]
    if s in _SECTOR_ALIASES:
        return INFO_GROUPS_BY_SECTOR[_SECTOR_ALIASES[s]]
    return INFO_GROUPS_BY_SECTOR["generic"]


def group_label(sector: str | None, group_id: str) -> str:
    """Human label for a group id within a sector (for the call-prompt
    REFERENCE INFO section). Falls back to a title-cased id."""
    for g in groups_for(sector):
        if g["id"] == group_id:
            return g["label"]
    return group_id.replace("_", " ").title()


def prefill_extra_info(sector: str | None, variables: dict[str, Any] | None) -> dict[str, Any]:
    """Seed an agent's extra_info from facts Eva captured during the
    build, so the operator lands on a partially-filled Additional Info
    page instead of a blank one.

    We map two high-signal captured facts:
      • variables.services → the sector's FIRST group (its "what we
        offer" group — menu_highlights / treatments / products / …).
      • variables.offers   → the "promotions" group (present in most
        sectors; skipped where absent).

    Conservative: only fills groups that genuinely exist for the sector,
    and only from non-empty captured values. The operator reviews and
    expands from there — this is a head-start, not the final word."""
    if not isinstance(variables, dict) or not variables:
        return {}
    groups = groups_for(sector)
    ids = [g["id"] for g in groups]
    if not ids:
        return {}

    def _as_text(v: Any) -> str:
        if isinstance(v, list):
            return ", ".join(str(x).strip() for x in v if str(x).strip())
        return str(v).strip() if v is not None else ""

    out: dict[str, Any] = {}
    services = _as_text(variables.get("services"))
    if services:
        out[ids[0]] = services
    offers = _as_text(variables.get("offers"))
    if offers and "promotions" in ids:
        out["promotions"] = offers
    return out


def render_reference_block(
    sector: str | None,
    extra_info: dict[str, Any] | None,
    groups: list[dict[str, Any]] | None = None,
) -> str:
    """Build the REFERENCE INFO section for the live-call system prompt
    from the operator's filled groups. Empty groups are skipped. Returns
    '' when nothing's filled so the prompt stays lean.

    `groups` overrides the sector default — pass an agent's per-agent
    `info_groups` (catch-all builds) so the block reflects ITS schema."""
    if not isinstance(extra_info, dict) or not extra_info:
        return ""
    group_list = groups if (isinstance(groups, list) and groups) else groups_for(sector)
    lines: list[str] = []
    for g in group_list:
        val = extra_info.get(g["id"])
        if not val:
            continue
        text = val.strip() if isinstance(val, str) else str(val)
        if not text:
            continue
        lines.append(f"## {g['label']}\n{text}")
    if not lines:
        return ""
    return (
        "\n\n━━━━━━━━━━━━━ REFERENCE INFO (answer callers using this) ━━━━━━━━━━━━━\n"
        "The operator provided the following business knowledge. Use it to "
        "answer caller questions accurately. If a caller asks about something "
        "not covered here, don't invent — offer a callback or to take a message.\n\n"
        + "\n\n".join(lines)
    )
