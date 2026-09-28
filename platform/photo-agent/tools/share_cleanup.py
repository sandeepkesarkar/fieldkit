"""
Shared drain for Drive share links that may still be public (issue #80).

Publishing an Instagram Reel briefly makes the approved video publicly readable on Drive
(Instagram fetches it by URL — see tools/drive.py). The obligation to take that link down
again is recorded in instagram_state's pending_share_cleanups BEFORE the file is made
public (see instagram_state.record_share_intent()).

This module is what acts on that list, and it is called from BOTH cron workers:

  - upload_instagram.py, on every tick, under upload_instagram.lock;
  - upload_facebook.py, on every tick, holding no Instagram lock at all.

It used to live only in upload_instagram.py. That made revocation die with the worker
that created the link: an Instagram worker killed mid-attempt (after the share call,
before the revoke), or an Instagram cron entry removed after that, left the link public,
the obligation correctly recorded, and nothing running that would retry it or remind
anyone about it. Draining from the Facebook worker too means revocation survives either
worker stopping. Revoking a Drive permission needs Drive credentials and nothing else —
no Instagram account id, no Meta token — so the Facebook-side drain is not gated on
Instagram being configured, nor on Facebook having a job, nor on a Page token.

WHICH ENTRIES EACH WORKER MAY REVOKE. An entry with attempts == 0 is a bare intent
written the moment the Drive file existed, and it is the ONLY kind of entry that can
belong to a link Instagram is still fetching: the live attempt that registered it clears
it on its own exit path. Revoking such a link mid-attempt would make that publish fail.

  - The Instagram worker holds upload_instagram.lock while it drains, before it starts
    an attempt of its own. No other Instagram attempt can be running (the OS releases the
    lock on process death), so every entry it sees is orphaned: it drains all of them.
  - Any other caller — the Facebook worker — cannot know whether an Instagram attempt is
    in flight, and deliberately does not try to find out by taking upload_instagram.lock
    (that would make a Facebook tick able to make an Instagram tick skip, against FR-013).
    It revokes only entries that are DUE regardless of any live attempt (_is_orphaned):
      * attempts >= 1 — a revoke has already been tried and failed, which only happens
        after the attempt that used the link has finished with it; or
      * a bare intent older than ORPHANED_INTENT_AFTER_SECONDS — longer than any Instagram
        attempt can legitimately run (it equals upload_instagram's claim lease, the bound
        after which Instagram itself treats an attempt as abandoned), so the attempt that
        registered it is dead or finished.
    So after an Instagram worker dies mid-attempt, the Facebook worker revokes its link
    within ORPHANED_INTENT_AFTER_SECONDS plus one tick.

CONCURRENCY. Both crons fire every minute. Two drains never run at once: each takes
share_cleanup.lock (next to instagram_state.json) NON-BLOCKING and skips its drain on
contention — the other drain is doing the same work this tick. Skipping, never waiting,
is what keeps a slow Drive call in one worker from delaying the other worker's publish
(FR-013); neither worker's publishing path ever takes this lock. Every read and write of
pending_share_cleanups still goes through instagram_state's own functions, whose writes go
through its single _transaction() chokepoint; this module adds no write path of its own.

ALERTS. A retry that fails bumps the entry through
instagram_state.record_share_cleanup(..., create_if_missing=False), which decides whether
to alert and stamps last_alerted_at inside one exclusive-lock transaction — so the daily
reminder reaches the admin from whichever worker is still running, and at most once per
_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS however many workers retry. The caller supplies how
to send it, so each worker's alerts stay its own.

Share URLs are never logged or alerted: only the Drive file id and project name, as
before. Exception text is passed through redact_secrets() on its way to the log.
"""

import fcntl
import logging
from datetime import datetime, timezone
from typing import IO, Callable

from tools import drive, instagram_state
from tools.redaction import redact_secrets

_log = logging.getLogger(__name__)

# How old a bare intent (attempts == 0) must be before a worker that does NOT hold
# upload_instagram.lock may treat it as orphaned. Must be at least
# upload_instagram._UPLOAD_LEASE_SECONDS (a test enforces it): that is the bound after
# which Instagram itself regards an attempt as abandoned, and it already covers the
# longest legitimate attempt (a Drive upload plus the 300s container-poll cap).
ORPHANED_INTENT_AFTER_SECONDS = 1800

LOCK_FILENAME = "share_cleanup.lock"


def _try_acquire_drain_lock() -> "IO | None":
    """Try to take share_cleanup.lock exclusively, without waiting.

    Returns the open lock file on success (the caller must unlock and close it), or None
    if another worker's drain holds it. Lives next to instagram_state.json because it
    guards that file's pending_share_cleanups list.
    """
    instagram_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(instagram_state.DATA_DIR / LOCK_FILENAME, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _is_orphaned(entry: dict, now: datetime) -> bool:
    """True if no live Instagram attempt can still need entry's link.

    See the module docstring for the rule. An intent whose recorded_at is missing or
    unparseable counts as orphaned: record_share_intent() always writes one, so only a
    hand-edited entry lacks it, and that is no evidence of a live attempt — while leaving
    a possibly-public link alone on no evidence is the failure this module exists to stop.
    """
    if entry.get("attempts", 0) >= 1:
        return True
    raw = entry.get("recorded_at")
    try:
        recorded = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return (now - recorded).total_seconds() >= ORPHANED_INTENT_AFTER_SECONDS


def drain(send_alert: Callable[[str], None], *, holds_instagram_lock: bool) -> None:
    """Retry every outstanding share-link revocation this caller may act on.

    holds_instagram_lock — True only when the caller holds upload_instagram.lock and is not
    itself mid-attempt, which makes every entry orphaned. False restricts the drain to
    entries _is_orphaned() says are due. See the module docstring.

    send_alert(text) is called for a failure the admin should hear about now. A revoke that
    succeeds clears its entry; one that fails keeps it, bumped, for the next tick.

    Raises whatever instagram_state raises for an unreadable state file — by design, a
    corrupt file is not treated as an empty one. Callers that must not fail on that
    (upload_facebook.py) catch it; see there.
    """
    lock_f = _try_acquire_drain_lock()
    if lock_f is None:
        _log.debug("another worker is draining share-link cleanups — skipping this tick")
        return
    try:
        now = datetime.now(timezone.utc)
        for entry in instagram_state.list_share_cleanups():
            file_id = entry.get("file_id")
            project_name = entry.get("project_name", "unknown")
            if not file_id:
                continue
            if not holds_instagram_lock and not _is_orphaned(entry, now):
                _log.debug(
                    "share link may still be in use by a live Instagram attempt — "
                    "leaving it: project=%s file_id=%s", project_name, file_id,
                )
                continue
            try:
                drive.revoke_share_link(file_id)
            except RuntimeError as exc:
                _log.error(
                    "retry of share-link revocation still failing: project=%s file_id=%s "
                    "error=%s", project_name, file_id, redact_secrets(str(exc)),
                )
                updated = instagram_state.record_share_cleanup(
                    file_id, project_name, create_if_missing=False
                )
                if updated:
                    send_alert(alert_text(updated))
                continue
            instagram_state.clear_share_cleanup(file_id)
            _log.info(
                "share-link revocation succeeded on retry: project=%s file_id=%s",
                project_name, file_id,
            )
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def alert_text(entry: dict) -> str:
    """Build the admin alert for a Drive share link that could not be revoked.

    The first alert and every re-escalation use this same wording, differing only in the
    attempt count, so the message never promises follow-up it does not deliver: FieldKit
    really does keep retrying, and really does keep reminding.
    """
    attempts = entry.get("attempts", 1)
    project_name = entry.get("project_name", "unknown")
    file_id = entry.get("file_id", "unknown")
    since = entry.get("recorded_at", "unknown")
    return (
        f"⚠️ Instagram: could not remove the temporary public link for {project_name} "
        f"(Drive file {file_id}). The video may still be publicly reachable.\n"
        f"Failed attempts: {attempts}, first failed: {since}.\n"
        "FieldKit keeps retrying every cron tick and will remind you daily until it "
        "succeeds. To fix it now, remove the file's 'Anyone with the link' permission "
        "in Drive."
    )
