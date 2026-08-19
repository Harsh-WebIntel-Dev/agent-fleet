#!/bin/sh
# Hermes is PUBLIC (wi-agent.widev.com.au, password-only) and client-facing. This restricts its
# toolsets. Re-run after any Hermes rebuild.
#
# WHAT WAS FOUND 2026-08-19, before this ran. A public, password-only endpoint had these ENABLED:
#   code_execution, terminal, file, browser, computer_use, cronjob
# i.e. anyone past one password could get an LLM to run code, open a shell, read files and install
# cron persistence on the production host — while web scraping pulled untrusted text into the same
# context. This was the default state of the image, not a change anyone made.
set -eu
C=${HERMES_CONTAINER:-hermes-agent-zhvjhbo5752ovx1nl2rk9v30}

docker exec "$C" sh -c 'cp /home/hermes/.hermes/config.yaml /home/hermes/.hermes/config.yaml.pre-harden'
docker exec "$C" python3 -c '
import sys, yaml; sys.path.insert(0,"/opt/hermes")
from hermes_cli.tools_config import PLATFORMS
p="/home/hermes/.hermes/config.yaml"
cfg=yaml.safe_load(open(p)) or {}

# What a client front-door legitimately needs. web is search (the client asks a question, Hermes
# looks it up); kanban is the client-facing taskboard; delegation is how work reaches PM.
# Deliberately absent: code_execution, terminal, file, browser, computer_use, cronjob, x_search,
# video*, context_engine.
SAFE=["clarify","delegation","image_gen","kanban","memory",
      "session_search","skills","todo","tts","vision","web"]

# EVERY platform is pinned, not just cli. This is the trap: `hermes tools disable` defaults to
# --platform cli, and a platform with no explicit list falls back to PLATFORMS[p]["default_toolset"]
# — the vendor default, NOT the cli list. So restricting cli alone leaves the actual public path
# (api_server, which is what the webui talks to) fully armed. Pinning all of them removes the need to
# know which one is live.
pt=cfg.setdefault("platform_toolsets",{})
for plat in PLATFORMS: pt[plat]=list(SAFE)

# redact_secrets masks key/token-shaped strings in tool output, logs and replies BEFORE the model or
# user sees them — the credential-theft control. On by default; pinned so it survives config edits.
# tirith is pre-exec scanning: inert while code_execution is off, correct if it is ever re-enabled.
s=cfg.setdefault("security",{})
s["redact_secrets"]=True
s["tirith_enabled"]=True

yaml.safe_dump(cfg, open(p,"w"), sort_keys=False, default_flow_style=False)
print("platforms pinned:", len(pt))
print("api_server ->", pt["api_server"])
'
docker restart "$C" >/dev/null
until docker ps --format '{{.Status}}' --filter "name=^${C}$" | grep -q healthy; do sleep 5; done
docker exec "$C" sh -c 'hermes tools list' | grep -E 'web|code_execution|terminal|file |browser|computer_use|cronjob'
echo "EXPECT: web enabled; code_execution/terminal/file/browser/computer_use/cronjob disabled."

# CAVEAT, deliberately not fixed here: yaml.safe_dump strips the comments that shipped in Hermes'
# config.yaml. The rationale lives in this script instead. Also note Hermes has no default
# model/provider configured (agent: {verify_on_stop:false} only), so `hermes -z` 401s against LiteLLM
# unless --provider litellm is passed. Whether the webui passes one has NOT been verified.
