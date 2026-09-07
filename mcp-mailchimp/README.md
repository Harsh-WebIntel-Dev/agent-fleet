# mcp-mailchimp

Wraps the third-party stdio MCP **`mailchimp-mcp@0.6.0`** and bridges it to streamable-HTTP with
**supergateway**, so the fleet's LiteLLM gateway can reach Mailchimp like any other sidecar. The
`publisher` agent uses it to build newsletter campaigns **as drafts only**.

Unlike the other sidecars this one is Node-based (`node:20-slim`): `mailchimp-mcp` is a Python
package pinned into its own venv (`/opt/mc`, `mcp<2`), and supergateway is Node — bridging with
supergateway keeps Node fully isolated from Python's `mcp` versioning.

## Draft-only, enforced at the gateway

Scoped to the **`wi_tools`** access group (Web Intelligenz only) and exposed through an
`allowed_tools` allow-list in `litellm-cfg/config.yaml`. 18 tools are published — read, build,
preview, `send_test_email`. **Send, schedule and destructive delete are withheld at the gateway**,
so the "never send without human approval" rule in Webster's SOUL is enforced in tooling rather
than in prose. A key's `blocked_tools` does not filter `tools/list`; the server-level
`allowed_tools` is what actually withholds them (CLAUDE.md §6).

## Secrets

`MAILCHIMP_API_KEY` is **pulled from Infisical at boot** — `environment=prod`, path `/shared` — by
`entrypoint.sh` via `infisical_fetch.py`, using the fleet machine identity
(`INFISICAL_CLIENT_ID` / `_CLIENT_SECRET` / `_PROJECT_ID` / `_API_URL` / `_ENV` in the Coolify env).
The Coolify `MAILCHIMP_API_KEY` variable is intentionally left **empty**: Infisical is the store,
and the Coolify entry exists only as the fail-safe fallback slot. An empty value there is correct,
not a misconfiguration.

The pull is fail-safe — if Infisical is unreachable the container still boots (CLAUDE.md §6). After
staging or rotating the secret in Infisical you must **redeploy this service**; the pull only
happens at boot.

### Reading the boot log

`entrypoint.sh` distinguishes four states. Read the exact line before diagnosing:

| Log line | Meaning |
|---|---|
| `pulled MAILCHIMP_API_KEY from Infisical prod:/shared` | Working. |
| `Infisical reachable and machine identity accepted, but MAILCHIMP_API_KEY is not staged …` | Wiring is fine — **stage the secret**, then redeploy. |
| `cannot reach or authenticate to Infisical at <url> -- env fallback` | Network or machine-identity problem. |
| `INFISICAL_CLIENT_ID is empty -- skipping the Infisical pull entirely` | The sidecar is not wired to Infisical at all. |

Earlier versions printed `Infisical unavailable -- env fallback` for **all three** failure states.
That single ambiguous line caused a full misdiagnosis on 2026-09-07 — the sidecar was read as
"never wired to Infisical" when it had been wired correctly since 2026-09-01 and the secret simply
had not been staged yet. `infisical_probe.py` exists purely to keep those cases apart.

## The crash — diagnosed and guarded (2026-09-07)

```
Error: No connection established for request ID: 0
    at WebStandardStreamableHTTPServerTransport.send (…/webStandardStreamableHttp.js:917:27)
    at file:///usr/local/lib/node_modules/supergateway/dist/gateways/stdioToStatelessStreamableHttp.js:120:39
Node.js v20.20.2
```

Thrown when supergateway writes a child response to an HTTP request whose connection has already
gone — overwhelmingly request id `0`, the `initialize` handshake. Between 2026-09-01 and 2026-09-07
it killed the container **36 times** (28 + 8 more on 09-07 alone).

**Why the existing guard doesn't catch it.** supergateway wraps the call:

```js
try { transport.send(jsonMsg) } catch (e) { logger.error(`Failed to send…`, e) }
```

but `transport.send()` is **`async`**. The SDK's throw becomes a *rejected promise*, the synchronous
`catch` never sees it, and Node 20 treats an unhandled rejection as fatal — hence the bare
`Node.js v20.20.2` line that ends every one of these logs.

**The guard.** `ENV NODE_OPTIONS="--unhandled-rejections=warn"` in the Dockerfile. A late write to a
dead socket is a no-op; it should be a logged warning, not a container death. Verified in the image:

```
$ NODE_OPTIONS=--unhandled-rejections=warn node -e 'Promise.reject(new Error(1)); …'
UnhandledPromiseRejectionWarning: …          # and the process keeps running
$ node -e 'Promise.reject(new Error(2)); …'  # default
Node.js v20.20.2                              # dead
```

Remove the guard once supergateway awaits or `.catch()`es `transport.send()`. `--stateful` was
considered as an alternative (it keeps a session's stream alive so the response has somewhere to go)
but it changes transport semantics for every client; the guard is the smaller change and addresses
the fatality directly.

**Why it mattered so much.** This sidecar is the fleet's `tools/list` outlier: the upstream exposes
**115 tools** (the gateway allow-lists 18 of them), so a full listing costs **7–11 s** against
**0.13–0.39 s** for every other sidecar. When a crash lands mid-handshake, LiteLLM's 30 s
`MCP_TOOL_LISTING_TIMEOUT` expires, mailchimp drops out of the aggregate, and the aggregated
`/mcp/` call blows past Hermes' 15 s tool-mount bound — so a *specialist* dispatch silently starts
with **zero** MCP tools. That is what blocked `t_d44a78e0` at 14:10 on 2026-09-07. See CLAUDE.md §7
"Tool mounting is a RACE".

## Deploying a change

The Coolify service runs a **locally built image** (`mcp-mailchimp:0.1.0`) — there is no registry.
A Coolify redeploy re-runs the entrypoint against the existing image; picking up a change to
`entrypoint.sh`, `infisical_probe.py` or the Dockerfile requires rebuilding that image on prod-2
first.
