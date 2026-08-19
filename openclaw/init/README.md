# Container init hooks

`setup-tools.sh` runs on every boot via `OPENCLAW_DOCKER_INIT_SCRIPT` — a supported OpenClaw
entrypoint hook, so no custom image is required.

## Higgsfield: the vendor CLI, not a wrapper

We use Higgsfield's own CLI rather than wrapping their REST API. **Their API surface will change;
their CLI is the thing they keep in step with it.** A wrapper would be ours to watch and maintain.

Their official MCP server (`mcp.higgsfield.ai`) is **not usable by the fleet**: it advertises only
`authorization_code` + `refresh_token` — no `client_credentials` — so it issues a per-user token
that a machine key can never use. Same shape that blocked SEMrush's OAuth path, but with no
API-key alternative documented.

## Auth — how it was done, and how to redo it

`higgsfield auth login` is browser OAuth PKCE with a **loopback callback on port 8765**
(`http://localhost:8765/callback`, found by grepping the CLI binary). `--port` is accepted but any
other port fails: the OAuth client has pre-registered redirect URIs and only 8765 is among them.

On a headless server, tunnel that exact port:

```
# terminal 1 — leave open
ssh -L 8765:localhost:8765 webintelligenz-prod-2
# terminal 2
ssh webintelligenz-prod-2 '~/.npm-global/bin/higgsfield auth login'   # no --port
higgsfield workspace set <workspace_id>                               # not secret
```

The stored credential carries a **`refresh_token`**, so this is a one-time interactive step — the
CLI renews itself thereafter.

**Authorise the server separately from any workstation.** If refresh tokens rotate on use, two
machines sharing one credential will invalidate each other and whichever refreshed last wins.

## Paths that matter

| path | why |
|---|---|
| `/data/npm-global` | npm prefix — **persistent volume**, so the CLI survives a container recreate |
| `/data/.config/higgsfield` | credentials — persistent, and symlinked from both `$HOME` values |

`HOME` is `/data` for the gateway process but `/root` in an exec session, and `/root` is an image
layer that is lost on recreate. The script symlinks both to the one persistent location rather than
copying, because a copy would drift the moment the CLI refreshes its token.

## Verified 2026-08-19
The `image` agent ran `higgsfield account status` and `higgsfield model list` through its own exec
tool: `accounts@webintelligenz.com`, ultra plan, 2584 credits, **80 models** including
`text2image_soul_v2`, `flux_2`, `gpt_image_2`, `grok_image`.

## Still to wire
Higgsfield outputs are retained for **~7 days only**. Generated assets must be pulled into Spaces
(`clients/<slug>/images/`) via the `spaces_write` tool, or they expire.
