#!/usr/bin/env bash
# Seed Higgsfield credentials ONCE from the Coolify secret onto the persisted volume, then hand the file
# to the CLI which owns refresh from there. We never overwrite an existing file: after first boot the CLI
# rotates the access token in place, and clobbering it with the (now stale) seed would break auth.
set -euo pipefail
CFG="${HOME:-/root}/.config/higgsfield"
mkdir -p "$CFG"
# Pull the credential from Infisical (the fleet's source of truth) when reachable; fall back to the
# Coolify env / the persisted volume if Infisical is down — a fail-safe so an Infisical outage can
# never brick this service at boot.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  if _pulled="$(python3 /app/infisical_fetch.py HIGGSFIELD_CREDENTIALS_JSON /shared 2>/dev/null)" && [ -n "$_pulled" ]; then
    HIGGSFIELD_CREDENTIALS_JSON="$_pulled"
    echo "[entrypoint] pulled HIGGSFIELD_CREDENTIALS_JSON from Infisical"
  else
    echo "[entrypoint] Infisical unavailable — using env/volume fallback" >&2
  fi
fi
if [ ! -f "$CFG/credentials.json" ] && [ -n "${HIGGSFIELD_CREDENTIALS_JSON:-}" ]; then
  printf '%s' "$HIGGSFIELD_CREDENTIALS_JSON" > "$CFG/credentials.json"
  chmod 600 "$CFG/credentials.json"
  echo "[entrypoint] seeded credentials.json from HIGGSFIELD_CREDENTIALS_JSON"
fi
if [ ! -f "$CFG/config.json" ] && [ -n "${HIGGSFIELD_WORKSPACE_ID:-}" ]; then
  printf '{"workspace_id":"%s"}' "$HIGGSFIELD_WORKSPACE_ID" > "$CFG/config.json"
  echo "[entrypoint] wrote config.json (workspace_id)"
fi
if [ ! -f "$CFG/credentials.json" ]; then
  echo "[entrypoint] WARNING: no credentials.json — set HIGGSFIELD_CREDENTIALS_JSON (from 'higgsfield auth login')." >&2
fi
exec python3 /app/server.py
