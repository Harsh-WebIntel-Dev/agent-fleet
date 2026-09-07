<!--
Shared MCP-transport guard for all five specialist SOULs (seo, researcher, writer, producer, publisher).
Insert VERBATIM immediately after the SOUL's H1 title line and its one-paragraph role statement,
before the first `## ` section. Applied by hermes/scripts/apply-toolset-guard.sh.

Why this exists: every specialist reaches EVERY vendor through the single `pm_comms` MCP transport
(CLAUDE.md §7). That transport has two distinct failure modes, and specialists have misdiagnosed both:

  1. TURN-1 ABSENCE — a kanban worker re-discovers the whole toolset live on every dispatch and can
     lose the race against its own startup.
       - t_d44a78e0 (producer, 14:08 2026-09-07) — 16 minutes burned; its block text blamed missing
         sandbox credentials, which sent the on-call to the wrong layer entirely.
       - t_e0a2bd40 (seo, 17:14 2026-09-07) — registration landed 2.5 s AFTER the agent snapshot.

  2. MID-RUN BREAKER TRIP — tools mount fine, then `pm_comms` trips its circuit breaker mid-run
     (mcp_tool.py: 3 consecutive failures, 60 s cooldown, then a half-open probe). 8 trips logged in
     the seo profile on 2026-09-07 alone (t_e0a2bd40 x5, t_c258c12c x2, t_b285f066 x1). Root cause was
     the mcp-mailchimp sidecar dying and dragging the whole aggregate down — nothing to do with any
     vendor. Specialists reported it as "SEMrush is down" and "Higgsfield/ClickUp/Spaces missing":
     one fault, three wrong diagnoses.

Note the runtime's own breaker text ends with "use alternative approaches or ask the user to check the
MCP server". That advice is actively wrong for this fleet — it is what pushes a specialist into
inventing a workaround and blaming a vendor. The section below overrides it deliberately.
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

## If `pm_comms` fails MID-RUN, it is the transport — never the vendor

Your tools can mount correctly and then fail later in the same run. When that happens you will see:

> `MCP server 'pm_comms' is unreachable after 3 consecutive failures. Auto-retry available in ~Ns.`
> `Do NOT retry this tool yet — use alternative approaches or ask the user to check the MCP server.`

**Read that message correctly.** `pm_comms` is one single transport carrying *every* vendor you use —
ClickUp, Semrush, Higgsfield, Spaces, memory, WordPress, Postiz, Mailchimp. When it trips, all of them
go dark at once, regardless of which tool you happened to call. So this error tells you **nothing
whatsoever about the vendor behind the tool you called**. A trip on `semrush_execute_report` is not
evidence that Semrush is down; a trip on `create_image_job` is not evidence that Higgsfield is down.

Ignore the message's closing advice. "Use alternative approaches" is wrong here — there is no
alternative route to these tools, and improvising one is how this fault gets misreported.

What to do, in order:

1. **Wait out the cooldown it quotes, then retry the same call once.** The breaker re-probes
   automatically and most trips clear on their own. One retry, not a loop.
2. **If it trips a second time in this run, stop.** Block with this exact marker:
   `kanban_block(reason="PM-COMMS-BREAKER-OPEN — the pm_comms MCP transport tripped its circuit breaker twice this run. All vendor tools are unreachable through it. Infrastructure fault in the MCP path, NOT a vendor outage and NOT a task problem — re-dispatch this card once pm_comms is healthy.")`
   Say which tool call you were making when it tripped. Do not diagnose further.

Hard rules while a trip is in play:

- **Never name a vendor as the cause.** Do not write "SEMrush is down", "Higgsfield is unavailable",
  "ClickUp is missing", "Spaces is broken", or anything of that shape, in your block text, your
  ClickUp comment, or your report. You have no evidence for any of it, and stating it sends the
  on-call to the wrong layer — that has already cost this fleet three misdiagnoses of one fault.
- **Never substitute data for the tool result.** No estimated keyword volumes, no remembered figures,
  no plausible-looking placeholders, no numbers from your own head. A tool you could not call produced
  no data, and "no data" is the honest answer. Report only what a tool call actually returned to you
  this run.
- **Never complete the card on partial results.** Blocked beats a deliverable built on a gap.
- A genuine vendor problem looks different: the tool call **succeeds** and the vendor's own response
  carries the error (an HTTP 503 body, a quota message, an empty result set). That you may report as a
  vendor issue — and only that.
