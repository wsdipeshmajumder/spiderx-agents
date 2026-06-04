"""Connector definitions + stub handlers.

Each connector exposes:
  * a Gemini `FunctionDeclaration` so the model can call it mid-conversation
  * a Python handler that returns a small JSON-serialisable payload

Stubs return plausible-looking data so the end-to-end function-call flow is
visible during testing. Replace the stub bodies with calls to your real CRM,
calendar, KB, SMS, etc. — no other plumbing changes required.
"""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib import request as urlrequest

from google.genai import types

log = logging.getLogger("eva.connectors")


# ───────────────────────────── declarations ─────────────────────────────────


def _schema(properties: dict, required: list[str] | None = None) -> types.Schema:
    return types.Schema(
        type=types.Type.OBJECT,
        properties=properties,
        required=required or [],
    )


def _str(desc: str) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=desc)


def _bool(desc: str) -> types.Schema:
    return types.Schema(type=types.Type.BOOLEAN, description=desc)


def _enum(desc: str, values: list[str]) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=desc, enum=values)


CONNECTOR_DECLS: dict[str, types.FunctionDeclaration] = {
    "http_webhook": types.FunctionDeclaration(
        name="http_webhook",
        description="POST a JSON payload to the configured webhook URL. Use only when no more-specific connector fits.",
        parameters=_schema(
            {
                "summary": _str("One-line summary of what's being sent."),
                "payload": types.Schema(
                    type=types.Type.OBJECT,
                    description="Arbitrary JSON payload (kept small).",
                    properties={},
                ),
            },
            required=["summary"],
        ),
    ),
    "calendar_check": types.FunctionDeclaration(
        name="calendar_check",
        description="Check available appointment slots on a given date. Returns up to 6 slots.",
        parameters=_schema(
            {
                "date": _str("ISO date, e.g. 2026-05-14. Resolve relative dates ('tomorrow') first."),
                "duration_min": types.Schema(type=types.Type.INTEGER, description="Slot duration in minutes (default 30)."),
            },
            required=["date"],
        ),
    ),
    "calendar_book": types.FunctionDeclaration(
        name="calendar_book",
        description="Book a previously-confirmed slot. Always call calendar_check first.",
        parameters=_schema(
            {
                "starts_at": _str("ISO datetime with timezone, e.g. 2026-05-14T10:30:00+05:30"),
                "duration_min": types.Schema(type=types.Type.INTEGER, description="Slot duration in minutes."),
                "name": _str("Caller's name."),
                "phone": _str("Caller's phone number."),
                "reason": _str("Short reason for the appointment."),
            },
            required=["starts_at", "name", "phone"],
        ),
    ),
    "crm_lookup": types.FunctionDeclaration(
        name="crm_lookup",
        description="Look up an existing customer by phone or email.",
        parameters=_schema(
            {
                "phone": _str("E.164 phone number, if known."),
                "email": _str("Email, if known."),
            },
        ),
    ),
    "crm_create_lead": types.FunctionDeclaration(
        name="crm_create_lead",
        description="Create a new lead/contact in the CRM.",
        parameters=_schema(
            {
                "name": _str("Full name."),
                "phone": _str("Phone (E.164 preferred)."),
                "email": _str("Email, if provided."),
                "interest": _str("Short description of what they're interested in."),
                "source": _str("Where this lead came from, e.g. 'inbound call'."),
            },
            required=["name", "interest"],
        ),
    ),
    "order_status": types.FunctionDeclaration(
        name="order_status",
        description="Look up the status of an order by order ID.",
        parameters=_schema(
            {"order_id": _str("Order identifier the caller gives you.")},
            required=["order_id"],
        ),
    ),
    "knowledge_base_search": types.FunctionDeclaration(
        name="knowledge_base_search",
        description="Search the company knowledge base / FAQ. Use to answer factual questions about the business.",
        parameters=_schema(
            {
                "query": _str("Search query in the caller's words."),
                "top_k": types.Schema(type=types.Type.INTEGER, description="Number of results (default 3)."),
            },
            required=["query"],
        ),
    ),
    "sms_send": types.FunctionDeclaration(
        name="sms_send",
        description="Send a short SMS to a number, e.g. an appointment confirmation.",
        parameters=_schema(
            {
                "to": _str("Phone number to send to."),
                "body": _str("SMS text body, under 320 chars."),
            },
            required=["to", "body"],
        ),
    ),
    "email_send": types.FunctionDeclaration(
        name="email_send",
        description="Send a short email, e.g. a confirmation.",
        parameters=_schema(
            {
                "to": _str("Recipient email."),
                "subject": _str("Email subject."),
                "body": _str("Email body, plain text."),
            },
            required=["to", "subject", "body"],
        ),
    ),
    "payment_link": types.FunctionDeclaration(
        name="payment_link",
        description="Generate a hosted payment link to send to the caller.",
        parameters=_schema(
            {
                "amount": types.Schema(type=types.Type.NUMBER, description="Amount in major currency units."),
                "currency": _enum("ISO 4217 currency code.", ["INR", "USD", "EUR", "GBP", "AED"]),
                "purpose": _str("What the payment is for."),
            },
            required=["amount", "currency"],
        ),
    ),
    "end_call": types.FunctionDeclaration(
        name="end_call",
        description=(
            "Wrap up the call. Call this ONCE at the very end, after you've "
            "said your goodbye line. It classifies the call into a canonical "
            "outcome, captures a 1-2 sentence summary and any structured data "
            "you collected, and persists a call record for analytics + fires "
            "the agent's webhook if one is configured. After end_call you may "
            "say one closing line (\"Take care!\") then stop talking."
        ),
        parameters=_schema(
            {
                "outcome": _str(
                    "Canonical outcome ID from the agent's outcomes list "
                    "(e.g. 'booked', 'qualified', 'not_interested', 'voicemail'). "
                    "Pick the single best fit."
                ),
                "reason": _enum(
                    "Why the call ended.",
                    [
                        "CONVERSATION_COMPLETE",
                        "USER_REQUESTED",
                        "VOICEMAIL_DETECTED",
                        "WRONG_NUMBER",
                        "ESCALATED_TO_HUMAN",
                        "ABANDONED",
                    ],
                ),
                "summary": _str(
                    "1-2 sentence recap of what happened (factual, no fluff)."
                ),
                "final_message": _str(
                    "Optional: the closing line you'll say after end_call. "
                    "Just for analytics — you still need to actually say it."
                ),
                "extracted": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "Structured fields you captured during the call. Free "
                        "shape — typically caller name, phone, appointment "
                        "time, amount, order id, etc."
                    ),
                    properties={},
                ),
                # ── per-call qualitative signals ──────────────────────────
                # Used by the call-log UI to colour rows + power "hot
                # leads" filters. The model assesses these honestly: an
                # information-only caller is 'cold', a "send me the
                # quote and I'll sign tomorrow" caller is 'hot'.
                "sentiment": _enum(
                    "Caller's emotional tone over the call. 'mixed' is for "
                    "calls that started one way and ended another (e.g. "
                    "frustrated → satisfied after resolution).",
                    ["positive", "neutral", "negative", "mixed"],
                ),
                "lead_quality": _enum(
                    "Lead temperature based on intent + readiness. "
                    "'hot' = ready to act now (book, buy, escalate). "
                    "'warm' = serious interest, needs follow-up. "
                    "'cold' = information-only, no clear intent. "
                    "'na' = wasn't a lead context (support, complaint, info).",
                    ["hot", "warm", "cold", "na"],
                ),
                "lead_signals": _str(
                    "One short sentence capturing the signals that drove "
                    "your lead_quality call. E.g. 'Asked about pricing + "
                    "urgent timeline, ready to book test drive.'"
                ),
            },
            required=["outcome", "reason", "summary"],
        ),
    ),
}


def build_tools(connector_ids: list[str]) -> list[types.Tool]:
    """Return a single types.Tool containing the requested declarations."""
    decls = [CONNECTOR_DECLS[c] for c in connector_ids if c in CONNECTOR_DECLS]
    if not decls:
        return []
    return [types.Tool(function_declarations=decls)]


def label_for(connector_id: str) -> str:
    decl = CONNECTOR_DECLS.get(connector_id)
    return decl.description if decl else connector_id


# ─────────────────────────────── handlers ───────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gen_slots(date_str: str, duration_min: int = 30) -> list[dict[str, Any]]:
    try:
        d = datetime.fromisoformat(date_str).date()
    except ValueError:
        d = datetime.now(timezone.utc).date()
    base = datetime(d.year, d.month, d.day, 9, 0)
    slots = []
    for i in range(8):
        start = base + timedelta(minutes=(duration_min + 30) * i)
        if start.hour >= 18:
            break
        if random.random() < 0.6:
            slots.append({
                "starts_at": start.isoformat() + "+00:00",
                "duration_min": duration_min,
            })
        if len(slots) >= 6:
            break
    return slots


def _post_webhook(url: str, payload: dict[str, Any], headers: dict | None = None) -> dict[str, Any]:
    try:
        data = json.dumps(payload).encode("utf-8")
        all_headers = {"Content-Type": "application/json"}
        if headers:
            for k, v in headers.items():
                if k and v:
                    all_headers[str(k)] = str(v)
        req = urlrequest.Request(url, data=data, headers=all_headers)
        with urlrequest.urlopen(req, timeout=8) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return {"status": resp.status, "body": body[:500]}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


async def _fire_post_call_notifications(*, agent: dict[str, Any],
                                          record: dict[str, Any], call_id: int | None,
                                          post_call: dict[str, Any]) -> None:
    """Email + (plan-gated) SMS summary to the org's owners. Caller
    has already short-circuited on "no channel enabled" so we know at
    least one of email/sms is true here.

    Recipient resolution: the agent's org owners are the natural
    audience — that's who pays + who needs to act on a hot lead. If
    the org has no owner (shouldn't happen post-Phase-2-backfill),
    fall back to the founder so a dev environment still sees something.
    SMS recipient comes from variables.phone if set.
    """
    from . import db, email_stub
    org_id = agent.get("org_id")
    if not org_id:
        return
    members = await db.list_org_members(org_id)
    owners = [m for m in members if m.get("role") == "owner"] or members
    if not owners:
        return
    org = None
    try:
        # Any member's user_id works — get_org_for_user resolves via JOIN.
        org = await db.get_org_for_user(owners[0]["user_id"])
    except Exception:
        pass
    org_name = (org or {}).get("name") or "your team"
    agent_name = agent.get("name") or "Agent"

    # Plan-gating: SMS requires a paid tier. We check the org owner's
    # plan state — single-tenant assumption (every member in an org
    # shares the same plan). If anyone's on a paid plan we honour SMS.
    sms_enabled = bool(post_call.get("sms"))
    if sms_enabled:
        try:
            paid = False
            for m in owners[:3]:  # cap lookups
                state = await db.get_user_plan_state(m["user_id"])
                slug = ((state or {}).get("plan") or {}).get("slug")
                if slug and slug != "free":
                    paid = True
                    break
            sms_enabled = paid
        except Exception:
            sms_enabled = False

    common = {
        "agent_name": agent_name,
        "outcome":      record.get("outcome"),
        "sentiment":    record.get("sentiment"),
        "lead_quality": record.get("lead_quality"),
        "duration_s":   record.get("duration_s") or 0,
    }
    # Build 196: parse the JSON-serialised transcript back to turns so
    # the HTML email can render chat-style bubbles like the dashboard.
    import json as _json
    raw_tx = record.get("transcript")
    transcript_turns = None
    if isinstance(raw_tx, str) and raw_tx.strip():
        try:
            parsed = _json.loads(raw_tx)
            if isinstance(parsed, list):
                transcript_turns = [t for t in parsed if isinstance(t, dict) and t.get("text")]
        except Exception:  # noqa: BLE001
            transcript_turns = None
    rich = {
        "lead_signals":     record.get("lead_signals"),
        "summary":          record.get("summary"),
        "extracted":        record.get("extracted"),
        "call_id":          call_id,
        "agent_slug":       agent.get("slug"),
        "transcript_turns": transcript_turns,
        "started_at_iso":   record.get("started_at"),
    }
    if post_call.get("email"):
        for m in owners:
            try:
                await email_stub.send_call_summary_email(
                    to=m["email"], org_name=org_name, **common, **rich,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("post_call.email failed for %s: %s", m.get("email"), e)
    # Build 196: ALSO fire the report to the fixed REPORT_EMAIL_TO
    # recipient (devteam@spiderx.ai by default) regardless of the
    # operator's post_call.email toggle. Useful for ops visibility on
    # every persisted call. No-op when REPORT_EMAIL_TO is unset.
    try:
        await email_stub.send_call_report_to_devteam(
            org_name=org_name, **common, **rich,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("post_call.devteam_email failed: %s", e)

    # Build 198: emit call lifecycle event for the Observability feed.
    # Severity tracks the outcome kind so the feed colour-codes wins vs
    # failures naturally — booking_made/resolved is info, voicemail/
    # abandoned is warning. Operators see lifecycle on the same stream
    # as system events.
    try:
        from . import events as _ev
        from . import call_outcomes as _co
        outcome_id = (record.get("outcome") or "").lower()
        kind_lookup = {c["id"]: c["kind"] for c in _co.catalogue_for(agent) if isinstance(c, dict)}
        outcome_kind = kind_lookup.get(outcome_id, "info")
        event_kind = "call.abandoned" if outcome_id == "abandoned" else "call.completed"
        event_sev = "warning" if outcome_kind == "failure" else "info"
        await _ev.emit(
            event_kind, source="system", severity=event_sev,
            org_id=agent.get("org_id"), agent_id=agent.get("id"),
            title=(
                f"{agent_name} — {outcome_id.replace('_', ' ') or 'call ended'} "
                f"({(record.get('duration_s') or 0):.0f}s)"
            ),
            payload={
                "call_id": call_id,
                "outcome": outcome_id,
                "outcome_kind": outcome_kind,
                "sentiment": record.get("sentiment"),
                "lead_quality": record.get("lead_quality"),
                "duration_s": record.get("duration_s"),
            },
        )
    except Exception as _e:  # noqa: BLE001
        log.debug("events.emit call.* failed: %s", _e)
    if sms_enabled:
        # SMS recipient: prefer `notification_phone` (dedicated post-call
        # SMS line, often a personal/WhatsApp number) over the generic
        # `phone` field (which doubles as the caller-facing escalation
        # number). Legacy agents predating the split fall back to `phone`.
        # `common` carries email-shaped kwargs; SMS takes a narrower
        # subset (no sentiment — too long for the 160-char SMS budget).
        v = agent.get("variables") or {}
        phone = (v.get("notification_phone") or v.get("phone") or "").strip()
        if phone:
            try:
                await email_stub.send_call_summary_sms(
                    to=phone,
                    agent_name=common["agent_name"],
                    outcome=common["outcome"],
                    lead_quality=common["lead_quality"],
                    duration_s=common["duration_s"],
                )
            except Exception as e:  # noqa: BLE001
                log.warning("post_call.sms failed: %s", e)


async def handle(connector_id: str, args: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call to the correct stub. Always returns a dict; never raises."""
    try:
        if connector_id == "end_call":
            # Tier 3: structured call closure. Persist a call record so the
            # cockpit can render analytics, then fire the agent's webhook
            # if one is configured (fire-and-forget — we don't block).
            from . import db
            outcome = (args.get("outcome") or "").strip() or None
            reason = (args.get("reason") or "").strip() or None
            summary = (args.get("summary") or "").strip() or None
            final = (args.get("final_message") or "").strip() or None
            extracted = args.get("extracted") if isinstance(args.get("extracted"), dict) else None

            # Disconnect-safety: if this call is very young AND the outcome is
            # "imprecise" (e.g. not_interested fired in the first 5s), reject
            # the end_call so the agent has to either pick a clearer outcome
            # or wait. Mirrors the Vani approach but driven by Eva's silent
            # policy on the agent — not a knob the user touches.
            policy = (agent.get("policy") or {}).get("disconnect_safety") or {}
            if policy.get("enabled") and outcome:
                imprecise = set(policy.get("imprecise_outcomes") or [])
                # `started_at` is stamped on the agent dict by the bridge when
                # the session opens; if missing we can't enforce age, so let
                # it through.
                started_at = agent.get("_call_started_at")
                if started_at and outcome in imprecise:
                    age_s = max(0.0, time.monotonic() - float(started_at))
                    min_age = float(policy.get("min_call_age_s") or 10)
                    if age_s < min_age:
                        return {
                            "ok": False,
                            "rejected": True,
                            "reason": "call too young for an imprecise outcome",
                            "min_call_age_s": min_age,
                            "age_s": round(age_s, 1),
                            "advice": (
                                "Pick a more confident outcome, or keep the "
                                "call going for at least a few more seconds."
                            ),
                        }

            # Per-call qualitative signals (Phase 8). The model is required
            # to provide these honestly — wrong calls (e.g. tagging an
            # information-only inquiry as 'hot') break the lead-quality
            # dashboard. The system-prompt instructs the assessment.
            sentiment = args.get("sentiment")
            lead_quality = args.get("lead_quality")
            lead_signals = (args.get("lead_signals") or "").strip() or None
            # Token + model fields are populated by gemini_bridge.py when it
            # observes the Gemini Live usage_metadata events; we stash them on
            # the agent dict as `_tokens_in / _tokens_out / _model_id` so we
            # pick them up here at end_call time. Missing → 0 (db_pg writes
            # NULL into the calls row but credits 0 to the rollup).
            record = {
                "agent_id": agent.get("id"),
                "started_at": agent.get("_call_started_iso") or _now_iso(),
                "ended_at": _now_iso(),
                "duration_s": (
                    (time.monotonic() - float(agent["_call_started_at"]))
                    if agent.get("_call_started_at") else 0
                ),
                "outcome": outcome,
                "reason": reason,
                "summary": summary,
                "final_message": final,
                "extracted": extracted,
                # Bridge stashes the in-session transcript turns on agent
                # ["_transcript"] right before calling end_call (build 188).
                # Pre-188 this was always None; the calls.transcript column
                # is TEXT so we serialise the JSON shape — the dashboard's
                # CallDetailModal parses it back to render chat bubbles.
                "transcript": (
                    json.dumps(agent.get("_transcript"), ensure_ascii=False)
                    if isinstance(agent.get("_transcript"), list) and agent.get("_transcript")
                    else None
                ),
                "input_tokens":  agent.get("_tokens_in"),
                "output_tokens": agent.get("_tokens_out"),
                "cached_tokens": agent.get("_tokens_cached"),
                "model_id":      agent.get("_model_id"),
                "sentiment":     sentiment,
                "lead_quality":  lead_quality,
                "lead_signals":  lead_signals,
                # Build 206 — recording-started marker. Filled below from the
                # writer's actual open time. We pre-compute `recording_started_at`
                # here so insert_call can stamp `recording_expires_at` (+180d)
                # in the same INSERT, keeping retention math co-located.
                "recording_started_at": agent.get("_recording_started_iso"),
            }
            # Build 206 — finalize the call recording writer (if one was
            # opened on session start). We do this BEFORE insert_call so
            # the writer's path/size metadata can land in the same row.
            # The writer doesn't know the real calls.id yet — pass None
            # and rename later from the temp-token dir.
            writer = agent.get("_recording_writer")
            if writer is not None:
                try:
                    meta = writer.finalize(call_id=None)
                    record["recording_path"]       = meta.get("recording_path")
                    record["recording_format"]     = meta.get("recording_format")
                    record["recording_size_bytes"] = meta.get("recording_size_bytes")
                    if meta.get("recording_started_at"):
                        record["recording_started_at"] = (
                            meta["recording_started_at"].isoformat()
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("end_call: recording finalize failed: %s", e)
            call_id = None
            try:
                # db.insert_call became async at the Phase-1 cutover; await it
                # so the rollup UPSERTs land atomically with the calls row.
                call_id = await db.insert_call(record)
            except Exception as e:  # noqa: BLE001
                log.exception("end_call: db.insert_call failed: %s", e)
            # Build 206 — now that we have the real call_id, rename the
            # recording directory from the temp token to call_id form so
            # the on-disk layout matches the DB row. Best-effort.
            if writer is not None and call_id and record.get("recording_path"):
                try:
                    from . import recordings as _rec
                    old_rel = record["recording_path"]
                    new_rel = _rec.relative_path_for(int(agent["id"]), int(call_id))
                    old_abs = _rec.RECORDING_ROOT / old_rel
                    new_abs = _rec.RECORDING_ROOT / new_rel
                    if old_abs.exists() and not new_abs.exists():
                        new_abs.parent.mkdir(parents=True, exist_ok=True)
                        old_abs.rename(new_abs)
                        # Update the freshly-inserted row to point at the
                        # final path. The audio files are now under the
                        # canonical layout the daily purge job scans.
                        await db.update_call_recording_path(int(call_id), new_rel)
                except Exception as e:  # noqa: BLE001
                    log.warning("end_call: recording rename failed: %s", e)

            # Fire the agent's webhook (optional, fire-and-forget).
            webhook_url = agent.get("webhook_url")
            if webhook_url:
                hdrs = agent.get("webhook_headers") or {}
                payload = {
                    "event": "call.ended",
                    "agent_id": agent.get("id"),
                    "agent_name": agent.get("name"),
                    "call_id": call_id,
                    "outcome": outcome,
                    "reason": reason,
                    "summary": summary,
                    "final_message": final,
                    "extracted": extracted,
                    "started_at": record["started_at"],
                    "ended_at": record["ended_at"],
                    "duration_s": record["duration_s"],
                }
                try:
                    _post_webhook(webhook_url, payload, headers=hdrs)
                except Exception:  # noqa: BLE001
                    pass

            # Post-call notifications. Owner email + SMS are driven by
            # agent.purpose.post_call (email universal, SMS plan-gated).
            # The fixed-recipient devteam report (build 196) ALWAYS fires
            # — that's wired inside _fire_post_call_notifications so we
            # call it unconditionally and let the inner branches decide.
            # All best-effort: a provider hiccup never poisons the calls
            # row that just landed.
            try:
                purpose = agent.get("purpose") or {}
                post_call = purpose.get("post_call") if isinstance(purpose, dict) else {}
                if not isinstance(post_call, dict):
                    post_call = {}
                await _fire_post_call_notifications(
                    agent=agent, record=record, call_id=call_id,
                    post_call=post_call,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("end_call: post_call notifications failed: %s", e)

            return {"ok": True, "call_id": call_id, "outcome": outcome}

        if connector_id == "http_webhook":
            url = os.environ.get("WEBHOOK_URL") or (agent.get("sip_config") or {}).get("webhook_url")
            if not url:
                return {"ok": True, "note": "webhook URL not configured; payload accepted but not sent.", "echo": args}
            return _post_webhook(url, args.get("payload") or args)

        if connector_id == "calendar_check":
            slots = _gen_slots(args.get("date") or "", int(args.get("duration_min") or 30))
            return {"ok": True, "date": args.get("date"), "slots": slots}

        if connector_id == "calendar_book":
            return {
                "ok": True,
                "booking_id": "BK-" + secrets.token_hex(4).upper(),
                "starts_at": args.get("starts_at"),
                "confirmation_sent": True,
            }

        if connector_id == "crm_lookup":
            phone = args.get("phone")
            email = args.get("email")
            if not phone and not email:
                return {"ok": False, "found": False, "note": "phone or email required"}
            return {
                "ok": True,
                "found": True,
                "customer": {
                    "id": "CUS-" + secrets.token_hex(3).upper(),
                    "name": "Asha Mehta" if random.random() < 0.5 else "Rahul Iyer",
                    "phone": phone,
                    "email": email,
                    "tier": random.choice(["standard", "gold", "platinum"]),
                    "last_seen": _now_iso(),
                },
            }

        if connector_id == "crm_create_lead":
            return {
                "ok": True,
                "lead_id": "LEAD-" + secrets.token_hex(3).upper(),
                "created_at": _now_iso(),
                "echo": args,
            }

        if connector_id == "order_status":
            statuses = ["confirmed", "packed", "out for delivery", "delivered"]
            return {
                "ok": True,
                "order_id": args.get("order_id"),
                "status": random.choice(statuses),
                "eta": (datetime.now(timezone.utc) + timedelta(days=random.randint(0, 3))).date().isoformat(),
            }

        if connector_id == "knowledge_base_search":
            q = args.get("query", "")
            sector = agent.get("sector") or "generic"
            return {
                "ok": True,
                "query": q,
                "results": [
                    {"title": f"{sector.title()} FAQ — {q[:40]}", "snippet": "Hours: Mon–Sat, 9am–6pm. Closed on national holidays."},
                    {"title": "Payment & cancellation", "snippet": "Full refund up to 24h before the appointment."},
                ],
            }

        if connector_id == "sms_send":
            return {"ok": True, "to": args.get("to"), "sent_at": _now_iso(), "provider_id": "SM-" + secrets.token_hex(3)}

        if connector_id == "email_send":
            return {"ok": True, "to": args.get("to"), "subject": args.get("subject"), "sent_at": _now_iso()}

        if connector_id == "payment_link":
            return {
                "ok": True,
                "url": f"https://pay.spiderx.ai/{secrets.token_hex(6)}",
                "amount": args.get("amount"),
                "currency": args.get("currency"),
                "expires_in_minutes": 60,
            }
    except Exception as e:  # noqa: BLE001
        log.exception("connector %s failed", connector_id)
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown connector {connector_id}"}


ConnectorHandler = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
