"""Everything that talks to LiteLLM: model calls, and the workflow-run ledger.

Two LiteLLM facts this module is built around, both verified live on 2026-08-18 (see the plan's
"LiteLLM capability audit") — do not "improve" this by assuming otherwise:

  * LiteLLM Workflows are a LEDGER, NOT AN ENGINE. Posting a run with a `steps` graph silently
    discards the steps; status stays `pending` until something external patches it. Sequencing,
    retries and gating therefore live in runner.py. We write breadcrumbs here so a crashed run is
    resumable and Langfuse correlates.

  * LiteLLM Agents are an A2A PROXY, NOT A RUNTIME. Registration takes a URL; there is nowhere to
    put a prompt. We host the agents; LiteLLM fronts them for budgets/caps/observability.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("nemoclaw.gateway")

# LiteLLM's own status vocabulary for workflow runs. Anything else is rejected with 422.
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"


class GatewayError(RuntimeError):
    pass


class BudgetExceeded(GatewayError):
    """LiteLLM returned 429 budget_exceeded for the acting client.

    Distinct from a generic failure because it is not retryable and must surface to the human,
    not be silently absorbed by a stage retry.
    """


class ModelAccessDenied(GatewayError):
    """403 — the acting key is not permitted this model alias."""



class MCPError(GatewayError):
    """An MCP tool call failed. Distinct so a stage can fail honestly instead of improvising."""


@dataclass
class MCPSession:
    """Executes MCP tool calls against LiteLLM's aggregated MCP gateway.

    Uses the OFFICIAL MCP Python SDK (`mcp`) rather than a hand-rolled JSON-RPC/SSE client. An
    earlier version of this class spoke the wire protocol directly and had to rediscover, the hard
    way, that `/mcp` 307-redirects to `/mcp/`, that `Accept` must list both `application/json` and
    `text/event-stream`, and that the `mcp-session-id` header has to be echoed on every call. The
    SDK handles all of that, and it is the first-party client for the protocol.

    Sync on purpose: the orchestrator is a synchronous FastAPI app, so each call runs its own
    short-lived event loop and session. The handshake costs milliseconds against a tool call that
    takes seconds, and it keeps this free of thread/loop plumbing.
    """

    base_url: str
    api_key: str
    timeout: float = 120.0

    async def _call_async(self, name: str, arguments: dict[str, Any]) -> str:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = f"{self.base_url.rstrip('/')}/mcp/"
        http_client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx2.Timeout(self.timeout, read=self.timeout),
            follow_redirects=True,
        )
        async with http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)

        if getattr(result, "isError", False):
            raise MCPError(f"tool '{name}' reported an error: {str(result.content)[:300]}")
        parts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
        if parts:
            return "\n".join(parts)
        structured = getattr(result, "structuredContent", None)
        return json.dumps(structured)[:4000] if structured else ""

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            return asyncio.run(self._call_async(name, arguments))
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure is a failed tool call
            raise MCPError(f"tool '{name}' call failed: {type(exc).__name__}: {exc}") from exc


@dataclass
class LiteLLMGateway:
    base_url: str
    master_key: str
    timeout: float = 180.0

    def _client(self, api_key: str | None = None, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key or self.master_key}"},
            timeout=timeout or self.timeout,
        )

    # ---------------------------------------------------------------- model calls

    # ---------------------------------------------------------------- MCP tools

    def list_mcp_tools(self, servers: set[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI-shaped tool definitions for the given MCP servers.

        Tool names arrive prefixed with the server alias (`postiz-integrationList`), which is how
        we scope an agent to only the servers its `mcp_tools` declares — the model is never even
        shown a tool it is not entitled to.
        """
        with self._client() as c:
            r = c.get("/v1/mcp/tools")
        if r.status_code >= 400:
            log.warning("could not list MCP tools: %s %s", r.status_code, r.text[:200])
            return []
        out: list[dict[str, Any]] = []
        for t in r.json().get("tools", []):
            name = t.get("name") or ""
            if servers is not None and name.split("-", 1)[0] not in servers:
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": (t.get("description") or "")[:1024],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return out


    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        client_key: str,
        response_schema: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tool_iterations: int = 6,
    ) -> dict[str, Any]:
        """One chat completion, billed to the acting client's key.

        `client_key` is the client's LiteLLM virtual key — NOT the master key. That is what makes
        spend attribute correctly and what makes the client's budget cap actually bite. Passing
        the master key here would silently bypass both.
        """
        # Tool-calling and structured output are mutually exclusive in practice: forcing a
        # json_schema response while the model still needs to emit tool_calls produces neither
        # cleanly. So we run the tool loop FIRST with tools attached and no schema, then ask for
        # the structured answer once the tools have actually been called.
        if tools:
            messages = self._run_tool_loop(
                model=model, messages=messages, client_key=client_key, tools=tools,
                timeout=timeout, max_iterations=max_tool_iterations, run_id=session_id,
            )

        payload: dict[str, Any] = {"model": model, "messages": messages}

        if response_schema is not None:
            # Structured output is how we avoid free-text drift from open-weight models. Not all
            # models honour json_schema; runner.py validates the result regardless, so this is a
            # strong hint rather than a guarantee.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "stage_output", "schema": response_schema, "strict": True},
            }
        if session_id:
            payload["metadata"] = {"session_id": session_id}

        # Per-agent timeout from agents.yaml. Reasoning-heavy open models (qwen3.8-max especially)
        # regularly exceed a 180s default when producing long structured output — observed live as
        # httpx.ReadTimeout mid-pipeline on 2026-08-18.
        with self._client(client_key, timeout=timeout) as c:
            r = c.post("/v1/chat/completions", json=payload)

        if r.status_code == 429:
            raise BudgetExceeded(r.text[:500])
        if r.status_code == 403:
            raise ModelAccessDenied(r.text[:500])
        if r.status_code >= 400:
            raise GatewayError(f"completion failed {r.status_code}: {r.text[:500]}")

        body = r.json()
        content = body["choices"][0]["message"].get("content") or ""
        return {
            "content": content,
            "usage": body.get("usage") or {},
            "model": body.get("model"),
        }


    def _run_tool_loop(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        client_key: str,
        tools: list[dict[str, Any]],
        timeout: float | None,
        max_iterations: int,
        run_id: str | None,
    ) -> list[dict[str, Any]]:
        """Let the model actually USE its tools, executing each call ourselves.

        This closes the gap that produced the third confabulation of 2026-08-18: the publisher was
        permitted to run because Postiz was registered in LiteLLM, but the completion payload
        carried no `tools` key at all — so the model had no means to act and invented a URL
        (a regurgitated `r2.dev` spam redirect, caught by verification.py). Registering a tool in
        the gateway is NOT the same as handing it to the model.

        Every call and result is appended to the run ledger, so afterwards we can prove from the
        record whether a tool really ran — rather than asking the model.

        LiteLLM's `/v1/responses` MCP mode (where the proxy executes tools itself) is NOT usable
        here: it translates to OpenAI's Responses API, which DigitalOcean's inference endpoint does
        not implement — verified live, it 404s with "Response with id ... not found".
        """
        convo = list(messages)
        session = MCPSession(
            base_url=self.base_url, api_key=self.master_key, timeout=timeout or self.timeout
        )

        for step in range(max_iterations):
            with self._client(client_key, timeout=timeout) as c:
                r = c.post("/v1/chat/completions", json={
                    "model": model, "messages": convo, "tools": tools, "tool_choice": "auto",
                })
            if r.status_code == 429:
                raise BudgetExceeded(r.text[:500])
            if r.status_code >= 400:
                raise GatewayError(f"tool step failed {r.status_code}: {r.text[:500]}")

            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            convo.append(msg)
            if not calls:
                return convo  # model is done reaching for tools

            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = session.call(name, args)
                    ok = True
                except MCPError as exc:
                    # Surfaced to the model as a tool error, NOT swallowed: it must see the
                    # failure so it reports failure rather than assuming success.
                    result, ok = f"TOOL CALL FAILED: {exc}", False
                    log.warning("MCP tool '%s' failed: %s", name, exc)

                if run_id:
                    self.record(run_id, event_type="tool_call", step_name=name,
                                data={"arguments": args, "ok": ok, "result": result[:1500]})
                convo.append({
                    "role": "tool", "tool_call_id": call.get("id"), "name": name,
                    "content": result[:8000],
                })

        log.warning("tool loop hit max_iterations=%s without settling", max_iterations)
        return convo


    def sync_agents(self, agents: list[tuple[str, dict[str, Any]]]) -> tuple[int, int]:
        """Make LiteLLM's agent registry match ours. Returns (published, failed).

        Delete-then-create rather than upsert: `agent_name` carries a UNIQUE constraint and LiteLLM
        exposes no update path, so a plain POST for an existing agent fails with
        "Unique constraint failed" while the agent still isn't visible via the API (its registry is
        in-memory and not rebuilt from the DB on boot). Deleting first is what actually makes a
        stale row reappear.
        """
        existing: dict[str, str] = {}
        try:
            with self._client() as c:
                r = c.get("/v1/agents")
            if r.status_code < 400:
                body = r.json()
                for a in (body if isinstance(body, list) else body.get("data", [])):
                    if a.get("agent_name") and a.get("agent_id"):
                        existing[a["agent_name"]] = a["agent_id"]
        except httpx.HTTPError:
            log.warning("could not list existing agents; will attempt to create regardless")

        published = failed = 0
        with self._client() as c:
            for name, card in agents:
                if name in existing:
                    c.delete(f"/v1/agents/{existing[name]}")
                r = c.post("/v1/agents", json={"agent_name": name, "agent_card_params": card})
                if r.status_code < 400:
                    published += 1
                elif "already exists" in r.text or "Unique constraint" in r.text:
                    # The goal is "this agent is registered", and it is. LiteLLM's registry lives
                    # in memory and its DELETE only touches the DB, so a row removed out-of-band
                    # leaves the in-memory entry behind and every POST reports a conflict. Treating
                    # that as success keeps startup idempotent instead of logging 13 scary
                    # warnings about a state that is actually correct.
                    published += 1
                else:
                    log.warning("agent '%s' registration failed %s: %s",
                                name, r.status_code, r.text[:200])
                    failed += 1
        return published, failed

    # ---------------------------------------------------------------- workflow ledger

    def open_run(self, *, workflow_type: str, task_input: dict[str, Any]) -> str:
        """Create a run record. Returns run_id.

        NB: LiteLLM ignores any `steps` you pass — this records that a run exists, nothing more.
        """
        with self._client() as c:
            r = c.post(
                "/v1/workflows/runs",
                json={"workflow_type": workflow_type, "input": task_input},
            )
        if r.status_code >= 400:
            raise GatewayError(f"could not open workflow run: {r.status_code} {r.text[:300]}")
        return r.json()["run_id"]

    def record(
        self, run_id: str, *, event_type: str, step_name: str, data: dict[str, Any] | None = None
    ) -> None:
        """Append one breadcrumb. Never raises — losing a log line must not kill a live run."""
        try:
            with self._client() as c:
                c.post(
                    f"/v1/workflows/runs/{run_id}/events",
                    json={"event_type": event_type, "step_name": step_name, "data": data or {}},
                )
        except Exception:  # noqa: BLE001 - deliberately swallowed, see docstring
            log.warning("failed to record event %s/%s on run %s", event_type, step_name, run_id)

    def say(self, run_id: str, *, role: str, content: str) -> None:
        """Append a human-readable message to the run.

        Distinct from `record()`: events are machine breadcrumbs (`stage_completed`, token usage),
        messages are the narrative a human reads in the LiteLLM UI's Workflow Runs view. Writing
        only events leaves that view empty, which is exactly how this gap was spotted.

        Never raises — a missing log line must not kill a live run.
        """
        try:
            with self._client() as c:
                c.post(
                    f"/v1/workflows/runs/{run_id}/messages",
                    json={"role": role, "content": content[:4000]},
                )
        except Exception:  # noqa: BLE001 - deliberately swallowed, see docstring
            log.warning("failed to append message to run %s", run_id)

    def close_run(
        self, run_id: str, *, status: str, output: dict[str, Any] | None = None
    ) -> None:
        if status not in {RUN_PENDING, RUN_RUNNING, RUN_PAUSED, RUN_COMPLETED, RUN_FAILED}:
            raise ValueError(f"invalid workflow run status {status!r}")
        with self._client() as c:
            r = c.patch(
                f"/v1/workflows/runs/{run_id}",
                json={"status": status, "output": output or {}},
            )
        if r.status_code >= 400:
            log.error("could not close run %s: %s %s", run_id, r.status_code, r.text[:300])

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._client() as c:
            r = c.get(f"/v1/workflows/runs/{run_id}")
        if r.status_code >= 400:
            raise GatewayError(f"run {run_id} not readable: {r.status_code}")
        return r.json()

    # ---------------------------------------------------------------- tenancy

    def ensure_client_team(self, *, client_id: str, max_budget: float, models: list[str]) -> str:
        """One LiteLLM Team per client — the tenant boundary.

        Teams (not bare keys) because `user_api_key_team_id` / `team_alias` propagate into spend
        logs automatically, whereas custom `client_id` key-metadata does NOT (verified 2026-08-18).
        Teams also carry their own budget and MCP scoping.
        """
        with self._client() as c:
            r = c.post(
                "/team/new",
                json={
                    "team_alias": client_id,
                    "max_budget": max_budget,
                    "models": models,
                },
            )
        if r.status_code >= 400:
            raise GatewayError(f"could not create team for {client_id}: {r.text[:300]}")
        return r.json()["team_id"]

    def issue_agent_key(
        self, *, team_id: str, agent_id: str, models: list[str]
    ) -> str:
        """A key scoped to one client (team) AND one agent.

        The intersection is the isolation: the team supplies whose credentials/budget apply, the
        agent identity caps which models and MCP tools are reachable. LiteLLM intersects
        permissions most-restrictive-wins.
        """
        with self._client() as c:
            r = c.post(
                "/key/generate",
                json={"team_id": team_id, "key_alias": f"{team_id}:{agent_id}", "models": models},
            )
        if r.status_code >= 400:
            raise GatewayError(f"could not issue key for {agent_id}: {r.text[:300]}")
        return r.json()["key"]

    def team_spend(self, team_id: str) -> dict[str, Any]:
        with self._client() as c:
            r = c.get(f"/team/info?team_id={team_id}")
        if r.status_code >= 400:
            raise GatewayError(f"team {team_id} not readable: {r.status_code}")
        info = r.json().get("team_info") or r.json()
        return {"spend": info.get("spend"), "max_budget": info.get("max_budget")}


def parse_json_output(raw: str) -> dict[str, Any]:
    """Best-effort JSON extraction from a model response.

    Open-weight models wrap JSON in prose or fences more often than frontier ones do, so we try
    progressively harder rather than failing on the first stumble. Still strict in the end: if
    there is no parseable object, that is a stage failure, not something to paper over.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty model response")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    if "```" in raw:
        chunk = raw.split("```", 2)[1]
        if chunk.startswith("json"):
            chunk = chunk[4:]
        try:
            return json.loads(chunk.strip())
        except json.JSONDecodeError:
            pass

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Brace-scan fallback. Observed live 2026-08-18: an open-weight model emitted
    # `{"{"body": "...", "cta": "..."}` — a stray `{"` prefix that defeats every strategy above,
    # because the outermost braces span the corruption. Walk forward from each `{` and return the
    # first balanced region that parses. Cheap, and recovers a class of near-miss output that
    # would otherwise cost a full retry.
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, len(raw)):
            c = raw[j]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[i : j + 1])
                    except json.JSONDecodeError:
                        break  # this candidate is junk; try the next '{'
                    if isinstance(parsed, dict) and parsed:
                        return parsed
                    break

    raise ValueError(f"model did not return parseable JSON: {raw[:200]}")
