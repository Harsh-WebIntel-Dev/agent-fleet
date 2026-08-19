"""MCP server giving the OpenClaw PM agent one deterministic way to message a client's Hermes.

WHY THIS EXISTS
PM (a shared OpenClaw agent) needs to "talk back" to the CLIENT-FACING Hermes — report a task is done,
or ask the client a question. Hermes is a real A2A v1.0 endpoint and LiteLLM's agent gateway already
proxies to it (POST /a2a/<agent_id>/message/send, verified). The only missing piece was a callable
surface for PM: OpenClaw agents reach LiteLLM for MODEL calls, not for /a2a invocations. This is that
surface — our own thin tool over LiteLLM's own /a2a route (no official MCP exists for it, same footing
as mcp-spaces).

TENANCY — THE LOAD-BEARING PART (learned the hard way on mcp-spaces)
There is one Hermes per client, so "which Hermes" is a tenancy decision and MUST NOT be made by the
model. LiteLLM does not forward caller identity to an MCP server, so a `client`/`agent_id` ARGUMENT
would be caller-chosen = a cross-client leak waiting to happen (PM messaging client A's Hermes with
client B's content). Instead:

  * The target Hermes agent_id is PINNED per client by the `x-hermes-agent-id` header that LiteLLM
    sets on this client's dedicated mcp_servers entry (static_headers). The model never supplies it.
    Duplicate-header injection is refused (getlist, exactly-one), same defence as mcp-spaces.
  * This server authenticates to /a2a with a LiteLLM key scoped (object_permission.agents) to ONLY
    that client's Hermes, so even a compromised server cannot reach another tenant's Hermes.
  * LiteLLM ALSO gates /a2a by the CALLER's key (object_permission.agents), so the client key that
    reaches this server's per-client entry is itself restricted to its own Hermes. Defence in depth.

So the tenant is fixed by config + gateway ACL at two layers, never by PM.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any

from mcp.server.mcpserver import MCPServer, Context

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm-v10up2yg1cwxo0k1ks9j2qro:4000")
# Key scoped (object_permission.agents) to this client's Hermes agent ONLY. Minimal blast radius.
A2A_KEY = os.environ.get("LITELLM_A2A_KEY", "")
# Fallback target when no per-client header is present (single-client / dev). Header wins.
DEFAULT_AGENT_ID = os.environ.get("A2A_TARGET_AGENT_ID", "")
REPLY_TIMEOUT = float(os.environ.get("A2A_REPLY_TIMEOUT", "120"))

mcp = MCPServer(
    name="a2a",
    instructions=(
        "Message the client's Hermes front-door. Use notify_client_hermes to tell the client's Hermes "
        "that work is done or to ask the client a question. You do NOT choose which client — the "
        "gateway is bound to the acting client. Report only what Hermes actually returns."
    ),
)


def _target_agent_id(ctx: Context) -> str:
    """The Hermes agent_id for THIS call — pinned by the gateway per client, never model-supplied."""
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(req, "headers", None)
    vals = headers.getlist("x-hermes-agent-id") if hasattr(headers, "getlist") else []
    if len(vals) > 1:
        raise ValueError("ambiguous target: multiple x-hermes-agent-id headers")
    if len(vals) == 1 and vals[0].strip():
        return vals[0].strip()
    if DEFAULT_AGENT_ID:
        return DEFAULT_AGENT_ID
    raise ValueError("no target Hermes configured (missing x-hermes-agent-id header)")


@mcp.tool()
def notify_client_hermes(ctx: Context, message: str, expect_reply: bool = True) -> dict[str, Any]:
    """Send a message to the acting client's Hermes and (by default) return its reply.

    Use for: reporting a task is complete, or asking the client for more information. The client's
    Hermes runs the message in its LIVE session (same agent talking to the client, full memory).
    """
    try:
        if not A2A_KEY:
            return {"ok": False, "error": "not_configured", "detail": "LITELLM_A2A_KEY is unset"}
        if not (message or "").strip():
            return {"ok": False, "error": "empty_message", "detail": "message is required"}
        agent_id = _target_agent_id(ctx)

        body = {
            "jsonrpc": "2.0",
            "id": "notify",  # MUST be a string — LiteLLM's response model rejects an int id
            "method": "message/send",
            "params": {"message": {
                "role": "user",
                "messageId": "pm-notify",
                "parts": [{"kind": "text", "text": message}],
            }},
        }
        req = urllib.request.Request(
            f"{LITELLM_BASE}/a2a/{agent_id}/message/send",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + A2A_KEY, "Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=REPLY_TIMEOUT)
            payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            # A 403 here means the gateway ACL refused this target — surface it honestly.
            return {"ok": False, "error": f"a2a_http_{exc.code}", "detail": detail}

        result = (payload or {}).get("result", {})
        task = result.get("task", {}) if isinstance(result, dict) else {}
        state = ((task.get("status") or {}).get("state")) if isinstance(task, dict) else None
        reply = ""
        msg = (task.get("status") or {}).get("message") if isinstance(task, dict) else None
        if isinstance(msg, dict):
            reply = "".join(p.get("text", "") for p in msg.get("parts", []) if isinstance(p, dict))
        out: dict[str, Any] = {"ok": True, "delivered": True, "state": state}
        if expect_reply:
            out["reply"] = reply
        return out
    except Exception as exc:  # noqa: BLE001 - report failure as data, never a fake success
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")), streamable_http_path="/mcp",
            stateless_http=True)
