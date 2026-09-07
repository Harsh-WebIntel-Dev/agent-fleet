#!/usr/bin/env bash
# Widen the two Hermes bounds that decide whether a kanban worker gets its MCP tools.
#
#   mcp_single_query_discovery_timeout: 15 -> 120   (how long agent-build waits for discovery)
#   mcp_servers.pm_comms.connect_timeout: 60 -> 90  (how long one server gets to connect+list)
#
# WHY THESE NUMBERS
#   Observed aggregated tools/list latency on 2026-09-07: 6-50 s (was 2.6-7.7 s before mcp-mailchimp
#   joined the aggregate). LiteLLM caps each upstream at 30 s, so a realistic worst case is ~35 s and
#   the observed worst was 50.4 s. 90 s gives real headroom over both.
#
#   The ORDER matters. discovery_timeout MUST exceed connect_timeout. If it doesn't, the agent can be
#   built while a connect is still in flight and then snapshot an empty tool registry even though the
#   connect succeeds moments later — which is exactly how t_e0a2bd40 lost by 2.5 s. With 120 > 90 the
#   build always waits for a definitive outcome: tools registered, or a connect that has actually
#   failed. Raising connect_timeout alone would just move the failure.
#
# COST: none on healthy dispatches. thread.join(timeout=N) returns the INSTANT discovery completes
# (hermes_cli/mcp_startup.py:180-194), so a fast connect still pays ~0 s. The worst case is +90 s of
# worker startup when the endpoint is genuinely broken; the claim TTL is 900 s, so no reclaim risk.
#
# RESTART: NOT required for kanban workers — each is a fresh `hermes` subprocess that reads its
# profile config.yaml at startup, so this applies to the NEXT dispatch. The root config.yaml is also
# patched for the long-lived gateway, which picks it up whenever it next restarts. CLAUDE.md §13's
# "clear both caches + restart both containers" does NOT apply: that rule covers changes to the SET
# of MCP tools/servers, and mcp_schema_cache.json is inert for pm_comms anyway (lazy-only path, and
# its entries carry ttl_ms: 0 so get_cached_entry always returns None).
#
#   ./apply-mcp-timeouts.sh          # dry run
#   APPLY=1 ./apply-mcp-timeouts.sh  # write, after taking .bak-mcptimeouts-<date>
#
# Line-insertion only — never a YAML round-trip. The profile configs use YAML anchors (&id001/*id001)
# that a load/dump cycle would silently expand and destroy.
set -euo pipefail

APPLY="${APPLY:-0}"
C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
[ -n "$C" ] || { echo "no hermes-agent container found" >&2; exit 1; }

PY=$(cat <<'PYEOF'
import os, re, shutil, sys

STAMP = "bak-mcptimeouts-20260907"
APPLY = os.environ.get("APPLY") == "1"
PATHS = ["/home/hermes/.hermes/profiles/%s/config.yaml" % p
         for p in ("producer", "publisher", "writer", "seo", "researcher")]
PATHS.append("/home/hermes/.hermes/config.yaml")

rc = 0
for path in PATHS:
    if not os.path.exists(path):
        print("== %s\n     MISSING — skipped" % path); rc = 1; continue
    src = open(path).read()
    lines = src.splitlines(True)
    notes = []

    if re.search(r"(?m)^mcp_single_query_discovery_timeout:", src):
        notes.append("= discovery bound already set")
    else:
        for i, ln in enumerate(lines):
            if ln.startswith("mcp_servers:"):
                lines.insert(i, "mcp_single_query_discovery_timeout: 120\n"); break
        else:
            lines.append("mcp_single_query_discovery_timeout: 120\n")
        notes.append("+ mcp_single_query_discovery_timeout: 120")

    if re.search(r"(?m)^    connect_timeout:", "".join(lines)):
        notes.append("= connect_timeout already set")
    else:
        out, ins, in_mcp = [], False, False
        for ln in lines:
            out.append(ln)
            if ln.startswith("mcp_servers:"):
                in_mcp = True
            elif in_mcp and re.match(r"^\S", ln):
                in_mcp = False
            if in_mcp and not ins and re.match(r"^    timeout:\s*\d+", ln):
                out.append("    connect_timeout: 90\n"); ins = True
        if ins:
            lines = out; notes.append("+   connect_timeout: 90  (pm_comms)")
        else:
            notes.append("! no pm_comms 'timeout:' line found — connect_timeout SKIPPED"); rc = 1

    new = "".join(lines)
    print("== %s" % path)
    for n in notes:
        print("     %s" % n)
    if new == src:
        print("     no change")
    elif APPLY:
        shutil.copy2(path, path + "." + STAMP)
        open(path, "w").write(new)
        print("     WROTE (backup .%s)" % STAMP)
    else:
        print("     (dry run — set APPLY=1 to write)")
sys.exit(rc)
PYEOF
)

printf '%s' "$PY" | docker exec -i -u hermes -e APPLY="$APPLY" "$C" /opt/hermes/.venv/bin/python -

echo
echo "Verify:"
echo "  docker exec -u hermes $C grep -H -A1 -E 'discovery_timeout|connect_timeout' \\"
echo "    /home/hermes/.hermes/config.yaml /home/hermes/.hermes/profiles/*/config.yaml"
echo "No restart needed for workers — next dispatch picks it up."
