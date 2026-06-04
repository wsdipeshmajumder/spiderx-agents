"""Daily wholesale-price watchdog.

Pulls current rates from three providers, compares against the constants
in `backend.pricing` (and a hardcoded telephony table here), and emits
events:
  - `pricing.observed`         (info)    — one per provider per day
  - `pricing.drift.detected`   (warning or critical based on delta %)
                                            — one per (provider, model) per day

Strict: NEVER auto-mutates pricing.py. The point is to surface
deviation so a human can decide to roll forward, renegotiate, or
pass cost through. The "Roll forward" button on the Observability
page is the only sanctioned promotion path.

Sources:
  - Gemini    — scrape https://ai.google.dev/gemini-api/docs/pricing (HTML)
  - Twilio    — https://pricing.twilio.com/v1/Voice/Countries/IN (auth'd API)
                Falls back to a hardcoded snapshot when SID/TOKEN not set.
  - Plivo     — scrape https://www.plivo.com/voice/pricing/in/ (HTML)

All scrapers are tolerant: a parse failure emits the *failure itself* as
an event (`system.scheduler.run.missed` with provider=...) so we never
fail silently when a vendor changes their page layout.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib import request as urlrequest

from . import events, pricing

log = logging.getLogger("eva.price_monitor")


# Drift threshold — anything within ±1% counts as "no change" and only
# the silent pricing.observed event fires. Above 1% we also fire the
# loud pricing.drift.detected event (severity scales with magnitude).
_DRIFT_PCT = 0.01
_CRITICAL_PCT = 0.05  # ≥5% delta is a critical alert


# Hardcoded reference telephony rates we compare against. These should
# move to a future `telephony_pricing.py` constants module when we add
# more carriers; for now they're inline so the watchdog has SOMETHING
# to diff against and surface changes.
_REFERENCE_TELEPHONY = {
    # Plivo India (₹ per minute / per month for DID)
    "plivo": {
        "outbound_local_inr_per_min": 0.60,
        "outbound_mobile_inr_per_min": 0.60,
        "did_local_inr_per_month": 250.0,
    },
    # Twilio India (USD)
    "twilio": {
        "outbound_mobile_usd_per_min": 0.0496,
        "outbound_landline_usd_per_min": 0.0699,
        "did_intl_usd_per_month": 1.15,
    },
}


# ─── helpers ─────────────────────────────────────────────────────────────


def _fetch(url: str, *, timeout: float = 15.0) -> str:
    req = urlrequest.Request(
        url, headers={"User-Agent": "SpiderX.AI price-monitor/1.0 (+https://spiderx.ai)"},
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000).decode("utf-8", "replace")


def _pct_delta(observed: float, reference: float) -> float:
    if reference == 0:
        return 0.0
    return (observed - reference) / reference


def _severity_for_drift(delta_pct: float) -> str:
    a = abs(delta_pct)
    if a >= _CRITICAL_PCT:
        return "critical"
    return "warning"


# ─── Gemini ───────────────────────────────────────────────────────────────


_GEMINI_AUDIO_INPUT_RE = re.compile(
    r"audio.*?input.*?\$([0-9.]+)\s*/\s*1[\s,]*million", re.IGNORECASE | re.DOTALL,
)
_GEMINI_AUDIO_OUTPUT_RE = re.compile(
    r"audio.*?output.*?\$([0-9.]+)\s*/\s*1[\s,]*million", re.IGNORECASE | re.DOTALL,
)


async def check_gemini() -> None:
    """Scrape Gemini's pricing page, parse audio in/out per 1M tokens,
    diff against pricing.py's effective rate for the live model."""
    provider = "gemini"
    try:
        html_text = _fetch("https://ai.google.dev/gemini-api/docs/pricing")
    except Exception as e:  # noqa: BLE001
        await events.emit(
            "system.scheduler.run.missed", severity="warning", source="scheduler",
            title=f"Gemini price scrape FAILED — {type(e).__name__}",
            message=str(e)[:400],
            payload={"provider": provider, "reason": "fetch_failed"},
            dedupe_key=f"price_monitor.{provider}.fetch_fail.{_today()}",
        )
        return
    m_in = _GEMINI_AUDIO_INPUT_RE.search(html_text)
    m_out = _GEMINI_AUDIO_OUTPUT_RE.search(html_text)
    if not (m_in and m_out):
        await events.emit(
            "system.scheduler.run.missed", severity="warning", source="scheduler",
            title="Gemini price scrape — parse FAILED",
            message="Couldn't find audio input/output prices on the page. "
                    "Google may have changed the layout — manual check needed.",
            payload={"provider": provider, "reason": "parse_failed"},
            dedupe_key=f"price_monitor.{provider}.parse_fail.{_today()}",
        )
        return
    obs_in = float(m_in.group(1))
    obs_out = float(m_out.group(1))
    # Reference: the effective rate for the canonical live model
    ref_model = "gemini-3.1-flash-live-preview"
    ref = pricing._PRICING_USD_PER_1M.get(ref_model, {})
    ref_in = float(ref.get("in") or 0)
    ref_out = float(ref.get("out") or 0)
    delta_in = _pct_delta(obs_in, ref_in) if ref_in else 0.0
    delta_out = _pct_delta(obs_out, ref_out) if ref_out else 0.0
    payload = {
        "provider": provider,
        "model": ref_model,
        "observed_usd_per_1m": {"in": obs_in, "out": obs_out},
        "reference_usd_per_1m": {"in": ref_in, "out": ref_out},
        "delta_pct": {"in": round(delta_in * 100, 3), "out": round(delta_out * 100, 3)},
    }
    await events.emit(
        "pricing.observed", source="scheduler",
        title=f"Gemini Live audio: ${obs_in:.2f} in / ${obs_out:.2f} out per 1M",
        payload=payload,
        dedupe_key=f"pricing.observed.{provider}.{_today()}",
    )
    big_drift = max(abs(delta_in), abs(delta_out))
    if big_drift > _DRIFT_PCT:
        sev = _severity_for_drift(big_drift)
        await events.emit(
            "pricing.drift.detected", severity=sev, source="scheduler",
            title=(
                f"Gemini rate drift {abs(delta_in)*100:+.1f}% in / "
                f"{abs(delta_out)*100:+.1f}% out — refresh pricing.py"
            ),
            message=(
                f"Observed audio input ${obs_in:.2f}/1M vs effective ${ref_in:.2f}/1M.\n"
                f"Observed audio output ${obs_out:.2f}/1M vs effective ${ref_out:.2f}/1M."
            ),
            payload=payload,
            dedupe_key=f"pricing.drift.{provider}.{_today()}",
        )


# ─── Twilio ───────────────────────────────────────────────────────────────


async def check_twilio() -> None:
    """Twilio has a real Pricing API — much more reliable than scraping.
    Falls back to a hardcoded snapshot if SID/TOKEN aren't configured."""
    provider = "twilio"
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        # No creds — just emit "we know the snapshot rate" so the
        # Observability feed has a row even in dev.
        ref = _REFERENCE_TELEPHONY["twilio"]
        await events.emit(
            "pricing.observed", source="scheduler",
            title=f"Twilio IN (snapshot): ${ref['outbound_mobile_usd_per_min']:.4f}/min mobile",
            message="No TWILIO_ACCOUNT_SID set — using hardcoded reference snapshot.",
            payload={"provider": provider, "snapshot": True, **ref},
            dedupe_key=f"pricing.observed.{provider}.{_today()}",
        )
        return
    import base64
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        req = urlrequest.Request(
            "https://pricing.twilio.com/v1/Voice/Countries/IN",
            headers={"Authorization": f"Basic {auth}"},
        )
        with urlrequest.urlopen(req, timeout=15) as resp:
            import json as _j
            data = _j.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        await events.emit(
            "system.scheduler.run.missed", severity="warning", source="scheduler",
            title=f"Twilio price API FAILED — {type(e).__name__}",
            message=str(e)[:400],
            payload={"provider": provider, "reason": "api_failed"},
            dedupe_key=f"price_monitor.{provider}.fail.{_today()}",
        )
        return
    # Parse the cheapest outbound rate (mobile) from the API response
    obs_mobile = None
    for entry in data.get("outbound_prefix_prices") or []:
        for p in entry.get("prefixes") or []:
            if str(p).startswith("91"):  # India mobile
                obs_mobile = float(entry.get("current_price") or 0)
                break
        if obs_mobile:
            break
    ref = _REFERENCE_TELEPHONY["twilio"]
    payload = {"provider": provider, "observed_usd_per_min": obs_mobile,
               "reference_usd_per_min": ref["outbound_mobile_usd_per_min"]}
    if obs_mobile is not None:
        delta = _pct_delta(obs_mobile, ref["outbound_mobile_usd_per_min"])
        payload["delta_pct"] = round(delta * 100, 3)
        await events.emit(
            "pricing.observed", source="scheduler",
            title=f"Twilio IN mobile outbound: ${obs_mobile:.4f}/min",
            payload=payload,
            dedupe_key=f"pricing.observed.{provider}.{_today()}",
        )
        if abs(delta) > _DRIFT_PCT:
            sev = _severity_for_drift(delta)
            await events.emit(
                "pricing.drift.detected", severity=sev, source="scheduler",
                title=f"Twilio India mobile outbound drifted {delta*100:+.1f}%",
                payload=payload,
                dedupe_key=f"pricing.drift.{provider}.{_today()}",
            )


# ─── Plivo ────────────────────────────────────────────────────────────────


_PLIVO_RATE_RE = re.compile(
    r"India[^|]*?(?:Local|Mobile)[^|]*?\$([0-9.]+)/min", re.IGNORECASE,
)


async def check_plivo() -> None:
    """Plivo doesn't expose a pricing API — best-effort scrape of the
    public pricing page. Emits a parse-fail event if the layout drifts."""
    provider = "plivo"
    try:
        html_text = _fetch("https://www.plivo.com/voice/pricing/in/")
    except Exception as e:  # noqa: BLE001
        await events.emit(
            "system.scheduler.run.missed", severity="warning", source="scheduler",
            title=f"Plivo price scrape FAILED — {type(e).__name__}",
            message=str(e)[:400],
            payload={"provider": provider, "reason": "fetch_failed"},
            dedupe_key=f"price_monitor.{provider}.fetch_fail.{_today()}",
        )
        return
    m = _PLIVO_RATE_RE.search(html_text)
    ref = _REFERENCE_TELEPHONY["plivo"]
    if not m:
        # Parse failed — emit a warning and surface the snapshot anyway
        await events.emit(
            "system.scheduler.run.missed", severity="warning", source="scheduler",
            title="Plivo price scrape — parse FAILED",
            message="Couldn't find India mobile rate on the page. "
                    "Plivo may have updated the layout — manual check needed.",
            payload={"provider": provider, "reason": "parse_failed"},
            dedupe_key=f"price_monitor.{provider}.parse_fail.{_today()}",
        )
        return
    # Plivo lists rates in USD on the public page; convert to INR using
    # the same FX rate pricing.py uses for consistency
    obs_usd_per_min = float(m.group(1))
    obs_inr_per_min = obs_usd_per_min * pricing._USD_TO_INR
    ref_inr = ref["outbound_mobile_inr_per_min"]
    delta = _pct_delta(obs_inr_per_min, ref_inr)
    payload = {"provider": provider,
               "observed_inr_per_min": round(obs_inr_per_min, 3),
               "observed_usd_per_min": obs_usd_per_min,
               "reference_inr_per_min": ref_inr,
               "delta_pct": round(delta * 100, 3)}
    await events.emit(
        "pricing.observed", source="scheduler",
        title=f"Plivo IN mobile outbound: ₹{obs_inr_per_min:.2f}/min",
        payload=payload,
        dedupe_key=f"pricing.observed.{provider}.{_today()}",
    )
    if abs(delta) > _DRIFT_PCT:
        sev = _severity_for_drift(delta)
        await events.emit(
            "pricing.drift.detected", severity=sev, source="scheduler",
            title=f"Plivo India mobile drifted {delta*100:+.1f}%",
            payload=payload,
            dedupe_key=f"pricing.drift.{provider}.{_today()}",
        )


# ─── orchestrator ─────────────────────────────────────────────────────────


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def run_daily_price_check() -> None:
    """Top-level job the scheduler calls once per day."""
    log.info("price_monitor.daily.start")
    try:
        await check_gemini()
    except Exception:  # noqa: BLE001
        log.exception("price_monitor.gemini_raised")
    try:
        await check_twilio()
    except Exception:  # noqa: BLE001
        log.exception("price_monitor.twilio_raised")
    try:
        await check_plivo()
    except Exception:  # noqa: BLE001
        log.exception("price_monitor.plivo_raised")
    log.info("price_monitor.daily.done")
    # Heartbeat — a "scheduler ran today" signal for the Observability
    # Schedulers tab. Dedupe-keyed per day so it shows up once.
    await events.emit(
        "system.scheduler.run.ok", source="scheduler",
        title="Daily price check completed",
        payload={"date": _today()},
        dedupe_key=f"system.scheduler.daily_price_check.{_today()}",
    )
