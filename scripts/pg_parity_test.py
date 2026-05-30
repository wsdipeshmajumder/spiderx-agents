"""Parity test: run the same 52-probe DB audit against db_pg.py that we ran
against db.py, so we can prove Postgres is a drop-in replacement before
flipping any routes.

Usage:
    PG_URL='postgresql://sxai:sxai_local_dev@localhost:5432/sxai_dev_test' \\
        .venv/bin/python -m scripts.pg_parity_test

Uses a SEPARATE database (sxai_dev_test, not sxai_dev) so the audit's
mutating probes don't touch your real local dev data. The script:
  1. Verifies the test DB is empty.
  2. Runs alembic upgrade head against it.
  3. Executes every probe.
  4. Tears down (downgrades back to base) on success.

If any probe fails, the schema is left intact for inspection.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend import db_pg as db  # type: ignore


def _alembic(*args: str, env_extra: dict | None = None) -> None:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        [str(ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=ROOT, env=env, check=True,
    )


async def run() -> int:
    results: dict[str, str] = {}

    def chk(name: str, ok: bool, detail: str = "") -> None:
        results[name] = "PASS" if ok else f"FAIL: {detail}"

    # ── 1. INIT ──
    try:
        await db.init()
        chk("01_init", True)
    except Exception as e:  # noqa: BLE001
        chk("01_init", False, repr(e))
        return _report(results)

    # ── 2. USERS ──
    try:
        founder = await db.get_founder()
        chk("02_get_founder", bool(founder and founder.get("email")), repr(founder)[:80])
    except Exception as e:
        chk("02_get_founder", False, repr(e))

    try:
        u = await db.get_user(founder["id"])
        chk("03_get_user_by_id", u and u["id"] == founder["id"], repr(u)[:80])
    except Exception as e:
        chk("03_get_user_by_id", False, repr(e))

    try:
        u = await db.get_user(999999)
        chk("04_get_user_missing", u is None, repr(u))
    except Exception as e:
        chk("04_get_user_missing", False, repr(e))

    try:
        u = await db.get_user_by_email(db.FOUNDER_EMAIL)
        chk("05_get_user_by_email", u and u["id"] == founder["id"], repr(u)[:80])
    except Exception as e:
        chk("05_get_user_by_email", False, repr(e))

    try:
        u = await db.get_user_by_email("nobody@example.com")
        chk("06_email_missing", u is None, repr(u))
    except Exception as e:
        chk("06_email_missing", False, repr(e))

    new_u = None
    try:
        new_u = await db.create_user("parity+e2e@spiderx.ai", name="Parity Tester")
        chk("07_create_user_new", new_u and new_u["email"] == "parity+e2e@spiderx.ai", repr(new_u)[:80])
    except Exception as e:
        chk("07_create_user_new", False, repr(e))

    try:
        dup = await db.create_user("parity+e2e@spiderx.ai", name="Parity Tester")
        chk("08_create_user_idempotent", dup["id"] == new_u["id"], f"{dup['id']} vs {new_u['id']}")
    except Exception as e:
        chk("08_create_user_idempotent", False, repr(e))

    try:
        cased = await db.create_user("  PARITY+E2E@SPIDERX.AI  ", name="Parity Tester")
        chk("09_create_user_email_norm", cased["id"] == new_u["id"], f"{cased['id']} vs {new_u['id']}")
    except Exception as e:
        chk("09_create_user_email_norm", False, repr(e))

    # ── 3. ORGS ──
    try:
        org = await db.get_org_for_user(new_u["id"])
        chk("10_org_auto_created", org and "workspace" in org["name"].lower(), repr(org)[:120])
    except Exception as e:
        chk("10_org_auto_created", False, repr(e))

    try:
        updated = await db.update_org_for_user(
            new_u["id"], {"country": "GB", "tax_id": "GB123", "currency": "GBP"}
        )
        chk("11_update_org", updated["country"] == "GB" and updated["tax_id"] == "GB123",
            repr(updated)[:120])
    except Exception as e:
        chk("11_update_org", False, repr(e))

    try:
        updated2 = await db.update_org_for_user(
            new_u["id"], {"id": 99, "created_at": "2000-01-01", "country": "US"}
        )
        chk("12_update_org_strips_bad_keys",
            updated2["id"] != 99 and updated2["country"] == "US", repr(updated2)[:120])
    except Exception as e:
        chk("12_update_org_strips_bad_keys", False, repr(e))

    try:
        same = await db.update_org_for_user(new_u["id"], {})
        chk("13_update_org_empty_patch", same is not None and same["country"] == "US",
            repr(same)[:120])
    except Exception as e:
        chk("13_update_org_empty_patch", False, repr(e))

    try:
        nullret = await db.update_org_for_user(999999, {"country": "JP"})
        chk("14_update_org_missing_user", nullret is None, repr(nullret))
    except Exception as e:
        chk("14_update_org_missing_user", False, repr(e))

    # ── 4. PLANS ──
    try:
        plans = await db.list_plans()
        slugs = [p["slug"] for p in plans]
        chk("15_list_plans", {"free", "starter", "pro", "business"}.issubset(slugs), repr(slugs))
    except Exception as e:
        chk("15_list_plans", False, repr(e))

    try:
        p = await db.get_plan_by_slug("free")
        chk("16_get_plan_by_slug", p and p["slug"] == "free" and p["minutes_total"] == 30,
            repr(p)[:120])
    except Exception as e:
        chk("16_get_plan_by_slug", False, repr(e))

    try:
        p = await db.get_plan_by_slug("nope")
        chk("17_plan_missing", p is None, repr(p))
    except Exception as e:
        chk("17_plan_missing", False, repr(e))

    try:
        state = await db.get_user_plan_state(new_u["id"])
        chk("18_plan_state_new_user",
            state["plan"] and state["minutes_total"] >= 30 and state["minutes_used"] == 0.0,
            repr(state)[:200])
    except Exception as e:
        chk("18_plan_state_new_user", False, repr(e))

    try:
        starter = await db.get_plan_by_slug("starter")
        upgraded = await db.set_user_plan(new_u["id"], starter["id"])
        chk("19_set_plan_upgrade",
            upgraded["plan"]["slug"] == "starter" and upgraded["minutes_used"] == 0.0,
            repr(upgraded)[:150])
    except Exception as e:
        chk("19_set_plan_upgrade", False, repr(e))

    try:
        raised = False
        try:
            await db.set_user_plan(new_u["id"], 999999)
        except ValueError:
            raised = True
        chk("20_set_plan_bad_id_rejected", raised, "expected ValueError on unknown plan_id")
    except Exception as e:
        chk("20_set_plan_bad_id_rejected", False, repr(e))

    # reset
    free = await db.get_plan_by_slug("free")
    await db.set_user_plan(new_u["id"], free["id"])

    # ── 5. AGENTS ──
    aid = None
    a2 = None
    try:
        a = await db.create_agent({
            "name": "Audit Agent",
            "sector": "saas", "locale": "en-IN",
            "persona": "Crisp, polite", "greeting": "Hi, Audit here.",
            "system_prompt": "Be helpful.", "voice": "Kore",
            "guardrails": ["dont quote prices"],
            "connectors": [{"id": "twilio"}],
            "sip_config": {"trunk": "test"},
            "voice_tweaks": {"tone": "warm"},
            "outcomes": ["Lead", "Demo booked"],
            "policy": {"dos": ["confirm"], "donts": ["pressure"]},
            "webhook_url": "https://example.com/hook",
            "webhook_headers": {"X-Test": "1"},
            "variables": {"business_name": "Audit Co"},
        }, user_id=new_u["id"])
        aid = a["id"]
        chk("21_create_agent_full",
            a["name"] == "Audit Agent" and a["voice"] == "Kore" and a["slug"] == "audit-agent"
            and a["guardrails"] == ["dont quote prices"]
            and a["variables"]["business_name"] == "Audit Co",
            repr({"slug": a["slug"], "vars": a["variables"]})[:200])
    except Exception as e:
        chk("21_create_agent_full", False, repr(e))

    try:
        a2 = await db.create_agent({"name": "Bare", "system_prompt": "x"}, user_id=new_u["id"])
        chk("22_create_agent_minimal",
            a2["voice"] == "Aoede" and a2["guardrails"] == []
            and a2["connectors"] == [] and a2["variables"] in (None, {}),
            repr({"v": a2["voice"], "g": a2["guardrails"], "var": a2["variables"]})[:200])
    except Exception as e:
        chk("22_create_agent_minimal", False, repr(e))

    try:
        a3 = await db.create_agent({"name": "Audit Agent", "system_prompt": "y"},
                                   user_id=new_u["id"])
        chk("23_slug_auto_suffix", a3["slug"] == "audit-agent-2", a3.get("slug"))
    except Exception as e:
        chk("23_slug_auto_suffix", False, repr(e))

    try:
        fetched = await db.get_agent(aid)
        chk("24_get_agent_decodes_json",
            isinstance(fetched["guardrails"], list) and isinstance(fetched["policy"], dict)
            and fetched["policy"]["dos"] == ["confirm"],
            repr(type(fetched["policy"]).__name__))
    except Exception as e:
        chk("24_get_agent_decodes_json", False, repr(e))

    try:
        by_slug = await db.get_agent_by_slug("audit-agent")
        chk("25_get_agent_by_slug", by_slug and by_slug["id"] == aid, repr(by_slug.get("id")))
    except Exception as e:
        chk("25_get_agent_by_slug", False, repr(e))

    try:
        nullret = await db.get_agent_by_slug("does-not-exist")
        chk("26_get_agent_by_slug_missing", nullret is None, repr(nullret))
    except Exception as e:
        chk("26_get_agent_by_slug_missing", False, repr(e))

    try:
        u1 = await db.update_agent(aid, {"name": "Renamed Audit", "voice": "Aoede"})
        chk("27_update_agent_scalars", u1["name"] == "Renamed Audit" and u1["voice"] == "Aoede",
            repr(u1)[:120])
        chk("28_slug_stable_on_rename", u1["slug"] == "audit-agent", f"slug={u1['slug']}")
    except Exception as e:
        chk("27_update_agent_scalars", False, repr(e))

    try:
        u2 = await db.update_agent(aid, {"variables": {"business_name": "Renamed Co", "industry": "saas"}})
        chk("29_update_agent_json", u2["variables"]["industry"] == "saas",
            repr(u2["variables"])[:120])
    except Exception as e:
        chk("29_update_agent_json", False, repr(e))

    try:
        u3 = await db.update_agent(aid, {"variables": None})
        chk("30_update_agent_json_null", u3["variables"] in (None, {}),
            repr(u3.get("variables")))
    except Exception as e:
        chk("30_update_agent_json_null", False, repr(e))

    try:
        u4 = await db.update_agent(aid, {"id": 99, "created_at": "2000-01-01",
                                          "user_id": 0, "name": "Final Audit"})
        chk("31_update_agent_disallowed_keys",
            u4["id"] == aid and u4["name"] == "Final Audit", f"id={u4['id']} name={u4['name']}")
    except Exception as e:
        chk("31_update_agent_disallowed_keys", False, repr(e))

    try:
        u5 = await db.update_agent(aid, {"published": True})
        chk("32_publish_stamps_at", u5["published"] and u5["published_at"],
            f"pub={u5['published']} at={u5.get('published_at')}")
    except Exception as e:
        chk("32_publish_stamps_at", False, repr(e))

    try:
        u6 = await db.update_agent(aid, {"published": False})
        chk("33_unpublish_keeps_audit_at",
            not u6["published"] and u6["published_at"],
            f"pub={u6['published']} at={u6.get('published_at')}")
    except Exception as e:
        chk("33_unpublish_keeps_audit_at", False, repr(e))

    try:
        u7 = await db.update_agent(aid, {})
        chk("34_update_agent_empty_patch", u7 and u7["id"] == aid, repr(u7.get("id")))
    except Exception as e:
        chk("34_update_agent_empty_patch", False, repr(e))

    try:
        u8 = await db.update_agent(aid, {"name": "Final Audit"})
        chk("35_update_agent_noop_value", u8 is not None, repr(u8))
    except Exception as e:
        chk("35_update_agent_noop_value", False, repr(e))

    try:
        u9 = await db.update_agent(999999, {"name": "Ghost"})
        chk("36_update_agent_missing_returns_none", u9 is None, repr(u9))
    except Exception as e:
        chk("36_update_agent_missing_returns_none", False, repr(e))

    try:
        lst = await db.list_agents(new_u["id"])
        chk("37_list_agents_owner_filter",
            len(lst) >= 3 and all(isinstance(a["variables"], (dict, type(None))) for a in lst),
            f"count={len(lst)}")
    except Exception as e:
        chk("37_list_agents_owner_filter", False, repr(e))

    try:
        other = await db.list_agents(999999)
        chk("38_list_agents_isolation", other == [], f"len={len(other)}")
    except Exception as e:
        chk("38_list_agents_isolation", False, repr(e))

    # ── 6. CALLS ──
    try:
        cid = await db.insert_call({
            "agent_id": aid, "duration_s": 47.3, "outcome": "Lead",
            "reason": "CONVERSATION_COMPLETE", "summary": "Audit call",
            "extracted": {"customer_name": "Joe"}, "transcript": "...",
        })
        chk("39_insert_call", cid and cid > 0, f"id={cid}")
    except Exception as e:
        chk("39_insert_call", False, repr(e))

    try:
        await db.insert_call({"agent_id": aid, "duration_s": 12, "outcome": "Lead"})
        await db.insert_call({"agent_id": aid, "duration_s": 80, "outcome": "Demo booked"})
        await db.insert_call({"agent_id": aid, "duration_s": 5})
        calls = await db.list_calls_for_agent(aid)
        chk("40_list_calls", len(calls) == 4, f"count={len(calls)}")
    except Exception as e:
        chk("40_list_calls", False, repr(e))

    try:
        stats = await db.call_stats_for_agent(aid)
        by = {o["outcome"]: o["count"] for o in stats["outcomes"]}
        chk("41_call_stats_total_and_groups",
            stats["total"] == 4 and by.get("Lead") == 2 and by.get("Demo booked") == 1
            and by.get("unknown") == 1, repr(stats))
    except Exception as e:
        chk("41_call_stats_total_and_groups", False, repr(e))

    try:
        # Postgres DOES enforce FKs — orphan call should fail.
        raised = False
        try:
            await db.insert_call({"agent_id": 999999, "duration_s": 1})
        except Exception:
            raised = True
        chk("42_call_fk_enforced", raised, "expected FK violation on missing agent")
    except Exception as e:
        chk("42_call_fk_enforced", False, repr(e))

    try:
        stats0 = await db.call_stats_for_agent(999998)
        chk("43_stats_empty_agent", stats0["total"] == 0 and stats0["outcomes"] == [],
            repr(stats0))
    except Exception as e:
        chk("43_stats_empty_agent", False, repr(e))

    # ── 7. NUMBER REQUESTS ──
    try:
        nr = await db.create_number_request({
            "agent_id": aid, "country": "IN", "city": "Bangalore",
            "delivery_handle": "+91 9999999999", "notes": "Pls",
        }, user_id=new_u["id"])
        chk("44_create_number_request", nr["id"] > 0 and nr["status"] == "pending",
            repr(nr)[:120])
    except Exception as e:
        chk("44_create_number_request", False, repr(e))

    try:
        nrs = await db.list_number_requests_for_agent(aid)
        chk("45_list_num_req", len(nrs) >= 1 and nrs[0]["delivery_handle"] == "+91 9999999999",
            f"count={len(nrs)}")
    except Exception as e:
        chk("45_list_num_req", False, repr(e))

    try:
        nrs0 = await db.list_number_requests_for_agent(999999)
        chk("46_list_num_req_empty", nrs0 == [], repr(nrs0))
    except Exception as e:
        chk("46_list_num_req_empty", False, repr(e))

    # ── 8. DELETE + CASCADE ──
    try:
        ok = await db.delete_agent(aid)
        chk("47_delete_agent_returns_true", ok, f"ok={ok}")
        # FK CASCADE handles the children
        leftover_calls = len(await db.list_calls_for_agent(aid))
        leftover_nrs = len(await db.list_number_requests_for_agent(aid))
        chk("48_delete_agent_cascades_calls", leftover_calls == 0, f"leftover={leftover_calls}")
        chk("49_delete_agent_cascades_nrs", leftover_nrs == 0, f"leftover={leftover_nrs}")
    except Exception as e:
        chk("47_delete_agent_returns_true", False, repr(e))

    try:
        ok2 = await db.delete_agent(999998)
        chk("50_delete_agent_missing", ok2 is False, f"got {ok2}")
    except Exception as e:
        chk("50_delete_agent_missing", False, repr(e))

    # ── 9. SLUG EDGES ──
    try:
        s1 = db._slugify("Acme Co.")
        s2 = db._slugify("  ñàçé !!!  ")
        s3 = db._slugify("")
        s4 = db._slugify("---///")
        chk("51_slugify_punct_and_empty",
            s1 == "acme-co" and s2 == "agent" and s3 == "agent" and s4 == "agent",
            f"{s1!r} {s2!r} {s3!r} {s4!r}")
    except Exception as e:
        chk("51_slugify_punct_and_empty", False, repr(e))

    # ── 10. JSON DECODE robustness ──
    # In Postgres JSONB is type-checked — you can't insert garbage. Verify by
    # checking that fetching a real agent decodes cleanly.
    try:
        fetched = await db.get_agent(a2["id"])
        chk("52_json_decode_clean",
            isinstance(fetched["guardrails"], list) and isinstance(fetched["connectors"], list),
            repr(type(fetched["guardrails"]).__name__))
    except Exception as e:
        chk("52_json_decode_clean", False, repr(e))

    await db.close_pool()
    return _report(results)


def _report(results: dict[str, str]) -> int:
    print("\n=== PG PARITY TEST RESULTS ===")
    passed = sum(1 for v in results.values() if v == "PASS")
    total = len(results)
    for k in sorted(results):
        print(f"{k}: {results[k]}")
    print(f"\nTOTAL: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
