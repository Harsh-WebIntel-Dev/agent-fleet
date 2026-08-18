# NemoClaw

A shared multi-agent marketing fleet serving multiple agency clients, running on open-weight
models. One PM orchestrates specialists; per-client Hermes front-doors are the only public surface.

## Architecture

```
hermes-<client>   [one Coolify project per client · PUBLIC via Traefik · thin]
     │             holds only that client's conversation (memory schema)
     ▼
nemoclaw          [ONE internal service · no public route]
   PM ──consults──> specialists      } A2A endpoints, registered in LiteLLM
        └pipelines─┘
     ▼
LiteLLM  — per-client budgets · per-agent MCP caps · policies · workflow-run ledger
     ▼
Langfuse (traces) · Postgres+pgvector (memory tiers) · DO Spaces (assets)
```

**LiteLLM is the substrate, NemoClaw is the brain.** LiteLLM's "Agents" are an A2A *proxy* that
takes a URL — it cannot host agents. Its "Workflows" are a *ledger*, not an engine — posting a step
graph silently discards the steps. So sequencing, retries and gating live in `orchestrator/runner.py`;
LiteLLM supplies budgets, tool caps, and the audit trail.

**One service, many agents.** Every agent in `agents.yaml` gets an A2A endpoint here
(`/a2a/<id>`) plus an agent card, all registered in LiteLLM. Specialists are *config* — a prompt, a
model alias, and an MCP allowlist — not deployments.

## Memory tiering (the load-bearing design decision)

| Agent | Remembers | Schema | Scope |
|---|---|---|---|
| Hermes | this client & company; conversation | `memory` | per-client (RLS) |
| PM | company facts + work done (e.g. a client's blog template) | `client_kb` | per-client (RLS) |
| Specialists | craft only — never anything client-specific | `skill_kb` | shared |

**Hard rule:** specialists receive client context as *data in the request* and never query client
storage. PM looks up the blog template and injects it. Enforced three ways — `memory.py` refuses
the call, LiteLLM per-agent MCP caps deny the tool, and Postgres RLS filters the rows.

Specialists are the shared tier (one Writer serves every client), so they are the obvious
cross-tenant leak path. Holding no client memory closes that structurally rather than by policy.

## Open-weight design constraints

We run open-weight models by choice. The previous attempt failed on *orchestration*, not tooling —
a model confabulated capabilities it lacked and subagent results were lost. Hence:

- **Control flow in code, judgement in the model.** The PM model picks *which* pipeline; it never
  invents the sequence.
- **Validate every stage against a JSON schema.** This catches malformed and drifting output.
  It does **not** catch lying: a schema enforces *shape*, not *truth*, and `{"type": "string"}` is
  satisfied by any invention. We learned this the hard way — see below.
- **Three layers against confabulation**, because prompt instructions are not a control:
  1. `requires_tools` on a stage — if the MCP server it needs is not registered in LiteLLM, the
     stage hard-fails **before the model is called**. No tool, no opportunity to improvise.
  2. `verify` on a stage — after the claim, the *runner* independently checks it (fetches the
     published URL, downloads each image and decodes its real dimensions). Verification never
     lives in the agent that made the claim.
  3. A PM review gate (`pm_must_approve`) on any pipeline with no QA gate, told which tools
     actually exist, so nothing user-facing leaves the fleet unreviewed.
  Layers 1 and 2 are mandatory on any stage producing a side-effect schema — `config.py` refuses
  to start otherwise.
- **State in the LiteLLM workflow-run ledger** plus a local `app.runs` mirror, so a crashed or
  paused run resumes rather than restarts.
- **Capability limits at the gateway**, so confabulation cannot become privilege.

### Why layer 1 and 2 exist (do not remove them)

On 2026-08-18, with **zero** MCP servers registered, `publisher` returned a complete, schema-valid
publish result — invented `remote_id`, and a Google *search* URL dressed up as a published page.
The same day, `social` reported having scheduled Facebook and Instagram posts via Postiz, with post
IDs, having called nothing. Both agents' prompts explicitly forbade reporting unachieved work.

Re-running the identical `social` request later, the same model with the same prompt refused and
cited its honesty rules. Same inputs, opposite behaviour — prompt-level honesty is a coin flip.
That is the whole argument for putting the gate in Python.

## Layout

```
fleet/
  agents.yaml        14 agents: prompt + model alias + MCP allowlist + memory tier
  pipelines.yaml     blog · gmb_post · consult — declared here, executed by code
  schemas.yaml       JSON schema per stage output
  prompts/           one per agent + _shared_specialist.md preamble
  orchestrator/
    config.py        registry load + fail-loud validation
    memory.py        the three tiers; tier isolation enforced
    gateway.py       LiteLLM calls + workflow ledger
    runner.py        deterministic pipeline executor  ← the core
    app.py           FastAPI: A2A endpoints, agent cards, /run, /approve
db/migrations/       pgvector schemas, RLS policies, client registry
infra/coolify/       compose files per service
litellm/config.yaml  model map + per-token pricing
```

## Running the tests

```bash
python -m venv .venv && .venv/bin/pip install -r fleet/orchestrator/requirements.txt pytest
.venv/bin/python -m pytest fleet/orchestrator/test_runner.py -q
```

They prove the deterministic guarantees: the human gate pauses *before* publish, a QA fail blocks
the run, a schema violation fails the stage, per-agent timeouts reach the gateway, and a specialist
never receives undeclared client keys.

## Deploying

The image is currently built **on the server** because this repo has no remote yet:

```bash
tar czf - --exclude=__pycache__ --exclude=.pytest_cache fleet \
  | ssh webintelligenz-prod-2 "rm -rf ~/nemoclaw-build/fleet && tar xzf - -C ~/nemoclaw-build \
    && cd ~/nemoclaw-build/fleet && docker build -t nemoclaw:<version> ."
```

Then bump the tag in the Coolify service and restart. **Switch to Coolify git deployment once a
remote exists.**

### Two Coolify gotchas that will bite you

1. **"Connect to Predefined Networks" must be enabled** on every service, or its containers sit on
   a private network and cannot resolve LiteLLM or Postgres. The Coolify API field is
   `connect_to_docker_network`; the MCP tool cannot set it — use the web UI.
2. **The compose service name is NOT the resolvable hostname.** Coolify renames containers to
   `<service>-<uuid>`. `http://nemoclaw:8000` does not resolve; `http://nemoclaw-<uuid>:8000` does.
   This matters for `NEMOCLAW_PUBLIC_URL`, since it goes into the agent cards LiteLLM registers.

## Status

Phases 0–3 complete: infrastructure, per-client budgets, and the orchestrator skeleton, all
verified end-to-end against real models. Phase 4 (MCP tools), Phase 5 (memory tiers wired +
promotion gate) and Phase 6 (Hermes + ClickUp/WhatsApp intake) remain.

Agents have MCP tool *allowlists* declared but the MCP servers behind them do not exist yet — so
specialists currently reason without tools.
