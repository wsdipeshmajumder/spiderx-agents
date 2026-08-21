"""MCP — SpiderX Voice as a tool.

This app is a product in its own right, and it is also about to be *one
capability inside a larger agent orchestration platform*. Model Context Protocol
is the wire for that: the platform's agents call these tools the same way they
call any other, and nothing about the orchestration layer needs to know what a
Gemini Live session is.

**Transport.** JSON-RPC 2.0 over a single HTTP endpoint (`POST /mcp`) —
Streamable HTTP. Implemented directly rather than pulled in as a dependency:
the surface an MCP server actually needs is `initialize`, `tools/list` and
`tools/call`, this app already speaks HTTP and JSON, and a protocol seam is a
bad place to inherit somebody else's release cadence.

**Authentication is the app's, not a second one.** Every call resolves a user
through the same `current_user` the REST routes use, and every tool that names
an agent goes through the same `require_agent_member` / `require_agent_admin`.
An orchestrator holding a token can do exactly what that user can do in the UI
— no more, and specifically not "whatever the MCP server was configured with".

**What is deliberately not exposed.** Nothing that mutates an agent's
configuration, and nothing that reads carrier secrets. An orchestrator should be
able to *use* a voice agent — place a call, read what happened, ask what it
knows — and should not be able to silently rewrite the agent underneath the
person who built it. Editing stays in the product that owns it.

**Placing a call is the one tool that acts on the world**, so it is annotated
`destructiveHint` and requires admin on the agent. The orchestration platform's
own pre-contact gate is expected to run before it calls this; that gate is not
this server's job, and this server does not pretend to be one.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("eva.mcp")
router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "spiderx-voice"
SERVER_VERSION = "1.0.0"

# JSON-RPC error codes. -32602 and friends are the spec's; -32000 is the
# reserved implementation-defined range, which is where "you may not do that"
# belongs — it is not a malformed request.
PARSE_ERROR, INVALID_REQUEST = -32700, -32600
METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32601, -32602, -32603
NOT_PERMITTED = -32000

_app = None          # late-bound in _bind, see the note there


def _bind(app_module) -> APIRouter:
    """Take the app's auth helpers rather than importing them.

    `app.py` imports this module, so importing back would be circular. Passing
    the module in also makes the dependency explicit: this server has exactly
    one thing it needs from the app, and it is the permission model.
    """
    global _app
    _app = app_module
    return router


# ─── tool definitions ────────────────────────────────────────────────────
#
# Descriptions are written for a model that has never seen this product. Each
# says what the tool does, what it costs, and — where it matters — what it will
# refuse. A tool description that only names its parameters makes the caller
# guess, and a guessing orchestrator places phone calls.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_voice_agents",
        "description":
            "List the phone agents this account can use. Each has an id, a "
            "name, the sector and language it was built for, and whether it is "
            "published (a published agent has a live number and can take real "
            "calls). Call this first — every other tool needs an agent_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "published_only": {
                    "type": "boolean",
                    "description": "Only agents that are live on a number.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "List voice agents"},
    },
    {
        "name": "get_voice_agent",
        "description":
            "Everything about one phone agent: its persona, greeting, the "
            "languages it speaks, what it can do on a call (its connectors), "
            "the outcomes it records, and its guardrails. Use this to decide "
            "whether an agent is the right one for a task before calling it.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "integer"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Describe a voice agent"},
    },
    {
        "name": "place_call",
        "description":
            "Have a phone agent call a number and hold the conversation. This "
            "dials a real telephone and costs real money, so it is the one "
            "tool here that acts on the world.\n\n"
            "It refuses if the agent has no carrier that can originate calls, "
            "if the number is not in international format, or if the caller "
            "lacks admin on the agent. It does NOT check consent, suppression "
            "lists, quiet hours or whether this person has already been called "
            "today — the calling platform is expected to have decided all of "
            "that before it gets here.\n\n"
            "Returns a call_id. The conversation is asynchronous: poll "
            "get_call_outcome to find out what happened.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "to": {
                    "type": "string",
                    "description": "International format, e.g. +918031321199.",
                },
                "context": {
                    "type": "object",
                    "description":
                        "Facts the agent should have on the call — the "
                        "person's name, what they enquired about, a booking "
                        "reference. Merged into the agent's variables for this "
                        "call only.",
                },
            },
            "required": ["agent_id", "to"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": False, "openWorldHint": True,
                        "title": "Place a phone call"},
    },
    {
        "name": "get_call_outcome",
        "description":
            "What happened on one call: whether it connected, how long it ran, "
            "the outcome the agent recorded (booked, callback requested, "
            "escalated…), and anything it extracted — a name, a date, a party "
            "size. Poll this after place_call. A call still in progress returns "
            "status 'running' with no outcome yet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "call_id": {"type": "integer"},
            },
            "required": ["agent_id", "call_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Read a call outcome"},
    },
    {
        "name": "recent_calls",
        "description":
            "The most recent calls for one agent, newest first, with their "
            "outcomes. Use this to answer questions about what an agent has "
            "been doing rather than polling individual calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "Default 20, max 100."},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Recent calls"},
    },
    {
        "name": "ask_agent_knowledge",
        "description":
            "Ask what a phone agent knows — its hours, services, prices, "
            "policies, the things it would tell a caller. Answers from the "
            "agent's own knowledge rather than from the model's guess, and "
            "says so plainly when the agent does not know. Use this instead of "
            "placing a call when you only need a fact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "question": {"type": "string"},
            },
            "required": ["agent_id", "question"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "title": "Ask what an agent knows"},
    },
]


# ─── tool implementations ────────────────────────────────────────────────
async def _user(request: Request) -> dict:
    return await _app.current_user(request)


async def t_list_voice_agents(request: Request, args: dict) -> dict:
    from . import db_pg
    user = await _user(request)
    agents = await db_pg.list_agents(user["id"])
    if args.get("published_only"):
        agents = [a for a in agents if a.get("published")]
    return {
        "agents": [
            {"agent_id": a["id"], "name": a["name"], "slug": a.get("slug"),
             "sector": a.get("sector"), "locale": a.get("locale"),
             "published": bool(a.get("published")),
             "greeting": a.get("greeting")}
            for a in agents
        ],
        "count": len(agents),
    }


async def t_get_voice_agent(request: Request, args: dict) -> dict:
    user = await _user(request)
    a = await _app._require_agent_owned(int(args["agent_id"]), user)
    return {
        "agent_id": a["id"], "name": a["name"], "sector": a.get("sector"),
        "locale": a.get("locale"), "persona": a.get("persona"),
        "greeting": a.get("greeting"), "voice": a.get("voice"),
        "published": bool(a.get("published")),
        "can_do_on_a_call": a.get("connectors") or [],
        "records_outcomes": a.get("outcomes") or [],
        "guardrails": a.get("guardrails") or [],
        # variables carry transfer numbers, prices and addresses — operator IP.
        # An orchestrator gets the shape, never the contents.
        "knows_about": sorted((a.get("variables") or {}).keys()),
    }


_E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


async def t_place_call(request: Request, args: dict) -> dict:
    to = str(args.get("to") or "").strip()
    if not _E164.match(to):
        raise ToolError(
            f"{to!r} is not a phone number in international format. "
            f"Use e.g. +918031321199.", INVALID_PARAMS)
    raise ToolError(
        "place_call is defined but not wired to the telephony path yet. "
        "The REST route it will call is POST /api/agents/{id}/telephony/"
        "outbound-call; this tool must go through the same "
        "`_require_agent_admin` check and the same carrier lookup, and that "
        "has not been written or tested. Refusing rather than half-dialling.",
        INTERNAL)


async def t_get_call_outcome(request: Request, args: dict) -> dict:
    from . import db_pg
    user = await _user(request)
    aid = int(args["agent_id"])
    await _app._require_agent_owned(aid, user)
    # get_call_detail is already agent-scoped, so a call id from another
    # agent comes back as None rather than as somebody else's transcript
    call = await db_pg.get_call_detail(aid, int(args["call_id"]))
    if not call:
        raise ToolError(f"no call {args['call_id']} on that agent", INVALID_PARAMS)
    return _call_shape(call)


async def t_recent_calls(request: Request, args: dict) -> dict:
    from . import db_pg
    user = await _user(request)
    aid = int(args["agent_id"])
    await _app._require_agent_owned(aid, user)
    limit = max(1, min(int(args.get("limit") or 20), 100))
    calls = await db_pg.list_calls_for_agent(aid, limit=limit)
    return {"calls": [_call_shape(c) for c in calls], "count": len(calls)}


def _call_shape(c: dict) -> dict:
    """One call, as an orchestrator wants it.

    Deliberately not the whole row. `transcript` is the caller's words and
    belongs to the person who owns the product, not to whatever asked; the
    recording URL is a credentialled link; token counts and cost are the
    operator's commercials. An orchestrator needs to know what happened and
    what was learned, and that is all this returns.
    """
    return {
        "call_id": c.get("id"),
        "status": "completed" if c.get("ended_at") else "running",
        "started_at": str(c.get("started_at") or ""),
        "duration_seconds": (float(c["duration_s"])
                             if c.get("duration_s") is not None else None),
        "outcome": c.get("outcome"),
        "reason": c.get("reason"),
        "summary": c.get("summary"),
        "extracted": c.get("extracted") or {},
    }


async def t_ask_agent_knowledge(request: Request, args: dict) -> dict:
    user = await _user(request)
    a = await _app._require_agent_owned(int(args["agent_id"]), user)
    q = str(args.get("question") or "").strip()
    if not q:
        raise ToolError("ask what?", INVALID_PARAMS)
    variables = a.get("variables") or {}
    hits = {k: v for k, v in variables.items()
            if any(w in k.lower() or w in str(v).lower()
                   for w in q.lower().split() if len(w) > 3)}
    if not hits:
        return {"answer": None,
                "known": False,
                "note": f"{a['name']} has nothing on file about that. It would "
                        f"tell a caller it does not know rather than guess.",
                "knows_about": sorted(variables.keys())}
    return {"answer": hits, "known": True, "source": "the agent's own knowledge"}


HANDLERS: dict[str, Callable] = {
    "list_voice_agents": t_list_voice_agents,
    "get_voice_agent": t_get_voice_agent,
    "place_call": t_place_call,
    "get_call_outcome": t_get_call_outcome,
    "recent_calls": t_recent_calls,
    "ask_agent_knowledge": t_ask_agent_knowledge,
}


class ToolError(Exception):
    def __init__(self, message: str, code: int = INTERNAL):
        self.message, self.code = message, code
        super().__init__(message)


# ─── JSON-RPC ────────────────────────────────────────────────────────────
def _result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": e}


async def _dispatch(request: Request, msg: dict) -> Optional[dict]:
    """Handle one JSON-RPC message. Returns None for a notification."""
    rpc_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if msg.get("jsonrpc") != "2.0" or not method:
        return None if is_notification else _error(
            rpc_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")

    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions":
                "SpiderX Voice — phone agents that answer and place calls.\n\n"
                "Call list_voice_agents first; everything else needs an "
                "agent_id. Use ask_agent_knowledge when you need a fact, and "
                "place_call only when a person genuinely needs to be spoken "
                "to: it dials a real telephone and costs real money. This "
                "server does not check consent, suppression or quiet hours — "
                "decide those before you call it.",
        })

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(rpc_id, {})

    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _error(rpc_id, INVALID_PARAMS, f"no tool named {name!r}")
        try:
            out = await fn(request, args)
        except ToolError as e:
            # A tool that refused is not a protocol error — the model should
            # see the refusal and its reason, and be able to act on it.
            return _result(rpc_id, {
                "content": [{"type": "text", "text": e.message}],
                "isError": True,
            })
        except HTTPException as e:
            # The app's own refusals — 401 unauthenticated, 403 not a member of
            # that agent's org. These are protocol-level: the caller is not
            # allowed to make this call at all, which is different from a tool
            # that ran and declined. Reported without the exception class name,
            # because a JSON-RPC error message is read by a model.
            detail = e.detail
            msg = (detail.get("message") if isinstance(detail, dict)
                   else str(detail))
            code = NOT_PERMITTED if e.status_code in (401, 403) else INTERNAL
            return _error(rpc_id, code, msg or "not permitted",
                          {"httpStatus": e.status_code})
        except Exception as e:                       # noqa: BLE001
            log.exception("mcp tool %s failed", name)
            return _error(rpc_id, INTERNAL, f"{type(e).__name__}: {e}")
        return _result(rpc_id, {
            "content": [{"type": "text",
                         "text": json.dumps(out, indent=1, default=str)}],
            "structuredContent": out,
            "isError": False,
        })

    return None if is_notification else _error(
        rpc_id, METHOD_NOT_FOUND, f"unknown method {method!r}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """The MCP endpoint. One POST, JSON-RPC in, JSON-RPC out.

    Batches are accepted because the spec allows them; a batch of notifications
    correctly produces 202 with no body rather than an empty array, which some
    clients reject.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"),
                            status_code=400)

    if isinstance(body, list):
        out = [r for r in
               [await _dispatch(request, m) for m in body if isinstance(m, dict)]
               if r is not None]
        if not out:
            return JSONResponse(None, status_code=202)
        return JSONResponse(out)

    if not isinstance(body, dict):
        return JSONResponse(_error(None, INVALID_REQUEST, "expected an object"),
                            status_code=400)

    res = await _dispatch(request, body)
    if res is None:
        return JSONResponse(None, status_code=202)
    return JSONResponse(res)


@router.get("/mcp")
async def mcp_discovery():
    """A plain description, for a human pointing a platform at this URL.

    The protocol itself needs no GET; this exists so that opening the endpoint
    in a browser explains what it is instead of returning 405.
    """
    return {
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "transport": "streamable-http",
        "endpoint": "POST /mcp",
        "authentication":
            "the same as the REST API — send the caller's identity header. "
            "Tools run as that user and can do exactly what that user can do.",
        "tools": [{"name": t["name"],
                   "title": t.get("annotations", {}).get("title"),
                   "readOnly": t.get("annotations", {}).get("readOnlyHint", False)}
                  for t in TOOLS],
    }
