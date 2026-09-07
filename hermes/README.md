# Hermes — the client-facing front door

One Hermes instance **per client**. Deployed as Coolify service `hermes-agent-for-webintelligenz`
(`zhvjhbo5752ovx1nl2rk9v30`) for the first client.

Hermes is a different product from the fleet runtime: **Hermes holds the conversation, OpenClaw does
the work.** `nousresearch/hermes-agent` + `ghcr.io/nesquena/hermes-webui`.

## Why one per client

The client's LiteLLM virtual key lives in this container, so a conversation can only ever spend that
client's budget, and their history stays in their own volume. That is the whole reason this is not
a shared service.

| | |
|---|---|
| agent image | `nousresearch/hermes-agent:v2026.8.18` (tag, not a sha digest — a digest tells a reviewer nothing about age) |
| webui image | `ghcr.io/nesquena/hermes-webui:0.51.92` |
| model | `flash` → `deepseek-v4-flash-0731`, **$0.08/$0.25 per M** |
| key | LiteLLM virtual key `client-webintelligenz`, $50/30d |
| config | `~/.hermes/config.yaml` — see `config.yaml.template` |

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

**PUBLIC by decision (2026-08-19):** `https://wi-agent.widev.com.au` — 302 to the login page.
`http://100.115.104.5:18793` is kept as an admin path that does not depend on Traefik.

`HERMES_WEBUI_PASSWORD` (31 chars) is the **only** control. No IP allowlist, no SSO, no second
factor — anyone who finds the hostname reaches the login page. Blast radius is bounded by the key
being per-client and budget-capped ($50/30d), not by the network.

Note the stored FQDN reads `https://wi-agent.widev.com.au:8787`; the `:8787` is Coolify's internal
port notation, not a listening port. The public URL is the plain hostname — `:8787` returns 000.

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

To remove the source volume the container must be **stopped and removed** first — a merely stopped
container still holds it and `docker volume rm` fails with `volume is in use`.

### Verified tag → version mapping (2026-09-07)

The tag date is **not** the version, and two different builds can report the same version string.
Verified by running `hermes --version` inside each image:

| tag | version | upstream rev |
|---|---|---|
| `v2026.7.7.2` | v0.18.2 | — |
| `v2026.8.18` | v0.20.4 | `e624e9fd` |
| `v2026.8.31` | **v0.21.0** | `29112bef` |
| `latest` / `main` (untagged rolling, 2026-09-06) | v0.21.0 | `693641aa` |

Note the last two rows: **`v2026.8.31` and the rolling build both report `v0.21.0` but are different
code.** Only the rolling one ships `/api/local-models/*` and `/api/audio/tts-lease`. Check the
upstream rev, not the version string. See CLAUDE.md §16 for the Hermes Desktop route-gap detail.

## Verified working 2026-08-19
- agent healthy on v2026.7.7.2, webui healthy on 0.51.680
- `https://wi-agent.widev.com.au` -> 302 (login), Tailscale 18793 -> 302
- a real turn billed `client-webintelligenz` $0.000000 -> $0.001207 while the fleet and acme keys
  stayed flat — ~$0.0012/turn on flash vs ~$0.023-0.038 on the fleet's pro tier
