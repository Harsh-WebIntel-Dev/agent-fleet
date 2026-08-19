"""MCP server giving fleet agents access to client assets in DigitalOcean Spaces.

WHY THIS EXISTS
DigitalOcean's official MCP server covers Spaces *key management* only — verified against
docs.digitalocean.com/reference/mcp/spaces-mcp-tools: spaces-key-create/delete/get/list/update and
nothing else. There is no upload, download or list-objects tool. The standing rule is official MCP
servers first, and where none exists, wrap the vendor's own API rather than adopt a community
server. Spaces is S3-compatible, so this is a thin boto3 wrapper over DO's documented API.

The alternative considered and rejected was a FUSE mount of the bucket into the agent container.
That needed CAP_SYS_ADMIN on the one service that executes agent code and drives a browser — a
real containment downgrade to buy convenience. A tool needs no capability at all.

TENANT ISOLATION IS ENFORCED HERE, IN CODE
Every operation is confined to `clients/<slug>/` and the slug is validated against a strict
pattern. Traversal (`..`), absolute paths and slug-shaped lookalikes are rejected before any S3
call. This matters because the caller is a language model: prompt-level rules are not a control,
as this project has demonstrated repeatedly. An agent that asks for another client's file gets an
error, not the file.

Registered in LiteLLM's MCP gateway, so it appears alongside Postiz in the MCP page and picks up
per-tool call counts and spend in tool-policies.
"""

from __future__ import annotations

import os
import re
import base64
import ipaddress
import socket
import struct
from typing import Any
from urllib.parse import urlparse

import httpx
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from mcp.server.mcpserver import MCPServer

BUCKET = os.environ.get("DO_SPACES_BUCKET", "wi-ai")
REGION = os.environ.get("DO_SPACES_REGION", "syd1")
ENDPOINT = os.environ.get("DO_SPACES_ENDPOINT", f"https://{REGION}.digitaloceanspaces.com")
ROOT_PREFIX = os.environ.get("SPACES_ROOT_PREFIX", "clients")

# A client slug is a directory name we control at onboarding. Anything else is refused outright
# rather than sanitised — silently "fixing" a bad slug is how you end up in the wrong tenant.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Read/write caps. Object storage will happily accept a 5GB PUT from a confused agent.
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_WRITE_BYTES = 32 * 1024 * 1024
# Ingest is server-side, so it is not constrained by model context — video needs headroom.
MAX_INGEST_BYTES = 256 * 1024 * 1024
FETCH_TIMEOUT = 120.0

# MCPServer is the 2.0 API (FastMCP was the 1.x name). Same .tool() decorator, same run().
mcp = MCPServer(
    name="spaces",
    instructions=(
        "Client asset storage in DigitalOcean Spaces. Every operation is scoped to a client slug "
        "and confined to that client's folder; requests outside it are refused. Objects are "
        "private — use spaces_presign to share one."
    ),
)


# ---------------------------------------------------------------- remote fetch helpers
#
# Used by spaces_ingest_url. Kept here rather than trusting the source URL blindly: the URL arrives
# from a language model relaying what a generation API returned, so it is untrusted input.

_PRIVATE_REFUSED = "refusing to fetch a non-public address"


def _is_public_url(url: str) -> tuple[bool, str]:
    """Reject anything that is not a publicly-resolvable http(s) URL.

    The SSRF guard matters here specifically: this server sits on the internal Docker network with
    reachability to LiteLLM, Postgres and the OpenClaw gateway. A crafted source_url pointing at
    169.254.169.254 or an internal host would otherwise make this a confused deputy.
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
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"host does not resolve ({exc})"
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return False, f"{_PRIVATE_REFUSED} ({addr})"
    return True, "public http(s) URL"


def _fetch(url: str) -> httpx.Response:
    """GET a URL, re-checking every redirect hop against the same guard.

    follow_redirects=False on purpose — an open redirect on a public host would otherwise be a way
    back into the private network.
    """
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False) as c:
        resp = c.get(url, headers={"User-Agent": "NemoClaw-Spaces/1.0"})
        hops = 0
        while resp.is_redirect and hops < 5:
            nxt = str(resp.next_request.url) if resp.next_request else ""
            ok, why = _is_public_url(nxt)
            if not ok:
                raise httpx.RequestError(f"redirect to non-public target: {why}")
            resp = c.get(nxt, headers={"User-Agent": "NemoClaw-Spaces/1.0"})
            hops += 1
        return resp


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Real width/height from the file header. No Pillow: smaller attack surface on untrusted bytes."""
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
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if data[12:16] == b"VP8 ":
            return (struct.unpack("<H", data[26:28])[0] & 0x3FFF,
                    struct.unpack("<H", data[28:30])[0] & 0x3FFF)
    return None


def _s3():
    if not os.environ.get("DO_SPACES_ACCESS_KEY") or not os.environ.get("DO_SPACES_SECRET_KEY"):
        raise RuntimeError("DO_SPACES_ACCESS_KEY / DO_SPACES_SECRET_KEY are not configured")
    return boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["DO_SPACES_ACCESS_KEY"],
        aws_secret_access_key=os.environ["DO_SPACES_SECRET_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _key(client: str, path: str) -> str:
    """Resolve (client, path) to an absolute object key, or raise.

    Every rejection here is deliberate. `..` is the obvious one, but a leading `/` is just as bad
    because it reads as "root" to a model and would otherwise be silently absorbed.
    """
    slug = (client or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"invalid client slug {client!r}: expected lowercase letters, digits and hyphens"
        )

    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise ValueError("path is required")
    if ".." in rel.split("/"):
        raise ValueError("path traversal ('..') is not allowed")
    if "\\" in rel or rel.startswith("~"):
        raise ValueError(f"invalid path {path!r}")

    key = f"{ROOT_PREFIX}/{slug}/{rel}"
    # Belt and braces: re-derive the prefix and confirm containment after normalisation.
    expected = f"{ROOT_PREFIX}/{slug}/"
    if not key.startswith(expected) or "//" in key:
        raise ValueError(f"refusing to operate outside {expected}")
    return key


def _err(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in ("NoSuchKey", "404"):
            return {"ok": False, "error": "not_found", "detail": "object does not exist"}
        if code in ("AccessDenied", "403"):
            return {"ok": False, "error": "access_denied", "detail": "credentials lack permission"}
        return {"ok": False, "error": code, "detail": str(exc)[:300]}
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}


@mcp.tool()
def spaces_list(client: str, path: str = "", limit: int = 100) -> dict[str, Any]:
    """List client asset files. `client` is the client slug, `path` an optional sub-folder.

    Returns object keys relative to the client's folder, with sizes and modified times.
    """
    try:
        slug = (client or "").strip().lower()
        if not SLUG_RE.match(slug):
            raise ValueError(f"invalid client slug {client!r}")
        rel = (path or "").strip().lstrip("/")
        if ".." in rel.split("/"):
            raise ValueError("path traversal ('..') is not allowed")
        prefix = f"{ROOT_PREFIX}/{slug}/" + (f"{rel.rstrip('/')}/" if rel else "")

        resp = _s3().list_objects_v2(
            Bucket=BUCKET, Prefix=prefix, MaxKeys=max(1, min(int(limit), 1000))
        )
        items = [
            {
                "path": o["Key"][len(f"{ROOT_PREFIX}/{slug}/"):],
                "size_bytes": o["Size"],
                "modified": o["LastModified"].isoformat(),
            }
            for o in resp.get("Contents", [])
            if not o["Key"].endswith("/")
            # .trash is operator-facing, not part of the client's working asset list
            and "/.trash/" not in o["Key"]
        ]
        return {"ok": True, "client": slug, "count": len(items), "files": items,
                "truncated": resp.get("IsTruncated", False)}
    except Exception as exc:  # noqa: BLE001 - every failure is reported to the model as data
        return _err(exc)


@mcp.tool()
def spaces_read(client: str, path: str) -> dict[str, Any]:
    """Read a client asset. Text is returned inline; binary is returned base64-encoded."""
    try:
        key = _key(client, path)
        s3 = _s3()
        head = s3.head_object(Bucket=BUCKET, Key=key)
        size = head["ContentLength"]
        if size > MAX_READ_BYTES:
            return {"ok": False, "error": "too_large",
                    "detail": f"{size} bytes exceeds the {MAX_READ_BYTES} byte read limit; "
                              "use spaces_presign to hand the URL to a tool that can stream it"}
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        ctype = head.get("ContentType", "application/octet-stream")
        try:
            return {"ok": True, "path": path, "content_type": ctype,
                    "size_bytes": size, "encoding": "utf-8", "content": body.decode("utf-8")}
        except UnicodeDecodeError:
            return {"ok": True, "path": path, "content_type": ctype, "size_bytes": size,
                    "encoding": "base64", "content": base64.b64encode(body).decode("ascii")}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def spaces_write(client: str, path: str, content: str, encoding: str = "utf-8",
                 content_type: str = "") -> dict[str, Any]:
    """Write a client asset. Set encoding='base64' for binary content."""
    try:
        key = _key(client, path)
        if encoding == "base64":
            data = base64.b64decode(content)
        elif encoding == "utf-8":
            data = content.encode("utf-8")
        else:
            return {"ok": False, "error": "bad_encoding",
                    "detail": "encoding must be 'utf-8' or 'base64'"}
        if len(data) > MAX_WRITE_BYTES:
            return {"ok": False, "error": "too_large",
                    "detail": f"{len(data)} bytes exceeds the {MAX_WRITE_BYTES} byte write limit"}

        _s3().put_object(
            Bucket=BUCKET, Key=key, Body=data, ACL="private",
            ContentType=content_type or "application/octet-stream",
        )
        return {"ok": True, "path": path, "key": key, "size_bytes": len(data),
                "note": "stored privately; use spaces_presign for a shareable time-limited URL"}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def spaces_ingest_url(client: str, path: str, source_url: str,
                      content_type: str = "") -> dict[str, Any]:
    """Fetch a remote asset and store it in the client's folder. Use this for generated media.

    THIS IS THE TOOL FOR SAVING GENERATED IMAGES AND VIDEO. Higgsfield (and most generation APIs)
    return a temporary URL — Higgsfield's own docs say outputs are retained for about SEVEN DAYS.
    An asset that only exists at that URL is lost.

    The SERVER fetches the bytes, so the model never has to base64 a file through its context: no
    32MB write cap in practice, no truncation, no corruption. It also means the fetch is real —
    if the URL does not resolve, this fails loudly instead of an agent reporting a save that never
    happened.

    Returns the stored path plus the real byte count and, for images, the true decoded dimensions
    read from the file header — evidence, not a claim.
    """
    try:
        key = _key(client, path)
        ok, why = _is_public_url(source_url)
        if not ok:
            return {"ok": False, "error": "bad_source_url", "detail": why, "url": source_url}

        try:
            resp = _fetch(source_url)
        except httpx.RequestError as exc:
            return {"ok": False, "error": "fetch_failed",
                    "detail": f"could not fetch {source_url}: {exc}"}
        if resp.status_code != 200:
            return {"ok": False, "error": "fetch_failed",
                    "detail": f"source returned HTTP {resp.status_code}", "url": source_url}

        data = resp.content
        if not data:
            return {"ok": False, "error": "empty_source", "detail": "source returned 0 bytes"}
        if len(data) > MAX_INGEST_BYTES:
            return {"ok": False, "error": "too_large",
                    "detail": f"{len(data)} bytes exceeds the {MAX_INGEST_BYTES} byte ingest limit"}

        ctype = content_type or resp.headers.get("content-type", "application/octet-stream")
        _s3().put_object(Bucket=BUCKET, Key=key, Body=data, ACL="private", ContentType=ctype)

        out: dict[str, Any] = {"ok": True, "path": path, "key": key,
                               "size_bytes": len(data), "content_type": ctype,
                               "source_url": source_url,
                               "note": "stored privately; use spaces_presign to share it"}
        dims = _image_dimensions(data)
        if dims:
            out["width"], out["height"] = dims
        return out
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def spaces_presign(client: str, path: str, expires_seconds: int = 3600) -> dict[str, Any]:
    """Create a temporary shareable URL for a client asset (default 1 hour, max 7 days).

    Objects are stored private. This is how an asset is handed to something outside the fleet —
    a human, or a platform that needs to fetch the file — without making the bucket public.
    """
    try:
        key = _key(client, path)
        ttl = max(60, min(int(expires_seconds), 7 * 24 * 3600))
        s3 = _s3()
        s3.head_object(Bucket=BUCKET, Key=key)  # fail loudly rather than sign a dead URL
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=ttl
        )
        return {"ok": True, "path": path, "url": url, "expires_in_seconds": ttl}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def spaces_delete(client: str, path: str) -> dict[str, Any]:
    """Retire a client asset. SOFT delete — the object is moved, never destroyed.

    Deliberately non-destructive. OpenClaw's `approvals` system gates shell exec only, NOT MCP tool
    calls, so there is no human-in-the-loop gate available for this call. Rather than hand an agent
    an irreversible action with no gate, deletion moves the object to `.trash/<timestamp>/` inside
    the same client folder, where a human can restore or purge it.

    Single-object by design: there is no recursive delete tool, so a confused agent cannot empty a
    client's folder in one call.
    """
    try:
        key = _key(client, path)
        s3 = _s3()
        head = s3.head_object(Bucket=BUCKET, Key=key)  # 404 rather than silently "succeeding"

        slug = client.strip().lower()
        stamp = head["LastModified"].strftime("%Y%m%dT%H%M%S")
        trash_key = f"{ROOT_PREFIX}/{slug}/.trash/{stamp}/{path.lstrip('/')}"

        s3.copy_object(Bucket=BUCKET, Key=trash_key,
                       CopySource={"Bucket": BUCKET, "Key": key}, ACL="private")
        s3.delete_object(Bucket=BUCKET, Key=key)
        return {"ok": True, "path": path, "deleted": True, "recoverable": True,
                "moved_to": trash_key.split(f"{ROOT_PREFIX}/{slug}/", 1)[1],
                "note": "soft delete — the object was moved to .trash/, not destroyed"}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


if __name__ == "__main__":
    # host/port are run() kwargs in mcp 2.0 (there is no settings.host/.port any more).
    # stateless_http=True: LiteLLM's gateway opens a fresh session per call rather than holding
    # one open, so per-session state would only accumulate for no benefit.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        streamable_http_path="/mcp",
        stateless_http=True,
    )
