"""Tests for proactive, single-flight, verified credential rotation.

These encode the two failures behind the 2026-09-09 outage:
  * rotation happened somewhere we never persisted from, so the durable seed rotted for 14 days;
  * refresh fired only at the cliff, leaving no retry budget when it was rejected.

Plus the suspected killer: two refreshers racing and tripping Clerk's reuse detection.

No network — the token exchange is always stubbed.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time

import pytest

import credguard
import refresher

LIVE = {
    "auth_version": 2,
    "access_token": "a" * 36,
    "refresh_token": "r" * 48,
    "expires_at": 4102444800,  # far future
    "token_type": "bearer",
    "scope": "email profile offline_access user:org:read",
}

CLERK_OK = {
    "access_token": "b" * 36,
    "refresh_token": "s" * 48,
    "expires_in": 86400,
    "token_type": "bearer",
    "scope": "email profile offline_access user:org:read",
}


@pytest.fixture()
def cfg(tmp_path):
    d = tmp_path / "higgsfield"
    d.mkdir()
    (d / "credentials.json").write_text(json.dumps(LIVE))
    return d


# --- threshold maths: proactive, not at the cliff -----------------------------------------------


def test_seconds_remaining_uses_the_absolute_expiry():
    assert refresher.seconds_remaining({"expires_at": 1000}, now=400) == 600


def test_seconds_remaining_is_none_without_a_usable_expiry():
    assert refresher.seconds_remaining({}, now=0) is None
    assert refresher.seconds_remaining(None) is None


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (86400, False),  # a whole 24h token: not due
        (28801, False),  # just outside the 8h lead
        (28799, True),   # inside the lead -> refresh now, with hours of retry budget
        (10, True),
        (-5000, True),   # already expired
    ],
)
def test_needs_refresh_fires_before_the_cliff(remaining, expected):
    bundle = {"expires_at": 1_000_000 + remaining}
    assert refresher.needs_refresh(bundle, lead_s=28800, now=1_000_000) is expected


def test_a_bundle_without_expiry_is_left_alone():
    """Better to defer to the CLI than to rotate a credential we cannot reason about."""
    assert refresher.needs_refresh({"refresh_token": "x"}, now=0) is False


# --- merging Clerk's response into the CLI's on-disk shape --------------------------------------


def test_merge_converts_expires_in_to_absolute_expires_at():
    merged = refresher.merge_token_response(LIVE, CLERK_OK, now=1_000_000)

    assert merged["expires_at"] == 1_000_000 + 86400
    assert merged["access_token"] == CLERK_OK["access_token"]
    assert merged["refresh_token"] == CLERK_OK["refresh_token"]


def test_merge_preserves_fields_clerk_does_not_return():
    """auth_version is the CLI's own; dropping it could make the file unreadable to the vendor."""
    merged = refresher.merge_token_response(LIVE, {"access_token": "z" * 36, "expires_in": 60})

    assert merged["auth_version"] == 2
    assert merged["refresh_token"] == LIVE["refresh_token"]  # rotation is optional per response
    assert merged["scope"] == LIVE["scope"]


def test_merge_does_not_mutate_the_previous_bundle():
    before = dict(LIVE)

    refresher.merge_token_response(LIVE, CLERK_OK, now=0)

    assert LIVE == before


def test_merge_rejects_a_response_without_an_access_token():
    with pytest.raises(refresher.RefreshUnavailable):
        refresher.merge_token_response(LIVE, {"expires_in": 60})


# --- refresh_once: verified install, rollback, persistence --------------------------------------


def test_refresh_once_installs_rotates_and_persists(cfg, monkeypatch):
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)
    persisted = []

    out = refresher.refresh_once(str(cfg), verify=lambda: True, persist=persisted.append,
                                 now=1_000_000)

    on_disk = json.loads((cfg / "credentials.json").read_text())
    assert on_disk["refresh_token"] == CLERK_OK["refresh_token"]
    assert on_disk["expires_at"] == 1_000_000 + 86400
    assert out == on_disk
    # The rotated bundle reached durable storage in the same breath as the rotation.
    assert json.loads(persisted[0])["refresh_token"] == CLERK_OK["refresh_token"]


def test_refresh_once_rolls_back_when_the_cli_rejects_the_new_bundle(cfg, monkeypatch):
    """A rotation we cannot verify is worse than no rotation."""
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)

    with pytest.raises(refresher.RefreshUnavailable, match="rolled back"):
        refresher.refresh_once(str(cfg), verify=lambda: False)

    assert json.loads((cfg / "credentials.json").read_text()) == LIVE


def test_refresh_once_leaves_the_credential_intact_when_clerk_rejects(cfg, monkeypatch):
    def rejected(_rt):
        raise refresher.RefreshRejected("invalid_grant")

    monkeypatch.setattr(refresher, "exchange_refresh_token", rejected)

    with pytest.raises(refresher.RefreshRejected):
        refresher.refresh_once(str(cfg))

    assert json.loads((cfg / "credentials.json").read_text()) == LIVE


def test_refresh_once_leaves_the_credential_intact_on_network_failure(cfg, monkeypatch):
    def down(_rt):
        raise refresher.RefreshUnavailable("URLError")

    monkeypatch.setattr(refresher, "exchange_refresh_token", down)

    with pytest.raises(refresher.RefreshUnavailable):
        refresher.refresh_once(str(cfg))

    assert json.loads((cfg / "credentials.json").read_text()) == LIVE


def test_a_failing_persist_does_not_undo_a_good_rotation(cfg, monkeypatch):
    """Durable storage being unwritable (the current Infisical 403) must not lose the rotation."""
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)

    def boom(_text):
        raise RuntimeError("infisical 403")

    refresher.refresh_once(str(cfg), verify=lambda: True, persist=boom)

    assert json.loads((cfg / "credentials.json").read_text())["refresh_token"] == CLERK_OK["refresh_token"]
    assert "persisted=False" in (cfg / "rotation.log").read_text()


def test_refresh_once_refuses_when_there_is_no_credential(cfg, monkeypatch):
    os.unlink(cfg / "credentials.json")
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)

    with pytest.raises(refresher.RefreshUnavailable, match="no usable"):
        refresher.refresh_once(str(cfg))


# --- exactly one refresher: cross-PROCESS exclusion ---------------------------------------------


def _hold_lock(cfg_dir, started, release):
    with credguard.exclusive_refresh_lock(cfg_dir, timeout_s=30):
        started.set()
        release.wait(20)


def test_refresh_lock_excludes_a_SEPARATE_PROCESS(cfg):
    """A threading.Lock would not catch this. Two OS processes must not both rotate."""
    ctx = multiprocessing.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(cfg), started, release))
    holder.start()
    try:
        assert started.wait(15), "child never acquired the lock"
        with pytest.raises(TimeoutError, match="refresh lock"):
            with credguard.exclusive_refresh_lock(str(cfg), timeout_s=1):
                pytest.fail("acquired a lock another process holds")
    finally:
        release.set()
        holder.join(20)


def test_the_lock_is_released_and_reusable_after_the_holder_exits(cfg):
    ctx = multiprocessing.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(cfg), started, release))
    holder.start()
    started.wait(15)
    release.set()
    holder.join(20)

    with credguard.exclusive_refresh_lock(str(cfg), timeout_s=5):
        pass  # must not raise


def test_the_lock_is_a_separate_file_from_the_vendor_cli_lock(cfg):
    with credguard.exclusive_refresh_lock(str(cfg), timeout_s=5):
        assert (cfg / credguard.REFRESH_LOCK_NAME).exists()
        # We must never squat on the CLI's own lock, or we'd fight the vendor for it.
        assert not (cfg / credguard.LOCK_NAME).exists()


def test_the_lock_records_its_holder_for_diagnosis(cfg):
    with credguard.exclusive_refresh_lock(str(cfg), timeout_s=5):
        body = (cfg / credguard.REFRESH_LOCK_NAME).read_text()

    assert f"pid={os.getpid()}" in body


def test_refresh_once_is_serialised_by_the_lock(cfg, monkeypatch):
    """The rotation path must itself take the lock, not merely offer one."""
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)
    ctx = multiprocessing.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(cfg), started, release))
    holder.start()
    try:
        assert started.wait(15)
        monkeypatch.setattr(credguard, "REFRESH_LOCK_TIMEOUT_S", 1)
        with pytest.raises(TimeoutError):
            refresher.refresh_once(str(cfg), verify=lambda: True, lock_timeout_s=1)
    finally:
        release.set()
        holder.join(20)


# --- the loop ------------------------------------------------------------------------------------


def test_loop_tick_does_nothing_when_the_token_is_healthy(cfg, monkeypatch):
    monkeypatch.setattr(refresher, "exchange_refresh_token",
                        lambda _rt: pytest.fail("must not refresh a healthy token"))

    assert refresher.RefreshLoop(str(cfg), lead_s=60).tick() == "not_due"


def test_loop_tick_refreshes_inside_the_lead_window(cfg, monkeypatch):
    (cfg / "credentials.json").write_text(json.dumps({**LIVE, "expires_at": time.time() + 100}))
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)

    loop = refresher.RefreshLoop(str(cfg), lead_s=28800, verify=lambda: True)

    assert loop.tick() == "refreshed"
    assert loop.last_error is None
    assert loop.last_refresh_at is not None


def test_loop_tick_reports_rejection_without_looping(cfg, monkeypatch):
    (cfg / "credentials.json").write_text(json.dumps({**LIVE, "expires_at": time.time() + 100}))

    def rejected(_rt):
        raise refresher.RefreshRejected("invalid_grant: dead")

    monkeypatch.setattr(refresher, "exchange_refresh_token", rejected)
    loop = refresher.RefreshLoop(str(cfg), lead_s=28800)

    assert loop.tick() == "rejected"
    assert "invalid_grant" in loop.last_error


def test_loop_tick_defers_on_a_transient_failure(cfg, monkeypatch):
    (cfg / "credentials.json").write_text(json.dumps({**LIVE, "expires_at": time.time() + 100}))

    def down(_rt):
        raise refresher.RefreshUnavailable("URLError: timed out")

    monkeypatch.setattr(refresher, "exchange_refresh_token", down)

    assert refresher.RefreshLoop(str(cfg), lead_s=28800).tick() == "deferred"


def test_loop_tick_handles_a_missing_credential(cfg):
    os.unlink(cfg / "credentials.json")

    assert refresher.RefreshLoop(str(cfg)).tick() == "no_credentials"


def test_the_refresh_lock_is_reentrant_within_a_thread(cfg):
    """Verification runs a CLI call from inside a rotation; without re-entrancy that self-deadlocks."""
    with credguard.exclusive_refresh_lock(str(cfg), timeout_s=5):
        with credguard.exclusive_refresh_lock(str(cfg), timeout_s=1):
            pass  # must not raise


def test_verification_may_take_the_lock_from_inside_a_rotation(cfg, monkeypatch):
    monkeypatch.setattr(refresher, "exchange_refresh_token", lambda _rt: CLERK_OK)
    seen = {}

    def verify_using_the_lock():
        # Exactly what server._run does during refresher verification.
        with credguard.exclusive_refresh_lock(str(cfg), timeout_s=2):
            seen["ran"] = True
        return True

    refresher.refresh_once(str(cfg), verify=verify_using_the_lock)

    assert seen["ran"] is True
    assert json.loads((cfg / "credentials.json").read_text())["refresh_token"] == CLERK_OK["refresh_token"]


def test_reentrancy_does_not_leak_across_processes(cfg):
    """Re-entrancy must be thread-local only; another PROCESS must still be excluded."""
    ctx = multiprocessing.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(cfg), started, release))
    holder.start()
    try:
        assert started.wait(15)
        with pytest.raises(TimeoutError):
            with credguard.exclusive_refresh_lock(str(cfg), timeout_s=1):
                pass
    finally:
        release.set()
        holder.join(20)
