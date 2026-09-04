#!/bin/sh
# The fleet's shared ISOLATED ssh-sandbox for terminal + code_execution (Webster PM + all specialists:
# writer, seo, producer, publisher, researcher).
#
# The agents (esp. the public, web-reading PM) must never run code on prod-2 directly (prompt-injection
# -> RCE). Instead TERMINAL_ENV=ssh routes terminal/code_execution to a separate, locked-down container
# that has: NO prod secrets in its env, NO route to the coolify/prod network (LiteLLM, MCP sidecars,
# Postgres), NO host mounts, NO docker socket, cpu/mem/pids caps, no-new-privileges. Internet egress
# is allowed (so it is useful). Its ONE real capability is `rclone` -> R2 bucket 'fleet-clients' (step 5):
# the fleet unzips a brand and copies it to r2:fleet-clients/clients/<slug>/... — the only path that can
# move a full binary brand (templates/fonts/logos) that the `spaces_*` tools can't. The R2 token is
# bucket-scoped + IP-filtered, so exfil is bounded to that one bucket + task data. `file` is deliberately
# NOT enabled (its ssh-backend path handling is container-only in Hermes and could touch the agent's own
# container).
#
# RE-RUN THIS AFTER ANY WEBSTER REDEPLOY. The image, network and sandbox container survive, but the
# `docker network connect` that lets Webster reach the sandbox is NOT Coolify-managed and does not
# survive a container RECREATION (a Coolify `deploy force:true`). A plain `docker restart` is fine.
# BOTH hermes containers run agent turns and so BOTH need this wiring: hermes-agent (`gateway run` —
# cron + kanban dispatch) and hermes-serve (`serve` — the backend Hermes Desktop clients connect to).
# Skipping it does not error: terminal/code_execution silently fall back to running INSIDE the
# container instead of the sandbox (see step 4c).
# The ssh key (/home/hermes/.hermes/.ssh/sandbox_key) and config.yaml live in the persistent volume
# and DO survive. The one-time key install + config wiring are NOT redone here (see NOTE below).
set -eu
C=${HERMES_CONTAINER:-hermes-agent-zhvjhbo5752ovx1nl2rk9v30}
SANDBOX=hermes-sandbox
IMG=${SANDBOX}:2   # :2 adds chromium + /usr/local/bin/render-card (HTML->PNG) for the producer's card renders
HERE=$(cd "$(dirname "$0")" && pwd)

# 1) image
docker image inspect ${IMG} >/dev/null 2>&1 || (cd "$HERE/sandbox" && docker build -t ${IMG} .)
# 2) dedicated network (internet NAT + DNS; NOT the coolify/prod network)
docker network inspect "$SANDBOX" >/dev/null 2>&1 || docker network create --driver bridge "$SANDBOX"
# 3) sandbox container (locked down)
docker ps --format '{{.Names}}' | grep -qx "$SANDBOX" || docker run -d --name "$SANDBOX" --restart unless-stopped \
  --network "$SANDBOX" --hostname "$SANDBOX" \
  -e TZ=Australia/Melbourne \
  --memory 2g --cpus 1 --pids-limit 512 --security-opt no-new-privileges ${IMG}
# 4) bridge Webster -> sandbox (idempotent). THIS is the step to re-run after a redeploy.
docker network connect "$SANDBOX" "$C" 2>/dev/null || echo "  (agent already connected)"

# 4b) SERVE-BACKEND ACCESS TO THE SANDBOX — put the SANDBOX on the hermes SERVICE network ($SVC_NET)
# that BOTH hermes containers are already on, rather than adding $SANDBOX to them as a third network.
# HARD WARNING, kept because it cost an outage: NEVER attach a Traefik-routed container to the
# sandbox's own network. Such a container has no `traefik.docker.network` label, so the extra network
# leaves Traefik unable to pick the backend and it DROPS THE PUBLIC ROUTE (webui outage 2026-08-26).
# That specific trap is moot now that hermes-serve is traefik.enable=false / tailnet-only, but the
# $SVC_NET approach is kept anyway: one connect reaches both containers with no per-container change.
# That network holds only the hermes containers + the coolify-proxy (NOT litellm/spaces/postgres), so
# the sandbox stays isolated from the prod services; it also has internet egress (Internal=false).
SVC_NET=${HERMES_SERVICE_NET:-zhvjhbo5752ovx1nl2rk9v30}
Sc=${HERMES_SERVE_CONTAINER:-hermes-serve-zhvjhbo5752ovx1nl2rk9v30}
docker network connect "$SVC_NET" "$SANDBOX" 2>/dev/null || echo "  (sandbox already on service net)"
# NO symlink step. hermes-serve runs the SAME image as the agent and mounts hermes-home at the SAME
# absolute path (/home/hermes/.hermes), so config.yaml's absolute ssh_key path resolves as-is. (The
# removed hermes-webui ran as `hermeswebui` off /home/hermeswebui and needed a /home/hermes symlink.)

# 4c) REFRESH the sandbox host key in the known_hosts the terminal backend ACTUALLY uses. A RECREATE
# regenerates the sandbox's ssh host key, so a pinned entry goes stale and strict host-key checking fails
# with "REMOTE HOST IDENTIFICATION HAS CHANGED" (the terminal then silently falls back to LOCAL exec in
# the agent container — the tool "works" but runs in the wrong place). The config `terminal:` block sets
# ssh_key but NO known_hosts path, so ssh uses each runtime user's DEFAULT ~/.ssh/known_hosts. Both
# containers run the same image as `hermes` with HOME=/opt/data, and /opt/data is an ANONYMOUS volume
# (the image's own VOLUME) — a recreate gets a fresh empty one, so known_hosts is lost in BOTH. Refresh
# both (NOT the .hermes copy — nothing reads that).
# Drop the stale entry + re-scan the fresh key.
docker exec -u hermes "$C" sh -c 'KH=/opt/data/.ssh/known_hosts; mkdir -p /opt/data/.ssh; \
  ssh-keygen -f "$KH" -R hermes-sandbox >/dev/null 2>&1; ssh-keyscan -H hermes-sandbox >> "$KH" 2>/dev/null' \
  && echo "  agent known_hosts refreshed" || echo "  agent known_hosts refresh FAILED"
docker exec -u hermes "$Sc" sh -c 'KH=/opt/data/.ssh/known_hosts; mkdir -p /opt/data/.ssh; \
  ssh-keygen -f "$KH" -R hermes-sandbox >/dev/null 2>&1; ssh-keyscan -H hermes-sandbox >> "$KH" 2>/dev/null' \
  && echo "  serve known_hosts refreshed" || echo "  serve known_hosts refresh FAILED"

# 5) rclone -> R2 (the fleet asset store 'fleet-clients'). This is the sandbox's ONE real capability:
# agents unzip a brand and `rclone copy` it to r2:fleet-clients/clients/<slug>/... The token is scoped
# to that one bucket + IP-filtered to prod-2, so the sandbox stays blind to everything else. NOT baked
# into the image (would put the secret in git). Survives a plain restart (it's in the container fs);
# re-run this after a RECREATE. Provide the creds file (lines R2_ACCESS_KEY_ID=.. / R2_SECRET_ACCESS_KEY=..).
R2_CREDS_FILE=${R2_CREDS_FILE:-$HOME/.r2creds}  # stage this here (600) right before a RECREATE run; not kept long-term
R2_ENDPOINT=${R2_ENDPOINT:-https://023cf06b87e6b0abe3065ec0a8f79b79.r2.cloudflarestorage.com}
if [ -f "$R2_CREDS_FILE" ]; then
  docker cp "$R2_CREDS_FILE" "$SANDBOX":/tmp/r2creds
  docker exec -e R2E="$R2_ENDPOINT" "$SANDBOX" sh -c '. /tmp/r2creds; mkdir -p /home/sandbox/.config/rclone; \
    printf "[r2]\ntype = s3\nprovider = Cloudflare\naccess_key_id = %s\nsecret_access_key = %s\nendpoint = %s\nregion = auto\nacl = private\nno_check_bucket = true\n" \
      "$R2_ACCESS_KEY_ID" "$R2_SECRET_ACCESS_KEY" "$R2E" > /home/sandbox/.config/rclone/rclone.conf; \
    chown -R sandbox:sandbox /home/sandbox/.config; chmod 600 /home/sandbox/.config/rclone/rclone.conf; rm -f /tmp/r2creds'
  docker exec "$SANDBOX" su - sandbox -c 'rclone lsd r2:fleet-clients >/dev/null 2>&1 && echo "  rclone->R2 ok" || echo "  rclone->R2 FAILED"'
else
  echo "  (no $R2_CREDS_FILE on this host — skipping rclone/R2 provisioning; sandbox lacks R2 until provided)"
fi

echo "hermes-agent networks: $(docker inspect "$C" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
echo "hermes-serve networks: $(docker inspect "$Sc" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
echo "Isolation self-check (all prod services should be unreachable):"
docker exec "$SANDBOX" sh -c 'for t in litellm-v10up2yg1cwxo0k1ks9j2qro:4000; do timeout 3 curl -s -o /dev/null http://$t/ 2>/dev/null && echo "  REACHED $t (BAD)" || echo "  unreachable $t (good)"; done; echo -n "  prod secrets in sandbox: "; env | grep -icE "CLICKUP|EMAIL|ANTHROPIC|OPENAI|_KEY=|PASSWORD" || true'

# NOTE — one-time wiring (already done 2026-08-26; redo only on a fresh Hermes volume):
#   a) private key: docker cp sandbox_key -> $C:/home/hermes/.hermes/.ssh/sandbox_key (chown hermes, 600)
#      known_hosts: sandbox host key -> $C:/home/hermes/.hermes/.ssh/known_hosts
#   b) config.yaml: terminal:{backend:ssh, ssh_host:hermes-sandbox, ssh_user:sandbox, ssh_port:22,
#      ssh_key:/home/hermes/.hermes/.ssh/sandbox_key} + add terminal,code_execution to
#      platform_toolsets.{cli,cron,api_server}  # (serve reads the same config). Then `docker restart $C`.
#   c) specialists (2026-08-27): terminal + code_execution added to every platform_toolsets list in
#      profiles/{writer,seo,producer,publisher,researcher}/config.yaml so they share this sandbox
#      (bulk uploads / code exec). Same ssh backend + key resolve for worker turns. `docker restart $C`.
