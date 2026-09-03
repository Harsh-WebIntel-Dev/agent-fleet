#!/usr/bin/env python3
"""Render hermes' per-profile .env files from Infisical (source of truth). Fail-safe: if Infisical is
unreachable, leaves the existing .env files untouched. Idempotent: replaces only the mapped keys,
preserves everything else. Prints ONLY key names + counts, never values.

Creds: env (INFISICAL_CLIENT_ID/SECRET) if present, else a staged /tmp/inf-creds file."""
import os, sys, json, urllib.request, urllib.error
from urllib.parse import quote

def load_creds():
    if os.environ.get("INFISICAL_CLIENT_ID") and os.environ.get("INFISICAL_CLIENT_SECRET"):
        return os.environ["INFISICAL_CLIENT_ID"], os.environ["INFISICAL_CLIENT_SECRET"]
    c = {}
    for line in open("/tmp/inf-creds"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); c[k.strip()] = v.strip()
    return c["INFISICAL_CLIENT_ID"], c["INFISICAL_CLIENT_SECRET"]

BASE = os.environ.get("INFISICAL_API_URL", "http://100.115.104.5:18789").rstrip("/")
PROJ = os.environ.get("INFISICAL_PROJECT_ID", "c48654fa-8af5-47d0-ba2d-a57cdbee9d0e")
ENV = os.environ.get("INFISICAL_ENV", "prod")
CID, CSEC = load_creds()

def req(m, p, b=None, t=None):
    d = json.dumps(b).encode() if b is not None else None
    h = {"Content-Type": "application/json"}
    if t: h["Authorization"] = "Bearer " + t
    with urllib.request.urlopen(urllib.request.Request(BASE + p, data=d, headers=h, method=m), timeout=10) as r:
        return json.loads(r.read())

try:
    tok = req("POST", "/api/v1/auth/universal-auth/login", {"clientId": CID, "clientSecret": CSEC})["accessToken"]
except Exception as e:
    print(f"Infisical unreachable ({type(e).__name__}) — leaving .env files unchanged"); sys.exit(0)

def fetch(key, path):
    try:
        return req("GET", f"/api/v3/secrets/raw/{key}?workspaceId={PROJ}&environment={ENV}&secretPath={quote(path)}", t=tok)["secret"]["secretValue"]
    except Exception:
        return None

# env var name -> (Infisical key, folder)
MAPPING = {
    "OPENAI_API_KEY": ("LITELLM_KEY", "/clients/webintelligenz"),
    "LITELLM_KEY_BIOGONE": ("LITELLM_KEY", "/clients/biogone"),
    "LITELLM_KEY_PRIDE_ADVICE": ("LITELLM_KEY", "/clients/pride-advice"),
    "LITELLM_KEY_RADIANCE_WEALTH": ("LITELLM_KEY", "/clients/radiance-wealth"),
}
values = {}
for evar, (k, p) in MAPPING.items():
    v = fetch(k, p)
    if v: values[evar] = v
if not values:
    print("no values fetched — leaving .env unchanged"); sys.exit(0)

def update_envfile(fp):
    lines, seen = [], set()
    if os.path.exists(fp):
        for line in open(fp):
            key = line.split("=", 1)[0].strip() if "=" in line else None
            if key in values:
                lines.append(f"{key}={values[key]}\n"); seen.add(key)
            else:
                lines.append(line if line.endswith("\n") else line + "\n")
    for evar, val in values.items():
        if evar not in seen:
            lines.append(f"{evar}={val}\n")
    with open(fp, "w") as f:
        f.writelines(lines)

targets = ["/home/hermes/.hermes/.env"] + [f"/home/hermes/.hermes/profiles/{p}/.env"
                                           for p in ("writer", "seo", "producer", "publisher", "researcher")]
for fp in targets:
    if os.path.isdir(os.path.dirname(fp)):
        update_envfile(fp)
print(f"rendered {len(values)} keys ({sorted(values)}) into {len(targets)} .env files from Infisical")
