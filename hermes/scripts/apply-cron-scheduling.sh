#!/usr/bin/env bash
# Give cron-spawned agents the `cronjob` toolset in the MAIN profile only.
#
#   config.yaml                    cron.allow_agent_scheduling: true
#   profiles/<5 specialists>       cron.allow_agent_scheduling: false   (belt-and-braces)
#
# WHY
#   cron/scheduler.py:358 _resolve_cron_disabled_toolsets() strips
#   [cronjob, messaging, clarify, memory] from every cron-spawned agent when the flag is
#   false, and only [messaging, clarify, memory] when it is true. The delta is exactly
#   `cronjob`. BOTH of Webster's intake paths (marketing-task-sweep, clickup-chat-intake)
#   are cron runs, so today he has no `cronjob` tool autonomously — and `cronjob` is the
#   only route to outbound email (there is no email-send tool; delivery is the job's
#   `deliver: "email:..."` field).
#
#   Verified in the running image before writing:
#     - platform_toolsets.cron (the ALLOW-list, _resolve_cron_enabled_toolsets precedence 2)
#       already contains `cronjob` in the main config — 13 entries vs the specialists' 12.
#       So the denylist really is the only thing removing it. All 5 cron jobs have
#       enabled_toolsets=None, so precedence 1 (per-job) does not apply.
#     - The explicit `false` on the five specialists is NOT load-bearing: their
#       platform_toolsets.cron omits `cronjob` outright, so they can never receive it
#       whatever this flag says. It is here to make the intent unmistakable if that
#       allow-list is ever widened. Do not mistake it for the control that matters.
#
# SAFE PARTIAL BLOCK: hermes_cli/config.py:3569 does _deep_merge(config, user_config) over
# DEFAULT_CONFIG, so a cron: block containing only allow_agent_scheduling keeps all 14 other
# defaults (preflight, wrap_response, script_timeout_seconds, chronos, …).
#
# RESTART: none. Cron jobs are dispatched by the gateway, which re-reads config per run.
#
#   ./apply-cron-scheduling.sh          # dry run
#   APPLY=1 ./apply-cron-scheduling.sh  # write, after taking .bak-cronsched-<date>
#
# Line-insertion only — never a YAML round-trip. The profile configs use YAML anchors
# (&id001/*id001) that a load/dump cycle would silently expand and destroy.
set -euo pipefail

APPLY="${APPLY:-0}"
C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
[ -n "$C" ] || { echo "no hermes-agent container found" >&2; exit 1; }

PY=$(cat <<'PYEOF'
import os, re, shutil, sys

STAMP = "bak-cronsched-20260907"
APPLY = os.environ.get("APPLY") == "1"
ANCHOR = "mcp_single_query_discovery_timeout:"   # known top-level key, stable insertion point

MAIN = """cron:
  # Cron-spawned agents keep the `cronjob` toolset. Both of Webster's intake paths
  # (marketing-task-sweep, clickup-chat-intake) are cron runs, so while this is false he
  # has no `cronjob` tool autonomously -- and `cronjob` is the only route to outbound
  # email (no email-send tool exists; delivery is the job's `deliver: "email:..."` field).
  # false strips [cronjob, messaging, clarify, memory] from every cron run (scheduler.py
  # _resolve_cron_disabled_toolsets); true strips only [messaging, clarify, memory].
  # Partial block is safe: load_config does _deep_merge(DEFAULT_CONFIG, user_config), so
  # preflight / wrap_response / script_timeout_seconds keep their defaults.
  allow_agent_scheduling: true
"""

SPECIALIST = """cron:
  # Belt-and-braces, NOT load-bearing. A specialist can never receive `cronjob` in a cron
  # run regardless of this flag, because its platform_toolsets.cron allow-list omits
  # `cronjob` (12 entries vs the main config's 13) and that allow-list is applied before
  # the denylist. This explicit false only makes the intent unmistakable if the allow-list
  # is ever widened. Partial block is safe: DEFAULT_CONFIG is deep-merged underneath.
  allow_agent_scheduling: false
"""

TARGETS = [("/home/hermes/.hermes/profiles/%s/config.yaml" % p, SPECIALIST)
           for p in ("producer", "publisher", "writer", "seo", "researcher")]
TARGETS.append(("/home/hermes/.hermes/config.yaml", MAIN))

rc = 0
for path, block in TARGETS:
    print("== %s" % path)
    if not os.path.exists(path):
        print("     MISSING -- skipped"); rc = 1; continue
    src = open(path).read()
    if re.search(r"(?m)^cron:", src):
        print("     = top-level cron: already present -- skipped"); continue

    lines = src.splitlines(True)
    for i, ln in enumerate(lines):
        if ln.startswith(ANCHOR):
            lines.insert(i, block); break
    else:
        print("     ! anchor %r not found -- SKIPPED" % ANCHOR); rc = 1; continue

    want = "true" if block is MAIN else "false"
    print("     + cron.allow_agent_scheduling: %s" % want)
    new = "".join(lines)
    if APPLY:
        shutil.copy2(path, path + "." + STAMP)
        open(path, "w").write(new)
        print("     WROTE (backup .%s)" % STAMP)
    else:
        print("     (dry run -- set APPLY=1 to write)")
sys.exit(rc)
PYEOF
)

printf '%s' "$PY" | docker exec -i -u hermes -e APPLY="$APPLY" "$C" /opt/hermes/.venv/bin/python -
