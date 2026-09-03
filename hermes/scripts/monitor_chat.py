#!/usr/bin/env python3
"""STABLE fingerprint of the AI Agent's ClickUp chat: one line per DM/GROUP_DM with the id of the
newest INBOUND message. Unchanged => the PM run is suppressed (zero tokens on a quiet tick).

WHY THE FINGERPRINT IS THE LAST INBOUND ID, NOT THE LAST ACTIVITY TIMESTAMP
---------------------------------------------------------------------------
Fingerprinting on `latest_comment_at` would change again the moment Webster POSTS ITS OWN REPLY,
waking the PM a second time to look at its own message. Keying on the newest inbound (non-bot)
message id is monotonic across a reply: inbound arrives -> id changes -> one wake; Webster
answers -> the newest INBOUND id is still the same -> no second wake.

WHY THIS TALKS TO REST AND NOT THE MCP
--------------------------------------
ClickUp's hosted MCP is capped at 300 CALLS PER 24 HOURS; REST is 100 per MINUTE (~144k/day). The
original 1-minute chat cron made ~5 MCP calls a tick (~7,200/day) and locked the workspace out of
MCP for hours. Detection runs here on REST; agent ACTIONS - including clickup_send_chat_message -
still go through the MCP, where the volume is low.

CALL COST: exactly ONE request on a quiet tick. A channel is only re-read when its
`latest_comment_at` has moved since the last tick, so a busy channel costs one extra request.

`counts.has_unread` is documented on the channel object but comes back NULL for this token - do
not build an unread gate on it. Only `latest_comment_at` is populated.

The token must be a personal API token on the Webster account (106813628): that identity is what
makes the bot's own DM/GROUP_DM channels visible. The MCP's credential does not work here - it is
a service JWT and REST rejects it with 401 OAUTH_019.

NOTE: ClickUp flags the Chat API as experimental. Any schema drift fails loudly rather than
reporting a quiet tick.
"""
import os, sys, json, urllib.request, urllib.error, urllib.parse

BASE = "https://api.clickup.com/api"
BOT  = "106813628"                       # Webster
WANTED_TYPES = ("DM", "GROUP_DM")        # CHANNEL (public rooms) deliberately excluded
MAX_PAGES = 10                           # bounded: a runaway pager must not burn the budget
SCAN_DEPTH = 20                          # messages to scan back when looking for newest inbound


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or "/home/hermes/.hermes"


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
    """GET with the ClickUp personal-token scheme (raw token, no 'Bearer' prefix). Never swallows."""
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


def workspace_id() -> str:
    """Resolved once and cached - the id never changes, and a lookup every tick is wasted budget."""
    cache = os.path.join(_hermes_home(), "cache", "clickup_workspace_id")
    try:
        wid = open(cache, encoding="utf-8").read().strip()
        if wid:
            return wid
    except OSError:
        pass
    teams = (get("/v2/team", []) or {}).get("teams") or []
    if len(teams) != 1:
        # Ambiguous or empty: refuse rather than silently picking the wrong workspace.
        raise RuntimeError(f"expected exactly 1 team, got {len(teams)}")
    wid = str(teams[0]["id"])
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        open(cache, "w", encoding="utf-8").write(wid)
    except OSError:
        pass                              # cache is an optimisation, not a requirement
    return wid


def is_actionable(msg: dict, ctype: str) -> bool:
    """Inbound from someone other than us. In a GROUP_DM it must also mention us - the proven rule
    from the old chat bridge, so Webster does not wake for every line of a group conversation."""
    if str(msg.get("user_id")) == BOT:
        return False
    if ctype == "GROUP_DM":
        return BOT in str(msg.get("content") or "")
    return True


STATE_PATH = os.path.join(_hermes_home(), "cache", "clickup_chat_seen.json")

try:
    try:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    wid = workspace_id()
    channels, cursor = [], None
    for _ in range(MAX_PAGES):
        params = [("limit", 100), ("include_closed", "false")]
        if cursor:
            params.append(("cursor", cursor))
        body = get(f"/v3/workspaces/{wid}/chat/channels", params)
        page = body.get("data")
        if page is None:
            raise RuntimeError(f"unexpected channels schema: keys={sorted(body)[:8]}")
        channels.extend(page)
        cursor = body.get("next_cursor")
        if not cursor:
            break
    else:
        raise RuntimeError(f"pagination exceeded {MAX_PAGES} pages - refusing to keep fetching")

    lines, fresh = [], {}
    for ch in channels:
        cid, ctype = ch.get("id"), ch.get("type")
        if not isinstance(cid, str) or ctype not in WANTED_TYPES or ch.get("archived"):
            continue
        ts = ch.get("latest_comment_at")
        prior = state.get(cid) or {}
        if prior.get("ts") == ts and "inbound" in prior:
            inbound = prior["inbound"]                  # nothing new here - no extra request
        else:
            msgs = get(f"/v3/workspaces/{wid}/chat/channels/{cid}/messages",
                       [("limit", SCAN_DEPTH)])
            data = msgs.get("data")
            if data is None:
                raise RuntimeError(f"unexpected messages schema for {cid}: keys={sorted(msgs)[:8]}")
            inbound = prior.get("inbound", "none")      # keep the old marker if only WE posted
            for msg in data:                            # newest-first
                if is_actionable(msg, ctype):
                    inbound = str(msg.get("id"))
                    break
        fresh[cid] = {"ts": ts, "inbound": inbound}
        lines.append(f"chan:{cid}:{ctype}:{inbound}")

    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(fresh, fh)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        # Not fatal: without the cache every channel is re-read each tick (more requests), but the
        # emitted fingerprint is unchanged, so no spurious wakes. Say so rather than hiding it.
        print(f"chat state cache unwritable ({exc}) - re-reading every tick", file=sys.stderr)
except Exception as exc:
    # Fail LOUDLY. A constant error string here would become the stored baseline, match on every
    # later tick, and suppress the sweep for hours while the run reported success.
    print(f"monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)

for line in sorted(lines):
    print(line)
if not lines:
    print("chan:none")
