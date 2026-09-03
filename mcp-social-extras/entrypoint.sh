#!/usr/bin/env sh
# Pull Postiz + Lnk.Bio creds from Infisical when reachable; fall back to Coolify env otherwise.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python /app/infisical_fetch.py POSTIZ_MCP_TOKEN /shared 2>/dev/null || true); [ -n "$V" ] && export POSTIZ_MCP_TOKEN="$V"
  V=$(python /app/infisical_fetch.py LNKBIO_CLIENT_ID /shared 2>/dev/null || true); [ -n "$V" ] && export LNKBIO_CLIENT_ID="$V"
  V=$(python /app/infisical_fetch.py LNKBIO_CLIENT_SECRET /shared 2>/dev/null || true); [ -n "$V" ] && export LNKBIO_CLIENT_SECRET="$V"
  V=$(python /app/infisical_fetch.py LNKBIO_PROFILE /shared 2>/dev/null || true); [ -n "$V" ] && export LNKBIO_PROFILE="$V"
  echo "[entrypoint] Infisical cred pull attempted (fail-safe; env fallback)"
fi
exec python /app/server.py
