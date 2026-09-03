---
name: blog
description: "Run a blog end-to-end: pick the topic, chain seo -> writer -> producer -> publisher, review, relay for approval."
version: 1.1.0
author: Web Intelligenz
metadata:
  hermes:
    tags: [marketing, blog, clickup, kanban]
---

# Blog production

Use when a ClickUp task asks for a blog post, or when the weekly blog slot comes round.

## Picking the topic (when the task does not name one)

The topic queue lives in object storage, not in ClickUp:
`spaces_read client=webintelligenz path=blog-topics.md`

Its own rules, which you follow exactly:
1. Take the **first unused** entry — a line starting `- [ ]`. Do not skip ahead, do not re-order, do
   not invent a topic while unused ones remain.
2. The format is `- [ ] Title | focus-keyword (approx AU vol) | internal-link`. All three parts feed
   the brief: the title is the working title, the keyword is the SEO stage's starting hypothesis (it
   still verifies with real SEMrush data), the internal link is a required link target.
3. **Verify it is not already published** before committing to it — check the site's
   `post-sitemap.xml` or `/blog/`. The list has been wrong before.
4. Once you commit, **tick it** `- [x]` and write the file back (`spaces_write`), appending the
   ClickUp task id, so the next run does not pick the same topic.
5. If every topic is used, say so and propose fresh ones from real SEMrush research rather than
   quietly inventing filler.

## The chain

Four cards, each assigned to the profile that owns the stage, each the parent of the next:

| Stage | Profile | Produces (as a ClickUp comment) |
|---|---|---|
| 1 | `seo` | Primary keyword with real SEMrush AU volume/difficulty, meta title (<=60), meta description (<=160), slug, internal-link targets. Blocks with a swap proposal if nothing is winnable. |
| 2 | `writer` | The draft: title + HTML-ready body in house voice, internal links placed, ~1,000-1,300 words unless the brief says otherwise. Marks **1-2 in-content image slots** inline as `[[IMAGE: <subject>, aspect 4:3]]` plus the hero brief. |
| 3 | `producer` | **Hero (16:9)** + each in-content slot image. Renders via Higgsfield at `resolution=2k`, hands off the **WebP `web_url`** (~150KB, web-optimised — never the multi-MB PNG), on-topic, brand palette, no baked-in text. Records real dimensions + byte size + the slot→web_url mapping. |
| 4 | `publisher` | WordPress **draft** (Yoast fields, existing category, **featured image = hero web_url**), replaces each `[[IMAGE: …]]` marker in the body with its uploaded in-content image, + Postiz social **drafts**. Comments the `edit_link`, `post_id` and preview links. |

There is no QA stage — **you are the review gate** (see your SOUL).

## Running it

1. Confirm the task is yours to act on (list `901613842998`, assignee `106813628`, status
   `to do`/`approved`/`rejected`).
2. Write the brief as a ClickUp comment — topic, audience, goal, tone, length, the keyword and
   internal-link targets from the topic bank, and the brand rules the specialists need (they are
   client-blind; see the `client-webintelligenz` skill).
3. Create the four cards with the ClickUp `task_id` in each, linked in order, then **verify each card
   has exactly its intended parent** and only `seo` has none.
4. Move the ClickUp task to `in progress`. **Stop** — the dispatcher runs the chain.

## When it comes back

Review per your SOUL: re-fetch the draft (`wp_get_post`) and the hero yourself, check voice, SEO
fields, compliance and that every claimed artefact is real. Then move to `in review`, add Paul and
Harry, and post the review links.

On `rejected`/changes: route the fix to the stage that owns it, reset that stage **and its
descendants**, and state what must be produced differently. Reuse the existing `post_id` — never
create a second draft.

## Publish

Only after an explicit human approval. `publisher` calls `wp_publish` on the saved `post_id`, you
verify it is genuinely live, then arm the social drafts against the live URL and move the task to
`completed`.
