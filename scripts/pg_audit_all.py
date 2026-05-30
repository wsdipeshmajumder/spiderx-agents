"""Comprehensive DB op audit — Phase 1 + Phase 2.

Exercises every public function in db_pg.py against a clean test database.
Each probe asserts a single contract and either PASSes or reports a
diagnostic. Designed to grow — when Phase 3 adds super_admins, append
probes here.

Usage:
    PG_URL='postgresql://sxai:sxai_local_dev@localhost:5432/sxai_dev_test' \\
        .venv/bin/python -m scripts.pg_audit_all

Wipes the target DB (downgrade → upgrade) before running, so probes get
deterministic state.
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


def _alembic(*args: str) -> None:
    subprocess.run(
        [str(ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=ROOT, env=os.environ.copy(), check=True,
        capture_output=True,
    )


async def run() -> int:
    print(f"→ Target: {os.environ.get('PG_URL', '<unset>').split('@')[-1]}")
    print("→ Resetting schema …")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")

    R: dict[str, str] = {}

    def chk(name: str, ok: bool, detail: str = "") -> None:
        R[name] = "PASS" if ok else f"FAIL: {detail}"

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 1 — schema, users, orgs, plans, agents, calls, NRs, slugs  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # ── init ──
    try:
        await db.init()
        chk("01_init", True)
    except Exception as e:  # noqa: BLE001
        chk("01_init", False, repr(e)); return _report(R)

    # ── users ──
    founder = await db.get_founder()
    chk("02_get_founder", bool(founder and founder["email"]), repr(founder)[:80])
    chk("03_get_user_by_id", (await db.get_user(founder["id"]))["id"] == founder["id"])
    chk("04_get_user_missing", await db.get_user(999999) is None)
    chk("05_get_user_by_email", (await db.get_user_by_email(db.FOUNDER_EMAIL))["id"] == founder["id"])
    chk("06_email_missing", await db.get_user_by_email("nobody@example.com") is None)

    alice = await db.create_user("alice@spiderx-test.ai", name="Alice")
    chk("07_create_user_new", alice["email"] == "alice@spiderx-test.ai", repr(alice)[:80])
    dup = await db.create_user("alice@spiderx-test.ai", name="Alice")
    chk("08_create_user_idempotent", dup["id"] == alice["id"])
    cased = await db.create_user("  ALICE@SPIDERX-TEST.AI  ", name="Alice")
    chk("09_create_user_email_norm", cased["id"] == alice["id"])

    # ── orgs ──
    org = await db.get_org_for_user(alice["id"])
    chk("10_org_auto_created", org and "workspace" in org["name"].lower(), repr(org)[:120])
    u = await db.update_org_for_user(alice["id"], {"country": "GB", "tax_id": "GB123", "currency": "GBP"})
    chk("11_update_org", u["country"] == "GB" and u["tax_id"] == "GB123")
    u2 = await db.update_org_for_user(alice["id"], {"id": 99, "created_at": "2000-01-01", "country": "US"})
    chk("12_update_org_strips_bad_keys", u2["id"] != 99 and u2["country"] == "US")
    same = await db.update_org_for_user(alice["id"], {})
    chk("13_update_org_empty_patch", same is not None and same["country"] == "US")
    chk("14_update_org_missing_user", await db.update_org_for_user(999999, {"country": "JP"}) is None)

    # ── plans ──
    plans = await db.list_plans()
    chk("15_list_plans", {"free","starter","pro","business"} <= {p["slug"] for p in plans})
    p = await db.get_plan_by_slug("free")
    chk("16_get_plan_free", p["minutes_total"] == 30)
    chk("17_plan_missing", await db.get_plan_by_slug("nope") is None)
    state = await db.get_user_plan_state(alice["id"])
    chk("18_plan_state_default", state["plan"]["slug"] == "free" and state["minutes_used"] == 0.0)
    starter = await db.get_plan_by_slug("starter")
    up = await db.set_user_plan(alice["id"], starter["id"])
    chk("19_set_plan_upgrade", up["plan"]["slug"] == "starter")

    raised = False
    try: await db.set_user_plan(alice["id"], 999999)
    except ValueError: raised = True
    chk("20_set_plan_bad_id_rejected", raised)

    free = await db.get_plan_by_slug("free")
    await db.set_user_plan(alice["id"], free["id"])

    # ── agents (Phase 1 + Phase 2 org-aware) ──
    alice_org = (await db.get_org_for_user(alice["id"]))["id"]
    a = await db.create_agent({
        "name": "Audit Agent", "sector": "saas", "locale": "en-IN",
        "persona": "Crisp", "greeting": "Hi", "system_prompt": "Be helpful.",
        "voice": "Kore", "guardrails": ["dont quote prices"],
        "connectors": [{"id": "twilio"}], "sip_config": {"trunk": "t"},
        "voice_tweaks": {"tone": "warm"}, "outcomes": ["Lead", "Demo booked"],
        "policy": {"dos": ["confirm"], "donts": ["pressure"]},
        "webhook_url": "https://example.com/hook",
        "webhook_headers": {"X-T": "1"}, "variables": {"business_name": "Audit Co"},
    }, user_id=alice["id"])
    aid = a["id"]
    chk("21_create_agent_full",
        a["slug"] == "audit-agent" and a["org_id"] == alice_org
        and a["variables"]["business_name"] == "Audit Co",
        repr({"slug": a["slug"], "org": a["org_id"]}))

    a2 = await db.create_agent({"name": "Bare", "system_prompt": "x"}, user_id=alice["id"])
    chk("22_create_agent_minimal", a2["voice"] == "Aoede" and a2["guardrails"] == [] and a2["variables"] == {})

    a3 = await db.create_agent({"name": "Audit Agent", "system_prompt": "y"}, user_id=alice["id"])
    chk("23_slug_auto_suffix", a3["slug"] == "audit-agent-2")

    chk("24_get_agent_decodes_json",
        (await db.get_agent(aid))["policy"]["dos"] == ["confirm"])

    chk("25_get_agent_by_slug", (await db.get_agent_by_slug("audit-agent"))["id"] == aid)
    chk("26_get_agent_by_slug_missing", await db.get_agent_by_slug("nope") is None)

    u1 = await db.update_agent(aid, {"name": "Renamed", "voice": "Aoede"})
    chk("27_update_agent_scalars", u1["name"] == "Renamed" and u1["voice"] == "Aoede")
    chk("28_slug_stable_on_rename", u1["slug"] == "audit-agent")

    u3 = await db.update_agent(aid, {"variables": {"industry": "saas"}})
    chk("29_update_agent_json", u3["variables"]["industry"] == "saas")

    u4 = await db.update_agent(aid, {"variables": None})  # coerced to {} (NOT NULL)
    chk("30_update_agent_jsonnull_coerced", u4["variables"] == {})

    u5 = await db.update_agent(aid, {"id": 99, "created_at": "2000", "user_id": 0, "name": "Final"})
    chk("31_update_disallowed_keys_stripped", u5["id"] == aid and u5["name"] == "Final")

    p_on = await db.update_agent(aid, {"published": True})
    chk("32_publish_stamps_at", p_on["published"] and p_on["published_at"])

    p_off = await db.update_agent(aid, {"published": False})
    chk("33_unpublish_keeps_audit_at", not p_off["published"] and p_off["published_at"])

    chk("34_update_agent_empty_patch", (await db.update_agent(aid, {}))["id"] == aid)
    chk("35_update_agent_noop", (await db.update_agent(aid, {"name": "Final"})) is not None)
    chk("36_update_missing_returns_none", await db.update_agent(999999, {"name": "Ghost"}) is None)

    lst = await db.list_agents(alice["id"])
    chk("37_list_agents_org_scoped", len(lst) == 3 and all(x["org_id"] == alice_org for x in lst))
    chk("38_list_agents_user_isolation", await db.list_agents(999999) == [])

    # ── calls ──
    cid = await db.insert_call({
        "agent_id": aid, "duration_s": 47.3, "outcome": "Lead",
        "reason": "CONVERSATION_COMPLETE", "summary": "x",
        "extracted": {"customer_name": "Joe"}, "transcript": "...",
        "input_tokens": 1500, "output_tokens": 300, "model_id": "gemini-2.0-flash-live-001",
        "cost_paise": 12,
    })
    chk("39_insert_call_with_tokens", cid > 0)

    await db.insert_call({"agent_id": aid, "duration_s": 12, "outcome": "Lead"})
    await db.insert_call({"agent_id": aid, "duration_s": 80, "outcome": "Demo booked"})
    await db.insert_call({"agent_id": aid, "duration_s": 5})
    chk("40_list_calls", len(await db.list_calls_for_agent(aid)) == 4)

    stats = await db.call_stats_for_agent(aid)
    by = {o["outcome"]: o["count"] for o in stats["outcomes"]}
    chk("41_call_stats", stats["total"] == 4 and by.get("Lead") == 2 and by.get("unknown") == 1)

    raised = False
    try: await db.insert_call({"agent_id": 999999, "duration_s": 1})
    except Exception: raised = True
    chk("42_call_fk_enforced", raised)

    chk("43_stats_empty_agent", (await db.call_stats_for_agent(999998))["total"] == 0)

    # ── number_requests ──
    nr = await db.create_number_request({
        "agent_id": aid, "country": "IN", "city": "Bangalore",
        "delivery_handle": "+91 9999999999", "notes": "Pls",
    }, user_id=alice["id"])
    chk("44_create_number_request", nr["id"] > 0 and nr["status"] == "pending")
    chk("45_list_num_req", len(await db.list_number_requests_for_agent(aid)) == 1)
    chk("46_list_num_req_empty", await db.list_number_requests_for_agent(999999) == [])

    # ── delete (cascade) ──
    ok = await db.delete_agent(aid)
    chk("47_delete_agent", ok)
    chk("48_cascade_calls", len(await db.list_calls_for_agent(aid)) == 0)
    chk("49_cascade_nrs", len(await db.list_number_requests_for_agent(aid)) == 0)
    chk("50_delete_missing", await db.delete_agent(999998) is False)

    # ── slug edge cases ──
    chk("51_slugify",
        db._slugify("Acme Co.") == "acme-co"
        and db._slugify("  ñàçé !!!  ") == "agent"
        and db._slugify("") == "agent"
        and db._slugify("---///") == "agent")

    # ── JSONB clean round-trip ──
    chk("52_json_decode_clean", isinstance((await db.get_agent(a2["id"]))["guardrails"], list))

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 2 — org_members + org_invites + agent org permissions      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # ── memberships ──
    bob = await db.create_user("bob@spiderx-test.ai", name="Bob")
    bob_org = (await db.get_org_for_user(bob["id"]))["id"]
    chk("53_users_have_separate_orgs", alice_org != bob_org)

    members = await db.list_org_members(alice_org)
    chk("54_list_members_default_owner",
        len(members) == 1 and members[0]["user_id"] == alice["id"] and members[0]["role"] == "owner")

    role = await db.get_member_role(alice_org, alice["id"])
    chk("55_get_role_owner", role == "owner")
    chk("56_get_role_non_member", await db.get_member_role(alice_org, bob["id"]) is None)
    chk("57_get_role_missing_org", await db.get_member_role(999999, alice["id"]) is None)

    added = await db.add_org_member(alice_org, bob["id"], "admin", invited_by=alice["id"])
    chk("58_add_member_admin", added["role"] == "admin")

    # idempotent: re-add returns existing row, doesn't change role
    re_add = await db.add_org_member(alice_org, bob["id"], "member")
    chk("59_add_member_idempotent", re_add["role"] == "admin", f"role={re_add['role']!r}")

    raised = False
    try: await db.add_org_member(alice_org, bob["id"], "BOGUS")
    except ValueError: raised = True
    chk("60_add_member_bad_role", raised)

    upd = await db.update_member_role(alice_org, bob["id"], "member")
    chk("61_update_member_role", upd["role"] == "member")
    chk("62_update_role_missing", await db.update_member_role(alice_org, 999999, "member") is None)

    # ── counts + listing ──
    chk("63_count_owners", await db.count_owners(alice_org) == 1)
    members2 = await db.list_org_members(alice_org)
    chk("64_list_members_sorted_owner_first",
        members2[0]["role"] == "owner" and len(members2) == 2,
        repr([m["role"] for m in members2]))

    orgs_for_bob = await db.list_orgs_for_user(bob["id"])
    chk("65_orgs_for_user_includes_invited",
        any(o["id"] == alice_org for o in orgs_for_bob)
        and any(o["id"] == bob_org for o in orgs_for_bob),
        repr([o["id"] for o in orgs_for_bob]))

    chk("66_list_agents_for_org_scoped",
        len(await db.list_agents_for_org(alice_org)) == 2,
        # aid was deleted; a2 + a3 remain
        f"got {len(await db.list_agents_for_org(alice_org))}")

    # bob, now a member of alice's org, sees alice's agents via list_agents
    bob_listing = await db.list_agents(bob["id"])
    chk("67_list_agents_via_membership",
        any(x["org_id"] == alice_org for x in bob_listing),
        f"bob sees {len(bob_listing)} agents")

    chk("68_get_agent_org",
        (await db.get_agent_org(a2["id"])) == alice_org)
    chk("69_get_agent_org_missing",
        (await db.get_agent_org(999999)) is None)

    # ── invites ──
    inv = await db.create_invite(alice_org, "carol@spiderx-test.ai", "member", invited_by=alice["id"])
    chk("70_create_invite",
        inv["token"] and inv["role"] == "member" and inv["email"] == "carol@spiderx-test.ai",
        repr({"role": inv["role"], "email": inv["email"]}))

    inv_dup = await db.create_invite(alice_org, "carol@spiderx-test.ai", "member", invited_by=alice["id"])
    chk("71_invite_idempotent_same_token", inv_dup["token"] == inv["token"])

    # case-insensitive idempotency
    inv_cased = await db.create_invite(alice_org, "  CAROL@SPIDERX-TEST.AI ", "member", invited_by=alice["id"])
    chk("72_invite_email_norm", inv_cased["token"] == inv["token"])

    raised = False
    try: await db.create_invite(alice_org, "bad@x.com", "owner", invited_by=alice["id"])
    except ValueError: raised = True
    chk("73_invite_owner_rejected", raised)

    raised = False
    try: await db.create_invite(alice_org, "  ", "member", invited_by=alice["id"])
    except ValueError: raised = True
    chk("74_invite_empty_email_rejected", raised)

    pending = await db.list_pending_invites(alice_org)
    chk("75_list_pending_invites", len(pending) == 1)

    preview = await db.get_invite_by_token(inv["token"])
    chk("76_invite_preview_includes_org_name",
        preview["org_name"] and preview["inviter_name"] == "Alice",
        repr({"org": preview.get("org_name"), "inviter": preview.get("inviter_name")}))

    chk("77_invite_preview_missing", await db.get_invite_by_token("not-a-token") is None)

    # ── accept ──
    carol = await db.create_user("carol@spiderx-test.ai", name="Carol")
    membership = await db.accept_invite(inv["token"], carol["id"])
    chk("78_accept_invite",
        membership["org_id"] == alice_org and membership["role"] == "member",
        repr(membership))

    chk("79_double_accept_returns_none", await db.accept_invite(inv["token"], carol["id"]) is None)
    chk("80_accept_bad_token", await db.accept_invite("ghost", carol["id"]) is None)

    # carol now sees alice's org agents
    carol_listing = await db.list_agents(carol["id"])
    chk("81_carol_sees_org_agents_via_accept",
        any(x["org_id"] == alice_org for x in carol_listing))

    # ── decline ──
    inv2 = await db.create_invite(alice_org, "dan@spiderx-test.ai", "member", invited_by=alice["id"])
    chk("82_decline", await db.decline_invite(inv2["token"]) is True)
    chk("83_decline_idempotent", await db.decline_invite(inv2["token"]) is False)
    p2 = await db.get_invite_by_token(inv2["token"])
    chk("84_declined_invite_has_timestamp", p2["declined_at"] is not None)

    # ── revoke ──
    inv3 = await db.create_invite(alice_org, "eve@spiderx-test.ai", "member", invited_by=alice["id"])
    chk("85_revoke_invite", await db.revoke_invite(inv3["id"], alice_org) is True)
    chk("86_revoke_wrong_org_fails", await db.revoke_invite(inv3["id"], bob_org) is False)
    chk("87_revoke_already_revoked", await db.revoke_invite(inv3["id"], alice_org) is False)

    # ── pending excludes finalised ──
    pending2 = await db.list_pending_invites(alice_org)
    chk("88_pending_excludes_finalised", len(pending2) == 0,
        f"got {len(pending2)} after accept/decline/revoke")

    # ── remove member ──
    chk("89_remove_member", await db.remove_org_member(alice_org, bob["id"]) is True)
    chk("90_remove_member_idempotent", await db.remove_org_member(alice_org, bob["id"]) is False)
    chk("91_count_owners_after_remove", await db.count_owners(alice_org) == 1)

    # ── agent ON DELETE CASCADE → all child rows in member-shared org ──
    # Verify that creating new agent inside alice's org → carol (member) can see it.
    fresh = await db.create_agent({"name": "After Carol", "system_prompt": "x"},
                                    user_id=alice["id"], org_id=alice_org)
    chk("92_carol_sees_new_agent",
        any(x["id"] == fresh["id"] for x in await db.list_agents(carol["id"])))

    # ── cleanup: delete agents to confirm CASCADE works even with member rows ──
    for aa in await db.list_agents_for_org(alice_org):
        await db.delete_agent(aa["id"])
    chk("93_org_emptied_cleanly", len(await db.list_agents_for_org(alice_org)) == 0)

    # ── ID-overlap probe: bob's org still standalone after he was removed ──
    chk("94_bob_org_intact_after_removal",
        (await db.get_org_for_user(bob["id"]))["id"] == bob_org)

    # ── role validation on update ──
    raised = False
    try: await db.update_member_role(alice_org, carol["id"], "BOGUS")
    except ValueError: raised = True
    chk("95_update_role_bad_value", raised)

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 3 — super_admins + audit_log + admin queries               ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # The migration seeded founder as super_admin. Verify and exercise.
    chk("96_founder_is_super_admin", await db.is_super_admin(founder["id"]) is True)
    chk("97_alice_not_super_admin", await db.is_super_admin(alice["id"]) is False)

    sa_list = await db.list_super_admins()
    chk("98_list_super_admins_seeded",
        len(sa_list) == 1 and sa_list[0]["user_id"] == founder["id"],
        repr(sa_list)[:160])

    chk("99_count_super_admins", await db.count_super_admins() == 1)

    granted = await db.grant_super_admin(alice["id"], granted_by=founder["id"])
    chk("100_grant_super_admin", granted is True)
    chk("101_alice_now_admin", await db.is_super_admin(alice["id"]) is True)
    chk("102_count_grew", await db.count_super_admins() == 2)

    # Idempotent re-grant
    chk("103_regrant_idempotent", await db.grant_super_admin(alice["id"], granted_by=founder["id"]) is False)

    revoked = await db.revoke_super_admin(alice["id"])
    chk("104_revoke", revoked is True)
    chk("105_alice_revoked", await db.is_super_admin(alice["id"]) is False)
    chk("106_revoke_idempotent", await db.revoke_super_admin(alice["id"]) is False)

    # audit log
    audit_id = await db.write_audit(
        actor_id=founder["id"], action="plan.override",
        target_kind="org", target_id=str(alice_org),
        diff={"new_plan": "pro", "before": "free"}, ip="127.0.0.1", user_agent="probe",
    )
    chk("107_write_audit", audit_id > 0)

    entries = await db.list_audit(limit=10)
    chk("108_list_audit", len(entries) >= 1 and entries[0]["action"] == "plan.override",
        f"got {len(entries)}, last action={entries[0]['action'] if entries else None}")
    chk("109_audit_actor_email_joined",
        entries[0].get("actor_email") == db.FOUNDER_EMAIL,
        repr(entries[0].get("actor_email")))
    chk("110_audit_diff_jsonb_round_trip",
        entries[0]["diff"]["new_plan"] == "pro")

    # Append another with a different actor & target so filter probes are meaningful
    bob_for_audit = await db.get_user_by_email("bob@spiderx-test.ai")
    await db.write_audit(
        actor_id=bob_for_audit["id"], action="agent.delete",
        target_kind="agent", target_id="42",
        diff={"deleted": True},
    )
    by_actor = await db.list_audit(actor_id=founder["id"])
    chk("111_audit_filter_by_actor", all(e["actor_id"] == founder["id"] for e in by_actor),
        f"got {len(by_actor)}")

    by_kind = await db.list_audit(target_kind="agent")
    chk("112_audit_filter_by_kind", all(e["target_kind"] == "agent" for e in by_kind),
        f"got {len(by_kind)}")

    by_target = await db.list_audit(target_kind="org", target_id=str(alice_org))
    chk("113_audit_filter_by_target", len(by_target) == 1)

    # ── admin cross-org queries ──
    orgs_admin = await db.admin_list_orgs()
    chk("114_admin_list_orgs",
        len(orgs_admin) >= 2 and all("members_count" in o for o in orgs_admin),
        f"got {len(orgs_admin)}, fields={list(orgs_admin[0].keys())[:6]}")

    users_admin = await db.admin_list_users()
    chk("115_admin_list_users",
        len(users_admin) >= 4 and all("is_super_admin" in u for u in users_admin),
        f"got {len(users_admin)}")

    users_search = await db.admin_list_users(search="alice")
    chk("116_admin_users_search",
        len(users_search) == 1 and "alice" in users_search[0]["email"],
        f"got {len(users_search)}")

    chk("117_admin_users_search_no_match",
        await db.admin_list_users(search="nonexistent-user-xyz") == [])

    summary = await db.admin_platform_summary()
    chk("118_admin_summary_shape",
        all(k in summary for k in ("users_count","orgs_count","agents_count",
                                    "calls_count","minutes_total","input_tokens_total",
                                    "output_tokens_total","cost_paise_total")),
        f"keys={list(summary.keys())}")

    chk("119_admin_summary_users_count",
        int(summary["users_count"]) == len(users_admin))

    # admin_recent_calls — should be empty after Phase-1 cleanup
    recent_calls = await db.admin_recent_calls(limit=10)
    chk("120_admin_recent_calls_shape",
        isinstance(recent_calls, list),
        f"type={type(recent_calls).__name__}")

    # Seed a call so we can verify the join + token columns flow through
    seeded = await db.create_agent({"name": "Admin Probe", "system_prompt": "x"},
                                     user_id=alice["id"], org_id=alice_org)
    await db.insert_call({
        "agent_id": seeded["id"], "duration_s": 9.9, "outcome": "Lead",
        "summary": "audit probe", "input_tokens": 100, "output_tokens": 30,
        "model_id": "gemini-2.0-flash-live-001", "cost_paise": 5,
    })
    recent2 = await db.admin_recent_calls(limit=5)
    last = recent2[0] if recent2 else {}
    chk("121_admin_recent_calls_join",
        last.get("agent_name") == "Admin Probe" and last.get("org_name") == "Alice's workspace",
        repr({"a": last.get("agent_name"), "o": last.get("org_name")}))
    chk("122_admin_calls_token_cols",
        last.get("input_tokens") == 100 and last.get("output_tokens") == 30
        and last.get("cost_paise") == 5)

    # Plan override pattern (the admin route's pattern) — verify member listing
    # works as expected. alice's org has alice (owner) + carol (member, joined
    # via accept_invite in probe 78), so two rows.
    org_members_for_alice = await db.list_org_members(alice_org)
    chk("123_admin_org_member_listing",
        len(org_members_for_alice) == 2
        and {m["user_id"] for m in org_members_for_alice} == {alice["id"], carol["id"]},
        f"got {len(org_members_for_alice)}, users={[m['user_id'] for m in org_members_for_alice]}")

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 4 — platform_settings + read-through cache                 ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # Settings live behind a cache module (backend/settings.py) but the raw
    # db helpers are tested independently here.
    settings = await db.list_platform_settings()
    # Seed evolved over phases: Phase 4 seeded 12 keys, Phase 6 added 2
    # rate-limit knobs. Assert "we have at least the canonical set" rather
    # than a brittle count match.
    chk("124_settings_seeded",
        len(settings) >= 12 and {s["key"] for s in settings} >= {
            "models.builder_model_id", "limits.free_minutes_per_month",
            "features.signups_open", "branding.support_email"
        },
        f"got {len(settings)} keys")

    chk("125_settings_category_filter",
        len(await db.list_platform_settings(category="models")) == 3)

    chk("126_settings_category_filter_empty",
        await db.list_platform_settings(category="bogus") == [])

    one = await db.get_platform_setting("limits.free_minutes_per_month")
    chk("127_get_one_setting", one and one["value"] == 30, repr(one)[:120])
    chk("128_get_missing_setting",
        await db.get_platform_setting("nonsense.key") is None)

    upd = await db.set_platform_setting("limits.free_minutes_per_month", 60,
                                          updated_by=founder["id"])
    chk("129_set_setting", upd and upd["value"] == 60, repr(upd)[:120])

    chk("130_setting_updated_by_stamped", upd["updated_by"] == founder["id"])

    chk("131_set_unknown_returns_none",
        await db.set_platform_setting("nonsense.key", "x", founder["id"]) is None)

    # Object values round-trip through JSONB cleanly
    obj_upd = await db.set_platform_setting(
        "branding.brand_palette", {"primary": "#abcdef", "accent": "#fedcba"},
        updated_by=founder["id"],
    )
    chk("132_setting_jsonb_object",
        obj_upd["value"]["primary"] == "#abcdef")

    # Read-through cache module
    from backend import settings as cfg  # local import — needs db pool warmed
    cfg.invalidate()
    v = await cfg.get("limits.free_minutes_per_month")
    chk("133_cache_get_after_set", v == 60, f"got {v!r}")

    chk("134_cache_default_for_missing",
        await cfg.get("not.in.table", "fallback") == "fallback")

    # `cfg.set` invalidates and persists
    diff = await cfg.set("limits.free_minutes_per_month", 90, updated_by=founder["id"])
    chk("135_cache_set_returns_diff",
        diff["before"] == 60 and diff["after"] == 90)
    chk("136_cache_get_after_immediate_set",
        await cfg.get("limits.free_minutes_per_month") == 90)

    # get_many for the admin UI — returns full metadata. Phase 4 seeded 4
    # limits keys, Phase 6 added 2 more (rate_capacity + rate_window_s),
    # giving 6 in the canonical limits set.
    panel = await cfg.get_many(category="limits")
    chk("137_cache_get_many",
        len(panel) >= 4 and all("label" in p and "description" in p for p in panel),
        f"got {len(panel)} limits keys")

    # Restore + verify
    await cfg.set("limits.free_minutes_per_month", 30, updated_by=founder["id"])
    await cfg.set("branding.brand_palette",
                   {"primary": "#a78bfa", "accent": "#2563eb"},
                   updated_by=founder["id"])
    chk("138_settings_restored",
        await cfg.get("limits.free_minutes_per_month") == 30)

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 5 — analytics rollups + token cost                         ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # Pricing pure-function unit test
    from backend import pricing
    chk("139_pricing_known_model",
        pricing.cost_paise("gemini-3.1-flash-live-preview", 1_000_000, 1_000_000) > 0)
    chk("140_pricing_missing_model",
        pricing.cost_paise("nonexistent-model", 1000, 500) == 0)
    chk("141_pricing_missing_tokens",
        pricing.cost_paise("gemini-3.1-flash-live-preview", None, 500) == 0)

    # Fresh agent for clean rollups (audit DB has data from earlier probes)
    smoke = await db.create_agent({"name": "Phase5 Smoke", "system_prompt": "x"},
                                    user_id=alice["id"], org_id=alice_org)
    # 5 calls with explicit tokens — verify rollups update atomically
    for outcome in ["Lead", "Lead", "Demo booked", "Lead", None]:
        await db.insert_call({
            "agent_id": smoke["id"], "duration_s": 60.0, "outcome": outcome,
            "input_tokens": 1000, "output_tokens": 200,
            "model_id": "gemini-3.1-flash-live-preview",
        })

    # agent_analytics shape
    aa = await db.agent_analytics(smoke["id"], days=30)
    chk("142_agent_analytics_totals",
        aa["totals"]["calls"] == 5 and float(aa["totals"]["minutes"]) == 5.0,
        repr(aa["totals"]))

    # Tokens summed correctly
    chk("143_agent_analytics_tokens",
        aa["totals"]["input_tokens"] == 5000 and aa["totals"]["output_tokens"] == 1000)

    # Cost computed via pricing (≈ 5 × (1000 × 0.40/1M + 200 × 1.60/1M) USD × 83.5 × 100 paise)
    expected_cost = pricing.cost_paise("gemini-3.1-flash-live-preview", 1000, 200) * 5
    chk("144_agent_analytics_cost",
        abs(int(aa["totals"]["cost_paise"]) - expected_cost) <= 5,
        f"got {aa['totals']['cost_paise']}, expected ~{expected_cost}")

    # Outcome distribution from JSONB jsonb_set accumulation
    by = {r["outcome"]: r["count"] for r in aa["by_outcome"]}
    chk("145_agent_analytics_outcomes",
        by.get("Lead") == 3 and by.get("Demo booked") == 1 and by.get("unknown") == 1,
        repr(by))

    # Org rollup picks the same numbers
    oa = await db.org_analytics(alice_org, days=30)
    chk("146_org_analytics_totals", oa["totals"]["calls"] >= 5,
        f"got {oa['totals']['calls']}")
    chk("147_org_analytics_top_agents",
        any(a["id"] == smoke["id"] for a in oa["top_agents"]))

    # Platform analytics aggregates across orgs
    pa = await db.platform_analytics(days=30)
    chk("148_platform_analytics_totals", pa["totals"]["calls"] >= 5)
    chk("149_platform_by_org_includes_alice",
        any(o["id"] == alice_org for o in pa["by_org"]))

    # `days=0` is "since today" per the SQL WHERE clause (`day >= current_date
    # - 0d`), so today's calls are included. The HTTP route clamps with
    # `max(1, …)` so callers never pass 0 anyway — but the db-layer
    # contract is what we lock in here.
    aa_today = await db.agent_analytics(smoke["id"], days=0)
    chk("150_analytics_zero_days_returns_today",
        aa_today["totals"]["calls"] == 5,
        repr(aa_today["totals"]))

    # CASCADE: deleting an agent wipes its rollup rows
    await db.delete_agent(smoke["id"])
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM agent_daily_stats WHERE agent_id = $1",
                                 smoke["id"])
    chk("151_rollup_cascade_on_delete", n == 0, f"leftover {n}")

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 6 — hardening: rate limit + email abstraction              ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # Rate-limit settings exist
    chk("152_rate_settings_seeded",
        await db.get_platform_setting("limits.rate_capacity") is not None
        and await db.get_platform_setting("limits.rate_window_s") is not None)

    # Token-bucket pure logic (no HTTP). Capacity=3, refill window=60s →
    # 3 immediate requests pass, 4th raises 429.
    from backend import ratelimit
    from fastapi import HTTPException
    ratelimit.reset()
    # Force the cache to see tiny capacity for this probe.
    await cfg.set("limits.rate_capacity", 3, updated_by=founder["id"])
    await cfg.set("limits.rate_window_s", 60, updated_by=founder["id"])
    cfg.invalidate()

    org_for_limit = alice_org
    ok_count, raised = 0, False
    for _ in range(3):
        await ratelimit.acquire(org_for_limit)
        ok_count += 1
    try:
        await ratelimit.acquire(org_for_limit)
    except HTTPException as e:
        if e.status_code == 429:
            raised = True
    chk("153_ratelimit_burst_caps",
        ok_count == 3 and raised,
        f"ok={ok_count} raised_429={raised}")

    # org_id=None bypasses the limit (super-admin / pre-auth paths)
    try:
        await ratelimit.acquire(None)
        chk("154_ratelimit_none_bypasses", True)
    except HTTPException:
        chk("154_ratelimit_none_bypasses", False, "should have bypassed")

    # Restore defaults
    await cfg.set("limits.rate_capacity", 60, updated_by=founder["id"])
    await cfg.set("limits.rate_window_s", 60, updated_by=founder["id"])
    ratelimit.reset()

    # Email stub never raises even when provider fails
    from backend import email_stub
    # Default provider is 'log' — call shouldn't raise
    raised_email = False
    try:
        await email_stub.send_invite_email(
            to="bogus@example.com", inviter_name="Test",
            org_name="Test Org", role="member", token="dummy",
        )
    except Exception:
        raised_email = True
    chk("155_email_stub_never_raises", not raised_email)

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║ PHASE 7 — universal LLM ledger                                   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    # Prior probes (Phase 1 + Phase 3 + Phase 5) seed agent calls that
    # also write llm_calls rows via insert_call. Wipe the ledger before
    # this probe block so counts assert exact contributions.
    pool0 = await db.get_pool()
    async with pool0.acquire() as conn:
        await conn.execute("DELETE FROM llm_calls")

    # Fresh agent (alice's org) for a clean ledger
    ledger_agent = await db.create_agent({"name": "Ledger Audit", "system_prompt": "x"},
                                           user_id=alice["id"], org_id=alice_org)

    # Agent calls: each insert_call writes calls + llm_calls('agent') in
    # one txn.
    for outcome in ["Lead", "Lead", "Demo booked"]:
        await db.insert_call({
            "agent_id": ledger_agent["id"], "duration_s": 60.0, "outcome": outcome,
            "input_tokens": 2000, "output_tokens": 500,
            "model_id": "gemini-3.1-flash-live-preview",
        })

    # Builder sessions: independent insert_llm_call writes
    for mins in (1.5, 2.5):
        await db.insert_llm_call({
            "kind": "builder", "user_id": alice["id"], "org_id": alice_org,
            "duration_s": mins * 60.0,
            "input_tokens": int(mins * 1500),
            "output_tokens": int(mins * 400),
            "model_id": "gemini-3.1-flash-live-preview",
        })

    # Universal ledger captures both
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        agent_n = await conn.fetchval(
            "SELECT COUNT(*) FROM llm_calls WHERE kind='agent' AND org_id=$1", alice_org)
        builder_n = await conn.fetchval(
            "SELECT COUNT(*) FROM llm_calls WHERE kind='builder' AND org_id=$1", alice_org)
        linked = await conn.fetchval(
            "SELECT COUNT(*) FROM llm_calls WHERE call_id IS NOT NULL AND org_id=$1", alice_org)
    chk("156_ledger_agent_rows", agent_n == 3, f"got {agent_n}")
    chk("157_ledger_builder_rows", builder_n == 2, f"got {builder_n}")
    chk("158_ledger_agent_rows_linked_to_calls", linked == 3,
        f"got {linked} (each 'agent' row should reference its calls row)")

    # Analytics surface both kinds, with cost-per-minute computed
    org_ledger = await db.llm_analytics_for_org(alice_org, days=30)
    chk("159_org_ledger_totals_count",
        int(org_ledger["totals"]["sessions"]) == 5,
        f"got {org_ledger['totals']['sessions']}")
    chk("160_org_ledger_by_kind_present",
        len(org_ledger["by_kind"]) == 2
        and {r["kind"] for r in org_ledger["by_kind"]} == {"agent", "builder"},
        repr([r["kind"] for r in org_ledger["by_kind"]]))
    chk("161_org_ledger_cost_per_minute_computed",
        org_ledger["totals"].get("cost_per_minute_paise") is not None
        and float(org_ledger["totals"]["cost_per_minute_paise"]) > 0)

    # Platform ledger sums across orgs
    plat_ledger = await db.llm_analytics_platform(days=30)
    chk("162_platform_ledger_includes_org_data",
        int(plat_ledger["totals"]["sessions"]) >= 5)

    # Generated cost_per_minute_paise column on the row itself. Filter to
    # rows with both duration AND cost > 0 — zero-cost rows (calls inserted
    # without model_id) generate cpm=0 by design, which is correct but
    # doesn't validate the formula.
    async with pool.acquire() as conn:
        cpm = await conn.fetchval(
            "SELECT cost_per_minute_paise FROM llm_calls "
            "WHERE org_id=$1 AND duration_s > 0 AND cost_paise > 0 "
            "ORDER BY id DESC LIMIT 1", alice_org)
    chk("163_generated_cost_per_minute_column", cpm is not None and float(cpm) > 0,
        f"got cpm={cpm}")

    # Reject bad kind on insert
    raised = False
    try:
        await db.insert_llm_call({"kind": "tinder", "duration_s": 1})
    except ValueError:
        raised = True
    chk("164_insert_llm_bad_kind_rejected", raised)

    # CASCADE on agent delete clears its llm_calls rows
    await db.delete_agent(ledger_agent["id"])
    async with pool.acquire() as conn:
        # ON DELETE SET NULL on llm_calls.agent_id — rows survive but agent_id
        # nulls out. The call_id FK with SET NULL means the calls row also
        # cascades and the FK nulls cleanly. Verify rows persist (ledger
        # entries are append-only finance data) but agent_id and call_id
        # are now NULL.
        leftover_for_agent = await conn.fetchval(
            "SELECT COUNT(*) FROM llm_calls WHERE agent_id = $1", ledger_agent["id"])
        nulled_agent = await conn.fetchval(
            "SELECT COUNT(*) FROM llm_calls WHERE kind='agent' AND agent_id IS NULL")
    chk("165_ledger_survives_agent_delete", leftover_for_agent == 0,
        f"agent_id={ledger_agent['id']} still has {leftover_for_agent} rows")
    chk("166_ledger_agent_id_set_null_on_cascade", nulled_agent >= 3,
        f"got {nulled_agent} agent-kind rows with NULL agent_id")

    await db.close_pool()
    return _report(R)


def _report(R: dict[str, str]) -> int:
    print("\n=== ALL DB OPS AUDIT — PHASE 1 + PHASE 2 ===")
    passed = sum(1 for v in R.values() if v == "PASS")
    total = len(R)
    failures = []
    for k in sorted(R):
        line = f"{k}: {R[k]}"
        print(line)
        if not R[k].startswith("PASS"):
            failures.append(line)
    print(f"\nTOTAL: {passed}/{total}")
    if failures:
        print(f"\nFAILURES:\n  " + "\n  ".join(failures))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
