#!/usr/bin/env sh
# Pull the Mailchimp API key from Infisical /shared. Fail-safe by design: any failure falls back to
# whatever is in the env and the container still boots (CLAUDE.md §6, "Integration = PULL, fail-safe").
#
# The three failure branches below are deliberately distinct. A single "Infisical unavailable"
# message for every empty result cost a full misdiagnosis on 2026-09-07: the sidecar had been wired
# to Infisical correctly since day one, and the only thing missing was the secret itself. Never
# collapse "cannot reach the store" and "the store has no such secret" into one line again.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  V=$(python3 /app/infisical_fetch.py MAILCHIMP_API_KEY /shared 2>/dev/null || true)
  if [ -n "$V" ]; then
    export MAILCHIMP_API_KEY="$V"
    echo "[entrypoint] pulled MAILCHIMP_API_KEY from Infisical ${INFISICAL_ENV:-prod}:/shared"
  elif python3 /app/infisical_probe.py; then
    echo "[entrypoint] Infisical reachable and machine identity accepted, but MAILCHIMP_API_KEY is not staged at ${INFISICAL_ENV:-prod}:/shared -- stage it there, then redeploy this service" >&2
  else
    echo "[entrypoint] cannot reach or authenticate to Infisical at ${INFISICAL_API_URL:-<default>} -- env fallback" >&2
  fi
else
  echo "[entrypoint] INFISICAL_CLIENT_ID is empty -- skipping the Infisical pull entirely, using env" >&2
fi
if [ -z "${MAILCHIMP_API_KEY:-}" ]; then
  echo "[entrypoint] WARNING: MAILCHIMP_API_KEY is not set — Mailchimp tools will fail auth" >&2
fi
# Bridge the stdio Mailchimp MCP to streamable-HTTP at /mcp on :8080. The child inherits MAILCHIMP_API_KEY.
exec supergateway --stdio "mailchimp-mcp" --outputTransport streamableHttp --port 8080 --host 0.0.0.0
