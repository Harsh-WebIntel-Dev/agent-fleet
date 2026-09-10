"""Proactive, single-flight OAuth refresh for the Higgsfield credential.

WHY WE OWN THE REFRESH INSTEAD OF THE CLI
The vendored `@higgsfield/cli` refreshes lazily — only once the access token is already unusable — and
on failure it deletes credentials.json outright (see credguard.py). Lazy-at-the-cliff means zero retry
budget: one bad minute and auth is gone until a human intervenes. It also puts rotation somewhere we
cannot observe or persist from.

So this module performs the refresh_token grant itself, against the same Clerk endpoint the CLI uses,
and then:
  1. installs the new bundle ATOMICALLY,
  2. VERIFIES it with a real CLI call, rolling back if the CLI rejects it,
  3. hands it to a persistence callback so it reaches durable storage immediately.

Because we keep the access token fresh, the CLI never reaches its own refresh path, which makes this
process the ONLY refresher in practice. That matters: Clerk rotates the refresh_token on every
exchange and applies reuse detection, so two refreshers racing can revoke the entire token family.
That is the most likely cause of the 26-Aug credential dying. Mutual exclusion is enforced with a
real OS advisory lock (flock) on the shared volume, so it holds across processes and containers, not
just across threads.

MEASURED FACTS (2026-09-10, from a freshly issued credential)
  * access token lifetime: ~86,400s (24h) — NOT the 2h a Clerk doc example suggests.
  * Cloudflare fronts the token endpoint and 403s bot-ish user agents (e.g. Python-urllib) while
    passing `higgsfield-cli/*`. The UA below is therefore load-bearing, not cosmetic.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import credguard

log = logging.getLogger("higgsfield.refresher")

TOKEN_URL = os.environ.get("HIGGSFIELD_OAUTH_TOKEN_URL", "https://clerk.higgsfield.ai/oauth/token")
# Public PKCE client id, read out of the CLI's own authorize URL. Not a secret by OAuth design.
CLIENT_ID = os.environ.get("HIGGSFIELD_OAUTH_CLIENT_ID", "sRGCQJvvJkPrrtRj")
# Cloudflare blocks bot-ish agents on the token endpoint; this one is verified to pass.
USER_AGENT = os.environ.get("HIGGSFIELD_OAUTH_UA", "higgsfield-cli/1.1.23")

# Refresh once less than this much life remains. With a 24h token, 8h leaves ~3 attempts before the
# cliff instead of the CLI's zero.
REFRESH_LEAD_S = float(os.environ.get("HIGGSFIELD_REFRESH_LEAD_S", "28800"))
CHECK_INTERVAL_S = float(os.environ.get("HIGGSFIELD_REFRESH_CHECK_S", "900"))
# After a rejected refresh, stop hammering: the token is dead and only a human can fix it.
BACKOFF_S = float(os.environ.get("HIGGSFIELD_REFRESH_BACKOFF_S", "3600"))
HTTP_TIMEOUT_S = float(os.environ.get("HIGGSFIELD_OAUTH_TIMEOUT_S", "20"))


class RefreshRejected(RuntimeError):
    """Clerk refused the grant (invalid_grant). Only an interactive re-auth fixes this."""


class RefreshUnavailable(RuntimeError):
    """Transient failure (network, 5xx). Worth retrying later."""


def seconds_remaining(bundle: dict[str, Any] | None, now: float | None = None) -> float | None:
    """Life left in the access token, or None if the bundle carries no usable expiry."""
    if not bundle:
        return None
    expires_at = bundle.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return None
    return expires_at - (now if now is not None else time.time())


def needs_refresh(bundle: dict[str, Any] | None, lead_s: float = REFRESH_LEAD_S,
                  now: float | None = None) -> bool:
    """True when the token is inside the proactive window (or already past expiry)."""
    remaining = seconds_remaining(bundle, now)
    if remaining is None:
        return False  # nothing we can reason about; leave it to the CLI/human
    return remaining < lead_s


def exchange_refresh_token(refresh_token: str) -> dict[str, Any]:
    """Perform the refresh_token grant. Returns Clerk's raw token response."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 400 and "invalid_grant" in detail:
            raise RefreshRejected(f"Clerk rejected the refresh token: {detail}") from exc
        raise RefreshUnavailable(f"token endpoint HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RefreshUnavailable(f"{type(exc).__name__}: {exc}") from exc


def merge_token_response(previous: dict[str, Any], response: dict[str, Any],
                         now: float | None = None) -> dict[str, Any]:
    """Build the next bundle in the CLI's on-disk shape.

    Immutable: returns a new dict. Unknown/absent fields are inherited from the previous bundle so we
    never drop something the CLI relies on (e.g. auth_version), and `expires_in` is converted to the
    absolute `expires_at` the CLI stores.
    """
    if not response.get("access_token"):
        raise RefreshUnavailable("token response carried no access_token")
    merged = {**previous}
    for field in ("access_token", "refresh_token", "token_type", "scope"):
        if response.get(field):
            merged[field] = response[field]
    expires_in = response.get("expires_in")
    if isinstance(expires_in, (int, float)):
        merged["expires_at"] = int((now if now is not None else time.time()) + expires_in)
    return merged


def refresh_once(cfg_dir: str, verify: Callable[[], bool] | None = None,
                 persist: Callable[[str], None] | None = None,
                 now: float | None = None,
                 lock_timeout_s: float | None = None) -> dict[str, Any]:
    """Rotate the credential once, under the cross-process lock, with verified rollback.

    Returns the installed bundle. Raises RefreshRejected / RefreshUnavailable on failure, leaving the
    previous credential in place either way.
    """
    lock_kwargs = {} if lock_timeout_s is None else {"timeout_s": lock_timeout_s}
    with credguard.exclusive_refresh_lock(cfg_dir, **lock_kwargs):
        current_text = credguard.read_live(cfg_dir)
        if current_text is None:
            raise RefreshUnavailable("no usable credentials.json to refresh")
        current = json.loads(current_text)

        response = exchange_refresh_token(current["refresh_token"])
        candidate = merge_token_response(current, response, now=now)
        if not candidate.get("refresh_token"):
            raise RefreshUnavailable("merged bundle lost its refresh_token")

        # Keep the pre-rotation copy so a rejected candidate can be put straight back.
        credguard.snapshot(cfg_dir)
        credguard.atomic_write(credguard.cred_path(cfg_dir), json.dumps(candidate))

        if verify is not None and not verify():
            credguard.atomic_write(credguard.cred_path(cfg_dir), current_text)
            raise RefreshUnavailable("new bundle failed CLI verification; rolled back")

        credguard.snapshot(cfg_dir)  # the verified bundle becomes the restore point
        if persist is not None:
            _persist_quietly(cfg_dir, candidate, persist)
        return candidate


def _persist_quietly(cfg_dir: str, bundle: dict[str, Any], persist: Callable[[str], None]) -> None:
    """Durable write-back must never fail a rotation that already succeeded."""
    try:
        persist(json.dumps(bundle))
    except Exception as exc:  # noqa: BLE001
        log.warning("durable persistence of rotated credential failed: %s", type(exc).__name__)
        credguard.journal_rotation(cfg_dir, persisted=False, reason=type(exc).__name__)


class RefreshLoop:
    """Background thread that keeps the access token well clear of its expiry."""

    def __init__(self, cfg_dir: str, verify=None, persist=None,
                 lead_s: float = REFRESH_LEAD_S, interval_s: float = CHECK_INTERVAL_S,
                 backoff_s: float = BACKOFF_S):
        self.cfg_dir = cfg_dir
        self.verify = verify
        self.persist = persist
        self.lead_s = lead_s
        self.interval_s = interval_s
        self.backoff_s = backoff_s
        self.last_error: str | None = None
        self.last_refresh_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hf-refresh", daemon=True)
        self._thread.start()
        log.info("proactive refresh loop started (lead=%ss check=%ss)", self.lead_s, self.interval_s)

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def seconds_until_due(self) -> float | None:
        """How long until the proactive window opens. Negative means a refresh is already due."""
        text = credguard.read_live(self.cfg_dir)
        if text is None:
            return None
        remaining = seconds_remaining(json.loads(text))
        return None if remaining is None else remaining - self.lead_s

    def tick(self) -> str:
        """One evaluation. Returns what happened, for logs and tests."""
        text = credguard.read_live(self.cfg_dir)
        if text is None:
            return "no_credentials"
        bundle = json.loads(text)
        if not needs_refresh(bundle, self.lead_s):
            return "not_due"
        try:
            refresh_once(self.cfg_dir, verify=self.verify, persist=self.persist)
        except RefreshRejected as exc:
            self.last_error = str(exc)
            log.error(
                "PROACTIVE REFRESH REJECTED — the refresh token is dead and no retry will help. "
                "A human must re-authenticate (see credguard.reauth_message()). %s", exc)
            return "rejected"
        except RefreshUnavailable as exc:
            self.last_error = str(exc)
            log.warning("proactive refresh deferred: %s", exc)
            return "deferred"
        self.last_error = None
        self.last_refresh_at = time.time()
        log.info("proactive refresh succeeded; credential rotated and persistence attempted")
        return "refreshed"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                outcome = self.tick()
            except Exception as exc:  # noqa: BLE001 - a loop crash would silently end refreshing
                log.warning("refresh loop error: %s: %s", type(exc).__name__, exc)
                outcome = "error"
            delay = self.backoff_s if outcome in ("rejected", "error") else self.interval_s
            self._stop.wait(delay)
