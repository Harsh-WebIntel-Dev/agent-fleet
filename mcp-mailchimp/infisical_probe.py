#!/usr/bin/env python3
"""Answer one question: can we authenticate to Infisical right now? Exit 0 = yes, 1 = no.

Prints nothing (a probe must never leak a token). Used by entrypoint.sh to tell "Infisical is
unreachable / the machine identity is rejected" apart from "Infisical is fine, the secret simply
is not staged" — infisical_fetch.py is deliberately fail-safe and silent, so it cannot distinguish
the two on its own."""
import json
import os
import sys
import urllib.request

DEFAULT_API_URL = "http://100.115.104.5:18789"
TIMEOUT_SECONDS = 8


def main():
    base = os.environ.get("INFISICAL_API_URL", DEFAULT_API_URL).rstrip("/")
    body = json.dumps(
        {
            "clientId": os.environ["INFISICAL_CLIENT_ID"],
            "clientSecret": os.environ["INFISICAL_CLIENT_SECRET"],
        }
    ).encode()
    req = urllib.request.Request(
        base + "/api/v1/auth/universal-auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        if not json.loads(r.read()).get("accessToken"):
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
