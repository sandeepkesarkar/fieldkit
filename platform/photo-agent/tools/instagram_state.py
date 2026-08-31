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
attempt has an intermediate server-side handle worth persisting for
debugging/observability. It is scoped strictly to ONE attempt — set_container_id()
records it, and release_claim() clears it, because every retry creates a fresh
container and must never republish a previous attempt's. mark_published() and
mark_failed() clear the whole record, so they discard it implicitly.

FB_PAGE_ACCESS_TOKEN is never stored here. Sensitive token values are never logged.
"""

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
}

# Cap on published_history so instagram_state.json doesn't grow without bound over a client's
# lifetime. Only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept.
_PUBLISH_HISTORY_LIMIT = 100


def _read(file_obj) -> dict:
    """Read and parse instagram_state.json from an open, locked file object."""
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        return dict(_DEFAULTS)
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
            # A reclaimed abandoned attempt may have left a container_id behind. Each attempt
            # creates a fresh container, so start every claim with a clean slate.
            record["container_id"] = None
            _write(f, data)
            return "claimed"
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def set_container_id(idempotency_key: str, container_id: str) -> None:
    """Record the media container created for the CURRENT attempt.

    Instagram-specific, with no facebook_state.py counterpart: the container is a server-side
    handle for an in-flight attempt, persisted for operational visibility while the poll runs.
    It is deliberately never used to resume a previous attempt — release_claim() clears it, and
    claim_pending_upload() resets it, so a retry always creates a fresh container.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    def _update(record, data):
        record["container_id"] = container_id
    _update_pending(idempotency_key, _update)
    logger.info("set_container_id: key=%s container_id=%s", idempotency_key, container_id)


def release_claim(idempotency_key: str) -> None:
    """Release a claim after a KNOWN, retryable (non-terminal) failure — resets status back to
    'pending' so the next claim_pending_upload() call is gated by the short retry cooldown
    rather than the much longer abandoned-claim lease. attempt_count/last_attempt_at — already
    advanced by the claim — are left as they are.

    Also clears container_id: the attempt is over, and the next one must create its own
    container rather than risk republishing this attempt's.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    def _update(record, data):
        record["status"] = "pending"
        record["container_id"] = None
    _update_pending(idempotency_key, _update)
    logger.info("release_claim: key=%s", idempotency_key)


def mark_published(idempotency_key: str, post_id: str) -> None:
    """Record the publish (published_idempotency_keys, published_history), then clear
    pending_instagram_upload.

    A published job is terminal: clearing pending here is what stops the cron entrypoint from
    ever calling get_pending_upload() and finding this job again — and, with
    published_idempotency_keys, is what makes a re-approval of the same video a no-op rather
    than a duplicate Reel (FR-011).
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
            "published_at": now,
        })
        del history[:-_PUBLISH_HISTORY_LIMIT]
        data["pending_instagram_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.info("mark_published: key=%s post_id=%s", idempotency_key, post_id)


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
