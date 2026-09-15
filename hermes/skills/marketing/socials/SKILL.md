---
name: socials
description: "Produce platform-specific social posts (Instagram, Facebook, Google Business Profile, LinkedIn) via Postiz — scheduled only on explicit human approval; nothing goes live without it."
version: 1.0.0
author: Web Intelligenz
metadata:
  hermes:
    tags: [marketing, social, postiz, gbp, clickup]
---

# Social posts

Use when a ClickUp task asks for social posts — standalone, or promoting an existing blog.

| Stage | Profile | Produces |
|---|---|---|
| 1 | `writer` | Per-platform copy + hashtags. Write each platform natively; never reuse one text everywhere. |
| 2 | `producer` | The visual, where the platform needs one. |
| 3 | `publisher` | On the PM-relayed approval: one Postiz post per **required platform** (Web Intelligenz = Instagram + Facebook + Google Business Profile unless the brief says otherwise), each with its image, in the approved slot. Comments each post id, preview link and slot in AEST. Postiz *drafts* are not used (decision 2026-09-15). |

If the task promotes an existing blog, that blog's URL is an input — put it in the brief; do not let
the writer invent one.

## Platform slate — the brief names every platform, the review gate checks every platform

The PM's brief enumerates the platforms this piece must reach; the writer writes each natively; the
publisher schedules **all of them**. At review the PM runs `postiz_list` over the slot window and requires
one queued post per required platform per piece, each with an image. A missing platform is a block to
**publisher**, never `in review` — on 2026-09-14 a week's build went out Instagram-only and only a human
noticed.

## Platform rules that actually matter

- **Facebook** — an attached image **suppresses the link preview card**. If the goal is clicks to the
  article, post the link without an image. Choose deliberately and say which you chose.
- **Instagram** — no clickable links in a caption. Drive to "link in bio" and check the bio link is
  the one we want.
- **Google Business Profile** — **no phone numbers**, 1,500 character limit, image spec applies, CTA
  must be one of the allowed enum values.
- **LinkedIn** — lead with the insight, not the announcement; no hashtag spam.

## Image sizing — render each platform's visual at its official size

A social image is not a blog hero. Render every social visual at the **target platform's official
aspect ratio** — the producer card must name the platform. Wrong aspect = the platform crops or
letterboxes it and it looks amateur. `create_image_job` on `nano_banana_pro` accepts
`1:1, 3:2, 2:3, 4:3, 3:4, 4:5, 5:4, 9:16, 16:9, 21:9` — there is **no 1.91:1**, so never chase a
link-card ratio with a generated image (post the bare link instead).

| Platform / placement | `aspect_ratio` | Official target |
|---|---|---|
| Instagram feed | **4:5** (portrait) | 1080×1350 — most feed space; `1:1` also fine |
| Instagram **3×3 grid tile** | rendered card | 1080×1080 from `social/ig.html` — all type and logo inside the **175 px safe inset (350 px at render scale 2)**; QA the 3:4 grid crop before review |
| Facebook feed image | **1:1** (square) | 1080×1080 — `4:5` also fine |
| Paired FB + IG feed image | **4:5** | one 4:5 render serves both feeds natively — don't render twice |
| Story / Reel (IG or FB) | **9:16** | 1080×1920 |
| Google Business Profile | **4:3** (landscape) | 1200×900 — keep the subject centred |
| LinkedIn feed image | **1:1** (square) | 1200×1200 — `16:9` ok for a landscape shot |
| X / Twitter single image | **16:9** | 1600×900 |

Keep `resolution="2k"` (same credit as `1k`) — a 2k render at any of these aspects comfortably exceeds
the platform's minimum pixels, so it stays crisp after the platform downscales. If one post targets
platforms with **different** aspects (e.g. IG 4:5 + GBP 4:3), render one image **per aspect** — a
mis-sized reuse is a defect, not a shortcut. An AI-generated **plate** carries no text or logos; a branded
**card** (render-card from the client's HTML template) carries the headline, kicker and logo — a social feed
card is always a rendered card, never a bare plate (see the Webster SOUL, "A social card is RENDERED").

## Hard rule

**Nothing goes live without an explicit, PM-relayed human approval for that specific piece.** The publisher
schedules Postiz posts only against that approval and reuses existing ids on rework — never a second copy of a
post. Postiz drafts are not part of the flow.
