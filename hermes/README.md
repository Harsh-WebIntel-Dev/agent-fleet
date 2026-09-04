# Hermes — the client-facing front door

One Hermes instance **per client**. Deployed as Coolify service `hermes-agent-for-webintelligenz`
(`zhvjhbo5752ovx1nl2rk9v30`) for the first client.

Hermes is a different product from the fleet runtime: **Hermes holds the conversation, OpenClaw does
the work.** Image: `nousresearch/hermes-agent` — a **single container**, which now also serves the
browser/Desktop HTTP surface itself (see *One container, two s6 slots* below). The former
`ghcr.io/nesquena/hermes-webui` sidecar was removed on 2026-09-04.

## Why one per client

The client's LiteLLM virtual key lives in this container, so a conversation can only ever spend that
client's budget, and their history stays in their own volume. That is the whole reason this is not
a shared service.

| | |
|---|---|
| agent image | `nousresearch/hermes-agent:v2026.8.18` (tag, not a sha digest — a digest tells a reviewer nothing about age) |
| webui image | — none. Removed 2026-09-04; the agent's own `dashboard` s6 slot serves the SPA |
| model | `flash` → `deepseek-v4-flash-0731`, **$0.08/$0.25 per M** |
| key | LiteLLM virtual key `client-webintelligenz`, $50/30d |
| config | `~/.hermes/config.yaml` — see `config.yaml.template` |

## One container, two s6 slots — do not add a third container

**Never add a second container that mounts `hermes-home`.** The image registers a supervised
`gateway-default` s6 slot in *every* container that mounts it and auto-runs
`hermes gateway run --replace`, so a sidecar becomes a **second gateway** and steals ownership of
`email`, `a2a` and `api_server` (`gateway_state.json`'s `writer_pid` flips to it). This happened on
2026-09-04 with a `hermes serve` sidecar; there is no env switch to opt a container out.

Extra HTTP surfaces go on the **existing** container via the image's own `dashboard` slot:

```yaml
- HERMES_DASHBOARD=1            # gates the slot; unset => run exits 0, finish returns 125 (down)
- HERMES_DASHBOARD_HOST=0.0.0.0 # CONTAINER-side bind; the publish pins the HOST side to the tailnet
- HERMES_DASHBOARD_PORT=9119
- 'HERMES_DASHBOARD_BASIC_AUTH_USERNAME=${...}'   # Coolify env vars, never literals
- 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=${...}'
- 'HERMES_DASHBOARD_BASIC_AUTH_SECRET=${...}'     # HMAC session key; without it restarts log everyone out
```

`hermes dashboard` and `hermes serve` are the **same backend** (`cmd_dashboard` →
`web_server.start_server`; `serve` only skips the SPA build), so this slot serves the `/api/health`
→ `/api/status` → `/api/ws` sequence **Hermes Desktop** probes — none of which are served by
`hermes gateway run`, by A2A on `:9900`, or by the `api_server` platform on `:8642`. Enabling it
starts no extra gateway: the slot execs `hermes dashboard` only, `web_server` never writes platform
ownership, and the cron-ticker/orphan-reap path is gated on `HERMES_DESKTOP=1` (unset here).

Auth is **fail-closed** — any non-loopback bind (tailnet included) engages the gate and
`start_server` refuses to boot without a registered provider. `HERMES_DASHBOARD_INSECURE` has been a
no-op since the June 2026 hardening.

**Hermes Desktop** connects in remote-URL mode (not SSH) to `http://100.115.104.5:18795` with those
basic-auth credentials, from a machine on the tailnet.

## Why flash and not the fleet tier

Hermes is high-turn, low-depth: it converses and hands off. deepseek-v4-flash is roughly **16×
cheaper in and out** than v4-pro ($0.08/$0.25 vs $1.32/$3.96). The fleet keeps the expensive tiers
for work that is actually produced.

`discover_models: false` is deliberate — LiteLLM exposes every alias the key can reach, including
embeddings and the tier-blocked partner models. Hermes should only offer the conversational ones.

## Exposure — read before changing

`*.widev.com.au` is a **WILDCARD DNS record onto this box's public IP**. Any FQDN Coolify generates
is internet-facing the moment the container starts, and this UI reaches an agent holding a client's
key and their conversation history.

**As of 2026-09-04 nothing on this stack is public.** `traefik.enable=false` on the agent, and both
HTTP surfaces are published with a **HostIp-scoped** binding to the Tailscale address, so they are
tailnet-only even though the container binds `0.0.0.0`:

| tailnet URL | container port | what | auth |
|---|---|---|---|
| `http://100.115.104.5:18794` | 9900 | A2A (agent card + JSON-RPC) | bearer, peer `fleet` |
| `http://100.115.104.5:18795` | 9119 | dashboard SPA + Hermes Desktop backend | basic |

Verified: `http://46.250.245.204:18795` and `:18794` on the **public** IP both refuse (000), and
`/api/status` reports `auth_required: true`, `auth_providers: ["basic"]`. A2A also has an HTTPS front
at `https://…ts.net:8447` via `tailscale serve`; `:18795` does not (yet).

**`https://wi-agent.widev.com.au` now returns 503** — it pointed at the removed webui and nothing
replaced it. That is a real loss of off-tailnet browser access, and restoring it is an open decision
(see CLAUDE.md §16). The previous posture was `HERMES_WEBUI_PASSWORD` (31 chars) as the *only* control
on an internet-facing UI holding a client's key and history — no IP allowlist, no SSO, no second
factor — so the tailnet-only replacement is a stricter posture, not merely an outage.

Note the stored Coolify FQDN reads `https://wi-agent.widev.com.au:8787`; the `:8787` was Coolify's
internal port notation, not a listening port. That sub-application record is now stale.

## Version pinning — do not "upgrade to latest" without reading this

`hermes-agent` is pinned to **v2026.7.7.2 (v0.18.2)** deliberately, NOT the latest.

hermes-agent **v0.19.0 (2026.7.20)** added a guard in `setup.py`:
`"Building wheels or sdists for hermes-agent is not supported. Use editable install instead."`
hermes-webui's `docker_init.bash` installs the agent with `uv pip install "$_stage_src[all]"` — a
wheel install — so **every agent from 2026.7.20 onward crash-loops the webui**.

Upstream: [nesquena/hermes-webui#6558](https://github.com/nesquena/hermes-webui/issues/6558),
**closed as not planned**, labelled "requires upstream changes". Verified still unfixed in webui
`0.51.680`, whose `docker_init.bash` line ~467 is unchanged. Upgrading the webui does not help;
only pinning the agent below 2026.7.20 does.

### Two traps that made this hard to diagnose

1. **The named volume shadows the image.** `hermes-agent-src:/opt/hermes` only populates when
   empty, so downgrading the image changed nothing — `hermes --version` still reported the NEW
   version while the image tag said otherwise. The volume must be removed for a version change to
   take effect: `docker volume rm <service>_hermes-agent-src`. Never remove `hermes-home` — it
   holds `config.yaml`, auth and sessions.
2. **Permissions after any volume reset.** The agent repopulates `/opt/hermes` as root with files
   the webui (which drops to uid 1000) cannot read, and its rsync fails with
   `code 23 / Failed to stage hermes-agent source`. Fix:
   `docker exec <agent> chmod -R a+rX /opt/hermes`, then restart the webui. **Re-run this after
   every source-volume reset.**

## Verified working 2026-08-19
- agent healthy on v2026.7.7.2, webui healthy on 0.51.680
- `https://wi-agent.widev.com.au` -> 302 (login), Tailscale 18793 -> 302
- a real turn billed `client-webintelligenz` $0.000000 -> $0.001207 while the fleet and acme keys
  stayed flat — ~$0.0012/turn on flash vs ~$0.023-0.038 on the fleet's pro tier
