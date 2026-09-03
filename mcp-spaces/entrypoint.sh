#!/usr/bin/env sh
# Pull R2 credentials from Infisical (fleet source of truth) when reachable; fall back to the Coolify
# env if Infisical is down — fail-safe so an Infisical outage never bricks this service at boot.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python /app/infisical_fetch.py R2_ACCESS_KEY_ID /shared 2>/dev/null || true)
  [ -n "$V" ] && export DO_SPACES_ACCESS_KEY="$V" && echo "[entrypoint] pulled DO_SPACES_ACCESS_KEY from Infisical"
  V=$(python /app/infisical_fetch.py R2_SECRET_ACCESS_KEY /shared 2>/dev/null || true)
  [ -n "$V" ] && export DO_SPACES_SECRET_KEY="$V" && echo "[entrypoint] pulled DO_SPACES_SECRET_KEY from Infisical"
  [ -z "${V:-}" ] && echo "[entrypoint] Infisical unavailable — using env fallback" >&2
fi
exec python /app/server.py
