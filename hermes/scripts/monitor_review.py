#!/usr/bin/env python3
"""Emit ClickUp marketing tasks that have JUST entered the human review gate, so the review-notify
cron emails the reviewers (Paul + Harsh) ONCE per task — on top of the ClickUp @mention.

WHY A STATE FILE AND NOT PURE HASH-SUPPRESSION
----------------------------------------------
The task-sweep monitor emits the whole actionable set and lets Hermes' hash-suppression wake the PM
whenever it changes. That is wrong for a NOTIFICATION: if we emitted every in-review task, then the
moment one task left review the set would change and the agent would re-email about all the others.
So we track which in-review tasks we have already notified (cache/review_notified.json) and emit
ONLY the newly-entered ones. Each task is announced exactly once, when it crosses into `in review`.
A task that leaves review is dropped from the notified set, so if it comes BACK (rejected → reworked
→ re-review) it is announced again — which is what a reviewer wants.

WHY REST, NOT THE MCP
---------------------
Same reason as monitor_tasks.py: the hosted ClickUp MCP is capped at 300 calls/24h (a daily budget),
REST is 100/min (~144k/day). Machine polling must live on REST; the MCP is reserved for the agents'
own in-task calls. Needs CLICKUP_API_TOKEN (personal token on the AI Agent account 106813628).

OUTPUT: `review-notify:<task_id>` per newly-in-review task (sorted), or a stable `review-notify:none`.
The agent turn fetches each task's client/title/links itself and writes the email; delivery is the
cron's `deliver: email` -> EMAIL_HOME_ADDRESS.
"""
import os, sys, json, urllib.request, urllib.error, urllib.parse

BASE = "https://api.clickup.com/api"
LIST = "901613842998"                 # Marketing
BOT  = "106813628"                    # AI Agent (Webster) — still an assignee after review hand-off
REVIEW_STATUS = "in review"           # the human-gate status Webster sets when work is ready
MAX_PAGES = 10
STATE_PATH = os.path.join(
    os.environ.get("HERMES_HOME", "") or "/home/hermes/.hermes", "cache", "review_notified.json"
)


def _token() -> str:
    """Cron subprocesses do not reliably inherit env under multiplex_profiles (secret scoping fails
    closed), so fall back to reading the profile .env directly. HOME is /opt/data, not ~ — do not
    use ~ here."""
    t = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if t:
        return t
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
    """GET with ClickUp's personal-token scheme (raw token, no 'Bearer'). Never swallow: a constant
    error string would become the stored baseline and silently suppress the sweep forever."""
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
        detail = e.read()[:200].decode(errors="replace")
        reset = e.headers.get("X-RateLimit-Reset", "?")
        raise RuntimeError(f"HTTP {e.code} reset={reset} {detail}") from None


def _load_notified() -> set:
    try:
        data = json.load(open(STATE_PATH, encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _save_notified(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(ids), fh)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        # Non-fatal: without persistence every in-review task re-announces next tick. Say so.
        print(f"review state cache unwritable ({exc})", file=sys.stderr)


try:
    current = set()
    for page in range(MAX_PAGES):
        body = get(f"/v2/list/{LIST}/task", [
            ("page", page),
            ("subtasks", "true"),
            ("include_closed", "false"),
            ("archived", "false"),
            ("assignees[]", BOT),
            ("statuses[]", REVIEW_STATUS),
        ])
        tasks = body.get("tasks") or []
        for t in tasks:
            tid = t.get("id")
            if isinstance(tid, str):
                current.add(tid)
        if body.get("last_page") is not False or not tasks:
            break
    else:
        raise RuntimeError(f"pagination exceeded {MAX_PAGES} pages - refusing to keep fetching")

    notified = _load_notified()
    fresh = current - notified
    # notified tracks exactly what is in review now, so a task that left review is re-announceable
    _save_notified(current)
except Exception as exc:
    print(f"monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)

for tid in sorted(fresh):
    print(f"review-notify:{tid}")
if not fresh:
    print("review-notify:none")
