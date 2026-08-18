#!/bin/sh
# Mount DigitalOcean Spaces (client assets) into the agent workspace.
#
# Runs via OPENCLAW_DOCKER_INIT_SCRIPT — a supported entrypoint hook, so no custom image is
# needed. rclone itself arrives through OPENCLAW_DOCKER_APT_PACKAGES.
#
# WHY A MOUNT AND NOT A TOOL: agents then use ordinary file operations, and isolation is enforced
# by WHAT IS MOUNTED rather than by trusting the model to stay inside a prefix. An agent cannot
# reach a client whose prefix was never mounted.
#
# Bucket layout (per the plan: one shared bucket, per-client prefixes):
#   s3://wi-ai/clients/<slug>/...  ->  /data/workspace/clients/<slug>/...
set -eu

MOUNT_POINT="${SPACES_MOUNT_POINT:-/data/workspace/clients}"
REMOTE_PREFIX="${SPACES_REMOTE_PREFIX:-clients}"

if [ -z "${DO_SPACES_ACCESS_KEY:-}" ] || [ -z "${DO_SPACES_SECRET_KEY:-}" ]; then
  echo "[spaces] DO_SPACES_ACCESS_KEY/SECRET_KEY not set — skipping mount (agents will see an empty dir)"
  exit 0
fi

if ! command -v rclone >/dev/null 2>&1; then
  echo "[spaces] ERROR: rclone missing. Add 'rclone fuse3' to OPENCLAW_DOCKER_APT_PACKAGES."
  exit 0   # non-fatal: never block the gateway from starting over storage
fi

mkdir -p /root/.config/rclone "$MOUNT_POINT"
cat > /root/.config/rclone/rclone.conf <<EOF
[spaces]
type = s3
provider = DigitalOcean
access_key_id = ${DO_SPACES_ACCESS_KEY}
secret_access_key = ${DO_SPACES_SECRET_KEY}
endpoint = ${DO_SPACES_ENDPOINT:-syd1.digitaloceanspaces.com}
acl = private
EOF
chmod 600 /root/.config/rclone/rclone.conf

# Already mounted (container restart without teardown)? Leave it alone.
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
  echo "[spaces] $MOUNT_POINT already mounted"
  exit 0
fi

echo "[spaces] mounting s3://${DO_SPACES_BUCKET:-wi-ai}/${REMOTE_PREFIX} -> $MOUNT_POINT"
# --vfs-cache-mode writes: needed for normal write/append semantics over object storage.
# --daemon so the entrypoint continues to the gateway.
rclone mount "spaces:${DO_SPACES_BUCKET:-wi-ai}/${REMOTE_PREFIX}" "$MOUNT_POINT" \
  --vfs-cache-mode writes \
  --vfs-cache-max-age 12h \
  --dir-cache-time 1m \
  --allow-other \
  --umask 022 \
  --log-level INFO \
  --log-file /data/.openclaw/rclone-spaces.log \
  --daemon || { echo "[spaces] WARNING: mount failed — see /data/.openclaw/rclone-spaces.log"; exit 0; }

sleep 3
if mountpoint -q "$MOUNT_POINT"; then
  echo "[spaces] mounted OK: $(ls -1 "$MOUNT_POINT" 2>/dev/null | wc -l) client prefix(es) visible"
else
  echo "[spaces] WARNING: mount did not settle; agents will see an empty directory"
fi
