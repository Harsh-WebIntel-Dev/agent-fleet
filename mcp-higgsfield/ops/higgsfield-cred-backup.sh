#!/usr/bin/env bash
# Copy the live Higgsfield credential OFF the Docker volume, onto the host.
#
# WHY THIS EXISTS (interim, and it should be temporary)
# The proper home for the rotated credential is Infisical, but the `fleet-hermes` machine identity is
# read-only: verified 2026-09-10, an idempotent self-write returns
#   403 {"message":"You are not allowed to edit on secrets","error":"PermissionDenied"}
# Until an admin grants that identity `secrets:edit` on `/shared`, the rotated credential exists ONLY
# on the `..._higgsfield-config` Docker volume — a single point of loss, which is exactly how the
# 2026-09-09 outage became unrecoverable.
#
# This script is the best durable option available WITHOUT that grant. The host filesystem survives
# container recreate, image rebuild and `docker volume rm`; it is the same durability tier as
# /home/harsh/litellm-cfg/, which CLAUDE.md 6 already treats as authoritative live config.
#
# Deliberately NOT R2/mcp-spaces: that bucket is readable by every agent via `spaces_read`, so a live
# refresh_token there would be exposed to the whole fleet.
#
# Writes only when the credential actually changed, keeps a bounded history, and never logs a token.
set -uo pipefail

DEST="${HIGGSFIELD_CRED_BACKUP_DIR:-/home/harsh/higgsfield-cred}"
KEEP="${HIGGSFIELD_CRED_BACKUP_KEEP:-10}"
SRC_PATH=/root/.config/higgsfield/credentials.json
LOG="$DEST/backup.log"

mkdir -p "$DEST"
chmod 700 "$DEST"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG"; }

CONTAINER="$(docker ps --format '{{.Names}}' | grep '^mcp-higgsfield' | head -1)"
if [ -z "$CONTAINER" ]; then
  log "SKIP no running mcp-higgsfield container"
  exit 0
fi

TMP="$(mktemp "$DEST/.incoming-XXXXXX")"
trap 'rm -f "$TMP"' EXIT
if ! docker cp "$CONTAINER:$SRC_PATH" "$TMP" 2>/dev/null; then
  log "SKIP credentials.json not present in container"
  exit 0
fi

# Only keep bundles that parse and carry the refresh_token we actually depend on.
if ! EXPIRES="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if not isinstance(d,dict) or not d.get("refresh_token"):
    sys.exit(1)
print(d.get("expires_at",""))
' "$TMP" 2>/dev/null)"; then
  log "SKIP fetched file is not a usable bundle"
  exit 0
fi

NEW_SHA="$(sha256sum "$TMP" | cut -d" " -f1)"
OLD_SHA=""
[ -f "$DEST/latest.json" ] && OLD_SHA="$(sha256sum "$DEST/latest.json" | cut -d" " -f1)"

if [ "$NEW_SHA" = "$OLD_SHA" ]; then
  exit 0  # unchanged; stay quiet so the log only records real rotations
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
install -m 600 "$TMP" "$DEST/credentials-$STAMP.json"
install -m 600 "$TMP" "$DEST/latest.json"
log "STORED generation $STAMP expires_at=$EXPIRES sha=${NEW_SHA:0:12}"

# Bounded history: keep the newest $KEEP generations.
ls -1t "$DEST"/credentials-*.json 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old" && log "PRUNED $(basename "$old")"
done
