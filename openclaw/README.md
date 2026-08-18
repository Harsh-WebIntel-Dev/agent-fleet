# OpenClaw — the agent runtime

Deployed as Coolify service `openclaw-s13f8pdutxps4w5z3fbl9lq5` on `webintelligenz-prod-2`.
Image `coollabsio/openclaw:2026.7.1-2` (pinned; the Coolify template shipped 2026.2.6 from February).

**This replaces the custom FastAPI orchestrator** that previously lived in `agent-fleet/nemoclaw/`.
That was a mistake: "NemoClaw" is a real NVIDIA product (an enterprise distribution of OpenClaw),
not a name to give a bespoke service. OpenClaw already provides the agent runtime, native MCP
tool-calling, channels and browser control that the custom code was reimplementing.

## Access — Tailscale only, never public

| | |
|---|---|
| URL | `http://100.115.104.5:18792` (nginx, HTTP basic auth) |
| Public | **blocked** — verified: public IP and the wildcard FQDN both return 000 |
| Credentials | Coolify env `SERVICE_USER_OPENCLAW` / `SERVICE_PASSWORD_OPENCLAW` |

Three independent things keep it private, because one is not enough:
1. No FQDN on the Coolify application (cleared in the UI — the API on Coolify 4.1.2 cannot do this)
2. `traefik.enable=false` label, so Traefik will not route to it even if an FQDN reappears
3. Port published to `100.115.104.5:18792` only — the Tailscale interface, never `0.0.0.0`

> **`*.widev.com.au` is a WILDCARD DNS record pointing at this box's public IP.** Any FQDN Coolify
> generates is therefore instantly internet-facing. Always clear it before starting a service here.

## Model routing — everything goes through LiteLLM

All model traffic is routed through the LiteLLM proxy so spend, logs and Langfuse traces land in
one place. Configured via `OPENCLAW_CONFIG_JSON`, which `/app/scripts/configure.js` merges into
`/data/.openclaw/openclaw.json` on every boot — the supported extension point, no image changes.

```
provider litellm: openai-completions -> http://litellm-v10up2yg1cwxo0k1ks9j2qro:4000/v1
models:  standard | fast | deep (deepseek-v4-pro) · oss-120b | oss-20b
primary: litellm/standard
```

The `apiKey` is a LiteLLM **virtual key** (`openclaw-gateway`, team `openclaw-fleet`, $25/30d cap),
so fleet spend is attributed and capped rather than running on the master key.

### Why a custom provider and not the built-in `openai` one
`configure.js` states built-in providers "already know their baseUrl" and will be **rejected** if
given a `models.providers` entry. A custom-named provider supplying `{api, baseUrl, models[]}` is
the documented route for an OpenAI-compatible proxy.

`OPENAI_API_KEY` is still set (to the same virtual key) purely to satisfy the entrypoint gate —
it requires at least one provider env var or the container crash-loops.

## Gotchas paid for in real time

- **`openclaw doctor --fix` takes ~3.5 minutes on first boot.** Nothing listens on 8080 until it
  finishes. It is not hung.
- **Port 8080 is nginx with basic auth**, proxying to the gateway on 18789. `gateway.bind` is
  `loopback`, so the gateway is only reachable through nginx.
- **Changing a Coolify env var recreates the container and leaves it `Created`.** A Coolify
  `restart` is not enough — `docker start <container>` may be needed. Always confirm with
  `docker ps`, never trust the queued-job message.
- Doctor logs `systemd user services are unavailable` — expected and harmless in a container; the
  entrypoint runs the gateway in the foreground instead.
- Doctor auto-upgrades retired model refs (it rewrote `openai/gpt-5.2` → `gpt-5.5` before our
  config took effect), so pin the primary model explicitly via `OPENCLAW_CONFIG_JSON`.
- Host port 8080 belongs to `coolify-proxy` on `0.0.0.0` — never bind it.
