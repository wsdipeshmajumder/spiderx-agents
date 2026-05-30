"""Static presets used by both the builder agent (as enums in its tool schema)
and the frontend tweaks panel. Edit freely to expand sectors / locales / voices."""

from __future__ import annotations

SECTORS = [
    {"id": "healthcare", "label": "Healthcare clinic / hospital"},
    {"id": "dental", "label": "Dental practice"},
    {"id": "real_estate", "label": "Real estate"},
    {"id": "restaurant", "label": "Restaurant / hospitality"},
    {"id": "salon", "label": "Salon / spa"},
    {"id": "retail", "label": "Retail / e-commerce support"},
    {"id": "logistics", "label": "Logistics / delivery"},
    {"id": "banking", "label": "Banking / financial services"},
    {"id": "insurance", "label": "Insurance"},
    {"id": "education", "label": "Education / coaching"},
    {"id": "events", "label": "Events / ticketing"},
    {"id": "travel", "label": "Travel / hotel booking"},
    {"id": "automotive", "label": "Automotive service"},
    {"id": "legal", "label": "Legal intake"},
    {"id": "saas_support", "label": "SaaS support / IT helpdesk"},
    {"id": "generic", "label": "Generic receptionist"},
]

LOCALES = [
    # English variants Gemini Live ships natively. Every region profile's
    # `default_agent_locale` MUST resolve to one of these (the save_agent
    # declaration binds `locale` to this enum — missing IDs = rejected
    # tool calls). en-AU was missing pre-audit; Australian agent saves
    # were silently failing.
    {"id": "en-IN", "label": "English (India)"},
    {"id": "en-US", "label": "English (US)"},
    {"id": "en-GB", "label": "English (UK)"},
    {"id": "en-AU", "label": "English (Australia)"},
    # India regional languages
    {"id": "hi-IN", "label": "Hindi (India)"},
    {"id": "bn-IN", "label": "Bengali (India)"},
    {"id": "ta-IN", "label": "Tamil (India)"},
    {"id": "te-IN", "label": "Telugu (India)"},
    {"id": "kn-IN", "label": "Kannada (India)"},
    {"id": "ml-IN", "label": "Malayalam (India)"},
    {"id": "mr-IN", "label": "Marathi (India)"},
    {"id": "gu-IN", "label": "Gujarati (India)"},
    # International
    {"id": "es-ES", "label": "Spanish (Spain)"},
    {"id": "es-MX", "label": "Spanish (Mexico)"},
    {"id": "pt-BR", "label": "Portuguese (Brazil)"},
    {"id": "fr-FR", "label": "French (France)"},
    {"id": "de-DE", "label": "German (Germany)"},
    {"id": "it-IT", "label": "Italian (Italy)"},
    {"id": "nl-NL", "label": "Dutch (Netherlands)"},
    {"id": "ar-SA", "label": "Arabic (Saudi Arabia)"},
    {"id": "ja-JP", "label": "Japanese (Japan)"},
    {"id": "ko-KR", "label": "Korean (South Korea)"},
    {"id": "zh-CN", "label": "Mandarin (China)"},
]

VOICES = [
    {"id": "Aoede", "label": "Aoede — warm, friendly"},
    {"id": "Puck", "label": "Puck — bright, upbeat"},
    {"id": "Charon", "label": "Charon — deep, calm"},
    {"id": "Kore", "label": "Kore — clear, neutral"},
    {"id": "Fenrir", "label": "Fenrir — energetic, gruff"},
    {"id": "Leda", "label": "Leda — soft, conversational"},
    {"id": "Orus", "label": "Orus — measured, formal"},
    {"id": "Zephyr", "label": "Zephyr — light, breezy"},
]

# Platform-wide voice fallback. Imported by gemini_bridge (Eva's prompt
# + runtime session voice resolution) and db_pg (last-resort voice on
# create_agent). Region profiles in gemini_bridge override this per-
# country. One source of truth — see NORTHSTAR Part II §inconsistencies.
DEFAULT_VOICE = "Aoede"

GUARDRAIL_LIBRARY = [
    {"id": "no_medical_advice", "label": "Never give medical advice; defer to a clinician."},
    {"id": "no_legal_advice", "label": "Never give legal advice; defer to a licensed attorney."},
    {"id": "no_financial_advice", "label": "No personalized financial advice; defer to an advisor."},
    {"id": "no_pii_recital", "label": "Never read full card numbers, OTPs, or passwords aloud."},
    {"id": "verify_identity", "label": "Verify caller identity (name + DOB or order ID) before account-specific actions."},
    {"id": "no_promises", "label": "Don't promise outcomes (refunds, approvals) on behalf of humans."},
    {"id": "escalate_to_human", "label": "Offer escalation to a human if the caller asks twice or is upset."},
    {"id": "respect_dnc", "label": "Honor 'do not call' / 'remove me' requests immediately."},
    {"id": "no_after_hours_commit", "label": "Do not commit to appointments outside business hours."},
    {"id": "language_safety", "label": "Refuse abusive, sexual, or illegal requests and end the call politely."},
]

CONNECTOR_TYPES = [
    {"id": "http_webhook", "label": "HTTP webhook (POST JSON)"},
    {"id": "calendar_book", "label": "Calendar — book appointment"},
    {"id": "calendar_check", "label": "Calendar — check availability"},
    {"id": "crm_lookup", "label": "CRM — lookup customer by phone/email"},
    {"id": "crm_create_lead", "label": "CRM — create lead"},
    {"id": "order_status", "label": "Order status lookup"},
    {"id": "knowledge_base_search", "label": "Knowledge base search (RAG)"},
    {"id": "sms_send", "label": "Send SMS confirmation"},
    {"id": "email_send", "label": "Send email confirmation"},
    {"id": "payment_link", "label": "Generate payment link"},
]

# Order matters: the dashboard's Go-live provider dropdown renders these
# in this order and defaults to the FIRST self_service entry (Plivo).
# Each self_service provider carries a `registrar` (the SIP server
# domain we pre-fill), a `console_url` (deep link to where the operator
# buys a number / creates an endpoint), and a `blurb`. Non-self-service
# providers are listed for completeness but route through the managed
# number-request flow.
SIP_PROVIDERS = [
    {"id": "plivo", "label": "Plivo",
     "self_service": True,
     "registrar": "sip.plivo.com",
     "console_url": "https://console.plivo.com",
     "blurb": "Self-service SIP forwarding. Create a Plivo SIP endpoint, "
              "point its application at our SIP URI, and inbound calls land "
              "on this agent."},
    {"id": "exotel", "label": "Exotel",
     "self_service": True,
     "registrar": "sip.exotel.com",
     "console_url": "https://my.exotel.com",
     "blurb": "Self-service SIP forwarding for Indian numbers. Connect your "
              "Exotel SIP endpoint and route inbound calls to this agent."},
    {"id": "voniz", "label": "Voniz / Vobiz",
     "self_service": True,
     "registrar": "registrar.vobiz.ai",
     "console_url": "https://voniz.com/console",
     "blurb": "Self-service SIP forwarding. Paste your Voniz endpoint here, "
              "point its Application at our SIP URI, and inbound calls land "
              "on this agent."},
    {"id": "twilio", "label": "Twilio Programmable Voice"},
    {"id": "telnyx", "label": "Telnyx"},
    {"id": "vonage", "label": "Vonage"},
    {"id": "asterisk", "label": "Self-hosted Asterisk / FreePBX"},
    {"id": "generic_sip", "label": "Other SIP trunk (RFC 3261)"},
]


def all_presets() -> dict:
    from .info_schemas import INFO_GROUPS_BY_SECTOR, _SECTOR_ALIASES
    return {
        "sectors": SECTORS,
        "locales": LOCALES,
        "voices": VOICES,
        "guardrails": GUARDRAIL_LIBRARY,
        "connectors": CONNECTOR_TYPES,
        "sip_providers": SIP_PROVIDERS,
        # Industry-adaptive Additional Info field groups. The dashboard
        # picks the list matching the agent's sector (with alias fallback).
        "info_groups": INFO_GROUPS_BY_SECTOR,
        "info_sector_aliases": _SECTOR_ALIASES,
    }
