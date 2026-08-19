#!/bin/sh
# Idempotent hardening for the ONE agent allowed to ingest untrusted web content.
#
# WHY THIS EXISTS AS A SCRIPT: the live policy lives in /data/.openclaw/openclaw.json inside a
# Docker volume. Nothing in git reproduces it, so a rebuild would silently restore an agent that
# reads attacker-controlled web pages while holding exec, cron and publish rights. Re-run this after
# any rebuild and verify with the log assertions at the bottom.
#
# THREAT MODEL. web_search and web_fetch put attacker-controlled text into the model's context. You
# cannot prevent the model from being persuaded by it. What you CAN do is ensure that a persuaded
# model has nothing worth abusing. That is the whole design: containment, not detection.
set -eu
C=${OPENCLAW_CONTAINER:-openclaw-s13f8pdutxps4w5z3fbl9lq5}

docker exec "$C" sh -c 'cp /data/.openclaw/openclaw.json /data/.openclaw/openclaw.json.pre-harden'
docker exec "$C" node -e '
const fs=require("fs"), p="/data/.openclaw/openclaw.json";
const c=JSON.parse(fs.readFileSync(p,"utf8"));
const list=c.agents.list;

// ---- 1. Search is enabled globally, provider parallel-free -------------------------------------
// parallel-free needs no API key and is purpose-built for agents. duckduckgo was rejected: it
// scrapes HTML, is documented as experimental, and serves CAPTCHAs "under heavy or automated use" —
// wrong foundation for an unattended fleet. Brave ($5/mo free credit ~= 1000 queries) is the paid
// upgrade if quality or volume ever demands it.
c.tools = c.tools || {};
c.tools.web = c.tools.web || {};
c.tools.web.search = {enabled:true, provider:"parallel-free", maxResults:5};

// ---- 2. Exactly ONE agent may search ----------------------------------------------------------
// Every other agent, INCLUDING main, is denied. Confining untrusted input to a single hardened
// agent is what makes the containment argument tractable — otherwise every agent needs this
// treatment and one missed agent undoes it.
for (const a of list) {
  if (a.id === "research") continue;
  a.tools = a.tools || {};
  const d = new Set(a.tools.deny || []); d.add("web_search"); d.add("web_fetch");
  a.tools.deny = [...d];
}

// ---- 3. Contain the searching agent ------------------------------------------------------------
// profile "minimal" is session_status ONLY; alsoAllow adds back a deliberately short list.
// Chosen over a denylist because a denylist fails open on every tool added in future — a new MCP
// server would silently be reachable from web-poisoned context. This fails CLOSED.
// NOTE: "allow" and "alsoAllow" cannot coexist in one scope (config validation rejects it).
const r = list.find(a => a.id === "research");
r.tools = {
  profile: "minimal",
  alsoAllow: [
    "web_search","web_fetch",       // the job
    "litellm__semrush-*",           // SEO research data (read-only by nature)
    "memory_search","memory_get",   // recall, not write
    "read","dir_list",              // own workspace only, see fs.workspaceOnly below
    "sessions_yield"                // so PM can await its result
  ],
  deny: [
    // Defence in depth. Redundant under "minimal" today, load-bearing the moment somebody widens
    // the profile. deny wins over profile and alsoAllow, so these can never come back by accident.
    // CODE EXECUTION
    "exec","process","cron","gateway","nodes","node_inference","skill_workshop",
    // FILE MUTATION — note deny:["write"] does NOT cover apply_patch, they are separate tool ids
    "write","file_write","edit","apply_patch",
    // OVER-PRIVILEGED / SIDE-EFFECTING: no publishing or task-board writes from web-fed context
    "litellm__clickup-*","litellm__postiz-*","litellm__spaces-*",
    // LATERAL MOVEMENT: cannot spawn or message other agents to launder an instruction onward
    "sessions_spawn","sessions_send","message",
    // MISC ATTACK SURFACE
    "browser","canvas","image_generate","video_generate"
  ],
  // CREDENTIAL THEFT: read/dir_list cannot leave the workspace, so agent dirs and env files holding
  // LiteLLM keys are unreachable even if the model is talked into trying.
  fs: {workspaceOnly: true}
};
fs.writeFileSync(p, JSON.stringify(c,null,2));
console.log("hardened: research profile=minimal alsoAllow="+r.tools.alsoAllow.length+" deny="+r.tools.deny.length);
console.log("agents with web_search: "+list.filter(a=>!((a.tools&&a.tools.deny)||[]).includes("web_search")).map(a=>a.id).join(", "));
'
docker exec "$C" openclaw config validate

# A gateway RESTART is required. Agent tool policy is NOT hot-reloaded — verified: the policy sat in
# a valid config file across two test runs and had no effect until the container was restarted.
docker restart "$C" >/dev/null
until docker ps --format '{{.Status}}' --filter "name=^${C}$" | grep -q healthy; do sleep 5; done

# ---- VERIFY FROM LOGS, NEVER FROM THE MODEL ---------------------------------------------------
# The model will confidently misreport its own tool inventory. Asked directly after this hardening
# was live, research answered "COUNT=134; HAS_EXEC=YES; HAS_CLICKUP=YES" — every part false. Only
# [agents/tool-policy] lines are evidence.
echo "--- run one turn, then assert on the tool-policy log ---"
docker exec "$C" openclaw agent --agent research --model litellm/standard --message "Reply OK." >/dev/null 2>&1 || true
docker logs --since 3m "$C" 2>&1 | grep -oE 'tool policy removed [0-9]+ tool\(s\) via [^:]+' | sort -u
echo "EXPECT: one line for tools.profile (minimal) removing exec/cron/gateway/write/apply_patch/process,"
echo "        and one removing the clickup/postiz/spaces tool families."
