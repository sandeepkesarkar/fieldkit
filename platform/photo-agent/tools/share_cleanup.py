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
that created the link. Deleting a Drive file needs Drive credentials and nothing
else, so the Facebook-side drain is not gated on Instagram being configured, on a Meta
token, or on Facebook having a job.

THE PROPERTY. No sequence may leave a public permission with no cleanup obligation
behind it.

WHAT RETIRES AN OBLIGATION: CONFIRMED PERMANENT DELETION, NOTHING LESS. The shared file is
a disposable copy that create_temporary_share_link() uploads fresh for each attempt, used
for nothing but Instagram's fetch. drive.delete_temporary_share() revokes (fast first
step), permanently deletes it with files.delete (never trash: a trashed file stays
reachable), and confirms files.get answers 404. Only that retires an obligation — in the
owning attempt's own exit path and in every drain alike. "No public permission visible
right now" is NOT enough: a permission POST whose response was lost may still be applied
by Drive after someone has looked, seen nothing, and moved on (round 3 of issue #80's
review), and nothing local can prove a remote request has finished acting. A file that no
longer exists cannot become public whenever such a grant lands. Deletion that fails, or
cannot be confirmed, keeps the obligation — retried by both crons, re-alerted daily.

PROVENANCE BEFORE PERMANENT DELETION. The file id comes from instagram_state.json, so a
corrupted or hand-edited entry could name a client's real file. Each entry therefore
records that it is a temporary copy, the folder it was uploaded into and its name
(record_share_intent), and drive.delete_temporary_share() checks the live file against them
and the configured DRIVE_ROOT_FOLDER_ID before touching it (provenance_for). A mismatch is
refused with NOTHING changed — no revoke, no delete — and treated like any failed cleanup:
the obligation stays, both crons retry it, and the admin is alerted daily, with wording
that says it was refused and why.

WHEN A DRAIN MAY ACT: THE OWNER FENCE. Deleting a live attempt's copy while Instagram is
still fetching it would break that publish, so a drain must know whether the attempt that
registered an obligation can still be using it. Age cannot say: a claim lease expiring lets
a later invocation reclaim the job, but does not stop the original process. So every
Instagram attempt runs inside share_owner(): it creates a uniquely-named owner file and
holds an exclusive flock on it for the whole attempt, and the token naming that file is
stored on every obligation the attempt registers. flock is released when the attempt
finishes or when the process dies, however it dies, and never while it is merely stalled.

  - Owner provably done (fence released, or its file gone): delete now, whatever the age.
    A killed worker's copy goes on the next tick of either cron.
  - Owner still alive: only once due — a cleanup already failed (attempts >= 1, which only
    happens after the attempt is finished with the link), or the entry is older than
    ORPHANED_INTENT_AFTER_SECONDS. Younger than that, Instagram may be fetching right now.
    A stalled attempt whose copy is deleted cannot re-expose it; when it resumes it fails
    (retryably) without publishing.
  - No owner token (entries written before owner tokens existed): the caller holding
    upload_instagram.lock acts at once — every writer of such an entry held that lock for
    its whole attempt; any other caller waits until the entry is due.

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

from tools import drive, instagram_state, state
from tools.redaction import redact_secrets

_log = logging.getLogger(__name__)

# How old an obligation must be before a drain may delete its copy while the owning attempt
# is still alive. At least upload_instagram._UPLOAD_LEASE_SECONDS (a test enforces it),
# which already covers the longest legitimate attempt: a Drive upload plus the 300s
# container-poll cap. This bounds only how soon a STALLED attempt's exposure ends; it is
# not what makes retiring an obligation safe — confirmed deletion is.
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


def sweep_dead_owner_files() -> int:
    """Remove owner files left by attempts that died before registering any obligation.

    A killed attempt releases its flock but leaves its file; if it had registered an
    obligation the drain removes the file once that obligation is retired, but an attempt
    killed before creating any link leaves one nobody refers to. Removed only when ALL of:
    older than ORPHANED_INTENT_AFTER_SECONDS (share_owner() creates the file a moment
    before it locks it, so a brand-new file may be unlocked and still live), lockable now,
    and named by no pending obligation. Returns how many were removed. Creates nothing.
    """
    owners_dir = instagram_state.DATA_DIR / OWNER_DIRNAME
    if not owners_dir.is_dir():
        return 0
    referenced = {e.get("owner") for e in instagram_state.list_share_cleanups()}
    cutoff = datetime.now(timezone.utc).timestamp() - ORPHANED_INTENT_AFTER_SECONDS
    removed = 0
    for path in owners_dir.glob("*.lock"):
        token = path.stem
        if token in referenced or not _TOKEN_RE.fullmatch(token):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except FileNotFoundError:
            continue
        if _owner_is_alive(token) is False:
            _forget_owner(token)
            removed += 1
    return removed


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
    """True if entry's copy may be deleted even though its owner may still be alive.

    An entry whose recorded_at is missing or unparseable counts as due:
    record_share_intent() always writes one, so only a hand-edited entry lacks it, and that
    is no evidence of a live fetch. Acting early is harmless to the property; the only cost
    is one retryable Instagram attempt.
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
    """Delete every outstanding share copy this caller may act on; retire the confirmed.

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
    alive = _owner_is_alive(token)
    if alive is None:
        owner_may_be_using_it = not holds_instagram_lock
    else:
        owner_may_be_using_it = alive
    if owner_may_be_using_it and not _is_due(entry, now):
        _log.debug(
            "share link may be in use by a live Instagram attempt — leaving it: "
            "project=%s file_id=%s", project_name, file_id,
        )
        return
    try:
        drive.delete_temporary_share(file_id, provenance=provenance_for(entry))
    except RuntimeError as exc:
        refused = isinstance(exc, drive.TemporaryShareRefused)
        _log.error(
            "%s: project=%s file_id=%s error=%s",
            "refused to delete a file that does not look like a temporary share copy"
            if refused else "retry of temporary share deletion still failing",
            project_name, file_id, redact_secrets(str(exc)),
        )
        updated = instagram_state.record_share_cleanup(
            file_id, project_name, create_if_missing=False
        )
        if updated:
            send_alert(alert_text(updated, refused=exc if refused else None))
        return
    # Confirmed permanently deleted: nothing — a late grant, a resumed attempt, anyone —
    # can make this file public again, so the obligation is retired whoever still lives.
    instagram_state.clear_share_cleanup(file_id)
    if alive is False:
        _forget_owner(token)
    _log.info(
        "temporary share copy deleted and confirmed gone: project=%s file_id=%s",
        project_name, file_id,
    )


def provenance_for(entry: dict) -> dict:
    """What drive.delete_temporary_share() must verify before deleting entry's file.

    - root_folder_id: the CONFIGURED DRIVE_ROOT_FOLDER_ID, read at cleanup time. The copy
      must sit directly in it; unset means nothing can be verified, so deletion is refused.
    - recorded_parent_id / expected_name: what record_share_intent() stored when the copy
      was created. An entry written before provenance was recorded has neither
      (temporary_copy is absent): it is checked conservatively — it must still be a video
      directly inside the configured root, not a folder, not the root, not protected — but
      its name cannot be checked. That is stated rather than hidden: a legacy entry pointing
      at some other video directly in the root folder would pass.
    - protected_ids: the approved video's Drive id from the pending approval, when one is
      recorded. The approved video lives in the project folder, not the root, so the parent
      check already excludes it; this is an extra, explicit guard. Unreadable approval state
      skips only this extra check (logged) — it must not stop a public copy being removed.
    """
    provenance_recorded = bool(entry.get("temporary_copy"))
    protected = []
    try:
        pending = state.get_pending_approval()
    except Exception as exc:  # noqa: BLE001 — see docstring
        _log.warning("could not read approval state for protected ids: %s", type(exc).__name__)
        pending = None
    if pending and pending.get("drive_video_file_id"):
        protected.append(pending["drive_video_file_id"])
    return {
        "root_folder_id": os.environ.get("DRIVE_ROOT_FOLDER_ID") or None,
        "recorded_parent_id": entry.get("parent_id") if provenance_recorded else None,
        "expected_name": entry.get("name") if provenance_recorded else None,
        "protected_ids": protected,
    }


def alert_text(entry: dict, refused: "Exception | None" = None) -> str:
    """Build the admin alert for a Drive share link that could not be revoked.

    The first alert and every re-escalation use this same wording, differing only in the
    attempt count, so the message never promises follow-up it does not deliver: FieldKit
    really does keep retrying, and really does keep reminding.
    """
    attempts = entry.get("attempts", 1)
    project_name = entry.get("project_name", "unknown")
    file_id = entry.get("file_id", "unknown")
    since = entry.get("recorded_at", "unknown")
    if refused is not None:
        return (
            f"⚠️ Instagram: FieldKit REFUSED to delete Drive file {file_id} recorded for "
            f"{project_name} — {refused}. It does not look like the temporary copy FieldKit "
            "created, so nothing was changed.\n"
            f"Attempts: {attempts}, first recorded: {since}.\n"
            "FieldKit keeps the cleanup entry and will remind you daily. Check that file in "
            "Drive: if it is a leftover temporary copy, delete it permanently (and empty it "
            "from the trash); if it is client content, the entry in instagram_state.json "
            "is wrong and needs checking."
        )
    return (
        f"⚠️ Instagram: could not remove the temporary public link for {project_name} "
        f"(Drive file {file_id}). The video may still be publicly reachable.\n"
        f"Failed attempts: {attempts}, first failed: {since}.\n"
        "FieldKit keeps retrying every cron tick and will remind you daily until it "
        "succeeds. To fix it now, permanently delete that file in Drive (delete it, "
        "then empty it from the trash — a trashed file is still reachable)."
    )
