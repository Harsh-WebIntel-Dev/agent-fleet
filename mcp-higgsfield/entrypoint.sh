#!/usr/bin/env bash
# Bring up the Higgsfield credential, then hand off to the MCP server.
#
# Higgsfield auth is a ROTATING OAuth pair (Clerk: ~24h access token — MEASURED 2026-09-10 — and a new
# refresh_token on every exchange). The live bundle therefore diverges from the staged seed as soon as
# it rotates, and the volume is the only place it lives.
#
# Credential SELECTION lives in bootstrap.py (unit-tested) rather than in this shell script: the rule
# is "newest expires_at wins" across {volume, snapshot, seed}, which both protects a rotated token
# from a stale seed and lets a human recover by re-staging a fresh one. A previous "never clobber"
# rule silently kept a dead bundle when a fresh seed was staged.
set -euo pipefail
CFG="${HIGGSFIELD_CONFIG_DIR:-${HOME:-/root}/.config/higgsfield}"
export HIGGSFIELD_CONFIG_DIR="$CFG"
mkdir -p "$CFG"

# Pull the seed from Infisical (the fleet's source of truth) when reachable, else fall back to the
# Coolify env — fail-safe, so an Infisical outage can never brick this service at boot.
#
# NOTE: this is a BOOT-ONLY pull. A secret re-staged in Infisical is not picked up until a restart —
# the same trap that made Mailchimp look unwired for six days.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  if _pulled="$(python3 /app/infisical_fetch.py HIGGSFIELD_CREDENTIALS_JSON /shared 2>/dev/null)" && [ -n "$_pulled" ]; then
    export HIGGSFIELD_CREDENTIALS_JSON="$_pulled"
    echo "[entrypoint] pulled HIGGSFIELD_CREDENTIALS_JSON from Infisical"
  else
    echo "[entrypoint] Infisical unavailable — falling back to the Coolify env seed" >&2
  fi
fi

if [ -n "${HIGGSFIELD_WORKSPACE_ID:-}" ] && [ ! -f "$CFG/config.json" ]; then
  printf '{"workspace_id":"%s"}' "$HIGGSFIELD_WORKSPACE_ID" > "$CFG/config.json"
  echo "[entrypoint] wrote config.json (workspace_id)"
fi

# Clears orphaned locks, then installs whichever of {volume, snapshot, seed} has the latest expiry.
python3 /app/bootstrap.py

exec python3 /app/server.py
