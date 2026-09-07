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

## Known issue — supergateway crash-restarts

Between 2026-09-01 and 2026-09-07 the container restarted **28 times**, once per crash:

```
Error: No connection established for request ID: 0
```

Thrown uncaught from supergateway's stateless streamable-HTTP bridge when it tries to write a child
response to an HTTP request whose connection has already gone (27 of 28 were request id `0`, the
`initialize` handshake). Node exits, Docker's `unless-stopped` policy restarts it. This is
**unrelated to the API key** — it happened equally before and after the key was staged, and it will
recur. `restart: unless-stopped` masks it; the symptom is a climbing `RestartCount` plus a brief
window where LiteLLM tool calls fail. Not yet fixed upstream-side; supergateway is now pinned to
`3.4.3` so a rebuild does not silently change this behaviour in either direction.

## Deploying a change

The Coolify service runs a **locally built image** (`mcp-mailchimp:0.1.0`) — there is no registry.
A Coolify redeploy re-runs the entrypoint against the existing image; picking up a change to
`entrypoint.sh`, `infisical_probe.py` or the Dockerfile requires rebuilding that image on prod-2
first.
