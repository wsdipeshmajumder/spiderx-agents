"""v1.0 readiness sweep — seeds 12 industries x 4 locales = 48 agents.

Goes through the same code paths Eva would: silent_defaults.merge_into_save_args
+ db.create_agent. The shapes match real builds. Runs against the local
sqlite DB; safe to re-run (deletes prior sweep agents by user first).

Usage:
    .venv/bin/python scripts/seed_v1_sweep.py [--wipe]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db
from backend.silent_defaults import merge_into_save_args


INDUSTRIES = [
    # 12 sectors covering the v1.0 launch surface — picked for breadth across
    # India (banking, salon, real estate, dental, restaurant) + global (SaaS,
    # legal, travel, education, automotive, insurance, retail).
    {"sector": "dental",       "tone": "warm dental receptionist",
     "voice": "Aoede",
     "first_sentences": "Acknowledges the caller, asks who they'd like to book with, confirms time, sends an SMS."},
    {"sector": "restaurant",   "tone": "polished maître d'",
     "voice": "Leda",
     "first_sentences": "Confirms party size, dietary asks, books a table, repeats the time."},
    {"sector": "salon",        "tone": "cheerful salon front-desk",
     "voice": "Puck",
     "first_sentences": "Books a service, confirms stylist, sends a calendar invite."},
    {"sector": "real_estate",  "tone": "knowledgeable broker assistant",
     "voice": "Charon",
     "first_sentences": "Qualifies budget + bedrooms + locality, schedules a viewing, creates a CRM lead."},
    {"sector": "banking",      "tone": "calm banking concierge",
     "voice": "Orus",
     "first_sentences": "Verifies caller (DOB / order id), routes to the right specialist, never recites card numbers."},
    {"sector": "insurance",    "tone": "patient insurance support",
     "voice": "Kore",
     "first_sentences": "Pulls up the policy, walks through claim status, offers callback if waiting on adjuster."},
    {"sector": "education",    "tone": "encouraging admissions coach",
     "voice": "Aoede",
     "first_sentences": "Asks about the prospective student, books a counselling slot, follows up with brochure."},
    {"sector": "travel",       "tone": "well-travelled trip planner",
     "voice": "Leda",
     "first_sentences": "Asks origin / dates / pax, drafts an itinerary, books holds, sends an email summary."},
    {"sector": "automotive",   "tone": "service-bay scheduler",
     "voice": "Fenrir",
     "first_sentences": "Confirms make/model + complaint, books a slot, gives a pickup ETA."},
    {"sector": "legal",        "tone": "intake paralegal",
     "voice": "Charon",
     "first_sentences": "Captures matter type + parties, schedules a 15-min consult, never gives legal advice."},
    {"sector": "retail",       "tone": "friendly e-commerce support",
     "voice": "Puck",
     "first_sentences": "Looks up the order, offers refund / replacement / status, never reads card numbers."},
    {"sector": "saas_support", "tone": "calm IT helpdesk",
     "voice": "Kore",
     "first_sentences": "Captures the issue, creates a ticket, offers a callback ETA, never asks for passwords."},
]

LOCALES = [
    {"id": "en-IN", "name_seed": ["Maya", "Kabir", "Aarav", "Ishita"],     "greeting_template": "Namaste, this is {name} at {brand}. How can I help you today?"},
    {"id": "hi-IN", "name_seed": ["Maya", "Kabir", "Riya", "Arjun"],       "greeting_template": "नमस्ते, ये {name} {brand} से बोल रही हूँ। मैं आपकी कैसे मदद कर सकती हूँ?"},
    {"id": "en-US", "name_seed": ["Alex", "Jordan", "Taylor", "Sam"],      "greeting_template": "Hi, this is {name} at {brand}. How can I help you today?"},
    {"id": "en-GB", "name_seed": ["Olivia", "Harry", "Emily", "George"],   "greeting_template": "Hello, you've reached {name} at {brand}. How can I help?"},
]

BRANDS_BY_SECTOR = {
    "dental":       "BrightSmile Dental",
    "restaurant":   "Olive Lane Bistro",
    "salon":        "Lush Hair & Spa",
    "real_estate":  "Meridian Realty",
    "banking":      "North Trust Bank",
    "insurance":    "Sterling Cover",
    "education":    "Crestline Academy",
    "travel":       "Wander Co.",
    "automotive":   "Apex Auto Service",
    "legal":        "Lawson & Holt",
    "retail":       "Coastline Goods",
    "saas_support": "Anchor SaaS",
}


def build_payload(industry: dict, locale: dict, name_idx: int) -> dict:
    name = locale["name_seed"][name_idx % len(locale["name_seed"])]
    brand = BRANDS_BY_SECTOR[industry["sector"]]
    return {
        "name": name,
        "sector": industry["sector"],
        "locale": locale["id"],
        "voice": industry["voice"],
        "persona": f"A {industry['tone']} at {brand}. Polite, calm, never makes promises a human can't keep.",
        "greeting": locale["greeting_template"].format(name=name, brand=brand),
        "system_prompt": (
            f"You are {name}, a {industry['tone']} at {brand}. Locale: {locale['id']}.\n"
            f"Behaviours: {industry['first_sentences']}\n"
            "Never give medical/legal/financial advice. Don't read card numbers aloud. "
            "If a caller asks twice for a human, hand off."
        ),
        "guardrails": ["no_medical_advice", "no_pii_recital", "escalate_to_human"],
        "connectors": ["calendar_check", "sms_send", "knowledge_base_search"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true", help="Delete all existing agents owned by founder first")
    args = ap.parse_args()

    db.init()
    founder = db.get_founder()
    print(f"Founder: id={founder['id']} email={founder['email']}")

    if args.wipe:
        agents = db.list_agents(founder["id"])
        for a in agents:
            db.delete_agent(a["id"])
        print(f"Wiped {len(agents)} agent(s).")

    created = []
    for industry in INDUSTRIES:
        for li, locale in enumerate(LOCALES):
            payload = build_payload(industry, locale, name_idx=li)
            merged = merge_into_save_args(payload)
            row = db.create_agent(merged, user_id=founder["id"])
            created.append(row)
            print(f"  ✓ {industry['sector']:<12} {locale['id']}  →  id={row['id']:<3} slug={row['slug']:<40} voice={row['voice']}")
    print(f"\nSeeded {len(created)} agents (expected 12×4=48).")
    if len(created) != 48:
        print("UNEXPECTED COUNT — investigate.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
