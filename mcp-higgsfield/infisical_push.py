#!/usr/bin/env python3
"""Push one secret value back to Infisical. Reads the VALUE FROM STDIN (never argv, so the secret
never appears in the process list) and prints nothing.

WHY THIS EXISTS
Higgsfield auth is a ROTATING OAuth pair: Clerk hands back a new refresh_token on every exchange and
the access token lives only 2h. The staged seed (Infisical /shared HIGGSFIELD_CREDENTIALS_JSON) is
therefore correct for about two hours after it is captured and worthless afterwards — verified on
2026-09-10, when the 26-Aug seed came back `invalid_grant`. Writing the rotated bundle back is what
stops the seed rotting, so a lost volume can actually be re-seeded.

FAIL-SAFE BY DESIGN
This is an optimisation, not a dependency. Any failure exits non-zero with no output and the caller
ignores it: an Infisical outage, or an identity without write scope, must never turn into a render
failure. Per CLAUDE.md §6 the `fleet-hermes` machine identity is a read-only Viewer and cannot stage
NEW secrets; whether it may UPDATE an existing one is a different permission and is unverified, so
we try update-then-create and degrade quietly. Exit codes let the caller log which happened.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_FORBIDDEN = 3  # identity lacks write scope — the documented, expected outcome


def _request(base, method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read() or b"{}")


def main():
    key = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "/shared"
    value = sys.stdin.read()
    if not value.strip():
        return EXIT_FAILED

    base = os.environ.get("INFISICAL_API_URL", "http://100.115.104.5:18789").rstrip("/")
    project = os.environ["INFISICAL_PROJECT_ID"]
    environment = os.environ.get("INFISICAL_ENV", "prod")

    token = _request(base, "POST", "/api/v1/auth/universal-auth/login", body={
        "clientId": os.environ["INFISICAL_CLIENT_ID"],
        "clientSecret": os.environ["INFISICAL_CLIENT_SECRET"],
    })["accessToken"]

    payload = {
        "workspaceId": project,
        "environment": environment,
        "secretPath": path,
        "secretValue": value,
    }
    endpoint = "/api/v3/secrets/raw/" + quote(key)

    try:
        _request(base, "PATCH", endpoint, token=token, body=payload)
        return EXIT_OK
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return EXIT_FORBIDDEN
        if exc.code != 404:
            return EXIT_FAILED

    # Absent at that path — create it instead.
    try:
        _request(base, "POST", endpoint, token=token, body={**payload, "type": "shared"})
        return EXIT_OK
    except urllib.error.HTTPError as exc:
        return EXIT_FORBIDDEN if exc.code in (401, 403) else EXIT_FAILED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(EXIT_FAILED)
