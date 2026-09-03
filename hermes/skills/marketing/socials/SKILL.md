---
name: socials
description: "Produce platform-specific social drafts (LinkedIn, Facebook, Instagram, GBP) via Postiz — draft only."
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
| 3 | `publisher` | Postiz **drafts** per channel. Comments each preview link and the intended slot in AEST. |

If the task promotes an existing blog, that blog's URL is an input — put it in the brief; do not let
the writer invent one.

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
| Facebook feed image | **1:1** (square) | 1080×1080 — `4:5` also fine |
| Paired FB + IG feed image | **4:5** | one 4:5 render serves both feeds natively — don't render twice |
| Story / Reel (IG or FB) | **9:16** | 1080×1920 |
| Google Business Profile | **4:3** (landscape) | 1200×900 — keep the subject centred |
| LinkedIn feed image | **1:1** (square) | 1200×1200 — `16:9` ok for a landscape shot |
| X / Twitter single image | **16:9** | 1600×900 |

Keep `resolution="2k"` (same credit as `1k`) — a 2k render at any of these aspects comfortably exceeds
the platform's minimum pixels, so it stays crisp after the platform downscales. If one post targets
platforms with **different** aspects (e.g. IG 4:5 + GBP 4:3), render one image **per aspect** — a
mis-sized reuse is a defect, not a shortcut. Never bake text or logos into the image; the caption and
platform carry those.

## Hard rule

**Drafts only — never schedule anything live.** Arming a live slot happens only after explicit human
approval, against the existing drafts rather than new ones.
