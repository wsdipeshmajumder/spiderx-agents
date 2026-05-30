"""SIP-trunk configuration for inbound phone routing.

The `agents.sip_config` JSONB column has been a free-form slot for ages.
With self-service Voniz support landing, we now define a CANONICAL
shape so the dashboard can read/write structured fields and the (future)
SIP terminator can route inbound INVITEs deterministically by parsing
the URI's user-part back to an agent id.

Shape stored on `agents.sip_config`:

  {
    "provider":    "voniz",           # required, must be in SIP_PROVIDERS
    "alias":       "sonyk",           # operator-friendly name from their console
    "username":    "...",             # SIP username at the provider
    "registrar":   "registrar.vobiz.ai",  # SIP server domain
    "remote_uri":  "sip:<username>@<registrar>",
                                       # the operator's identity AT the provider
    "password":    "...",             # optional — only stored if the operator
                                       # entered it (needed for outbound calls
                                       # later; not needed for inbound)
    "inbound_uri": "sip:agent-<id>@sip.spiderx.ai",
                                       # the URI we generate, that the operator
                                       # pastes into Voniz's Application field
    "status":      "configured",      # configured | needs_credentials |
                                       # active | failed
    "configured_at": "ISO-8601",      # when the operator last saved
    "verified_at":   "ISO-8601 | None",
                                       # when we last saw a successful call
                                       # (Phase 2 — currently always null)
  }

Validation rules (enforced by `validate_and_normalize`):
  • provider must be a known SIP_PROVIDERS id
  • alias / username / registrar are required strings, trimmed, ≤120 chars
  • registrar must look like a domain (basic regex)
  • remote_uri is auto-derived from username + registrar if absent
  • password is optional, write-only (we don't echo it back to the
    dashboard once stored — see `redacted_view`)
  • status is computed from the inputs (not operator-supplied)
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .presets import SIP_PROVIDERS


# Inbound SIP host — where Voniz (or any forwarder) should send INVITEs.
# Set via env so the operator's "paste this URI into Voniz" instruction is
# always correct for the deployment. Default is a placeholder for local /
# unconfigured environments; production should set SIP_INBOUND_HOST to
# the public SIP server hostname.
SIP_INBOUND_HOST = os.environ.get("SIP_INBOUND_HOST", "sip.spiderx.ai")


_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}\.)+[a-zA-Z]{2,}$")
_SIP_URI_RE = re.compile(r"^sip:[^@]+@[^@]+$")
# E.164: leading '+', 1-15 digits total, first digit non-zero. The
# spec allows up to 15 digits including the country code; we accept
# 7-15 to flag obvious typos (a 3-digit DID is almost certainly wrong)
# while still allowing short national numbers (e.g. some India tollfree).
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _normalize_did(raw: Any) -> str:
    """Strip spaces, dashes, and parens from a DID; return E.164-shaped
    string (with leading +). Returns '' if the input is empty or
    obviously not a phone number. Caller validates the result against
    _E164_RE."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # Common operator typo: missing '+'. If it's all digits and looks
    # plausibly long, assume they meant a leading '+'.
    cleaned = re.sub(r"[\s\-().]", "", s)
    if cleaned and not cleaned.startswith("+") and cleaned.isdigit() and len(cleaned) >= 7:
        cleaned = "+" + cleaned
    return cleaned


def _trim(v: Any, *, max_len: int = 120) -> str:
    """Trim + cap a single string field. Returns '' for None/non-string."""
    if v is None:
        return ""
    s = str(v).strip()
    return s[:max_len]


def known_provider_ids() -> set[str]:
    """The set of provider ids the dashboard is allowed to write."""
    return {p["id"] for p in SIP_PROVIDERS}


def inbound_uri_for(agent_id: int) -> str:
    """The SIP URI the operator pastes into their Voniz Application field.
    Format: `sip:agent-<id>@<SIP_INBOUND_HOST>`. The user-part encodes the
    agent id so the (future) SIP terminator can dispatch the inbound
    INVITE without a database lookup."""
    return f"sip:agent-{int(agent_id)}@{SIP_INBOUND_HOST}"


def validate_and_normalize(
    raw: dict[str, Any], *, agent_id: int, existing: Optional[dict[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate operator-supplied sip_config and produce the canonical
    stored shape. Returns (config, None) on success or (None, error_msg)
    on validation failure.

    `existing` is the previously-stored config (if any) — we preserve
    the `password` from existing when the operator doesn't re-enter one
    (otherwise the field would clear on every dashboard save).
    """
    if not isinstance(raw, dict):
        return None, "sip_config must be a JSON object"

    provider = _trim(raw.get("provider")).lower()
    if not provider:
        return None, "provider is required"
    if provider not in known_provider_ids():
        return None, f"unknown provider {provider!r}"

    alias = _trim(raw.get("alias"))
    username = _trim(raw.get("username"))
    registrar = _trim(raw.get("registrar")).lower()
    remote_uri = _trim(raw.get("remote_uri"), max_len=240)

    # If the operator pasted a SIP URI, we can derive username +
    # registrar from it (covers the case where they copy-paste the URI
    # straight from the Voniz dashboard without splitting it themselves).
    if remote_uri and (not username or not registrar):
        m = re.match(r"^sip:([^@]+)@([^@;:]+)", remote_uri)
        if m:
            if not username:
                username = m.group(1)
            if not registrar:
                registrar = m.group(2).lower()

    if not alias:
        return None, "alias is required"
    if not username:
        return None, "username is required"
    if not registrar:
        return None, "registrar is required"
    if not _DOMAIN_RE.match(registrar):
        return None, f"registrar {registrar!r} doesn't look like a domain"

    # Re-derive remote_uri canonically so what we store is always well-
    # formed regardless of what the operator typed.
    remote_uri = f"sip:{username}@{registrar}"

    # DID — the actual phone number callers dial. Optional in v1
    # (operator might paste credentials before buying the number) but
    # without it the channel isn't actually live. When present, must
    # be valid E.164.
    did = _normalize_did(raw.get("did"))
    if did and not _E164_RE.match(did):
        return None, f"did {did!r} doesn't look like an E.164 phone number (expected +<country><number>, 7-15 digits)"

    # Password: optional. Preserve from existing if not re-entered.
    password_in = raw.get("password")
    if password_in is None or (isinstance(password_in, str) and not password_in.strip()):
        password = (existing or {}).get("password") or ""
    else:
        password = _trim(password_in, max_len=240)

    # Status derivation:
    #   • `pending_did` — credentials saved but no DID yet → operator
    #     still needs to attach a number in their Voniz console.
    #   • `configured` — credentials + DID both present, ready to
    #     receive calls (pending the SIP terminator going live).
    status = "configured" if did else "pending_did"

    config: dict[str, Any] = {
        "provider":      provider,
        "alias":         alias,
        "username":      username,
        "registrar":     registrar,
        "remote_uri":    remote_uri,
        "inbound_uri":   inbound_uri_for(agent_id),
        "did":           did or None,
        "status":        status,
        "configured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verified_at":   (existing or {}).get("verified_at"),
    }
    if password:
        config["password"] = password
    return config, None


def redacted_view(stored: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Strip the password before returning to the frontend. The
    dashboard never displays the stored password — it shows
    'Stored securely · re-enter to change' instead. Mirrors what the
    Voniz console itself shows for their own password field."""
    if not stored or not isinstance(stored, dict):
        return None
    out = dict(stored)
    if out.get("password"):
        out["password"] = None
        out["password_set"] = True
    else:
        out["password_set"] = False
    return out
