#!/usr/bin/env sh
# Lnk.Bio creds: try Infisical /shared (future), else use the Coolify env values. WEB INTELLIGENZ ONLY.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  for K in LNKBIO_CLIENT_ID LNKBIO_CLIENT_SECRET LNKBIO_PROFILE; do
    V=$(python /app/infisical_fetch.py "$K" /shared 2>/dev/null || true); [ -n "$V" ] && export "$K=$V"
  done
fi
exec python /app/server.py
