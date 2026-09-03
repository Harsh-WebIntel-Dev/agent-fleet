#!/usr/bin/env python3
"""Emit a STABLE fingerprint of actionable ClickUp work.
Unchanged output => Hermes suppresses the PM run entirely (zero tokens on a quiet tick).
No timestamps, sorted output - any instability here would wake the PM every tick.

WHY THIS TALKS TO THE REST API AND NOT THE MCP
----------------------------------------------
ClickUp's hosted MCP (https://mcp.clickup.com/mcp) is capped at 300 CALLS PER 24 HOURS on our
plan - a daily budget, not a per-minute rate. The REST API is 100 requests per MINUTE per token
(~144,000/day). A polling monitor cannot live inside 300/day at any useful cadence: the original
1-minute chat cron burned ~7,200 calls/day and locked the whole workspace out of MCP for hours.
So machine polling runs on REST, and the hosted MCP is reserved for the agents' own in-task tool
calls. See developer.clickup.com/docs/connect-an-ai-assistant-to-clickups-mcp-server.

The MCP's credential CANNOT be reused here: the hosted MCP is OAuth-only and its token is a
service JWT that REST rejects with 401 OAUTH_019. This needs CLICKUP_API_TOKEN - a personal API
token minted on the AI Agent account (106813628), so the identity stays the same as before.
"""
import os, sys, json, urllib.request, urllib.error, urllib.parse

BASE = "https://api.clickup.com/api"
LIST = "901613842998"                              # Marketing (reference only)
TEAM = "307311"                                    # workspace id - watch ALL of Webster's lists, not one
BOT  = "106813628"                                 # AI Agent
# Client marketing work lives on the client's OWN ClickUp list (shared into our workspace), NOT the
# Marketing list - e.g. 'Pride Advice' (901613842937). So we query team-wide by assignee+status and
# EXCLUDE the non-marketing lists Webster is also a member of (admin/ops), rather than pinning one list.
EXCLUDE_LISTS = {"901613842995", "901613843001"}   # Admin Tasks, Hosting - never act on these
ACTIONABLE = ["to do", "approved", "rejected"]     # NOT include_closed - it does not exclude 'completed'
MAX_PAGES = 10                                     # bounded: a runaway pager must not burn the budget

# Review-gate trigger. A task moved to 'in progress' has a kanban chain running; the PM must review it
# only ONCE all its specialist cards are done. We detect that from the LOCAL kanban.db (zero ClickUp
# cost) rather than by polling ClickUp — the cards carry the ClickUp task id in their body. Emitting
# `review:<id>` exactly when the work completes wakes the PM once; while cards are still in flight we
# emit nothing for the task, so the fingerprint stays stable and the PM is not woken every tick.
KANBAN_DB = os.path.join(os.environ.get("HERMES_HOME", "") or "/home/hermes/.hermes", "kanban.db")


def _review_ready(task_ids):
    """Given ClickUp task ids that are 'in progress', return the subset whose kanban cards ALL exist
    and are done (so they are ready for the PM review gate). Local sqlite read only — never a network
    call. A task with no cards, or any card not yet done (running/todo/blocked/triage/...), is NOT
    ready and is omitted entirely (keeps the fingerprint quiet during ongoing work)."""
    if not task_ids:
        return set()
    ready = set()
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT id, status, coalesce(body,'') FROM tasks WHERE status != 'archived'"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # a kanban read problem must not crash task intake — just skip review detection
        print(f"review-ready check skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return set()
    for tid in task_ids:
        cards = [st for _cid, st, body in rows if tid in body]
        if cards and all(st == "done" for st in cards):
            ready.add(tid)
    return ready


def _token() -> str:
    """The cron subprocess does not reliably inherit env (secret scoping fails closed under
    multiplex_profiles), so fall back to reading the profile .env directly."""
    t = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if t:
        return t
    # NOTE: do NOT use ~ here - the hermes user's HOME is /opt/data, not /home/hermes.
    for cand in (os.environ.get("HERMES_HOME", ""), "/home/hermes/.hermes"):
        if not cand:
            continue
        try:
            for line in open(os.path.join(cand, ".env"), encoding="utf-8"):
                if line.startswith("CLICKUP_API_TOKEN="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
        except OSError:
            continue
    return ""


TOKEN = _token()


def get(path: str, params: list) -> dict:
    """GET with the ClickUp personal-token scheme (raw token, no 'Bearer' prefix).

    Every failure raises. A monitor that cannot see the world must never look like a quiet one:
    printing a constant error string here was a real bug - the string became the stored baseline,
    every later tick matched it, and the sweep was suppressed for hours while reporting success.
    """
    if not TOKEN:
        raise RuntimeError("CLICKUP_API_TOKEN is not set")
    url = f"{BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"Authorization": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            remaining = r.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 20:
                print(f"clickup rate budget low: {remaining} left this minute", file=sys.stderr)
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 429 carries X-RateLimit-Reset (unix seconds). Surface it - never swallow it.
        detail = e.read()[:200].decode(errors="replace")
        reset = e.headers.get("X-RateLimit-Reset", "?")
        raise RuntimeError(f"HTTP {e.code} reset={reset} {detail}") from None


try:
    seen = set()
    for page in range(MAX_PAGES):
        body = get(f"/v2/team/{TEAM}/task", [
            ("page", page),
            ("subtasks", "true"),
            ("include_closed", "false"),
            ("archived", "false"),
            ("assignees[]", BOT),
            *[("statuses[]", s) for s in ACTIONABLE],
        ])
        tasks = body.get("tasks") or []
        for t in tasks:
            tid = t.get("id")
            if (t.get("list") or {}).get("id") in EXCLUDE_LISTS:
                continue
            st = t.get("status")
            if isinstance(st, dict):
                st = st.get("status")
            if isinstance(tid, str) and isinstance(st, str):
                seen.add(f"task:{tid}:{st}")
        if body.get("last_page") is not False or not tasks:
            break
    else:
        raise RuntimeError(f"pagination exceeded {MAX_PAGES} pages - refusing to keep fetching")

    # Review gate: fetch 'in progress' tasks (one REST call) and emit review:<id> for those whose
    # kanban chain is fully done. This is what advances a finished blog to the PM's review gate.
    ip = get(f"/v2/team/{TEAM}/task", [
        ("subtasks", "true"), ("include_closed", "false"), ("archived", "false"),
        ("assignees[]", BOT), ("statuses[]", "in progress"),
    ])
    ip_ids = [t.get("id") for t in (ip.get("tasks") or [])
              if isinstance(t.get("id"), str) and (t.get("list") or {}).get("id") not in EXCLUDE_LISTS]
    for tid in _review_ready(ip_ids):
        seen.add(f"review:{tid}")
except Exception as exc:
    print(f"monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)

for line in sorted(seen):
    print(line)
if not seen:
    print("task:none")
