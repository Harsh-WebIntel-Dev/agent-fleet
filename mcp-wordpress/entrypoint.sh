#!/usr/bin/env sh
# Pull the client's WordPress creds from Infisical when reachable; fall back to Coolify env otherwise.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python /app/infisical_fetch.py WP_SITE_URL /clients/webintelligenz 2>/dev/null || true); [ -n "$V" ] && export WP_SITE_URL="$V"
  V=$(python /app/infisical_fetch.py WP_USERNAME /clients/webintelligenz 2>/dev/null || true); [ -n "$V" ] && export WP_USERNAME="$V"
  V=$(python /app/infisical_fetch.py WP_APP_PASSWORD /clients/webintelligenz 2>/dev/null || true)
  if [ -n "$V" ]; then export WP_APP_PASSWORD="$V"; echo "[entrypoint] pulled WP creds from Infisical"; else echo "[entrypoint] Infisical unavailable — using env fallback" >&2; fi
fi
exec python /app/server.py
