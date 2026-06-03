"""Async DB facade.

Single backend: Postgres via asyncpg in `db_pg.py`. The legacy SQLite
path was retired at Phase 6 — `data/eva.db.snapshot.*` files are kept
as cold rollback artifacts but the code path is gone.

Every function here is `async def` — callers MUST `await` them. The
file deliberately stays as a thin re-export shim so app.py can keep its
existing `from . import db` and call `await db.list_agents(...)`
unchanged. The shim layer survives a future move to Cloud SQL,
Supabase, AlloyDB, or a different driver — those swaps only touch
db_pg, never the call sites.

Pool lifecycle: `init()` warms the asyncpg pool on FastAPI startup,
`shutdown()` drains it. Both are wired into app.py's @on_event hooks.
"""
from __future__ import annotations

from typing import Any, Optional

from . import db_pg


def backend() -> str:
    return "pg"


# Pure-Python helpers re-exported for callers that need them without await.
FOUNDER_EMAIL = db_pg.FOUNDER_EMAIL
FOUNDER_NAME = db_pg.FOUNDER_NAME
_slugify = db_pg._slugify


# ─── lifecycle ───────────────────────────────────────────────────────────

async def init() -> None:
    await db_pg.init()


async def shutdown() -> None:
    await db_pg.close_pool()


# ─── users ───────────────────────────────────────────────────────────────

get_user            = db_pg.get_user
get_user_by_email   = db_pg.get_user_by_email
create_user         = db_pg.create_user
get_founder         = db_pg.get_founder


# ─── orgs ────────────────────────────────────────────────────────────────

get_org_for_user      = db_pg.get_org_for_user
update_org_for_user   = db_pg.update_org_for_user


# ─── plans ───────────────────────────────────────────────────────────────

list_plans            = db_pg.list_plans
get_plan              = db_pg.get_plan
get_plan_by_slug      = db_pg.get_plan_by_slug
get_user_plan_state   = db_pg.get_user_plan_state
set_user_plan         = db_pg.set_user_plan


# ─── agents ──────────────────────────────────────────────────────────────

create_agent          = db_pg.create_agent
get_agent             = db_pg.get_agent
get_agent_by_slug     = db_pg.get_agent_by_slug
delete_agent          = db_pg.delete_agent
update_agent          = db_pg.update_agent
list_agents           = db_pg.list_agents
list_agents_for_org   = db_pg.list_agents_for_org
get_agent_org         = db_pg.get_agent_org


# ─── calls ───────────────────────────────────────────────────────────────

insert_call               = db_pg.insert_call
list_calls_for_agent      = db_pg.list_calls_for_agent
get_call_detail           = db_pg.get_call_detail
call_stats_for_agent      = db_pg.call_stats_for_agent


# ─── number_requests ─────────────────────────────────────────────────────

list_number_requests_for_agent  = db_pg.list_number_requests_for_agent
create_number_request           = db_pg.create_number_request


# ─── teams (Phase 2) ─────────────────────────────────────────────────────

list_org_members      = db_pg.list_org_members
get_member_role       = db_pg.get_member_role
add_org_member        = db_pg.add_org_member
update_member_role    = db_pg.update_member_role
remove_org_member     = db_pg.remove_org_member
count_owners          = db_pg.count_owners
list_orgs_for_user    = db_pg.list_orgs_for_user

create_invite         = db_pg.create_invite
get_invite_by_token   = db_pg.get_invite_by_token
list_pending_invites  = db_pg.list_pending_invites
accept_invite         = db_pg.accept_invite
decline_invite        = db_pg.decline_invite
revoke_invite         = db_pg.revoke_invite


# ─── super admin + audit (Phase 3) ───────────────────────────────────────

is_super_admin            = db_pg.is_super_admin
list_super_admins         = db_pg.list_super_admins
grant_super_admin         = db_pg.grant_super_admin
revoke_super_admin        = db_pg.revoke_super_admin
count_super_admins        = db_pg.count_super_admins
write_audit               = db_pg.write_audit
list_audit                = db_pg.list_audit
admin_list_orgs           = db_pg.admin_list_orgs
admin_list_users          = db_pg.admin_list_users
admin_recent_calls        = db_pg.admin_recent_calls
admin_platform_summary    = db_pg.admin_platform_summary


# ─── platform settings (Phase 4) ─────────────────────────────────────────

list_platform_settings    = db_pg.list_platform_settings
get_platform_setting      = db_pg.get_platform_setting
set_platform_setting      = db_pg.set_platform_setting


# ─── analytics (Phase 5) ─────────────────────────────────────────────────

agent_analytics       = db_pg.agent_analytics
org_analytics         = db_pg.org_analytics
platform_analytics    = db_pg.platform_analytics


# ─── llm ledger (Phase 7) ────────────────────────────────────────────────

insert_llm_call             = db_pg.insert_llm_call
llm_analytics_for_org       = db_pg.llm_analytics_for_org
llm_analytics_platform      = db_pg.llm_analytics_platform


# ─── build_sessions (durable Eva build state) ────────────────────────────

get_build_session              = db_pg.get_build_session
merge_build_facts              = db_pg.merge_build_facts
mark_build_committed           = db_pg.mark_build_committed
abandon_stale_build_sessions   = db_pg.abandon_stale_build_sessions
append_transcript_turn         = db_pg.append_transcript_turn
get_helper_memory              = db_pg.get_helper_memory
append_helper_turns            = db_pg.append_helper_turns
set_helper_summary             = db_pg.set_helper_summary
seed_helper_memory             = db_pg.seed_helper_memory
bump_extraction_count          = db_pg.bump_extraction_count
set_build_template             = db_pg.set_build_template
record_template_answer         = db_pg.record_template_answer
