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

## Auth (interactive, human-only)
1. On any machine with a browser: `higgsfield auth login` (ideally a session dedicated to the fleet).
2. Copy the resulting `~/.config/higgsfield/credentials.json` contents.
3. Paste it into Coolify / Infisical `/shared` as `HIGGSFIELD_CREDENTIALS_JSON`, then restart.
4. **Stop using the CLI on that machine afterwards** — see the rotation warning below.

The entrypoint seeds it onto a persisted volume once; the CLI refreshes from there. An existing usable
bundle is never overwritten on restart, because the seed is older than the rotated token.

### The credential is rotating, and the volume is its only live copy
Clerk issues a **~24-hour** access token (86,400s — MEASURED 2026-09-10 from a freshly issued
credential; a Clerk doc *example* showing 7200s is not this deployment) and returns a **new
`refresh_token` on every exchange**. So:

- `HIGGSFIELD_CREDENTIALS_JSON` is a **first-boot seed only**. It is correct only until the next
  rotation; after that it replays as `invalid_grant`. A restart will re-seed it and still fail.
- The live bundle exists **only** on the `higgsfield-config` volume. If it is lost, auth is lost and a
  human must repeat the browser login — there is no headless recovery.
- Never let two copies refresh in parallel (e.g. a laptop login left active after transplanting it).
  Rotation with reuse detection can revoke the whole token family.

### Durability guard (`credguard.py`)
The vendored CLI **deletes `credentials.json` on a failed refresh and writes no replacement** — it took
the lock, unlinked the file, and left a zero-byte `credentials.json.lock` behind on 2026-09-09, which is
durable data loss on a named volume. Reproduced deterministically on 2026-09-10: it happens on *every*
failed refresh. We cannot patch the vendor binary, so every CLI call is bracketed instead:

- **snapshot → run → restore.** If the bundle goes missing, empty or unparseable, it is atomically put
  back from `credentials.json.prev`. A successful (rotated) bundle is never clobbered.
- **All writes are temp + fsync + rename**, so no crash can leave a zero-byte credentials file.
- **Stale locks are cleared** on boot and after a destructive failure, so one dead refresh cannot block
  every retry forever.
- **CLI calls are serialised in-process**, so two concurrent refreshes can't present the same
  refresh_token to Clerk and trip reuse detection.
- **Rotated bundles are pushed back to Infisical** (`infisical_push.py`, value on stdin, never argv) so
  the seed stops rotting. **Currently blocked:** verified 2026-09-10 that Infisical answers
  `403 "You are not allowed to edit on secrets"` — the `fleet-hermes` identity cannot edit an existing
  secret. Grant it `secrets:edit` on `/shared` and this starts working with no code change. Until then
  the push logs `FORBIDDEN`, records the miss in `rotation.log`, and renders continue.
- **Auth failures are reported honestly.** The CLI's `request failed (no response received)` is
  rewritten into the actual remedy, because that string means dead credentials, not a network blip.

### Health signal: `account_status` reports credential drift
The rot was silent for 14 days — the credential rotated daily while the seed stayed frozen, and
nothing surfaced it. `account_status` now returns a `credential` block on both the success and failure
paths:

| field | meaning |
|---|---|
| `status` | `ok` / `seed_drift` / `no_seed` / `no_credentials` |
| `seed_matches_live` | **the rot signal** — false means the seed would replay as `invalid_grant` |
| `live_expires_at_utc`, `seed_expires_at_utc` | readable expiries |
| `live_*_token_len` | lengths only — never a token value |

The same line is logged on every boot, so `docker logs` answers "is the seed still good?" without a
tool call. `rotation.log` on the volume records each rotation and whether it persisted (bounded to
100 lines). An expired *access* token is normal between calls, so it is reported but never alarmed on.

## Tests
No container or network needed:
```bash
python3 -m pytest                 # credguard + server._run  (61 tests)
./test_entrypoint.sh              # boot precedence + lock clearing (16 checks)
```

## Deploy
Build `mcp-higgsfield:0.2.2` on the server, run as a Coolify service (internal only), enable "Connect to
Predefined Networks", then register in LiteLLM as `higgsfield` under access group `fleet_tools`.
