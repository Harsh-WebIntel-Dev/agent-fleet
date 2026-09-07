---
name: newsletter
description: "Produce a monthly newsletter as a Mailchimp DRAFT from the stored master template — never send."
version: 1.1.0
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
| 2 | `writer` | Subject line, preview text, and the copy for each template slot in house voice, each with a link out. |
| 3 | `producer` | Hero image if the edition needs one — same web-optimised JPEG spec as a blog hero. |
| 4 | `publisher` | A Mailchimp **draft campaign**, built by filling the stored master template. Comments the campaign id and preview link. |

## The master template — never rebuild the HTML

The newsletter layout is **stored, not reconstructed**. It lives in object storage:

```
spaces_read client=webintelligenz path=newsletter/template.html
```

Read that file, fill the slots, and hand the whole string to `set_campaign_content`. **Never rewrite
the layout from memory or rebuild it by eye** — that is what this file exists to stop. If the render
looks wrong, the fix is a corrected `template.html`, not a hand-built one-off.

Alongside it, `newsletter/template-source-aug2026.html` is the untouched rendered August 2026
campaign, kept only as a reference for what the original looked like. **Do not send that one** — it
still carries the August campaign's resolved links. Always build from `template.html`.

### Fill only the marked slots

`template.html` carries a slot index in an HTML comment at the top of `<head>`, and every per-issue
region is marked inline with `<!-- SLOT:... -->`. The slots, in document order:

| Slot | What goes in it |
|---|---|
| `SLOT:TITLE` | `<title>`, mirroring the subject line |
| `SLOT:PREHEADER` | hidden inbox preview text — keep it in step with the campaign's `preview_text` |
| `SLOT:ISSUE_DATE` | the month eyebrow in the header (e.g. `August 2026`) |
| `SLOT:HERO_IMAGE` | hero banner `src` + `alt` only |
| `SLOT:EYEBROW` | the kicker above the H1 (e.g. `This month`) |
| `SLOT:H1` | lead headline |
| `SLOT:INTRO` | lead paragraph — **keep the FNAME greeting merge tag and the `<br><br>` breaks** |
| `SLOT:PRIMARY_CTA` | lead button label + `href` |
| `SLOT:FEATURE` | optional 2-col spotlight; delete the whole `START`→`END` region to drop it |
| `SLOT:BLOG_HEADING` | the blog-section kicker + headline |
| `SLOT:BLOG_ITEM_1..3` | one card per article: thumbnail `src`/`alt`, date, title, blurb, and the article href — which appears **3 times** per card (thumbnail, title, "Read more") and must match in all three |

To carry more or fewer than three articles, repeat or delete a whole blog-item `<tr>` block — the
in-file comment marks it.

### Leave everything else alone

Static furniture, byte-intact: header logo + dot motif, the Support24/48/96 band, the services grid,
the CTA band, the footer NAP/socials/address, all dark-mode CSS, the MSO conditionals, and the 600px
table structure. Touching those is how a newsletter silently breaks in Outlook or dark mode.

### Merge tags are already correct — do not replace them with URLs

The master ships with real Mailchimp merge tags. Mailchimp resolves them per recipient at send:

- `*|IF:FNAME|**|FNAME|**|ELSE:|*there*|END:IF|*` — the greeting. Personalised, with a fallback so a
  blank first name reads "Hi there," rather than "Hi ,".
- `*|ARCHIVE|*` — the top "View it in your browser" and the footer "View in browser".
- `*|UNSUB|*` — footer unsubscribe. **Required, never remove it.**
- `*|UPDATE_PROFILE|*` — footer update-preferences.

All four sit **inside** an `href` and resolve to a bare URL, so the surrounding `<a>` stays as it is.
Never substitute a literal campaign URL: a hard-coded `mailchi.mp` archive link or a
`list-manage.com` unsubscribe link carries a *past* campaign id, which is a real compliance and
deliverability problem. There is likewise no need for `[UNIQID]` anywhere.

## Mailchimp tool limits — why it works this way

The template lives in Spaces because the MCP genuinely cannot attach a Mailchimp template. Verified
against the deployed `mailchimp-mcp` 0.6.0:

- **`create_campaign` has no `template_id` parameter.** It accepts `list_id`, `subject_line`,
  `title`, `preview_text`, `from_name`, `reply_to`, `segment_id`, `campaign_type`,
  `variate_settings_json` — and sends only `type`/`recipients`/`settings`. A campaign cannot be
  created against a stored template.
- **`set_campaign_content` accepts `html` only** — `set_campaign_content(campaign_id, html)`, which
  PUTs `{"html": ...}`. No `template`, no `sections`. It replaces the body entirely.
- **`get_template_default_content` is empty for our classic template.** For template id
  `11148675` ("WI Monthly Newsletter — v1") it returns `{"html": null, "sections": {}}`, so the
  layout cannot be recovered from Mailchimp.
- **There is no `get_campaign_content` tool.** You cannot read a previous campaign's HTML back out to
  reuse it. Nothing round-trips.
- `create_template` / `update_template` do exist, but storing a template in Mailchimp buys nothing —
  `create_campaign` still has no way to attach it.

So the only path to pixel-exact output is: read the stored HTML → fill the slots →
`set_campaign_content(campaign_id, html)`.

**`template_id` must be a STRING.** `get_template` and `get_template_default_content` are typed
`template_id: str`; passing the id as an integer fails with a pydantic validation error before the
call is made. Use `"11148675"`, not `11148675`.

## Hard rule

**Draft only. Never send.** `create_campaign` and `set_campaign_content` are draft-only by design —
the campaign is created in `save` status, and `send_campaign`, `schedule_campaign` and
`delete_campaign` are **withheld at the litellm gateway**, so they are not callable even by mistake.
Sending is a human action, always — even after the content is approved, a person presses send. Say so
explicitly when you relay the draft.

`send_test_email` is available and is the right way to preview a draft in a real inbox.

## Running it

Same shape as `blog`: brief as a ClickUp comment, linked cards carrying the `task_id`, verify the
parents, move to `in progress`, stop. Review it yourself before `in review` — every link resolves,
the subject line is honest rather than clickbait, the merge tags are still intact, and the segment
named in the brief matches what the publisher actually targeted.
