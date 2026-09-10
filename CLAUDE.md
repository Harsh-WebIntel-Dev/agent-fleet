# CLAUDE.md — Agent Fleet

Guidance for Claude Code (and humans) working in this repo. This documents the **current, deployed**
system as of **2026-09-10** (read-only audit of prod-2 that day). Where an older subfolder README disagrees (e.g. `hermes/README.md` still
describes an "OpenClaw does the work" model), **this file is the authority** — the live system is the
Hermes-native fleet below.

---

## 1. What this is

A **multi-agent marketing fleet** running a digital-marketing agency's production work for multiple
clients. One PM agent (**Webster**) talks to people and orchestrates five specialists through a kanban
dependency chain. ClickUp is the source of truth; specialists produce the work; a human approves
before anything goes live.

- **Runtime:** [Hermes](https://github.com/nousresearch/hermes-agent) — an agent runtime with
  profiles, toolsets, an in-process cron scheduler, a webui, and an MCP client. Replaced an OpenClaw
  build on **2026-08-24** (`openclaw/`, `nemoclaw/` are **superseded**, kept for history).
- **Model gateway:** self-hosted **LiteLLM** — the single front door for models, virtual keys,
  budgets, and the aggregated MCP tool endpoint.
- **Tools:** first-party MCPs (ClickUp, Postiz, Semrush, Higgsfield) + self-hosted **sidecars**
  (`mcp-*/`).

### The agents

| Profile | Role | Owns (concrete tools) |
|---|---|---|
| **Webster** (default profile, bot user `106813628`) | PM / client liaison: talks to people, runs the client-readiness gate, composes the work, runs the review gate, relays approvals. Does **not** produce deliverables himself. | `cronjob`, `kanban`, `delegation`, `memory`, ClickUp + chat + email (via `pm_comms` MCP), sandbox terminal/code |
| **seo** | Keyword & search strategy | **Semrush** + web search → keyword/on-page brief |
| **researcher** | Sourced facts & competitive intel | **Firecrawl**, web search, Semrush |
| **writer** | The words: blog/social/newsletter/ad/page copy | writing craft (reads seo brief + researcher facts) |
| **producer** | Visual assets | **Higgsfield** (images + short-form video), `render-card` (HTML→PNG cards), R2/Spaces |
| **publisher** | Staging & publishing (**draft-only, human-gated**) | **WordPress** (`wp_*`), **Postiz** (social + Google Business Profile), **Mailchimp** (newsletter drafts), **Lnk.Bio** |

Delegation is **dynamic** — Webster composes each job from a capabilities catalogue in his SOUL and
builds only the cards a task needs (no fixed pipeline). **Do not hand-deliver work to Webster via CLI**
(`hermes -z`, forced cron) — the fleet runs without a human/Claude in the middle. Fix the *intake
mechanism*, never the individual task.

---

## 2. Where it runs & how to reach it

- **Host:** prod-2 `46.250.245.204`, SSH alias **`webintelligenz-prod-2`** (user `harsh`, in `docker`
  group → `docker` works without sudo; root login is not key-authorized). Managed by **Coolify**.
- **Containers** (share the `hermes-home` volume at `/home/hermes/.hermes`):
  - `hermes-agent-zhvjhbo5752ovx1nl2rk9v30` — agent + in-process cron scheduler (the "gateway") **and**,
    since 2026-09-04, the dashboard / Hermes-Desktop backend on **`100.115.104.5:18795`** (tailnet-only,
    basic-auth: `/` → 302 `/login`, every `/api/*` except `/api/health` → 401 without credentials) plus
    A2A on **`100.115.104.5:18794`** (bearer; unauthenticated POST → 401; `tailscale serve` HTTPS
    `…ts.net:8447` fronts it). `traefik.enable=false`.
  - There is **no `hermes-webui` container any more** (removed 2026-09-04, image gone from the host).
    Coolify still holds `hermes-webui` and `hermes-serve` application records in `exited` state under
    service `hermes-agent-for-webintelligenz`, which is why Coolify reports the service
    `degraded:unhealthy` while `docker` reports the agent healthy.
  - `hermes-sandbox` — the terminal/code sandbox (see §12).
- **Hermes CLI:** **`/opt/hermes/.venv/bin/hermes`** inside `hermes-agent` (NOT on `$PATH`; `tirith`
  in `~/.hermes/bin` is a shell-security guard, not the CLI).
  ```bash
  C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
  docker exec -u hermes "$C" /opt/hermes/.venv/bin/hermes cron list
  ```
- **Key files** (`/home/hermes/.hermes/`): `SOUL.md` (Webster), `profiles/<name>/SOUL.md` (specialists),
  `config.yaml`, `cron/jobs.json`. **SOULs are read per-turn — edit live, no restart.** The deployed
  SOULs are the **source of truth**; `hermes/souls/*.SOUL.md` in this repo are original templates that
  **drift** — always read the live file first and keep a dated `.bak-*`.
- **No public webui.** `https://wi-agent.widev.com.au` still resolves and Traefik still holds the
  router (the Coolify `hermes-webui` record keeps the FQDN) but it answers **503** — nothing is behind
  it. The only operator UI is the tailnet dashboard slot above.

---

## 3. Architecture

```
   people: ClickUp chat/tasks  +  email (technology@webintelligenz.com)
                 │
                 ▼
   ┌─────────────────────────────┐   ClickUp = single source of truth
   │  WEBSTER (PM, default)      │   (tasks, comments, chat, approvals)
   │  intake: 2 cron sweeps      │
   │   • marketing-task-sweep    │──── lists his actionable tasks via ClickUp MCP (no gate)
   │   • clickup-chat-intake     │──── monitor_chat.py gate → reads the changed channel
   │  gate → compose → review    │
   └──────────────┬──────────────┘
                  │ dependency-chained kanban cards (one per stage), billed to the client's key
                  ▼
   kanban dispatcher (in Hermes)  ── starts each specialist when its parents are `done`
      ┌───────────┬───────────┬───────────┬───────────┐
      ▼           ▼           ▼           ▼           ▼
    seo      researcher    writer     producer    publisher
      └───────────┴─────┬─────┴───────────┴───────────┘
                        ▼
         ┌──────────────────────────────────┐
         │  LiteLLM gateway (fleet-core-     │  models · virtual keys · budgets
         │  litellm)  →  /mcp/ aggregated    │  · aggregated MCP tool endpoint · vision · embeddings
         └───────────────┬──────────────────┘
                         ▼
   ClickUp · Postiz · Semrush · Higgsfield (first-party MCPs)
   + sidecars: memory · postiz-extras · lnkbio · mailchimp · wordpress · spaces · firecrawl
                         │
                         ▼
     mcp-memory (Postgres+pgvector, per-client RLS)  ·  R2/Spaces (assets)
```

---

## 4. How Webster operates (the work lifecycle & rules)

This is the heart of the system — the discipline in Webster's SOUL.

**Access.** Any ClickUp-workspace member can reach him: a 1:1 DM, an @mention in a group
(`@webster` / `#user_mention#106813628`), a task assigned to bot `106813628`, or an email to
**technology@webintelligenz.com**. No per-user allowlist for chat/tasks; email has an
SPF/DKIM/DMARC-verified sender allowlist (WI team only).

**1 — Client-readiness / brand-kit gate (before ANY work).** He won't decompose a task until he holds
that client's essentials: business basics, brand voice & policy, logo (and NAP / social handles when
the work is local/social). He checks `spaces_read(clients/<slug>/brand-kit.md)` + `memory_search(…,
client="<slug>")` — *recorded evidence only, never a vibe*. Missing anything → run the
**`client-brand-kit`** skill: reply listing exactly what's needed, hold the work (comment on the task,
**no cards**), ask **once**, then save answers to the brand kit + memory so he never re-asks. A
brand-new client (`memory_search` → "unknown client") is registered with `memory_register_client`.

**2 — Per-client budget billing.** Each onboarded client has its own LiteLLM key + monthly budget.
Webster sets **`provider="litellm-<slug>"` + `tenant="<slug>"`** on every `kanban_create` so the
specialist runs on that client's key. Slugs: `biogone`, `pride-advice`, `radiance-wealth`; Web
Intelligenz's own work uses the default (`litellm` key — omit `provider`). Never guess a slug (a wrong
one silently mis-bills another client). **WordPress is NOT yet per-client** — the shared WP connects
only to webintelligenz.com, so a client's WP *publishing* is held until per-client WP is wired
(everything else — writing, images, social, SEO, research — bills the client key fine).
**Live caveat (2026-09-10):** the `LITELLM_KEY_BIOGONE` / `_PRIDE_ADVICE` / `_RADIANCE_WEALTH` env
vars are **not set** on the `hermes-agent` container, so `provider="litellm-<slug>"` has no key to
resolve; every card on the board so far (91 active) has `tenant` and `provider_override` null, and all
spend lands on the `hermes-webintelligenz` key (the Pride Advice Fathers' Day cards included).

**3 — Compose → dispatch.** He builds a dependency-chained set of kanban cards (only the stages the
task needs), each carrying the ClickUp `task_id`, a stage brief in his words, and "read the task +
comments first". Build the graph with `kanban decompose` where possible; verify parent links. The
dispatcher runs each specialist when its parents are `done`. Then he tells the person it's with the
team and stops. (The dispatcher runs in the gateway every 60s; concurrency is capped at **4 cards
in progress fleet-wide, 1 per specialist** — `kanban:` in config.)

**4 — The review gate is Webster's — there is NO QA agent.** Nothing reaches a human until he has:
   1. **Re-fetched every artefact himself** (`wp_get_post`, hero URL, Postiz preview) — a specialist's
      claim is NOT evidence (the fleet has reported fabricated post ids / invented URLs).
   2. Checked it against **brief + brand kit** (house voice, Australian English, two-word brand-name
      rule, logo/colours, claims/compliance) and **compliance** (no absolute/unverifiable claims, no
      phone numbers in Google Business posts).
   3. **Attached visual QA screenshots** — `qa-shot <url> [w] [h]` in his sandbox renders a page to a
      small JPEG (desktop + a `390 1600` mobile pass), prints base64; `clickup_attach_task_file(...)`
      attaches it. Only surfaces he can actually reach (live URL, public Postiz preview
      `postiz.widev.com.au/p/<id>`, or an R2 card pulled via `rclone`). Auth-gated WP previews aren't
      reachable from the isolated sandbox — he says so plainly, never fabricates a shot.
   4. Moved the task to `in review`, added **all four reviewers**, posted review links + QA shots.

**Reviewers — any ONE may approve or reject:** Paul Thewlis `312732`, Harry Cade `316464`,
Nipuni Gamage `2772136`, Harsh `312719`.

**5 — THE BOARD IS THE EVIDENCE, not the comments.** (Broken once in production: the agent believed old
comments and reported "complete" when no specialist had run.) **No kanban cards for a task → the work
has NOT been done**, whatever a comment claims. `in review` requires all three: cards he created,
every one `done`, and artefacts he re-fetched **this run**. He never re-reports a figure (keyword
volume, post id, char count) that didn't come from a tool call he made this run.

**6 — Blocks route to the stage that OWNS the defect** (symptom ≠ location): hero-too-large/413 →
producer; voice/length/CTA/factual → writer; keyword/meta/slug → seo; unsourced claim → researcher.
Re-running an upstream stage does NOT re-flow the ones below — reset the stage **and every descendant**
with an explicit "REDO … produce X differently" (or the stage sees the old artefact and declares
itself done). Bounded to **2 automatic rework cycles**, then a loud stop + escalate to reviewers.

**7 — Relay decisions; publishing is human-gated, always.** `in review` is NOT approval. On
**approved** → publisher publishes against the **existing** draft (reuse ids, never a second one),
Webster verifies a real live URL himself, then moves the task to `completed`. On **changes** → route
to the owning stage with the exact words + reset descendants. He never claims something is live without
verifying the URL, and never publishes / arms social / sends a newsletter until a human approved that
specific piece.

**8 — Email front door.** `technology@webintelligenz.com` (Gmail IMAP+SMTP; platform `enabled` in
config). A request/feedback email is treated like a ClickUp DM (gate → compose → reply); a
"knowledge" email (a colleague forwarding something useful) → he extracts the durable insight into
memory (global or client-scoped) and confirms. Email is a front door, never a second source of truth,
and never approval to go live. Email is **Webster-only** — pinned OFF on the 5 specialists.

**Voice:** practical, Australian English, no hype, no emoji; admits uncertainty; reports only what
actually happened.

---

## 5. Repo layout

| Path | What it is | Status |
|---|---|---|
| `hermes/` | The fleet: `souls/` (agent prompts, templates), `skills/marketing/` (blog/socials/newsletter/client-brand-kit/client-webintelligenz), `scripts/` (`monitor_*.py`), `sandbox/` (Dockerfile + `render-card`/`qa-shot`), `setup-sandbox.sh`, `compose.yml`, `config.yaml.template` | **current** |
| `litellm-wrapper/` | LiteLLM image wrapper (`infisical_fetch.py`, `litellm_entrypoint.sh`, `LITELLM_CONFIG_B64`) | current |
| `mcp-memory/` | Vectorised fleet memory (Postgres+pgvector, per-client RLS, per-agent) | current |
| `mcp-postiz-extras/` | Postiz mgmt the built-in MCP lacks (`postiz_list/delete/set_status/edit`) | current |
| `mcp-lnkbio/` | Lnk.Bio link-in-bio (rolling top-5), WI-only | current |
| `mcp-mailchimp/` | Mailchimp newsletter drafts (draft-only, WI-only) — key staged in Infisical 2026-09-07; Coolify service **stopped by hand** (`exited`) since 2026-09-08 | current, stopped |
| `mcp-higgsfield/` | Higgsfield image/video generation | current |
| `mcp-wordpress/` | Custom `wp-json` WordPress sidecar (7 tools) | current |
| `mcp-spaces/` | R2 / DO Spaces asset store (ingest/presign/read/write) | current |
| `mcp-a2a/` | Agent-to-agent comms (`ask_pm`/`create_pm_task`) | OpenClaw-era, mostly legacy |
| `mcp-social-extras/` | older social helper | legacy |
| `firecrawl/` (+ `searxng/`) | self-hosted **Firecrawl** (runs with **SearXNG** + Redis + RabbitMQ + nuq-postgres) — the fleet's web-crawl/search backend, reached via the Hermes `web/firecrawl` plugin. Its containers carry Coolify names (`api-da1g1lpeilodrfywwglgasgf`, `searxng-…`, `rabbitmq-…`), not "firecrawl" | current (infra) — **API down, see §7** |
| `clickup-bridge/`, `clickup-sweep/` | OpenClaw-era ClickUp intake (`bridge.js`/`sweep.js`) | **superseded** by Hermes crons |
| `openclaw/`, `nemoclaw/`, `plan/` | earlier builds + planning | **superseded** / reference |

---

## 6. Platform infrastructure — LiteLLM · Cloudflare · Infisical

### LiteLLM gateway (models, keys, tools)

- Coolify service `fleet-core-litellm` (`v10up2yg1cwxo0k1ks9j2qro`). Running image **`litellm-fleet:1`**
  = `ghcr.io/berriai/litellm:main-stable` wrapped by **`litellm-wrapper/`** — a fail-safe entrypoint
  that at boot pulls its provider keys, tool tokens, and `LITELLM_MASTER_KEY` from **Infisical
  `/shared`** (`DEEPSEEK_API_KEY`, `DO_INFERENCE_KEY`, `SEMRUSH_API_KEY`, `POSTIZ_MCP_TOKEN`,
  `CLICKUP_MCP_TOKEN`), falling back to the Coolify env if Infisical is unreachable (never blocks boot).
- **Live config is a host bind-mount: `/home/harsh/litellm-cfg/config.yaml`** (NOT
  `nemoclaw/litellm/config.yaml`, an older copy; a `LITELLM_CONFIG_B64` env mechanism also exists).
  `docker restart` the container reloads it (~108s to healthy).
- **Aggregated MCP endpoint** `/mcp/` (streamable-HTTP). Servers are grouped by **access group**
  (`fleet_tools`, `fleet_internal`, `wi_tools`) and can carry a server-level `allowed_tools` allow-list.
- **Keys / scoping:**
  - **WI agency key** grants by **access-group** → auto-inherits new servers. All profiles' `pm_comms`
    uses this key, so WI-only scoping (lnkbio, mailchimp) is **SOUL policy**, not a hard boundary.
  - **3 client keys** (biogone / pride-advice / radiance-wealth) grant by explicit **server-ID list**
    → must be updated per new server; the TEAM must allow a server before a key can.
- **Models** (`model_aliases` + LiteLLM routing): Webster **and all five specialists run on `standard`**
  (`model.default: standard`, `provider: litellm` in `config.yaml` and every `profiles/*/config.yaml`;
  `standard` → DeepSeek V4 Pro). `flash` (deepseek-v4-flash, ~16× cheaper) is offered by the provider
  but is **nobody's default** — the "Webster converses on flash" design was never applied live. DeepSeek-direct via the deepseek key; other models via DigitalOcean GenAI
  (`inference.do-ai.run`, `DO_INFERENCE_KEY`) — premium tiers can be 403 tier-gated. **Vision** for all
  agents is a LiteLLM `vision` alias (→ DO llama-4-maverick), wired via `auxiliary.vision` in
  `config.yaml`. **Embeddings** = `embed` alias (bge-m3, 1024-dim) used by mcp-memory. Tiers seen:
  `flash`, `fast`, `standard`, `deep`, `vision`, `embed`.
- **Model routing & per-client billing (`providers:` in the Hermes config).** Hermes reaches models
  ONLY through litellm (`…:4000/v1`, `discover_models:false`). The default provider `litellm` uses the
  WI agency key (`OPENAI_API_KEY`); three per-client providers `litellm-<slug>` use that client's
  `key_env` (`LITELLM_KEY_BIOGONE`, `_PRIDE_ADVICE`, `_RADIANCE_WEALTH`). Each stamps an
  `x-litellm-tags` header (agent/client/fleet) for spend attribution. Setting `provider="litellm-<slug>"`
  on a kanban card (§4) makes the specialist run on that client's key → their budget. Key/budget/spend
  **state lives in `litellm-postgres`**; litellm also has a `tailscale-proxy` sidecar.
- **⚠️ DESTRUCTIVE TRAP:** `POST /v1/mcp/server` **replaces** a record and **nulls** omitted fields
  (caused a ~15-min Postiz outage). No PATCH. To change a config-defined server: edit `config.yaml` +
  `DELETE /v1/mcp/server/{id}` + restart (re-seeds from config). A key's `blocked_tools` does NOT
  filter `tools/list` — use server `allowed_tools`.

### Cloudflare (DNS + R2)

- **DNS.** `widev.com.au` is on **Cloudflare** (nameservers `simon`/`hope.ns.cloudflare.com`). The
  wildcard `*.widev.com.au` and the fleet records (`wi-agent`, `postiz`, …) point **directly to
  prod-2 `46.250.245.204` — DNS-only, NOT proxied**; Traefik on the box terminates TLS, so every
  FQDN is internet-facing the moment its container starts. Cloudflare here is authoritative DNS, not
  a proxy/WAF. (The agency's own site webintelligenz.com is also on Cloudflare, but that's separate.)
- **R2 (object storage) — the fleet asset store.** Account `023cf06b87e6b0abe3065ec0a8f79b79`,
  endpoint `…r2.cloudflarestorage.com`, bucket **`fleet-clients`**, key layout `clients/<slug>/…`
  (brand kits, templates, fonts, logos, rendered cards, QA sources). Two access paths:
  - **mcp-spaces** (`fleet-core-mcp-spaces`) — S3-compatible MCP (`spaces_ingest_url/presign/read/
    write`). Env names are legacy `DO_SPACES_*` but point at R2 (live: `BUCKET=fleet-clients`,
    endpoint `023cf06b….r2.cloudflarestorage.com`). Creds (`R2_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`)
    pulled from Infisical.
  - **sandbox rclone** (`r2:fleet-clients`) — the sandbox's ONE outbound capability, for moving full
    binary brand kits the `spaces_*` tools can't. Token is **bucket-scoped + IP-filtered to prod-2**,
    staged at recreate via `R2_CREDS_FILE` (not baked) — re-provide it when re-running `setup-sandbox.sh`.

### Infisical (secrets store)

Self-hosted secrets = Coolify service `fleet-core-infisical` (containers: `infisical` + postgres +
redis + a **tailscale-proxy** sidecar). **Tailnet-only — not internet-facing.**

- **Access:** service-to-service via the Tailscale IP **`http://100.115.104.5:18789`** (the
  `INFISICAL_API_URL` default in `infisical_fetch.py`); admin UI via Tailscale Serve HTTPS `…ts.net:8443`.
- **Auth:** machine identity (Universal Auth `clientId`/`clientSecret`). Secrets live at
  `environment=prod`, path **`/shared`** (fleet-wide) + **`/clients/<slug>`** (per-client).
- **Integration = PULL, fail-safe.** Every service's entrypoint runs `infisical_fetch.py` to pull the
  secrets it needs at boot; if Infisical is down it falls back to the Coolify env and boots anyway.
  Coolify is **not** a sync target — Infisical never pushes.
- **Gotchas:** a `SITE_URL`/HTTPS misconfig blocks machine-identity creation; the `fleet-hermes`
  identity is **read-only Viewer** — it can neither create nor **edit** secrets (a self-write returns
  `403 You are not allowed to edit on secrets`). That is why Higgsfield credential rotation cannot
  persist itself to Infisical (§16). Staging or granting `secrets:edit` needs an admin token.

---

## 7. MCP tools & sidecars

All agents reach tools through the litellm **aggregated `/mcp/`** endpoint (Hermes calls it
`pm_comms`), scoped by **access group**: `fleet_tools` (all fleet), `fleet_internal` (ClickUp only),
`wi_tools` (Web-Intelligenz-only). The **10 registered MCP servers** (`mcp_servers:` in
`litellm-cfg/config.yaml`):

| MCP | Where | Group | Key tools | Notes |
|---|---|---|---|---|
| **clickup** | external `mcp.clickup.com` | `fleet_internal` | full ClickUp: tasks, lists, comments, chat, docs, time | **no** tool restriction → the whole toolset (that's why Webster can list tasks + DM) |
| **semrush** | external `mcp.semrush.com/v2` | `fleet_tools` | keyword / backlink / organic / competitor / site-audit research | header auth (`SEMRUSH_API_KEY_HEADER`) |
| **postiz** | self-hosted `postiz.widev.com.au` | `fleet_tools` | `integrationSchedulePostTool`, `integrationList`, `groupList`, `integrationSchema`, `triggerTool`, `generateImage/VideoTool`, `uploadFromUrlTool` | social + Google Business Profile; 10-tool allow-list (excludes `ask_postiz`). **Live 2026-09-11: publishing is dead.** Last `PUBLISHED` post 2026-09-06 20:00 Melbourne; 14 posts dated 07–10 Sep still `QUEUE` (0 `ERROR`). The publisher is the pm2-managed **`orchestrator`** (Temporal worker) inside the `postiz-…` container: OOM-killed in the 07–08 Sep memory exhaustion (`exited with code [137]`, 2026-09-07 17:18Z, pm2 ↺14), the restart came up hung — no listener on `:3002`, no log output since, and Temporal task queue `main` shows a 35-workflow backlog aged ~3 days with **zero pollers**. The Docker healthcheck only probes the frontend `:5000`, so Docker/Coolify report `healthy`. Repair owned by AO worker agent-fleet-21 (`postiz-orchestrator`). |
| **postiz_extras** | sidecar `mcp-postiz-extras` | `fleet_tools` | `postiz_list`, `postiz_delete`, `postiz_set_status`, `postiz_edit` | fills gaps the built-in Postiz MCP lacks; delete-500 = success; 15-min past-slot guard on edit |
| **lnkbio** | sidecar `mcp-lnkbio` | **`wi_tools`** | `lnkbio_list`, `lnkbio_set_link` | **WI-only**; rolling top-5 (adds a link, drops the oldest) |
| **mailchimp** | sidecar `mcp-mailchimp` (node) | **`wi_tools`** | 17 **draft-only**: `create_campaign`, `update_campaign`, `set_campaign_content`, `send_test_email`, `list_audiences/templates/campaigns`, … | send/schedule/delete withheld at the gateway; key staged 2026-09-07 and the tools were used that day. Container **stopped by hand** since 2026-09-08 (`NODE_OPTIONS=--unhandled-rejections=warn` is set on the Coolify service); while stopped LiteLLM logs `MCPServerListError … 'mailchimp'` on every discovery (~50/h) and the aggregate is **110** tools, not 128 |
| **wordpress** | sidecar `mcp-wordpress` | `fleet_tools` | `wp_create_draft`, `wp_update_post`, `wp_get_post`, `wp_publish`, `wp_upload_media`, `wp_list_categories`, `account_status` | content-bot role; **WI site only** (not per-client yet) |
| **higgsfield** | sidecar `mcp-higgsfield` | `fleet_tools` | `create_image_job`, `get_image_job`, `list_image_models`, `verify_url`, `account_status` | async: `create_image_job` → poll `get_image_job`. `account_status` also reports credential health (`status: ok\|seed_drift`, expiry, `refresher_running`). **Deployment caveat:** the durability code (`credguard.py`, `infisical_push.py`, `refresher.py`, `bootstrap.py`) lives in the **running container's writable layer** (`docker diff`), not in the image Coolify would redeploy — and the tag Coolify's record names (`mcp-higgsfield:0.2.0`) **no longer exists** on the host |
| **spaces** | sidecar `mcp-spaces` → **R2** | `fleet_tools` | `spaces_list`, `spaces_read`, `spaces_write`, `spaces_ingest_url`, `spaces_presign`, `spaces_delete` | asset store, bucket `fleet-clients` (see §6 Cloudflare) |
| **memory** | sidecar `mcp-memory` | `fleet_tools` | `memory_remember`, `memory_search`, `memory_stats`, `memory_register_client` | pgvector, per-client RLS + per-agent (§8) |

**Not in the registry:** Firecrawl is a Hermes **plugin** (`web/firecrawl`), not a litellm MCP.
**Live 2026-09-10 — the Firecrawl API is down.** `http://api-da1g1lpeilodrfywwglgasgf:3002` refuses
connections from `hermes-agent` (nothing listens on 3002 inside the container; only the queue-worker
is alive). Root cause: the stack's `REDIS_URL` host is the bare name `redis`, and on the shared
`coolify` network (IPv6-enabled) that name resolves AAAA-first to **`coolify-redis`** (alias `redis`,
`--requirepass`), so the worker loops on `NOAUTH Authentication required` (~17k lines/h; 26.7k of the
container's 120k log lines). With `web/ddgs` disabled there is **no fallback** — `web_extract` /
`web_search` fail for every agent (`HTTPConnectionPool(host='api-…', port=3002)`); the semrush feed
cron only survives by fetching through the sandbox terminal.
`mcp-a2a` (`ask_pm`/`create_pm_task`/`notify_client_hermes`) and `mcp-social-extras` are
**OpenClaw-era / legacy** (superseded by `postiz_extras` + `lnkbio`; the a2a Coolify service is exited).

### Sidecar build pattern

Each `mcp-*/` is a self-hosted MCP server, its own Coolify service:
`python:3.12-slim` (mailchimp = `node:20-slim`), `from mcp.server.mcpserver import MCPServer`,
`@mcp.tool()`, `mcp.run(transport="streamable-http", host="0.0.0.0", port=8080,
streamable_http_path="/mcp", stateless_http=True)`. `infisical_fetch.py` pulls creds (Infisical
`/shared` + per-client paths, Coolify-env fallback). Must be on the litellm docker network
(`connect_to_docker_network=true` / "Connect to Predefined Networks" toggle) and bind `0.0.0.0`; wire
into `litellm-cfg/config.yaml` under `mcp_servers:` with an `access_groups:` entry (and an
`allowed_tools:` allow-list to withhold tools, as postiz/mailchimp do).

**Deployed Coolify services** (fleet-relevant): `fleet-core-litellm`, `fleet-core-mcp-memory`,
`fleet-core-mcp-spaces`, `fleet-core-infisical`, `fleet-core-mcp-a2a` (exited), `mcp-postiz-extras`,
`mcp-lnkbio`, `mcp-mailchimp`, `mcp-higgsfield`, `mcp-wordpress`, `firecrawl`, `hermes-agent-for-
webintelligenz`, plus `postiz`, `n8n`, `metabase`, and the `elmo*` trackers.

Several are **multi-container stacks**: `fleet-core-litellm` (+ Postgres + tailscale-proxy),
`fleet-core-infisical` (+ Postgres/Redis/tailscale-proxy), `firecrawl` (+ SearXNG + Redis), and the
self-hosted `postiz` (+ a full **Temporal** stack: temporal/ui/postgresql/elasticsearch + Redis —
Postiz's scheduler; upgrading Postiz is not a tag bump). **Container-naming trap:** Coolify names stack members `<compose-service>-<service-uuid>`, so nothing in `docker ps` says "firecrawl" — the stack is `api-`, `nuq-postgres-`, `playwright-service-`, `rabbitmq-`, `redis-`, `searxng-da1g1lpeilodrfywwglgasgf` — and a grep for `postiz` finds one of seven: the stack is `postiz-`, `postgres-`, `redis-`, `temporal-`, `temporal-ui-`, `temporal-postgresql-`, `temporal-elasticsearch-hooyxybihdd13t7g2nfovaw1`. Filter by suffix, not by product name. `n8n`/`metabase`/`elmo*` belong to the
separate dashboard/AI-visibility projects on the same box, not the fleet core.

---

## 8. Memory (`mcp-memory`)

- Postgres + **pgvector** (bge-m3, 1024-dim via the LiteLLM `embed` alias). DSN in
  `/run/secrets/memory.env` (not the container env).
- **Three isolation layers:** `x-client-slug` header pins the client (the agency key falls back to a
  `client` arg); restricted `fleet_app` role with FORCED **row-level security** per `app.client_id`;
  `agent_id` narrows recall to the author.
- Tools: `memory_remember`, `memory_search` (default = **your own** agent's notes;
  `across_agents=True` = all agents for that client), `memory_stats`, `memory_register_client`.
- **Global scope** (`client="global"`): the daily SEMrush feed writes here (`agent="webster"`).
- **⚠️ Gotcha (fixed 2026-09-03):** specialists recall global with their own `agent_id`, so
  webster-authored global entries were invisible until their SOULs were changed to pass
  `across_agents=True`. Any global knowledge a specialist must consume needs `across_agents=True`.

---

## 9. Intake & cron

The in-process scheduler runs in `hermes-agent`; jobs in `cron/jobs.json`; manage with
`hermes cron {list,create,edit,run,remove,runs,status}`. It **re-reads `jobs.json` every tick** →
`hermes cron edit` applies next tick, **no restart**. Active jobs:

| Job | id | Schedule | What |
|---|---|---|---|
| `marketing-task-sweep` | `12b1e0f820e9` | 15m | **MCP-only** (no gate): Webster lists his actionable ClickUp tasks himself and composes/reviews. |
| `clickup-chat-intake` | `f3d04e2607f5` | 5m | `monitor_chat.py` gate → reads the changed channel; DM = answer all, group = only if @tagged. |
| `semrush-blog-global-feed` | `21cb02f87693` | 07:00 | Ingest SEMrush blog → global memory + Spaces `clients/global/semrush-feed.md`. |
| `review-notify` | `415989b00956` | 15m | `monitor_review.py` gate → emails reviewers "ready for review". `deliver` = `email:<addr>` on **all four** reviewer entries (prefix fixed 2026-09-07). **Intermittent, not dead:** it delivers when the email adapter is connected (2026-09-09 16:22 — all four `delivered … via live adapter`), and the same job is `BLOCKED by pre-dispatch config validation — 'email' has no gateway credentials` whenever the adapter's IMAP fetch has just timed out (`email_imap_fetch_failed`, ~8×/day in `errors.log`). |

**Monitor-gate pattern:** a job may name a `monitor_script`/`monitor_url`; Hermes runs it each tick and
wakes the LLM only when its output hash **changes**. The task-sweep deliberately has **no** gate; the
chat intake keeps its gate because it does real channel-routing, not just cost-gating.

**⚠️ Cron traps:** (a) a killed/`timeout`-wrapped `hermes cron run` can baseline-without-processing and
stick "no change" — never wrap it in `timeout`. (b) A cron's first tick fired inside the UI session
(formerly the webui, now the dashboard) that created it records a **false** `failed` ledger status — re-run it standalone with `hermes cron run
<id>` to prove/reset.

---

## 10. Timezone (Australia/Melbourne)

Two independent layers, both set 2026-09-03:
1. **Hermes app TZ** (cron firing + timestamps): `timezone: Australia/Melbourne` at the top of
   `config.yaml`. Resolved by `hermes_time.py` (`HERMES_TIMEZONE` env → config `timezone` → server
   local), **cached** → a `hermes-agent` restart applies a change. Cron *expressions* now mean
   Melbourne wall-clock; interval jobs are TZ-agnostic.
2. **Container OS TZ** (`date`/logs): `/etc/localtime` → Melbourne on all 3 (live; resets on redeploy).
   Durability: `TZ`+`HERMES_TIMEZONE` on the Coolify service; sandbox bakes `TZ` in its Dockerfile +
   `setup-sandbox.sh`.

---

## 11. Reminders (Webster)

SOUL `## Reminders`: on request he schedules with `cronjob` — **one-off** `cronjob(action="create",
repeat=1, schedule=<cron-expr or "2h">, prompt=<self-contained>)` (`repeat=1` = "once", Melbourne
wall-clock); **recurring** = a recurring expr / the `custom-reminder` blueprint. He **DMs** the person
with `clickup_send_chat_message` when it fires (ClickUp isn't a cron `deliver` target). The cron prompt
must be **self-contained** (fired crons are isolated turns): who (name+uid), where (channel id), what.

---

## 12. Sandbox (terminal / code isolation)

Terminal + code_execution run via `terminal.backend: ssh` into **`hermes-sandbox`** — non-root
`sandbox` user, 2 GiB memory cap, on its own `hermes-sandbox` network plus the agent's Coolify service
network (**not** the shared `coolify` network). It is *not* air-gapped: it **has internet egress**
(HTTP 200 to public sites) and holds **one** credential — the bucket-scoped, IP-filtered R2 rclone
token at `/home/sandbox/.config/rclone/rclone.conf` (run `rclone` as `-u sandbox`; as root it finds no
config). Image `hermes-sandbox:2` (2026-08-31) bakes chromium, `render-card` (HTML→PNG), `qa-shot`,
Pillow, rclone, tzdata. **Not baked:** the Jost + Open Sans brand fonts and the font-preflight
`render-card` (2026-09-08) exist only in the running container's writable layer (`docker diff
hermes-sandbox`) — a recreate loses them; the baked rebuild is PR #15 (`hermes/sandbox/REBUILD-RUNBOOK.md`).
`setup-sandbox.sh` provisions it and **wires the network link (step 4) — re-run it after any Webster
redeploy** (a `docker restart` keeps the link; a redeploy/recreate drops it). Never attach a
Traefik-fronted container to the sandbox's own network (that is what broke the old webui).

---

## 13. Making changes safely

- **SOUL edits:** read the **live** file, keep a dated `.bak-*`, edit, stream back as `hermes`, `diff`.
  Read per-turn → **no restart**.
- **MCP tool / `config.yaml` / model changes:** clear `cache/mcp_schema_cache.json` +
  `tool_discovery_cache.json` (main **and** `profiles/*/cache/`) and **restart the `hermes-agent`
  container** — there has been a single Hermes process since 2026-09-04 (the "restart both" rule only
  returns if a second container is ever re-added).
- **Restart vs redeploy:** `docker restart -t 30 <agent>` preserves volumes + the sandbox link;
  a Coolify **redeploy recreates** the container → drops the sandbox link (re-run `setup-sandbox.sh`).
  Restart only in a **quiet window** (`hermes cron runs` shows nothing in-flight).
- **Hard rules:** explain state-changing actions **before** doing them; never touch a running process
  to "nudge" it; never put yourself in the middle of Webster's autonomous flow; treat Webster as a peer
  LLM.

---

## 14. Secrets

**Infisical / Coolify env only — NEVER commit keys.** `.gitignore` excludes `.env*`, `*.key`, `*.pem`,
`secrets/`, backups, scratch. Sidecars read creds via `infisical_fetch.py`; LiteLLM config uses
`os.environ/…`. Official / first-party MCPs only; else call the vendor REST API directly. The
secrets store is self-hosted **Infisical** — see §6 for its access, auth, and pull-based model.

---

## 15. Known traps (index)

- LiteLLM `POST /v1/mcp/server` is destructive (nulls fields); no PATCH → edit config + DELETE + restart.
- (Historical — one container since 2026-09-04) two Hermes processes cache MCP schemas independently →
  if a webui/serve container is ever re-added, clear both caches + restart both for tool changes.
- The shared `coolify` network is IPv6-enabled and `coolify-redis` carries the alias **`redis`** on it —
  any stack that joins that network and names its Redis `redis` (Firecrawl does) resolves to Coolify's
  password-protected Redis and dies on `NOAUTH`.
- `mcp-higgsfield` runs hot-patched code (container layer only) from an image tag that no longer exists
  — a Coolify redeploy/recreate reverts or fails the sidecar; merge + build + deploy PR #16 first.
- Coolify's `hermes-agent-for-webintelligenz` service still lists `hermes-webui` and `hermes-serve` as
  members (`exited`) → a Coolify-level restart/redeploy may resurrect a second gateway on the shared
  `hermes-home` volume (the email/A2A hijack) and a port-18795 clash. Prefer `docker restart` on the
  agent container until those records are removed.
- Sandbox network link is not Coolify-managed → re-run `setup-sandbox.sh` after a redeploy.
- Never wrap `hermes cron run` in `timeout` (baselines-without-processing → stuck "no change"); a
  cron's first tick inside its creating UI session logs a **false** `failed`.
- Memory global recall needs `across_agents=True` (per-agent scoping by default).
- Postiz `delete` returns 500-means-success and removes only the Postiz record, not the live post; a
  recreate at a slot already passed publishes a **duplicate** (postiz-extras has a 15-min past-slot guard).
- (Obsolete since 2026-09-04 — no webui container) `hermes-agent` past `v0.19.0 (2026.7.20)`
  crash-looped the *webui* (wheel-install guard). Deployed agent is `v2026.8.18` = `v0.20.4`; keep the pin deliberate.
- prod-2 load is largely hypervisor **CPU steal** (`vmstat` `st` 27–61 % on 2026-09-10), not fleet
  workload — removing services won't fix it. **But** 2026-09-07/08 was a genuine memory exhaustion
  (swap full, load 273) with **three independent production casualties**: the Postiz `orchestrator`
  was OOM-killed and came back hung (publishing dead since 06 Sep — §7), `temporal-elasticsearch`
  restarted 16×, and the Higgsfield credential refresh stalled in the same window (per agent-fleet-1/19).
  Also that day: `hermes-agent` + `hermes-sandbox` carry `OOMKilled=true`, the gateway process restarted
  8×, LiteLLM 11×, Infisical 23× (cumulative), firecrawl's rabbitmq went unhealthy for 3 days. Watch
  `free -m`, not just load.
- A container's `healthy` only means its healthcheck passed — Postiz's checks the frontend `:5000`, not
  the pm2 `orchestrator` that actually publishes. `docker exec postiz-… pm2 list` (restart count ↺,
  RSS) and `temporal task-queue describe --task-queue main` (pollers) are the real signals.
- Temporal's CLI inside `temporal-hooyx…` binds the container IP, not loopback: pass
  `--address 10.0.12.7:7233` (or the current IP) or every command is a false "connection refused".

---

## 16. Open items

- **Postiz publishing outage (CRITICAL)** — orchestrator hung since 2026-09-07 17:18Z, 14 posts queued
  for 07–10 Sep never went out (§7). Repair owned by agent-fleet-21; after it, add an orchestrator-aware
  healthcheck (`:3002` or pm2 status) so a hung worker cannot sit behind a `healthy` container again.
- **Mailchimp** — key staged 2026-09-07 and working; the container is **stopped by hand**. Decide:
  restart it, or drop it from `mcp_servers` in `litellm-cfg/config.yaml` so LiteLLM stops logging
  `MCPServerListError` on every discovery.
- **review-notify** — intermittent: blocked on the ticks where the email adapter's IMAP fetch has timed
  out (§9). Root cause is the adapter's IMAP reliability, not missing SMTP creds.
- **Higgsfield Infisical grant** — `fleet-hermes` needs `secrets:edit` on `/shared` so token rotation
  persists; until then the container volume `gtjymgp2unprqsjd82i0o328_higgsfield-config` and the
  10-minute host backup `~/higgsfield-cred/` (cron on prod-2) are the only live copies (`seed_drift`
  + `durable_recoverable=false` firing). Also make the 0.3.0 code a real deployed image (PR #16).
- **Firecrawl API down** — `redis` alias collision on the `coolify` network (§7); fix the stack's
  `REDIS_URL` host (use the Coolify-suffixed container name) or detach it from the shared network.
- **Sandbox image rebuild** — bake fonts + preflight `render-card` (PR #15); R2 template swap to the
  self-contained font bundle still pending (R2 `fb.html` still pulls `fonts.googleapis.com`).
- **Webster outbound-email SOUL patch** — not applied; its md5 guard (`a5c7d3bb…`) no longer matches the
  live SOUL (`8e0a5f30…` after the 2026-09-08 social-card edit). Re-base before applying.
- **Board hygiene** — three abandoned Health Clinics chains (original, `v2`, `[clean]`: 10 cards in
  `triage`/`todo` since 2026-09-07) sit beside the finished `[rebuild]` chain; two 2026-08-26 cards
  carry the force-complete signature (`done` with `session_id` and `result` null).
- **Per-client WordPress** — client keys can't publish to their own sites yet; client WP publishing is
  held until wired.

---

*Keep this current: when you change how the system works, update the matching section — this is the
map a future session starts from.*
