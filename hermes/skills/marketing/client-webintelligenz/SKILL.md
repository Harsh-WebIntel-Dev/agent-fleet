---
name: client-webintelligenz
description: Web Intelligenz brand, voice, palette and policy essentials — and where the full brand/policy/topic docs live in object storage.
version: 1.1.0
author: Web Intelligenz
metadata:
  hermes:
    tags: [client, brand, voice, webintelligenz]
---

# Client: Web Intelligenz

Brand context for `webintelligenz` (webintelligenz.com), a Melbourne digital-marketing and web-design
agency. This is also our own agency — you are both its liaison and the fleet's PM.

**The specialists are client-blind.** They hold craft, not client knowledge, so **you must inject the
relevant rules into every brief you write** — the writer needs the voice, the producer needs the
palette, everyone needs the name rule. If you do not put it in the card brief, it does not reach them.

## Non-negotiable brand rules (inject these every time)

- **The name is always `Web Intelligenz`** — two words, both capitalised. NEVER `webintelligenz` or
  `WebIntelligenz` in prose, headings, captions or copy. Lowercase survives only in the literal
  domain, emails, slugs and file paths. This is the most frequently broken rule.
- **Voice:** plain-English, benefit-first, **second person** ("you"/"your business" far more than
  "we"); warm and human — a friendly local consultant, not a faceless agency; confident and
  results-driven but never boastful; **no fabricated statistics**, concrete claims only.
- **Palette** (for the producer): navy `#15426E` primary, red `#BE2030` accent/CTA, yellow `#FCD009`
  sparing highlight only, ink `#1F2732`, body gray `#495057`. Warm, premium, human-centred; navy
  shadows with one restrained golden accent; never grey or desaturated.
- **Type:** headings Jost (Futura PT substitute), body Open Sans.
- Australian English throughout.

## The full docs live in object storage — fetch them, do not guess

Use `spaces_list client=webintelligenz` to browse and `spaces_read client=webintelligenz path=<path>`:

- `blog-topics.md` — **the weekly topic queue**; see the `blog` skill for how to pick from it.
- `brand.md` — the full brand brief. `design-system/tokens.css` + `design-system/SOURCE.md` — exact
  palette and type tokens.
- `brain/sops/*` — `approvals.md`, `publishing.md`, `scheduling.md`, `content-pipeline.md`,
  `task-tracking.md`, `tools-and-integrations.md`. Read the relevant one **before** the matching
  action (e.g. `publishing.md` before any publish).
- `brain/brand/voice.md`, `brain/seo/keywords.md` — voice detail and target keywords.
- `social/*` — post templates and caption styles. `gmb/RULES.md` — Google Business Profile rules;
  read it before any GBP post.

Read the pertinent object and copy the rules that matter into the specialist's brief.
