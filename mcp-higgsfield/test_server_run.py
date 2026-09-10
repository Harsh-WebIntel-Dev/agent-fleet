"""Integration tests for server._run — the single choke point every Higgsfield tool goes through.

Stubs the MCP package (not installed outside the image) and the CLI subprocess, so these run
anywhere. What matters here is the behaviour the fleet actually sees:

  * a failed refresh that destroys credentials.json no longer bricks the service, and
  * the CLI's misleading "request failed (no response received)" becomes an actionable instruction
    instead of something an agent might report as a transient network blip.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

SEED = {"auth_version": 2, "access_token": "a" * 36, "refresh_token": "r" * 48,
        "expires_at": 1787775122, "token_type": "bearer"}

AUTH_FAILURE_STDERR = "Error: higgsfield: request failed (no response received)\nHint: Run: hf auth login"


def _install_mcp_stub():
    """Minimal stand-in for mcp.server.mcpserver so server.py imports without the real package."""
    if "mcp.server.mcpserver" in sys.modules:
        return

    class _MCPServer:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

        def run(self, *a, **k):
            raise AssertionError("server must not be started in tests")

    mod = types.ModuleType("mcp.server.mcpserver")
    mod.MCPServer = _MCPServer
    mod.Context = object
    pkg = types.ModuleType("mcp")
    server_pkg = types.ModuleType("mcp.server")
    sys.modules.setdefault("mcp", pkg)
    sys.modules.setdefault("mcp.server", server_pkg)
    sys.modules["mcp.server.mcpserver"] = mod


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """server module with a throwaway credential dir and no real CLI on disk."""
    _install_mcp_stub()
    cfg = tmp_path / "higgsfield"
    cfg.mkdir()
    (cfg / "credentials.json").write_text(json.dumps(SEED))

    monkeypatch.setenv("HIGGSFIELD_CONFIG_DIR", str(cfg))
    sys.modules.pop("server", None)
    import server

    monkeypatch.setattr(server, "CRED_DIR", str(cfg))
    monkeypatch.setattr(server.shutil, "which", lambda _n: "/usr/local/bin/higgsfield")
    # No stub for _persist_rotated_credentials: the default fake CLI never rotates the bundle, so
    # write-back cannot fire by accident, and the tests that do want it patch it themselves.
    return server, cfg


def _fake_cli(monkeypatch, srv_mod, *, returncode, stderr="", stdout="", side_effect=None):
    def fake_run(cmd, **kwargs):
        if side_effect is not None:
            side_effect()
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)


def test_successful_call_returns_parsed_json(srv, monkeypatch):
    server, _cfg = srv
    _fake_cli(monkeypatch, server, returncode=0, stdout='{"credits": 42}')

    ok, data, err = server._run(["account", "status"])

    assert (ok, data, err) == (True, {"credits": 42}, "")


def test_destructive_auth_failure_restores_credentials_and_explains_the_remedy(srv, monkeypatch):
    """The exact production sequence: CLI takes the lock, unlinks the bundle, fails."""
    server, cfg = srv

    def destroy():
        (cfg / "credentials.json.lock").write_bytes(b"")
        (cfg / "credentials.json").unlink()

    _fake_cli(monkeypatch, server, returncode=1, stderr=AUTH_FAILURE_STDERR, side_effect=destroy)

    ok, _data, err = server._run(["account", "status"])

    assert ok is False
    # The service is NOT bricked: credentials survived the failed refresh.
    assert json.loads((cfg / "credentials.json").read_text()) == SEED
    # And the orphaned lock is gone, so the next call is not blocked.
    assert not (cfg / "credentials.json.lock").exists()
    # The error tells a human what to do, and admits the destruction happened.
    assert "auth login" in err
    assert "HIGGSFIELD_CREDENTIALS_JSON" in err
    assert "restored" in err
    # The CLI's own text is preserved for diagnosis rather than hidden.
    assert "no response received" in err


def test_auth_failure_without_destruction_still_explains_the_remedy(srv, monkeypatch):
    server, cfg = srv
    _fake_cli(monkeypatch, server, returncode=1, stderr="Not authenticated.")

    ok, _data, err = server._run(["account", "status"])

    assert ok is False
    assert "auth login" in err
    assert "restored" not in err  # nothing was destroyed, so don't claim it was
    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_non_auth_errors_are_passed_through_untouched(srv, monkeypatch):
    """A model-not-found must not be dressed up as a credentials problem."""
    server, _cfg = srv
    _fake_cli(monkeypatch, server, returncode=1, stderr="model not found: banana")

    ok, _data, err = server._run(["generate", "create"])

    assert ok is False
    assert err == "model not found: banana"
    assert "auth login" not in err


def test_missing_cli_is_reported_without_touching_credentials(srv, monkeypatch):
    server, cfg = srv
    monkeypatch.setattr(server.shutil, "which", lambda _n: None)
    monkeypatch.setattr(server.os.path, "exists", lambda _p: False)

    ok, _data, err = server._run(["account", "status"])

    assert (ok, err) == (False, "higgsfield CLI not found in container")
    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_cli_timeout_is_reported_and_is_not_an_auth_failure(srv, monkeypatch):
    """A slow render must not be misreported as dead credentials."""
    server, cfg = srv

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, server.CLI_TIMEOUT)

    monkeypatch.setattr(server.subprocess, "run", timeout)

    ok, _data, err = server._run(["account", "status"])

    assert ok is False
    assert "timed out" in err
    assert "auth login" not in err
    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_plain_text_cli_output_is_handed_back_as_is(srv, monkeypatch):
    server, _cfg = srv
    _fake_cli(monkeypatch, server, returncode=0, stdout="not json at all")

    ok, data, err = server._run(["model", "list"])

    assert (ok, data, err) == (True, "not json at all", "")


@pytest.mark.parametrize(
    ("returncode", "expected_fragment"),
    [
        (0, "pushed back to Infisical"),
        (3, "FORBIDDEN"),          # read-only Viewer identity — the documented, expected case
        (1, "failed (exit 1)"),
    ],
)
def test_write_back_reports_each_outcome_without_raising(srv, monkeypatch, caplog,
                                                         returncode, expected_fragment):
    """Write-back is best-effort: every outcome is logged, none propagates to the caller."""
    server, _cfg = srv
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=""),
    )

    with caplog.at_level("INFO"):
        assert server._persist_rotated_credentials(json.dumps(SEED)) is None

    assert expected_fragment in caplog.text


def test_write_back_survives_an_unlaunchable_helper(srv, monkeypatch, caplog):
    """An Infisical outage or a missing helper must never surface as a render failure."""
    server, _cfg = srv

    def boom(cmd, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(server.subprocess, "run", boom)

    with caplog.at_level("WARNING"):
        assert server._persist_rotated_credentials(json.dumps(SEED)) is None

    assert "skipped" in caplog.text


def test_write_back_passes_the_secret_on_stdin_never_argv(srv, monkeypatch):
    """A secret in argv would be visible in the process list to anything that can read /proc."""
    server, _cfg = srv
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    server._persist_rotated_credentials(json.dumps(SEED))

    assert captured["input"] == json.dumps(SEED)
    assert not any(SEED["refresh_token"] in str(part) for part in captured["cmd"])


def test_rotation_triggers_write_back(srv, monkeypatch):
    """A successful refresh must push the rotated bundle back, or the staged seed rots again."""
    server, cfg = srv
    pushed = []
    monkeypatch.setattr(server, "_persist_rotated_credentials", pushed.append)
    rotated = {**SEED, "refresh_token": "n" * 48}

    def rotate():
        (cfg / "credentials.json").write_text(json.dumps(rotated))

    _fake_cli(monkeypatch, server, returncode=0, stdout="{}", side_effect=rotate)

    server._run(["account", "status"])

    assert len(pushed) == 1
    assert json.loads(pushed[0]) == rotated
