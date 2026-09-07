#!/usr/bin/env bash
# Insert the turn-1 "confirm your tools are mounted" guard into the five live specialist SOULs.
#
# Follows CLAUDE.md §13: reads the LIVE file, keeps a dated .bak-*, edits, prints a diff.
# SOULs are read per-turn, so this takes effect on the NEXT dispatch — no restart, no cache clear.
#
#   ./apply-toolset-guard.sh          # dry run: print the diff, change nothing
#   APPLY=1 ./apply-toolset-guard.sh  # write, after taking .bak-toolsetguard-<date>
#
# Idempotent: a SOUL that already contains the guard is skipped.
set -euo pipefail

STAMP="bak-toolsetguard-$(date +%Y%m%d)"
MARKER="## FIRST — confirm your tools are mounted"
SNIPPET="$(dirname "$0")/../souls/_toolset-guard.snippet.md"
PROFILES="seo researcher writer producer publisher"
APPLY="${APPLY:-0}"

C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
[ -n "$C" ] || { echo "no hermes-agent container found" >&2; exit 1; }

# Strip the HTML review comment: it is guidance for humans reading the repo, not prompt text.
BODY=$(sed '/^<!--$/,/^-->$/d' "$SNIPPET" | sed '/./,$!d')

for p in $PROFILES; do
  SOUL="/home/hermes/.hermes/profiles/$p/SOUL.md"
  echo "== $p"

  if docker exec -u hermes "$C" grep -qF "$MARKER" "$SOUL" 2>/dev/null; then
    echo "   already guarded — skipped"
    continue
  fi

  # Insert before the SOUL's first '## ' heading, i.e. after the title + role paragraph.
  NEW=$(docker exec -u hermes "$C" cat "$SOUL" \
        | awk -v guard="$BODY" '
            !done && /^## / { print guard "\n"; done=1 }
            { print }
            END { if (!done) print "\n" guard }')

  if [ "$APPLY" != "1" ]; then
    printf '%s\n' "$NEW" | docker exec -i -u hermes "$C" sh -c "diff -u '$SOUL' - || true" | head -40
    echo "   (dry run — set APPLY=1 to write)"
    continue
  fi

  docker exec -u hermes "$C" cp -p "$SOUL" "$SOUL.$STAMP"
  printf '%s\n' "$NEW" | docker exec -i -u hermes "$C" sh -c "cat > '$SOUL'"
  echo "   wrote (backup $(basename "$SOUL").$STAMP)"
  docker exec -u hermes "$C" sh -c "diff -u '$SOUL.$STAMP' '$SOUL' || true" | head -40
done

echo
echo "SOULs are read per-turn — no restart needed. Verify with:"
echo "  docker exec -u hermes $C grep -c 'TOOLSET-NOT-MOUNTED' /home/hermes/.hermes/profiles/*/SOUL.md"
