# Architecture review: scaling the Hermes marketing fleet to all clients

**Review date:** 2026-09-04

**Scope:** Read-only architecture review of the current Hermes-native fleet. No production state was inspected or changed.

**Authority:** `CLAUDE.md` is treated as the live-system map. `hermes/`, `litellm-wrapper/`, current `mcp-*` sidecars, `firecrawl/`, and `searxng/` were checked against it. `openclaw/`, `nemoclaw/`, and `plan/` were used only as superseded history, not as descriptions of the target system.

## Executive assessment

The fleet is a capable **shared agency runtime with some tenant-aware controls**, not yet a multi-tenant platform. Memory has the strongest isolation: a pinned client header where configured, application checks, and forced PostgreSQL row-level security (RLS). Model spend can be assigned to per-client LiteLLM keys and budgets. In contrast, compute, the PM, the kanban database and dispatcher, ClickUp intake, most tool credentials, publishing integrations, and the host are shared. Several boundaries depend on Webster choosing the correct slug/provider in a prompt rather than on an authenticated request context.

The immediate scaling constraint is not model capacity. It is the combination of:

1. **unsafe/incomplete tenant routing for side-effecting tools**, especially WordPress and shared social credentials;
2. **no transactional onboarding control plane**, leaving every client dependent on coordinated manual edits across Infisical, LiteLLM, Hermes, object storage, memory, ClickUp and publishing systems;
3. **single-instance orchestration and host failure domains**, including one Webster, one gateway/scheduler and one machine; and
4. **a fleet-wide work cap of four cards and one card per specialist**, which creates head-of-line blocking as client count grows.

Do not onboard arbitrary clients by adding more slugs to the current shared prompts. First establish a server-enforced tenant identity, a repeatable onboarding workflow, per-client publication routing, and measurable queues/budgets. Then split orchestration into cells so failure and noisy-neighbour effects remain bounded.

## Severity model

| Severity | Meaning |
|---|---|
| **Critical** | Credible cross-client action/data exposure, uncontrolled production action, or a failure that prevents safe onboarding. Fix before broad rollout. |
| **High** | Material availability, capacity, security, or operational risk likely to surface with more clients. |
| **Medium** | Important control or efficiency gap that increases toil and recovery time but does not alone block a small pilot. |
| **Low** | Maintainability or future-scale concern; schedule after core isolation and reliability work. |

## Current isolation posture

| Plane | Current evidence | Assessment for all-client scale |
|---|---|---|
| **Compute** | One shared Hermes gateway dispatches all profiles; a shared sandbox has one CPU and 2 GiB limits; Firecrawl is capped at two jobs, three crawl requests and one worker per queue (`hermes/setup-sandbox.sh`, `firecrawl/compose.yml`). | **Not isolated.** One client can consume specialist slots, crawler capacity, PM attention and sandbox resources. |
| **Task/orchestration data** | ClickUp is the shared source of truth; kanban state is a local SQLite database in the shared Hermes volume, and review detection scans card bodies for task IDs (`hermes/scripts/monitor_tasks.py`). | **Not a hard tenant boundary.** Tenant identity is embedded in task/card content and selected by the model. |
| **Memory** | `mcp-memory/server.py` resolves a pinned `x-client-slug` when present, rejects conflicts, sets `app.client_id` for every transaction, and documents a restricted role plus forced RLS. Agent identity further narrows recall. | **Best existing boundary**, but the repo does not contain schema/migration/policy tests, so the deployed RLS contract is not reproducible or continuously verified. Shared-agency calls may still choose a client argument. |
| **Assets** | `mcp-spaces` confines paths under `clients/<slug>/`, optionally allowlists slugs, and can honour a pinned header. The current shared agency path accepts a model-supplied client. The sandbox credential covers the shared client bucket. | **Logical partition, not credential isolation.** A compromised shared agent or bucket credential can affect multiple clients. |
| **Secrets** | Services pull `/shared` and selected `/clients/<slug>` paths at boot, then fall back to Coolify environment values. The fleet identity is Viewer/read-only. Several integrations use shared credentials (`litellm-wrapper/`, sidecar entrypoints). | **Partially separated, operationally manual.** Read-only runtime access is correct, but there is no scoped provisioning identity/workflow and fallback copies increase secret locations. |
| **Budgets/models** | Webster must set `provider="litellm-<slug>"` and `tenant="<slug>"`; per-client providers reference per-client key environment variables and LiteLLM holds key/budget state. Existing client keys grant tools by explicit server IDs (`CLAUDE.md`, Webster SOUL). | **Accounting exists but routing is prompt-enforced.** A wrong or omitted provider can silently charge the agency or another client. Tool entitlement changes require per-key edits. |
| **Publishing** | WordPress is a single process whose site and Basic credentials are fixed at startup to Web Intelligenz. Postiz and its extras use a shared agency token. Mailchimp and Lnk.Bio are WI-only by access-group/SOUL policy (`mcp-wordpress`, `mcp-postiz-extras`, `mcp-mailchimp`, `mcp-lnkbio`). | **Not ready.** Client WordPress is explicitly unwired; social/account selection is not demonstrated as a server-enforced tenant boundary; some WI-only restrictions are policy rather than least-privilege credentials. |
| **Conversation/UI** | One internet-facing web UI is protected only by a password. Agent and web UI share the Hermes volume; the gateway is internal (`hermes/compose.yml`, `CLAUDE.md`). | **High blast radius.** Compromise exposes conversations and an agent able to invoke broadly shared tools. Budget caps limit model spend, not data access or publishing actions. |
| **Availability** | One Webster, one Hermes agent with in-process cron, one web UI, one host, local kanban SQLite, and several colocated stateful stacks. Sandbox connectivity is restored by a manual script after redeploy. | **No HA and weak recovery guarantees.** Host/gateway loss stops intake, scheduling, dispatch and review. |

## Ranked findings and concrete improvements

### 1. Critical — tenant identity is not end-to-end and is sometimes model-chosen

**Evidence.** Webster's SOUL instructs the LLM to select both `provider` and `tenant`; a wrong slug is documented to silently mis-bill. Shared-agency calls to memory and Spaces fall back to a `client` tool argument. The local kanban record is associated with ClickUp mainly through task IDs embedded in card bodies. Postiz is explicitly shared across fleet clients. LiteLLM keys and access groups control which server can be reached, but reaching a shared server does not necessarily constrain which downstream client/account it acts on.

**Risk.** Prompt injection, an ambiguous ClickUp task, stale memory, or simple model error can read/write the wrong asset prefix, charge the wrong budget, or act on the wrong publication account. Soft conventions are not a tenant boundary.

**Improve.** Introduce an immutable **work envelope** created by deterministic intake, not the LLM: `work_id`, canonical `client_id`, `clickup_workspace_id`, `task_id`, `budget_key_id`, allowed capability set, and publication-target IDs. Sign or persist it server-side and pass only its opaque ID to cards. The dispatcher resolves the envelope and injects tenant/provider headers; workers cannot override them. Every tenant-aware MCP must reject missing or conflicting identity. Remove model-selectable client arguments from side-effecting production paths. Add negative integration tests proving client A cannot address client B for memory, assets, ClickUp, Postiz, WordPress and Mailchimp.

### 2. Critical — publishing is not safely multi-client; WordPress reaches only the agency site

**Evidence.** `mcp-wordpress/server.py` loads one `WP_SITE_URL`, username and application password at process start. Its entrypoint hard-codes the Web Intelligenz Infisical path. The source itself labels multi-client routing as future work. `CLAUDE.md` says client WordPress publishing is held. Postiz uses one shared token, while account/integration choice is performed within that shared account. Mailchimp and Lnk.Bio are WI-only, with some scoping relying on access groups and SOUL policy.

**Risk.** Broad rollout either leaves a core service unusable for clients or encourages unsafe workarounds. A shared publisher credential also creates a large cross-client and irreversible-action blast radius.

**Improve.** Prefer a **per-client publishing connector instance or per-client credential broker lease**, each bound to one canonical client and an allowlist of site/account IDs. Register a distinct LiteLLM MCP server identity per connector initially, but expose it to workers through a stable capability resolver so keys need not know raw server IDs. Separate `stage` and `publish` capabilities. Publishing credentials should permit only the required content type/site, and live actions must require a short-lived approval token tied to `work_id`, artefact hash, target and expiry. Keep default-deny for clients without a verified connector.

### 3. Critical — onboarding is a manual, non-transactional sequence with no readiness authority

**Evidence.** The brand-kit skill gates creative work and memory can register a client, but its docstring explicitly says LiteLLM setup is separate. New providers/keys are enumerated in configuration, Spaces has a manually maintained slug allowlist, WordPress is hard-coded, client keys use explicit MCP server lists, and secrets require an Infisical admin because the runtime identity is Viewer. No current component owns the complete lifecycle or rollback.

**Risk.** Partial onboarding produces dangerous states: a slug exists without a budget, a key exists without required tools, work starts without a complete brand kit, or publication targets/credentials point elsewhere. The first task becomes the integration test.

**Improve.** Build an idempotent onboarding controller (CLI or service) driven by a reviewed, non-secret client manifest. It should implement a state machine and resumable steps:

1. reserve and validate a canonical slug/client UUID; reject aliases and collisions;
2. map approved ClickUp workspace/list/channel IDs and service account membership;
3. create the R2 prefix and least-privilege client asset credential/policy;
4. register memory tenant and run RLS cross-tenant tests;
5. create a LiteLLM team/key with monthly/hard budgets, rate limits, tags and capability bundle;
6. create client-scoped secret paths through a separate narrowly scoped **provisioner identity** (never grant write access to the runtime identity);
7. configure and health-check client publishing connectors in draft-only mode;
8. ingest, validate and human-approve `brand-kit.md` plus required assets;
9. run synthetic model, memory, asset, ClickUp and draft-publication tests;
10. atomically mark the client `READY`, then allow intake/dispatch.

Failures must leave the client `PENDING` or `BLOCKED`, never partially active. Offboarding must revoke keys/credentials, stop intake, preserve/export records according to retention policy, and disable rather than silently delete memory.

### 4. High — client LiteLLM tool grants create per-server, per-client toil

**Evidence.** The agency key grants access groups and inherits new servers, while the existing client keys contain explicit server-ID lists. A new server must be allowed by the team before each key can use it. LiteLLM MCP record updates are destructive replacements, increasing change risk.

**Risk.** Entitlement drift grows roughly with clients × servers. Clients silently miss tools, operators make inconsistent grants, and repetitive production edits raise outage likelihood.

**Improve.** Define versioned **capability bundles** such as `research-v1`, `content-draft-v1`, `social-draft-v1`, and `publish-approved-v1`. Generate LiteLLM teams/keys and server grants from a declarative manifest, diff before apply, and reconcile continuously. If LiteLLM cannot assign server access transitively by group with the needed isolation, put a tenant-aware policy proxy/capability resolver before `/mcp/`; never compensate with broader shared access. Canary and rollback configuration changes, and back up the full server record before any destructive update.

### 5. High — the read-only Infisical runtime identity creates a manual admin step per client

**Evidence.** `CLAUDE.md` records the `fleet-hermes` machine identity as Viewer and says it cannot stage new secrets; this already blocks the Mailchimp integration. Entrypoints only pull secrets and fall back to Coolify environment values.

**Risk.** Onboarding cadence is gated by an administrator, secret placement becomes inconsistent, and duplicated fallback values complicate rotation/revocation. Granting write privileges to the runtime identity would solve toil by creating a more serious compromise path.

**Improve.** Preserve runtime read-only access. Add a separate, audited onboarding provisioner with permission only to create/update an approved key schema under `/clients/<slug>`, invoked by a human-approved workflow. Validate secret presence and connector authentication without printing values. Track owner, rotation due date and last successful load. After migration, minimise Coolify fallbacks; if break-glass fallback remains, monitor its use and keep the same scope/rotation guarantees.

### 6. High — fleet throughput is capped at four concurrent cards and one per specialist

**Evidence.** The live configuration documented in `CLAUDE.md` caps the dispatcher at four cards fleet-wide and one per specialist. Dependency chains serialize many jobs. Firecrawl independently has small global concurrency caps, and the sandbox is limited to one CPU. One publisher or writer card can block every client's demand for that specialty.

**Risk.** Queueing delay grows non-linearly with clients; urgent work has no guaranteed capacity; a long or stuck card creates cross-client head-of-line blocking. Raising one global number on a CPU-steal host will reduce reliability rather than solve capacity.

**Improve.** Instrument arrival rate, service time and queue age by stage/client before changing limits. Add fair scheduling with per-client quotas, weighted priority, maximum runtime/lease renewal, idempotent retry and a dead-letter state. Scale worker replicas independently by specialist and external-provider limits. Reserve capacity for approvals/fixes and urgent work. Move crawling/rendering to separate worker pools. Set per-client concurrency and rate limits so one tenant cannot occupy the fleet.

### 7. High — Webster and the Hermes gateway/cron scheduler are single points of failure

**Evidence.** One Webster owns intake, decomposition, review and approval relay. The only gateway contains the in-process scheduler and dispatcher. Kanban and cron state reside in its shared volume/local files. A separate web UI process does not provide scheduler redundancy. `review-notify` is already recorded as failing.

**Risk.** Process/redeploy failure stops all client intake and work progression. Running two gateways naïvely risks duplicate crons, duplicate cards and duplicate publication because leader election/idempotency are absent. Webster's review workload also becomes a human-like serial bottleneck.

**Improve.** Extract durable intake events and work state to a transactional database/queue. Give every event and stage an idempotency key. Run schedulers under leader election, and workers under leases with heartbeat/reclaim semantics. Split Webster into horizontally scalable PM sessions partitioned by client while keeping one deterministic review state machine. Maintain an approval audit log independent of chat. Test crash/restart at every transition, especially draft creation and publish.

### 8. High — one Contabo host is the fleet-wide failure and performance domain

**Evidence.** The agent, LiteLLM and its database, Infisical and its stores, Firecrawl/SearXNG, Postiz/Temporal and other workloads are colocated. `CLAUDE.md` notes significant hypervisor CPU steal and no HA. The sandbox link and host-key repair require a manual post-redeploy script.

**Risk.** Host or provider failure takes out intake, orchestration, model routing, secrets, tools and publication together. CPU steal makes latency unpredictable and undermines concurrency tuning. Colocation also expands lateral blast radius.

**Improve.** First make state recoverable: encrypted off-host backups with restore drills for LiteLLM, memory, Infisical, Postiz/Temporal and orchestration state; export config to declarative source; define RPO/RTO. Then separate control plane, stateful data and elastic workers across failure domains. Run at least two stateless gateways behind internal routing once leader election exists. Place databases on replicated/managed storage where practical. Treat the sandbox as disposable infrastructure with network policy expressed in deployment code, not a manual network-connect step.

### 9. High — ClickUp intake and cron scheduling will contend across clients/workspaces

**Evidence.** Task monitoring uses one hard-coded workspace and bot identity, team-wide queries, fixed excluded-list IDs, bounded pagination, and a local scan of kanban card bodies. Chat intake expects exactly one workspace and polls all DM/group-DM channels. Both share the same gateway scheduler with global feeds and notification jobs. ClickUp's hosted MCP has a small daily allowance, while REST polling uses a personal token.

**Risk.** This design cannot discover multiple ClickUp workspaces without code/config duplication. Poll cost, pagination, API limits and cron run duration grow with channels/tasks. A slow sweep delays other jobs; hard-coded exclusions can admit unrelated work or omit a client. One token/account becomes a shared availability and security dependency.

**Improve.** Create a registry of client-to-workspace/list/channel mappings and credentials. Prefer ClickUp webhooks into a durable, authenticated event queue; retain reconciliation sweeps per workspace as a backstop with cursors, jitter and independent rate budgets. Partition consumers by workspace/client and deduplicate on ClickUp event/task/message IDs. Schedule each job independently rather than serialising all work in the agent process. Alert on webhook lag, sweep age, pagination exhaustion, 429s and credential failure.

### 10. High — observability does not cover the failure modes that determine client service

**Evidence.** Components mainly log locally with rotation. The monitors fail loudly, but no central alert path is defined. Known failures include stuck monitor baselines, stranded cards, false cron ledger failures, a failing review notification job and provider budget limits. There is no documented per-client SLO/dashboard or automated stuck-work detector.

**Risk.** The first signal of failure is a client asking where the work is. Quiet failure is especially dangerous for cron intake and dependency chains.

**Improve.** Emit structured events with `client_id`, `work_id`, `task_id`, stage, attempt and trace ID, excluding content/secrets. Centralise metrics/logs/traces and add alerts for:

- actionable ClickUp item not acknowledged within the intake SLO;
- card queued/running/blocked beyond stage thresholds or lease heartbeat absent;
- dependency chain with no progress, orphan card, or task/card state mismatch;
- cron missed, failed repeatedly, overlapping, or last-success too old;
- LiteLLM budget at 70/85/100%, rejected spend, anomalous burn rate, or missing tenant tag;
- connector auth/health failure, draft/publish mismatch, and approval-token rejection;
- host CPU steal, saturation, disk pressure, database health and backup age.

Budget exhaustion should pause only that client, create an operator/client-visible event, and never fall back to the agency key.

### 11. High — the public password-only web UI has excessive blast radius

**Evidence.** The public web UI has no SSO, MFA, IP policy or WAF in the documented deployment. It shares conversation state with the agent. DNS points directly to the host. The agent can reach shared ClickUp and MCP capabilities; budget caps constrain inference spend only.

**Risk.** Password compromise can expose client conversations, induce model/tool actions, consume budgets and potentially reach multiple clients through shared agency credentials. A single shared login has weak attribution and revocation.

**Improve.** Remove direct public exposure until it sits behind an identity-aware proxy with per-user accounts, MFA, session expiry, rate limiting and audit logs. Prefer per-client portals/sessions mapped server-side to one tenant. Put the edge behind a managed proxy/WAF, restrict administrative paths to the private network, rotate the existing credential, and test that UI identity cannot select another client. Apply CSRF/session hardening and alert on login anomalies.

### 12. Medium — shared tool accounts and broad tool surfaces weaken least privilege

**Evidence.** ClickUp has an unrestricted hosted MCP toolset. Postiz and Higgsfield use agency-wide accounts/workspaces. The WI key auto-inherits servers by group, and `pm_comms` is available across profiles; WI-only separation partly depends on SOUL instructions. The shared R2 sandbox token covers the fleet bucket.

**Risk.** Compromise or tool misuse reaches more clients and actions than required. Prompt policy cannot reliably enforce account boundaries.

**Improve.** Separate read/research, draft, approval and live-publish capabilities. Use server `allowed_tools`, client-scoped downstream tokens, and worker-role policies. Remove publish/send/delete from normal worker credentials; mint just-in-time capability tokens after approval. Narrow ClickUp to an internal facade exposing only required operations and enforcing registered workspace/list IDs. Prefer per-client storage credentials or a broker that signs object operations only within the envelope prefix.

### 13. Medium — fail-safe secret fallback favours uptime over revocation consistency

**Evidence.** LiteLLM and multiple sidecars continue with Coolify environment values when Infisical is unavailable. Higgsfield also persists refreshed credentials on a volume.

**Risk.** A revoked/rotated secret may remain usable from a stale fallback; operators cannot easily tell which source is active. Compromise response and audit become uncertain.

**Improve.** Classify secrets by availability requirement. Fail closed for tenant publishing and privileged actions if current credentials cannot be fetched/validated; allow time-bounded cached credentials only for low-risk reads. Emit the source and version (never value), age and rotation status. Encrypt persistent credential caches and establish a forced-reload/revocation procedure.

### 14. Medium — repository templates drift from the deployed source of truth

**Evidence.** `CLAUDE.md` explicitly says deployed SOULs are authoritative and repository SOULs drift. `hermes/README.md` still describes the superseded OpenClaw execution model. The checked-in `config.yaml.template` reflects an earlier one-client shape and does not contain the live multi-provider/kanban settings described in `CLAUDE.md`. Live LiteLLM configuration is a host bind mount outside the repository.

**Risk.** Recovery and scale automation cannot reproduce production. Reviewers may validate obsolete configuration; manual changes accumulate without tests or rollback.

**Improve.** Make sanitised declarative configuration the source of truth in version control, with secrets referenced by path. Add schema validation, rendered-config tests, drift detection and promotion through staging/canary/production. Archive or clearly banner superseded docs. Export live SOUL/config changes back through review rather than editing only the volume.

### 15. Medium — local SQLite and text matching are fragile orchestration primitives

**Evidence.** Review readiness reads `kanban.db` locally and finds task IDs by substring in card bodies. A task is ready when all matching non-archived cards are done. The system has already experienced phantom/stranded-card and stale-comment failures, motivating mechanical checks in `WORKER_PROTOCOL.md`.

**Risk.** ID collisions, malformed bodies, partial writes, host loss and concurrent schedulers can produce incorrect readiness or stalled work. The database is not a multi-node coordination store.

**Improve.** Store typed `client_id`, `task_id`, `stage`, dependency and artefact records in a transactional orchestration database with uniqueness/foreign-key constraints. Use an outbox for ClickUp updates and immutable transition history. Validate completion evidence schemas per stage and make publication operations idempotent.

### 16. Medium — recovery, retention and client lifecycle controls are undocumented

**Evidence.** The reviewed current materials describe operation and several restart traps, but not tested restore procedures, client retention/deletion, legal holds, key rotation cadence, data export or disaster exercises.

**Risk.** Multi-client operation will eventually face offboarding, access requests, accidental deletion or a regional outage without a proven response.

**Improve.** Define data ownership and retention per store, offboarding and export procedures, backup encryption/immutability, quarterly restore tests, and incident runbooks. Record RPO/RTO by service and prove them before claiming HA.

### 17. Low — unpinned `latest` images and heterogeneous sidecars increase change risk

**Evidence.** Firecrawl, its Playwright service and NUQ Postgres use `latest`; LiteLLM uses a moving stable tag, while Hermes/web UI pinning has already required careful compatibility work. Sidecars duplicate secret-fetch implementations.

**Risk.** A recreate can introduce an unreviewed change; duplicated bootstrap logic drifts.

**Improve.** Pin production images by tested version/digest, automate vulnerability scans and staged upgrades, and standardise secret/bootstrap/health behaviour in a small maintained base or library without coupling business logic.

## Target architecture

Adopt a **cell-based multi-tenant architecture**, rather than either one global singleton or one full stack per client.

- **Global control plane:** client registry and lifecycle state; declarative capability catalogue; onboarding reconciler; audit log; identity integration; metrics routing. It stores metadata, not client content or reusable publishing credentials.
- **Tenant-aware intake plane:** ClickUp webhook endpoints and reconciliation pollers authenticate a workspace, resolve it to one client registry record, and emit durable, deduplicated work events.
- **Orchestration cells:** each cell serves a bounded number of clients with redundant gateways/schedulers, a durable queue/database, and independently scalable specialist pools. Consistent hashing or registry assignment gives one active cell per client; evacuation supports maintenance.
- **Immutable work context:** all jobs carry a server-created envelope. The model can propose work but cannot choose tenant, budget key, ClickUp workspace, asset prefix or publishing account.
- **Tool policy gateway:** resolves envelope + role + capability to an allowed connector. Client keys receive capability bundles rather than raw server inventories. Every side-effect is authorised and audited at this layer.
- **Client-scoped data and credentials:** RLS-backed shared databases where mature and tested; per-client object prefixes with scoped credentials; isolated publishing connector credentials; explicit retention and export controls.
- **Approval service:** records reviewer identity, artefact hash, destination and expiry, and issues a one-use token for the exact live action. Chat/task status alone is not authority to publish.
- **Elastic worker pools:** stage-specific replicas with fair scheduling, per-client quotas and provider-aware rate limits. Crawling and rendering run outside the control-plane host.
- **Reliability foundation:** multi-zone or at least multi-host stateless services, replicated/managed databases, off-host backups, leader election and tested failover.

For very high-risk or regulated clients, assign a dedicated cell and dedicated downstream accounts. For ordinary clients, logical isolation within a cell is acceptable only after automated cross-tenant tests and scoped credentials are in place.

## Phased roadmap

### Phase 0 — containment and measurement (0–2 weeks)

**Goal:** stop the highest-risk growth while making current behaviour visible.

- Freeze new production-publishing clients; keep unsupported WordPress destinations blocked.
- Put the web UI behind an identity-aware proxy with MFA and individual audit identity; restrict admin access privately.
- Alert on stuck cards, missed/failed crons, intake last-success, review-notify failure, and per-client LiteLLM budget thresholds.
- Enforce “budget exhausted = pause this client”; prohibit fallback to agency billing.
- Inventory every client, key, ClickUp location, asset prefix, downstream account and secret path in a non-secret registry.
- Create encrypted off-host backups and perform a documented restore drill for orchestration, LiteLLM and memory state.
- Baseline queue arrival/service time by specialist and host CPU steal.

**Exit criteria:** all active work has a known client mapping; unsupported publishing fails closed; actionable alerts reach an on-call person; restore results and RPO/RTO are recorded.

### Phase 1 — safe, repeatable onboarding (2–6 weeks)

**Goal:** onboard a pilot client without ad hoc production edits.

- Define canonical client manifests, lifecycle states and slug rules.
- Build the idempotent onboarding reconciler and separate least-privilege Infisical provisioner identity.
- Automate LiteLLM team/key/budget/rate-limit creation and capability bundles.
- Put sanitised Hermes/LiteLLM configuration under version control with validation and drift detection.
- Add memory migrations and automated positive/negative RLS tests.
- Make the brand kit a versioned, human-approved readiness artefact; block dispatch until it passes.
- Provision a client-pinned WordPress connector and validate draft-only operation; add equivalent account binding for social/email where sold.
- Run a synthetic onboarding test and rollback/offboarding test.

**Exit criteria:** a new client goes from manifest to `READY` reproducibly; rerunning is safe; no runtime identity can write secrets; cross-client access tests fail as expected; draft publication targets the verified client site/account.

### Phase 2 — deterministic tenancy and durable orchestration (4–10 weeks)

**Goal:** remove tenant selection and workflow correctness from prompts/local files.

- Deploy the immutable work envelope and server-side tenant/provider injection.
- Replace text matching/local SQLite coordination with typed durable records, idempotency keys, outbox events and worker leases.
- Implement ClickUp webhooks plus per-workspace reconciliation and deduplication.
- Put a tenant-aware capability/policy gateway in front of MCP tools.
- Implement approval tokens bound to artefact and destination for every live action.
- Add per-client audit trails spanning intake → model spend → tool actions → approval → publication.

**Exit criteria:** workers cannot override tenant or destination; duplicate delivery cannot duplicate drafts/publication; gateway restart does not lose or repeat work; end-to-end traces identify client and work without logging content.

### Phase 3 — capacity and failure-domain scaling (8–16 weeks)

**Goal:** support growing client volume without noisy neighbours or singleton outages.

- Introduce fair queues, per-client concurrency quotas and stage-specific autoscaling.
- Separate crawler, renderer and specialist worker resources; capacity-test against real service-time distributions.
- Run redundant stateless gateways and leader-elected schedulers.
- Move stateful dependencies off the single host or replicate them; distribute orchestration cells across at least two failure domains.
- Exercise gateway, worker, database, provider and whole-host failure; verify RTO/RPO and no duplicate side effects.

**Exit criteria:** one client cannot consume another's reserved capacity; one gateway/worker/host failure does not stop intake or corrupt state; tested throughput meets the forecast at acceptable queue age.

### Phase 4 — controlled expansion and continuous assurance (ongoing)

**Goal:** make scale routine rather than a sequence of exceptions.

- Roll out by client cohorts with canaries and automatic rollback gates.
- Continuously reconcile keys, capabilities, secret metadata, connector health and config drift.
- Run scheduled tenant-escape, restore, budget-exhaustion and publish-idempotency tests.
- Offer dedicated cells/accounts where contractual risk requires them.
- Review capacity, incidents, cost attribution and access quarterly; version capability bundles instead of mutating them in place.

**Exit criteria:** onboarding/offboarding has an auditable SLA; isolation tests run in CI and production-safe probes; capacity is forecast from telemetry; no manual per-server grant or secret-staging step remains in the normal path.

## Recommended first design decisions

1. **Choose the tenant authority:** a registry-issued client UUID mapped to a stable slug; never infer it from free text.
2. **Choose the isolation unit:** shared cells for normal clients, dedicated cells for high-risk clients; do not deploy one unbounded global fleet.
3. **Choose the publication boundary:** client-pinned connectors plus one-use approval capabilities, not a shared credential selected by the model.
4. **Choose the entitlement abstraction:** versioned capabilities reconciled to LiteLLM, not explicit server lists maintained on every key.
5. **Choose availability objectives:** publish RPO/RTO and queue/intake SLOs before selecting database/HA topology.
6. **Keep runtime identities read-only:** solve provisioning with a separate approval-controlled identity rather than expanding Hermes privileges.

## Evidence reviewed

- `CLAUDE.md` (primary live-system description)
- `hermes/WORKER_PROTOCOL.md`, `hermes/config.yaml.template`, `hermes/compose.yml`, `hermes/setup-sandbox.sh`
- `hermes/souls/*.SOUL.md`, `hermes/skills/marketing/**`, and current intake monitors in `hermes/scripts/`
- `litellm-wrapper/infisical_fetch.py` and `litellm-wrapper/litellm_entrypoint.sh`
- Current sidecars under `mcp-memory/`, `mcp-spaces/`, `mcp-wordpress/`, `mcp-postiz-extras/`, `mcp-mailchimp/`, `mcp-lnkbio/`, and `mcp-higgsfield/`; legacy MCPs were considered only for lessons
- `firecrawl/compose.yml` and `searxng/settings.yml.template`

This review intentionally contains no credentials or secret values.
