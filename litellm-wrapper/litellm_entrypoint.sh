#!/bin/sh
# Fail-safe wrapper: pull litellm's external keys from Infisical when reachable, else fall back to the
# Coolify env. Then hand off to litellm's original entrypoint unchanged. A pull failure NEVER blocks boot.
if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
  for pair in \
    "DEEPSEEK_API_KEY=DEEPSEEK_API_KEY" \
    "DO_INFERENCE_KEY=DO_INFERENCE_KEY" \
    "SEMRUSH_API_KEY=SEMRUSH_API_KEY_HEADER" \
    "POSTIZ_MCP_TOKEN=POSTIZ_MCP_TOKEN" \
    "CLICKUP_MCP_TOKEN=CLICKUP_MCP_TOKEN" \
    "LITELLM_MASTER_KEY=LITELLM_MASTER_KEY"; do
    ikey=${pair%%=*}; evar=${pair##*=}
    val=$(python3 /app/infisical_fetch.py "$ikey" /shared 2>/dev/null)
    [ -n "$val" ] && export "$evar=$val"
  done
  echo "[litellm-wrapper] Infisical pull attempted (fail-safe; env fallback if unreachable)"
fi
# Hand off to the command the compose passes (e.g. litellm --config /app/config.yaml --port 4000).
exec "$@"
