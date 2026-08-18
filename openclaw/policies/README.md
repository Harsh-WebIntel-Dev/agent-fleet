# Company policies

**Global policy** lives in LiteLLM (`policies.wi-baseline`, attached at scope `*`) and is a
mechanical guardrail: it blocks credential-shaped strings in and out. It catches shapes, not meaning.

**Company policy** is a per-client `.md` file at `clients/<slug>/POLICY.md` in DO Spaces, read by
agents through the `spaces_read` tool. It carries the things that need judgement — claim limits,
voice, channel rules, approval requirements.

Why a file in Spaces rather than config:
- It is the client's document. Account managers can edit it without a deploy.
- Agents already have `spaces_read`, scoped to that client's folder, so no new mechanism.
- OpenClaw's memory indexer can pick it up, so it is recallable rather than only fetchable.

`POLICY.md.template` is the starting point. Copy it per client and edit.

## Wiring
`pm` and `qa` SOUL.md both instruct: read `POLICY.md` before approving, publishing or returning
work; treat it as **binding**, overriding both the agent's preferences and a user request that would
breach it; and if the file is missing, say so and treat the work as unapproved rather than assuming
no policy exists.

## Verified 2026-08-19
qa was given a draft breaching four rules at once and returned FAIL citing each specific rule:
absolute claims ("everything you need"), superlative + guarantee ("guaranteed the best"),
unsubstantiated environmental claims ("greener home", "eco-friendly" — ACL risk), and a phone number
in a GBP body. It read the policy from Spaces itself; nothing was pasted into the prompt.
