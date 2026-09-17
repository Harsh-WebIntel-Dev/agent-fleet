#!/usr/bin/env python3
"""Intake-health watchdog for the Hermes fleet (no LLM). Runs as a `--no-agent` cron job every
30 minutes; stdout becomes the alert email body. Prints NOTHING when healthy, so silence means
"checked and fine" — except a weekly self-test line (Monday 08:00-08:29) that proves delivery.

Why this exists (2026-09-12 → 09-15 outage): the gateway process re-registered only 32 of ~117 MCP
tools after a restart during a DNS outage, Webster paused his own task sweep, and his blocker
reports went to `deliver: local` files nobody reads — for two days. Every check below is a
signal that would have surfaced that within 30 minutes.

Checks (each alerts once per state change, recorded in cache/intake_health_state.json):
  - registered MCP tool count for the gateway process < FLOOR (from `Job '<id>': N MCP tool(s)
    available` lines — gateway-only logger — or mcp_floor_status.json when the hook exists)
  - intake jobs disabled / paused / failure streak / stale last run
  - a new gateway start in logs/gateway-exit-diag.log (PID-tagged, unlike agent.log)
  - chat monitor errors in the newest chat-intake output
  - a non-[SILENT] response from an intake job (= a reported blocker, per the RUN CONTRACT)
  - N consecutive bare [SILENT] from a job that must report (the SEMrush feed while firecrawl is down)
  - LiteLLM 429 / budget errors in the last 30 minutes
  - swap or disk pressure
"""
from __future__ import annotations
import glob, json, os, re, shutil, sys, time
from datetime import datetime, timedelta, timezone

HOME = os.environ.get("HERMES_HOME") or "/home/hermes/.hermes"
STATE = os.path.join(HOME, "cache", "intake_health_state.json")
LOGS = os.path.join(HOME, "logs")
CRON = os.path.join(HOME, "cron")
FLOOR = int(os.environ.get("INTAKE_HEALTH_TOOL_FLOOR", "100"))
SWAP_GB = float(os.environ.get("INTAKE_HEALTH_SWAP_GB", "8"))
DISK_PCT = float(os.environ.get("INTAKE_HEALTH_DISK_PCT", "85"))
SILENT_STREAK = int(os.environ.get("INTAKE_HEALTH_SILENT_STREAK", "3"))
INTAKE_JOBS = {"12b1e0f820e9": ("marketing-task-sweep", 15), "f3d04e2607f5": ("clickup-chat-intake", 5)}
MUST_REPORT_JOBS = {"21cb02f87693": "semrush-blog-global-feed"}
# A genuine failure an intake job surfaces in prose. Kept deliberately tight so a routine
# "no blocked/triage/todo cards … nothing actionable this tick" reconciliation never matches.
BLOCKER_MARKERS = (
    "toolset-not-mounted", "pm-comms-breaker-open", "not a deferrable tool",
    "not in this run's toolset", "no clickup_", "no `clickup", "cannot read or reply",
    "could not read", "unable to reach", "honest blocker", "mcp is detached",
    "tools are not present", "toolset contains no", "\u26d4",
)
MEL = timezone(timedelta(hours=10))  # AEST; the fleet runs on Melbourne wall-clock


def now_local() -> datetime:
    return datetime.now(MEL)


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, STATE)
    except OSError as exc:  # never fail the run over the state cache
        print(f"- state cache unwritable ({exc}); alerts may repeat", file=sys.stderr)


def read_tail(path: str, max_bytes: int = 4_000_000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def tool_count() -> tuple[int | None, str]:
    """Registered tool count as the gateway itself sees it."""
    status = os.path.join(HOME, "mcp_floor_status.json")
    try:
        with open(status, encoding="utf-8") as fh:
            d = json.load(fh)
            if isinstance(d.get("registered"), int):
                return d["registered"], f"mcp_floor_status.json ({d.get('ts')})"
    except (OSError, ValueError):
        pass
    best: tuple[str, int] | None = None
    for path in sorted(glob.glob(os.path.join(LOGS, "agent.log*"))):
        for m in re.finditer(r"^(\S+ \S+) INFO cron\.scheduler: Job '[0-9a-f]+': (\d+) MCP tool\(s\) available", read_tail(path), re.M):
            if best is None or m.group(1) > best[0]:
                best = (m.group(1), int(m.group(2)))
    return (best[1], f"agent.log {best[0]}") if best else (None, "no signal")


def load_jobs() -> dict:
    try:
        with open(os.path.join(CRON, "jobs.json"), encoding="utf-8") as fh:
            return {j["id"]: j for j in json.load(fh).get("jobs", [])}
    except (OSError, ValueError, KeyError):
        return {}


def latest_outputs(job_id: str, n: int) -> list[str]:
    files = sorted(glob.glob(os.path.join(CRON, "output", job_id, "*.md")), key=os.path.getmtime, reverse=True)
    return files[:n]


def response_of(path: str) -> str:
    text = read_tail(path, 400_000)
    m = re.search(r"^## Response\s*\n(.*)\Z", text, re.S | re.M)
    return (m.group(1).strip() if m else "").strip()


def is_monitor_quiet(path: str) -> bool:
    return "no_change (agent run suppressed)" in read_tail(path, 4000)


def _final_token(resp: str) -> str:
    lines = [ln.strip() for ln in resp.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _is_silent(resp: str) -> bool:
    """Handled/quiet: empty, or the last non-empty line is the [SILENT] marker.

    Webster ends a clean sweep with narration then [SILENT] (or just "nothing actionable"),
    so exact-equality on the whole body is wrong."""
    if not resp:
        return True
    return _final_token(resp).strip("*`_. ").upper() == "[SILENT]"


def _looks_like_blocker(resp: str) -> bool:
    low = resp.lower()
    return any(m in low for m in BLOCKER_MARKERS)


def gateway_starts_since(ts_iso: str | None) -> list[str]:
    out = []
    for line in read_tail(os.path.join(LOGS, "gateway-exit-diag.log")).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = str(d.get("ts") or "")
        if ts_iso is None or ts > ts_iso:
            out.append(f"{ts} {d.get('event') or d.get('kind') or ''} pid={d.get('pid')}".strip())
    return out


def litellm_errors_recent(minutes: int = 30) -> int:
    cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
    n = 0
    for line in read_tail(os.path.join(LOGS, "agent.log")).splitlines():
        # Only genuine LiteLLM/provider errors: HTTP 429 status lines, rate-limit exceptions, budget
        # exceeded. (Not "duration_ms=429.0", not "Agent budget: max_iterations", not "budget=16/500".)
        if line[:16] >= cutoff and re.search(
            r"status[_ ]code[=: ]+429|Error code: 429|HTTP/\S+ 429|RateLimitError|ExceededBudget|Budget has been exceeded|budget_exceeded|Too Many Requests",
            line,
        ):
            n += 1
    return n


def swap_used_gb() -> float | None:
    try:
        info = dict(l.split(":", 1) for l in open("/proc/meminfo") if ":" in l)
        total = int(info["SwapTotal"].split()[0]); free = int(info["SwapFree"].split()[0])
        return (total - free) / 1024 / 1024
    except (OSError, KeyError, ValueError):
        return None


def main() -> int:
    state = load_state()
    prev = state.get("alerts", {})
    alerts: dict[str, str] = {}
    info: list[str] = []
    now = now_local()

    # 1. tool floor
    count, src = tool_count()
    if count is not None and count < FLOOR:
        alerts["tools"] = f"gateway registered only {count} MCP tools (floor {FLOOR}) — source {src}. Fix: in a quiet window re-discover tools via the gateway-default slot procedure in CLAUDE.md section 13, then confirm the registered count is LiteLLM probe + 4."
    # 2. intake jobs
    jobs = load_jobs()
    for jid, (name, minutes) in INTAKE_JOBS.items():
        j = jobs.get(jid)
        if not j:
            alerts[f"job-missing-{jid}"] = f"cron job {name} ({jid}) is missing from jobs.json"; continue
        if not j.get("enabled") or j.get("state") == "paused":
            alerts[f"job-paused-{jid}"] = f"{name} ({jid}) is {j.get('state')} (enabled={j.get('enabled')}). Resume it (cron resume {jid}) once its tools are back."
        if (j.get("failure_streak") or 0) > 0:
            alerts[f"job-failing-{jid}"] = f"{name} failure_streak={j.get('failure_streak')} last_status={j.get('last_status')} last_error={str(j.get('last_error'))[:160]}"
        lr = j.get("last_run_at")
        if lr:
            try:
                age = (datetime.now(MEL) - datetime.fromisoformat(lr)).total_seconds() / 60
                if age > 3 * minutes + 5:
                    alerts[f"job-stale-{jid}"] = f"{name} last ran {age:.0f} min ago (schedule every {minutes} min) — is the gateway ticker alive? (cron status)"
            except ValueError:
                pass
        # A genuine failure an intake job reported in prose (deliver: local, otherwise invisible).
        # NOT a routine "nothing actionable … [SILENT]" reconciliation — that is the job working.
        for f in latest_outputs(jid, 1):
            if is_monitor_quiet(f) or _is_silent(response_of(f)):
                continue
            resp = response_of(f)
            if _looks_like_blocker(resp):
                alerts[f"blocker-{jid}"] = f"{name} reported a blocker ({os.path.basename(f)}):\n    " + resp[:600].replace("\n", "\n    ")
    # 3. chat monitor errors
    for f in latest_outputs("f3d04e2607f5", 1):
        head = read_tail(f, 6000)
        if re.search(r"monitor failed|monitor_error|Traceback", head):
            alerts["chat-monitor"] = f"chat monitor error in {os.path.basename(f)}"
    # 4. silent-when-broken
    for jid, name in MUST_REPORT_JOBS.items():
        files = latest_outputs(jid, SILENT_STREAK)
        if len(files) >= SILENT_STREAK and all(_is_silent(response_of(f)) for f in files):
            alerts[f"silent-{jid}"] = f"{name} has replied bare [SILENT] on its last {len(files)} runs (newest {os.path.basename(files[0])}) — check firecrawl/web_extract and the feed."
    # 5. gateway restarts
    starts = gateway_starts_since(state.get("last_gateway_event_ts"))
    if state.get("last_gateway_event_ts") and starts:
        alerts["gateway-restart"] = "gateway process (re)started: " + "; ".join(starts[-3:])
    if starts:
        state["last_gateway_event_ts"] = max(s.split(" ")[0] for s in starts)
    elif "last_gateway_event_ts" not in state:
        state["last_gateway_event_ts"] = now.isoformat()
    # 6. LiteLLM budget / rate limit
    n = litellm_errors_recent()
    if n:
        alerts["litellm-429"] = f"{n} LiteLLM 429/budget/rate-limit lines in agent.log in the last 30 min — a client key may be budget-capped."
    # 7. host pressure
    sw = swap_used_gb()
    if sw is not None and sw > SWAP_GB:
        alerts["swap"] = f"swap in use {sw:.1f} GB (> {SWAP_GB} GB) — watchdog restarts and stalled Chromium renders follow this."
    try:
        du = shutil.disk_usage(HOME)
        pct = du.used / du.total * 100
        if pct > DISK_PCT:
            alerts["disk"] = f"disk {pct:.0f}% used on the hermes volume"
    except OSError:
        pass

    # state-change logic: alert on new keys, note recoveries, stay silent otherwise
    new = {k: v for k, v in alerts.items() if k not in prev}
    # gateway-restart is a point-in-time event, not a condition — it never "recovers".
    recovered = [k for k in prev if k not in alerts and k != "gateway-restart"]
    lines: list[str] = []
    if new:
        lines.append("ALERTS")
        lines += [f"- {v}" for v in new.values()]
    if recovered:
        lines.append("RECOVERED: " + ", ".join(sorted(recovered)))
    selftest = now.weekday() == 0 and now.hour == 8 and now.minute < 30 and state.get("last_selftest") != now.strftime("%Y-%m-%d")
    if selftest:
        state["last_selftest"] = now.strftime("%Y-%m-%d")
        lines.append(f"SELF-TEST: watchdog alive; tools={count} ({src}); {len(alerts)} active alert(s); swap={sw if sw is None else round(sw,1)} GB")
    state["alerts"] = alerts
    state["last_run"] = now.isoformat()
    save_state(state)
    if lines:
        print(f"Fleet intake health — {now.strftime('%a %d %b %Y %H:%M')} AEST")
        print("\n".join(lines))
        if alerts and not new:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
