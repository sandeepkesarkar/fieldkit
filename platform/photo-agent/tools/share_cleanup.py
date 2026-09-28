"""
Shared drain for Drive share links that may still be public (issue #80).

Publishing an Instagram Reel briefly makes the approved video publicly readable on Drive
(Instagram fetches it by URL — see tools/drive.py). The obligation to take that link down
again is recorded in instagram_state's pending_share_cleanups BEFORE the file is made
public (see instagram_state.record_share_intent()).

This module is what acts on that list, and it is called from BOTH cron workers:

  - upload_instagram.py, on every tick, under upload_instagram.lock, before its attempt;
  - upload_facebook.py, on every tick, AFTER its own publish path, holding no Instagram
    lock at all.

It used to live only in upload_instagram.py, which made revocation die with the worker
that created the link. Revoking a Drive permission needs Drive credentials and nothing
else, so the Facebook-side drain is not gated on Instagram being configured, on a Meta
token, or on Facebook having a job.

THE PROPERTY. No sequence may leave a public permission with no cleanup obligation
behind it. Revoking is idempotent and harmless, so it can be done early and often.
CLEARING an obligation is the dangerous act, because the obligation is registered
before the permission is granted: an attempt that stalls between the two, or after the
grant, can still create or keep the permission after someone else has revoked and
walked away. Age cannot tell whether that attempt is still alive — a claim lease
expiring lets a later invocation reclaim the job, but it does not stop or fence the
original process. So:

OWNERSHIP IS FENCED WITH A LOCK THE OS RELEASES. Every Instagram attempt runs inside
share_owner(): it creates a uniquely-named owner file and holds an exclusive flock on it
for the whole attempt, and the token naming that file is stored on every obligation the
attempt registers. flock is released when the attempt finishes — after its own final
revoke — or when the process dies, however it dies, and never while it is merely stalled.
So "owner file absent, or lockable" is proof that the attempt can no longer grant or keep
a permission for that file, and nothing weaker is accepted.

  - An obligation is CLEARED by a drain only if its owner was proven done BEFORE a revoke
    that then succeeded. Whatever the owner did before it finished, that revoke came after.
    (The owning attempt also clears its own obligation on its own exit path, after its own
    revoke — it is past its last use of the link by then.)
  - An obligation whose owner is still alive is never cleared by a drain. It may still be
    REVOKED — once it is due (below) — as often as every tick. If the stalled attempt then
    resumes and grants the permission, the entry is still there and the next tick takes it
    down again; the attempt itself cannot fetch through a revoked link, so it fails closed
    (a retryable failure, and its exit path revokes).
  - While the owner is alive, an obligation is due for revoking only once a revoke has
    already failed (attempts >= 1, which only happens after the attempt is finished with
    the link), or once it is older than ORPHANED_INTENT_AFTER_SECONDS. Younger than that,
    Instagram may be fetching the video right now, and pulling it would break the publish.
    A dead owner's link is revoked on the next drain, whatever its age.
  - Entries written before owner tokens existed carry none. Only a caller holding
    upload_instagram.lock may clear those — the old rule: every writer of such an entry
    held that lock for its whole attempt, so while the caller holds it none of them can be
    running. Any other caller revokes them when due but leaves them for the Instagram
    worker to clear.

Owner files and the flock both live on the local filesystem next to instagram_state.json,
which is where both workers already coordinate; this assumes, as the rest of the pipeline
does, that both crons run on the one machine that owns that directory.

CONCURRENCY. Two drains never run at once: each takes share_cleanup.lock non-blocking and
skips its drain on contention — the other drain is doing the same work this tick. Neither
publishing path takes that lock. Every read and write of pending_share_cleanups goes
through instagram_state's own functions, whose writes go through its single _transaction()
chokepoint; this module adds no write path of its own.

NOTHING IS CREATED FOR A CLIENT THAT DOES NOT USE INSTAGRAM. drain() looks for a state
file with outstanding entries first, without creating a directory, a file or a lock, and
only then takes the drain lock and re-reads the list under it.

ALERTS. A retry that fails bumps the entry through
instagram_state.record_share_cleanup(..., create_if_missing=False), which decides whether
to alert and stamps last_alerted_at inside one exclusive-lock transaction — so the daily
reminder reaches the admin from whichever worker is still running, at most once per
_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS however many workers retry. The caller supplies how
to send it, so each worker's alerts stay its own.

Share URLs are never logged or alerted: only the Drive file id and project name, as
before. Exception text is passed through redact_secrets() on its way to the log.
"""

import logging
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
# Bound by name rather than looked up through the fcntl module on each call: owner
# liveness MEANS "can this flock be taken", so it must always be the real call.
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from typing import IO, Callable, Iterator

from tools import drive, instagram_state
from tools.redaction import redact_secrets

_log = logging.getLogger(__name__)

# How old an obligation must be before a drain may revoke it while its owning attempt is
# still alive. At least upload_instagram._UPLOAD_LEASE_SECONDS (a test enforces it), which
# already covers the longest legitimate attempt: a Drive upload plus the 300s
# container-poll cap. Past this, a stalled attempt's link is revoked — but never cleared
# while the attempt lives; see the module docstring.
ORPHANED_INTENT_AFTER_SECONDS = 1800

LOCK_FILENAME = "share_cleanup.lock"
OWNER_DIRNAME = "share_owners"

_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def _owner_path(token: str):
    return instagram_state.DATA_DIR / OWNER_DIRNAME / f"{token}.lock"


@contextmanager
def share_owner() -> Iterator[str]:
    """Hold an ownership fence for one Instagram attempt; yields the owner token.

    Pass the token to instagram_state.record_share_intent() for every link the attempt
    creates. Exit (normal, exception, or process death) releases the flock; on a normal
    or exception exit the owner file is removed as well. The caller must exit only after
    its last revoke — upload_instagram.py wraps the whole of _process_upload().
    """
    token = secrets.token_hex(16)
    path = _owner_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        flock(f, LOCK_EX | LOCK_NB)  # a brand-new random name: never contended
        yield token
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        flock(f, LOCK_UN)
        f.close()


def _owner_is_alive(token) -> "bool | None":
    """True if the attempt that owns `token` may still act, False if provably done.

    None when the entry carries no usable token (written before owners existed, or
    hand-edited) — ownership is then unknown and the caller decides. Opens without
    creating: an owner file that is absent means its owner removed it on exit, or a drain
    removed it after proving the owner dead.
    """
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    try:
        fd = os.open(_owner_path(token), os.O_RDONLY)
    except FileNotFoundError:
        return False
    with os.fdopen(fd) as f:
        try:
            flock(f, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            return True
        flock(f, LOCK_UN)
        return False


def _forget_owner(token) -> None:
    """Remove a provably-done owner's file. Safe at any time once it is proven done."""
    if isinstance(token, str) and _TOKEN_RE.fullmatch(token):
        try:
            _owner_path(token).unlink()
        except FileNotFoundError:
            pass


def _try_acquire_drain_lock() -> "IO | None":
    """Try to take share_cleanup.lock exclusively, without waiting.

    Returns the open lock file on success (the caller must unlock and close it), or None
    if another worker's drain holds it. Only called once there is known to be something
    to drain, so it never creates files for a client that does not use Instagram.
    """
    instagram_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(instagram_state.DATA_DIR / LOCK_FILENAME, "w")
    try:
        flock(f, LOCK_EX | LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _is_due(entry: dict, now: datetime) -> bool:
    """True if entry's link may be revoked even though its owner may still be alive.

    An entry whose recorded_at is missing or unparseable counts as due:
    record_share_intent() always writes one, so only a hand-edited entry lacks it, and that
    is no evidence of a live fetch. Revoking is harmless to the property; the only cost of
    revoking early is one retryable Instagram attempt.
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
    """Revoke every outstanding share link this caller may act on; clear the provably-done.

    holds_instagram_lock — True only when the caller holds upload_instagram.lock and is not
    itself mid-attempt. It matters only for entries without an owner token; see the module
    docstring.

    send_alert(text) is called for a failure the admin should hear about now.

    Raises whatever instagram_state raises for an unreadable state file — by design, a
    corrupt file is not treated as an empty one. Callers that must not fail on that
    (upload_facebook.py) catch it; see there.
    """
    # Look before touching anything: list_share_cleanups() creates no file or directory,
    # so a client with no Instagram state ends this call with no new files.
    if not instagram_state.list_share_cleanups():
        return
    lock_f = _try_acquire_drain_lock()
    if lock_f is None:
        _log.debug("another worker is draining share-link cleanups — skipping this tick")
        return
    try:
        now = datetime.now(timezone.utc)
        # Re-read under the lock: the list may have changed since the unlocked look above.
        for entry in instagram_state.list_share_cleanups():
            _drain_entry(entry, now, send_alert, holds_instagram_lock)
    finally:
        flock(lock_f, LOCK_UN)
        lock_f.close()


def _drain_entry(entry, now, send_alert, holds_instagram_lock) -> None:
    """Apply the module's rule to one entry. See the module docstring."""
    file_id = entry.get("file_id")
    project_name = entry.get("project_name", "unknown")
    if not file_id:
        return
    token = entry.get("owner")
    # Decided BEFORE the revoke, deliberately: an owner proven done now stays done, so a
    # revoke that succeeds after this point is after anything the owner could have done.
    alive = _owner_is_alive(token)
    if alive is None:
        may_clear = holds_instagram_lock
        owner_may_be_using_it = not holds_instagram_lock
    else:
        may_clear = not alive
        owner_may_be_using_it = alive
    if owner_may_be_using_it and not _is_due(entry, now):
        _log.debug(
            "share link may be in use by a live Instagram attempt — leaving it: "
            "project=%s file_id=%s", project_name, file_id,
        )
        return
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
        return
    if not may_clear:
        # Revoked, but the attempt that registered it may still grant or keep the
        # permission, so the obligation stays and a later tick revokes again.
        _log.warning(
            "revoked a share link whose owning Instagram attempt may still be running — "
            "keeping the obligation until it is provably done: project=%s file_id=%s",
            project_name, file_id,
        )
        return
    instagram_state.clear_share_cleanup(file_id)
    _forget_owner(token)
    _log.info(
        "share-link revocation succeeded on retry: project=%s file_id=%s",
        project_name, file_id,
    )


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
