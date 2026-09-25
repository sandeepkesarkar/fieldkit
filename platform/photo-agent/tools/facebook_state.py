"""
State manager for the Facebook video upload pipeline.

Manages:
  $FIELDKIT_DATA_DIR/photo-agent/facebook_state.json
    — pending VideoUploadJob record, published idempotency keys, a capped
      history of publish outcomes (published_history), and publishes whose
      outcome is unknown (pending_publish_reconciliations)

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
clear_pending_upload, set_upload_session, mark_publish_attempted,
mark_publish_settled) only acts if the CURRENT pending record still has that
key — compare-and-update, not blind overwrite — so a caller reasoning from an
earlier snapshot can never corrupt or destroy a different, newer job enqueued
in its place.

Every write goes through _transaction(), the single place that takes the
exclusive lock and the single place that calls _write() — enforced at RUNTIME
by an authorisation token only _transaction() holds, exactly as in
tools/instagram_state.py. Read that module's _transaction() and
_WriteAuthorisation docstrings for why a chokepoint rather than per-site
discipline; the reasoning carries over unchanged. The same caveat applies too:
the file is rewritten in place, so a transaction is atomic against other
processes but not crash-atomic; _read() fails closed on the torn shapes it can
detect (issue #79).

DUPLICATE-PUBLISH RECONCILIATION (issue #78). facebook_api.upload_video() is a
sessionized upload whose START phase yields a video_id before anything is
published, and whose FINISH phase is the irreversible publish. The job record
carries that handle and a marker:

  video_id / upload_session_id — written by set_upload_session() straight after
      START, before any bytes are transferred;
  publish_attempted_at         — written by mark_publish_attempted() BEFORE
      FINISH is sent. Set means "Meta was asked to publish video_id and FieldKit
      has not yet established what happened".

The pair (video_id, publish_attempted_at) is an unresolved publish. It is closed
only by a definitive Meta answer — mark_published() / record_recovered_publish()
(it went live) or mark_publish_settled() (Meta reports it did not). Any
transaction that drops or replaces a record still carrying an open publish moves
it into pending_publish_reconciliations in the same write
(_preserve_unresolved_obligation()), and set_pending_upload() refuses a key that
is quarantined there. This mirrors instagram_state's
pending_publish_reconciliations, keyed by video_id instead of container_id.

What a quarantine NEVER accepts as an answer: a match against the Page's recent
videos. That cannot tell FieldKit's upload from one a human posted in the same
window, so it may be shown to a person as evidence but cannot release a key.
The answer-asserting verbs (mark_publish_settled, clear_publish_reconciliation)
require the caller to name the video and the observation Meta returned, both
checked here; like Instagram's, that narrows accidental routes but cannot prove
Meta was consulted, which lives in the caller's control flow.

FB_APP_SECRET is never stored here. Sensitive token values are never logged.
"""

import copy
import fcntl
import json
import logging
import os
from contextlib import contextmanager
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
    "has_outstanding_job",
    "set_upload_session",
    "mark_publish_attempted",
    "mark_publish_settled",
    "record_recovered_publish",
    "record_publish_reconciliation",
    "list_publish_reconciliations",
    "clear_publish_reconciliation",
    "has_unresolved_publish",
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

# Fields that record what META did, and are therefore not a caller's to supply — see
# instagram_state._PROVENANCE_KEYS. set_pending_upload() refuses a record carrying either,
# because _preserve_unresolved_obligation() reads publish_settled_at as proof of an answer.
_PROVENANCE_KEYS = frozenset({"publish_attempted_at", "publish_settled_at"})

# The observations (facebook_api.get_video_publish_state()'s `observed` tokens) allowed to
# assert an answer. Duplicated rather than imported so this module stays free of HTTP
# dependencies; tests/test_facebook_state.py asserts the two copies agree.
_OBSERVED_PUBLISHED = "publish_status_published"
_OBSERVED_NOT_PUBLISHED = frozenset({
    "publish_status_error",
    "video_status_error",
    "video_status_upload_failed",
    "video_status_expired",
})
_OBSERVED_DEFINITIVE = _OBSERVED_NOT_PUBLISHED | {_OBSERVED_PUBLISHED}

_DEFAULTS = {
    "pending_facebook_upload": None,
    "published_idempotency_keys": [],
    "published_history": [],
    "pending_publish_reconciliations": [],
}

# Cap on published_history so facebook_state.json doesn't grow without bound over a client's
# lifetime. Only the most recent _PUBLISH_HISTORY_LIMIT publishes are kept.
_PUBLISH_HISTORY_LIMIT = 100

# How long to wait before re-alerting about a publish whose outcome is STILL unknown. Same
# value and reasoning as instagram_state's _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS.
_PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS = 24 * 60 * 60


_KNOWN_TOP_LEVEL_KEYS = frozenset(_DEFAULTS)


class _WriteAuthorisation:
    """The capability to write the state file. Constructed exactly once, below.

    A runtime chokepoint: _write() and _open_for_write() refuse to run without this token,
    and only _transaction() holds it, so no alias or indirection can persist a change that
    skipped _preserve_unresolved_obligation(). See instagram_state._WriteAuthorisation.
    """

    __slots__ = ()


_WRITE_AUTHORISATION = _WriteAuthorisation()


def _read(file_obj) -> dict:
    """Read and parse facebook_state.json from an open, locked file object.

    FAILS CLOSED on anything that is not recognisably this file, matching
    tools/instagram_state.py — see its _read() for the full reasoning. An ABSENT file
    legitimately means a fresh client; a PRESENT ZERO-LENGTH one cannot arise in normal
    operation, because _write() always writes content before it truncates and
    _transaction() initialises a file it creates. What is left is an external
    truncation or a crash between creating the file and initialising it, and reading
    either as "nothing was ever recorded" would silently discard published_idempotency_keys
    and pending_publish_reconciliations — the lists that stop a re-approval from posting
    the same video twice.
    """
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        raise RuntimeError(
            "facebook_state.json is present but empty — an interrupted write, or a file "
            "created and never initialised. Refusing to read it as fresh state. If this "
            "client has never published, delete the file; otherwise restore it."
        )
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("facebook_state.json is corrupt: %s", exc)
        raise RuntimeError("facebook_state.json is corrupt — delete or restore it manually") from exc
    if not isinstance(data, dict) or not (_KNOWN_TOP_LEVEL_KEYS & set(data)):
        raise RuntimeError(
            "facebook_state.json parsed but does not look like state (no recognised "
            "top-level keys) — delete or restore it manually"
        )
    return data


def _write(file_obj, data: dict, authorisation=None) -> None:
    """Overwrite facebook_state.json via an open, locked file object.

    Refuses to write without _transaction()'s authorisation token — writing outside a
    transaction would skip _preserve_unresolved_obligation().
    """
    if authorisation is not _WRITE_AUTHORISATION:
        raise RuntimeError(
            "_write() called outside _transaction(). Every write to facebook_state.json "
            "must go through _transaction(), which is what enforces the unresolved-publish "
            "invariant on the way out."
        )
    content = json.dumps(data, indent=2)
    file_obj.seek(0)
    file_obj.write(content)
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _open_for_write(authorisation=None):
    """Open facebook_state.json for read+write. Returns (file, created_by_us).

    Authorised the same way as _write(). O_EXCL so the caller can tell a file it just
    created (which must be initialised, not read) from one that was already there — see
    instagram_state._open_for_write() for the accepted first-run race.
    """
    if authorisation is not _WRITE_AUTHORISATION:
        raise RuntimeError(
            "_open_for_write() called outside _transaction() — see _WriteAuthorisation."
        )
    try:
        fd_no = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return os.fdopen(os.open(STATE_FILE, os.O_RDWR, 0o644), "r+"), False
    return os.fdopen(fd_no, "r+"), True


class _Transaction:
    """One exclusive-lock read-modify-write over facebook_state.json.

    `data` is the parsed state, free to mutate. Nothing is persisted unless commit() is
    called, so a transaction can inspect state and decline to change it.
    """

    __slots__ = ("data", "_committed")

    def __init__(self, data: dict):
        self.data = data
        self._committed = False

    def commit(self) -> None:
        """Mark this transaction's changes for persisting when the block exits."""
        self._committed = True


@contextmanager
def _transaction():
    """THE single write path for this module. Every mutation goes through here.

    On every committed write, _preserve_unresolved_obligation() compares the pending record
    this transaction started with against the one it leaves behind, so no mutation site —
    including ones added later — can drop an unresolved publish. Mirrors
    instagram_state._transaction(); read its docstring for the history and for exactly what
    "atomic" does and does not mean here.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f, created = _open_for_write(_WRITE_AUTHORISATION)
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            if created:
                # Initialise before yielding, so a transaction that declines to commit never
                # leaves the zero-length file _read() refuses.
                data = copy.deepcopy(_DEFAULTS)
                _write(f, data, _WRITE_AUTHORISATION)
            else:
                data = _read(f)
            incoming = data.get("pending_facebook_upload")
            incoming = copy.deepcopy(incoming) if incoming is not None else None
            txn = _Transaction(data)
            yield txn
            if not txn._committed:
                return
            _preserve_unresolved_obligation(incoming, data)
            _write(f, data, _WRITE_AUTHORISATION)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _has_open_publish(record: dict | None) -> bool:
    """True if record asked Meta to publish a specific video and has no answer yet.

    The obligation is the PAIR — a video_id and a publish attempted against it. A video_id
    with no attempt cannot have gone live (FINISH was never sent), and a marker with no
    video_id names nothing to reconcile.
    """
    if not record:
        return False
    return bool(record.get("video_id")) and bool(record.get("publish_attempted_at"))


def _preserve_unresolved_obligation(incoming: dict | None, data: dict) -> None:
    """Carry a departing record's unresolved publish into the quarantine list.

    Runs on every committed write. `incoming` is the pending record as this transaction
    found it; data["pending_facebook_upload"] is what the transaction leaves there. The
    obligation is still tracked — and nothing needs doing — in exactly three cases:

      - the outgoing record is the same job, same video_id, publish still open;
      - the outgoing record is the same job, same video_id, publish SETTLED
        (publish_settled_at — Meta answered "not published");
      - the key is now in published_idempotency_keys — the publish is a recorded fact,
        so any quarantine for it is retired rather than created.

    Anything else — removed, replaced (including by a fresh record under the SAME key),
    pointed at a different video, or stripped of its marker — quarantines it here. Same
    rule as instagram_state._preserve_unresolved_obligation().
    """
    if not _has_open_publish(incoming):
        return
    key = incoming.get("idempotency_key")
    video_id = incoming.get("video_id")
    outgoing = data.get("pending_facebook_upload")

    if (
        outgoing is not None
        and outgoing.get("idempotency_key") == key
        and outgoing.get("video_id") == video_id
        and (_has_open_publish(outgoing) or outgoing.get("publish_settled_at"))
    ):
        return

    if key in data.get("published_idempotency_keys", []):
        _drop_publish_reconciliations_for_key(data, key)
        return

    _quarantine_unresolved_in_txn(incoming, data, datetime.now(timezone.utc).isoformat())


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
    """Write the pending VideoUploadJob.

    Raises ValueError on missing keys, on a caller-supplied provenance field
    (publish_attempted_at / publish_settled_at — see _PROVENANCE_KEYS), on an
    idempotency_key already published, and on one whose publish is still unresolved in
    pending_publish_reconciliations. The last is the structural guard for issue #78: a
    publish whose response was lost never reached published_idempotency_keys, so without it
    a re-approval would post the same video a second time.
    """
    missing = _REQUIRED_UPLOAD_KEYS - set(record.keys())
    if missing:
        raise ValueError(f"set_pending_upload: missing required keys: {missing}")
    forged = _PROVENANCE_KEYS & set(record)
    if forged:
        raise ValueError(
            f"set_pending_upload: {sorted(forged)} may not be supplied by a caller — "
            "these record what Meta did and are written only by mark_publish_attempted() "
            "and mark_publish_settled()"
        )
    with _transaction() as txn:
        data = txn.data
        key = record["idempotency_key"]
        if key in data.get("published_idempotency_keys", []):
            raise ValueError(
                f"set_pending_upload: idempotency_key {key!r} already in published_idempotency_keys"
            )
        if _unresolved_publish_entry(data, key) is not None:
            raise ValueError(
                f"set_pending_upload: idempotency_key {key!r} has an unresolved publish "
                "awaiting reconciliation with Facebook"
            )
        data["pending_facebook_upload"] = record
        txn.commit()
        logger.info("set_pending_upload: project=%s key=%s", record.get("project_name"), key)


def _update_pending(idempotency_key: str, updater) -> bool:
    """Read; if the CURRENT pending record's idempotency_key still matches idempotency_key,
    apply updater(record, data) and write — all under one exclusive lock. Returns True if the
    updater ran, False if there was nothing matching to update (no pending record, already
    resolved/cleared, or replaced by a newer job under a different key).

    This compare-before-update is what makes mark_published/mark_failed/clear_pending_upload
    safe to call from a caller holding an earlier snapshot: they can never mutate or destroy a
    DIFFERENT job that's since taken the pending slot's place.
    """
    with _transaction() as txn:
        data = txn.data
        record = data.get("pending_facebook_upload")
        if record is None or record.get("idempotency_key") != idempotency_key:
            return False
        updater(record, data)
        txn.commit()
        return True


def clear_pending_upload(expected_idempotency_key: str) -> bool:
    """Clear pending_facebook_upload back to null — but only if the CURRENT pending record's
    idempotency_key still matches expected_idempotency_key (compare-and-clear). Leaves
    published_idempotency_keys / published_history intact either way. An unresolved publish
    on the cleared record is quarantined by _transaction(), not destroyed.

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

    video_id / upload_session_id / publish_attempted_at are deliberately PRESERVED by a claim:
    a reclaimed attempt may have left an unresolved publish, and the caller must reconcile it
    before uploading anything (issue #78).

    Returns one of:
      "mismatch"        — no pending record, or its idempotency_key has changed since the
                           caller's last read. Nothing to do.
      "in_flight"        — status is 'uploading' and the lease hasn't expired: genuinely claimed
                           by another still-running invocation. Do not retry.
      "cooldown"         — last_attempt_at is too recent (within cooldown_seconds). Nothing to do.
      "stale_published"  — idempotency_key is already in published_idempotency_keys; cleared.
      "stale_failed"     — status was already 'failed'; cleared (an open publish on it is
                           quarantined in the same write).
      "exhausted"        — attempt_count already at max_attempts; cleared (terminal failure).
                           An open publish on it is quarantined in the SAME write.
      "claimed"          — success: status is now 'uploading', attempt_count/last_attempt_at
                           already advanced for this attempt.
    """
    now_dt = datetime.now(timezone.utc)
    with _transaction() as txn:
        data = txn.data
        record = data.get("pending_facebook_upload")
        if record is None or record.get("idempotency_key") != idempotency_key:
            return "mismatch"

        if idempotency_key in data.get("published_idempotency_keys", []):
            data["pending_facebook_upload"] = None
            txn.commit()
            return "stale_published"

        if record.get("status") == "failed":
            data["pending_facebook_upload"] = None
            txn.commit()
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
            txn.commit()
            return "exhausted"

        record["status"] = "uploading"
        record["attempt_count"] = attempt_count + 1
        record["last_attempt_at"] = now_dt.isoformat()
        txn.commit()
        return "claimed"


def set_upload_session(idempotency_key: str, video_id: str, upload_session_id: str) -> None:
    """Record the video_id / upload_session_id Meta returned from the START phase.

    Called before any bytes are transferred, and long before FINISH — so a later attempt,
    or the quarantine, always has the handle to reconcile against.

    Clears publish_attempted_at / publish_settled_at in the same write: they describe the
    PREVIOUS video, and a fresh session has not been asked to publish anything. If the
    record still carried an OPEN publish on a different video, _transaction() quarantines
    that one rather than letting this overwrite erase it.

    Raises RuntimeError if no pending record with this key exists — the caller must not go
    on to upload for a job that is no longer there.
    """
    def _update(record, data):
        record["video_id"] = video_id
        record["upload_session_id"] = upload_session_id
        record["publish_attempted_at"] = None
        record["publish_settled_at"] = None
    if not _update_pending(idempotency_key, _update):
        raise RuntimeError(
            f"set_upload_session: no pending record for key {idempotency_key!r}"
        )
    logger.info("set_upload_session: key=%s video_id=%s", idempotency_key, video_id)


def mark_publish_attempted(idempotency_key: str, video_id: str) -> None:
    """Record, BEFORE sending FINISH, that video_id is about to be published.

    Written first because the failure it guards against is the process dying, or the
    response being lost, during FINISH itself. It is what distinguishes "never asked Meta to
    publish" (safe to start over) from "asked and never heard back" (must reconcile).

    Raises RuntimeError if the marker was NOT written — no matching pending record, or the
    record holds a different video. The caller must then not send FINISH: an unmarked
    publish is exactly the unreconcilable case issue #78 describes.
    """
    now = datetime.now(timezone.utc).isoformat()
    mismatched = []

    def _update(record, data):
        if record.get("video_id") != video_id:
            mismatched.append(record.get("video_id"))
            return
        record["publish_attempted_at"] = now
        record["publish_settled_at"] = None
    written = _update_pending(idempotency_key, _update)
    if not written or mismatched:
        raise RuntimeError(
            f"mark_publish_attempted: could not record the attempt for key "
            f"{idempotency_key!r} video {video_id!r}; refusing to let FINISH proceed"
        )
    logger.info("mark_publish_attempted: key=%s video_id=%s", idempotency_key, video_id)


def mark_publish_settled(idempotency_key: str, video_id: str, observed: str) -> None:
    """Record that Meta reports video_id was NOT published, closing the open publish.

    Takes the evidence: observed must be one of the not-published observations
    facebook_api.get_video_publish_state() returns, and video_id must be the video the
    record holds. Stamps publish_settled_at as publish_attempted_at is cleared, so
    _preserve_unresolved_obligation() can tell a settlement from an erasure.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer
    matches.
    """
    if observed not in _OBSERVED_NOT_PUBLISHED:
        raise ValueError(
            f"mark_publish_settled: {observed!r} does not mean the video failed to publish; "
            f"expected one of {sorted(_OBSERVED_NOT_PUBLISHED)}"
        )
    now = datetime.now(timezone.utc).isoformat()
    mismatched = []

    def _update(record, data):
        if record.get("video_id") != video_id:
            mismatched.append(record.get("video_id"))
            return
        record["publish_attempted_at"] = None
        record["publish_settled_at"] = now
    _update_pending(idempotency_key, _update)
    if mismatched:
        raise ValueError(
            f"mark_publish_settled: asked to settle video {video_id!r} but the pending "
            f"record holds {mismatched[0]!r}"
        )
    logger.info(
        "mark_publish_settled: key=%s video_id=%s observed=%s", idempotency_key, video_id, observed
    )


def release_claim(idempotency_key: str) -> None:
    """Release a claim after a KNOWN, retryable (non-terminal) failure — resets status back to
    'pending' so the next claim_pending_upload() call is gated by the short retry cooldown
    rather than the much longer abandoned-claim lease. Call this whenever an upload attempt
    fails in a way the caller catches and intends to retry (e.g. FacebookUploadError below
    _MAX_ATTEMPTS). attempt_count/last_attempt_at — already advanced by the claim — are left as
    they are; that's what makes the next claim's cooldown/attempt-budget checks correct.

    Deliberately keeps video_id and publish_attempted_at: the next attempt needs them to
    establish whether this one published before it failed.

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
    can't hide an earlier one a caller is still polling for. Any quarantine for the
    key is retired in the same write.
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
        _drop_publish_reconciliations_for_key(data, idempotency_key)
        data["pending_facebook_upload"] = None
    _update_pending(idempotency_key, _update)
    logger.info("mark_published: key=%s post_id=%s", idempotency_key, post_id)


def record_recovered_publish(
    idempotency_key: str, project_name: str, video_id: str, observed: str
) -> None:
    """Record a publish Meta reports as live but FieldKit never observed.

    For the issue #78 window: FINISH succeeded at Meta and the response was lost. A later
    reconciliation read video_id's status and got publish_status == "published"; observed
    must be that observation. Unlike mark_published() this does not require a pending
    record to exist (the attempt may have been the job's last, already cleared as
    exhausted), and clears one only if its key matches. The video id IS the post id the
    success path records, so it is stored as fb_post_id, with recovered=True.

    What matters most: the key goes into published_idempotency_keys, which is what makes a
    re-approval of the same video impossible from here on.
    """
    if observed != _OBSERVED_PUBLISHED:
        raise ValueError(
            f"record_recovered_publish: {observed!r} is not the published observation "
            f"{_OBSERVED_PUBLISHED!r}"
        )
    now = datetime.now(timezone.utc).isoformat()
    with _transaction() as txn:
        data = txn.data
        keys = data.setdefault("published_idempotency_keys", [])
        if idempotency_key not in keys:
            keys.append(idempotency_key)
        history = data.setdefault("published_history", [])
        history.append({
            "project_name": project_name,
            "idempotency_key": idempotency_key,
            "fb_post_id": video_id,
            "recovered": True,
            "published_at": now,
        })
        del history[:-_PUBLISH_HISTORY_LIMIT]
        _drop_publish_reconciliations_for_key(data, idempotency_key)
        record = data.get("pending_facebook_upload")
        if record is not None and record.get("idempotency_key") == idempotency_key:
            data["pending_facebook_upload"] = None
        txn.commit()
    logger.warning(
        "record_recovered_publish: key=%s video_id=%s — Facebook reports this video as "
        "already published; recorded without republishing",
        idempotency_key, video_id,
    )


def mark_failed(idempotency_key: str) -> None:
    """Clear pending_facebook_upload — every call site treats a failure as terminal (no further
    retries follow it), so clearing here stops the cron entrypoint from reprocessing this job
    forever with no backoff. The record itself is discarded, not persisted with
    status='failed'.

    Discarding the record does NOT discard an unresolved publish: _transaction() moves it
    into pending_publish_reconciliations in this same write, so the key stays blocked
    (issue #78). Callers that know Meta's answer should settle or record it first.
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


def has_outstanding_job(idempotency_key: str) -> bool:
    """Return True if a job for idempotency_key is still awaiting resolution.

    "Outstanding" means a pending record with this key is still sitting in the file —
    enqueued, mid-retry, or claimed and in flight. Everything else is terminal: a
    published job, a terminally-failed job, and a job that was never enqueued all clear
    (or never create) the pending record, and all read as False here.

    Added for Feature 005's cross-platform cleanup coordination: the approved video file
    on disk is shared by the Facebook and Instagram upload jobs, so neither may delete it
    while the other still has work to do for the same approval. See tools/upload_cleanup.py
    — callers should go through that rather than calling this directly, so the "is that
    platform even enabled for this client" half of the question isn't forgotten.
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
# Unresolved publishes awaiting reconciliation with Facebook (issue #78)
# ---------------------------------------------------------------------------
#
# The same mechanism as instagram_state's pending_publish_reconciliations, keyed by
# video_id. An entry means: FINISH was sent for this video, FieldKit never learned the
# outcome, and the job that sent it is gone. It blocks its idempotency key in
# set_pending_upload(), is re-checked by upload_facebook.py on every tick, and clears only
# on a definitive answer from Meta about that video_id.


def _add_publish_reconciliation(
    data: dict, *, video_id: str, project_name: str, idempotency_key: str, now: str
) -> bool:
    """Insert a quarantine entry into already-read state. Returns True if it was added.

    Idempotent and non-counting (attempts=0, never alerted): a safety net, not a check.
    """
    pending = data.setdefault("pending_publish_reconciliations", [])
    if any(entry.get("video_id") == video_id for entry in pending):
        return False
    pending.append({
        "video_id": video_id,
        "project_name": project_name,
        "idempotency_key": idempotency_key,
        "recorded_at": now,
        "last_attempt_at": None,
        "last_alerted_at": None,
        "attempts": 0,
    })
    return True


def _quarantine_unresolved_in_txn(record: dict, data: dict, now: str) -> bool:
    """Quarantine record's video in-transaction if its publish outcome is unknown.

    Called only from _preserve_unresolved_obligation(), which _transaction() runs on every
    committed write — so the quarantine lands in the same write as the removal that made
    it necessary.
    """
    if not _has_open_publish(record):
        return False
    video_id = record["video_id"]
    added = _add_publish_reconciliation(
        data,
        video_id=video_id,
        project_name=record.get("project_name", "unknown"),
        idempotency_key=record.get("idempotency_key", ""),
        now=now,
    )
    if added:
        logger.error(
            "quarantined an unresolved publish while discarding its job: video_id=%s key=%s "
            "— the video may be live; re-approval of this key is now blocked",
            video_id, record.get("idempotency_key"),
        )
    return added


def _drop_publish_reconciliations_for_key(data: dict, idempotency_key: str) -> int:
    """Remove any quarantine entries for idempotency_key from already-read state."""
    pending = data.get("pending_publish_reconciliations", [])
    remaining = [e for e in pending if e.get("idempotency_key") != idempotency_key]
    removed = len(pending) - len(remaining)
    if removed:
        data["pending_publish_reconciliations"] = remaining
    return removed


def _unresolved_publish_entry(data: dict, idempotency_key: str) -> dict | None:
    """Return the quarantine entry holding idempotency_key in already-read state, or None."""
    for entry in data.get("pending_publish_reconciliations", []):
        if entry.get("idempotency_key") == idempotency_key:
            return entry
    return None


def has_unresolved_publish(idempotency_key: str) -> bool:
    """True if a publish for idempotency_key is still awaiting a definitive answer.

    Read by check_approval.py so a blocked re-approval is explained to the admin;
    set_pending_upload() enforces the block regardless.
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
    video_id: str, *, project_name: str, idempotency_key: str, counts_as_check: bool = True
) -> dict | None:
    """Record that video_id's publish outcome is unknown and must keep being checked.

    Returns the entry when the admin SHOULD BE ALERTED now (first record, then every
    _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS while unresolved), else None — decided and
    stamped in the same transaction that bumps the check count. counts_as_check=False
    records an alert decision without claiming a check happened. Idempotent per video.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _transaction() as txn:
        data = txn.data
        pending = data.setdefault("pending_publish_reconciliations", [])
        for entry in pending:
            if entry.get("video_id") != video_id:
                continue
            if counts_as_check:
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_attempt_at"] = now
            should_alert = _has_elapsed(
                entry.get("last_alerted_at"), _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS, now_dt
            )
            if should_alert:
                entry["last_alerted_at"] = now
            txn.commit()
            logger.warning(
                "record_publish_reconciliation: video_id=%s still unresolved after %d checks "
                "(re-alerting=%s)", video_id, entry.get("attempts", 0), should_alert,
            )
            return dict(entry) if should_alert else None

        entry = {
            "video_id": video_id,
            "project_name": project_name,
            "idempotency_key": idempotency_key,
            "recorded_at": now,
            "last_attempt_at": now if counts_as_check else None,
            "last_alerted_at": now,
            "attempts": 1 if counts_as_check else 0,
        }
        pending.append(entry)
        txn.commit()
        logger.error(
            "record_publish_reconciliation: video_id=%s project=%s key=%s — publish outcome "
            "UNKNOWN; the video may be live. Re-approval of this key is blocked until "
            "Facebook is definitive.", video_id, project_name, idempotency_key,
        )
        return dict(entry)


def list_publish_reconciliations() -> list[dict]:
    """Return the videos whose publish outcome is still unknown (oldest first)."""
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


def clear_publish_reconciliation(video_id: str, observed: str) -> bool:
    """Drop video_id from the unresolved list once Meta has been definitive about it.

    observed must be a definitive observation from get_video_publish_state() — published
    (record it via record_recovered_publish() first) or one of the not-published ones.
    Returns True if an entry was removed.
    """
    if observed not in _OBSERVED_DEFINITIVE:
        raise ValueError(
            f"clear_publish_reconciliation: {observed!r} is not a definitive observation; "
            f"expected one of {sorted(_OBSERVED_DEFINITIVE)}"
        )
    with _transaction() as txn:
        data = txn.data
        pending = data.get("pending_publish_reconciliations", [])
        remaining = [e for e in pending if e.get("video_id") != video_id]
        if len(remaining) == len(pending):
            return False
        data["pending_publish_reconciliations"] = remaining
        txn.commit()
        logger.info(
            "clear_publish_reconciliation: video_id=%s resolved observed=%s", video_id, observed
        )
        return True
