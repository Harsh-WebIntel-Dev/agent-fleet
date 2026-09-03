#!/usr/bin/env sh
# Pull the shared Postiz API token from Infisical when reachable; fall back to Coolify env otherwise.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python /app/infisical_fetch.py POSTIZ_MCP_TOKEN /shared 2>/dev/null || true)
  if [ -n "$V" ]; then export POSTIZ_MCP_TOKEN="$V"; echo "[entrypoint] pulled POSTIZ_MCP_TOKEN from Infisical"; else echo "[entrypoint] Infisical unavailable -- env fallback" >&2; fi
fi
exec python /app/server.py
