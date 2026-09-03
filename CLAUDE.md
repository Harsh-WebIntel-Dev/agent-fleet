# CLAUDE.md — Agent Fleet

Guidance for Claude Code (and humans) working in this repo. This documents the **current, deployed**
system as of **2026-09-03**. Where an older README in a subfolder disagrees (e.g. `hermes/README.md`
still describes an "OpenClaw does the work" model), **this file is the authority** — the live system
is the Hermes-native fleet described below.

---

## 1. What this is

A **multi-agent marketing fleet** that runs a real digital-marketing agency's production work for
multiple clients. One PM agent (**Webster**) talks to people and orchestrates five specialist agents
through a kanban dependency chain. ClickUp is the source of truth; work is produced by the
specialists and human-gated before anything goes live.

- **Runtime:** [Hermes](https://github.com/nousresearch/hermes-agent) (`nousresearch/hermes-agent`)
  — an agent runtime with profiles, toolsets, an in-process cron scheduler, a webui, and an MCP
  client. It replaced an earlier OpenClaw-based build on **2026-08-24** (see `openclaw/`, `nemoclaw/`,
  which are **superseded** — kept for history, not deployed).
- **Model gateway:** a self-hosted **LiteLLM** instance is the single front door for models, virtual
  keys, budgets, and the aggregated MCP tool endpoint.
- **Tools:** first-party MCPs (ClickUp, Postiz, Semrush, Higgsfield) plus a set of **self-hosted MCP
  sidecars** in this repo (`mcp-*/`).

### The agents

| Profile | Role | Owns (concrete tools) |
|---|---|---|
| **Webster** (default profile) | PM / client liaison. Talks to people, shapes briefs, composes the work, runs the review gate, relays approvals. Does **not** produce deliverables himself. | `cronjob`, `kanban`, `delegation`, `memory`, ClickUp (via `pm_comms` MCP), chat |
| **seo** | Keyword & search strategy | **Semrush** + web search → keyword/on-page brief |
| **researcher** | Sourced facts & competitive intel | **Firecrawl**, web search, Semrush |
| **writer** | The words: blog/social/newsletter/ad/page copy | (writing craft; reads seo brief + researcher facts) |
| **producer** | Visual assets | **Higgsfield** (images + short-form video), `render-card` (HTML→PNG template cards), R2/Spaces |
| **publisher** | Staging & publishing (**draft-only, human-gated**) | **WordPress** (`wp_*`), **Postiz** (social + Google Business Profile), **Mailchimp** (newsletter drafts), **Lnk.Bio** |

Delegation is **dynamic** — Webster composes each job from a capabilities catalogue in his SOUL and
builds only the kanban cards a task needs. There is **no fixed pipeline**. Do **not** hand-deliver
work to Webster via CLI (`hermes -z`, forced cron) — the fleet is meant to run without a human/Claude
in the middle. Fix the *intake mechanism*, not the individual task.

---

## 2. Where it runs & how to reach it

- **Host:** prod-2 VPS `46.250.245.204`, SSH alias **`webintelligenz-prod-2`** (user `harsh`, in the
  `docker` group — so `docker` works without sudo; root login is **not** key-authorized). Managed by
  **Coolify**.
- **Containers** (share the `hermes-home` volume mounted at `/home/hermes/.hermes`):
  - `hermes-agent-zhvjhbo5752ovx1nl2rk9v30` — the agent + the in-process cron scheduler (the gateway).
  - `hermes-webui-zhvjhbo5752ovx1nl2rk9v30` — the web UI (front-end only, no scheduler).
  - `hermes-sandbox` — the **isolated** terminal/code sandbox the agents SSH into (see §11).
- **Hermes CLI** lives at **`/opt/hermes/.venv/bin/hermes`** inside `hermes-agent` (it is NOT on
  `$PATH`; `tirith` in `~/.hermes/bin` is a shell-security guard, not the CLI). Run e.g.:
  ```bash
  C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
  docker exec -u hermes "$C" /opt/hermes/.venv/bin/hermes cron list
  ```
- **Key files inside the container** (`/home/hermes/.hermes/`):
  - `SOUL.md` — Webster's system prompt. `profiles/<name>/SOUL.md` — each specialist's.
    **SOULs are read per-turn — edit them live, no restart needed.** These deployed copies are the
    **source of truth**; the `hermes/souls/*.SOUL.md` in this repo are the original templates and
    **drift** (they do not reflect live edits). Always read the live file before editing; keep a
    dated `.bak-*` backup when you change one.
  - `config.yaml` — profile toolsets, MCP servers, terminal backend, `timezone`, platforms.
  - `cron/jobs.json` — cron registry (managed via `hermes cron …`, not hand-edited).
- **Webui (public):** `https://wi-agent.widev.com.au` — password-only (`HERMES_WEBUI_PASSWORD`),
  internet-facing. Blast radius is bounded by per-client budget-capped keys, not the network.

---

## 3. System architecture

```
      people (ClickUp chat / tasks)
                 │
                 ▼
   ┌─────────────────────────────┐   ClickUp = single source of truth
   │  WEBSTER (PM, default)      │   (tasks, comments, chat, approvals)
   │  intake: 2 cron sweeps      │
   │   • marketing-task-sweep    │──── lists his actionable tasks via ClickUp MCP (no gate)
   │   • clickup-chat-intake     │──── monitor_chat.py gate → reads changed channel
   │  composes → kanban chain    │
   └──────────────┬──────────────┘
                  │ builds dependency-chained kanban cards (one per stage)
                  ▼
   kanban dispatcher (in Hermes)  ── starts each specialist when its parents are `done`
                  │
      ┌───────────┼───────────┬───────────┬───────────┐
      ▼           ▼           ▼           ▼           ▼
    seo      researcher    writer     producer    publisher
      └───────────┴───────────┴─────┬─────┴───────────┘
                                    ▼
                 ┌──────────────────────────────────┐
                 │  LiteLLM gateway (fleet-core-     │  models + virtual keys + budgets
                 │  litellm)  →  /mcp/ aggregated    │  + aggregated MCP tool endpoint
                 └───────────────┬──────────────────┘
                                 ▼
   ClickUp · Postiz · Semrush · Higgsfield (first-party MCPs)
   + self-hosted sidecars: memory · postiz-extras · lnkbio · mailchimp · wordpress · spaces · firecrawl
                                 │
                                 ▼
          mcp-memory (Postgres + pgvector, per-client RLS)   ·   R2/Spaces (assets)
```

Work never goes live without **explicit human approval** (reviewers approve/reject on the ClickUp
task; publisher stages drafts only).

---

## 4. Repo layout

| Path | What it is | Status |
|---|---|---|
| `hermes/` | The fleet: `souls/` (agent prompts, templates), `skills/marketing/` (blog/socials/newsletter/brand-kit), `scripts/` (`monitor_*.py` cron sources), `sandbox/` (Dockerfile + `render-card`/`qa-shot`), `setup-sandbox.sh`, `compose.yml`, `config.yaml.template` | **current** |
| `litellm-wrapper/` | LiteLLM image wrapper — `infisical_fetch.py` pulls creds, `litellm_entrypoint.sh` boots with `LITELLM_CONFIG_B64` | current |
| `mcp-memory/` | Vectorised fleet memory MCP (Postgres+pgvector, per-client RLS, per-agent) | current |
| `mcp-postiz-extras/` | Postiz post management the built-in MCP lacks (`postiz_list/delete/set_status/edit`) | current |
| `mcp-lnkbio/` | Lnk.Bio link-in-bio (rolling top-5), WI-only | current |
| `mcp-mailchimp/` | Mailchimp newsletter drafts (draft-only, WI-only) — **blocked on Infisical key** | current |
| `mcp-higgsfield/` | Higgsfield image/video generation sidecar | current |
| `mcp-wordpress/` | Custom `wp-json` WordPress sidecar (7 tools) | current |
| `mcp-spaces/` | R2/DigitalOcean Spaces asset store (ingest/presign/read/write) | current |
| `mcp-a2a/` | Agent-to-agent comms (legacy OpenClaw-era `ask_pm`/`create_pm_task`) | mostly legacy |
| `mcp-social-extras/` | (older social helper) | legacy |
| `firecrawl/`, `searxng/` | Self-hosted web crawl + search backends | current (infra) |
| `clickup-bridge/`, `clickup-sweep/` | OpenClaw-era ClickUp intake (`bridge.js`/`sweep.js`) | **superseded** by Hermes crons |
| `openclaw/`, `nemoclaw/` | Earlier platform builds | **superseded** (kept for history) |
| `plan/` | Planning docs | reference |

---

## 5. LiteLLM gateway (the front door)

- Coolify service `fleet-core-litellm` (`v10up2yg1cwxo0k1ks9j2qro`). **Live config is a host
  bind-mount: `/home/harsh/litellm-cfg/config.yaml`** on prod-2 (NOT `nemoclaw/litellm/config.yaml`
  in this repo, which is an older copy). `docker restart` the litellm container reloads it (~108s to
  healthy).
- Exposes an **aggregated MCP endpoint** `/mcp/` (streamable-HTTP). MCP servers are grouped by
  **access group**: `fleet_tools`, `fleet_internal`, `wi_tools`. Servers can carry a server-level
  `allowed_tools` allow-list (e.g. Postiz excludes `ask_postiz`).
- **Keys / scoping:**
  - The **WI agency key** grants tools by **access-group** → auto-inherits any new server added to
    `fleet_tools`/`wi_tools`. All fleet profiles' `pm_comms` uses this key, so WI-only scoping
    (lnkbio, mailchimp) is enforced as **SOUL policy**, not a hard boundary.
  - The **3 client keys** (biogone / pride-advice / radiance-wealth) grant by explicit **server-ID
    list** — must be updated per new server, and the TEAM must allow a server before a key can.
- **⚠️ DESTRUCTIVE TRAP:** `POST /v1/mcp/server` **replaces** a server record and **nulls** omitted
  fields (once caused a ~15-min Postiz outage). There is **no PATCH**. To change a config-defined
  server: edit `config.yaml` + `DELETE /v1/mcp/server/{id}` + restart (it re-seeds from config). A
  key's `blocked_tools` does NOT filter `tools/list` — use server `allowed_tools`.

---

## 6. MCP sidecar pattern

Each `mcp-*/` is a small self-hosted MCP server, deployed as its own Coolify service:

- `python:3.12-slim` (except mailchimp = `node:20-slim`), `from mcp.server.mcpserver import MCPServer`,
  `@mcp.tool()`, `mcp.run(transport="streamable-http", host="0.0.0.0", port=8080,
  streamable_http_path="/mcp", stateless_http=True)`.
- `infisical_fetch.py` pulls creds from self-hosted Infisical (`/shared` + per-client paths), with a
  Coolify-env fallback.
- Must be on the litellm docker network: set `connect_to_docker_network=true` (raw Coolify API PATCH)
  or the "Connect to Predefined Networks" UI toggle, and the app must bind `0.0.0.0`.
- Wire into `litellm-cfg/config.yaml` under `mcp_servers:` with an `access_groups:` entry.

---

## 7. Memory (`mcp-memory`)

- Postgres + **pgvector** (bge-m3, 1024-dim embeddings via the LiteLLM `embed` alias). **Not** local
  to the container — DSN in `/run/secrets/memory.env`.
- **Three isolation layers:** `x-client-slug` header pins the client (the agency key falls back to a
  `client` arg); a restricted `fleet_app` role with FORCED **row-level security** per
  `app.client_id`; and `agent_id` narrows recall to the author.
- Tools: `memory_remember`, `memory_search` (default = **your own** agent's notes;
  `across_agents=True` = all agents for that client), `memory_stats`, `memory_register_client`.
- **Global knowledge scope** (`client="global"`): a daily **SEMrush blog feed** cron
  (`semrush-blog-global-feed`, 07:00) ingests useful articles as memories under
  `agent="webster", client="global"` + a Spaces doc `clients/global/semrush-feed.md`.
- **⚠️ Gotcha (fixed 2026-09-03):** specialists recall global with their own `agent_id`, so
  webster-authored global entries were invisible to them until their SOULs were changed to pass
  `across_agents=True`. Any global knowledge a specialist must consume needs `across_agents=True`.

---

## 8. Intake & cron

The in-process scheduler runs in `hermes-agent`; jobs live in `cron/jobs.json`; manage with
`hermes cron {list,create,edit,run,remove,runs,status}`. It **re-reads `jobs.json` every tick**, so
`hermes cron edit` applies on the next tick with **no restart**. Active jobs:

| Job | id | Schedule | What |
|---|---|---|---|
| `marketing-task-sweep` | `12b1e0f820e9` | every 15m | **MCP-only** (no monitor gate): Webster lists his actionable ClickUp tasks himself and composes/reviews. |
| `clickup-chat-intake` | `f3d04e2607f5` | every 5m | `monitor_chat.py` gate → Webster reads the changed channel and replies (DM = answer all; group = only if @tagged). |
| `semrush-blog-global-feed` | `21cb02f87693` | 07:00 | Ingest SEMrush blog → global memory + Spaces. |
| `review-notify` | `415989b00956` | every 15m | Email "ready for review" to reviewers (email delivery currently unconfigured → these log as blocked). |

**Monitor-gate pattern:** a job may name a `monitor_script`/`monitor_url`; Hermes runs it each tick
and only wakes the LLM when its output hash **changes**. The task-sweep deliberately has **no** gate
(MCP-only) — Webster is smart enough to check ClickUp himself; the chat intake keeps its gate because
it does real channel-routing, not just cost-gating.

---

## 9. Timezone (Australia/Melbourne)

Two independent layers, both set on **2026-09-03**:

1. **Hermes app TZ** (governs cron firing + every timestamp): `timezone: Australia/Melbourne` at the
   top of `config.yaml`. Resolved by `hermes_time.py` (`HERMES_TIMEZONE` env → `config.yaml
   timezone` → server local) and **cached** — a `hermes-agent` **restart** applies a change. Cron
   *expressions* now mean Melbourne wall-clock (`0 7 * * *` = 7am Melbourne). Interval jobs are
   TZ-agnostic.
2. **Container OS TZ** (`date`/logs/shell): `/etc/localtime` → Australia/Melbourne on all 3 containers
   (live, survives restart, **resets on redeploy**). Durability: `TZ`+`HERMES_TIMEZONE` on the
   Coolify service; sandbox bakes `TZ` in its `Dockerfile` + `setup-sandbox.sh`.

---

## 10. Reminders (Webster capability)

Webster's SOUL `## Reminders` section: when asked ("remind me Thursday", "in 2 hours", "every Monday")
he schedules it himself with the `cronjob` tool:

- **one-off** → `cronjob(action="create", repeat=1, schedule=<cron-expr or "2h">, prompt=<self-contained>)`
  — `repeat=1` fires **once**. Schedules in Melbourne wall-clock (no UTC math).
- **recurring** → recurring cron expr / the built-in `custom-reminder` blueprint.
- **delivery** → he DMs the person with `clickup_send_chat_message` when it fires (ClickUp is not a
  cron `deliver` platform). The cron prompt must be **self-contained** (fired crons are isolated
  turns): who (name+uid), where (channel id), what.

---

## 11. Sandbox (terminal / code isolation)

Webster's and the specialists' `terminal` + `code_execution` run via `terminal.backend: ssh` into the
**`hermes-sandbox`** container — **off the prod network, no secrets** — NOT on prod-2. Image baked
with chromium, `render-card` (HTML→PNG), `qa-shot`, Pillow, rclone, tzdata. The agents SSH in as
non-root `sandbox`.

- `setup-sandbox.sh` provisions it and **wires the network link** (step 4). The link is **not**
  Coolify-managed → **re-run `setup-sandbox.sh` after any Webster redeploy** (a plain `docker restart`
  keeps it; a redeploy/recreate drops it).
- Do NOT connect the webui to the sandbox's own network (breaks Traefik routing → webui outage).

---

## 12. Making changes safely

- **SOUL edits:** read the **live** file in the container, keep a dated `.bak-*`, edit, stream it
  back as the `hermes` user, `diff` to confirm. Read per-turn → **no restart**.
- **MCP tool / `config.yaml` / model changes:** need a cache clear + restart of **both** hermes
  containers — clear `cache/mcp_schema_cache.json` + `tool_discovery_cache.json` (main **and**
  `profiles/*/cache/`) and restart, because each process caches tool schemas independently.
- **Restart vs redeploy:** `docker restart -t 30 <agent> <webui>` preserves volumes and the sandbox
  network link; a Coolify **redeploy recreates** the container → drops the sandbox link
  (re-run `setup-sandbox.sh`). Restart only in a **quiet window** (check `hermes cron runs` for
  in-flight jobs first).
- **Working rules (hard):** explain state-changing actions **before** doing them; never touch a
  running process to "nudge" it; never put yourself in the middle of Webster's autonomous flow;
  treat Webster as a peer LLM.

---

## 13. Secrets

**Infisical / Coolify env only — NEVER commit keys.** The `.gitignore` excludes `.env*`, `*.key`,
`*.pem`, `secrets/`. Sidecars read creds via `infisical_fetch.py`; LiteLLM `config.yaml` uses
`os.environ/…`. Only official / first-party MCPs; otherwise call the vendor REST API directly.

---

## 14. Known traps (quick index)

- LiteLLM `POST /v1/mcp/server` is destructive (nulls fields); no PATCH. → edit config + DELETE + restart.
- Two Hermes processes cache MCP schemas independently → clear both caches + restart both for tool changes.
- Sandbox network link is not Coolify-managed → re-run `setup-sandbox.sh` after a redeploy.
- One-shot / monitor crons: a killed run can baseline-without-processing and stick "no change" — do
  not wrap `hermes cron run` in `timeout`.
- Memory global recall needs `across_agents=True` (per-agent scoping by default).
- Postiz `delete` returns 500-means-success and only removes the Postiz record, not the live post.
- `hermes-agent` upgrades past `v0.19.0 (2026.7.20)` crash-loop the webui (wheel-install guard) — pin
  deliberately; the deployed agent is `v2026.8.18`.

---

*Keep this file current. When you change how the system works, update the matching section here —
this is the map a future session (or teammate) starts from.*
