"""MCP server giving the fleet `publisher` agent a minimal, honest WordPress surface over the official
WP REST API (wp-json).

WHY THIS EXISTS (and why NOT the official Automattic wordpress-mcp plugin)
The fleet publisher must: create a blog DRAFT on the client's WordPress, set its Yoast SEO title/meta,
attach a hero image, hand a review link to the client, and PUBLISH only after human approval -- returning
a real, verifiable live URL. The official wordpress-mcp plugin authenticates its MCP endpoint with
short-lived JWTs only; application-password auth does not set the user there, so a standing fleet
integration would need a daily token-refresh dance. The standard wp-json REST API, by contrast, accepts
the content-bot application password directly (proven). So this is a thin wrapper over the OFFICIAL REST
API with Basic app-password auth -- the same mechanism the retired marketing-agent bridge ran in prod.

HONESTY / ANTI-CONFABULATION
Every field returned comes from the WordPress API response (post id, permalink) or from an independent
fetch this server performs (publish re-fetches the live URL and reports whether it is a real 200). It
NEVER invents a post id or URL. A failed publish is an honest error, not a fabricated success -- the same
contract the fleet runner enforces on its side.

TENANCY
Single site (webintelligenz.com, client #1) via WP_SITE_URL / WP_USERNAME / WP_APP_PASSWORD env. When more
clients onboard, pin the target site + credentials per client via a gateway static header (same pattern as
mcp-a2a) -- never model-chosen.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer, Context

WP_SITE_URL = os.environ.get("WP_SITE_URL", "https://webintelligenz.com").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "content-bot")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
HTTP_TIMEOUT = float(os.environ.get("WP_HTTP_TIMEOUT", "30"))
MEDIA_MAX_BYTES = int(os.environ.get("WP_MEDIA_MAX_BYTES", str(12 * 1024 * 1024)))  # 12 MB hero cap

if urllib.parse.urlparse(WP_SITE_URL).scheme != "https":
    raise SystemExit(f"WP_SITE_URL must be https, got {WP_SITE_URL!r}")

mcp = MCPServer(
    name="wordpress",
    instructions=(
        "Publish to WordPress over the official REST API. Typical blog flow: wp_upload_media(image_url) to "
        "get a media_id, then wp_create_draft(title, content_html, seo_title, seo_description, "
        "focus_keyword, category, featured_media_id) which returns the post_id + an edit_link to send the "
        "client for review. On QA/review changes, wp_update_post(post_id, ...). ONLY after human approval, "
        "wp_publish(post_id) -- it sets the post live and independently re-fetches the URL to confirm it is "
        "a real 200 (a Cloudflare cache may lag; 'published' reflects WordPress, 'verified_live' the "
        "fetch). account_status is a health/identity check. Report only the real post_id and url the API "
        "returned -- never invent them; a failed call is an honest failure, not a placeholder."
    ),
)


class WpError(RuntimeError):
    pass


def _auth_header() -> str:
    if not WP_APP_PASSWORD:
        raise WpError("WP_APP_PASSWORD is not set")
    # WordPress shows app passwords with spaces for readability and strips them on auth; do the same.
    token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD.replace(' ', '')}".encode()).decode()
    return f"Basic {token}"


def _wp_api(method: str, path: str, payload: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None) -> tuple[int, Any]:
    """Authenticated JSON call to the client's own WP REST API (host fixed by env, not model input).
    Returns (status, parsed_json)."""
    url = WP_SITE_URL + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "nemoclaw-wp/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": body[:500]}


def _wp_upload(raw: bytes, content_type: str, filename: str) -> tuple[int, Any]:
    """Raw-body POST to /wp/v2/media (the media endpoint wants bytes + Content-Disposition, not JSON)."""
    req = urllib.request.Request(WP_SITE_URL + "/wp-json/wp/v2/media", data=raw, method="POST")
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "nemoclaw-wp/1.0")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": body[:500]}


def _safe_get(url: str, headers: dict[str, str] | None = None,
              timeout: float = 20.0, max_bytes: int = 200000) -> tuple[int, bytes]:
    """SSRF-safe GET for model-supplied URLs (image sources, published-page verification). Guards the
    scheme, resolves the host ONCE and pins a verified-public IP (no DNS TOCTOU), preserves Host + TLS
    SNI, and REFUSES redirects (a 3xx raises rather than silently following to an internal host). Returns
    (status, body). Raises ValueError on a disallowed URL or a redirect. (Verbatim from mcp-higgsfield.)"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned = None
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"refusing URL resolving to non-public address {info[4][0]}")
        if pinned is None:
            pinned = info[4][0]
    if pinned is None:
        raise ValueError(f"could not resolve {host}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    raw = socket.create_connection((pinned, port), timeout=timeout)
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) \
            if parsed.scheme == "https" else raw
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request("GET", path, headers={"Connection": "close", **(headers or {})})
        resp = conn.getresponse()
        if 300 <= resp.status < 400:
            raise ValueError(f"refusing redirect ({resp.status}) to {resp.getheader('Location')!r}")
        return resp.status, resp.read(max_bytes)
    finally:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass


def _sniff_image(body: bytes) -> tuple[str, str] | None:
    """Identify image type from magic bytes (never trust a Content-Type header). Returns
    (content_type, extension) or None for anything not a known raster image -- we refuse to upload it."""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _resolve_category(name: str) -> tuple[int | None, list[str]]:
    """Find an EXISTING category by name (case-insensitive). content_bot can assign existing terms
    (assign_terms == edit_posts) but cannot create them, so an unknown name is an honest error listing
    what exists, never a silent new term."""
    status, data = _wp_api("GET", "/wp-json/wp/v2/categories",
                           params={"per_page": 100, "search": name})
    available: list[str] = []
    if status == 200 and isinstance(data, list):
        for term in data:
            available.append(term.get("name", ""))
            if str(term.get("name", "")).strip().lower() == name.strip().lower():
                return int(term.get("id")), available
    return None, available


@mcp.tool()
def account_status(ctx: Context) -> dict[str, Any]:
    """Health + identity check: GET /wp/v2/users/me. Confirms the app password authenticates and shows
    whether this user can publish. Returns {ok, user_id, name, roles, can_publish, site}."""
    status, data = _wp_api("GET", "/wp-json/wp/v2/users/me", params={"context": "edit"})
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "status": status, "error": (data or {}).get("code", "auth_failed"),
                "detail": str(data)[:300], "site": WP_SITE_URL}
    caps = data.get("capabilities") or {}
    return {"ok": True, "user_id": data.get("id"), "name": data.get("name"),
            "roles": data.get("roles"), "can_publish": bool(caps.get("publish_posts")),
            "site": WP_SITE_URL}


@mcp.tool()
def wp_list_categories(ctx: Context, search: str = "") -> dict[str, Any]:
    """List existing post categories (id, name, slug), optionally filtered by `search`. Use to pick the
    category name to pass to wp_create_draft."""
    params: dict[str, Any] = {"per_page": 100}
    if search.strip():
        params["search"] = search.strip()
    status, data = _wp_api("GET", "/wp-json/wp/v2/categories", params=params)
    if status != 200 or not isinstance(data, list):
        return {"ok": False, "status": status, "detail": str(data)[:300]}
    return {"ok": True, "categories": [{"id": t.get("id"), "name": t.get("name"),
                                        "slug": t.get("slug")} for t in data]}


@mcp.tool()
def wp_upload_media(ctx: Context, image_url: str, filename: str = "", alt_text: str = "") -> dict[str, Any]:
    """Fetch an image from `image_url` (SSRF-safe, raster images only) and upload it to the WP media
    library. Returns {ok, media_id, source_url}. Pass media_id to wp_create_draft as featured_media_id.
    A failed fetch/upload is an honest error -- it never returns a made-up media_id."""
    if not (image_url or "").strip():
        return {"ok": False, "error": "empty", "detail": "image_url is required"}
    try:
        status, body = _safe_get(image_url, {"User-Agent": "nemoclaw-wp/1.0"},
                                 timeout=HTTP_TIMEOUT, max_bytes=MEDIA_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "fetch_failed", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if status != 200:
        return {"ok": False, "error": f"fetch_http_{status}", "detail": f"image source returned {status}"}
    sniff = _sniff_image(body)
    if not sniff:
        return {"ok": False, "error": "not_an_image",
                "detail": "source is not a PNG/JPEG/GIF/WebP; refusing to upload"}
    content_type, ext = sniff
    if not filename.strip():
        base = os.path.basename(urllib.parse.urlparse(image_url).path) or "image"
        filename = base if base.lower().endswith(f".{ext}") else f"{base.split('.')[0] or 'image'}.{ext}"
    up_status, up = _wp_upload(body, content_type, filename)
    if up_status not in (200, 201) or not isinstance(up, dict) or not up.get("id"):
        return {"ok": False, "status": up_status, "error": (up or {}).get("code", "upload_failed"),
                "detail": str(up)[:400]}
    media_id = int(up["id"])
    if alt_text.strip():
        _wp_api("POST", f"/wp-json/wp/v2/media/{media_id}", {"alt_text": alt_text.strip()})
    return {"ok": True, "media_id": media_id, "source_url": up.get("source_url")}


def _seo_meta(seo_title: str, seo_description: str, focus_keyword: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if seo_title.strip():
        meta["_yoast_wpseo_title"] = seo_title.strip()
    if seo_description.strip():
        meta["_yoast_wpseo_metadesc"] = seo_description.strip()
    if focus_keyword.strip():
        meta["_yoast_wpseo_focuskw"] = focus_keyword.strip()
    return meta


@mcp.tool()
def wp_create_draft(ctx: Context, title: str, content_html: str, seo_title: str = "",
                    seo_description: str = "", focus_keyword: str = "", category: str = "",
                    featured_media_id: int = 0) -> dict[str, Any]:
    """Create a blog post as a DRAFT (never published here). Sets the Yoast SEO title/meta/focus keyword,
    assigns an existing category by name (optional), and sets a featured image (optional). Returns the
    real {post_id, status, edit_link, will_be_live_at}. Send edit_link to the client for review; publish
    only after approval via wp_publish."""
    if not title.strip() or not content_html.strip():
        return {"ok": False, "error": "missing", "detail": "title and content_html are required"}
    payload: dict[str, Any] = {"title": title, "content": content_html, "status": "draft"}
    meta = _seo_meta(seo_title, seo_description, focus_keyword)
    if meta:
        payload["meta"] = meta
    if featured_media_id:
        payload["featured_media"] = int(featured_media_id)
    if category.strip():
        cid, available = _resolve_category(category)
        if cid is None:
            return {"ok": False, "error": "category_not_found",
                    "detail": f"'{category}' not found; existing near match: {available[:15]}"}
        payload["categories"] = [cid]
    status, data = _wp_api("POST", "/wp-json/wp/v2/posts", payload)
    if status not in (200, 201) or not isinstance(data, dict) or not data.get("id"):
        return {"ok": False, "status": status, "error": (data or {}).get("code", "create_failed"),
                "detail": str(data)[:400]}
    pid = int(data["id"])
    return {"ok": True, "post_id": pid, "status": data.get("status"),
            "edit_link": f"{WP_SITE_URL}/wp-admin/post.php?post={pid}&action=edit",
            "will_be_live_at": data.get("link"), "seo_set": list(meta.keys())}


@mcp.tool()
def wp_update_post(ctx: Context, post_id: int, title: str = "", content_html: str = "",
                   seo_title: str = "", seo_description: str = "", focus_keyword: str = "",
                   category: str = "", featured_media_id: int = 0) -> dict[str, Any]:
    """Update an existing draft during the QA/review loop. Only the fields you pass are changed. Returns
    {ok, post_id, status, edit_link}."""
    if not post_id:
        return {"ok": False, "error": "missing", "detail": "post_id is required"}
    payload: dict[str, Any] = {}
    if title.strip():
        payload["title"] = title
    if content_html.strip():
        payload["content"] = content_html
    meta = _seo_meta(seo_title, seo_description, focus_keyword)
    if meta:
        payload["meta"] = meta
    if featured_media_id:
        payload["featured_media"] = int(featured_media_id)
    if category.strip():
        cid, available = _resolve_category(category)
        if cid is None:
            return {"ok": False, "error": "category_not_found",
                    "detail": f"'{category}' not found; existing near match: {available[:15]}"}
        payload["categories"] = [cid]
    if not payload:
        return {"ok": False, "error": "nothing_to_update", "detail": "pass at least one field"}
    status, data = _wp_api("POST", f"/wp-json/wp/v2/posts/{int(post_id)}", payload)
    if status not in (200, 201) or not isinstance(data, dict) or not data.get("id"):
        return {"ok": False, "status": status, "error": (data or {}).get("code", "update_failed"),
                "detail": str(data)[:400]}
    pid = int(data["id"])
    return {"ok": True, "post_id": pid, "status": data.get("status"),
            "edit_link": f"{WP_SITE_URL}/wp-admin/post.php?post={pid}&action=edit"}


@mcp.tool()
def wp_get_post(ctx: Context, post_id: int) -> dict[str, Any]:
    """Fetch a post's current state (any status) for review or verification. Returns {ok, post_id,
    status, title, link, seo_title, seo_description}."""
    if not post_id:
        return {"ok": False, "error": "missing", "detail": "post_id is required"}
    status, data = _wp_api("GET", f"/wp-json/wp/v2/posts/{int(post_id)}", params={"context": "edit"})
    if status != 200 or not isinstance(data, dict) or not data.get("id"):
        return {"ok": False, "status": status, "error": (data or {}).get("code", "not_found"),
                "detail": str(data)[:300]}
    meta = data.get("meta") or {}
    title = data.get("title") or {}
    return {"ok": True, "post_id": int(data["id"]), "status": data.get("status"),
            "title": title.get("raw") or title.get("rendered"), "link": data.get("link"),
            "seo_title": meta.get("_yoast_wpseo_title", ""),
            "seo_description": meta.get("_yoast_wpseo_metadesc", "")}


@mcp.tool()
def wp_publish(ctx: Context, post_id: int) -> dict[str, Any]:
    """Set a post live (status=publish). ONLY call after human approval -- the publish gate is the fleet
    runner's job, not this tool's. Independently re-fetches the live URL and reports whether it is a real
    200: `published` reflects WordPress's own status; `verified_live` reflects the fetch (a Cloudflare
    cache can lag a publish, and a human must purge Cloudflare). Returns {ok, published, post_id, url,
    wp_status, verified_live, verify_detail}."""
    if not post_id:
        return {"ok": False, "error": "missing", "detail": "post_id is required"}
    status, data = _wp_api("POST", f"/wp-json/wp/v2/posts/{int(post_id)}", {"status": "publish"})
    if status not in (200, 201) or not isinstance(data, dict):
        return {"ok": False, "published": False, "status": status,
                "error": (data or {}).get("code", "publish_failed"), "detail": str(data)[:400]}
    wp_status = data.get("status")
    url = data.get("link") or ""
    verified, detail = False, ""
    try:
        cb = hashlib.sha256(url.encode()).hexdigest()[:10]
        sep = "&" if "?" in url else "?"
        st, body = _safe_get(f"{url}{sep}cb={cb}",
                             {"User-Agent": "nemoclaw-verify/1.0", "Cache-Control": "no-cache"},
                             timeout=20, max_bytes=300000)
        html = body.decode(errors="replace").lower()
        verified = st == 200 and ("<html" in html or "<!doctype html" in html)
        detail = f"http {st}"
    except Exception as exc:  # noqa: BLE001
        detail = f"verify_inconclusive: {type(exc).__name__}: {str(exc)[:150]}"
    published = wp_status == "publish"
    return {"ok": published, "published": published, "post_id": int(post_id), "url": url,
            "wp_status": wp_status, "verified_live": verified, "verify_detail": detail}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")), streamable_http_path="/mcp",
            stateless_http=True)
