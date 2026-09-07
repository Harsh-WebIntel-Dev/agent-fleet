#!/usr/bin/env python3
"""Patch the live Hermes SOULs for the social-card render-path defect (2026-09-08).

Root cause: whether a social card carries brand type is decided by the wording of Webster's kanban
brief. Nothing in Webster's SOUL requires template rendering for a social card, and the producer's
"no text baked into the image" rule (correct for a photographic plate) reads as agreement with a
brief that says "no baked text or logos" — so the producer ships a bare AI photo where a card belongs.

Secondary: the producer SOUL's own template step pointed at clients/<slug>/brand/templates/, a prefix
that has never existed in R2. The real paths are clients/<slug>/social/ and clients/<slug>/gmb/.

Refuses to write unless every anchor matches exactly. Idempotent.
"""
import io, os, shutil, sys, datetime

STAMP = datetime.date.today().strftime("%Y%m%d")
WEBSTER = "/home/hermes/.hermes/SOUL.md"
PRODUCER = "/home/hermes/.hermes/profiles/producer/SOUL.md"

# --- producer: fix the dead template path -----------------------------------------------------------
PROD_OLD_PATH = ("1. `rclone copy r2:fleet-clients/clients/<slug>/brand/templates/ "
                 "/workspace/<slug>-<task>/` — pull the client's HTML card template + brand fonts/logo "
                 "(never invent a layout; the template and palette are the client's, from the brand kit).")
PROD_NEW_PATH = (
    "1. Pull the client's card templates. For Web Intelligenz they live at "
    "`r2:fleet-clients/clients/<slug>/social/` (`fb.html` 1200×630, `ig.html` 1080×1080) and "
    "`r2:fleet-clients/clients/<slug>/gmb/` (`card.html` 1200×900 — read `gmb/RULES.md` first). "
    "`rclone copy r2:fleet-clients/clients/<slug>/social/ /workspace/<slug>-<task>/` (and the `gmb/` "
    "prefix if you need the GBP card), or read a single file with `spaces_read`. There is **no** "
    "`brand/templates/` prefix — if an `rclone copy` returns nothing, you have the wrong path: list it "
    "with `rclone lsf` before concluding the template is missing. Never invent a layout; the template "
    "and palette are the client's, from the brand kit.")

# --- producer: the brief is not allowed to turn a card into a bare photo -----------------------------
PROD_OLD_TAIL = ('Keep the "no invented facts, no guessed brand values" rule: if the template, a brand '
                 "font, or the logo isn't in the brand kit, say so and block — don't substitute a "
                 "generic card.")
PROD_NEW_TAIL = PROD_OLD_TAIL + """

**The text ban does NOT apply to a card.** "No text baked into the image" governs what a *generation
model* draws — the photographic plate. A social/feed card is the opposite: its whole job is to carry
legible brand type. So if a brief for a Facebook / Instagram / LinkedIn / Google Business card says
"images", "no baked text", "no logos", or "the caption and platform carry those", **that brief is
wrong** — a bare photo in a feed slot is the defect this section exists to prevent. Render the card
from the template anyway, and say plainly in your comment that you read the brief as asking for a
textless plate and produced a branded card instead, so the PM can correct the brief. Resizing or
cropping a Higgsfield plate to a platform aspect ratio does **not** make it a card: a real card comes
out of `render-card` as a **PNG**; if you are about to hand over `social/<slot>.jpg`, stop."""

# --- webster: the rule that was missing entirely -----------------------------------------------------
WEB_ANCHOR = "## How work actually gets done"
WEB_RULE = """## A social card is RENDERED from the template, never AI-generated

A social feed card (Facebook / Instagram / LinkedIn / Google Business Profile) is the one artefact in
the fleet that must carry legible type — headline, kicker, logo, brand rule. The producer renders it
with `render-card` from the client's HTML template; Higgsfield only makes the untexted photographic
**plate** that sits behind that type.

**Your brief decides which one you get.** A visuals card must therefore always:
- say **"social cards — RENDER FROM HTML TEMPLATES, NOT AI photos"**, and
- name the actual template paths in Spaces — `social/fb.html` (1200×630), `social/ig.html`
  (1080×1080), `gmb/card.html` (1200×900, read `gmb/RULES.md` first), and
- give the **real headline / kicker / stat line** to drop into them, from the writer's copy.

Never write "no baked text", "no logos", or "the caption and platform carry those" on a card brief.
That phrasing belongs only to a *photographic hero*; on a social card the producer reads it as
agreement with its own no-text-in-AI-images rule and hands back a bare photo. This is exactly how the
2026-09-07 socials shipped as untexted stock-looking images.

When you review the result: a rendered card is a **PNG** and you can see the headline on it. A
`social/<slot>.jpg` is a resized AI plate — reject it and route the block to **producer**.

"""


def patch(path, edits, label):
    src = io.open(path, encoding="utf-8").read()
    out = src
    applied, already = [], []
    for old, new in edits:
        if new in out:
            already.append(old[:48])
            continue
        if out.count(old) != 1:
            print("ABORT %s: anchor found %dx (need exactly 1):\n  %r" % (label, out.count(old), old[:110]))
            return None
        out = out.replace(old, new)
        applied.append(old[:48])
    if out == src:
        print("%s: already patched, no change" % label)
        return False
    bak = "%s.bak-socialcards-%s" % (path, STAMP)
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    io.open(path, "w", encoding="utf-8").write(out)
    print("%s: patched (%d edits), backup %s" % (label, len(applied), os.path.basename(bak)))
    print("   %d -> %d bytes" % (len(src), len(out)))
    return True


PUBLISHER = "/home/hermes/.hermes/profiles/publisher/SOUL.md"
PUB_ANCHOR = "## Blocking usefully"
PUB_RULE = """## A social card must be a rendered card, not a photo

You are the last gate before something is public, so check the *asset*, not just that an asset
exists. A branded social card is rendered from the client's HTML template by `render-card`, which
only ever writes **PNG**. So:

- `blog/<slug>/social/<slot>.png` — a real card. Open the presigned URL and confirm you can see the
  **headline text** on it.
- `blog/<slug>/social/<slot>.jpg` — **not a card.** That is a Higgsfield photographic plate someone
  resized to the platform aspect ratio. It has no headline, no kicker and no logo.

If you are handed a `.jpg` for a feed slot, or a URL whose image carries no legible text, **block and
route to producer** with "social card was delivered as a resized AI plate, needs a template render".
Do not attach it and do not schedule it, even when the brief hands you the URL directly — a brief
that says "attach the correct platform-sized image" is not evidence that the image is a card. This
happened on 2026-09-07: three live posts went out carrying bare stock-looking photos.

"""

ok = True
r = patch(PRODUCER, [(PROD_OLD_PATH, PROD_NEW_PATH), (PROD_OLD_TAIL, PROD_NEW_TAIL)], "producer")
ok = ok and r is not None
r = patch(WEBSTER, [(WEB_ANCHOR, WEB_RULE + WEB_ANCHOR)], "webster")
ok = ok and r is not None
r = patch(PUBLISHER, [(PUB_ANCHOR, PUB_RULE + PUB_ANCHOR)], "publisher")
ok = ok and r is not None
sys.exit(0 if ok else 1)
