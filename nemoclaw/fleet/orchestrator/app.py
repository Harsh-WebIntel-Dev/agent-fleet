"""NemoClaw service — ONE deployment, one A2A endpoint per agent.

Architecture note (decided 2026-08-18): we do not deploy a container per specialist. Every agent
in agents.yaml gets a route here plus an A2A agent card, and all of them are registered in
LiteLLM's agent gateway by URL. That buys per-agent budgets, rate limits, MCP caps and
observability at the gateway while keeping this a single service.

LiteLLM cannot host agents itself (its Agents feature is an A2A proxy that takes a URL — verified
live), which is precisely why this service exists.

Exposure: internal only. `hermes-<client>` projects are the public tier and reach PM through
`fleet-net`; PM never gets a public route.
"""

from __future__ import annotations

import logging
import os

import httpx
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import Registry, Stage, load_registry
from .gateway import LiteLLMGateway
from .memory import ClientDataAccessDenied, MemoryStore
from .runner import AwaitingHuman, PipelineRunner, QAGateFailed, RunState, StageFailed

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("nemoclaw")

SERVICE_URL = os.getenv("NEMOCLAW_PUBLIC_URL", "http://nemoclaw:8000")

registry: Registry = load_registry()
gateway = LiteLLMGateway(
    base_url=os.environ["LITELLM_BASE_URL"],
    master_key=os.environ["LITELLM_MASTER_KEY"],
)
memory = MemoryStore(dsn=os.environ["FLEET_DATABASE_URL"])
# available_tools is what makes `requires_tools` real: the runner asks LiteLLM what MCP servers
# exist before it lets a side-effect stage run at all.
runner = PipelineRunner(
    registry=registry, gateway=gateway, memory=memory,
    available_tools=lambda: _available_mcp_tools(),
)

app = FastAPI(title="NemoClaw", version="0.1.0")


# ---------------------------------------------------------------- models


class A2AMessage(BaseModel):
    """Minimal A2A message/send shape."""

    message: str
    client_id: str = Field(..., description="tenant; selects team, key and RLS scope")
    client_key: str = Field(..., description="that client's LiteLLM virtual key")
    session_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    pipeline: str
    client_id: str
    client_key: str
    task: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    run_id: str
    client_id: str
    client_key: str
    pipeline: str
    approved: bool
    task: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- health / discovery


def _available_mcp_tools() -> set[str]:
    """Which MCP servers NemoClaw can ACTUALLY use right now.

    Ground truth, not what agents.yaml wishes were true. An agent can declare `mcp_tools:
    [postiz]` while no Postiz server exists — and a model will happily narrate having used it
    (observed live 2026-08-18). Feeding this set into the layer-1 stage gate and PM's review turns
    "did you really do that?" from a judgement call into a checkable fact.

    Derived from `/v1/mcp/tools`, NOT `/v1/mcp/server`. That distinction matters: the server list
    reflects *registration*, so a server with a stale or wrong credential still appears there and
    would wrongly satisfy the gate. Listing tools requires LiteLLM to actually connect and
    handshake, so a server only counts once its tools really came back. Tool names arrive prefixed
    with the server alias (`postiz-integrationList`), which is where the server names come from.

    Fails CLOSED: if the gateway can't be reached we return the empty set, so every declared tool
    counts as unavailable and side-effect stages refuse to run.
    """
    try:
        with httpx.Client(
            base_url=os.environ["LITELLM_BASE_URL"].rstrip("/"),
            headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
            timeout=20.0,
        ) as c:
            r = c.get("/v1/mcp/tools")
        if r.status_code >= 400:
            log.warning("MCP tool listing returned HTTP %s; treating all tools as unavailable",
                        r.status_code)
            return set()
        tools = r.json().get("tools", [])
        return {t["name"].split("-", 1)[0] for t in tools if isinstance(t, dict) and t.get("name")}
    except Exception:  # noqa: BLE001 - see docstring: unreachable gateway means "nothing available"
        log.warning("could not list MCP tools; treating all declared tools as unavailable")
        return set()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "agents": sorted(registry.agents),
        "pipelines": sorted(registry.pipelines),
    }


@app.get("/a2a/{agent_id}/.well-known/agent-card.json")
@app.get("/a2a/{agent_id}/.well-known/agent.json")
def agent_card(agent_id: str) -> dict[str, Any]:
    """A2A discovery document — this is what LiteLLM fetches when registering the agent."""
    try:
        agent = registry.agent(agent_id)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'") from None

    return {
        "protocolVersion": "0.3",
        "name": agent.display_name,
        "description": agent.description,
        "url": f"{SERVICE_URL}/a2a/{agent.id}",
        "version": "0.1.0",
        "preferredTransport": "JSONRPC",
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {
                "id": agent.id,
                "name": agent.display_name,
                "description": agent.description,
                "tags": [agent.tier, *agent.mcp_tools],
            }
        ],
    }


# ---------------------------------------------------------------- agent invocation


@app.post("/a2a/{agent_id}/message/send")
@app.post("/a2a/{agent_id}")
def invoke_agent(agent_id: str, msg: A2AMessage) -> dict[str, Any]:
    """Invoke one agent directly.

    For specialists this is a single stateless call — client context must arrive in
    `msg.context`, because a tier=skill agent cannot look anything up (memory.py enforces it).
    """
    try:
        agent = registry.agent(agent_id)
    except KeyError:
        raise HTTPException(404, f"unknown agent '{agent_id}'") from None

    if not agent.enabled:
        raise HTTPException(409, f"agent '{agent_id}' is disabled")

    run_id = gateway.open_run(
        workflow_type=f"direct:{agent_id}",
        task_input={"client_id": msg.client_id, "message": msg.message[:2000]},
    )
    state = RunState(run_id=run_id, client_id=msg.client_id, pipeline_id=f"direct:{agent_id}")

    payload = dict(msg.context)
    payload["message"] = msg.message

    # PM may enrich from client_kb; specialists may not, and asking is an error not a fallback.
    if agent.is_client_tier:
        payload["client"] = memory.build_client_context(agent, msg.client_id)

    stage = Stage(
        id="direct",
        agent=agent_id,
        inputs=(),
        output_schema="consult_answer",
        gate=None,
        on_fail="retry",
    )
    try:
        out = runner._run_agent_stage(stage, agent, payload, state, msg.client_key)
    except ClientDataAccessDenied as exc:
        gateway.close_run(run_id, status="failed")
        raise HTTPException(403, str(exc)) from None
    except StageFailed as exc:
        gateway.close_run(run_id, status="failed")
        raise HTTPException(502, exc.reason) from None

    # RULE (user, 2026-08-18): output that did not pass a QA gate gets a PM check before it goes
    # back to the user. A direct specialist call has no QA stage, so PM reviews it here.
    # PM is also told which of the specialist's declared tools actually exist, so it can catch the
    # confabulation class we hit live — an agent reporting work it had no means to perform.
    review: dict[str, Any] | None = None
    if agent.id != runner.pm_agent_id:
        pm = registry.agent(runner.pm_agent_id)
        available = _available_mcp_tools()
        missing = [t for t in agent.mcp_tools if t not in available]
        review_stage = Stage(
            id="pm_review",
            agent=pm.id,
            inputs=(),
            output_schema="pm_review",
            gate="pm_must_approve",
            on_fail="abort",
        )
        review_payload = {
            "specialist": agent.id,
            "specialist_output": out,
            "original_request": msg.message,
            "declared_tools": list(agent.mcp_tools),
            "tools_actually_available": sorted(available & set(agent.mcp_tools)),
            "tools_UNAVAILABLE": missing,
            "instruction": (
                "Review this specialist's output before it is returned to the user. Any claim of "
                "an action performed with a tool listed in tools_UNAVAILABLE could NOT have "
                "happened — list it in unverified_claims and do not approve it as fact."
            ),
        }
        try:
            review = runner._run_agent_stage(
                review_stage, pm, review_payload, state, msg.client_key
            )
        except StageFailed as exc:
            gateway.close_run(run_id, status="failed")
            raise HTTPException(502, f"PM review failed: {exc.reason}") from None

        if not review.get("approved_for_user", False):
            gateway.close_run(run_id, status="failed", output={"review": review})
            raise HTTPException(
                422,
                {
                    "error": "PM did not approve this output for the user",
                    "concerns": review.get("concerns"),
                    "unverified_claims": review.get("unverified_claims"),
                },
            )

    gateway.close_run(run_id, status="completed", output=out)
    return {
        "agent": agent_id,
        "run_id": run_id,
        "result": out,
        "pm_review": review,  # None only when PM itself was the agent invoked
    }


# ---------------------------------------------------------------- pipelines


def _execute_pipeline(req: RunRequest, run_id: str) -> None:
    """Run a pipeline to completion (or to its first gate) in the background.

    Pipelines are LONG-LIVED by nature: several model calls, retries, and a human gate that may sit
    for hours. Running them inside the HTTP request was a real mistake — a `gmb_post` run
    time-limited the caller and died with httpx.ReadTimeout mid-retry on 2026-08-18. Callers now
    poll /runs/{run_id} instead.
    """
    try:
        runner.run(
            pipeline_id=req.pipeline,
            client_id=req.client_id,
            task=req.task,
            client_key=req.client_key,
            resume_run_id=run_id,
        )
    except AwaitingHuman:
        log.info("run %s paused for human approval", run_id)
    except QAGateFailed as exc:
        log.warning("run %s blocked by QA gate: %s", run_id, exc.reason)
    except (StageFailed, ClientDataAccessDenied) as exc:
        log.error("run %s failed: %s", run_id, exc)
    except Exception:  # noqa: BLE001 - background task must never die silently
        log.exception("run %s crashed unexpectedly", run_id)
        gateway.close_run(run_id, status="failed")


@app.post("/run")
def start_run(req: RunRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Start a pipeline. Returns immediately with a run_id; poll /runs/{run_id} for progress.

    Deliberately does NOT wait for the pipeline. See _execute_pipeline.
    """
    try:
        registry.pipeline(req.pipeline)  # fail fast on a bad pipeline name
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None

    run_id = gateway.open_run(
        workflow_type=req.pipeline,
        task_input={"client_id": req.client_id, **req.task},
    )
    background.add_task(_execute_pipeline, req, run_id)
    return {"status": "accepted", "run_id": run_id, "poll": f"/runs/{run_id}"}


@app.post("/approve")
def approve(req: ApprovalRequest) -> dict[str, Any]:
    """Resume a run paused at a human gate (ClickUp approved/rejected drives this)."""
    state = RunState(
        run_id=req.run_id,
        client_id=req.client_id,
        pipeline_id=req.pipeline,
        outputs=req.outputs,
        completed=req.completed,
    )
    try:
        state = runner.resume_after_approval(
            state=state, approved=req.approved, client_key=req.client_key, task=req.task
        )
    except AwaitingHuman as pause:
        return {"status": "awaiting_approval", "run_id": pause.run_id, "stage": pause.stage_id}
    except QAGateFailed as exc:
        raise HTTPException(422, f"QA gate failed: {exc.reason}") from None
    except StageFailed as exc:
        raise HTTPException(502, str(exc)) from None

    return {"status": "completed", "run_id": state.run_id, "outputs": state.outputs}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return gateway.get_run(run_id)


# ---------------------------------------------------------------- tenancy admin


@app.post("/clients/{client_id}/provision")
def provision_client(client_id: str, max_budget: float = 50.0) -> dict[str, Any]:
    """Create the LiteLLM Team for a client and issue one key per enabled agent.

    Teams are the tenant boundary because team_id/team_alias propagate into spend logs while
    custom key metadata does not (verified 2026-08-18).
    """
    models = sorted({a.model for a in registry.agents.values() if a.enabled} |
                    {a.fallback for a in registry.agents.values() if a.enabled and a.fallback})
    team_id = gateway.ensure_client_team(
        client_id=client_id, max_budget=max_budget, models=models
    )
    keys = {
        a.id: gateway.issue_agent_key(
            team_id=team_id,
            agent_id=a.id,
            models=[m for m in (a.model, a.fallback) if m],
        )
        for a in registry.agents.values()
        if a.enabled
    }
    return {"client_id": client_id, "team_id": team_id, "agent_keys": keys}
