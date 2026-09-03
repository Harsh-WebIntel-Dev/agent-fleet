"""MCP server giving fleet agents a vectorised memory that never mixes clients.

WHY THIS EXISTS
LiteLLM cannot host this. Its `/v1/vector_stores` passthrough needs an OpenAI key we do not use,
its `pg_vector` provider is a config shim for a sidecar we do not run, and `/v1/memory` is a flat
key-value store with no embedding column. Every vector provider LiteLLM knows about is a remote
service. So LiteLLM stays what it is good at — models, keys, budgets, policy and the tool gateway —
and the vectors live in our own Postgres.

mem0 was the other candidate and was rejected deliberately: installing `mem0ai` into the Hermes venv
risks upgrading shared dependencies underneath a working fleet, bge-m3 is absent from its
`KNOWN_DIMS` so the dimension has to be hand-patched, and its read filter scopes on `user_id` only,
which would have needed patching anyway to get per-agent isolation.

TENANT ISOLATION: THREE LAYERS, DELIBERATELY REDUNDANT
1. The client slug is PINNED by the gateway-set `x-client-slug` header where one exists (LiteLLM
   `static_headers` on a per-client entry). A conflicting `client` argument is refused, not
   reconciled. Only the shared agency fleet — one key legitimately serving every client — falls back
   to the argument.
2. Every statement runs as the restricted `fleet_app` role (NOSUPERUSER, NOBYPASSRLS) with
   `app.client_id` set per transaction, so Postgres FORCED row-level security is the real boundary.
   A bug in layer 1 still cannot read another client's rows.
3. `agent_id` narrows recall to the agent that wrote it, unless the caller explicitly asks across
   the whole client.

Embeddings come from LiteLLM's `embed` alias (bge-m3, 1024 dims) so the tokens are still metered,
budgeted and policy-checked centrally.
"""

from __future__ import annotations

import os
import re
import json
from typing import Any

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from mcp.server.mcpserver import MCPServer, Context

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm-v10up2yg1cwxo0k1ks9j2qro:4000")
LITELLM_KEY = os.environ["LITELLM_KEY"]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embed")
EMBED_DIMS = int(os.environ.get("EMBED_DIMS", "1024"))

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")

MAX_CONTENT_CHARS = 8000
MAX_RESULTS = 25

mcp = MCPServer(
    name="memory",
    instructions=(
        "Long-term memory for fleet agents. Facts are stored per client and per agent and recalled "
        "by meaning, not keyword. You only ever see memories for the client you are working for."
    ),
)


# ---------------------------------------------------------------- identity resolution


def _one_header(ctx: Context, name: str) -> str:
    """Read exactly one instance of a gateway header, or none. Duplicates are refused because a
    repeated header is the classic injection shape — the gateway sets one, a caller adding another
    is trying to change who it is."""
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(req, "headers", None)
    vals = headers.getlist(name) if hasattr(headers, "getlist") else []
    if len(vals) > 1:
        raise ValueError(f"ambiguous request: multiple {name} headers")
    return vals[0].strip().lower() if (len(vals) == 1 and vals[0].strip()) else ""


def _resolve_client(ctx: Context, client: str = "") -> str:
    pinned = _one_header(ctx, "x-client-slug")
    asked = (client or "").strip().lower()
    if pinned:
        if asked and asked != pinned:
            raise ValueError(
                f"client mismatch: pinned to {pinned!r} but the call asked for {asked!r}"
            )
        slug = pinned
    elif asked:
        slug = asked
    else:
        raise ValueError("no client specified: pass `client`, or deploy behind an x-client-slug header")
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid client slug {slug!r}")
    return slug


def _resolve_agent(ctx: Context, agent: str = "") -> str:
    val = _one_header(ctx, "x-agent-id") or (agent or "").strip().lower()
    if not val:
        raise ValueError("no agent specified: pass `agent` (your profile name, e.g. 'writer')")
    if not AGENT_RE.match(val):
        raise ValueError(f"invalid agent id {val!r}")
    return val


# ---------------------------------------------------------------- storage


def _conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def _client_id(cur, slug: str) -> str:
    cur.execute("SELECT id FROM app.clients WHERE slug = %s AND active", (slug,))
    row = cur.fetchone()
    if not row:
        raise ValueError(
            f"unknown client {slug!r} — register it once with memory_register_client (onboarding), "
            f"then retry. If it was registered but is inactive, it has been deliberately disabled."
        )
    return row["id"] if isinstance(row, dict) else row[0]


def _scope(cur, client_id: str) -> None:
    """Arm row-level security for this transaction. Everything after this is invisible across
    clients even if a query forgets its WHERE."""
    cur.execute("SELECT set_config('app.client_id', %s, true)", (str(client_id),))


def _embed(text: str) -> list[float]:
    r = httpx.post(
        f"{LITELLM_URL}/v1/embeddings",
        headers={"Authorization": f"Bearer {LITELLM_KEY}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": text[:MAX_CONTENT_CHARS]},
        timeout=60.0,
    )
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"]
    if len(vec) != EMBED_DIMS:
        # Caught here rather than as an opaque Postgres cast error 3 frames deeper.
        raise ValueError(
            f"embedding is {len(vec)}-dimensional but the column is vector({EMBED_DIMS}); "
            f"model {EMBED_MODEL!r} does not match the schema"
        )
    return vec


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}


# ---------------------------------------------------------------- tools


@mcp.tool()
def memory_remember(ctx: Context, content: str, agent: str = "", client: str = "",
                    session_key: str = "general") -> dict[str, Any]:
    """Store one durable fact for later recall.

    Write things that will still be true and useful next week — a client's preferences, a decision
    and its reason, a constraint you discovered. Do NOT write transient chatter, or anything you can
    re-read from the ClickUp task.
    """
    try:
        slug = _resolve_client(ctx, client)
        who = _resolve_agent(ctx, agent)
        body = (content or "").strip()
        if not body:
            raise ValueError("content is required")
        if len(body) > MAX_CONTENT_CHARS:
            raise ValueError(f"content too long ({len(body)} chars, max {MAX_CONTENT_CHARS})")

        vec = _embed(body)
        with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cid = _client_id(cur, slug)
            _scope(cur, cid)
            cur.execute(
                "INSERT INTO memory.entries (client_id, session_key, agent_id, embedding, content) "
                "VALUES (%s, %s, %s, %s::vector, %s) RETURNING id",
                (cid, session_key or "general", who, vec, body),
            )
            new_id = cur.fetchone()["id"]
        return {"ok": True, "id": str(new_id), "client": slug, "agent": who,
                "stored_chars": len(body)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def memory_search(ctx: Context, query: str, limit: int = 5, agent: str = "", client: str = "",
                  across_agents: bool = False) -> dict[str, Any]:
    """Recall by meaning. Returns your own memories for this client, closest first.

    Set across_agents=True to search everything the fleet remembers for this client rather than only
    your own notes — useful for the PM, noisy for a specialist.
    """
    try:
        slug = _resolve_client(ctx, client)
        who = _resolve_agent(ctx, agent)
        q = (query or "").strip()
        if not q:
            raise ValueError("query is required")
        n = max(1, min(int(limit), MAX_RESULTS))

        vec = _embed(q)
        with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cid = _client_id(cur, slug)
            _scope(cur, cid)
            if across_agents:
                cur.execute(
                    "SELECT content, agent_id, created_at, "
                    "       1 - (embedding <=> %s::vector) AS similarity "
                    "FROM memory.entries WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (vec, vec, n),
                )
            else:
                cur.execute(
                    "SELECT content, agent_id, created_at, "
                    "       1 - (embedding <=> %s::vector) AS similarity "
                    "FROM memory.entries WHERE embedding IS NOT NULL AND agent_id = %s "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (vec, who, vec, n),
                )
            rows = cur.fetchall()
        return {
            "ok": True, "client": slug, "agent": who, "across_agents": across_agents,
            "count": len(rows),
            "results": [
                {"content": r["content"], "agent": r["agent_id"],
                 "similarity": round(float(r["similarity"]), 4),
                 "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def memory_stats(ctx: Context, client: str = "", agent: str = "") -> dict[str, Any]:
    """How many memories exist for this client, broken down by agent. Cheap sanity check."""
    try:
        slug = _resolve_client(ctx, client)
        with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cid = _client_id(cur, slug)
            _scope(cur, cid)
            cur.execute(
                "SELECT coalesce(agent_id,'(none)') AS agent_id, count(*) AS n "
                "FROM memory.entries GROUP BY 1 ORDER BY 2 DESC"
            )
            rows = cur.fetchall()
        return {"ok": True, "client": slug,
                "total": sum(int(r["n"]) for r in rows),
                "by_agent": {r["agent_id"]: int(r["n"]) for r in rows}}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def memory_register_client(ctx: Context, client: str = "", display_name: str = "") -> dict[str, Any]:
    """Register a client in fleet memory so agents can store and recall for it. Onboarding-time,
    idempotent, PM-facing.

    Call this ONCE for a new client before the first memory_remember/memory_search for that slug —
    the other tools refuse an unknown client on purpose. Registering only creates the client's row in
    the registry; it does NOT touch any other client's memories and the per-client isolation is
    unchanged. If the client already exists this is a no-op that returns its id (it will NOT
    re-activate a client that was deliberately deactivated, nor rename one). Behind a per-client key
    the slug is pinned, so you can only register your own; the shared agency fleet key (the PM) may
    register any new slug via the `client` argument. Setting up the per-client LiteLLM key/team is a
    separate onboarding step — this does not create one.
    """
    try:
        slug = _resolve_client(ctx, client)
        name = (display_name or "").strip() or slug
        if len(name) > 200:
            raise ValueError(f"display_name too long ({len(name)} chars, max 200)")
        with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Registry-only insert. app.clients has no RLS (it is the tenant list, not tenant data);
            # the isolation boundary is memory.entries' FORCED row-level security, untouched here.
            # ON CONFLICT keeps it idempotent without clobbering display_name or the active flag;
            # (xmax = 0) distinguishes a fresh insert from finding an existing row.
            cur.execute(
                "INSERT INTO app.clients (slug, display_name) VALUES (%s, %s) "
                "ON CONFLICT (slug) DO UPDATE SET updated_at = now() "
                "RETURNING id, display_name, active, (xmax = 0) AS created",
                (slug, name),
            )
            row = cur.fetchone()
        return {
            "ok": True, "client": slug, "id": str(row["id"]),
            "display_name": row["display_name"], "active": row["active"],
            "created": bool(row["created"]),
        }
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        streamable_http_path="/mcp",
        stateless_http=True,
    )
