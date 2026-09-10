"""Tests for boot credential selection.

The rule under test exists because of a real miss on 2026-09-10: a human staged a fresh credential in
Infisical, but the dead 26-Aug bundle on the volume still parsed, so the old "never clobber" guard
kept the dead one and the redeploy silently changed nothing.
"""

from __future__ import annotations

import json
import time

import pytest

import bootstrap
import credguard

BASE = {"auth_version": 2, "access_token": "a" * 36, "refresh_token": "r" * 48,
        "token_type": "bearer", "scope": "email profile offline_access"}

DEAD = {**BASE, "refresh_token": "dead" + "d" * 44, "expires_at": 1787775122}      # 26 Aug
FRESH = {**BASE, "refresh_token": "fresh" + "f" * 43, "expires_at": 1789088912}    # 11 Sep
ROTATED = {**BASE, "refresh_token": "rot" + "o" * 45, "expires_at": 1789200000}    # later still


@pytest.fixture()
def cfg(tmp_path):
    d = tmp_path / "higgsfield"
    d.mkdir()
    return d


# --- selection ----------------------------------------------------------------------------------


def test_a_fresh_seed_beats_a_dead_bundle_on_the_volume():
    """THE 2026-09-10 miss: this must choose the seed."""
    chosen = bootstrap.choose_newest([("volume", json.dumps(DEAD)), ("seed", json.dumps(FRESH))])

    assert chosen[0] == "seed"
    assert chosen[1]["refresh_token"] == FRESH["refresh_token"]


def test_a_rotated_volume_bundle_beats_an_older_seed():
    """The original protection must survive: never clobber a rotated token with a stale seed."""
    chosen = bootstrap.choose_newest([("volume", json.dumps(ROTATED)), ("seed", json.dumps(DEAD))])

    assert chosen[0] == "volume"


def test_the_snapshot_wins_when_it_is_the_newest():
    chosen = bootstrap.choose_newest([
        ("volume", None), ("snapshot", json.dumps(ROTATED)), ("seed", json.dumps(DEAD))])

    assert chosen[0] == "snapshot"


def test_unparseable_and_incomplete_candidates_are_ignored():
    chosen = bootstrap.choose_newest([
        ("volume", "not json{"),
        ("snapshot", json.dumps({"access_token": "x"})),   # no refresh_token
        ("seed", json.dumps(DEAD)),
    ])

    assert chosen[0] == "seed"


def test_no_usable_candidate_returns_none():
    assert bootstrap.choose_newest([("volume", ""), ("seed", "not json{")]) is None


def test_ties_prefer_the_earlier_source_to_avoid_rewriting_the_file():
    chosen = bootstrap.choose_newest([("volume", json.dumps(FRESH)), ("seed", json.dumps(FRESH))])

    assert chosen[0] == "volume"


def test_a_bundle_without_expiry_loses_to_one_with_a_real_expiry():
    no_exp = {k: v for k, v in BASE.items()}
    chosen = bootstrap.choose_newest([("volume", json.dumps(no_exp)), ("seed", json.dumps(DEAD))])

    assert chosen[0] == "seed"


# --- bootstrap end to end -----------------------------------------------------------------------


def test_bootstrap_installs_the_fresh_seed_over_a_dead_volume_bundle(cfg):
    (cfg / "credentials.json").write_text(json.dumps(DEAD))

    out = bootstrap.bootstrap(str(cfg), json.dumps(FRESH))

    assert (out["source"], out["installed"]) == ("seed", True)
    assert json.loads((cfg / "credentials.json").read_text())["refresh_token"] == FRESH["refresh_token"]


def test_bootstrap_keeps_an_already_live_newest_bundle_without_rewriting(cfg):
    (cfg / "credentials.json").write_text(json.dumps(ROTATED))
    before = (cfg / "credentials.json").stat().st_mtime_ns

    out = bootstrap.bootstrap(str(cfg), json.dumps(DEAD))

    assert (out["source"], out["installed"]) == ("volume", False)
    assert (cfg / "credentials.json").stat().st_mtime_ns == before


def test_bootstrap_recovers_when_the_volume_bundle_was_destroyed(cfg):
    """The 23:47 shape: file gone, snapshot present."""
    (cfg / "credentials.json.prev").write_text(json.dumps(ROTATED))

    out = bootstrap.bootstrap(str(cfg), json.dumps(DEAD))

    assert out["source"] == "snapshot"
    assert json.loads((cfg / "credentials.json").read_text())["refresh_token"] == ROTATED["refresh_token"]


def test_bootstrap_replaces_a_zero_byte_stub(cfg):
    (cfg / "credentials.json").write_bytes(b"")

    out = bootstrap.bootstrap(str(cfg), json.dumps(FRESH))

    assert out["installed"] is True
    assert (cfg / "credentials.json").stat().st_size > 0


def test_bootstrap_clears_every_lock_including_the_refresh_lock(cfg):
    (cfg / "credentials.json").write_text(json.dumps(FRESH))
    (cfg / credguard.LOCK_NAME).write_bytes(b"")
    (cfg / credguard.REFRESH_LOCK_NAME).write_text("pid=999 since=0")

    out = bootstrap.bootstrap(str(cfg), None)

    assert len(out["cleared_locks"]) == 2
    assert not (cfg / credguard.LOCK_NAME).exists()
    assert not (cfg / credguard.REFRESH_LOCK_NAME).exists()


def test_bootstrap_reports_no_source_when_nothing_is_usable(cfg):
    out = bootstrap.bootstrap(str(cfg), None)

    assert (out["source"], out["installed"]) == (None, False)


def test_bootstrap_reports_remaining_life_so_staleness_is_visible(cfg):
    live = {**BASE, "expires_at": time.time() + 3600}

    out = bootstrap.bootstrap(str(cfg), json.dumps(live))

    assert 3500 < out["seconds_remaining"] <= 3600


def test_bootstrap_writes_the_credential_owner_only(cfg):
    bootstrap.bootstrap(str(cfg), json.dumps(FRESH))

    assert oct((cfg / "credentials.json").stat().st_mode & 0o777) == "0o600"
