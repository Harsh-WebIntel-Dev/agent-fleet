"""MCP server exposing the Postiz post-management operations the built-in Postiz MCP OMITS.

Postiz's built-in MCP (/api/mcp) is create-only for posts: it can create a draft/scheduled/now post but
has NO tool to LIST, DELETE, change the STATUS of, or EDIT an existing post by id (verified in the Postiz
source across v2.22.1..main -- the only edit tool, postSettingsTool, edits per-platform settings and is
unreleased). Those operations DO exist on Postiz's own public REST API, so this is a thin, honest wrapper
over the OFFICIAL Postiz public API (auth = the same POSTIZ_MCP_TOKEN as the built-in MCP, sent RAW in
Authorization -- not Bearer, per this self-hosted instance).

SHARED across all fleet clients (fleet_tools). HONESTY: every field returned comes from the API response;
a failed call is an honest error, never a fabricated success. Nothing is scheduled/published except an
explicit status change the caller asks for.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer, Context

POSTIZ_BASE = os.environ.get("POSTIZ_API_URL", "https://postiz.widev.com.au/api/public/v1").rstrip("/")
POSTIZ_TOKEN = os.environ.get("POSTIZ_MCP_TOKEN", "")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
# A QUEUE recreate whose slot is in the past (or too close) publishes IMMEDIATELY, which — with the old
# post already fired — produces a DUPLICATE live post. Refuse to recreate a queued post inside this lead
# window. (2026-09-01 incident: a 9am post fired image-less, then a 1:43pm "fix" recreate published a
# second copy because its target 9am slot had already passed.)
MIN_LEAD_SECONDS = int(os.environ.get("POSTIZ_MIN_LEAD_SECONDS", "900"))  # 15 min
UA = "nemoclaw-postiz-extras/1.0"

mcp = MCPServer(
    name="postiz_extras",
    instructions=(
        "Manage EXISTING social posts that the create-only Postiz MCP cannot: postiz_list (find ids), "
        "postiz_delete (remove a post), postiz_set_status (draft<->schedule = un-queue / queue at its "
        "stored date), postiz_edit (change content and/or date of an unpublished post; if the in-place "
        "update is rejected, delete + create anew). Report only real ids the API returned; a failed call "
        "is an honest error, never a fabricated success."
    ),
)


def _postiz(method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
    if not POSTIZ_TOKEN:
        return 0, {"error": "POSTIZ_MCP_TOKEN not set"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(POSTIZ_BASE + path, data=data, method=method)
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
        content = p.get("content") if isinstance(p.get("content"), str) else ""
        out.append({"id": p.get("id"), "group": p.get("group"), "state": p.get("state"),
                    "date": p.get("publishDate"),
                    "platform": integ.get("providerIdentifier") or integ.get("provider"),
                    "snippet": content.replace("\n", " ")[:80]})
    return {"ok": True, "count": len(out), "posts": out}


def _post_exists(post_id: str) -> bool | None:
    """Best-effort: is this post still present in the org? Scans a wide window. Returns True/False, or
    None if the check itself failed (inconclusive)."""
    st, data = _postiz("GET", "/posts?startDate=2020-01-01T00:00:00.000Z&endDate=2035-01-01T00:00:00.000Z")
    if st != 200 or not isinstance(data, dict):
        return None
    return any(p.get("id") == post_id for p in (data.get("posts") or []))


@mcp.tool()
def postiz_delete(ctx: Context, post_id: str) -> dict[str, Any]:
    """Permanently delete a Postiz post by id (use this to 'remove' a post, or as the first half of an
    edit-by-recreate). 404 means it was already gone (safe). Postiz has a known quirk where a delete can
    return HTTP 500 yet actually succeed (a missing post id surfaces as 500), so on a 500 this re-checks
    whether the post is really gone and reports honestly. Returns {ok, deleted}."""
    if not post_id.strip():
        return {"ok": False, "error": "post_id is required"}
    pid = post_id.strip()
    st, data = _postiz("DELETE", f"/posts/{urllib.parse.quote(pid)}")
    if st in (200, 201):
        return {"ok": True, "deleted": pid}
    if st == 404:
        return {"ok": True, "deleted": pid, "note": "already gone (404)"}
    if st == 500:
        # Known Postiz quirk: verify by re-fetch rather than trust the 500 either way.
        exists = _post_exists(pid)
        if exists is False:
            return {"ok": True, "deleted": pid, "note": "API returned 500 but the post is verified gone"}
        if exists is True:
            return {"ok": False, "status": 500, "detail": "delete returned 500 and the post is still present"}
        return {"ok": False, "status": 500, "detail": "delete returned 500; could not verify (re-check manually)"}
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


# Platforms Postiz REJECTS without an attachment (schema: instagram "should have at least one attachment").
IMAGE_REQUIRED_PROVIDERS = {"instagram"}


def _upload_from_url(image_url: str) -> dict | None:
    """Upload an image into Postiz's media library from a public URL -> {id, path} for a post's value.image."""
    st, data = _postiz("POST", "/upload-from-url", {"url": image_url})
    if st in (200, 201) and isinstance(data, dict) and data.get("path"):
        return {"id": data.get("id"), "path": data.get("path")}
    return None


def _provider_settings(provider: str, post_type: str) -> dict:
    """Rebuild the platform-required settings for a recreate. GET /posts returns settings=null, so we
    reconstruct the REQUIRED fields per Postiz's integrationSchema — notably Instagram's `post_type`
    (required, enum post/story) + `collaborators`. Facebook/GMB have no required settings."""
    s: dict[str, Any] = {"__type": provider}
    if provider == "instagram":
        s["post_type"] = post_type or "post"   # required for IG; default a feed post
        s["collaborators"] = []
    elif provider == "facebook" and post_type:
        s["post_type"] = post_type
    return s


@mcp.tool()
def postiz_edit(ctx: Context, post_id: str, window_start: str, window_end: str,
                new_content: str = "", new_date: str = "", image_url: str = "",
                post_type: str = "") -> dict[str, Any]:
    """Edit/RESCHEDULE an EXISTING unpublished post: change the copy (new_content) and/or the scheduled
    time (new_date, ISO UTC). Pass post_id + a date window (window_start/window_end, ISO) containing the
    current post. Only DRAFT/QUEUE posts.

    IMPLEMENTATION + IMPORTANT LIMITATION: Postiz's public API has no in-place update, so this RECREATES the
    post (new id) then deletes the old one. But `GET /posts` does NOT return the post's IMAGE or its
    platform SETTINGS, so those cannot be carried over automatically:
    - **Image:** re-supply it with `image_url=` (the hero the producer recorded on the task / in R2). For
      Instagram (which requires an attachment) image_url is MANDATORY — without it this returns an
      image_required error rather than creating a broken imageless post.
    - **Settings:** the required ones are rebuilt automatically (Instagram `post_type` — pass post_type=
      'story' for a story, else it defaults to a feed 'post'). Other settings the API hides (GBP
      call-to-action, IG collaborators/audio) are NOT preserved — if a post relies on those, recreate it
      from source with the full brief instead of editing.

    PAST-SLOT GUARD: editing a QUEUE (armed) post whose slot is in the past or under ~15 min away is
    REFUSED (error slot_in_past_or_too_soon) — a recreate then would publish immediately as a DUPLICATE,
    and if the original already fired it is already live. Fix a scheduled post WELL BEFORE its slot.

    Returns {ok, new_post_id, old_post_id, old_deleted, state}. The post id CHANGES."""
    if not post_id.strip():
        return {"ok": False, "error": "post_id is required"}
    if not new_content.strip() and not new_date.strip():
        return {"ok": False, "error": "pass new_content and/or new_date (a reschedule = new_date)"}
    old_id = post_id.strip()
    post = _find_post(old_id, window_start, window_end)
    if not post:
        return {"ok": False, "error": "post_not_found",
                "detail": "no post with that id in the given window; widen the window or check the id"}
    state = post.get("state")
    if state not in ("DRAFT", "QUEUE"):
        return {"ok": False, "error": "not_editable",
                "detail": f"only DRAFT/QUEUE posts can be edited; this is {state}"}
    integ = post.get("integration") or {}
    provider = (integ.get("providerIdentifier") or "").lower()
    settings = _provider_settings(provider, post_type.strip())
    # The API hides the original media, so it can't be preserved — re-supply via image_url.
    image: list[dict] = []
    if image_url.strip():
        up = _upload_from_url(image_url.strip())
        if not up:
            return {"ok": False, "error": "image_upload_failed",
                    "detail": f"could not upload image from {image_url}"}
        image = [up]
    elif provider in IMAGE_REQUIRED_PROVIDERS:
        return {"ok": False, "error": "image_required",
                "detail": (f"{provider} posts require an image, but Postiz's API does not return the "
                           "original media so it can't be carried through a reschedule. Re-supply it with "
                           "image_url= (the hero recorded on the task / in R2).")}
    content = new_content if new_content.strip() else (post.get("content") or "")
    new_type = "draft" if state == "DRAFT" else "schedule"   # QUEUE->schedule keeps it armed at its date
    eff_date = new_date.strip() or post.get("publishDate") or ""
    # PAST-SLOT GUARD: recreating a QUEUE post at a past/too-soon slot would publish it immediately as a
    # DUPLICATE (the original may already have fired at its scheduled time). Refuse, and tell the caller.
    if new_type == "schedule":
        try:
            slot = datetime.fromisoformat(eff_date.replace("Z", "+00:00"))
            if slot.tzinfo is None:
                slot = slot.replace(tzinfo=timezone.utc)
            lead = (slot - datetime.now(timezone.utc)).total_seconds()
        except Exception:  # noqa: BLE001
            lead = None
        if lead is not None and lead < MIN_LEAD_SECONDS:
            return {"ok": False, "error": "slot_in_past_or_too_soon",
                    "detail": (f"the target slot {eff_date} is "
                               f"{'in the past' if lead < 0 else f'only {int(lead//60)} min away'} "
                               f"(minimum lead is {MIN_LEAD_SECONDS//60} min). Recreating now would PUBLISH "
                               "IMMEDIATELY as a duplicate — and if the original already fired at its slot, "
                               "it is already live and cannot be recalled. Do NOT recreate: pass a genuine "
                               "future new_date=, or if it already went out, leave it and report that."),
                    "already_fired_risk": lead is not None and lead < 0}
    body = {
        "type": new_type,
        "date": eff_date or post.get("publishDate"),
        "shortLink": False, "tags": post.get("tags") or [],
        "posts": [{"integration": {"id": integ.get("id")},
                   "value": [{"content": content, "image": image}],
                   "settings": settings}],
    }
    st, data = _postiz("POST", "/posts", body)
    if st not in (200, 201):
        return {"ok": False, "status": st, "detail": str(data)[:400],
                "note": "create of the revised post failed; the original is untouched"}
    new_id = None
    if isinstance(data, list) and data:
        new_id = data[0].get("id") or data[0].get("postId")
    elif isinstance(data, dict):
        new_id = data.get("id") or (data.get("posts") or [{}])[0].get("id")
    dst, _ = _postiz("DELETE", f"/posts/{urllib.parse.quote(old_id)}")
    old_deleted = dst in (200, 201, 404) or (dst == 500 and _post_exists(old_id) is False)
    result = {"ok": True, "new_post_id": new_id, "old_post_id": old_id, "old_deleted": old_deleted,
              "state": state}
    if not old_deleted:
        result["warning"] = f"old post {old_id} may still exist — remove it with postiz_delete"
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")), streamable_http_path="/mcp",
            stateless_http=True)
