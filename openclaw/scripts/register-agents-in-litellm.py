"""Register the fleet in LiteLLM's Agents pane as a ROSTER.

Read this before assuming the pane means more than it does.

LiteLLM's agent registry is an A2A PROXY: each entry needs a URL LiteLLM can call. OpenClaw agents
are invoked through OpenClaw's own gateway (`openclaw agent --agent <name>`) and have no per-agent
HTTP endpoint, so every card here points at the gateway. These are inventory entries, not a working
A2A invocation path.

Spend reads $0.00 here BY DESIGN and that is not a bug: requests carry a per-CLIENT virtual key, so
LiteLLM cannot attribute a call to an agent. Real spend is under Keys/Teams > client-<slug>. Getting
genuine per-agent spend would need one provider (or model) entry per agent per client — rejected as
disproportionate for an analytics field.

Idempotent: existing agents are skipped, so this is safe to re-run. Worth re-running after a LiteLLM
redeploy — the registry is in-memory and only rehydrates because general_settings.store_model_in_db
is true.
"""

import json, os, sys, urllib.request, urllib.error

LL = os.environ.get("LITELLM_BASE_URL", "http://100.115.104.5:18791")
GW = os.environ.get("OPENCLAW_GATEWAY_URL", "http://openclaw-s13f8pdutxps4w5z3fbl9lq5:8080")

# Never hardcode this. Registering agents is an admin operation and needs the master key, so it is
# read from the environment and the script refuses to run without it.
K = os.environ.get("LITELLM_MASTER_KEY")
if not K:
    sys.exit("LITELLM_MASTER_KEY is not set. Export it (or source it from Infisical) and re-run.")

TIERS = {
    "pm": "deep", "qa": "deep",
    "writer": "standard", "research": "standard", "video": "standard", "dev": "standard",
    "accounts_manager": "fast", "seo": "fast", "social": "fast",
    "newsletter": "fast", "image": "fast", "publisher": "fast", "gmb": "fast",
}
ROLE = {
    "pm": "Routes work, injects client context, reviews output before it reaches a user",
    "qa": "Hard compliance gate — brand fit, factual support, ACL/greenwashing risk",
    "writer": "Long-form drafting to a client's template and tone",
    "research": "Sourced research; every claim carries a source URL",
    "video": "Video briefs and renders",
    "dev": "Site changes — backup, verify, revert on failure",
    "accounts_manager": "Budget gate before work is delegated",
    "seo": "On-page SEO, meta, internal linking",
    "social": "Facebook and Instagram scheduling via Postiz",
    "newsletter": "Monthly newsletter — draft only, never sends",
    "image": "Image generation and asset production",
    "publisher": "WordPress publishing",
    "gmb": "Google Business Profile posts",
}
CAVEAT = ("ROSTER ENTRY ONLY. Invoked through the OpenClaw gateway "
          "(`openclaw agent --agent {name}`), not via A2A through LiteLLM — the url below is the "
          "gateway, not a per-agent endpoint. Spend shows $0.00 here BY DESIGN: requests carry a "
          "per-CLIENT virtual key, so real spend is under Keys/Teams > client-<slug>.")

def req(method, path, body=None):
    r = urllib.request.Request(LL + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": "Bearer " + K, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=40).read() or b"null")

existing = {}
try:
    cur = req("GET", "/v1/agents")
    for a in (cur if isinstance(cur, list) else cur.get("data", [])):
        if a.get("agent_name"): existing[a["agent_name"]] = a.get("agent_id")
except Exception as e:
    print("could not list:", e)

ok = skipped = failed = 0
for name, tier in TIERS.items():
    if name in existing:
        print(f"  {name:18} already registered"); skipped += 1; continue
    card = {
        "protocolVersion": "0.3",
        "name": name,
        "description": f"{ROLE[name]}. Model tier: litellm/{tier}. " + CAVEAT.format(name=name),
        "url": GW,
        "version": "2026.7.1",
        "capabilities": {},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{"id": name, "name": name, "description": ROLE[name], "tags": ["fleet", tier]}],
    }
    try:
        req("POST", "/v1/agents", {"agent_name": name, "agent_card_params": card})
        print(f"  {name:18} registered (litellm/{tier})"); ok += 1
    except urllib.error.HTTPError as e:
        detail = e.read()[:100].decode(errors="replace")
        if "already exists" in detail or "Unique constraint" in detail:
            print(f"  {name:18} already exists"); skipped += 1
        else:
            print(f"  {name:18} HTTP {e.code}: {detail}"); failed += 1

print(f"\nregistered={ok} skipped={skipped} failed={failed}")
cur = req("GET", "/v1/agents")
rows = cur if isinstance(cur, list) else cur.get("data", [])
print("now in LiteLLM:", len(rows), sorted(a.get("agent_name") for a in rows))
