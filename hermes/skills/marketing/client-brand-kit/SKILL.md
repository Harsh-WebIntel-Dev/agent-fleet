---
name: client-brand-kit
description: "Create a missing client brand kit before any work starts — request the essentials from the requester (never invent them), then write clients/<slug>/brand-kit.md so the fleet can work on-brand."
version: 1.0.0
author: Web Intelligenz
metadata:
  hermes:
    tags: [marketing, client, brand, onboarding, gate]
---

# Client onboarding — build the brand kit

**Use this the moment work arrives for a client whose brand kit is missing or incomplete** — i.e. the
readiness gate in your SOUL found no `clients/<slug>/brand-kit.md`, or it exists but a required field
is blank. Also use it whenever someone asks to onboard or set up a new client.

## The one rule: never invent a client's facts

A brand kit records what the **client** told us — their name, services, voice, policy, logo, NAP. You
may **not** make any of it up, guess it, or scrape it off the web and present it as theirs. Getting a
business's phone number, address or claims policy wrong is far worse than asking. If it isn't already
on file, you **ask the person who gave you the work**. The only things you may fill without asking are
format/derived fields (the `slug`, the file path) — never a business fact.

If part of a kit already exists (some facts in client memory, an older doc), reuse those and ask only
for the genuine gaps — don't re-ask for what you already hold.

## Step 1 — Register the client, then check what's missing

If this is a **new** client (no `clients/<slug>/` and you've never worked for it), first claim the slug
in fleet memory so the memory tools will work for it:

- `memory_register_client(client="<slug>", display_name="<Client Name>")` — once. Idempotent; it only
  creates the client's memory namespace and touches no other client's data. Until you do this,
  `memory_search` and `memory_remember` refuse the slug as an "unknown client".

Then check what you already hold:

- `spaces_read(path="brand-kit.md", client="<slug>")`
- `memory_search(query="brand voice policy NAP logo", agent="webster", client="<slug>")`

Note which required items (below) are present and which are blank. An item counts as *held* only if it
is actually recorded — not a general impression of who the client is.

## Step 2 — Request the gaps from the requester (once, all at once)

Reply to the person who asked, in plain language, listing exactly what you still need. Ask for
**everything missing in one message** — don't dribble out one question at a time. Post the same list as
a comment on the ClickUp task, set it to `waiting on client`, and **do not build any cards** until you
have it. For example:

> Before I start on <the work>, I need a few brand basics for <Client> so everything we produce is
> on-brand and accurate. Could you send me:
> - …the missing items…
>
> Once I have these I'll get straight onto it.

### Required items (the brand-kit checklist)

**Hard-required before ANY deliverable**
- **Business basics** — trading name (exact capitalisation), what they do, main services/products,
  target audience, service area.
- **Brand voice & policy** — tone; do's and don'ts; any claims/compliance limits; anything they must
  never say; competitors not to name.
- **Logo** — the actual file (ask them to attach it), plus brand colours and fonts if they have them.

**Required when the work needs it**
- **Local / GMB / anything carrying contact details** — NAP: exact business name, street address,
  phone; website URL; Google Business Profile link.
- **Social** — the client's own handles/profiles to post to.

## Step 3 — Capture what they send, so you never re-ask

- **Logo / any attached asset:** ingest it immediately —
  `spaces_ingest_url(path="assets/logo.png", source_url="<the attachment url>", client="<slug>")`.
  ClickUp attachment URLs die in ~5 minutes, so do this in the **same turn** you receive them.
- **Write the kit:** `spaces_write(path="brand-kit.md", client="<slug>", content=…)` using the
  template below. Fill only fields you were actually given; on anything still outstanding leave an
  explicit `— to confirm` rather than inventing it.
- **Mirror the durable essentials** (voice, policy, NAP, the name rule) to
  `memory_remember(…, agent="webster", client="<slug>")` so a later run recalls them by meaning.

## Step 4 — Confirm and proceed

Tell the person the kit is set up, then start the work normally (build the chain). The specialists read
`clients/<slug>/brand-kit.md` themselves, so from here the client is "ready" and the gate passes.

## The brand-kit.md template

```markdown
# <Client> — Brand Kit

> The fleet's on-file brand kit for client `<slug>`. Authoritative. If something is wrong or
> missing, fix it here; don't work around it.

## Business basics
- **Name:** <exact, correctly capitalised>
- **What it is:** <one line>
- **Services:** <list>
- **Audience:** <who>
- **Service area:** <where>
- **Tagline:** <if any — else "— none supplied">

## Brand voice & policy
- **Voice:** <characteristics>
- **Spelling:** <e.g. Australian/British English>
- **CTAs / phrasing:** <preferred>
- **Must never:** <claims/compliance limits, banned terms, competitors not to name>

## Brand assets
- **Logo:** assets/logo.png (+ any variants) — <describe>
- **Colours:** <hex list, with roles>
- **Fonts:** <headings / body>
- **Image style:** <direction>

## NAP & links
- **Phone:** <or "— to confirm">
- **Website:** <url>
- **Google Business Profile:** <url or "— to confirm">
- **Socials:** <handles or "— confirm via Postiz">
- **Address:** <full street address or "— to confirm">
```

## Creating the client's namespace (folder + memory)

A client needs two things set up once, both keyed by the same slug:

- **Asset folder (R2):** the first `spaces_write(path="brand-kit.md", client="<slug>")` for a new slug
  creates `clients/<slug>/` on the fly (no operator step needed). For a **full brand pack** the
  requester zipped — social templates, fonts, logos, source files — ingest it through your sandbox
  instead: `curl` the zip, `unzip`, and `rclone copy` the tree to
  `r2:fleet-clients/clients/<slug>/brand/` (see *Your sandbox* in your SOUL). The `spaces_*` tools move
  text and single files, not a zip of binaries.
- **Fleet memory:** `memory_register_client(client="<slug>", display_name="<Name>")` once, so
  `memory_remember`/`memory_search` work for the client (they refuse an unknown slug).

Pick a clean slug: lowercase letters, digits and hyphens only, no spaces, no `..`. Use that same slug
everywhere for the client — its R2 folder AND its `memory_*` calls — so the client's knowledge stays in
one place. If a client already exists, reuse its exact slug rather than inventing a variant (e.g. don't
create `carpet` when `carpet-institute` already exists). Confirm the slug with the requester if there's
any doubt.
