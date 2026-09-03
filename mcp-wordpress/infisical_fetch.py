#!/usr/bin/env python3
"""Fetch a single secret from Infisical and print its value (nothing else). Fail-safe: any error
exits non-zero with no output, so the caller can fall back to the existing env/volume. Auth via a
machine identity (Universal Auth) read from env. Endpoint via env (default: the host tailscale proxy)."""
import os, sys, json, urllib.request, urllib.error
from urllib.parse import quote

def main():
    key = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "/shared"
    base = os.environ.get("INFISICAL_API_URL", "http://100.115.104.5:18789").rstrip("/")
    proj = os.environ["INFISICAL_PROJECT_ID"]
    cid = os.environ["INFISICAL_CLIENT_ID"]; csec = os.environ["INFISICAL_CLIENT_SECRET"]

    def req(method, p, body=None, tok=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if tok: h["Authorization"] = "Bearer " + tok
        with urllib.request.urlopen(urllib.request.Request(base + p, data=data, headers=h, method=method), timeout=8) as r:
            return json.loads(r.read())

    tok = req("POST", "/api/v1/auth/universal-auth/login", {"clientId": cid, "clientSecret": csec})["accessToken"]
    r = req("GET", f"/api/v3/secrets/raw/{key}?workspaceId={proj}&environment={os.environ.get('INFISICAL_ENV','prod')}&secretPath={quote(path)}", tok=tok)
    val = r["secret"]["secretValue"]
    if not val:
        raise SystemExit(2)
    sys.stdout.write(val)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
