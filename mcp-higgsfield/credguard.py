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

# A refresh is a single API round-trip. A lock older than this cannot belong to a live refresh, so it
# was orphaned by a crashed/killed one and would otherwise block every retry forever.
DEFAULT_LOCK_MAX_AGE_S = float(os.environ.get("HIGGSFIELD_LOCK_MAX_AGE_S", "120"))

# Serialise CLI calls in-process. Two concurrent refreshes would present the same refresh_token to
# Clerk; with rotation + reuse detection that can revoke the whole token family.
_CALL_LOCK = threading.Lock()

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
