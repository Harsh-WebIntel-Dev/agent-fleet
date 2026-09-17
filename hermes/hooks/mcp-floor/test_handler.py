"""Unit tests for the pure decision logic (run: python3 -m pytest hermes/hooks/mcp-floor -q)."""
import importlib.util, os, sys, time
HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("mcp_floor_handler", os.path.join(HERE, "handler.py"))
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)


def setup_function(_):
    h._state["heals"] = []


def test_healthy_registry_never_heals():
    assert h.decide(117, True, None, time.time()) == (False, "healthy")


def test_not_connected_is_left_to_discovery():
    ok, why = h.decide(None, False, 113, time.time()); assert not ok and "not connected" in why


def test_partial_registry_with_healthy_litellm_heals():
    ok, why = h.decide(32, True, 113, time.time()); assert ok and "32 < floor" in why


def test_partial_registry_but_litellm_also_partial_does_not_heal():
    ok, why = h.decide(32, True, 40, time.time()); assert not ok and "nothing better" in why


def test_probe_failure_does_not_heal():
    ok, why = h.decide(32, True, None, time.time()); assert not ok and "probe failed" in why


def test_probe_not_better_does_not_heal():
    ok, why = h.decide(110, True, 105, time.time()) if h.FLOOR > 110 else h.decide(99, True, 96, time.time())
    assert not ok


def test_cooldown_and_hourly_cap():
    now = time.time()
    h._state["heals"] = [now - 10]
    ok, why = h.decide(32, True, 113, now); assert not ok and "cooldown" in why
    h._state["heals"] = [now - 3000, now - 2000, now - 1000]
    ok, why = h.decide(32, True, 113, now); assert not ok and "max heals" in why


def test_status_file_written(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    h._write_status(registered=32, healed=False, reason="t")
    import json; d = json.load(open(tmp_path / "mcp_floor_status.json")); assert d["registered"] == 32 and d["floor"] == h.FLOOR
