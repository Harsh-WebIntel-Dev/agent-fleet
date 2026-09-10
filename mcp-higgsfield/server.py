"""MCP server giving the fleet `image` agent brand-quality renders via the Higgsfield CLI.

WHY THIS EXISTS
The fleet `image` agent reaches tools only through LiteLLM's MCP gateway. Higgsfield has no API key —
auth is OAuth 2.0 PKCE (browser login once, then headless refresh via the stored refresh_token). The
official `@higgsfield/cli` npm package encapsulates the whole API + token refresh, so this server is a
thin wrapper that shells to that CLI. Credentials live on a persisted volume (seeded once from a Coolify
secret) so the CLI can refresh the access token across restarts without a browser.

ASYNC BY DESIGN — the load-bearing constraint
The OpenClaw MCP gateway times out a tool call at ~30s, but an image render takes minutes. So this server
NEVER blocks on `--wait`. `create_image_job` starts the job and returns a job_id fast; `get_image_job`
polls it fast. The `image` agent loops get_image_job until the job is completed, exactly as it would poll
any async render API.

HONESTY
Every tool returns what the CLI actually returned, or an explicit error. It NEVER fabricates an image URL
or dimensions — a real render has a real result URL, and dimensions are read from the actual image bytes.
This is the anti-confabulation contract: a plausible URL is worse than an honest failure.

TENANCY
Single Higgsfield workspace (the agency account) for now. When clients get their own Higgsfield
workspaces, pin the workspace per client via a gateway static header, same pattern as mcp-a2a — never
model-chosen.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer, Context

import credguard

log = logging.getLogger("higgsfield.credguard")

HIGGSFIELD_BIN = os.environ.get("HIGGSFIELD_BIN", "higgsfield")
# Each CLI call is a quick API round-trip; keep well under the ~30s gateway timeout.
CLI_TIMEOUT = float(os.environ.get("HIGGSFIELD_CLI_TIMEOUT", "25"))
DEFAULT_MODEL = os.environ.get("HIGGSFIELD_DEFAULT_MODEL", "nano_banana_pro")
# The CLI's credential dir. Every invocation is bracketed by credguard so a failed refresh can never
# leave this dir empty (see credguard.py for the 2026-09-09 data loss this prevents).
CRED_DIR = os.environ.get(
    "HIGGSFIELD_CONFIG_DIR", os.path.join(os.path.expanduser("~"), ".config", "higgsfield")
)
CRED_SECRET_NAME = "HIGGSFIELD_CREDENTIALS_JSON"
INFISICAL_PUSH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "infisical_push.py")

mcp = MCPServer(
    name="higgsfield",
    instructions=(
        "Render brand-quality images with Higgsfield. Async: create_image_job(prompt, ...) starts a render "
        "and returns a job_id fast; get_image_job(job_id) polls it — call it every ~15s until status is "
        "'completed', then use the returned url + real dimensions. list_image_models shows available models "
        "(default is a general hero model; use text2image_soul_v2 with image_references for the brand "
        "presenter / Soul refs). account_status is a health/credits check. Report only the real url and "
        "dimensions the render returned — never invent an image URL or size; a failed render is an honest "
        "failure, not a placeholder."
    ),
)


def _persist_rotated_credentials(bundle_text: str) -> None:
    """Write a rotated bundle back to Infisical so the staged seed stops rotting.

    Best-effort by contract: Higgsfield renders must not depend on the secrets store being writable.
    """
    try:
        proc = subprocess.run(
            ["python3", INFISICAL_PUSH, CRED_SECRET_NAME, "/shared"],
            input=bundle_text, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("rotation write-back skipped: %s", type(exc).__name__)
        return
    if proc.returncode == 0:
        log.info("rotated credentials pushed back to Infisical")
    elif proc.returncode == 3:
        # Documented, expected on a read-only Viewer identity. Say so once, plainly, and move on.
        log.warning(
            "rotation write-back FORBIDDEN (identity lacks write scope) — the staged seed will "
            "stay stale; the live bundle exists ONLY on this volume"
        )
    else:
        log.warning("rotation write-back failed (exit %s)", proc.returncode)


def _invoke_cli(args: list[str]) -> tuple[bool, Any, str]:
    """The raw CLI call. Wrapped by _run, which adds credential-loss protection."""
    try:
        proc = subprocess.run(
            [HIGGSFIELD_BIN, *args, "--json", "--no-color"],
            capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, None, f"CLI timed out after {CLI_TIMEOUT}s"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # Surface the CLI's own error text (stderr), trimmed. Never a fake success.
        return False, None, (proc.stderr or out or f"exit {proc.returncode}").strip()[:400]
    try:
        return True, json.loads(out) if out else {}, ""
    except json.JSONDecodeError:
        return True, out, ""  # some commands print plain text; hand it back as-is


def _run(args: list[str]) -> tuple[bool, Any, str]:
    """Run the higgsfield CLI with --json; return (ok, parsed_json_or_text, error).

    Bracketed by credguard: the vendored CLI deletes credentials.json on a failed refresh and writes
    no replacement, so we snapshot first and atomically restore after. A rejected refresh is also
    rewritten into an error a human can act on, instead of the CLI's misleading
    "request failed (no response received)".
    """
    if not shutil.which(HIGGSFIELD_BIN) and not os.path.exists(HIGGSFIELD_BIN):
        return False, None, "higgsfield CLI not found in container"

    (ok, data, err), restored = credguard.guarded_run(
        CRED_DIR, lambda: _invoke_cli(args), on_rotate=_persist_rotated_credentials
    )
    if ok:
        return ok, data, err

    if credguard.is_auth_failure(err):
        note = " (credentials were destroyed by the failed refresh and have been restored)" if restored else ""
        return False, None, f"{credguard.reauth_message()}{note} CLI said: {err}"
    return ok, data, err


def _find_urls(obj: Any) -> list[str]:
    """Recursively pull result media URLs out of a job JSON blob, order-preserving, de-duped."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.startswith("http") and (
                    "url" in k.lower() or "result" in k.lower() or "output" in k.lower()
                    or v.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp"))
                ):
                    if v not in found:
                        found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return found


def _image_dims(url: str) -> tuple[int | None, int | None]:
    """Read real width/height from the first bytes of the image (PNG/JPEG/WebP). No Pillow (CVE surface)."""
    try:
        _, head = _safe_get(url, {"User-Agent": "nemoclaw-image/1.0"}, timeout=15, max_bytes=65536)
    except Exception:  # noqa: BLE001 - dimensions are best-effort; the URL is the real proof
        return None, None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head[:3] == b"\xff\xd8\xff":  # JPEG: scan SOFn markers
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return int(w), int(h)
                seg = struct.unpack(">H", head[i + 2:i + 4])[0]
                i += 2 + seg
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            if head[12:16] == b"VP8X":
                w = 1 + int.from_bytes(head[24:27], "little")
                h = 1 + int.from_bytes(head[27:30], "little")
                return w, h
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


# --------------------------------------------------------------------------- tools

@mcp.tool()
def create_image_job(ctx: Context, prompt: str, aspect_ratio: str = "16:9",
                     model: str = "", image_references: str = "",
                     resolution: str = "2k") -> dict[str, Any]:
    """Start a Higgsfield image render. Returns a job_id fast — poll get_image_job until it completes.

    `model` defaults to a general hero model; use `text2image_soul_v2` (with `image_references`) for the
    brand presenter / Soul refs. `image_references` is a comma-separated list of upload ids / job ids /
    Soul ids. Weave the article's real title/subject into `prompt`; ban lettering inside the image.

    `resolution` is the native render size: '1k', '2k' (default), or '4k'. 1k and 2k cost the SAME
    (2 credits); 4k costs more (4 credits). For a WEB image (blog hero, in-content) 2k is ideal — the
    web deliverable is the WebP `web_url` from get_image_job (~150KB regardless of resolution), so there
    is no page-weight reason to drop below 2k. `aspect_ratio` for a blog hero is 16:9; in-content images
    are commonly 4:3.
    """
    if not (prompt or "").strip():
        return {"ok": False, "error": "empty", "detail": "prompt is required"}
    res = (resolution or "2k").strip().lower()
    if res not in ("1k", "2k", "4k"):
        return {"ok": False, "error": "bad_resolution",
                "detail": f"invalid resolution {res!r}: expected 1k, 2k or 4k"}
    job_type = (model or DEFAULT_MODEL).strip()
    # Harden against argv flag-smuggling: these values come from a (semi-trusted) model. job_type,
    # aspect_ratio and refs are identifiers/enums — whitelist them and reject a leading '-'. The free-text
    # prompt can contain anything, so pass it as --prompt=<value> (attached form) where the CLI cannot
    # re-parse it as a flag. subprocess uses an arg LIST (no shell), so this closes flag-smuggling.
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", job_type):
        return {"ok": False, "error": "bad_model", "detail": f"invalid model job_type: {job_type!r}"}
    ar = aspect_ratio.strip()
    if ar and not re.fullmatch(r"\d{1,2}:\d{1,2}", ar):
        return {"ok": False, "error": "bad_aspect_ratio", "detail": f"invalid aspect_ratio: {ar!r}"}
    refs = [r.strip() for r in image_references.split(",") if r.strip()]
    for ref in refs:
        if ref.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", ref):
            return {"ok": False, "error": "bad_reference", "detail": f"invalid image reference: {ref!r}"}
    args = ["generate", "create", job_type, f"--prompt={prompt}"]
    if ar:
        args.append(f"--aspect_ratio={ar}")
    args.append(f"--resolution={res}")
    for ref in refs:
        args.append(f"--image-references={ref}")
    ok, data, err = _run(args)
    if not ok:
        return {"ok": False, "error": "create_failed", "detail": err}
    # `generate create --json` returns a bare array of job-id strings, e.g. ["<uuid>"]. Handle that first,
    # then the dict shapes defensively.
    job_id = None
    if isinstance(data, list) and data:
        first = data[0]
        job_id = first if isinstance(first, str) else (first.get("id") if isinstance(first, dict) else None)
    elif isinstance(data, dict):
        job_id = data.get("id") or data.get("job_id") or (data.get("job") or {}).get("id")
        if not job_id and isinstance(data.get("jobs"), list) and data["jobs"]:
            j0 = data["jobs"][0]
            job_id = j0 if isinstance(j0, str) else (j0.get("id") if isinstance(j0, dict) else None)
    if not job_id:
        return {"ok": False, "error": "no_job_id", "detail": f"CLI returned no job id: {str(data)[:300]}"}
    return {"ok": True, "job_id": job_id, "model": job_type, "status": "started",
            "note": "Poll get_image_job(job_id) every ~15s until status is 'completed'."}


@mcp.tool()
def get_image_job(ctx: Context, job_id: str) -> dict[str, Any]:
    """Poll one image job. When completed, returns the real result url + real width/height read from bytes.

    Returns status while running; a real url + dimensions when done; an explicit error on failure.
    """
    jid = (job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "empty", "detail": "job_id is required"}
    if jid.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", jid):
        return {"ok": False, "error": "bad_job_id", "detail": f"invalid job_id: {jid!r}"}
    ok, data, err = _run(["generate", "get", jid])
    if not ok:
        return {"ok": False, "error": "get_failed", "detail": err}
    # `generate get --json` returns TWO artefacts: `result_url` = the full-res lossless PNG (multi-MB,
    # too big for a web upload / 413s WordPress), and `min_result_url` = the SAME image WebP-compressed
    # to ~150KB (full dimensions, web-ready, WordPress-friendly). The web deliverable is min_result_url;
    # the PNG is the archival master. Surfacing only the PNG was why the hero stage kept blocking.
    status, result_url, min_result_url, params = "", None, None, {}
    if isinstance(data, dict):
        status = str(data.get("status") or data.get("state") or "").lower()
        result_url = data.get("result_url")
        min_result_url = data.get("min_result_url")
        params = data.get("params") or {}
    if status in ("failed", "error", "cancelled", "canceled"):
        return {"ok": False, "error": "render_failed", "status": status, "detail": str(data)[:300]}
    url = result_url or (_find_urls(data)[:1] or [None])[0]
    done = status in ("completed", "succeeded", "success", "done") or bool(url)
    if not done:
        return {"ok": True, "status": status or "processing", "done": False,
                "note": "still rendering — poll again in ~15s"}
    if not url:
        return {"ok": False, "error": "completed_no_url", "status": status,
                "detail": "job reported done but no result URL was found — do not fabricate one"}
    w, h = params.get("width"), params.get("height")
    if not (isinstance(w, int) and isinstance(h, int)):
        w, h = _image_dims(url)   # fall back to reading the real bytes
    return {"ok": True, "status": "completed", "done": True,
            "url": url,                       # full-res PNG (archival master)
            "web_url": min_result_url or url, # WebP ~150KB — UPLOAD THIS for web / WordPress
            "web_format": "webp" if min_result_url else "unknown",
            "width": w, "height": h,
            "note": "Upload web_url (WebP, web-optimised) to WordPress; url is the full-res PNG master."}


@mcp.tool()
def list_image_models(ctx: Context) -> dict[str, Any]:
    """List available Higgsfield image models (job_type + display_name) so you can pick the right one."""
    ok, data, err = _run(["model", "list"])
    if not ok:
        return {"ok": False, "error": "list_failed", "detail": err}
    rows = data if isinstance(data, list) else (data.get("data") or data.get("models") or [])
    imgs = [{"job_type": m.get("job_type"), "display_name": m.get("display_name")}
            for m in rows if isinstance(m, dict) and str(m.get("type", "")).lower() == "image"]
    return {"ok": True, "default": DEFAULT_MODEL, "models": imgs}


@mcp.tool()
def account_status(ctx: Context) -> dict[str, Any]:
    """Health check: confirm the sidecar is authenticated and report remaining credits. Never returns tokens."""
    ok, data, err = _run(["account", "status"])
    if not ok:
        return {"ok": False, "authenticated": False, "error": "account_failed", "detail": err}
    credits = email = plan = None
    if isinstance(data, dict):
        credits = data.get("credits") or data.get("balance")
        email = data.get("email")
        plan = data.get("subscription_plan_type") or data.get("plan")
    return {"ok": True, "authenticated": True, "credits": credits, "email": email, "plan": plan}


def _safe_get(url: str, headers: dict[str, str] | None = None,
              timeout: float = 20.0, max_bytes: int = 200000) -> tuple[int, bytes]:
    """SSRF-safe GET. Guards the scheme, resolves the host ONCE and pins a verified-public IP (no DNS
    TOCTOU — the connection uses that exact IP, not a second lookup), preserves Host + TLS SNI so certs
    still validate, and REFUSES redirects (a 3xx raises rather than silently following to an internal
    host). Returns (status, body). Raises ValueError on a disallowed URL or a redirect."""
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
    raw = socket.create_connection((pinned, port), timeout=timeout)  # connect to the pinned public IP
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) \
            if parsed.scheme == "https" else raw
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock  # http.client adds the correct Host header from `host`; no re-resolve
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


@mcp.tool()
def verify_url(ctx: Context, url: str, expect_text: str = "") -> dict[str, Any]:
    """Cache-busted GET of a published page — proof-of-life a publisher can trust.

    A cached 200 is not proof; this appends a cache-buster and requires a real 200. If `expect_text` is
    given (e.g. the post title), it must appear in the returned HTML. Rejects non-public/loopback URLs.
    Returns {ok, live, status, title_present}. Use this to confirm a WordPress post is genuinely live.
    """
    if not (url or "").strip():
        return {"ok": False, "error": "empty", "detail": "url is required"}
    try:
        cb = hashlib.sha256(url.encode()).hexdigest()[:10]
        sep = "&" if "?" in url else "?"
        status, raw = _safe_get(f"{url}{sep}cb={cb}",
                                {"User-Agent": "nemoclaw-verify/1.0", "Cache-Control": "no-cache"},
                                timeout=20, max_bytes=200000)
        body = raw.decode(errors="replace")
        looks_html = "<html" in body.lower() or "<!doctype html" in body.lower()
        title_present = (expect_text.strip().lower() in body.lower()) if expect_text.strip() else None
        live = status == 200 and looks_html and (title_present is not False)
        return {"ok": live, "live": live, "status": status, "title_present": title_present}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "live": False, "status": exc.code, "error": f"http_{exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "live": False, "error": type(exc).__name__, "detail": str(exc)[:300]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")), streamable_http_path="/mcp",
            stateless_http=True)
