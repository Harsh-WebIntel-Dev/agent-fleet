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

Currently Tailscale-only: `http://100.115.104.5:18793`, `traefik.enable=false`.

If the client is meant to reach it, restoring the FQDN is a deliberate decision, and
`HERMES_WEBUI_PASSWORD` would be the **only** control — anyone who finds the hostname reaches the
login page.

## Still required
- **"Connect to Predefined Networks"** — currently `false`; without it Hermes cannot resolve
  LiteLLM. UI-only on Coolify 4.1.2.
- Drop `config.yaml` into the `hermes-home` volume, then verify a turn bills the
  `client-webintelligenz` key and no other.
