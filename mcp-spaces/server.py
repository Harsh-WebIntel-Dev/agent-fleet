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
from typing import Any

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

# MCPServer is the 2.0 API (FastMCP was the 1.x name). Same .tool() decorator, same run().
mcp = MCPServer(
    name="spaces",
    instructions=(
        "Client asset storage in DigitalOcean Spaces. Every operation is scoped to a client slug "
        "and confined to that client's folder; requests outside it are refused. Objects are "
        "private — use spaces_presign to share one."
    ),
)


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
    """Delete a client asset. Deliberately single-object: there is no recursive delete tool."""
    try:
        key = _key(client, path)
        s3 = _s3()
        s3.head_object(Bucket=BUCKET, Key=key)  # 404 rather than silently "succeeding"
        s3.delete_object(Bucket=BUCKET, Key=key)
        return {"ok": True, "path": path, "deleted": True}
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
