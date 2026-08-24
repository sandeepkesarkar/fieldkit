"""
State manager for the Facebook video upload pipeline.

Manages:
  $FIELDKIT_DATA_DIR/photo-agent/facebook_state.json
    — pending VideoUploadJob record, published idempotency keys, and a capped
      history of publish outcomes (published_history)

pending_facebook_upload is always cleared (set back to null) once a job
resolves — via mark_published() or mark_failed(), both terminal. A cron
entrypoint must never reprocess a job it finds in this file; the resolved
state lives in published_idempotency_keys / published_history instead.

claim_pending_upload() is the only concurrency-safe way to start (or resume)
uploading the pending job: it collapses what would otherwise be a read, a
staleness check, and a status/attempt-count transition into ONE exclusive-lock
read-modify-write, so two overlapping invocations of the cron script (e.g. a
slow upload still running when the next minute's tick starts) can never both
observe an unclaimed job and both call the Facebook API. Every mutator that
takes an idempotency_key (mark_published, mark_failed, release_claim,
clear_pending_upload) only acts if the CURRENT pending record still has that
key — compare-and-update, not blind overwrite — so a caller reasoning from an
earlier snapshot can never corrupt or destroy a different, newer job enqueued
in its place.

All read-modify-write operations acquire an exclusive file lock (fcntl.LOCK_EX)
before reading and release it after writing, mirroring the pattern in state.py.

FB_APP_SECRET is never stored here. Sensitive token values are never logged.
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
STATE_FILE = DATA_DIR / "facebook_state.json"

__all__ = [
    "get_pending_upload",
    "set_pending_upload",
    "claim_pending_upload",
    "release_claim",
    "clear_pending_upload",
    "mark_published",
    "mark_failed",
    "is_published",
    "find_published",
]

_REQUIRED_UPLOAD_KEYS = frozenset({
    "project_name",
    "video_local_path",
    "page_id",
    "status",
    "attempt_count",
    "last_attempt_at",
    "triggered_at",
    "idempotency_key",
    "fb_post_id",
})

_DEFAULTS = {
    "pending_facebook_upload": None,
    "published_idempotency_keys": [],
    "published_history": [],
}

# Cap on published_history so facebook_state.json doesn't grow without bound over a client's
# lifetime. Only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept.
_PUBLISH_HISTORY_LIMIT = 100


def _read(file_obj) -> dict:
    """Read and parse facebook_state.json from an open, locked file object."""
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        return dict(_DEFAULTS)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("facebook_state.json is corrupt: %s", exc)
        raise RuntimeError("facebook_state.json is corrupt — delete or restore it manually") from exc


def _write(file_obj, data: dict) -> None:
    """Overwrite facebook_state.json via an open, locked file object."""
    content = json.dumps(data, indent=2)
    file_obj.seek(0)
    file_obj.write(content)
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _open_for_write():
    """Open facebook_state.json for read+write, creating it if absent."""
    fd_no = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd_no, "r+")


def get_pending_upload() -> dict | None:
    """Return the pending VideoUploadJob record, or None if absent or null."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                return data.get("pending_facebook_upload")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return None


def set_pending_upload(record: dict) -> None:
    """Write the pending VideoUploadJob. Raises ValueError on missing keys or duplicate idempotency_key."""
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
            data["pending_facebook_upload"] = record
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
            record = data.get("pending_facebook_upload")
            if record is None or record.get("idempotency_key") != idempotency_key:
                return False
            updater(record, data)
            _write(f, data)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def clear_pending_upload(expected_idempotency_key: str) -> bool:
    """Clear pending_facebook_upload back to null — but only if the CURRENT pending record's
    idempotency_key still matches expected_idempotency_key (compare-and-clear). Leaves
    published_idempotency_keys / published_history intact either way.

    Returns True if cleared, False if left untouched: the job the caller expected to clear had
    already been cleared/resolved, or a newer job (a different idempotency_key) has since taken
    the pending slot's place and must not be silently destroyed.
    """
    def _update(record, data):
        data["pending_facebook_upload"] = None
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
    read-modify-write. This is the ONLY concurrency-safe way to start an upload attempt: reading
    the pending record, checking it's not already resolved or claimed, and transitioning it to
    'uploading' each happen under the SAME lock acquisition rather than three separate ones with
    a caller-visible gap between them — so two overlapping invocations of the cron script can
    never both observe an unclaimed job and both call the Facebook API.

    idempotency_key must be the key of the record the caller most recently observed pending. If
    the CURRENT pending record no longer has that key — cleared, resolved, or replaced by a
    newer enqueue — this declines rather than acting on stale information.

    lease_seconds bounds how long a claim (status=='uploading') is treated as still genuinely
    in-progress. Past that, it's treated as abandoned — the claiming process crashed or was
    killed without calling release_claim() or mark_published()/mark_failed() — and becomes
    reclaimable again, subject to the same cooldown/attempt-budget checks as any other retry.
    This is a deliberate lease-pattern tradeoff, not a perfect guarantee: pick lease_seconds
    comfortably longer than any realistic upload duration, or a legitimately slow upload still
    running past it could be reclaimed and re-attempted by a second invocation while the first
    is still genuinely in flight — the same duplicate-post risk this function otherwise closes.
    See upload_facebook.py's _UPLOAD_LEASE_SECONDS for the chosen value and rationale. A KNOWN
    (caught) failure should call release_claim() immediately rather than wait out the lease —
    that's what keeps ordinary retries on the short cooldown instead of the long lease.

    Returns one of:
      "mismatch"        — no pending record, or its idempotency_key has changed since the
                           caller's last read. Nothing to do.
      "in_flight"        — status is 'uploading' and the lease hasn't expired: genuinely claimed
                           by another still-running invocation. Do not retry.
      "cooldown"         — last_attempt_at is too recent (within cooldown_seconds). Nothing to do.
      "stale_published"  — idempotency_key is already in published_idempotency_keys; cleared.
      "stale_failed"     — status was already 'failed'; cleared.
      "exhausted"        — attempt_count already at max_attempts; cleared (terminal failure).
      "claimed"          — success: status is now 'uploading', attempt_count/last_attempt_at
                           already advanced for this attempt. The caller should now attempt the
                           upload using the OTHER (immutable) fields from its own snapshot —
                           project_name/video_local_path/page_id/idempotency_key never change
                           across a record's lifetime.
    """
    now_dt = datetime.now(timezone.utc)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            record = data.get("pending_facebook_upload")
            if record is None or record.get("idempotency_key") != idempotency_key:
                return "mismatch"

            if idempotency_key in data.get("published_idempotency_keys", []):
                data["pending_facebook_upload"] = None
                _write(f, data)
                return "stale_published"

            if record.get("status") == "failed":
                data["pending_facebook_upload"] = None
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
                data["pending_facebook_upload"] = None
                _write(f, data)
                return "exhausted"

            record["status"] = "uploading"
            record["attempt_count"] = attempt_count + 1
            record["last_attempt_at"] = now_dt.isoformat()
            _write(f, data)
            return "claimed"
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def release_claim(idempotency_key: str) -> None:
    """Release a claim after a KNOWN, retryable (non-terminal) failure — resets status back to
    'pending' so the next claim_pending_upload() call is gated by the short retry cooldown
    rather than the much longer abandoned-claim lease. Call this whenever an upload attempt
    fails in a way the caller catches and intends to retry (e.g. FacebookUploadError below
    _MAX_ATTEMPTS). attempt_count/last_attempt_at — already advanced by the claim — are left as
    they are; that's what makes the next claim's cooldown/attempt-budget checks correct.

    Compare-and-update (via _update_pending): a no-op if the current pending record's
    idempotency_key no longer matches.
    """
    def _update(record, data):
        record["status"] = "pending"
    _update_pending(idempotency_key, _update)
    logger.info("release_claim: key=%s", idempotency_key)


def mark_published(idempotency_key: str, post_id: str) -> None:
    """Record the publish (published_idempotency_keys, published_history), then clear
    pending_facebook_upload.

    A published job is terminal: clearing pending here is what stops the cron
    entrypoint from ever calling get_pending_upload() and finding this job again.
    published_history preserves project_name/fb_post_id for callers (e.g. the
    e2e test rig's find_published()) that need to observe the outcome of a
    specific publish after the pending record is gone — a capped list rather
    than a single last-one slot, so an unrelated publish landing in between
    can't hide an earlier one a caller is still polling for.
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
            "fb_post_id": post_id,
            "published_at": now,
        })
        del history[:-_PUBLISH_HISTORY_LIMIT]
        data["pending_facebook_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.info("mark_published: key=%s post_id=%s", idempotency_key, post_id)


def mark_failed(idempotency_key: str) -> None:
    """Clear pending_facebook_upload — every call site treats a failure as terminal (no further
    retries follow it), so clearing here stops the cron entrypoint from reprocessing this job
    forever with no backoff. Nothing needs the failed record's fields afterward, so status is
    not written anywhere (the record itself is discarded, not persisted with status='failed').
    """
    def _update(record, data):
        data["pending_facebook_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.error("mark_failed: key=%s", idempotency_key)


def find_published(project_name: str) -> dict | None:
    """Return the most recent published_history entry for project_name ({project_name,
    idempotency_key, fb_post_id, published_at}), or None if that project has never been
    published — including if it WAS published but has since aged out of the retained history
    (only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept; see mark_published()). A
    caller polling for a specific publish (e.g. the e2e test rig's Stage 5) should only rely on
    this within a reasonably short window of that publish happening — practically never a
    concern for a manually-run e2e test, but worth knowing if this is ever reused elsewhere.
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
