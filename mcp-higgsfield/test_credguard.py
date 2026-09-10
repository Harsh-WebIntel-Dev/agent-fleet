"""Tests for credguard — the durability guard around the vendored Higgsfield CLI's credential file.

These tests encode the exact production failure of 2026-09-09 23:47 UTC: a failed token refresh took
the lock, unlinked credentials.json, and wrote no replacement, leaving the service with ZERO
credentials on a named volume (durable data loss). The invariant under test is therefore:

    a failed refresh can never leave the config dir without a usable credentials.json.

Everything here is pure filesystem work — no network, no CLI, no container.
"""

from __future__ import annotations

import json
import os

import pytest

import credguard

SEED = {
    "auth_version": 2,
    "access_token": "a" * 36,
    "refresh_token": "r" * 48,
    "expires_at": 1787775122,
    "token_type": "bearer",
    "scope": "email profile offline_access user:org:read",
}


@pytest.fixture()
def cfg(tmp_path):
    d = tmp_path / "higgsfield"
    d.mkdir()
    return d


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return path


# --- atomic_write -------------------------------------------------------------------------------


def test_atomic_write_creates_file_with_owner_only_permissions(cfg):
    target = cfg / "credentials.json"

    credguard.atomic_write(str(target), json.dumps(SEED))

    assert json.loads(target.read_text()) == SEED
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_atomic_write_leaves_no_temp_files_behind(cfg):
    target = cfg / "credentials.json"

    credguard.atomic_write(str(target), json.dumps(SEED))

    assert [p.name for p in cfg.iterdir()] == ["credentials.json"]


def test_atomic_write_preserves_old_content_when_serialisation_fails(cfg):
    """The whole point of temp+rename: a failure must not truncate the existing file."""
    target = _write(cfg / "credentials.json", SEED)

    with pytest.raises(TypeError):
        credguard.atomic_write(str(target), object())  # not a str -> fails before replace

    assert json.loads(target.read_text()) == SEED


# --- clear_stale_lock ---------------------------------------------------------------------------


def test_clear_stale_lock_removes_orphaned_zero_byte_lock(cfg):
    lock = cfg / "credentials.json.lock"
    lock.write_bytes(b"")

    removed = credguard.clear_stale_lock(str(cfg), max_age_s=0)

    assert removed is True
    assert not lock.exists()


def test_clear_stale_lock_is_noop_when_no_lock_present(cfg):
    assert credguard.clear_stale_lock(str(cfg), max_age_s=0) is False


def test_clear_stale_lock_keeps_a_fresh_lock(cfg):
    """A lock a live refresh is holding must survive; only stale ones are cleared."""
    lock = cfg / "credentials.json.lock"
    lock.write_bytes(b"")

    removed = credguard.clear_stale_lock(str(cfg), max_age_s=3600)

    assert removed is False
    assert lock.exists()


# --- snapshot / restore: the core invariant -----------------------------------------------------


def test_snapshot_copies_a_valid_credentials_file(cfg):
    _write(cfg / "credentials.json", SEED)

    assert credguard.snapshot(str(cfg)) is True
    assert json.loads((cfg / "credentials.json.prev").read_text()) == SEED


def test_snapshot_refuses_to_capture_an_empty_file(cfg):
    """Never snapshot garbage — that would make the restore path useless."""
    (cfg / "credentials.json").write_bytes(b"")

    assert credguard.snapshot(str(cfg)) is False
    assert not (cfg / "credentials.json.prev").exists()


def test_snapshot_refuses_to_capture_invalid_json(cfg):
    (cfg / "credentials.json").write_text("not json{")

    assert credguard.snapshot(str(cfg)) is False


def test_snapshot_refuses_to_capture_json_without_a_refresh_token(cfg):
    _write(cfg / "credentials.json", {"access_token": "x"})

    assert credguard.snapshot(str(cfg)) is False


def test_restore_recovers_credentials_the_cli_unlinked(cfg):
    """THE production failure: file unlinked by a failed refresh."""
    _write(cfg / "credentials.json", SEED)
    credguard.snapshot(str(cfg))

    os.unlink(cfg / "credentials.json")  # what the CLI did at 23:47
    assert credguard.restore_if_lost(str(cfg)) is True

    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_restore_recovers_credentials_truncated_to_zero_bytes(cfg):
    """The other half-written outcome: file present but empty."""
    _write(cfg / "credentials.json", SEED)
    credguard.snapshot(str(cfg))
    (cfg / "credentials.json").write_bytes(b"")

    assert credguard.restore_if_lost(str(cfg)) is True
    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_restore_is_noop_when_credentials_are_intact(cfg):
    """A successful refresh rotated the token — never clobber it with the older snapshot."""
    _write(cfg / "credentials.json", SEED)
    credguard.snapshot(str(cfg))
    rotated = {**SEED, "refresh_token": "n" * 48, "expires_at": 1789000000}
    _write(cfg / "credentials.json", rotated)

    assert credguard.restore_if_lost(str(cfg)) is False
    assert json.loads((cfg / "credentials.json").read_text()) == rotated


def test_restore_reports_false_when_there_is_no_snapshot(cfg):
    os.path.exists(cfg / "credentials.json") or None

    assert credguard.restore_if_lost(str(cfg)) is False


def test_guarded_run_restores_after_a_destructive_failed_refresh(cfg):
    """End-to-end invariant: wrap a callable that behaves exactly like the broken CLI."""
    _write(cfg / "credentials.json", SEED)

    def destructive_cli():
        (cfg / "credentials.json.lock").write_bytes(b"")
        os.unlink(cfg / "credentials.json")
        return "boom"

    result, restored = credguard.guarded_run(str(cfg), destructive_cli)

    assert result == "boom"
    assert restored is True
    assert json.loads((cfg / "credentials.json").read_text()) == SEED


def test_guarded_run_clears_a_stale_lock_before_running(cfg):
    _write(cfg / "credentials.json", SEED)
    (cfg / "credentials.json.lock").write_bytes(b"")
    seen = {}

    def cli():
        seen["lock_present"] = (cfg / "credentials.json.lock").exists()
        return "ok"

    credguard.guarded_run(str(cfg), cli, lock_max_age_s=0)

    assert seen["lock_present"] is False


def test_guarded_run_detects_rotation_and_leaves_new_token_in_place(cfg):
    _write(cfg / "credentials.json", SEED)
    rotated = {**SEED, "refresh_token": "n" * 48}

    def rotating_cli():
        _write(cfg / "credentials.json", rotated)
        return "ok"

    result, restored = credguard.guarded_run(str(cfg), rotating_cli)

    assert restored is False
    assert json.loads((cfg / "credentials.json").read_text()) == rotated


# --- rotation write-back hook (the cure for seed rot) -------------------------------------------


def test_on_rotate_receives_the_rotated_bundle(cfg):
    """Without this hook the staged seed goes stale ~2h after capture and stays stale forever."""
    _write(cfg / "credentials.json", SEED)
    rotated = {**SEED, "refresh_token": "n" * 48}
    seen = []

    def rotating_cli():
        _write(cfg / "credentials.json", rotated)
        return "ok"

    credguard.guarded_run(str(cfg), rotating_cli, on_rotate=seen.append)

    assert len(seen) == 1
    assert json.loads(seen[0]) == rotated


def test_on_rotate_is_not_called_when_nothing_rotated(cfg):
    _write(cfg / "credentials.json", SEED)
    seen = []

    credguard.guarded_run(str(cfg), lambda: "ok", on_rotate=seen.append)

    assert seen == []


def test_on_rotate_is_not_called_after_a_destructive_failure(cfg):
    """A wiped-then-restored bundle is not a rotation — pushing it would re-stage a dead token."""
    _write(cfg / "credentials.json", SEED)
    seen = []

    def destructive_cli():
        os.unlink(cfg / "credentials.json")
        return "boom"

    credguard.guarded_run(str(cfg), destructive_cli, on_rotate=seen.append)

    assert seen == []


def test_a_failing_on_rotate_never_breaks_the_tool_call(cfg):
    """Infisical write-back is an optimisation; an outage must not surface as a render failure."""
    _write(cfg / "credentials.json", SEED)

    def rotating_cli():
        _write(cfg / "credentials.json", {**SEED, "refresh_token": "n" * 48})
        return "ok"

    def exploding_hook(_text):
        raise RuntimeError("infisical unreachable")

    result, restored = credguard.guarded_run(str(cfg), rotating_cli, on_rotate=exploding_hook)

    assert (result, restored) == ("ok", False)


def test_fingerprint_is_none_without_a_valid_bundle_and_stable_with_one(cfg):
    assert credguard.fingerprint(str(cfg)) is None

    _write(cfg / "credentials.json", SEED)

    assert credguard.fingerprint(str(cfg)) == credguard.fingerprint(str(cfg))


def test_guarded_run_propagates_exceptions_but_still_restores(cfg):
    """A CLI crash mid-refresh must not leave the service credential-less either."""
    _write(cfg / "credentials.json", SEED)

    def crashing_cli():
        os.unlink(cfg / "credentials.json")
        raise RuntimeError("cli crashed")

    with pytest.raises(RuntimeError):
        credguard.guarded_run(str(cfg), crashing_cli)

    assert json.loads((cfg / "credentials.json").read_text()) == SEED


# --- health signal: the drift that was silent for 14 days ---------------------------------------
#
# The credential rotated every ~2h from 26 Aug while the staged seed stayed frozen. Nothing surfaced
# that divergence, so the first symptom was a hard failure two weeks later. health() exists to make
# the divergence itself visible, and it must never emit a token value.


def test_health_reports_seed_drift_when_live_and_seed_differ():
    """THE missed signal: this would have been true from ~26 Aug 20:12 UTC onward."""
    live = {**SEED, "refresh_token": "rotated" + "x" * 41, "expires_at": 4102444800}

    report = credguard.health_from_bundles(live, SEED)

    assert report["seed_matches_live"] is False
    assert report["status"] == "seed_drift"
    assert "rot" in report["warning"].lower() or "drift" in report["warning"].lower()


def test_health_is_ok_when_seed_matches_live():
    report = credguard.health_from_bundles(SEED, SEED)

    assert report["seed_matches_live"] is True
    assert report["status"] == "ok"
    assert report["warning"] is None


def test_health_flags_absent_credentials_as_the_critical_case():
    report = credguard.health_from_bundles(None, SEED)

    assert report["live_present"] is False
    assert report["status"] == "no_credentials"
    assert report["seed_matches_live"] is None


def test_health_flags_a_missing_seed_because_reseeding_would_be_impossible():
    report = credguard.health_from_bundles(SEED, None)

    assert report["seed_present"] is False
    assert report["status"] == "no_seed"


def test_health_reports_expiries_as_readable_utc_and_staleness_hours():
    report = credguard.health_from_bundles(SEED, SEED)

    assert report["live_expires_at_utc"].startswith("2026-08-26T20:12")
    assert report["live_seconds_since_expiry"] > 0  # the 26-Aug seed, long expired
    assert report["seed_expires_at_utc"] == report["live_expires_at_utc"]


def test_health_never_emits_a_token_value():
    """Hard constraint: lengths, expiries and pass/fail only."""
    live = {**SEED, "refresh_token": "SUPERSECRETREFRESH" + "z" * 30}

    blob = json.dumps(credguard.health_from_bundles(live, SEED))

    assert "SUPERSECRETREFRESH" not in blob
    assert SEED["access_token"] not in blob
    assert SEED["refresh_token"] not in blob
    # but the shape IS reported, so drift is still diagnosable
    assert credguard.health_from_bundles(live, SEED)["live_refresh_token_len"] == len(live["refresh_token"])


def test_an_expired_live_access_token_alone_is_not_an_alarm():
    """Between calls the access token is routinely expired; the CLI refreshes on demand. Alarming on
    that would cry wolf and hide the real signal."""
    stale_access = {**SEED, "expires_at": 1787775122}

    report = credguard.health_from_bundles(stale_access, stale_access)

    assert report["live_access_token_expired"] is True
    assert report["status"] == "ok"


def test_health_reads_the_live_bundle_off_disk(cfg):
    _write(cfg / "credentials.json", SEED)

    report = credguard.health(str(cfg), json.dumps(SEED))

    assert report["live_present"] is True
    assert report["seed_matches_live"] is True


def test_health_survives_an_unparseable_seed(cfg):
    _write(cfg / "credentials.json", SEED)

    report = credguard.health(str(cfg), "not json{")

    assert report["seed_present"] is False
    assert report["status"] == "no_seed"


def test_rotation_journal_records_events_without_tokens(cfg):
    _write(cfg / "credentials.json", SEED)

    credguard.journal_rotation(str(cfg), persisted=False, reason="forbidden")

    line = (cfg / "rotation.log").read_text().strip()
    assert "forbidden" in line
    assert SEED["refresh_token"] not in line
    assert "persisted=False" in line or "persisted=false" in line.lower()


def test_rotation_journal_appends_and_is_bounded(cfg):
    _write(cfg / "credentials.json", SEED)
    for _ in range(120):
        credguard.journal_rotation(str(cfg), persisted=True, reason="ok")

    lines = (cfg / "rotation.log").read_text().strip().splitlines()
    assert 1 < len(lines) <= credguard.JOURNAL_MAX_LINES


# --- error classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: higgsfield: request failed (no response received)\nHint: Run: hf auth login",
        "Session expired.",
        "Not authenticated.",
        "invalid_grant: The refresh token is malformed or not valid.",
    ],
)
def test_auth_failures_are_classified_as_reauth_required(stderr):
    assert credguard.is_auth_failure(stderr) is True


@pytest.mark.parametrize(
    "stderr",
    ["CLI timed out after 25s", "model not found: banana", "", "rate limited, try later"],
)
def test_non_auth_failures_are_not_classified_as_reauth(stderr):
    assert credguard.is_auth_failure(stderr) is False


def test_reauth_message_names_the_real_remedy_not_the_canned_hf_hint():
    """The CLI's own 'Run: hf auth login' hint is useless here: the container is headless and the
    binary is not on PATH. The message must say what a human actually has to do."""
    msg = credguard.reauth_message()

    assert "auth login" in msg
    assert "HIGGSFIELD_CREDENTIALS_JSON" in msg
    assert "browser" in msg.lower()
