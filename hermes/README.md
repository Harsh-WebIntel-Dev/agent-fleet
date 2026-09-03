# Hermes — the client-facing front door

> **Scope & authority.** This documents the `hermes-agent-for-webintelligenz` Coolify service
> (`zhvjhbo5752ovx1nl2rk9v30`) — the Hermes runtime that hosts the fleet. For the **current fleet
> model** (Webster the PM plus five specialists as Hermes profiles, per-client billing via LiteLLM
> keys), **`CLAUDE.md` in the repo root is the authority.** Where this older per-service file and
> `CLAUDE.md` disagree, follow `CLAUDE.md`.

Hermes hosts the conversation **and** the work: Webster (the default/PM profile) talks to people and
orchestrates five specialist profiles in the same runtime via `multiplex_profiles`. The earlier
"Hermes holds the conversation, OpenClaw does the work" split is **superseded** — OpenClaw was
replaced by the Hermes-native fleet on **2026-08-24**.

## Per-client isolation — how it actually works now

This README originally ran **one Hermes container per client**, so a conversation could only ever
spend that client's LiteLLM budget. The live system does this differently: a **single** Hermes-native
fleet serves every client, and per-client budget isolation is enforced by **per-client LiteLLM
virtual keys + spend tags** (Webster stamps `provider="litellm-<slug>"` on each kanban card) rather
than by a container per client. See `CLAUDE.md` §4 and §6.

| | |
|---|---|
| agent image | `nousresearch/hermes-agent:v2026.8.18` = **v0.20.4** (tag, not a sha digest — a digest tells a reviewer nothing about age) |
| webui image | `ghcr.io/nesquena/hermes-webui@sha256:d483b07…` = **0.52.247**, pinned **by digest** (see below) |
| model | default profile runs on **`standard`** live — the older `flash` rationale below is under review; see the discrepancy note in `CLAUDE.md` §6 |
| keys | per-client LiteLLM virtual keys + budgets (WI agency + `biogone` / `pride-advice` / `radiance-wealth`) — see `CLAUDE.md` §6 |
| config | `~/.hermes/config.yaml` — see `config.yaml.template` |

## A note on the model tier

Hermes is high-turn, low-depth: it converses and hands off, so the front door was speced for a cheap
tier — deepseek-v4-flash is roughly **16× cheaper in and out** than v4-pro ($0.08/$0.25 vs
$1.32/$3.96), and the fleet keeps the expensive tiers for work that is actually produced.

> **⚠️ Discrepancy to confirm (2026-09-04):** live `config.yaml` has `model.default: standard`, not
> `flash`. Intent is **unconfirmed** — this may be a deliberate quality change or a drift. Verify via
> litellm spend logs which model `webster-pm` actually bills before relying on either the flash
> reasoning above or this note. See `CLAUDE.md` §6.

`discover_models: false` is deliberate — LiteLLM exposes every alias the key can reach, including
embeddings and the tier-blocked partner models. Hermes should only offer the conversational ones.

## Exposure — read before changing

`*.widev.com.au` is a **WILDCARD DNS record onto this box's public IP**. Any FQDN Coolify generates
is internet-facing the moment the container starts, and this UI reaches an agent holding a client's
key and their conversation history.

**PUBLIC by decision (2026-08-19):** `https://wi-agent.widev.com.au` — 302 to the login page.
`http://100.115.104.5:18793` is kept as an admin path that does not depend on Traefik.

`HERMES_WEBUI_PASSWORD` (31 chars) is the **only** control. No IP allowlist, no SSO, no second
factor — anyone who finds the hostname reaches the login page. Blast radius is bounded by the key
being per-client and budget-capped ($50/30d), not by the network.

Note the stored FQDN reads `https://wi-agent.widev.com.au:8787`; the `:8787` is Coolify's internal
port notation, not a listening port. The public URL is the plain hostname — `:8787` returns 000.

## Version — the old pin is resolved history

`hermes-agent` **was** pinned to v2026.7.7.2 (v0.18.2), NOT the latest. The reason:

hermes-agent **v0.19.0 (2026.7.20)** added a guard in `setup.py`:
`"Building wheels or sdists for hermes-agent is not supported. Use editable install instead."`
hermes-webui's `docker_init.bash` installed the agent with `uv pip install "$_stage_src[all]"` — a
wheel install — so **every agent from 2026.7.20 onward crash-looped the webui** (upstream
[nesquena/hermes-webui#6558](https://github.com/nesquena/hermes-webui/issues/6558)).

**That is fixed.** The issue closed **2026-07-29**, one minute after webui v0.52.106 was published;
`docker_init.bash` now does `uv pip install -e "$_stage_src[all]"` — an **editable** install, exactly
what the agent demands. The fleet then **upgraded 2026-08-19** to agent **v0.20.4 (`v2026.8.18`)** on
**webui 0.52.247**, pinned **by digest** `d483b07…` (0.52.247 is an exp/prerelease — the only webui
that installs the A2A-capable agent — so it is pinned by digest rather than `:latest` to stop a
recreate silently pulling a newer, possibly-broken build onto this public service).

**Why the upgrade: A2A.** v0.18.2 does not ship Agent2Agent; v0.20.4 does (agent card at
`/.well-known/agent-card.json`, JSON-RPC 2.0 over `POST /`, plus `a2a_discover`/`a2a_call` tools).
A2A is what lets the fleet's profiles talk through LiteLLM's agent gateway instead of a custom shim.
`hermes/compose.yml` documents this in full; see also `CLAUDE.md` §2 (A2A, internal-only :9900) and
§15.

### Two operational traps if you ever change the agent version again

1. **The named volume shadows the image.** `hermes-agent-src:/opt/hermes` only populates when
   empty, so downgrading the image changes nothing — `hermes --version` still reports the volume's
   version while the image tag says otherwise. The volume must be removed for a version change to
   take effect: `docker volume rm <service>_hermes-agent-src`. Never remove `hermes-home` — it
   holds `config.yaml`, auth and sessions.
2. **Permissions after any volume reset.** The agent repopulates `/opt/hermes` as root with files
   the webui (which drops to uid 1000) cannot read, and its rsync fails with
   `code 23 / Failed to stage hermes-agent source`. Fix:
   `docker exec <agent> chmod -R a+rX /opt/hermes`, then restart the webui. **Re-run this after
   every source-volume reset.**

## Verified

- **2026-09-04 (read-only prod-2 audit):** agent healthy on **v0.20.4 (`v2026.8.18`)**, webui on
  **0.52.247** (digest `d483b07…`), 0 restarts; A2A enabled internal-only.
- **2026-08-19:** `https://wi-agent.widev.com.au` → 302 (login), Tailscale 18793 → 302; a real turn
  billed `client-webintelligenz` ~$0.0012/turn on flash vs ~$0.023–0.038 on the fleet's pro tier
  while the fleet and acme keys stayed flat.
