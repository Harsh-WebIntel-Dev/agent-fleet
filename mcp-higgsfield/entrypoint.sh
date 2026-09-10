#!/usr/bin/env bash
# Bring up the Higgsfield credential file, then hand it to the CLI which owns refresh from there.
#
# Higgsfield auth is a ROTATING OAuth pair (Clerk: 2h access token, new refresh_token on every
# exchange). So the staged seed is only correct for ~2h after capture, and the LIVE bundle exists
# only on this volume. Two consequences drive the logic below:
#   1. We never clobber a newer on-volume bundle with the (older) seed.
#   2. If the live bundle is gone we prefer credguard's snapshot (rotated, newer) over the seed.
#
# On 2026-09-09 a failed refresh unlinked credentials.json and left an orphaned zero-byte lock that
# blocked every retry, so boot also clears stale locks: at boot nothing can legitimately hold one.
set -euo pipefail
CFG="${HIGGSFIELD_CONFIG_DIR:-${HOME:-/root}/.config/higgsfield}"
mkdir -p "$CFG"

# --- 0. Clear locks orphaned by a crashed refresh -------------------------------------------------
for lock in "$CFG"/*.lock; do
  [ -e "$lock" ] || continue
  echo "[entrypoint] clearing orphaned lock $(basename "$lock") ($(stat -c%s "$lock") bytes, mtime $(stat -c%y "$lock"))" >&2
  rm -f "$lock"
done

# A bundle is only usable if it parses AND carries the refresh_token we depend on.
_usable() {
  [ -s "$1" ] || return 1
  python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
sys.exit(0 if isinstance(d,dict) and d.get("refresh_token") else 1)' "$1" 2>/dev/null
}

# Write via temp+rename so an interrupted boot can never leave a zero-byte credentials.json.
_atomic_install() {
  local dest="$1" tmp
  tmp="$(mktemp "$(dirname "$dest")/.entrypoint-XXXXXX")"
  cat > "$tmp"
  chmod 600 "$tmp"
  if _usable "$tmp"; then
    mv -f "$tmp" "$dest"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# --- 1. Keep a usable live bundle exactly as it is ------------------------------------------------
if _usable "$CFG/credentials.json"; then
  echo "[entrypoint] existing credentials.json is usable — leaving it untouched"
else
  rm -f "$CFG/credentials.json"  # drop an empty/corrupt stub so the fallbacks can run

  # --- 2. Prefer credguard's snapshot: it is the rotated bundle, newer than any staged seed -------
  if _usable "$CFG/credentials.json.prev"; then
    if _atomic_install "$CFG/credentials.json" < "$CFG/credentials.json.prev"; then
      echo "[entrypoint] restored credentials.json from credguard snapshot (newer than the seed)"
    fi
  fi
fi

# --- 3. Last resort: the staged seed -------------------------------------------------------------
# Pull from Infisical (the fleet's source of truth) when reachable; fall back to the Coolify env if
# Infisical is down — fail-safe, so an Infisical outage can never brick this service at boot.
if ! _usable "$CFG/credentials.json"; then
  if [ -n "${INFISICAL_CLIENT_ID:-}" ]; then
    if _pulled="$(python3 /app/infisical_fetch.py HIGGSFIELD_CREDENTIALS_JSON /shared 2>/dev/null)" && [ -n "$_pulled" ]; then
      HIGGSFIELD_CREDENTIALS_JSON="$_pulled"
      echo "[entrypoint] pulled HIGGSFIELD_CREDENTIALS_JSON from Infisical"
    else
      echo "[entrypoint] Infisical unavailable — using env fallback" >&2
    fi
  fi
  if [ -n "${HIGGSFIELD_CREDENTIALS_JSON:-}" ]; then
    if printf '%s' "$HIGGSFIELD_CREDENTIALS_JSON" | _atomic_install "$CFG/credentials.json"; then
      echo "[entrypoint] seeded credentials.json from HIGGSFIELD_CREDENTIALS_JSON"
      # A rotating pair means the seed is usually already superseded. Say so, so a stale seed is not
      # mistaken for working auth.
      python3 -c 'import json,os,sys,datetime
p=sys.argv[1]
try: exp=json.load(open(p)).get("expires_at")
except Exception: sys.exit(0)
if not isinstance(exp,(int,float)): sys.exit(0)
age_h=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromtimestamp(exp,datetime.timezone.utc)).total_seconds()/3600
if age_h > 2:
    print(f"[entrypoint] WARNING: seeded access token expired {age_h:.0f}h ago. Refresh tokens rotate, "
          f"so this seed is probably already superseded and the next call may fail with invalid_grant. "
          f"If it does, a human must re-run `higgsfield auth login` in a browser and re-stage the secret.")' \
        "$CFG/credentials.json" >&2
    else
      echo "[entrypoint] ERROR: HIGGSFIELD_CREDENTIALS_JSON is not a usable bundle — not installing it" >&2
    fi

  fi
fi

if ! _usable "$CFG/credentials.json"; then
  echo "[entrypoint] WARNING: no usable credentials.json — Higgsfield tools will fail until a human runs" >&2
  echo "[entrypoint]          'higgsfield auth login' (needs a browser) and re-stages HIGGSFIELD_CREDENTIALS_JSON." >&2
fi

exec python3 /app/server.py
