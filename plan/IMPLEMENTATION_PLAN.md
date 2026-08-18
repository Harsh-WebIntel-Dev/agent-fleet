# OpenClaw + Hermes — Multi-Agent Platform Implementation Plan

> Hand this to Claude Code as the build spec. Companion files: `fleet-network-architecture.svg`, `model-path-architecture.svg`, `agent-model-table.csv`.

---

## 1. What we're building

A shared multi-agent fleet (**OpenClaw**) that serves multiple agency clients, fronted by **per-client Hermes** conversational agents. One shared fleet does the work; each client gets an isolated front-door. Runs self-hosted on a **Contabo VDS** managed by **Coolify**.

The tenant boundary is **not** the deployment — the fleet is shared. Isolation is enforced by **scoped per-client credentials passed on every call** (object-storage token, secrets, LiteLLM virtual key, KB partition). Every LLM and tool call carries a `client_id`; nothing runs unscoped.

## 2. Locked architectural decisions

- **Host:** Contabo VDS. **Control plane:** Coolify.
- **Projects:** one shared Coolify project `fleet-core` on an internal-only Docker network `fleet-net`; one project per client `hermes-<client>` (public via Traefik). Only Hermes gets public routes — `fleet-core` services are never exposed.
- **Model layer:** self-hosted **LiteLLM proxy** → **DigitalOcean serverless inference** (`https://inference.do-ai.run/v1`, OpenAI-compatible) as the single upstream. **No Cloudflare AI Gateway.** Provider-direct exception for models DO lacks (e.g. Gemini → Google direct, behind the same LiteLLM).
- **Budgets:** LiteLLM per-client **virtual keys** with hard dollar caps + spend attribution. DO's prepaid balance is the master tap; LiteLLM allocates per client. Enforcement is deterministic (LiteLLM), not an LLM agent.
- **Data:** one **Postgres + pgvector** (schemas: `app`, `memory`, `skill_kb`, `client_kb`) + **Langfuse** (observability, one project per client).
- **Storage:** object storage, **bucket per client**, scoped credential per client = the tenant boundary. (Cloudflare R2 for zero-egress, or Contabo Object Storage for co-location — pick at Phase 0.) Small local scratch volume for in-flight work.
- **Secrets:** **Infisical**, keyed per client, injected into the shared specialists at call-time. Never in code or repo.
- **Knowledge base:** `skill_kb` (shared, client-agnostic technique) vs `client_kb` (per-client, siloed). Default writes go to `client_kb`; promotion into `skill_kb` requires a **sanitization/curation gate** (strip client identifiers first). Reads are partitioned by Postgres row-level security keyed to `client_id`.

## 3. The model-selection rule (do not violate)

DO documents tool/function calling for **Claude, GPT, GLM 5.1/5.2, Qwen3.8-max, and MiMo V2.5 Pro** — but **not** for DeepSeek V4 or Kimi. Every tool-facing agent must run on a tool-capable model. DeepSeek is used **only** for pure text generation (Writer's bulk-draft tier), never as a tool-calling agent. See `agent-model-table.csv` for the full per-agent mapping (default + fallback).

## 4. Repo structure to scaffold

```
openclaw-platform/
├── README.md
├── infra/
│   ├── coolify/                # compose / service definitions per project
│   │   ├── fleet-core/
│   │   └── hermes-template/    # parameterized per client
│   └── network.md              # fleet-net topology + who-may-reach-whom
├── litellm/
│   └── config.yaml             # model map + provider routing (see §6.1)
├── db/
│   └── migrations/             # pgvector schemas, RLS policies (see §6.2)
├── fleet/
│   ├── orchestrator/           # OpenClaw: PM entry, delegation graph
│   └── agents/
│       ├── pm/{agent.yaml, system_prompt.md}
│       ├── accounts_manager/…
│       ├── research/…
│       ├── writer/…
│       ├── seo/…
│       ├── social/…
│       ├── newsletter/…
│       ├── publisher/…
│       ├── image/…
│       ├── video/…
│       ├── dev/…
│       ├── qa/…
│       └── google_ads/…
├── hermes/
│   └── template/               # per-client front-door service
├── kb/
│   └── promotion_gate/         # skill_kb sanitization + promotion job
└── clients/
    └── <client>/{policy.md, brand.md}   # or mirrored in object storage
```

## 5. Phased build

Each phase lists tasks and an **acceptance check**. Do phases in order; later phases assume earlier ones pass.

### Phase 0 — Prerequisites (HUMAN, not Claude Code)
See §8. Claude Code should **stop and confirm** these exist before Phase 1 (DO key, Coolify up, storage buckets, Infisical reachable).

### Phase 1 — Core infrastructure (`fleet-core`)
- Create Coolify project `fleet-core` and internal network `fleet-net`.
- Deploy **Postgres + pgvector**; run `db/migrations` (schemas + RLS from §6.2).
- Deploy **Redis** (orchestration queue + LiteLLM cache).
- Deploy **Langfuse** (self-hosted).
- Deploy **Infisical** (or connect existing).
- Deploy **LiteLLM proxy** with `litellm/config.yaml`.
- **Acceptance:** every `fleet-core` service resolves on `fleet-net` and has **no public route**; `curl` a chat completion through LiteLLM and get a response from a DO model; Langfuse receives the trace.

### Phase 2 — LiteLLM model map + per-client budgets
- Define model aliases → DO model IDs (§6.1).
- Wire the **provider-direct exception** (Gemini → `gemini/…`) behind the same proxy.
- Generate one **virtual key per client** with `max_budget` and `metadata.client_id`; enable the Langfuse callback.
- **Acceptance:** each alias resolves to the right DO model; a client key over budget returns HTTP 429; spend is attributed per `client_id` in LiteLLM + Langfuse.

### Phase 3 — Fleet + agents
- Deploy the **OpenClaw orchestrator** in `fleet-core`; **PM is the single entry point**; **Accounts Manager gates PM** (budget check before delegation).
- Scaffold all 14 agents from `agent-model-table.csv`. Each `agent.yaml`: default + fallback model **alias** (via LiteLLM), tool list, and delegation targets (the "Talks to" column). Social and Newsletter are **coordinators** — they brief Writer/Image/Video and assemble; they generate nothing themselves.
- **Acceptance:** PM decomposes and delegates a test task end-to-end; a specialist completes one tool call; a forced model failure falls back to the alias's fallback.

### Phase 4 — Tool / MCP integrations
- Wire per-agent tools: SEMrush (SEO), Mailchimp (Newsletter), WordPress (Publisher), Postiz (Social), Higgsfield (Image/Video), Google Ads API (Google Ads), ClickUp (Hermes/PM), web search (Research).
- Every tool call pulls the **acting client's** credentials from Infisical, scoped by `client_id`.
- **Acceptance:** each tool-using agent completes a real call in a sandbox account using the correct client's creds; a cross-client cred request is refused.

### Phase 5 — Client folders, storage, knowledge base
- Object storage: create/verify **bucket per client** + scoped credential; agents can read a client's `policy.md` + `brand.md` from **that bucket only**.
- Implement `kb/promotion_gate`: agents write learnings to `client_kb` by default; promotion to `skill_kb` runs the sanitization step (strip client identifiers, confirm generic) before writing.
- Embeddings: use DO embeddings (e.g. `bge-m3`) or a local model; store vectors in pgvector.
- **Acceptance:** an agent operating for client A cannot read client B's bucket or `client_kb` rows (RLS); a `skill_kb` write is rejected unless it passed the gate.

### Phase 6 — Per-client Hermes front-doors
- For each client: Coolify project `hermes-<client>` from `hermes/template`; deploy the Hermes service; attach to `fleet-net` **to reach PM only**; expose a public route via Traefik.
- Hermes holds that client's conversation state + auth.
- **Acceptance:** a client message → Hermes → PM → fleet → response; two Hermes instances share no network and cannot see each other.

### Phase 7 — Budgets, observability, guardrails live
- Wire Accounts Manager: reads LiteLLM spend + Langfuse; tops up the DO prepaid master; allocates/adjusts per-client sub-budgets; alerts on overrun.
- Enforce the **QA gate before Publisher**; content with environmental/legal claims (e.g. Biogone) escalates QA to Opus.
- **Acceptance:** per-client dashboards show spend + traces; QA blocks a deliberately non-compliant (greenwashing) test asset before it reaches Publisher.

## 6. Config skeletons

### 6.1 `litellm/config.yaml`
```yaml
model_list:
  # --- Claude tiers via DO (OpenAI-compatible) ---
  - model_name: haiku
    litellm_params: {model: openai/anthropic-claude-haiku-4.5, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  - model_name: sonnet
    litellm_params: {model: openai/anthropic-claude-4.6-sonnet, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  - model_name: opus
    litellm_params: {model: openai/anthropic-claude-opus-4.8, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  # --- cheap tool-capable open models ---
  - model_name: glm
    litellm_params: {model: openai/glm-5.2, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  - model_name: qwen-max
    litellm_params: {model: openai/qwen3.8-max, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  - model_name: qwen-coder
    litellm_params: {model: openai/qwen3-coder-flash, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  - model_name: gpt-codex
    litellm_params: {model: openai/openai-gpt-5.3-codex, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  # --- generation-only (NO tools): Writer bulk drafts ---
  - model_name: deepseek-writer
    litellm_params: {model: openai/deepseek-v4-pro, api_base: https://inference.do-ai.run/v1, api_key: os.environ/DO_INFERENCE_KEY}
  # --- provider-direct exception (not on DO) ---
  - model_name: gemini
    litellm_params: {model: gemini/gemini-2.5-pro, api_key: os.environ/GOOGLE_API_KEY}

litellm_settings:
  success_callback: ["langfuse"]
  drop_params: true

# Per-client virtual keys are created via LiteLLM's /key/generate (or DB) with:
#   max_budget: <client dollar cap>, metadata: {client_id: <id>}, models: [<allowed aliases>]
# Agents authenticate to LiteLLM with the acting client's virtual key so spend + limits attribute correctly.
```
> Verify exact DO model ID strings against `GET https://inference.do-ai.run/v1/models` at build time — they change. Image/video (Higgsfield) and Wan2.2/Flux fallbacks are called via their own tools, not through this text-model map.

### 6.2 `db/migrations` (sketch)
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA app;        -- tasks, orchestration state
CREATE SCHEMA memory;     -- per-client working/conversation memory (vectors)
CREATE SCHEMA skill_kb;   -- shared, client-agnostic technique (promote-gated)
CREATE SCHEMA client_kb;  -- per-client siloed knowledge (vectors)

CREATE TABLE client_kb.entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid NOT NULL,
  embedding vector(1024),
  content text,
  created_at timestamptz DEFAULT now()
);
ALTER TABLE client_kb.entries ENABLE ROW LEVEL SECURITY;
CREATE POLICY client_isolation ON client_kb.entries
  USING (client_id = current_setting('app.client_id')::uuid);
-- Each request sets: SET app.client_id = '<acting client>'; before touching client_kb / memory.
```

### 6.3 `agent.yaml` (example — Publisher)
```yaml
name: publisher
model: glm            # default alias (GLM-5.2 via LiteLLM) — tool-capable
fallback: qwen-max    # Qwen3.8-max — also tool-capable
tools: [wordpress, scheduler]
receives_from: [qa]   # never publishes anything that hasn't passed QA
talks_to: []
notes: "Deterministic publishing. NEVER route DeepSeek here (no documented tool calling on DO)."
```

## 7. Hard constraints Claude Code must respect

1. **Tool-capable models on tool-facing agents only.** Do not substitute DeepSeek V4 or Qwen3 Coder Flash into a tool-calling role without first confirming tool-calling works; the safe defaults are in `agent-model-table.csv`.
2. **Tenant isolation is per-call.** Every LLM/tool/storage call carries `client_id`; creds come from Infisical scoped to that client; `SET app.client_id` before any `client_kb`/`memory` access. Never let one client's context, creds, or KB rows reach another.
3. **Network exposure.** Only `hermes-<client>` projects get public Traefik routes. `fleet-core` (Postgres, Langfuse, LiteLLM, Infisical, orchestrator) is internal-only. Never expose the Coolify dashboard to clients.
4. **Secrets.** Infisical only. Never commit keys; never hard-code provider or client credentials.
5. **KB writes.** Default to `client_kb`. `skill_kb` writes must pass the promotion/sanitization gate.
6. **Publishing gate.** QA must pass before Publisher runs. Environmental/legal claims (Biogone and similar) escalate QA to Opus.
7. **Budgets.** LiteLLM enforces per-client dollar caps; do not build budget logic into the request path of an LLM agent.

## 8. Human-only steps (do before / alongside Claude Code)

- Provision the Contabo VDS; install Coolify; confirm Docker + `fleet-net` creatable.
- Create a DigitalOcean account, enable Serverless Inference, generate a **Model Access Key**, and **fund the prepaid balance** (with auto-reload).
- (Optional) Anthropic and Google API keys for provider-direct fallback.
- Choose object storage (R2 or Contabo Object Storage); create a **bucket + scoped key per client**.
- Stand up Infisical (or provide access) and load all creds, keyed per client: WordPress, Meta/Instagram, YouTube, X, Google Business Profile, Google Ads, Mailchimp, Postiz, SEMrush, ClickUp, Higgsfield.
- DNS records for each client-facing Hermes endpoint.

## 9. Agent → model reference

Authoritative mapping (default + fallback, coordination) is in **`agent-model-table.csv`**. Summary of the non-obvious calls:
- **PM / QA / Google Ads** → Claude Sonnet 4.6 (Opus escalation) — reliability and judgment matter most here.
- **Writer** → Opus for hero/brand; DeepSeek V4 Pro only for bulk drafts (generation, no tools).
- **SEO / Social / Newsletter / Publisher / Accounts Manager** → GLM-5.2 (Qwen3.8-max fallback) — cheap and tool-capable.
- **Research** → Qwen3.8-max + web search.
- **Dev** → Qwen3 Coder Flash for routine builds; **if it runs agentically with file/deploy tools, DO doesn't document tool calling for it — lead with GPT-5.3-Codex or Sonnet instead.**
- **Image / Video** → cheap vision/text "brain" (Haiku / Sonnet) to prompt + evaluate; **Higgsfield** renders (its own integration, not on DO); Flux Schnell / Wan2.2 on DO as cheap fallbacks.
