"""Independent verification of claimed side effects.

WHY THIS FILE EXISTS — read before changing anything in it.

On 2026-08-18 the `publisher` agent, with NO WordPress or GMB MCP server in existence, returned:

    {"published": true,
     "remote_id": "campaign_b0cb6d80_0132_root-post",
     "url": "https://www.google.com/search?q=demo+co+melbourne&ibnd=R0CB6D80-0132-Root-Post1"}

and it **passed JSON-schema validation cleanly**, because a schema enforces SHAPE, not TRUTH.
`{"type": "string", "minLength": 1}` is satisfied by any invention. Earlier the same day the
`social` agent fabricated Postiz post IDs for work it never did. Two agents, one session.

So "validate, don't trust" was really "validate the shape, then trust". This module closes that:
after a stage claims an external side effect, the RUNNER goes and looks. Verification never lives
in the agent that made the claim — that is just asking a liar to mark its own homework.

Design rules:
  * A verifier answers with evidence it fetched itself, never with anything the model said.
  * Unreachable/ambiguous == FAIL. Fail closed. A publish we cannot confirm did not happen.
  * No network call trusts a redirect to somewhere private — a fabricated `http://localhost:8000`
    would otherwise return 200 from our own service and "verify" successfully.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger("nemoclaw.verification")

VERIFY_TIMEOUT = 20.0

# Shapes that have actually shown up in fabricated output, plus the obvious placeholder domains.
# A real published article never lives at any of these.
_FAKE_HOST_MARKERS = (
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "yoursite.com",
    "yourdomain.com",
    "placeholder",
)
_FAKE_PATH_MARKERS = (
    "/search?",  # the exact google-search-string forgery seen on 2026-08-18
    "/search/",
)


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str
    evidence: dict[str, Any]

    @staticmethod
    def fail(reason: str, **evidence: Any) -> VerificationResult:
        return VerificationResult(False, reason, evidence)

    @staticmethod
    def pass_(reason: str, **evidence: Any) -> VerificationResult:
        return VerificationResult(True, reason, evidence)


class UnverifiableClaim(Exception):
    """A stage claimed a side effect that could not be independently confirmed."""

    def __init__(self, stage_id: str, result: VerificationResult):
        super().__init__(f"stage '{stage_id}' claim unverified: {result.reason}")
        self.stage_id = stage_id
        self.result = result


# ---------------------------------------------------------------- URL sanity


def _is_public_url(url: str) -> tuple[bool, str]:
    """Reject anything that isn't a publicly-resolvable http(s) URL.

    Two separate jobs: catch obvious forgeries cheaply, and make sure a "published" URL can't
    secretly point back inside our own network where almost anything answers 200.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"unparseable URL ({exc})"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' is not http(s)"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    lowered = url.lower()
    for marker in _FAKE_HOST_MARKERS:
        if marker in (host or "").lower():
            return False, f"host looks like a placeholder ('{marker}')"
    for marker in _FAKE_PATH_MARKERS:
        if marker in lowered:
            return False, (
                "URL is a search-results link, not a published page — this is the exact shape "
                "of a previously observed fabrication"
            )

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"host does not resolve ({exc})"

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return False, f"host resolves to a non-public address ({addr})"

    return True, "public http(s) URL"


def _fetch(url: str, *, method: str = "GET") -> httpx.Response:
    # follow_redirects=False on purpose: we re-check every hop against _is_public_url rather than
    # letting httpx quietly land us somewhere internal.
    with httpx.Client(timeout=VERIFY_TIMEOUT, follow_redirects=False) as c:
        resp = c.request(method, url, headers={"User-Agent": "NemoClaw-Verifier/1.0"})
        hops = 0
        while resp.is_redirect and hops < 5:
            nxt = str(resp.next_request.url) if resp.next_request else ""
            ok, why = _is_public_url(nxt)
            if not ok:
                raise httpx.RequestError(f"redirect to non-public target: {why}")
            resp = c.request(method, nxt, headers={"User-Agent": "NemoClaw-Verifier/1.0"})
            hops += 1
        return resp


# ---------------------------------------------------------------- image bytes


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Real width/height straight out of the file header. No Pillow dependency.

    Only the formats our image agents actually produce. Anything else returns None and the caller
    treats it as unverifiable rather than guessing.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)

    if data[:2] == b"\xff\xd8":  # JPEG: walk the marker chain to a start-of-frame
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
        return None

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if data[12:16] == b"VP8 ":
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return int(w), int(h)
    return None


# ---------------------------------------------------------------- verifiers


def verify_publish_result(output: dict[str, Any]) -> VerificationResult:
    """`published: true` means nothing on its own. Go and load the page."""
    if not output.get("published"):
        return VerificationResult.pass_("stage did not claim publication")

    url = (output.get("url") or "").strip()
    remote_id = (output.get("remote_id") or "").strip()
    if not url:
        return VerificationResult.fail("claimed published but returned no url")
    if not remote_id:
        return VerificationResult.fail("claimed published but returned no remote_id")

    ok, why = _is_public_url(url)
    if not ok:
        return VerificationResult.fail(f"claimed url is not a real published page: {why}", url=url)

    try:
        resp = _fetch(url)
    except httpx.RequestError as exc:
        return VerificationResult.fail(f"claimed url could not be fetched: {exc}", url=url)

    if resp.status_code != 200:
        return VerificationResult.fail(
            f"claimed url returned HTTP {resp.status_code}, not 200", url=url,
            status=resp.status_code,
        )

    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype.lower():
        return VerificationResult.fail(
            f"claimed url served '{ctype}', not an HTML page", url=url, content_type=ctype
        )

    return VerificationResult.pass_(
        "published url fetched and returned 200 HTML",
        url=url, remote_id=remote_id, bytes=len(resp.content),
    )


def verify_image_set(output: dict[str, Any]) -> VerificationResult:
    """Download each image and compare REAL pixel dimensions against the claimed ones.

    Declared width/height was supposed to be the "hard to fake" field. It isn't — a model invents
    `1920` as easily as anything else. Actually decoding the header is what makes it hard to fake.
    """
    images = output.get("images") or []
    if not images:
        return VerificationResult.fail("image_set contained no images")

    checked: list[dict[str, Any]] = []
    for img in images:
        url = (img.get("url") or "").strip()
        slot = img.get("slot", "?")
        ok, why = _is_public_url(url)
        if not ok:
            return VerificationResult.fail(f"image '{slot}' url is not fetchable: {why}", url=url)

        try:
            resp = _fetch(url)
        except httpx.RequestError as exc:
            return VerificationResult.fail(f"image '{slot}' could not be fetched: {exc}", url=url)

        if resp.status_code != 200:
            return VerificationResult.fail(
                f"image '{slot}' returned HTTP {resp.status_code}", url=url
            )

        dims = _image_dimensions(resp.content)
        if dims is None:
            return VerificationResult.fail(
                f"image '{slot}' is not a decodable PNG/JPEG/WebP "
                f"({len(resp.content)} bytes, content-type '{resp.headers.get('content-type')}')",
                url=url,
            )

        real_w, real_h = dims
        if (real_w, real_h) != (img.get("width"), img.get("height")):
            return VerificationResult.fail(
                f"image '{slot}' claimed {img.get('width')}x{img.get('height')} but the actual "
                f"file is {real_w}x{real_h}",
                url=url, claimed=[img.get("width"), img.get("height")], actual=[real_w, real_h],
            )
        checked.append({"slot": slot, "url": url, "dimensions": [real_w, real_h]})

    return VerificationResult.pass_(f"{len(checked)} image(s) fetched and dimensions matched",
                                    images=checked)


VERIFIERS: dict[str, Callable[[dict[str, Any]], VerificationResult]] = {
    "publish_result": verify_publish_result,
    "image_set": verify_image_set,
}
