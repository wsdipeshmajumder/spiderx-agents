"""Deterministic build templates — the "Eva is the mother, agents are her
children" engine.

Eva used to be probabilistic: a ~30 KB system prompt encoded "for a
dental clinic ask about bookings; for retail ask about returns" and we
trusted the LLM to follow it. As the matrix grew (industry × locale ×
city), drift compounded. This module replaces that with a deterministic
flow:

  1. Triage — Eva asks 2-3 questions to identify the operator's
     (industry × sub_industry × locale × city) tuple.
  2. The server matches a YAML template from `backend/build_templates/`
     based on those facets (with a "most specific wins" inheritance
     chain — city > locale > industry > _generic).
  3. Eva walks the operator through the template's `questions:` list
     IN ORDER. Each answer is captured via the `record_template_answer`
     tool and validated server-side.
  4. `save_agent` composes the final agent from the template's
     `agent_profile:` skeleton + the answered slot values.

Key behaviours implemented here:

  • `load_all()` — at startup, walks the directory, parses every YAML,
    resolves inheritance into a flat "effective" view per template,
    builds keyword indexes for fast matching. Fails fast on bad YAML.

  • `find_best_match(industry, sub_industry, locale, city)` — runs the
    resolution chain. Returns the most specific template that matches,
    or None (caller falls back to today's probabilistic flow).

  • `validate_answer(question, raw_value)` — type-aware coercion:
    text / text_list / enum / bool / phone / email.

  • `compose_save_args(template, answers)` — substitutes
    `{{slot_name}}` placeholders in the agent_profile's greeting /
    persona / system_prompt with the captured values, slots into the
    save_agent payload shape, returns a dict ready for db.create_agent.

  • `triage_match(industry_text, ...) → (industry_id, sub_industry_id,
    city_id)` — keyword classifies the operator's free-text triage
    answers into canonical facet ids using the templates' own
    `matches:` keyword lists. No external classifier; the templates ARE
    the classifier.

Templates are loaded once at process start and held in memory. They're
small (a few KB each) — no caching layer needed.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("eva.build_templates")


# Templates directory — relative to THIS file so it works from any cwd.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "build_templates"


# Question types supported by the deterministic interview. Each maps
# to a validator in `validate_answer`. Adding a new type requires:
#   1. Adding the validator branch below
#   2. Updating the runtime tool decl's description so Eva knows
#      what shape to send.
_QUESTION_TYPES = {"text", "text_list", "enum", "bool", "phone", "email", "url"}


# Affirmative tokens for the `bool` question type. Reused from
# build_state.py spirit but inlined to avoid an import cycle (this
# module is imported by gemini_bridge AND by build_state's render).
_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|okay|ok|alright|absolutely|"
    r"haan|han|ji|theek|accha|achha|chalega|true|t|1)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|nah|not|never|don'?t|false|f|0)\b",
    re.IGNORECASE,
)

# Loose E.164-ish phone validator. Stricter form lives in sip_config.py;
# this one is permissive on purpose (operators paste in many shapes).
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_URL_RE = re.compile(r"^https?://\S+$|^[a-z0-9.-]+\.[a-z]{2,}(/\S*)?$", re.IGNORECASE)


# In-memory registry, populated by load_all() at startup.
_REGISTRY: dict[str, dict[str, Any]] = {}
_RESOLVED: dict[str, dict[str, Any]] = {}  # id → effective (inheritance-flattened) template


# ─── loader ──────────────────────────────────────────────────────────────


def load_all(strict: bool = False) -> dict[str, dict[str, Any]]:
    """Walk _TEMPLATES_DIR, parse every .yaml, populate _REGISTRY +
    _RESOLVED. Returns the resolved registry.

    `strict=True` raises on the first bad template. Default false:
    bad templates log a warning and are skipped so a broken file
    doesn't take down the whole build flow.
    """
    import yaml  # local — keeps pyyaml off the import path of code that doesn't need it

    _REGISTRY.clear()
    _RESOLVED.clear()

    if not _TEMPLATES_DIR.is_dir():
        log.warning("build_templates: directory %s does not exist", _TEMPLATES_DIR)
        return _RESOLVED

    found = 0
    failed = 0
    for path in sorted(_TEMPLATES_DIR.rglob("*.yaml")):
        try:
            with path.open() as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: top-level must be a mapping")
            _validate_template_shape(raw, path)
            tid = raw["id"]
            if tid in _REGISTRY:
                raise ValueError(f"{path}: duplicate template id {tid!r} (already loaded from {_REGISTRY[tid]['_path']})")
            raw["_path"] = str(path.relative_to(_TEMPLATES_DIR.parent))
            _REGISTRY[tid] = raw
            found += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.error("build_templates: failed to load %s: %s", path, e)
            if strict:
                raise

    # Second pass: resolve inheritance for every template. Done after
    # the first pass so children can reference parents loaded later.
    for tid, raw in _REGISTRY.items():
        try:
            _RESOLVED[tid] = _resolve_inheritance(tid, set())
        except Exception as e:  # noqa: BLE001
            log.error("build_templates: failed to resolve %s: %s", tid, e)
            if strict:
                raise

    log.info(
        "build_templates: loaded %d templates (%d failed) from %s",
        found, failed, _TEMPLATES_DIR,
    )
    return _RESOLVED


def _validate_template_shape(t: dict[str, Any], path: Path) -> None:
    """Schema sanity check. Raises ValueError with a useful message on
    any malformed field. Catches typos at startup, not at first user
    request."""
    if not isinstance(t.get("id"), str) or not t["id"]:
        raise ValueError(f"{path}: missing required string field 'id'")
    if "facets" in t and not isinstance(t["facets"], dict):
        raise ValueError(f"{path}: 'facets' must be a mapping")
    if "questions" in t:
        if not isinstance(t["questions"], list):
            raise ValueError(f"{path}: 'questions' must be a list")
        seen_ids: set[str] = set()
        for i, q in enumerate(t["questions"]):
            if not isinstance(q, dict):
                raise ValueError(f"{path}: questions[{i}] must be a mapping")
            for req in ("id", "prompt", "slot", "type"):
                if not q.get(req):
                    raise ValueError(f"{path}: questions[{i}] missing '{req}'")
            if q["type"] not in _QUESTION_TYPES:
                raise ValueError(f"{path}: questions[{i}].type {q['type']!r} unknown (allowed: {sorted(_QUESTION_TYPES)})")
            if q["type"] == "enum" and not q.get("options"):
                raise ValueError(f"{path}: questions[{i}] type=enum requires non-empty 'options' list")
            if q["id"] in seen_ids:
                raise ValueError(f"{path}: duplicate question id {q['id']!r}")
            seen_ids.add(q["id"])
    if "questions_append" in t:
        if not isinstance(t["questions_append"], list):
            raise ValueError(f"{path}: 'questions_append' must be a list")
    if "agent_profile" in t and not isinstance(t["agent_profile"], dict):
        raise ValueError(f"{path}: 'agent_profile' must be a mapping")


def _resolve_inheritance(tid: str, seen: set[str]) -> dict[str, Any]:
    """Flatten a template's inheritance chain into a single effective
    view. Children override scalar fields; questions are appended via
    `questions_append`; agent_profile dicts deep-merge with child winning."""
    if tid in seen:
        raise ValueError(f"build_templates: cycle in inheritance involving {tid!r}")
    if tid not in _REGISTRY:
        raise ValueError(f"build_templates: unknown parent template id {tid!r}")
    seen = seen | {tid}
    t = _REGISTRY[tid]
    parent_id = t.get("inherits")
    if not parent_id:
        # Root — return a deep copy so callers can mutate freely.
        return _deep_copy(t)

    base = _resolve_inheritance(parent_id, seen)
    out = _deep_copy(base)

    # Scalar overrides: every top-level non-special field in the child
    # replaces the parent's value.
    SPECIAL = {"id", "_path", "schema_version", "inherits",
               "questions_append", "agent_profile_overrides", "matches"}
    for k, v in t.items():
        if k in SPECIAL:
            continue
        out[k] = _deep_copy(v)
    out["id"] = t["id"]
    out["_path"] = t.get("_path", out.get("_path"))
    out["schema_version"] = t.get("schema_version", out.get("schema_version"))

    # `matches`: UNION the keyword lists from parent + child (rather
    # than overriding) so a city template can ADD city keywords
    # without losing the parent's industry keywords.
    if t.get("matches"):
        cm = t["matches"]
        bm = out.setdefault("matches", {})
        for key, vals in cm.items():
            if isinstance(vals, list):
                merged = list(bm.get(key, []))
                for x in vals:
                    if x not in merged:
                        merged.append(x)
                bm[key] = merged
            else:
                bm[key] = vals

    # `questions_append`: append to the parent's question list.
    if t.get("questions_append"):
        out_qs = list(out.get("questions", []))
        existing_ids = {q["id"] for q in out_qs}
        for q in t["questions_append"]:
            if q["id"] in existing_ids:
                # Child wants to OVERRIDE an inherited question of the
                # same id rather than append a duplicate.
                out_qs = [q if existing.get("id") == q["id"] else existing for existing in out_qs]
            else:
                out_qs.append(q)
                existing_ids.add(q["id"])
        out["questions"] = out_qs

    # `agent_profile_overrides`: deep-merge into the inherited
    # agent_profile, child wins.
    if t.get("agent_profile_overrides"):
        ap = dict(out.get("agent_profile") or {})
        for k, v in t["agent_profile_overrides"].items():
            if isinstance(v, dict) and isinstance(ap.get(k), dict):
                ap[k] = {**ap[k], **v}
            else:
                ap[k] = _deep_copy(v)
        out["agent_profile"] = ap

    return out


def _deep_copy(v: Any) -> Any:
    """Cheap deep copy for plain JSON-shaped data. Avoids importing
    copy.deepcopy at module top (small perf win)."""
    if isinstance(v, dict):
        return {k: _deep_copy(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_deep_copy(x) for x in v]
    return v


# ─── matcher ─────────────────────────────────────────────────────────────


def find_best_match(
    *,
    industry: Optional[str] = None,
    sub_industry: Optional[str] = None,
    locale: Optional[str] = None,
    country: Optional[str] = None,
    city: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the most specific template that matches the given facets,
    or None if nothing matches and no `_generic` template is loaded.

    Match score: more specific facets matched = higher score. Templates
    must match every NON-WILDCARD facet they declare.
    """
    if not _RESOLVED:
        return None
    best: Optional[dict[str, Any]] = None
    best_score = -1
    for tid, t in _RESOLVED.items():
        facets = t.get("facets") or {}
        score = _score_match(
            facets,
            industry=industry,
            sub_industry=sub_industry,
            locale=locale,
            city=city,
        )
        if score < 0:
            continue
        if score > best_score:
            best_score = score
            best = t
    # Fall back to _generic if nothing scored.
    if best is None and "_generic" in _RESOLVED:
        best = _RESOLVED["_generic"]
    return best


def match_by_industry(
    industry: str,
    *,
    locale: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve the best LOCALE-tolerant template for a known industry,
    ignoring the strict locale gate `find_best_match` enforces.

    Used by the homepage industry preset (`/for-<industry>` + the
    dropdown). When the operator has explicitly told us the industry up
    front, we want to lock that industry's template even if our only
    coverage for it is a different locale than the browser's (today most
    templates are en-IN). `find_best_match` would reject those because it
    treats a declared-but-mismatched locale facet as incompatible; here we
    instead PREFER a locale match but gracefully fall back to any locale
    variant for the same industry.

    Resolution within an industry:
      1. city-less template whose locale == requested locale  (best)
      2. city-less template whose locale == en-IN             (our default)
      3. any city-less template for the industry              (first by id)
    City-specific templates are never preset — the city is discovered
    during the interview, not chosen on the landing page.

    Returns None when no template declares this industry, so the caller
    falls back to the generic / probabilistic flow.
    """
    if not _RESOLVED or not industry:
        return None
    want = str(industry).strip().lower()
    loc = (locale or "").strip().lower() or None
    candidates: list[dict[str, Any]] = []
    for tid, t in _RESOLVED.items():
        facets = t.get("facets") or {}
        ind = facets.get("industry")
        if ind in (None, "*"):
            continue
        if str(ind).lower() != want:
            continue
        # Skip city-specific templates — the preset locks industry only.
        if facets.get("city") not in (None, "*"):
            continue
        candidates.append(t)
    if not candidates:
        return None

    def _rank(t: dict[str, Any]) -> tuple[int, str]:
        f = t.get("facets") or {}
        tloc = str(f.get("locale") or "").lower()
        if loc and tloc == loc:
            tier = 0
        elif tloc == "en-in":
            tier = 1
        else:
            tier = 2
        return (tier, t.get("id") or "")

    candidates.sort(key=_rank)
    return candidates[0]


def _score_match(facets: dict[str, Any], *,
                 industry: Optional[str],
                 sub_industry: Optional[str],
                 locale: Optional[str],
                 city: Optional[str]) -> int:
    """Return a non-negative score (higher = more specific match), or
    -1 if the template doesn't match. Wildcard facets (`"*"`) match
    anything but contribute 0 to the score."""
    score = 0
    pairs = [
        ("industry", industry, 1),
        ("sub_industry", sub_industry, 2),
        ("locale", locale, 4),
        ("city", city, 8),
    ]
    for key, value, weight in pairs:
        decl = facets.get(key)
        if decl in (None, "*"):
            continue
        if value is None:
            # Template declares this facet but caller didn't supply it →
            # incompatible.
            return -1
        if str(decl).lower() != str(value).lower():
            return -1
        score += weight
    return score


def triage_classify(
    *,
    industry_text: Optional[str] = None,
    sub_industry_text: Optional[str] = None,
    city_text: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Classify free-text triage answers into canonical facet ids.

    Uses the keywords each template declares in its `matches:` block.
    First-match wins. Returns a dict with optional keys: industry,
    sub_industry, city. None for fields we couldn't classify.

    Doesn't classify locale — that's derived from the operator's
    locale settings + country profile, not from a question answer.
    """
    out: dict[str, Optional[str]] = {
        "industry": None,
        "sub_industry": None,
        "city": None,
    }
    if not _RESOLVED:
        return out

    def _norm(s: Optional[str]) -> str:
        return (s or "").lower()

    industry_norm = _norm(industry_text)
    sub_norm = _norm(sub_industry_text)
    city_norm = _norm(city_text)

    for tid, t in _RESOLVED.items():
        facets = t.get("facets") or {}
        matches = t.get("matches") or {}
        # Industry
        if out["industry"] is None and industry_norm and facets.get("industry") not in (None, "*"):
            keywords = matches.get("industry_keywords") or []
            if any(kw.lower() in industry_norm for kw in keywords):
                out["industry"] = facets["industry"]
        # Sub-industry
        if out["sub_industry"] is None and (sub_norm or industry_norm) and facets.get("sub_industry") not in (None, "*"):
            keywords = matches.get("sub_industry_keywords") or []
            # Allow the operator's industry text to also classify
            # sub-industry (they often say "car dealership" in one breath).
            text_to_check = sub_norm + " " + industry_norm
            if any(kw.lower() in text_to_check for kw in keywords):
                out["sub_industry"] = facets["sub_industry"]
        # City
        if out["city"] is None and city_norm and facets.get("city") not in (None, "*"):
            keywords = matches.get("city_keywords") or []
            if any(kw.lower() in city_norm for kw in keywords):
                out["city"] = facets["city"]
    return out


# ─── validation ──────────────────────────────────────────────────────────


def validate_answer(question: dict[str, Any], raw_value: Any) -> tuple[Any, Optional[str]]:
    """Type-aware coercion + validation. Returns (cleaned_value, None)
    on success or (None, error_msg) on failure.

    The cleaned value's shape matches the question type:
      text     → str
      text_list → list[str]
      enum     → str (one of options)
      bool     → bool
      phone    → str (E.164-ish)
      email    → str
      url      → str
    """
    qtype = question.get("type")
    required = bool(question.get("required"))

    def _to_str(v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    s = _to_str(raw_value)

    if not s and required:
        return None, f"answer is required for question {question.get('id')!r}"
    if not s and not required:
        return None, None  # optional + empty = OK, just no value

    if qtype == "text":
        if len(s) > 240:
            return None, "answer too long (max 240 chars)"
        return s, None

    if qtype == "text_list":
        # Operator may have given a list directly OR comma/newline-separated text.
        if isinstance(raw_value, list):
            items = [str(x).strip() for x in raw_value if str(x).strip()]
        else:
            parts = re.split(r"[,\n;]+", s)
            items = [p.strip() for p in parts if p.strip()]
        seen: set[str] = set()
        cleaned: list[str] = []
        for it in items:
            key = it.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(it[:60])
            if len(cleaned) >= 20:
                break
        if not cleaned and required:
            return None, "list must have at least one entry"
        return cleaned, None

    if qtype == "enum":
        opts = question.get("options") or []
        norm = {str(o).lower(): str(o) for o in opts}
        if s.lower() in norm:
            return norm[s.lower()], None
        return None, f"expected one of {opts}, got {s!r}"

    if qtype == "bool":
        # If the raw value was already a boolean (e.g. Eva sent JSON true),
        # accept it directly.
        if isinstance(raw_value, bool):
            return raw_value, None
        if _AFFIRMATIVE.search(s):
            return True, None
        if _NEGATIVE.search(s):
            return False, None
        return None, f"couldn't read {s!r} as yes/no"

    if qtype == "phone":
        if _PHONE_RE.match(s):
            return s, None
        return None, f"{s!r} doesn't look like a phone number"

    if qtype == "email":
        if _EMAIL_RE.match(s):
            return s.lower(), None
        return None, f"{s!r} doesn't look like an email address"

    if qtype == "url":
        if _URL_RE.match(s):
            return s, None
        return None, f"{s!r} doesn't look like a URL"

    return None, f"unknown question type {qtype!r}"


# ─── compose save_agent args ────────────────────────────────────────────


_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")
_HANDLEBARS_IF = re.compile(
    r"\{\{#if\s+([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}(.*?)\{\{/if\}\}",
    re.DOTALL,
)


def _expand_handlebars_if(text: str, ctx: dict[str, Any]) -> str:
    """Render `{{#if foo}}…{{/if}}` blocks. Non-recursive; one level of
    nesting is enough for our templates (they're authored, not
    user-generated). Inside the {{#if}}/{{/if}} block we also expand
    the simple {{var}} substitutions in a second pass via the regular
    _substitute path."""
    def _rep(m: re.Match) -> str:
        cond_key = m.group(1)
        body = m.group(2)
        val = _lookup(ctx, cond_key)
        return body if val else ""
    # Loop until no more matches (handles non-overlapping blocks).
    prev = None
    while prev != text:
        prev = text
        text = _HANDLEBARS_IF.sub(_rep, text)
    return text


def _substitute(text: str, ctx: dict[str, Any]) -> str:
    """Replace `{{slot}}` references in text with values from ctx.
    Unknown slots are left as `{{slot}}` so the operator can spot them
    on the dashboard. Supports nested keys via dots ('variables.brands'
    → ctx['variables']['brands'])."""
    if not isinstance(text, str):
        return text
    text = _expand_handlebars_if(text, ctx)

    def _rep(m: re.Match) -> str:
        key = m.group(1)
        v = _lookup(ctx, key)
        if v is None:
            return m.group(0)
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        if isinstance(v, bool):
            return "yes" if v else "no"
        return str(v)

    return _VAR_PATTERN.sub(_rep, text)


def _lookup(ctx: dict[str, Any], dotted_key: str) -> Any:
    """Walk a dotted-key path through a nested dict context. Returns
    None for any missing intermediate."""
    cur: Any = ctx
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def compose_save_args(
    template: dict[str, Any], answers: dict[str, Any],
) -> dict[str, Any]:
    """Build a save_agent args dict from a resolved template and the
    operator's answers. The template's `agent_profile:` is the
    skeleton; answers fill in the slots; placeholders are substituted
    in greeting/persona/system_prompt.

    Slot keys in `answers` use the template's `slot:` field — top-level
    keys like `agent_name`, `primary_job`, OR nested like
    `variables.brands` (the loader honours dotted paths)."""
    profile = _deep_copy(template.get("agent_profile") or {})

    # Build the substitution context: a nested dict whose top-level
    # keys mirror the save_agent args (name, persona, greeting, ...)
    # plus a `variables` sub-dict. The system_prompt template can then
    # use `{{variables.brands}}`, `{{business_name}}`, etc.
    ctx: dict[str, Any] = {"variables": {}}
    for q in (template.get("questions") or []):
        slot_path = q.get("slot")
        if not slot_path:
            continue
        val = answers.get(q["id"])
        if val is None and "default" in q:
            val = q["default"]
        if val is None:
            continue
        _assign_path(ctx, slot_path, val)

    # Convenience: copy variables.* into the top-level ctx too so
    # templates can write {{business_name}} without {{variables.}}
    for k, v in (ctx.get("variables") or {}).items():
        ctx.setdefault(k, v)

    args: dict[str, Any] = {
        "sector":   profile.get("sector"),
        "locale":   profile.get("locale"),
        "voice":    profile.get("voice"),
        "name":     ctx.get("agent_name") or "Agent",
        "persona":  _substitute(profile.get("persona") or "", ctx),
        "greeting": _substitute(profile.get("greeting") or "", ctx),
        "system_prompt": _substitute(profile.get("system_prompt") or "", ctx),
        "connectors": list(profile.get("connectors") or []),
        "outcomes":   list(profile.get("outcomes") or []),
        "small_talk": list(profile.get("small_talk") or []),
        "guardrails": list(profile.get("guardrails") or []),
        "policy":   _deep_copy(profile.get("policy") or {}),
        "variables": _deep_copy(ctx.get("variables") or {}),
    }
    # purpose: if the template carries one, copy through (post-substitute).
    if profile.get("purpose"):
        args["purpose"] = _deep_copy(profile["purpose"])
        if isinstance(args["purpose"].get("summary"), str):
            args["purpose"]["summary"] = _substitute(args["purpose"]["summary"], ctx)

    # ── runtime placeholder cleanup ──
    # `_substitute` deliberately preserves unknown `{{slot}}` references
    # so a template AUTHOR can spot a typo on the dashboard. At RUNTIME
    # though — when the force-commit watchdog fires mid-interview with
    # only some answers recorded — those preserved placeholders leak
    # into the operator's dashboard as literal `{{brands}}` text. That's
    # uglier than the missing data itself. Sweep them here. Strings get
    # the braces stripped to empty; lists/dicts get recursively swept.
    _sweep_placeholders(args)
    return args


_LEFTOVER_PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}")


def _sweep_placeholders(obj: Any) -> None:
    """In-place: remove any leftover `{{...}}` placeholders that survived
    substitution. Strings: replace placeholder with empty string and
    collapse double spaces. Lists / dicts: recurse. Scalars: untouched."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                if _LEFTOVER_PLACEHOLDER_RE.search(v):
                    cleaned = _LEFTOVER_PLACEHOLDER_RE.sub("", v)
                    # Collapse runs of whitespace that the removal left
                    # behind, but preserve newlines so multi-line
                    # system_prompts don't collapse onto one line.
                    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
                    cleaned = re.sub(r" *\n", "\n", cleaned)
                    obj[k] = cleaned.strip()
            else:
                _sweep_placeholders(v)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                if _LEFTOVER_PLACEHOLDER_RE.search(item):
                    cleaned = _LEFTOVER_PLACEHOLDER_RE.sub("", item)
                    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
                    obj[i] = cleaned
            else:
                _sweep_placeholders(item)


def _assign_path(ctx: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Assign `value` to `ctx[a][b][c]` for dotted_key='a.b.c'.
    Intermediate dicts are created as needed."""
    parts = dotted_key.split(".")
    cur = ctx
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


# ─── reverse lookup: id → resolved template ─────────────────────────────


def get_template(tid: str) -> Optional[dict[str, Any]]:
    """Look up a resolved template by id (e.g. for the WS-level recovery
    path where we know the template_id from the build_session row)."""
    return _RESOLVED.get(tid)


def question_by_id(template: dict[str, Any], qid: str) -> Optional[dict[str, Any]]:
    """Look up a question dict by id within a (resolved) template."""
    for q in template.get("questions") or []:
        if q.get("id") == qid:
            return q
    return None


# ─── wizard projection (REST /api/build/template) ───────────────────────────
#
# The deterministic FORM build (the default UX) renders the template's
# question list as a multi-step form. It needs the same question metadata
# the chat/voice paths consume, plus a short human field LABEL (the chat
# prompt is a full sentence — fine spoken, too long as a form label).

_WIZARD_LABEL_OVERRIDES = {
    "business_name": "Business name",
    "agent_name": "Agent name",
    "primary_job": "Main job",
    "phone": "Transfer number",
    "hours": "Hours",
}


def _wizard_label(q: dict[str, Any]) -> str:
    """A short, form-friendly label for a question. Prefers an explicit
    override, else humanises the slot's leaf key (variables.products →
    'Products', has_service_centre → 'Service centre')."""
    qid = q.get("id") or ""
    if qid in _WIZARD_LABEL_OVERRIDES:
        return _WIZARD_LABEL_OVERRIDES[qid]
    slot = q.get("slot") or qid
    leaf = str(slot).split(".")[-1]
    # Drop common boolean prefixes so "has_service_centre" → "service centre".
    for pre in ("has_", "is_", "does_", "offers_", "offer_"):
        if leaf.startswith(pre):
            leaf = leaf[len(pre):]
            break
    words = leaf.replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else qid


def wizard_payload(template: dict[str, Any]) -> dict[str, Any]:
    """Shape a resolved template for the form-wizard frontend. Returns the
    ordered question list (with short labels) plus light persona metadata
    for the wizard's left rail."""
    profile = template.get("agent_profile") or {}
    facets = template.get("facets") or {}
    questions: list[dict[str, Any]] = []
    for q in (template.get("questions") or []):
        questions.append({
            "id": q.get("id"),
            "label": _wizard_label(q),
            "prompt": q.get("prompt"),
            "type": q.get("type") or "text",
            "required": bool(q.get("required")),
            "hint": q.get("hint"),
            "options": q.get("options"),
            "suggestions": q.get("suggestions"),
            "default": q.get("default"),
            "slot": q.get("slot"),
        })
    industry = facets.get("industry")
    industry = None if industry in (None, "*") else industry
    return {
        "id": template.get("id"),
        "industry": industry,
        "sector": profile.get("sector"),
        "intro": template.get("intro") or "",
        "persona": profile.get("persona") or "",
        "questions": questions,
    }


def next_unanswered_question(
    template: dict[str, Any], answers: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Return the first question whose id isn't in `answers`. None if
    all required questions are answered."""
    for q in template.get("questions") or []:
        if q["id"] in answers:
            continue
        # Skip optional unanswered questions only if explicitly required.
        if not q.get("required"):
            # Optional: skip if we've collected the answer elsewhere.
            # For now treat optional as "ask, but allow skip" — operator
            # can say "skip" and we record None.
            return q
        return q
    return None


# ─── extractor-driven auto-fill ─────────────────────────────────────────
#
# Eva is one half of the build pipeline; the eavesdropping extractor is
# the other half. When the operator speaks, the extractor pulls
# structured slots out of the transcript (sector_kind, business_name,
# city, hours, …) and writes them to the build_session row, completely
# independent of Eva calling tools. The functions below let the runner
# project those auto-captured slots into:
#   (a) a template_id (auto-pick if no template locked yet), and
#   (b) template_answers (pre-fill questions the extractor already
#       resolved, so Eva doesn't re-ask them).
#
# This is what closes the "Eva forgot to call select_build_template"
# failure mode: even if the model never fires the tool, the server can
# still flip the build into deterministic mode the moment the extractor
# has enough signal.


# Map known build_session fact-keys to a triage facet AND a value
# extractor. The extractor keys come from extractor.py's schema +
# merge_build_facts. The triage facets are what `find_best_match`
# accepts.
def facets_from_build_row(row: dict[str, Any]) -> dict[str, Optional[str]]:
    """Project a build_sessions row into the triage facets the matcher
    expects. Returns whatever the row has — missing facets are None,
    and `find_best_match` happily falls back through the inheritance
    chain.

    `industry` is derived from sector_kind via the same keyword-bucket
    table the triage classifier uses, so "homeopathic pharmacy" still
    routes to industry=healthcare even though the operator never used
    that word."""
    if not isinstance(row, dict):
        return {"industry": None, "sub_industry": None, "locale": None, "country": None, "city": None}
    extras = row.get("extras")
    if not isinstance(extras, dict):
        extras = {}
    sector_kind = (row.get("sector_kind") or "").strip().lower()
    industry, sub = _industry_from_sector_kind(sector_kind)
    return {
        "industry":     industry,
        "sub_industry": sub,
        "locale":       extras.get("locale_hint") or None,
        "country":      extras.get("country") or None,
        "city":         extras.get("city") or None,
    }


def _industry_from_sector_kind(sk: str) -> tuple[Optional[str], Optional[str]]:
    """Bucket a free-text sector phrase into (industry, sub_industry).
    Keyword-based and conservative — anything we can't confidently
    bucket returns (None, None) so the build falls back to the
    `_generic` template (which now also asks a full battery of
    universal business questions).

    The mapping mirrors common sector_kind values produced by both Eva
    and the extractor. Expand cautiously: a wrong route is worse than
    no route (the operator gets the wrong question list)."""
    if not sk:
        return (None, None)
    # Order matters: more specific first. The salon family includes pet
    # grooming explicitly because operators say "pet salon" / "pet
    # groomer" interchangeably with hair/nail/spa salons, and the
    # salon template's question set covers all of them.
    if any(k in sk for k in ("car dealership", "auto dealership", "showroom", "dealership", "automotive")):
        return ("automotive", "dealership" if "dealership" in sk or "showroom" in sk else None)
    if any(k in sk for k in ("dental", "dentist", "orthodont")):
        return ("dental", None)
    if any(k in sk for k in ("restaurant", "cafe", "café", "bistro", "diner", "eatery")):
        return ("restaurant", None)
    # Salon family — hair, nail, spa, beauty parlour, and pet grooming.
    # Sub-industry stays None here; the template's first question asks
    # the operator to pick {hair, nail, spa, pet grooming, full-service}.
    if any(k in sk for k in (
        "salon", "spa", "parlour", "parlor",
        "beauty", "hair", "nail", "barber",
        "grooming", "groomer", "pet salon",
    )):
        return ("salon", None)
    # Healthcare (general medical) — dental is matched above first so a
    # dental clinic doesn't get bucketed here.
    if any(k in sk for k in (
        "clinic", "hospital", "medical", "doctor", "physician",
        "healthcare", "nursing home", "polyclinic", "diagnostic",
    )):
        return ("healthcare", None)
    if any(k in sk for k in (
        "real estate", "realty", "property", "properties", "builder",
        "developer", "flats", "apartment", "plots", "broker", "housing",
    )):
        return ("real_estate", None)
    if any(k in sk for k in (
        "school", "college", "coaching", "institute", "academy",
        "tuition", "classes", "training", "education", "edtech", "tutor",
    )):
        return ("education", None)
    if any(k in sk for k in (
        "travel", "tour", "holiday", "hotel", "resort", "trip",
        "vacation", "tourism", "homestay", "package",
    )):
        return ("travel", None)
    if any(k in sk for k in (
        "retail", "shop", "store", "boutique", "showroom", "mart",
        "e-commerce", "ecommerce", "online store", "merchandise",
    )):
        return ("retail", None)
    # If we ever ship more templates, add buckets here.
    return (None, None)


# Map template question id → an accessor that pulls the answer value
# from a build_session row. Returning None means "no extractor coverage
# for this question — Eva must ask it." Question ids we don't cover
# stay unanswered and surface in the NEXT QUESTION block normally.
def extracted_answers_from_build_row(
    template: dict[str, Any], row: dict[str, Any],
) -> dict[str, Any]:
    """Scan the template's question list and pull any answers the
    extractor / note_build_facts already captured into the build row.
    Returns a {question_id: value} dict ready to merge into
    template_answers."""
    if not isinstance(row, dict):
        return {}
    extras = row.get("extras") if isinstance(row.get("extras"), dict) else {}
    captured: dict[str, Any] = {}
    for q in (template.get("questions") or []):
        qid = q.get("id")
        if not qid:
            continue
        # ID-based mapping — explicit so we never accidentally pull a
        # plausible-but-wrong field. Add new mappings cautiously.
        val: Any = None
        if qid == "business_name":
            val = row.get("business_name")
        elif qid == "agent_name":
            val = row.get("agent_name")
        elif qid == "primary_job":
            val = row.get("primary_job")
        elif qid == "city":
            val = extras.get("city")
        elif qid == "country":
            val = extras.get("country")
        elif qid == "hours":
            val = extras.get("hours")
        elif qid == "phone":
            val = extras.get("escalation_phone") or extras.get("phone")
        elif qid == "email":
            val = extras.get("email")
        elif qid == "website":
            val = extras.get("website")
        elif qid == "services":
            val = extras.get("services")
        elif qid == "address":
            val = extras.get("address")
        elif qid == "languages":
            val = extras.get("locale_hint")
        else:
            continue
        if val is None or val == "" or val == []:
            continue
        captured[qid] = val
    return captured
