"""MCP server over the OFFICIAL Lnk.Bio REST API -- maintains the Web Intelligenz Instagram bio page as a
ROLLING TOP-5 of links.

WEB INTELLIGENZ ONLY. Lnk.Bio (lnk.bio/webintelligenz_au) is WI's own link-in-bio page; no other client
has one, so this MCP must NOT be exposed to the client keys. Instagram captions can't carry clickable
links, so IG posts say "link in bio" and the URL is pushed here. When a new blog post or service page
goes live, `lnkbio_set_link` adds it and trims the page to the 5 most-recent (dropping the oldest).

API is Lnk.Bio Private App, client_credentials grant (1h token minted per call, never stored). Singular
paths (/lnk/list|add|delete) -- the plural /lnks 302s on this account. Groups are paywalled, so links are
a flat list ordered by `position`. HONESTY: every field comes from the API; a failed call is an honest
error, never a fabricated success. Refuses to add a url that isn't a live public https 200.
"""

from __future__ import annotations

import base64
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

TOKEN_URL = "https://lnk.bio/oauth/token"
BASE = "https://lnk.bio/oauth/v1"
LNKBIO_ID = os.environ.get("LNKBIO_CLIENT_ID", "")
LNKBIO_SECRET = os.environ.get("LNKBIO_CLIENT_SECRET", "")
LNKBIO_PROFILE = os.environ.get("LNKBIO_PROFILE", "webintelligenz_au")
LNKBIO_KEEP = int(os.environ.get("LNKBIO_KEEP", "5"))  # rolling window size
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
UA = "nemoclaw-lnkbio/1.0"

mcp = MCPServer(
    name="lnkbio",
    instructions=(
        "Maintain the Web Intelligenz Instagram bio page (lnk.bio) as a rolling top-5. lnkbio_list shows "
        "the current links; lnkbio_set_link(url, title) adds a link when a new blog post or service page "
        "goes live and drops the oldest so only the 5 most-recent remain. WEB INTELLIGENZ ONLY. Reports "
        "only what the API returned; a failed call is an honest error."
    ),
)


def _token() -> str | None:
    if not LNKBIO_ID or not LNKBIO_SECRET:
        return None
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": "Basic " + base64.b64encode(f"{LNKBIO_ID}:{LNKBIO_SECRET}".encode()).decode(),
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp).get("access_token")
    except Exception:  # noqa: BLE001
        return None


def _lnk(path: str, params: dict | None = None, method: str = "GET") -> tuple[int, Any]:
    tok = _token()
    if not tok:
        return 0, {"error": "Lnk.Bio auth failed (LNKBIO_CLIENT_ID/SECRET not set or rejected)"}
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
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
    """SSRF-safe GET: refuse a bio link that isn't a live public https 200 (a dead bio link is worse than
    none). Resolves once, pins a public IP, follows no redirects."""
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
    """List the links currently on the Web Intelligenz Instagram bio page (lnk.bio). Returns {ok, profile,
    count, links:[{id, position, title, link}]} ordered by position (lowest = oldest)."""
    st, data = _lnk("/lnk/list")
    if st != 200 or not isinstance(data, dict) or not data.get("status", False):
        return {"ok": False, "status": st, "detail": str(data)[:400]}
    rows = sorted((data.get("data") or []), key=lambda r: r.get("position", 0))
    return {"ok": True, "profile": f"https://lnk.bio/{LNKBIO_PROFILE}", "count": len(rows),
            "links": [{"id": r.get("id"), "position": r.get("position"),
                       "title": r.get("title"), "link": r.get("link")} for r in rows]}


@mcp.tool()
def lnkbio_set_link(ctx: Context, url: str, title: str) -> dict[str, Any]:
    """Add a link to the WI Instagram bio page and keep it a ROLLING TOP-5: after adding, links beyond the
    5 most-recent are deleted (oldest first). Use when a new blog post or service page goes live. Refuses a
    url that is not a live https 200; skips if already present. Returns {ok, added|skipped, kept:[titles],
    dropped:[titles], profile}."""
    if not url.strip().startswith("https://") or not title.strip():
        return {"ok": False, "error": "url (https) and title are required"}
    if not _safe_head_ok(url.strip()):
        return {"ok": False, "error": "url_not_live",
                "detail": f"{url} did not return a public https 200 -- refusing to add a dead bio link"}
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
    # re-list, then trim to the newest LNKBIO_KEEP -- delete lowest positions (oldest) first.
    st2, after = _lnk("/lnk/list")
    rows2 = sorted((after.get("data") or []), key=lambda r: r.get("position", 0)) if st2 == 200 else []
    dropped = []
    if len(rows2) > LNKBIO_KEEP:
        for r in rows2[:len(rows2) - LNKBIO_KEEP]:
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
