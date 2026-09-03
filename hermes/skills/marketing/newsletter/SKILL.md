---
name: newsletter
description: "Produce a monthly newsletter as a Mailchimp DRAFT — never send."
version: 1.0.0
author: Web Intelligenz
metadata:
  hermes:
    tags: [marketing, newsletter, mailchimp, clickup]
---

# Newsletter

Use when a ClickUp task asks for a newsletter or email campaign.

| Stage | Profile | Produces |
|---|---|---|
| 1 | `researcher` | What we actually published and what changed since the last edition — cited, with links. Skip only if the brief already lists the content. |
| 2 | `writer` | Subject line, preview text, and body sections in house voice, each with a link out. |
| 3 | `producer` | Header image if the edition needs one — same web-optimised JPEG spec as a blog hero. |
| 4 | `publisher` | A Mailchimp **draft campaign**. Comments the campaign id and preview link. |

## Hard rule

**Draft only. Never send.** Sending is a human action, always — even after the content is approved, a
person presses send. Say so explicitly when you relay the draft.

## Running it

Same shape as `blog`: brief as a ClickUp comment, linked cards carrying the `task_id`, verify the
parents, move to `in progress`, stop. Review it yourself before `in review` — every link resolves,
the subject line is honest rather than clickbait, and the segment named in the brief matches what the
publisher actually targeted.
