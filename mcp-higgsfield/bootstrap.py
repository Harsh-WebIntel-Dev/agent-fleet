"""Choose and install the best available credential at boot.

WHY "NEWEST WINS" AND NOT "DON'T CLOBBER"
The original entrypoint refused to overwrite any existing credentials.json, to protect a rotated
token from being clobbered by the older staged seed. Correct intent, wrong rule: on 2026-09-10 a
human staged a FRESH credential in Infisical, and because the dead 26-Aug bundle on the volume still
*parsed*, the guard preserved the dead one and ignored the good one. The redeploy looked clean and
changed nothing.

The right rule compares `expires_at` and installs whichever candidate is newest:

    live volume bundle   vs   credguard snapshot (.prev)   vs   staged seed

That protects a rotated token (it is newer than the seed) AND lets a human recover by re-staging
(their fresh seed is newer than anything on the volume). Selection is pure and unit-tested.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any

import credguard

SEED_ENV = "HIGGSFIELD_CREDENTIALS_JSON"


def _expiry(bundle: dict[str, Any]) -> float:
    value = bundle.get("expires_at")
    return float(value) if isinstance(value, (int, float)) else float("-inf")


def choose_newest(candidates: list[tuple[str, str | None]]) -> tuple[str, dict[str, Any]] | None:
    """Pick the usable candidate with the latest expiry.

    `candidates` is an ordered list of (source_name, raw_text). Ties keep the earlier entry, so the
    order passed in acts as the tiebreak (volume before seed: prefer not to rewrite the file).
    """
    best: tuple[str, dict[str, Any]] | None = None
    for name, raw in candidates:
        if not raw:
            continue
        bundle = credguard._valid_bundle(raw)
        if bundle is None:
            continue
        if best is None or _expiry(bundle) > _expiry(best[1]):
            best = (name, bundle)
    return best


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def clear_boot_locks(cfg_dir: str) -> list[str]:
    """Clear the VENDOR's orphaned lock only.

    The CLI treats the mere existence of `credentials.json.lock` as the lock, so an orphaned one
    blocks every retry forever and must go — that is what stuck on 2026-09-09.

    We deliberately do NOT touch our own `refresh.lock`. That is an flock file: the lock lives in the
    kernel, not in the file, so removing it achieves nothing useful and is actively harmful — if
    another process still holds the inode, a newcomer would create a FRESH inode and take an
    INDEPENDENT lock, giving us the two concurrent refreshers the lock exists to prevent.
    """
    cleared = []
    path = os.path.join(cfg_dir, credguard.LOCK_NAME)
    try:
        size = os.path.getsize(path)
        os.unlink(path)
        cleared.append(f"{os.path.basename(path)} ({size}b)")
    except OSError:
        pass
    return cleared


def bootstrap(cfg_dir: str, seed_text: str | None) -> dict[str, Any]:
    """Clear locks, select the newest credential, install it if it isn't already live."""
    os.makedirs(cfg_dir, exist_ok=True)
    result: dict[str, Any] = {"cleared_locks": clear_boot_locks(cfg_dir)}

    live_raw = _read(credguard.cred_path(cfg_dir))
    chosen = choose_newest([
        ("volume", live_raw),
        ("snapshot", _read(os.path.join(cfg_dir, credguard.PREV_NAME))),
        ("seed", seed_text),
    ])
    if chosen is None:
        result["source"] = None
        result["installed"] = False
        return result

    source, bundle = chosen
    result["source"] = source
    result["expires_at"] = bundle.get("expires_at")
    text = json.dumps(bundle)
    if live_raw is not None and credguard._valid_bundle(live_raw) == bundle:
        result["installed"] = False  # already live; don't churn the file
    else:
        credguard.atomic_write(credguard.cred_path(cfg_dir), text)
        result["installed"] = True

    remaining = None
    if isinstance(bundle.get("expires_at"), (int, float)):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        remaining = bundle["expires_at"] - now
    result["seconds_remaining"] = remaining
    return result


def main() -> int:
    cfg_dir = os.environ.get(
        "HIGGSFIELD_CONFIG_DIR",
        os.path.join(os.path.expanduser("~"), ".config", "higgsfield"),
    )
    out = bootstrap(cfg_dir, os.environ.get(SEED_ENV))

    for lock in out["cleared_locks"]:
        print(f"[bootstrap] cleared orphaned lock {lock}", file=sys.stderr)

    if out["source"] is None:
        print("[bootstrap] WARNING: no usable credential from volume, snapshot or seed — "
              "Higgsfield tools will fail until a human runs `higgsfield auth login` (needs a "
              "browser) and re-stages HIGGSFIELD_CREDENTIALS_JSON.", file=sys.stderr)
        return 0

    iso = None
    if isinstance(out.get("expires_at"), (int, float)):
        iso = datetime.datetime.fromtimestamp(
            out["expires_at"], datetime.timezone.utc).isoformat()
    verb = "installed" if out["installed"] else "kept"
    print(f"[bootstrap] {verb} credential from {out['source']} (expires {iso})")

    remaining = out.get("seconds_remaining")
    if remaining is not None and remaining <= 0:
        print(f"[bootstrap] WARNING: that credential expired {abs(remaining) / 3600:.0f}h ago. "
              "Refresh tokens rotate, so it may already be superseded; expect invalid_grant. "
              "If so, a human must re-run `higgsfield auth login` and re-stage the secret.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
