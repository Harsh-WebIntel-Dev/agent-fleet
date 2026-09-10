#!/usr/bin/env bash
# Boot-path tests for entrypoint.sh — the precedence rules that keep a rotating OAuth bundle alive
# across restarts, plus the stale-lock clearing added after the 2026-09-09 credential loss.
#
# Runs the real entrypoint with only the final `exec` stubbed out, against a throwaway config dir.
# No Docker, no network. Usage: ./test_entrypoint.sh
set -uo pipefail
cd "$(dirname "$0")"

PASS=0
FAIL=0
GOOD='{"auth_version":2,"access_token":"a","refresh_token":"live-token","expires_at":4102444800}'
SEED='{"auth_version":2,"access_token":"a","refresh_token":"seed-token","expires_at":4102444800}'
STALE_SEED='{"auth_version":2,"access_token":"a","refresh_token":"seed-token","expires_at":1787775122}'

# Point the entrypoint at the local bootstrap.py and stub the final exec, so the REAL boot script
# runs end to end without a container.
STUB="$(mktemp -d)/entrypoint-under-test.sh"
sed -e "s#^exec python3 /app/server.py\$#echo BOOT_COMPLETE#" \
    -e "s#python3 /app/bootstrap.py#python3 $PWD/bootstrap.py#" \
    entrypoint.sh > "$STUB"
chmod +x "$STUB"

# run <config-dir> [seed-json] -> prints combined output
run_boot() {
  local dir="$1" seed="${2-}"
  if [ -n "$seed" ]; then
    HIGGSFIELD_CONFIG_DIR="$dir" HIGGSFIELD_CREDENTIALS_JSON="$seed" "$STUB" 2>&1
  else
    HIGGSFIELD_CONFIG_DIR="$dir" "$STUB" 2>&1
  fi
}

check() {
  local name="$1" cond="$2"
  if [ "$cond" = "1" ]; then
    PASS=$((PASS + 1)); echo "  ok   - $name"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL - $name"
  fi
}

refresh_token_of() {
  python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("refresh_token",""))' "$1" 2>/dev/null
}

echo "1. a usable live bundle is left untouched"
D="$(mktemp -d)"; printf '%s' "$GOOD" > "$D/credentials.json"
OUT="$(run_boot "$D" "$SEED")"
check "keeps the live token (does not clobber with the seed)" "$([ "$(refresh_token_of "$D/credentials.json")" = "live-token" ] && echo 1)"
check "says it left it untouched" "$(echo "$OUT" | grep -q 'kept credential from volume' && echo 1)"
check "boot completes" "$(echo "$OUT" | grep -q BOOT_COMPLETE && echo 1)"

echo "2. a missing bundle is restored from the credguard snapshot, NOT the older seed"
D="$(mktemp -d)"; printf '%s' "$GOOD" > "$D/credentials.json.prev"
OUT="$(run_boot "$D" "$SEED")"
check "restores the rotated snapshot" "$([ "$(refresh_token_of "$D/credentials.json")" = "live-token" ] && echo 1)"
check "reports the snapshot restore" "$(echo "$OUT" | grep -q 'from snapshot' && echo 1)"

echo "3. with neither live bundle nor snapshot, the seed is installed"
D="$(mktemp -d)"
OUT="$(run_boot "$D" "$SEED")"
check "installs the seed" "$([ "$(refresh_token_of "$D/credentials.json")" = "seed-token" ] && echo 1)"
check "file is owner-only" "$([ "$(stat -c%a "$D/credentials.json")" = "600" ] && echo 1)"

echo "4. a zero-byte credentials.json (the 2026-09-09 shape) never survives boot"
D="$(mktemp -d)"; : > "$D/credentials.json"; printf '%s' "$GOOD" > "$D/credentials.json.prev"
OUT="$(run_boot "$D" "$SEED")"
check "empty stub replaced from snapshot" "$([ "$(refresh_token_of "$D/credentials.json")" = "live-token" ] && echo 1)"
check "no zero-byte file left" "$([ -s "$D/credentials.json" ] && echo 1)"

echo "5. an orphaned lock is cleared on boot"
D="$(mktemp -d)"; printf '%s' "$GOOD" > "$D/credentials.json"; : > "$D/credentials.json.lock"
OUT="$(run_boot "$D" "$SEED")"
check "lock removed" "$([ ! -e "$D/credentials.json.lock" ] && echo 1)"
check "clearing is logged" "$(echo "$OUT" | grep -q 'cleared orphaned lock' && echo 1)"

echo "6. an unusable seed is refused rather than installed"
D="$(mktemp -d)"
OUT="$(run_boot "$D" '{"access_token":"a"}')"   # no refresh_token
check "does not install a bundle without refresh_token" "$([ ! -e "$D/credentials.json" ] && echo 1)"
check "warns that auth is absent" "$(echo "$OUT" | grep -q 'no usable credential' && echo 1)"
check "boot still completes (fail-safe)" "$(echo "$OUT" | grep -q BOOT_COMPLETE && echo 1)"

echo "7. a stale seed is installed but loudly flagged as probably superseded"
D="$(mktemp -d)"
OUT="$(run_boot "$D" "$STALE_SEED")"
check "seed installed" "$([ -s "$D/credentials.json" ] && echo 1)"
check "warns the seed is stale" "$(echo "$OUT" | grep -q 'expired' && echo 1)"

echo "8. a FRESH staged seed beats a dead-but-parseable volume bundle (the 2026-09-10 miss)"
D="$(mktemp -d)"; printf '%s' "$STALE_SEED" > "$D/credentials.json"
OUT="$(run_boot "$D" "$GOOD")"
check "installs the fresh seed instead of keeping the dead bundle" "$([ "$(refresh_token_of "$D/credentials.json")" = "live-token" ] && echo 1)"
check "reports installing from the seed" "$(echo "$OUT" | grep -q 'installed credential from seed' && echo 1)"

echo "9. boot clears the VENDOR lock but leaves our flock file alone"
D="$(mktemp -d)"; printf '%s' "$GOOD" > "$D/credentials.json"
: > "$D/credentials.json.lock"; echo "pid=999 since=0" > "$D/refresh.lock"
OUT="$(run_boot "$D" "$SEED")"
check "vendor lock removed" "$([ ! -e "$D/credentials.json.lock" ] && echo 1)"
check "our flock file preserved (removing it would allow a 2nd refresher)" "$([ -e "$D/refresh.lock" ] && echo 1)"

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
