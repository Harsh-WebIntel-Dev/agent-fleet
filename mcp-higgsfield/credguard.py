"""Durability guard around the vendored Higgsfield CLI's credential file.

WHY THIS EXISTS
On 2026-09-09 23:47 UTC a token refresh failed and the vendored `@higgsfield/cli` binary took
`credentials.json.lock`, unlinked `credentials.json`, and wrote no replacement. The config dir is a
named Docker volume, so that was durable data loss: the ONLY live copy of a rotating OAuth pair was
destroyed, and every later call returned "Not authenticated." Reproduced deterministically on
2026-09-10 — it happens on EVERY failed refresh, not as a rare race.

We cannot patch the vendor binary (it owns the file and the refresh). So we bracket every CLI
invocation instead and enforce the invariant the vendor breaks:

    a failed refresh can never leave the config dir without a usable credentials.json.

Mechanism: snapshot before, atomically restore after if the file went missing/empty/unparseable, and
clear locks that a dead refresh orphaned. Writes go through temp+fsync+rename, so a crash mid-write
leaves either the old file or the new one — never a zero-byte stub.

This module is deliberately network-free and filesystem-only, so it is unit-testable without a
container: rotation write-back is delivered through the `on_rotate` callback (see server.py).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Callable

CRED_NAME = "credentials.json"
PREV_NAME = "credentials.json.prev"
LOCK_NAME = "credentials.json.lock"
# Our own lock, distinct from the vendor CLI's, guarding every rotation-capable operation.
REFRESH_LOCK_NAME = "refresh.lock"
REFRESH_LOCK_TIMEOUT_S = float(os.environ.get("HIGGSFIELD_REFRESH_LOCK_TIMEOUT_S", "60"))

# A refresh is a single API round-trip. A lock older than this cannot belong to a live refresh, so it
# was orphaned by a crashed/killed one and would otherwise block every retry forever.
DEFAULT_LOCK_MAX_AGE_S = float(os.environ.get("HIGGSFIELD_LOCK_MAX_AGE_S", "120"))

# Serialise CLI calls in-process. Two concurrent refreshes would present the same refresh_token to
# Clerk; with rotation + reuse detection that can revoke the whole token family.
_CALL_LOCK = threading.Lock()

# Per-thread re-entrancy depth for exclusive_refresh_lock (see the note in that function).
_LOCAL = threading.local()

_AUTH_FAILURE_NEEDLES = (
    "no response received",
    "session expired",
    "not authenticated",
    "invalid_grant",
    "auth login",
    "unauthorized",
)


def cred_path(cfg_dir: str) -> str:
    return os.path.join(cfg_dir, CRED_NAME)


def atomic_write(path: str, data: str) -> None:
    """Write `data` to `path` atomically, owner-only. Never leaves a partial file at `path`."""
    if not isinstance(data, str):
        raise TypeError(f"atomic_write expects str, got {type(data).__name__}")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credguard-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        _unlink_quietly(tmp)
        raise


def _fsync_dir(directory: str) -> None:
    """Persist the rename itself, so a host crash can't resurrect the pre-rename directory entry."""
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _valid_bundle(text: str) -> dict[str, Any] | None:
    """A bundle is only worth keeping if it parses AND carries the refresh_token we depend on."""
    try:
        bundle = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(bundle, dict):
        return None
    if not bundle.get("refresh_token"):
        return None
    return bundle


def _read_valid(path: str) -> str | None:
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    return text if _valid_bundle(text) else None


def fingerprint(cfg_dir: str) -> str | None:
    """Content hash of the live bundle — lets callers detect rotation without handling the secret."""
    text = _read_valid(cred_path(cfg_dir))
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def read_live(cfg_dir: str) -> str | None:
    """The live bundle's text if it is usable, else None."""
    return _read_valid(cred_path(cfg_dir))


@contextlib.contextmanager
def exclusive_refresh_lock(cfg_dir: str, timeout_s: float = REFRESH_LOCK_TIMEOUT_S):
    """Cross-process mutual exclusion for anything that may rotate the credential.

    Clerk rotates the refresh_token on every exchange and applies reuse detection, so two refreshers
    racing can revoke the whole token family — the most likely cause of the 26-Aug credential dying.
    A threading.Lock only covers one process; this uses a real OS advisory lock (flock) on a file on
    the shared volume, so exclusion holds across processes and across containers mounting it.

    Held on a DEDICATED file, never the CLI's own credentials.json.lock, so we don't fight the vendor
    for its lock.
    """
    # Re-entrant within a thread. flock is per open-file-description, so a second fd opened by the
    # SAME process still conflicts — without this, a verification CLI call made from inside a
    # rotation (refresher.refresh_once -> verify -> _run) would deadlock against its own lock.
    depth = getattr(_LOCAL, "refresh_depth", 0)
    if depth:
        _LOCAL.refresh_depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.refresh_depth -= 1
        return

    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, REFRESH_LOCK_NAME)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.time() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"another process has held the Higgsfield refresh lock for >{timeout_s}s"
                    )
                time.sleep(0.2)
        # Record the holder so a stuck lock is diagnosable without guesswork.
        try:
            os.truncate(fd, 0)
            os.write(fd, f"pid={os.getpid()} since={time.time():.0f}\n".encode())
        except OSError:
            pass
        _LOCAL.refresh_depth = 1
        yield
    finally:
        _LOCAL.refresh_depth = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def clear_stale_lock(cfg_dir: str, max_age_s: float = DEFAULT_LOCK_MAX_AGE_S) -> bool:
    """Remove an orphaned refresh lock. Returns True if one was removed."""
    lock = os.path.join(cfg_dir, LOCK_NAME)
    try:
        age = time.time() - os.stat(lock).st_mtime
    except OSError:
        return False
    if age < max_age_s:
        return False  # a live refresh may still hold it
    _unlink_quietly(lock)
    return not os.path.exists(lock)


def snapshot(cfg_dir: str) -> bool:
    """Capture the current bundle so a destructive refresh is recoverable. True if captured."""
    text = _read_valid(cred_path(cfg_dir))
    if text is None:
        return False
    atomic_write(os.path.join(cfg_dir, PREV_NAME), text)
    return True


def restore_if_lost(cfg_dir: str) -> bool:
    """Put the snapshot back if the live bundle went missing/empty/unparseable. True if restored."""
    if _read_valid(cred_path(cfg_dir)) is not None:
        return False  # intact (possibly rotated) — never clobber it
    previous = _read_valid(os.path.join(cfg_dir, PREV_NAME))
    if previous is None:
        return False
    atomic_write(cred_path(cfg_dir), previous)
    return True


def guarded_run(
    cfg_dir: str,
    fn: Callable[[], Any],
    lock_max_age_s: float = DEFAULT_LOCK_MAX_AGE_S,
    on_rotate: Callable[[str], None] | None = None,
) -> tuple[Any, bool]:
    """Run `fn` (a CLI invocation) with credential loss made impossible.

    Returns (fn_result, restored). `restored` True means the CLI destroyed the bundle and we put it
    back — the call still failed, but the service is not bricked.
    """
    with _CALL_LOCK:
        clear_stale_lock(cfg_dir, lock_max_age_s)
        snapshot(cfg_dir)
        before = fingerprint(cfg_dir)
        try:
            result = fn()
        finally:
            restored = restore_if_lost(cfg_dir)
            if restored:
                # The refresh that just died left its lock behind. We hold _CALL_LOCK and fn has
                # returned, so nothing can legitimately hold it — remove it outright rather than by
                # age, which a little clock skew could otherwise defeat.
                _unlink_quietly(os.path.join(cfg_dir, LOCK_NAME))
        after = fingerprint(cfg_dir)
        if not restored and after is not None and after != before and on_rotate is not None:
            _notify_rotation(cfg_dir, on_rotate)
        return result, restored


def _notify_rotation(cfg_dir: str, on_rotate: Callable[[str], None]) -> None:
    """Hand the rotated bundle to the persistence hook. Best-effort: never fail the tool call."""
    text = _read_valid(cred_path(cfg_dir))
    if text is None:
        return
    try:
        on_rotate(text)
    except Exception:  # noqa: BLE001 - write-back is an optimisation, not a dependency
        pass


# --- health signal --------------------------------------------------------------------------------
#
# The credential rotated from 26 Aug while the staged seed stayed frozen, and NOTHING
# surfaced that divergence — the first symptom was a hard failure 14 days later. These helpers make
# the divergence itself observable, so a routine check catches the rot before a refresh failure does.
# They report lengths, expiries and booleans only: never a token value.

JOURNAL_NAME = "rotation.log"
JOURNAL_MAX_LINES = int(os.environ.get("HIGGSFIELD_JOURNAL_MAX_LINES", "100"))


def _utc(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def _bundle_facts(bundle: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
    """Non-secret facts about a bundle: lengths and expiry, never values."""
    if not bundle:
        return {f"{prefix}_present": False, f"{prefix}_expires_at_utc": None}
    expires_at = bundle.get("expires_at")
    return {
        f"{prefix}_present": True,
        f"{prefix}_access_token_len": len(bundle.get("access_token") or ""),
        f"{prefix}_refresh_token_len": len(bundle.get("refresh_token") or ""),
        f"{prefix}_expires_at": expires_at if isinstance(expires_at, (int, float)) else None,
        f"{prefix}_expires_at_utc": _utc(expires_at),
    }


def health_from_bundles(
    live: dict[str, Any] | None, seed: dict[str, Any] | None
) -> dict[str, Any]:
    """Compare the live bundle against the staged seed and classify the drift."""
    report: dict[str, Any] = {**_bundle_facts(live, "live"), **_bundle_facts(seed, "seed")}

    live_exp = report.get("live_expires_at")
    now = time.time()
    report["live_seconds_since_expiry"] = (now - live_exp) if isinstance(live_exp, (int, float)) else None
    # Routinely true between calls — the CLI refreshes on demand. Reported, never alarmed on.
    report["live_access_token_expired"] = (
        report["live_seconds_since_expiry"] > 0 if report["live_seconds_since_expiry"] is not None else None
    )

    if live and seed:
        report["seed_matches_live"] = live.get("refresh_token") == seed.get("refresh_token")
    else:
        report["seed_matches_live"] = None

    report["status"], report["warning"] = _classify(live, seed, report["seed_matches_live"])
    return report


def _classify(live, seed, matches) -> tuple[str, str | None]:
    if not live:
        return "no_credentials", (
            "No usable credentials on the volume. Higgsfield calls will fail until a human "
            "re-authenticates (see reauth_message)."
        )
    if not seed:
        return "no_seed", (
            "No staged seed. If this volume is ever lost there is nothing to re-seed from — stage "
            "the CURRENT bundle as HIGGSFIELD_CREDENTIALS_JSON."
        )
    if matches is False:
        return "seed_drift", (
            "SEED ROT: the live refresh_token has rotated away from the staged seed, so the seed "
            "would replay as invalid_grant and this volume is now the ONLY live copy. Re-stage the "
            "current bundle, or grant the machine identity write access so rotation persists itself."
        )
    return "ok", None


def health(cfg_dir: str, seed_text: str | None) -> dict[str, Any]:
    """health_from_bundles against the on-disk bundle. Never raises."""
    live_text = _read_valid(cred_path(cfg_dir))
    live = json.loads(live_text) if live_text else None
    seed = _valid_bundle(seed_text) if seed_text else None
    return health_from_bundles(live, seed)


def durable_drift(cfg_dir: str, durable_text: str | None) -> dict[str, Any]:
    """Has the DURABLE store fallen behind the live credential?

    Distinct from the boot-seed comparison in health(): that snapshot is frozen at boot, so it cannot
    answer "if this volume died right now, would the stored secret still work?". From 26 Aug to
    09 Sep the answer was no and nothing said so.
    """
    live = _valid_bundle(read_live(cfg_dir) or "") if read_live(cfg_dir) else None
    durable = _valid_bundle(durable_text) if durable_text else None
    out: dict[str, Any] = {
        "durable_seed_present": durable is not None,
        "durable_seed_expires_at_utc": _utc(durable.get("expires_at")) if durable else None,
    }
    if live is None or durable is None:
        out["durable_seed_matches_live"] = None
        out["durable_recoverable"] = False
        return out
    matches = live.get("refresh_token") == durable.get("refresh_token")
    out["durable_seed_matches_live"] = matches
    # "Recoverable" = if the volume vanished, re-seeding from durable storage would actually work.
    out["durable_recoverable"] = matches
    if not matches:
        out["durable_warning"] = (
            "DURABLE ROT: the credential in Infisical is not the live one. If this volume is lost, "
            "re-seeding will fail with invalid_grant and a human must re-authenticate. Grant the "
            "machine identity secrets:edit on /shared so rotation can persist itself."
        )
    return out


def journal_rotation(cfg_dir: str, persisted: bool, reason: str) -> None:
    """Append a token-free line recording that a rotation happened and whether it was persisted.

    Off-volume persistence may be unavailable (a read-only secrets identity), in which case this
    journal is the only durable record that the staged seed has fallen behind, and by how long.
    """
    live_text = _read_valid(cred_path(cfg_dir))
    expires = None
    if live_text:
        try:
            expires = _utc(json.loads(live_text).get("expires_at"))
        except ValueError:
            expires = None
    stamp = _utc(time.time())
    line = f"{stamp} rotated persisted={persisted} reason={reason} new_expiry={expires}\n"
    path = os.path.join(cfg_dir, JOURNAL_NAME)
    try:
        with open(path, "a") as fh:
            fh.write(line)
        _trim_journal(path)
    except OSError:
        pass  # the journal is diagnostics; never fail a render for it


def _trim_journal(path: str) -> None:
    """Keep the journal bounded so it can never fill the volume."""
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= JOURNAL_MAX_LINES:
        return
    atomic_write(path, "".join(lines[-JOURNAL_MAX_LINES:]))


def is_auth_failure(stderr: str) -> bool:
    """True when CLI output means 'credentials are dead', not 'this one call went wrong'.

    The vendored CLI reports a rejected refresh as "request failed (no response received)", which
    reads like a network fault. Verified 2026-09-10 that egress and Cloudflare are fine and the real
    upstream answer is Clerk `invalid_grant`, so this string must be treated as an auth failure.
    """
    if not stderr:
        return False
    low = stderr.lower()
    return any(needle.lower() in low for needle in _AUTH_FAILURE_NEEDLES)


def reauth_message() -> str:
    """The remedy a human can actually act on.

    The CLI's own canned hint ("Run: hf auth login") is a dead end here: this container is headless,
    the binary is not on PATH, and the OAuth flow needs a browser plus a localhost callback.
    """
    return (
        "Higgsfield credentials are no longer valid and cannot be refreshed headlessly "
        "(Clerk returns invalid_grant). A human must re-authenticate: run `higgsfield auth login` "
        "on a machine WITH a browser (npm i -g @higgsfield/cli), which writes "
        "~/.config/higgsfield/credentials.json, then re-stage that whole JSON as "
        "HIGGSFIELD_CREDENTIALS_JSON in Infisical /shared and restart this service. "
        "Do not keep using that CLI afterwards: refresh tokens rotate, and a second copy refreshing "
        "in parallel can revoke the token family."
    )
