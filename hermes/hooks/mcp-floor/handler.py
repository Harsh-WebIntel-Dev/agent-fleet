"""mcp-floor — re-discover pm_comms tools when the gateway holds a partial registry.

Loaded by gateway/hooks.py from ~/.hermes/hooks/mcp-floor/. Two entry points:

  gateway:startup  -> start a background task that checks the floor with backoff for up to 24 h
                      (a boot during broken egress heals itself once egress returns).
  agent:start      -> a rate-limited check on every gateway message turn.

Decision rule (deliberately conservative — never trade one partial registry for another):
  heal iff  registered(pm_comms) < FLOOR  and  probe >= FLOOR  and  probe + UTILITY > registered
where `probe` is the tool count LiteLLM returns right now on GET <base>/mcp-rest/tools/list using
the same URL/headers Hermes uses for pm_comms. Heal = shutdown_mcp_servers() + discover_mcp_tools()
in a thread (exactly what the /reload-mcp slash command does). Cron runs build a fresh agent per
run, so a healed registry is picked up by the next tick without touching cached chat agents.

Rate limits: MIN_INTERVAL_S between heals, MAX_HEALS_PER_HOUR. Every decision is written to
~/.hermes/mcp_floor_status.json so the intake-health watchdog (and humans) can read it.
No secrets are logged or written; the probe only uses the header in memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Any, Optional

log = logging.getLogger("hooks.mcp_floor")

SERVER = os.environ.get("MCP_FLOOR_SERVER", "pm_comms")
FLOOR = int(os.environ.get("MCP_FLOOR_MIN_TOOLS", "100"))
UTILITY = 4                      # list_resources/read_resource/list_prompts/get_prompt per server
MIN_INTERVAL_S = int(os.environ.get("MCP_FLOOR_MIN_INTERVAL_S", "600"))
MAX_HEALS_PER_HOUR = int(os.environ.get("MCP_FLOOR_MAX_HEALS_PER_HOUR", "3"))
PROBE_TIMEOUT_S = 60
BACKOFF = [60, 120, 300, 600]     # startup retry schedule (then 600 s, capped at 24 h)
STARTUP_MAX_S = 24 * 3600

_state: dict[str, Any] = {"heals": [], "last_check": 0.0, "startup_task": None}


def _home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _status_path() -> str:
    return os.path.join(_home(), "mcp_floor_status.json")


def _write_status(**fields: Any) -> None:
    doc = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), "server": SERVER, "floor": FLOOR}
    doc.update(fields)
    try:
        tmp = _status_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
        os.replace(tmp, _status_path())
    except OSError as exc:
        log.warning("mcp-floor: cannot write status file: %s", exc)


def registered_count() -> tuple[Optional[int], bool]:
    """(registered tool count for SERVER, connected?) from the live in-process registry."""
    try:
        from tools import mcp_tool  # type: ignore
        lock = getattr(mcp_tool, "_lock", None)
        servers = getattr(mcp_tool, "_servers", {})
        if lock is not None:
            with lock:
                srv = servers.get(SERVER)
        else:
            srv = servers.get(SERVER)
    except Exception as exc:  # registry unavailable in this process
        log.debug("mcp-floor: registry unavailable: %s", exc)
        return None, False
    if srv is None:
        return None, False
    names = getattr(srv, "_registered_tool_names", None) or []
    return len(names), getattr(srv, "session", None) is not None


def probe_litellm() -> Optional[int]:
    """Tool count LiteLLM would serve right now, via the same url/headers as pm_comms."""
    try:
        from tools.mcp_tool import _load_mcp_config  # type: ignore
        cfg = (_load_mcp_config() or {}).get(SERVER) or {}
        url = str(cfg.get("url") or "")
        headers = dict(cfg.get("headers") or {})
    except Exception as exc:
        log.debug("mcp-floor: cannot read mcp config: %s", exc)
        return None
    if not url:
        return None
    base = url.split("/mcp")[0]
    try:
        req = urllib.request.Request(base + "/mcp-rest/tools/list", headers=headers)
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            data = json.loads(r.read())
        tools = data.get("tools", []) if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get("error"):
            return None
        return len(tools)
    except Exception as exc:
        log.debug("mcp-floor: probe failed: %s", type(exc).__name__)
        return None


def decide(registered: Optional[int], connected: bool, probe: Optional[int], now: float) -> tuple[bool, str]:
    """Pure decision function (unit-tested)."""
    if registered is None or not connected:
        return False, "server not connected (failed servers are retried by discovery itself)"
    if registered >= FLOOR:
        return False, "healthy"
    if probe is None:
        return False, "below floor but LiteLLM probe failed — will retry"
    if probe < FLOOR:
        return False, f"below floor but LiteLLM itself only lists {probe} — nothing better to swap in"
    if probe + UTILITY <= registered:
        return False, "probe would not improve on the current registry"
    heals = [t for t in _state["heals"] if now - t < 3600]
    if heals and now - heals[-1] < MIN_INTERVAL_S:
        return False, f"cooldown ({int(MIN_INTERVAL_S - (now - heals[-1]))}s left)"
    if len(heals) >= MAX_HEALS_PER_HOUR:
        return False, "max heals per hour reached"
    return True, f"registered {registered} < floor {FLOOR}; LiteLLM lists {probe}"


def _heal_sync() -> Optional[int]:
    from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools  # type: ignore
    shutdown_mcp_servers()
    discover_mcp_tools()
    return registered_count()[0]


async def check_and_heal(reason: str) -> dict:
    now = time.time()
    _state["last_check"] = now
    loop = asyncio.get_running_loop()
    registered, connected = registered_count()
    probe = await loop.run_in_executor(None, probe_litellm) if (registered is not None and connected and registered < FLOOR) else None
    should, why = decide(registered, connected, probe, now)
    result = {"registered": registered, "connected": connected, "probe": probe, "healed": False, "reason": why, "trigger": reason}
    if should:
        log.warning("mcp-floor: HEALING — %s (trigger=%s)", why, reason)
        _state["heals"].append(now)
        try:
            after = await loop.run_in_executor(None, _heal_sync)
            result.update(healed=True, registered_after=after)
            log.warning("mcp-floor: re-discovery done — registered %s -> %s", registered, after)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log.error("mcp-floor: heal failed: %s", exc)
    _write_status(**result)
    return result


async def _startup_loop() -> None:
    start = time.time()
    i = 0
    while time.time() - start < STARTUP_MAX_S:
        delay = BACKOFF[min(i, len(BACKOFF) - 1)]
        await asyncio.sleep(delay)
        i += 1
        try:
            res = await check_and_heal("startup-retry")
        except Exception as exc:
            log.warning("mcp-floor: startup check error: %s", exc)
            continue
        if res.get("registered") is not None and res["registered"] >= FLOOR:
            log.info("mcp-floor: registry healthy (%s tools); startup loop done", res["registered"])
            return


async def handle(event_type: str, context: Optional[dict] = None) -> None:
    if event_type == "gateway:startup":
        # first check shortly after boot, then the backoff loop until healthy or 24 h
        task = asyncio.get_running_loop().create_task(_startup_loop())
        _state["startup_task"] = task
        return
    if event_type == "agent:start":
        if time.time() - _state["last_check"] < MIN_INTERVAL_S:
            return
        await check_and_heal("agent:start")
