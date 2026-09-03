"""MCP server exposing the post-management operations the fleet's official MCPs OMIT.

WHY THIS EXISTS
- Postiz's built-in MCP (/api/mcp) is create-only for posts: it can create a draft/scheduled/now post,
  but has NO tool to LIST, DELETE, change the STATUS of, or EDIT an existing post by id (verified in the
  Postiz source across v2.22.1..main). Those operations DO exist on Postiz's own public REST API, so this
  is a thin, honest wrapper over the OFFICIAL Postiz public API (auth = the same POSTIZ_MCP_TOKEN as the
  built-in MCP, sent RAW in Authorization -- not Bearer, per this instance).
- Lnk.Bio has no MCP at all -- only a public REST API. `lnkbio_set_link` maintains the Instagram bio page
  as a ROLLING TOP-5: it adds a link and trims to the 5 most-recent, dropping the oldest.

HONESTY: every field returned comes from the vendor API response; a failed call is an honest error, never
a fabricated success. Nothing is published/scheduled by these tools except an explicit status change the
caller asks for.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import ssl
import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer, Context

# --- Postiz (self-hosted) public API ---
POSTIZ_BASE = os.environ.get("POSTIZ_API_URL", "https://postiz.widev.com.au/api/public/v1").rstrip("/")
POSTIZ_TOKEN = os.environ.get("POSTIZ_MCP_TOKEN", "")
# --- Lnk.Bio (link-in-bio) ---
LNKBIO_TOKEN_URL = "https://lnk.bio/oauth/token"
LNKBIO_BASE = "https://lnk.bio/oauth/v1"
LNKBIO_ID = os.environ.get("LNKBIO_CLIENT_ID", "")
LNKBIO_SECRET = os.environ.get("LNKBIO_CLIENT_SECRET", "")
LNKBIO_PROFILE = os.environ.get("LNKBIO_PROFILE", "webintelligenz_au")
LNKBIO_KEEP = int(os.environ.get("LNKBIO_KEEP", "5"))  # rolling window size
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
UA = "nemoclaw-social-extras/1.0"

mcp = MCPServer(
    name="social_extras",
    instructions=(
        "Manage EXISTING social posts that the create-only Postiz MCP cannot: postiz_list (find ids), "
        "postiz_delete (remove a post), postiz_set_status (draft<->schedule = un-queue / queue at its "
        "stored date), postiz_edit (change content and/or date of an unpublished post). To 'edit' beyond "
        "that, delete + create anew. lnkbio_set_link maintains the Instagram bio page as a rolling top-5 "
        "(adds the link, drops the oldest); lnkbio_list shows the current links. Report only real ids the "
        "API returned; a failed call is an honest error, never a fabricated success."
    ),
)


# ------------------------- Postiz helpers -------------------------
def _postiz(method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
    if not POSTIZ_TOKEN:
        return 0, {"error": "POSTIZ_MCP_TOKEN not set"}
    url = POSTIZ_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", POSTIZ_TOKEN)  # RAW token, not Bearer (this instance)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
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


def _find_post(post_id: str, start_date: str, end_date: str) -> dict | None:
    st, data = _postiz("GET", f"/posts?startDate={urllib.parse.quote(start_date)}"
                              f"&endDate={urllib.parse.quote(end_date)}")
    if st != 200 or not isinstance(data, dict):
        return None
    for p in (data.get("posts") or []):
        if p.get("id") == post_id:
            return p
    return None


@mcp.tool()
def postiz_list(ctx: Context, start_date: str, end_date: str) -> dict[str, Any]:
    """List the organization's Postiz posts scheduled between two ISO datetimes (both required, e.g.
    2026-09-01T00:00:00.000Z). Returns each post's {id, group, state (DRAFT/QUEUE/PUBLISHED/ERROR),
    date, platform, snippet} so you can pick the id to delete/edit/re-status."""
    if not start_date or not end_date:
        return {"ok": False, "error": "start_date and end_date (ISO) are required"}
    st, data = _postiz("GET", f"/posts?startDate={urllib.parse.quote(start_date)}"
                              f"&endDate={urllib.parse.quote(end_date)}")
    if st != 200 or not isinstance(data, dict):
        return {"ok": False, "status": st, "detail": str(data)[:400]}
    out = []
    for p in (data.get("posts") or []):
        integ = p.get("integration") or {}
        content = p.get("content")
        if not isinstance(content, str):
            content = ""
        out.append({"id": p.get("id"), "group": p.get("group"), "state": p.get("state"),
                    "date": p.get("publishDate"),
                    "platform": integ.get("providerIdentifier") or integ.get("provider"),
                    "snippet": content.replace("\n", " ")[:80]})
    return {"ok": True, "count": len(out), "posts": out}


@mcp.tool()
def postiz_delete(ctx: Context, post_id: str) -> dict[str, Any]:
    """Permanently delete a Postiz post by id (use this to 'remove' a post, or as the first half of an
    edit-by-recreate). 404 means it was already gone (safe). Returns {ok, deleted}."""
    if not post_id.strip():
        return {"ok": False, "error": "post_id is required"}
    st, data = _postiz("DELETE", f"/posts/{urllib.parse.quote(post_id.strip())}")
    if st in (200, 201):
        return {"ok": True, "deleted": post_id}
    if st == 404:
        return {"ok": True, "deleted": post_id, "note": "already gone (404)"}
    return {"ok": False, "status": st, "detail": str(data)[:400]}


@mcp.tool()
def postiz_set_status(ctx: Context, post_id: str, status: str) -> dict[str, Any]:
    """Change an existing post's status. status='schedule' QUEUES a draft to publish at its stored date
    (arming); status='draft' pulls a queued post back to a draft so it will NOT publish (un-arming). Only
    'draft' or 'schedule' are valid; published posts cannot be changed. Returns {ok, post_id, status}."""
    status = status.strip().lower()
    if status not in ("draft", "schedule"):
        return {"ok": False, "error": "status must be 'draft' or 'schedule'"}
    if not post_id.strip():
        return {"ok": False, "error": "post_id is required"}
    st, data = _postiz("PUT", f"/posts/{urllib.parse.quote(post_id.strip())}/status", {"status": status})
    if st in (200, 201):
        return {"ok": True, "post_id": post_id, "status": status,
                "state": "QUEUE" if status == "schedule" else "DRAFT"}
    return {"ok": False, "status": st, "detail": str(data)[:400]}


@mcp.tool()
def postiz_edit(ctx: Context, post_id: str, window_start: str, window_end: str,
                new_content: str = "", new_date: str = "") -> dict[str, Any]:
    """Edit an EXISTING unpublished post in place, preserving its id. Pass the post_id plus a date window
    (window_start/window_end, ISO) that contains it so the current post can be looked up. Change the copy
    (new_content) and/or the scheduled time (new_date, ISO UTC); omit a field to leave it unchanged. Only
    DRAFT/QUEUE posts can be edited. If in-place edit is rejected, delete + create anew instead. Returns
    {ok, post_id, edited}."""
    if not post_id.strip():
        return {"ok": False, "error": "post_id is required"}
    if not new_content.strip() and not new_date.strip():
        return {"ok": False, "error": "pass new_content and/or new_date"}
    post = _find_post(post_id.strip(), window_start, window_end)
    if not post:
        return {"ok": False, "error": "post_not_found",
                "detail": "no post with that id in the given window; widen the window or check the id"}
    if post.get("state") == "PUBLISHED":
        return {"ok": False, "error": "already_published", "detail": "published posts cannot be edited"}
    integ = post.get("integration") or {}
    settings = post.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:  # noqa: BLE001
            settings = {}
    settings = settings or {}
    settings["__type"] = integ.get("providerIdentifier")
    # preserve existing images on the post's value entry
    images = post.get("image") or []
    content = new_content if new_content.strip() else (post.get("content") or "")
    body = {
        "type": "update",
        "date": new_date.strip() or post.get("publishDate"),
        "shortLink": False,
        "tags": post.get("tags") or [],
        "group": post.get("group"),
        "posts": [{
            "group": post.get("group"),
            "integration": {"id": integ.get("id")},
            "value": [{"content": content, "image": images}],
            "settings": settings,
        }],
    }
    st, data = _postiz("POST", "/posts", body)
    if st in (200, 201):
        return {"ok": True, "post_id": post_id, "edited": {"content": bool(new_content.strip()),
                "date": new_date.strip() or None}, "state": post.get("state")}
    return {"ok": False, "status": st, "detail": str(data)[:500],
            "hint": "if the API rejects in-place update, use postiz_delete + create a new post"}


# ------------------------- Lnk.Bio helpers -------------------------
def _lnk_token() -> str | None:
    if not LNKBIO_ID or not LNKBIO_SECRET:
        return None
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(LNKBIO_TOKEN_URL, data=body, headers={
        "Authorization": "Basic " + base64.b64encode(f"{LNKBIO_ID}:{LNKBIO_SECRET}".encode()).decode(),
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp).get("access_token")
    except Exception:  # noqa: BLE001
        return None


def _lnk(path: str, params: dict | None = None, method: str = "GET") -> tuple[int, Any]:
    tok = _lnk_token()
    if not tok:
        return 0, {"error": "Lnk.Bio auth failed (LNKBIO_CLIENT_ID/SECRET not set or rejected)"}
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(f"{LNKBIO_BASE}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": body[:400]}


def _safe_head_ok(url: str) -> bool:
    """SSRF-safe HEAD: refuse a bio link that isn't a live public https 200 (a dead bio link is worse
    than none). Resolves once, pins a public IP, no redirects followed."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except Exception:  # noqa: BLE001
        return False
    pinned = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
        pinned = pinned or info[4][0]
    if not pinned:
        return False
    path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    raw = None
    try:
        raw = socket.create_connection((pinned, 443), timeout=20)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        conn = http.client.HTTPConnection(parsed.hostname, 443, timeout=20)
        conn.sock = sock
        conn.request("GET", path, headers={"Connection": "close", "User-Agent": UA})
        return conn.getresponse().status == 200
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            raw and raw.close()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool()
def lnkbio_list(ctx: Context) -> dict[str, Any]:
    """List the links currently on the Instagram bio page (lnk.bio). Returns {ok, profile, count, links:
    [{id, position, title, link}]} ordered by position."""
    st, data = _lnk("/lnk/list")
    if st != 200 or not isinstance(data, dict) or not data.get("status", False):
        return {"ok": False, "status": st, "detail": str(data)[:400]}
    rows = sorted((data.get("data") or []), key=lambda r: r.get("position", 0))
    return {"ok": True, "profile": f"https://lnk.bio/{LNKBIO_PROFILE}", "count": len(rows),
            "links": [{"id": r.get("id"), "position": r.get("position"),
                       "title": r.get("title"), "link": r.get("link")} for r in rows]}


@mcp.tool()
def lnkbio_set_link(ctx: Context, url: str, title: str) -> dict[str, Any]:
    """Add a link to the Instagram bio page and keep it a ROLLING TOP-5: after adding, the oldest links
    beyond the 5 most-recent are deleted. Use when a new blog post or service page goes live. Refuses a
    url that is not a live https 200 (a dead bio link is worse than none); skips if already present.
    Returns {ok, added|skipped, kept:[titles], dropped:[titles], profile}."""
    if not url.strip().startswith("https://") or not title.strip():
        return {"ok": False, "error": "url (https) and title are required"}
    if not _safe_head_ok(url.strip()):
        return {"ok": False, "error": "url_not_live",
                "detail": f"{url} did not return a public https 200 — refusing to add a dead bio link"}
    st, cur = _lnk("/lnk/list")
    if st != 200 or not isinstance(cur, dict) or not cur.get("status", False):
        return {"ok": False, "status": st, "detail": f"list failed: {str(cur)[:300]}"}
    rows = cur.get("data") or []
    if any((r.get("link") or "").rstrip("/") == url.strip().rstrip("/") for r in rows):
        return {"ok": True, "skipped": "already on the page", "url": url,
                "profile": f"https://lnk.bio/{LNKBIO_PROFILE}"}
    add_st, add = _lnk("/lnk/add", {"link": url.strip(), "title": title.strip()}, "POST")
    if add_st not in (200, 201) or not isinstance(add, dict) or not add.get("status", False):
        return {"ok": False, "status": add_st, "detail": f"add failed: {str(add)[:300]}"}
    # re-list and trim to the newest LNKBIO_KEEP (higher position == newer here; verified on this account)
    st2, after = _lnk("/lnk/list")
    rows2 = sorted((after.get("data") or []), key=lambda r: r.get("position", 0)) if st2 == 200 else []
    dropped = []
    if len(rows2) > LNKBIO_KEEP:
        for r in rows2[:len(rows2) - LNKBIO_KEEP]:  # lowest positions = oldest
            d_st, _ = _lnk("/lnk/delete", {"id": r.get("id")}, "DELETE")
            if d_st in (200, 201):
                dropped.append(r.get("title"))
        st2, after = _lnk("/lnk/list")
        rows2 = sorted((after.get("data") or []), key=lambda r: r.get("position", 0)) if st2 == 200 else rows2
    return {"ok": True, "added": title, "url": url, "dropped": dropped,
            "kept": [r.get("title") for r in rows2], "profile": f"https://lnk.bio/{LNKBIO_PROFILE}"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")), streamable_http_path="/mcp",
            stateless_http=True)
