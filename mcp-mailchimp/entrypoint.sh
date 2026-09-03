#!/usr/bin/env sh
# Pull the Mailchimp API key from Infisical /shared when reachable; fall back to Coolify env otherwise.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python3 /app/infisical_fetch.py MAILCHIMP_API_KEY /shared 2>/dev/null || true)
  if [ -n "$V" ]; then export MAILCHIMP_API_KEY="$V"; echo "[entrypoint] pulled MAILCHIMP_API_KEY from Infisical"; else echo "[entrypoint] Infisical unavailable -- env fallback" >&2; fi
fi
if [ -z "${MAILCHIMP_API_KEY:-}" ]; then
  echo "[entrypoint] WARNING: MAILCHIMP_API_KEY is not set — Mailchimp tools will fail auth" >&2
fi
# Bridge the stdio Mailchimp MCP to streamable-HTTP at /mcp on :8080. The child inherits MAILCHIMP_API_KEY.
exec supergateway --stdio "mailchimp-mcp" --outputTransport streamableHttp --port 8080 --host 0.0.0.0
