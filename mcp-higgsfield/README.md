# mcp-higgsfield

Thin MCP server (mirrors `mcp-a2a`) that wraps the official `@higgsfield/cli` so the fleet `image` agent
can render brand-quality images through the LiteLLM MCP gateway.

## Why a sidecar CLI (not an API key)
Higgsfield has **no API key** — auth is OAuth 2.0 PKCE (browser login once, then headless refresh via the
stored `refresh_token`). The CLI encapsulates the API + refresh, so we wrap it rather than reverse-engineer
the API.

## Async (30s gateway timeout)
Renders take minutes; the OpenClaw MCP gateway times out a call at ~30s. So the tools are async:
`create_image_job` → job_id (fast); `get_image_job(job_id)` → poll (fast); the agent loops until completed.
Tools: `create_image_job`, `get_image_job`, `list_image_models`, `account_status`.

## Auth (one-time, you)
1. On any machine with a browser: `higgsfield auth login` (ideally a session dedicated to the fleet).
2. Copy the resulting `~/.config/higgsfield/credentials.json` contents.
3. Paste it into Coolify as the secret `HIGGSFIELD_CREDENTIALS_JSON` on this service.
The entrypoint seeds it onto a persisted volume once; the CLI refreshes from there. Never overwritten on
restart (a stale seed would break the rotated token).

## Deploy
Build `mcp-higgsfield:0.1.0` on the server, run as a Coolify service (internal only), enable "Connect to
Predefined Networks", then register in LiteLLM as `higgsfield` under access group `fleet_tools`.
