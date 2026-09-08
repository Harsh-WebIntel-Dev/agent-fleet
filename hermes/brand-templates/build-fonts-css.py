#!/usr/bin/env python3
"""Regenerate a client's `fonts.css` — brand typefaces as woff2 data URIs.

Why embed instead of installing: a social card is rendered by headless Chromium in the isolated
sandbox. If the typeface is only available over the network, a render silently substitutes a fallback
face and ships a card that looks *almost* right — the exact defect class this guards against. If it
is only installed in the container, it is lost the moment the sandbox is recreated (the image is
rebuilt rarely and `setup-sandbox.sh` has to be re-run afterwards). Embedding it in a stylesheet that
travels with the templates means the bundle needs nothing at all.

Only the **latin** subset and only the **variable** font is fetched, so one file per family covers
every weight the templates use. Sizes are small enough to sit next to a 2 KB template:
    Jost       100..900                      ~26 KB woff2  -> ~35 KB base64
    Open Sans  300..800, 75-100% width       ~83 KB woff2  -> ~111 KB base64

Keep `fonts.css` NEXT TO the templates — they load it as a relative stylesheet, so an
`rclone copy` of the whole prefix brings it along, and `render-card`'s font preflight reads it.

Usage:  build-fonts-css.py [outfile]        # default: webintelligenz/fonts.css
"""
import base64
import hashlib
import pathlib
import re
import sys
import urllib.request

# A Chrome UA is required or Google serves ttf instead of woff2.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/151.0.0.0 Safari/537.36")

# family -> (css2 query, expected sha256 of the latin variable woff2)
# The hashes pin what we ship. Google re-cuts these files occasionally; a mismatch is not
# necessarily an attack, but it IS a silent change to how every brand card looks, so it stops here
# and asks for a human to re-verify rather than quietly redrawing the brand.
FAMILIES = {
    "Jost": ("Jost:ital,wght@0,100..900",
             "7726a5cd6f3c0e876c028ea2a643d45f7aad4b0f164b70966c669f4a4668f4b9"),
    "Open Sans": ("Open+Sans:ital,wdth,wght@0,75..100,300..800",
                  "9b806284e1e5fb52cf403cc0dcc95f148b483b0bba1299ce67150476c63f617e"),
}


def get(url, as_text=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return raw.decode("utf-8") if as_text else raw


def latin_block(css):
    """The @font-face block Google marks `/* latin */` — the only subset a brand card needs."""
    m = re.search(r"/\* latin \*/\s*(@font-face\s*\{[^}]*\})", css)
    if not m:
        sys.exit("could not find the /* latin */ @font-face block")
    return m.group(1)


out_lines = [
    "/* fonts.css — brand typefaces EMBEDDED as woff2 data URIs. GENERATED, do not hand-edit.",
    " * Regenerate with hermes/brand-templates/build-fonts-css.py (it pins each font by sha256).",
    " * Keep this file next to the card templates: they load it as a relative stylesheet, so an",
    " * `rclone copy` of the prefix brings it along and render-card's preflight can read it.",
    " */",
]

for family, (query, want_sha) in FAMILIES.items():
    css = get("https://fonts.googleapis.com/css2?family=%s&display=swap" % query, as_text=True)
    block = latin_block(css)
    url = re.search(r"src:\s*url\(([^)]+)\)", block).group(1)
    woff2 = get(url)
    got_sha = hashlib.sha256(woff2).hexdigest()
    if got_sha != want_sha:
        sys.exit("%s: sha256 mismatch\n  expected %s\n  got      %s\n"
                 "  Google re-cut this font. Re-render a card, eyeball it, then update the hash."
                 % (family, want_sha, got_sha))
    # Keep Google's own descriptors (weight/stretch ranges), swap the URL for the data URI,
    # and drop unicode-range + font-display: we ship one subset and Chromium blocks on
    # a data URI anyway, so `swap` would only invite a fallback-face flash.
    block = re.sub(r"src:\s*url\([^)]+\)\s*format\('woff2'\);",
                   "src: url(data:font/woff2;base64,%s) format('woff2');"
                   % base64.b64encode(woff2).decode(), block)
    block = re.sub(r"\s*(unicode-range|font-display):[^;]+;", "", block)
    out_lines.append(block)
    print("%-10s %6d B woff2  sha256 ok" % (family, len(woff2)), file=sys.stderr)

dest = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                    else pathlib.Path(__file__).parent / "webintelligenz" / "fonts.css")
dest.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print("wrote %s (%d bytes)" % (dest, dest.stat().st_size), file=sys.stderr)
