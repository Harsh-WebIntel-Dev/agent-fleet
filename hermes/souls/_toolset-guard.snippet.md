<!--
Shared turn-1 guard for all five specialist SOULs (seo, researcher, writer, producer, publisher).
Insert VERBATIM immediately after the SOUL's H1 title line and its one-paragraph role statement,
before the first `## ` section. Applied by hermes/scripts/apply-toolset-guard.sh.

Why this exists: a kanban worker re-discovers the whole `pm_comms` toolset live on every dispatch and
can lose the race against its own startup (CLAUDE.md §7, "Tool mounting is a RACE"). Twice on
2026-09-07 a specialist was handed built-ins only and burned a full dispatch before blocking:
  - t_d44a78e0 (producer, 14:08) — 16 minutes, and its block text blamed missing sandbox
    credentials, which sent the on-call to the wrong layer entirely.
  - t_e0a2bd40 (seo, 17:14)      — registration landed 2.5 s AFTER the agent snapshot.
-->

## FIRST — confirm your tools are mounted

Your MCP tools arrive as `mcp__pm_comms__*` and are re-discovered from scratch on **every** dispatch.
That discovery sometimes loses a race against your own startup. When it does you are handed the
built-ins only — no `clickup_*`, no `spaces_*`, no `memory_*`, no research or render tools. This is
an **infrastructure fault in the dispatch**: not a task problem, and not a credentials problem.

On turn 1, before reading the card or planning anything, check your tool list for
`mcp__pm_comms__clickup_get_task`. If it is missing:

1. Block immediately, with this exact marker so the fleet can find it:
   `kanban_block(reason="TOOLSET-NOT-MOUNTED — no mcp__pm_comms__* tools in this dispatch. MCP discovery lost the race at worker startup. Infrastructure fault, not a task problem — re-dispatch this card.")`
2. Stop. Produce nothing, investigate nothing.

Do **not** go looking for API keys or tokens in the sandbox, read config files, or try to reach a
service over HTTP from the terminal. The sandbox is secret-free **by design** — its emptiness is
expected and tells you nothing. MCP tools mount at the agent level; if they are absent, no amount of
terminal work can recover them, and guessing at credentials in your block text sends the on-call to
the wrong layer.
