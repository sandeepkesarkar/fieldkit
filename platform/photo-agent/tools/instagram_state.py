"""
State manager for the Instagram video upload pipeline (Feature 005).

Manages:
  $FIELDKIT_DATA_DIR/photo-agent/instagram_state.json
    — pending InstagramUploadJob record, published idempotency keys, and a
      capped history of publish outcomes (published_history)

This module deliberately mirrors tools/facebook_state.py's structure, locking
discipline, and claim semantics field-for-field; only the record's field names
differ (ig_business_account_id instead of page_id, ig_post_id instead of
fb_post_id, plus the Instagram-specific container_id). Keeping the two modules
shaped alike is what makes upload_instagram.py reviewable side-by-side with
upload_facebook.py. Read facebook_state.py's docstrings for the full rationale
behind the claim/lease/compare-and-update design summarized below.

instagram_state.json is a SEPARATE file from facebook_state.json, in the same
directory. That separation is what structurally guarantees FR-013 (platform
independence): a Facebook job's state, lock, and claim namespace are never
touched by an Instagram job for the same video, and vice versa. The two are
correlated only by sharing the same idempotency_key.

pending_instagram_upload is always cleared (set back to null) once a job
resolves — via mark_published() or mark_failed(), both terminal.

claim_pending_upload() is the only concurrency-safe way to start (or resume)
uploading the pending job: it collapses a read, a staleness check, and a
status/attempt-count transition into ONE exclusive-lock read-modify-write, so
two overlapping cron invocations can never both observe an unclaimed job and
both call the Instagram Graph API. Every mutator that takes an idempotency_key
(set_container_id, mark_published, mark_failed, release_claim,
clear_pending_upload) only acts if the CURRENT pending record still has that
key — compare-and-update, not blind overwrite.

container_id is Instagram-specific and has no Facebook counterpart: the Graph
API's video publish is a two-phase create-container → publish flow, so an
attempt has an intermediate server-side handle. It SURVIVES across attempts, and
that is load-bearing for FR-011 rather than merely convenient.

This inverts an earlier decision in this file, which scoped container_id to a
single attempt and had both claim_pending_upload() and release_claim() clear it,
on the reasoning that a retry must never republish a previous attempt's
container. That reasoning had it backwards. The irreversible external side effect
is publish_container(); the durable record of it is mark_published(). A crash,
kill, or lost HTTP response between the two leaves Meta holding a published Reel
that FieldKit has no record of — and the old rule then DISCARDED the one handle
that could have revealed it, so the next attempt created a second container and
published the same video again. Duplicate Reels on a real client account, which
is precisely what FR-011 forbids and is irreversible.

Keeping container_id lets upload_instagram.py ask Instagram what actually
happened before it publishes anything: a container whose status_code is PUBLISHED
is authoritative proof the Reel is already live. The "never republish a previous
attempt's container" rule is preserved, and is now enforced where it belongs — by
that reconciliation, not by throwing the evidence away. See
upload_instagram.py's _classify_prior_container(), and record_recovered_publish()
below for how a publish discovered this way is recorded.

FB_PAGE_ACCESS_TOKEN is never stored here. Sensitive token values are never logged.
"""

import copy
import fcntl
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_data_dir_raw = os.environ.get("FIELDKIT_DATA_DIR", "")
if not _data_dir_raw:
    raise RuntimeError("FIELDKIT_DATA_DIR is not set — add it to your client .env file")
DATA_DIR = Path(_data_dir_raw) / "photo-agent"
STATE_FILE = DATA_DIR / "instagram_state.json"

__all__ = [
    "get_pending_upload",
    "set_pending_upload",
    "claim_pending_upload",
    "release_claim",
    "clear_pending_upload",
    "set_container_id",
    "mark_published",
    "mark_failed",
    "is_published",
    "find_published",
    "has_outstanding_job",
    "record_recovered_publish",
    "mark_publish_attempted",
    "record_publish_reconciliation",
    "list_publish_reconciliations",
    "clear_publish_reconciliation",
    "has_unresolved_publish",
    "record_share_intent",
    "record_share_cleanup",
    "list_share_cleanups",
    "clear_share_cleanup",
]

_REQUIRED_UPLOAD_KEYS = frozenset({
    "project_name",
    "video_local_path",
    "ig_business_account_id",
    "status",
    "attempt_count",
    "last_attempt_at",
    "triggered_at",
    "idempotency_key",
    "container_id",
    "ig_post_id",
})

_DEFAULTS = {
    "pending_instagram_upload": None,
    "published_idempotency_keys": [],
    "published_history": [],
    "pending_share_cleanups": [],
    "pending_publish_reconciliations": [],
}

# Cap on published_history so instagram_state.json doesn't grow without bound over a client's
# lifetime. Only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept.
_PUBLISH_HISTORY_LIMIT = 100

# How long to wait before re-alerting the admin about a share link that STILL hasn't been
# revoked. A dangling public link is a standing privacy problem, so one alert at creation is
# not enough — if the retries keep failing, the reminder has to keep coming back or the
# problem silently becomes permanent. Deliberately a wall-clock interval rather than a retry
# count: the cron cadence is a deployment detail, "you have been told once a day" is not.
_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS = 24 * 60 * 60

# Same cadence, same reasoning, for a container whose publish outcome is still unknown
# (see the pending_publish_reconciliations section at the end of this module). A Reel that
# may or may not be live on a client's account is at least as worth a daily reminder as a
# public link, and for the same reason: the retry loop is silent, so without a recurring
# alert a permanently-unresolvable container would be mentioned once and then never again.
_PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS = 24 * 60 * 60


def _read(file_obj) -> dict:
    """Read and parse instagram_state.json from an open, locked file object."""
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        # deepcopy, NOT dict(): a shallow copy would alias _DEFAULTS' list values, so any
        # caller appending to e.g. published_idempotency_keys on an empty/absent state file
        # would mutate the module-level defaults for the rest of the process.
        return copy.deepcopy(_DEFAULTS)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("instagram_state.json is corrupt: %s", exc)
        raise RuntimeError("instagram_state.json is corrupt — delete or restore it manually") from exc


def _write(file_obj, data: dict) -> None:
    """Overwrite instagram_state.json via an open, locked file object."""
    content = json.dumps(data, indent=2)
    file_obj.seek(0)
    file_obj.write(content)
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _open_for_write():
    """Open instagram_state.json for read+write, creating it if absent."""
    fd_no = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd_no, "r+")


def get_pending_upload() -> dict | None:
    """Return the pending InstagramUploadJob record, or None if absent or null."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                return data.get("pending_instagram_upload")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return None


def set_pending_upload(record: dict) -> None:
    """Write the pending InstagramUploadJob.

    Raises ValueError on missing keys or on an idempotency_key that has already
    been published (the duplicate-post guard behind FR-011/SC-006).
    """
    missing = _REQUIRED_UPLOAD_KEYS - set(record.keys())
    if missing:
        raise ValueError(f"set_pending_upload: missing required keys: {missing}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            key = record["idempotency_key"]
            if key in data.get("published_idempotency_keys", []):
                raise ValueError(
                    f"set_pending_upload: idempotency_key {key!r} already in published_idempotency_keys"
                )
            # An unresolved publish holds this key hostage until Meta is definitive about
            # it. Accepting a new job here would let a re-approval create and publish a
            # SECOND container for a Reel that may already be live — the duplicate FR-011
            # forbids, and the one an idempotency check alone cannot catch, because a
            # publish whose response was lost never made it into published_idempotency_keys.
            # check_approval.py checks has_unresolved_publish() first and reports it
            # properly; this is the backstop that makes the guarantee structural.
            if _unresolved_publish_entry(data, key) is not None:
                raise ValueError(
                    f"set_pending_upload: idempotency_key {key!r} has an unresolved publish "
                    "awaiting reconciliation with Instagram"
                )
            data["pending_instagram_upload"] = record
            _write(f, data)
            logger.info("set_pending_upload: project=%s key=%s", record.get("project_name"), key)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _update_pending(idempotency_key: str, updater) -> bool:
    """Read; if the CURRENT pending record's idempotency_key still matches idempotency_key,
    apply updater(record, data) and write — all under one exclusive lock. Returns True if the
    updater ran, False if there was nothing matching to update (no pending record, already
    resolved/cleared, or replaced by a newer job under a different key).

    This compare-before-update is what makes mark_published/mark_failed/clear_pending_upload
    safe to call from a caller holding an earlier snapshot: they can never mutate or destroy a
    DIFFERENT job that's since taken the pending slot's place.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            record = data.get("pending_instagram_upload")
            if record is None or record.get("idempotency_key") != idempotency_key:
                return False
            updater(record, data)
            _write(f, data)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def clear_pending_upload(expected_idempotency_key: str) -> bool:
    """Clear pending_instagram_upload back to null — but only if the CURRENT pending record's
    idempotency_key still matches expected_idempotency_key (compare-and-clear). Leaves
    published_idempotency_keys / published_history intact either way.

    Returns True if cleared, False if left untouched.
    """
    def _update(record, data):
        data["pending_instagram_upload"] = None
    cleared = _update_pending(expected_idempotency_key, _update)
    logger.info("clear_pending_upload: key=%s cleared=%s", expected_idempotency_key, cleared)
    return cleared


def _has_elapsed(iso_timestamp: str | None, seconds: int, now_dt: datetime) -> bool:
    """Return True if iso_timestamp is None, unparseable, or at least `seconds` old. None/
    unparseable count as elapsed: a cooldown/lease exists to prevent premature retries, not to
    wedge a job forever because of a missing or corrupt timestamp.
    """
    if iso_timestamp is None:
        return True
    try:
        last_dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        logger.warning("unparseable timestamp=%r — treating as elapsed", iso_timestamp)
        return True
    return now_dt - last_dt >= timedelta(seconds=seconds)


def claim_pending_upload(
    idempotency_key: str, *, cooldown_seconds: int, max_attempts: int, lease_seconds: int
) -> str:
    """Atomically validate and claim the pending job for upload, in a single exclusive-lock
    read-modify-write. This is the ONLY concurrency-safe way to start an upload attempt —
    see facebook_state.claim_pending_upload()'s docstring for the full rationale, which this
    function mirrors exactly.

    lease_seconds bounds how long a claim (status=='uploading') is treated as still genuinely
    in-progress. Note that an Instagram attempt can legitimately run much longer than a
    Facebook one: the container poll alone is capped at 300s (see instagram_api's
    _MAX_POLL_ATTEMPTS × _POLL_INTERVAL_SECONDS), on top of the Drive upload that precedes it.
    Pick lease_seconds comfortably above that ceiling — see upload_instagram.py's
    _UPLOAD_LEASE_SECONDS.

    Returns one of:
      "mismatch"        — no pending record, or its idempotency_key has changed since the
                           caller's last read. Nothing to do.
      "in_flight"       — status is 'uploading' and the lease hasn't expired: genuinely claimed
                           by another still-running invocation. Do not retry.
      "cooldown"        — last_attempt_at is too recent (within cooldown_seconds). Nothing to do.
      "stale_published" — idempotency_key is already in published_idempotency_keys; cleared.
      "stale_failed"    — status was already 'failed'; cleared.
      "exhausted"       — attempt_count already at max_attempts; cleared (terminal failure).
      "claimed"         — success: status is now 'uploading', attempt_count/last_attempt_at
                           already advanced for this attempt.
    """
    now_dt = datetime.now(timezone.utc)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            record = data.get("pending_instagram_upload")
            if record is None or record.get("idempotency_key") != idempotency_key:
                return "mismatch"

            if idempotency_key in data.get("published_idempotency_keys", []):
                data["pending_instagram_upload"] = None
                _write(f, data)
                return "stale_published"

            if record.get("status") == "failed":
                data["pending_instagram_upload"] = None
                _write(f, data)
                return "stale_failed"

            last_attempt_at = record.get("last_attempt_at")

            if record.get("status") == "uploading":
                if not _has_elapsed(last_attempt_at, lease_seconds, now_dt):
                    return "in_flight"
                # Lease expired: treat as an abandoned claim and fall through to the same
                # cooldown/attempt-budget checks as any other reclaim.

            if not _has_elapsed(last_attempt_at, cooldown_seconds, now_dt):
                return "cooldown"

            attempt_count = record.get("attempt_count", 0)
            if attempt_count >= max_attempts:
                data["pending_instagram_upload"] = None
                _write(f, data)
                return "exhausted"

            record["status"] = "uploading"
            record["attempt_count"] = attempt_count + 1
            record["last_attempt_at"] = now_dt.isoformat()
            # container_id is deliberately PRESERVED here. A reclaimed abandoned attempt may
            # have left one behind, and that container is the only evidence of whether the
            # abandoned attempt got as far as publishing. Clearing it — which this function
            # used to do — is what allowed a crash between publish and mark_published to
            # produce a duplicate Reel. The caller reconciles it before acting; see the
            # module docstring.
            _write(f, data)
            return "claimed"
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def set_container_id(idempotency_key: str, container_id: str) -> None:
    """Record the media container created for the CURRENT attempt.

    Instagram-specific, with no facebook_state.py counterpart: the container is a server-side
    handle that outlives the attempt that created it, both on Meta's side and here. It is
    persisted before the container is ever published so that an interrupted publish can be
    reconciled against it rather than blindly repeated (FR-011) — see the module docstring.

    Clears publish_attempted_at at the same time, in the same transaction. The two fields
    are one fact — "this is the container in play, and whether anything has been published
    from it" — and a stale marker carried onto a fresh container would quarantine a job
    whose new container demonstrably never reached a publish call.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    def _update(record, data):
        record["container_id"] = container_id
        record["publish_attempted_at"] = None
    _update_pending(idempotency_key, _update)
    logger.info("set_container_id: key=%s container_id=%s", idempotency_key, container_id)


def mark_publish_attempted(idempotency_key: str) -> None:
    """Record, BEFORE calling it, that publish_container() is about to be attempted.

    The whole point is that it is written first. Afterwards is too late: the failure this
    guards against is the process dying, or the response being lost, during that very call,
    and a marker written after the call returns would be missing in exactly the case it
    exists to describe.

    What it buys is the ability to tell "we never asked Meta to publish this container"
    from "we asked and never heard back". The first is definitively unpublished and needs
    no further thought; the second is an unknown that must be quarantined rather than
    retried blindly (see pending_publish_reconciliations below). Without the distinction
    the only safe policy would be to quarantine every leftover container, which would
    needlessly block re-approval for the ordinary stuck-container case.

    Optional field: a record written before this existed simply has no key, which reads as
    "not attempted" — the same answer it would have given.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    now = datetime.now(timezone.utc).isoformat()

    def _update(record, data):
        record["publish_attempted_at"] = now
    _update_pending(idempotency_key, _update)
    logger.info("mark_publish_attempted: key=%s", idempotency_key)


def release_claim(idempotency_key: str) -> None:
    """Release a claim after a KNOWN, retryable (non-terminal) failure — resets status back to
    'pending' so the next claim_pending_upload() call is gated by the short retry cooldown
    rather than the much longer abandoned-claim lease. attempt_count/last_attempt_at — already
    advanced by the claim — are left as they are.

    Deliberately does NOT clear container_id — see the module docstring. The next attempt
    needs it to establish whether this one published before it failed; discarding it is
    what turned an interrupted publish into a duplicate Reel. Deciding whether that
    container may be reused, must be reconciled, or should be abandoned is the caller's
    job, and it needs the id in hand to do it.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    def _update(record, data):
        record["status"] = "pending"
    _update_pending(idempotency_key, _update)
    logger.info("release_claim: key=%s", idempotency_key)


def mark_published(idempotency_key: str, post_id: str, permalink: str | None = None) -> None:
    """Record the publish (published_idempotency_keys, published_history), then clear
    pending_instagram_upload.

    A published job is terminal: clearing pending here is what stops the cron entrypoint from
    ever calling get_pending_upload() and finding this job again — and, with
    published_idempotency_keys, is what makes a re-approval of the same video a no-op rather
    than a duplicate Reel (FR-011).

    post_id is the Graph API media ID. permalink is the human-usable
    https://www.instagram.com/reel/<shortcode>/ URL, which is a DIFFERENT value fetched
    separately (see instagram_api.get_media_permalink) — the media ID cannot be turned into a
    working URL by string formatting. It is optional because the permalink lookup can fail on a
    Reel that genuinely published; the publish is still recorded, with permalink left None.
    """
    now = datetime.now(timezone.utc).isoformat()

    def _update(record, data):
        keys = data.setdefault("published_idempotency_keys", [])
        if idempotency_key not in keys:
            keys.append(idempotency_key)
        history = data.setdefault("published_history", [])
        history.append({
            "project_name": record.get("project_name"),
            "idempotency_key": idempotency_key,
            "ig_post_id": post_id,
            "ig_permalink": permalink,
            "published_at": now,
        })
        del history[:-_PUBLISH_HISTORY_LIMIT]
        data["pending_instagram_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.info("mark_published: key=%s post_id=%s", idempotency_key, post_id)


def record_recovered_publish(
    idempotency_key: str, project_name: str, container_id: str
) -> None:
    """Record a publish that Instagram reports as done but FieldKit never observed.

    The recovery counterpart to mark_published(), for the window FR-011 has to survive:
    publish_container() succeeded on Meta's side, and the process died (or lost the
    response) before mark_published() could record it. A later run discovers the truth by
    reading the container's status_code, and this is how it writes that down.

    Differs from mark_published() in two ways that both matter:

      - It does NOT require a pending record to still exist, and clears one only if it
        happens to match. The interrupted attempt may well have been the job's last, in
        which case claim_pending_upload() has already cleared the record as "exhausted" —
        and that must not stop the publish from being recorded, or a re-approval of the
        same video would post a SECOND Reel.
      - The history entry carries ig_post_id=None and the container id instead. The media
        ID is genuinely unknowable after the fact: the Graph API offers no container →
        media lookup, and guessing from the account's recent media could just as easily
        latch onto something a human posted. recovered=True marks the entry so nothing
        downstream mistakes a null post id for a bug.

    What it MUST do, and does, is put idempotency_key into published_idempotency_keys.
    That list is what set_pending_upload() and claim_pending_upload() consult, so this one
    write is what makes the duplicate impossible from here on.
    """
    now = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            keys = data.setdefault("published_idempotency_keys", [])
            if idempotency_key not in keys:
                keys.append(idempotency_key)
            history = data.setdefault("published_history", [])
            history.append({
                "project_name": project_name,
                "idempotency_key": idempotency_key,
                "ig_post_id": None,
                "ig_permalink": None,
                "ig_container_id": container_id,
                "recovered": True,
                "published_at": now,
            })
            del history[:-_PUBLISH_HISTORY_LIMIT]
            record = data.get("pending_instagram_upload")
            if record is not None and record.get("idempotency_key") == idempotency_key:
                data["pending_instagram_upload"] = None
            _write(f, data)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    logger.warning(
        "record_recovered_publish: key=%s container_id=%s — Instagram reports this "
        "container as already published; recorded without republishing",
        idempotency_key, container_id,
    )


def mark_failed(idempotency_key: str) -> None:
    """Clear pending_instagram_upload — every call site treats a failure as terminal (no further
    retries follow it), so clearing here stops the cron entrypoint from reprocessing this job
    forever with no backoff. The record (including any container_id) is discarded, not persisted
    with status='failed' — matching facebook_state.mark_failed().
    """
    def _update(record, data):
        data["pending_instagram_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.error("mark_failed: key=%s", idempotency_key)


def find_published(project_name: str) -> dict | None:
    """Return the most recent published_history entry for project_name ({project_name,
    idempotency_key, ig_post_id, published_at}), or None if that project has never been
    published — including if it WAS published but has since aged out of the retained history
    (only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept; see mark_published()).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                history = data.get("published_history", [])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return None
    for entry in reversed(history):
        if entry.get("project_name") == project_name:
            return entry
    return None


def has_outstanding_job(idempotency_key: str) -> bool:
    """Return True if a job for idempotency_key is still awaiting resolution.

    Mirrors facebook_state.has_outstanding_job() exactly — see its docstring. "Outstanding"
    means a pending record with this key is still in the file; published, terminally
    failed, and never-enqueued all read as False.

    Used by tools/upload_cleanup.py to decide whether the shared approved video file on
    disk may be deleted yet. Go through that module rather than calling this directly.
    """
    record = get_pending_upload()
    return record is not None and record.get("idempotency_key") == idempotency_key


def is_published(idempotency_key: str) -> bool:
    """Return True if idempotency_key is in published_idempotency_keys."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                return idempotency_key in data.get("published_idempotency_keys", [])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Pending Drive share-link cleanups
# ---------------------------------------------------------------------------
#
# Publishing a Reel requires briefly making the approved video publicly readable on
# Drive (Instagram fetches it by URL; see tools/drive.py). Revoking that link is a
# SEPARATE concern from the publish itself: the Reel can be genuinely live while the
# revoke call fails on a transient Drive error.
#
# Treating that as an acceptable success would leave a client's video publicly
# reachable forever with nothing recording the fact — the failure mode the privacy
# gate exists to prevent. So a failed revoke is written down here instead, durably,
# and retried on every subsequent cron tick until it succeeds. This list is keyed by
# Drive file id and is deliberately independent of the upload job's lifecycle: the
# job is terminal, the cleanup is not — and it is deliberately not gated on Instagram
# still being configured for the client, since a link that is already public stays public
# whether or not anyone intends to publish another Reel.


def record_share_intent(file_id: str, project_name: str) -> None:
    """Register a cleanup obligation for a Drive file BEFORE it is made public.

    Called from drive.create_temporary_share_link()'s on_file_id hook, at the one moment
    when the file exists but no public permission has been granted yet. It closes an
    otherwise unrecoverable window: if the permission POST succeeds server-side and its
    response is lost to a timeout or a crash, the permission is real, the call raises, and
    the caller holds no file id — an untracked public link with nothing left that could
    ever revoke it. Registering the obligation first means the id is already written down
    no matter how that call turns out.

    Deliberately SILENT, unlike record_share_cleanup(): nothing has gone wrong yet. This is
    an intent, not a failure, so it does not alert, and it starts at attempts=0 with no
    last_alerted_at — the admin hears about it only if a later revoke actually fails. The
    normal path clears it moments later via clear_share_cleanup().

    Idempotent: re-registering a file id that is already recorded leaves the existing
    entry, and its alert history, untouched.
    """
    now = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            cleanups = data.setdefault("pending_share_cleanups", [])
            if any(entry.get("file_id") == file_id for entry in cleanups):
                return
            cleanups.append({
                "file_id": file_id,
                "project_name": project_name,
                "recorded_at": now,
                "last_attempt_at": None,
                "last_alerted_at": None,
                "attempts": 0,
            })
            _write(f, data)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    logger.info(
        "record_share_intent: file_id=%s project=%s — cleanup obligation registered "
        "before the file was shared",
        file_id, project_name,
    )


def record_share_cleanup(file_id: str, project_name: str) -> dict | None:
    """Record that file_id's public Drive permission still needs revoking.

    Returns the entry dict when the admin SHOULD BE ALERTED right now, or None when they
    should not. That decision is made here rather than by the caller because it needs the
    entry's alert history, and deciding-and-stamping has to happen inside the same
    exclusive-lock transaction that bumps the attempt count — otherwise two overlapping
    ticks could both decide to alert about the same link.

    The admin is alerted on the FIRST failure, and then again every
    _SHARE_CLEANUP_ALERT_INTERVAL_SECONDS for as long as the link stays unrevoked. A single
    alert at creation would be worse than useless: a link that keeps failing to revoke stays
    publicly reachable indefinitely, and after one message nothing would ever mention it
    again. The returned entry carries `attempts` and `recorded_at` so the caller can say how
    long this has been going on.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            cleanups = data.setdefault("pending_share_cleanups", [])
            for entry in cleanups:
                if entry.get("file_id") != file_id:
                    continue
                entry["attempts"] = entry.get("attempts", 1) + 1
                entry["last_attempt_at"] = now
                should_alert = _has_elapsed(
                    entry.get("last_alerted_at"),
                    _SHARE_CLEANUP_ALERT_INTERVAL_SECONDS,
                    now_dt,
                )
                if should_alert:
                    entry["last_alerted_at"] = now
                _write(f, data)
                logger.warning(
                    "record_share_cleanup: file_id=%s still pending after %d attempts "
                    "(re-alerting=%s)",
                    file_id, entry["attempts"], should_alert,
                )
                return dict(entry) if should_alert else None

            entry = {
                "file_id": file_id,
                "project_name": project_name,
                "recorded_at": now,
                "last_attempt_at": now,
                "last_alerted_at": now,
                "attempts": 1,
            }
            cleanups.append(entry)
            _write(f, data)
            logger.error(
                "record_share_cleanup: file_id=%s project=%s — share link NOT revoked",
                file_id, project_name,
            )
            return dict(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def list_share_cleanups() -> list[dict]:
    """Return the Drive share links still awaiting revocation (oldest first)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return list(_read(f).get("pending_share_cleanups", []))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return []


def clear_share_cleanup(file_id: str) -> bool:
    """Drop file_id from the pending-cleanup list once its permission is really gone.

    Returns True if an entry was removed, False if there was nothing recorded for it.
    """
    def _remove(data):
        cleanups = data.get("pending_share_cleanups", [])
        remaining = [e for e in cleanups if e.get("file_id") != file_id]
        if len(remaining) == len(cleanups):
            return False
        data["pending_share_cleanups"] = remaining
        return True

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            removed = _remove(data)
            if removed:
                _write(f, data)
                logger.info("clear_share_cleanup: file_id=%s revoked", file_id)
            return removed
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Unresolved publishes awaiting reconciliation with Instagram
# ---------------------------------------------------------------------------
#
# publish_container() is an irreversible side effect on a real client account;
# mark_published() is the durable record of it. A crash, a kill, or a lost HTTP
# response between the two leaves Meta holding a live Reel FieldKit has no record
# of. Keeping container_id alive across ATTEMPTS closes most of that gap — the
# next attempt asks Instagram what became of the container before publishing
# anything — but it does not close it across TERMINAL FAILURE.
#
# The surviving sequence: the publish lands at Meta, its response is lost, and the
# container then cannot be reconciled for the whole retry budget (a Graph outage, a
# network partition, a status code this code does not recognise). mark_failed()
# discards the record, container_id goes with it, and nothing is left that could
# ever check again. A later re-approval is then accepted — the key never reached
# published_idempotency_keys, because the publish was never observed — and a second
# container is created and published. Duplicate Reel, irreversible, on a client's
# real account. An advisory Telegram warning is not a control: it asks a human to
# remember something at the exact moment the system has told them it failed.
#
# So an unknown fate is quarantined DURABLY here instead, and stays quarantined
# until Meta is definitive. This list is keyed by container id, carries the
# idempotency key it is holding, and — exactly like pending_share_cleanups above —
# is independent of the upload job's lifecycle: the job is terminal, the obligation
# is not. That shape is reused deliberately rather than invented again; the two are
# the same category of thing (an unresolved external obligation that must outlive
# the work that created it, be retried on every later tick, and clear only on
# genuine resolution), and a third mechanism would be a third thing to get right.
#
# Note what this does NOT require: mark_failed() still discards the whole record,
# mirroring facebook_state.mark_failed() exactly as before. Storing the obligation
# OUTSIDE the job record is what lets the two state modules stay aligned while
# Instagram still satisfies FR-011. See upload_instagram.py's module docstring for
# the Facebook side, which has the same latent exposure and cannot be fixed this
# way — a Page video upload exposes no handle that could be reconciled after the
# fact.


def _unresolved_publish_entry(data: dict, idempotency_key: str) -> dict | None:
    """Return the unresolved-publish entry holding idempotency_key, or None.

    Takes already-read state rather than reading it, so callers that are mid-transaction
    under the exclusive lock can use it without re-entering the lock.
    """
    for entry in data.get("pending_publish_reconciliations", []):
        if entry.get("idempotency_key") == idempotency_key:
            return entry
    return None


def has_unresolved_publish(idempotency_key: str) -> bool:
    """True if a publish for idempotency_key is still awaiting a definitive answer.

    Read by check_approval.py before enqueueing, so a re-approval of a video whose fate is
    unknown is refused and explained rather than silently turned into a second Reel.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return _unresolved_publish_entry(_read(f), idempotency_key) is not None
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False


def record_publish_reconciliation(
    container_id: str, *, project_name: str, idempotency_key: str
) -> dict | None:
    """Record that container_id's publish outcome is unknown and must keep being checked.

    Returns the entry dict when the admin SHOULD BE ALERTED right now, or None when they
    should not — the same contract, and for the same reason, as record_share_cleanup():
    the decision needs the entry's alert history, and deciding-and-stamping has to happen
    inside the same exclusive-lock transaction that bumps the attempt count, or two
    overlapping ticks could both decide to alert about the same container.

    The admin is alerted on the FIRST record and then every
    _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS for as long as it stays unresolved, because
    this is a state a human may eventually have to resolve by looking at the account.

    Idempotent per container: re-recording bumps attempts rather than duplicating.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            pending = data.setdefault("pending_publish_reconciliations", [])
            for entry in pending:
                if entry.get("container_id") != container_id:
                    continue
                entry["attempts"] = entry.get("attempts", 1) + 1
                entry["last_attempt_at"] = now
                should_alert = _has_elapsed(
                    entry.get("last_alerted_at"),
                    _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS,
                    now_dt,
                )
                if should_alert:
                    entry["last_alerted_at"] = now
                _write(f, data)
                logger.warning(
                    "record_publish_reconciliation: container_id=%s still unresolved after "
                    "%d checks (re-alerting=%s)",
                    container_id, entry["attempts"], should_alert,
                )
                return dict(entry) if should_alert else None

            entry = {
                "container_id": container_id,
                "project_name": project_name,
                "idempotency_key": idempotency_key,
                "recorded_at": now,
                "last_attempt_at": now,
                "last_alerted_at": now,
                "attempts": 1,
            }
            pending.append(entry)
            _write(f, data)
            logger.error(
                "record_publish_reconciliation: container_id=%s project=%s key=%s — publish "
                "outcome UNKNOWN; the Reel may be live. Re-approval of this key is blocked "
                "until Instagram is definitive.",
                container_id, project_name, idempotency_key,
            )
            return dict(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def list_publish_reconciliations() -> list[dict]:
    """Return the containers whose publish outcome is still unknown (oldest first)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return list(_read(f).get("pending_publish_reconciliations", []))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return []


def clear_publish_reconciliation(container_id: str) -> bool:
    """Drop container_id from the unresolved list once Instagram has been definitive.

    Call this ONLY on a definitive answer — PUBLISHED (record it via
    record_recovered_publish() first), or FINISHED/ERROR/EXPIRED, all three of which mean
    the container was never published. Clearing on anything less would release the
    idempotency key while the Reel's fate is still unknown, which is the whole thing this
    list exists to prevent.

    Returns True if an entry was removed, False if there was nothing recorded for it.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            pending = data.get("pending_publish_reconciliations", [])
            remaining = [e for e in pending if e.get("container_id") != container_id]
            if len(remaining) == len(pending):
                return False
            data["pending_publish_reconciliations"] = remaining
            _write(f, data)
            logger.info("clear_publish_reconciliation: container_id=%s resolved", container_id)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
