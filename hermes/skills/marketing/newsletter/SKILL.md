---
name: newsletter
description: "Produce a monthly newsletter as a Mailchimp DRAFT by filling the master template's tokens — never send."
version: 2.0.0
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
| 2 | `writer` | The per-issue token values in house voice (subject, preheader, hero, feature, section headings, CTA band). |
| 3 | `producer` | The hero image, and the feature image if the issue runs a feature — absolute hosted URLs, same web-optimised spec as a blog hero. |
| 4 | `publisher` | Fills every token in the master template and creates a Mailchimp **draft campaign**. Comments the campaign id and preview link. |

## The master template — stored, never rebuilt

The layout is a **token template**, not something you write. Read it from object storage:

```
spaces_read client=webintelligenz path=newsletter/master-template.html
spaces_read client=webintelligenz path=newsletter/setup-and-tokens.html
```

- `master-template.html` (31,939 bytes) — the shippable master. 56 distinct `{{tokens}}`,
  80 occurrences. **Fill tokens only. Never edit the structure**, the dark-mode CSS, the MSO
  conditionals or the 600px table layout.
- `setup-and-tokens.html` (16,433 bytes) — the design-system authoring notes: per-token length
  guidance, image dimensions and the platform merge-tag mapping.

The canonical originals live **outside this repo**, in a different project that is not ours to
change — reference only, never edit:

```
/home/harsh/workspace/webintelligenz-newsletter/
  build_newsletter.py                                        <- reference implementation
  Web Intelligenz Design System/newsletter/Newsletter Template.html
  Web Intelligenz Design System/newsletter/Setup &amp; Tokens.html   <- note: literal "&amp;" in the filename
  dist/wi_newsletter_*.html                                  <- previously built issues
```

`build_newsletter.py` is the reference for *what the token values should be*. The fleet does not run
it — it lives in another repo and pulls posts from WordPress itself. Read it for the contract below,
do not copy it here.

## Delivery path — the publisher fills the tokens itself

**Mailchimp's MCP cannot attach a template.** This is settled; do not re-litigate it:

- `create_campaign` takes **no `template_id`** — it accepts `list_id`, `subject_line`, `title`,
  `preview_text`, `from_name`, `reply_to`, `segment_id`, `campaign_type`, `variate_settings_json`,
  and sends only `type`/`recipients`/`settings`.
- `set_campaign_content` takes **`html` only** — no `template`, no `sections`.
- `get_template_default_content` returns `{"html": null, "sections": {}}` for our classic template,
  so the layout cannot be recovered from Mailchimp.
- There is **no `get_campaign_content` tool** — a previous campaign's HTML cannot be read back.

Therefore the sequence is always:

1. `spaces_read` the master template.
2. Replace every `{{token}}` with this month's value (plain string substitution).
3. `create_campaign(...)` → take the returned `id`.
4. `set_campaign_content(campaign_id, html=<the finished HTML>)`.

If you ever need a `template_id` for `get_template` or `get_template_default_content`, pass it as a
**string** — an integer fails pydantic validation before the call is made.

## The token contract

56 tokens, in four groups. Every one must be filled or removed — a leftover `{{token}}` ships a
visible defect.

### 1. Brand constants (12) — always these values

```
company_name         = Web Intelligenz
company_address      = 2/1188 Toorak Rd, Camberwell VIC 3124
phone_number         = 1300 140 056
phone_e164           = 1300140056
email_address        = md@webintelligenz.com
linkedin_url         = https://www.linkedin.com/company/web-intelligenz
facebook_url         = https://www.facebook.com/webintelligenz
instagram_url        = https://www.instagram.com/webintelligenz/
blog_index_url       = https://webintelligenz.com/blog/
view_in_browser_url  = *|ARCHIVE|*
unsubscribe_url      = *|UNSUB|*
preferences_url      = *|UPDATE_PROFILE|*
```

The last three are **Mailchimp merge tags, not URLs**. They sit inside an `href` and Mailchimp
resolves them per recipient at send. Never substitute a literal `mailchi.mp` or `list-manage.com`
link — those carry a *past* campaign id, which is a real compliance and deliverability problem.

### 2. Two tokens that are NOT plain substitutions

- **`assets_base_url`** is composite. The template writes `{{assets_base_url}}/<filename>`; each
  whole path resolves to a hosted URL. The eight asset filenames are `logo-color.png`,
  `logo-white.png`, `website-design.png`, `software-development.png`, `ai.png`, `seo.png`,
  `google-ads.png`, `digital-marketing.png`. `logo-color.png` and `logo-white.png` map to
  `https://webintelligenz.com/wp-content/themes/shwib/_/img/logo.png` and `…/logo-white.png`; the six
  service glyphs map to `https://webintelligenz.com/wp-content/uploads/2026/06/wi-nl-<name>`.
- **`twitter_url` is never filled — the link is removed.** Web Intelligenz has no X account.
  `build_newsletter.py`'s `strip_x_link()` deletes the X footer `<td>` **and its preceding bullet
  separator**, and hard-errors unless exactly one match is removed. Do the same. Filling this token
  ships a footer link to an account that does not exist.

### 3. Per-issue tokens (27) — the writer authors these

| Token | Goes where | Notes |
|---|---|---|
| `email_subject` | subject line | ~30–55 chars |
| `preheader_text` | hidden inbox preview | ~80–120 chars |
| `issue_label` | header, top-right | e.g. "September 2026" |
| `hero_image_url` / `hero_image_alt` | hero banner | 600×~340px, **absolute** URL |
| `hero_eyebrow` | kicker above the headline | |
| `hero_heading` | lead headline | 4–8 words |
| `hero_intro` | lead paragraph | **must open with the greeting merge tag — see below** |
| `hero_cta_label` / `hero_cta_url` | primary button | label ≤ 4 words |
| `feature_eyebrow` / `feature_heading` / `feature_body` | optional 2-col spotlight | project spotlight or seasonal offer |
| `feature_image_url` / `feature_image_alt` | feature image | ~232px wide, absolute URL |
| `feature_cta_label` / `feature_cta_url` | feature text link | |
| `blogs_heading` | blog section heading | |
| `packages_heading` / `packages_intro` | support-packages band | Support24/48/96 and their credits are **fixed in the template** |
| `packages_cta_label` / `packages_cta_url` | packages button | |
| `services_heading` | services grid heading | the six services themselves are fixed |
| `cta_band_heading` / `cta_band_text` / `cta_band_label` / `cta_band_url` | closing dark CTA band | |

**The greeting lives inside `hero_intro`, not in the template.** Author it exactly as:

```
*|IF:FNAME|*Hi *|FNAME|*,*|ELSE:|*Hi there,*|END:IF|*<br><br>
```

then the body copy. That gives a personalised greeting with a fallback, so a blank first name reads
"Hi there," rather than "Hi ,".

**To drop the feature block entirely**, delete the HTML between the `OPTIONAL FEATURE` and
`END FEATURE` comments rather than leaving its seven tokens empty.

### 4. Per-post tokens (15) — pulled from WordPress

Three articles × five fields: `blog_{1,2,3}_url`, `_title`, `_excerpt`, `_image_url`, `_date`.

| Field | Source | Notes |
|---|---|---|
| `blog_N_url` | post permalink | |
| `blog_N_title` | post title | HTML-unescaped |
| `blog_N_excerpt` | post excerpt | 1–2 lines |
| `blog_N_image_url` | featured image | ~210×140px, **absolute** URL |
| `blog_N_date` | publish date | formatted `3 August 2026` (day, full month, year) |

For more or fewer than three articles, repeat or delete a whole `BLOG ITEM` row in the template and
renumber accordingly.

## Before you hand it over

- **No `{{` left anywhere.** Grep the finished HTML — the reference build treats any unfilled token
  as a fatal error, and so should you.
- **Every image URL absolute.** Email clients cannot load relative paths.
- The three Mailchimp merge tags are still intact and unquoted.
- The X/Twitter footer link and its bullet separator are gone.

## Hard rule

**Draft only. Never send.** `create_campaign` creates the campaign in `save` status, and
`send_campaign`, `schedule_campaign` and `delete_campaign` are **withheld at the litellm gateway**,
so they are not callable even by mistake. Never call them. Sending is a human action, always — even
after the content is approved, a person presses send. Say so explicitly when you relay the draft.

`send_test_email` is available and is the right way to preview a draft in a real inbox.

## Running it

Same shape as `blog`: brief as a ClickUp comment, linked cards carrying the `task_id`, verify the
parents, move to `in progress`, stop. Review it yourself before `in review` — every link resolves,
no tokens remain, the subject line is honest rather than clickbait, and the segment named in the
brief matches what the publisher actually targeted.
