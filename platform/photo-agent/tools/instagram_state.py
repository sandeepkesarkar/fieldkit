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

Every write in this module goes through _transaction(), which is the single
place that takes the lock and the single place that calls _write(). That is not
tidiness: it is where the unresolved-publish obligation described further down is
enforced, after four separate mutation sites lost it across successive reviews.
Read _transaction()'s docstring before adding a mutator — including what "atomic"
does and does not mean for the state file, which is written in place and is
therefore not crash-atomic, only fail-closed on read.

Two boundaries are worth knowing before changing anything here.

TRUST BOUNDARY. set_pending_upload() is the only function that writes a pending
record wholesale, and it is therefore the only place a caller can put arbitrary
fields into state. It refuses publish_attempted_at and publish_settled_at
(_PROVENANCE_KEYS), because those record what META did and the logic downstream
reads them as proof. Before that refusal existed, a caller could supply
publish_settled_at itself and _preserve_unresolved_obligation() would accept it as
an answer, releasing an unresolved publish with no Meta involvement at all.

VERIFICATION BOUNDARY, stated precisely because overstating it has been a
recurring mistake here.

The write chokepoint is enforced at RUNTIME. _write() and _open_for_write() refuse
to run without an authorisation token that only _transaction() holds
(_WriteAuthorisation). That is what makes the guarantee independent of how a call
is spelled: an alias, a globals() lookup, a method on a class, or anything else
still has to invoke the real function object and hand it the token.

There are ALSO source-parsing tests — that _write() is called from one place, that
the settlement stamp is written by three named transitions, that this module
defines no unexpected class. Those are a tripwire, not the guarantee. They match
calls by literal syntactic NAME and are therefore blind to aliasing: `_w = _write`
followed by a call to `_w` is invisible to them, which a reviewer demonstrated.
They are kept because they fail early and name the offending scope, but nothing
rests on them alone.

Outside this file nothing can be enforced structurally. The verbs that assert Meta
answered — mark_publish_settled(), clear_publish_reconciliation() — instead require
the CALLER to name the container and the observed Graph status, both of which are
checked here, so an aliased or accidentally-refactored call cannot assert an answer
by arriving with too few arguments. That narrows the accidental routes; it does not
prove Meta was consulted, and nothing in this module can, because that fact lives
in the caller's control flow.

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
from contextlib import contextmanager
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
    "mark_publish_settled",
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

# Fields that assert something about what META did, and are therefore NOT the caller's to
# supply. Only this module writes them, and only from a transition that has actually
# established the fact: mark_publish_attempted() before the irreversible call,
# mark_publish_settled() after a definitive status. set_pending_upload() REFUSES a record
# carrying either — see its docstring. Without that refusal the whole
# settled-versus-erased distinction collapses, because a caller could simply write the
# answer itself and _preserve_unresolved_obligation() would believe it.
_PROVENANCE_KEYS = frozenset({"publish_attempted_at", "publish_settled_at"})

# The Graph API container statuses that mean "this container did NOT publish". Settling an
# open publish requires naming one of them, so the claim is carried BY THE CALL rather than
# implied by where it was made — an alias or a refactored caller cannot settle by accident,
# and PUBLISHED can never be mistaken for a settlement.
_NON_PUBLISHED_STATUSES = frozenset({"FINISHED", "ERROR", "EXPIRED"})
# Every status that resolves a quarantine, in either direction.
_DEFINITIVE_STATUSES = _NON_PUBLISHED_STATUSES | {"PUBLISHED"}

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


# Every top-level key this module writes. A state file that parses but carries none of
# them is not a state file — see _read().
_KNOWN_TOP_LEVEL_KEYS = frozenset(_DEFAULTS)


class _WriteAuthorisation:
    """The capability to write the state file. Constructed exactly once, below.

    A RUNTIME chokepoint, replacing a purely syntactic one. The guard tests used to scan
    this module's source for calls to _write() by name, which meant they saw a call only
    when it was spelled that way: `_aliased_write = _write` followed by a call to the
    alias, or `globals()["_write"](...)`, was invisible to all of them, and a reviewer
    demonstrated both wiping a marker-bearing record with the whole suite green.

    That was not an incomplete scan, it was the wrong mechanism — no syntactic check can
    see what OBJECT flows to a call. Every indirection, however it is named, still has to
    invoke the real function at runtime and hand it this token. _transaction() is the only
    thing that holds one, so a bypass no longer depends on how it is spelled.
    """

    __slots__ = ()


_WRITE_AUTHORISATION = _WriteAuthorisation()


def _read(file_obj) -> dict:
    """Read and parse instagram_state.json from an open, locked file object.

    FAILS CLOSED on anything that is not recognisably this file. That property is
    load-bearing rather than defensive: pending_publish_reconciliations is what stops a
    video whose Reel may already be live from being re-approved (FR-011), so a read that
    quietly produced an EMPTY state would present an empty quarantine and let the
    duplicate through. Three shapes are refused:

      - present but ZERO LENGTH. An absent file legitimately means a fresh client, but a
        present empty one cannot arise in normal operation: _write() always writes content
        before it truncates, and _transaction() initialises a file it creates before
        yielding, precisely so that nothing leaves a zero-length file behind. (That was not
        true until this was written — _open_for_write()'s O_CREAT left one whenever a
        transaction declined to commit, which is exactly why "empty means fresh" was unsafe
        to assume.) What remains is an external truncation, or a crash in the moment
        between creating the file and initialising it. Both are better refused than read as
        "nothing was ever recorded".
      - not a JSON object.
      - a JSON object carrying none of this module's top-level keys, which rejects
        degenerate survivors like `{}` while still accepting a state file written before a
        newer key existed — absent individual keys fall back to defaults as they always
        have, so this does not break forward migration.

    NOT detectable here, and deliberately not claimed: a partial write that happens to
    parse AND carries a recognised key. _write() emits the whole document in one call, so
    a torn write yields malformed JSON in practice rather than a plausible one — but that
    is a property of the write, not something this function can verify. See issue #79.
    """
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        raise RuntimeError(
            "instagram_state.json is present but empty — an interrupted write, or a file "
            "created and never initialised. Refusing to read it as fresh state, which "
            "would present an empty publish quarantine. If this client has never "
            "published, delete the file; otherwise restore it."
        )
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("instagram_state.json is corrupt: %s", exc)
        raise RuntimeError("instagram_state.json is corrupt — delete or restore it manually") from exc
    if not isinstance(data, dict) or not (_KNOWN_TOP_LEVEL_KEYS & set(data)):
        raise RuntimeError(
            "instagram_state.json parsed but does not look like state (no recognised "
            "top-level keys) — delete or restore it manually"
        )
    return data


def _write(file_obj, data: dict, authorisation=None) -> None:
    """Overwrite instagram_state.json via an open, locked file object.

    Refuses to write without _transaction()'s authorisation token. Writing outside a
    transaction skips _preserve_unresolved_obligation(), which is what carries an
    unresolved publish across a record being removed or replaced — the invariant six
    rounds of review have been about. See _WriteAuthorisation.
    """
    if authorisation is not _WRITE_AUTHORISATION:
        raise RuntimeError(
            "_write() called outside _transaction(). Every write to instagram_state.json "
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
    """Open instagram_state.json for read+write. Returns (file, created_by_us).

    Authorised the same way as _write(), one level down: taking the exclusive lock outside
    a transaction is the first half of a bypass, so it is refused for the same reason.

    O_EXCL rather than plain O_CREAT so the caller can tell a file it just created from
    one that was already there. _read() refuses a present-but-empty file, so a
    transaction that creates one has to initialise it rather than read it — and has to do
    so even if it then declines to commit, or it would leave behind exactly the
    zero-length file _read() now refuses.

    Known, accepted first-run race: the new inode becomes visible between the O_EXCL
    creation and _transaction() taking its first flock. Another process can open it and
    win the lock in that window, find it zero length, and fail closed. That is a
    TRANSIENT FIRST-RUN ERROR on a brand-new client — not lost state and not a duplicate
    publish — and it self-heals on the next tick, once the creating process has written
    the defaults. Recorded here so the next reader recognises it as a startup race rather
    than mistaking it for corruption.
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
    """One exclusive-lock read-modify-write over instagram_state.json.

    `data` is the parsed state, free to mutate. Nothing is persisted unless commit() is
    called, which is what lets a transaction inspect the state and decline to change it —
    a claim declined for cooldown, an update whose key no longer matches — without
    rewriting and re-fsyncing the file on every cron tick.
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

    Why a chokepoint rather than a helper each mutator remembers to call: the
    unresolved-publish obligation (see the pending_publish_reconciliations section below)
    was lost at four separate mutation sites across successive reviews — the exhausted
    claim, stale_failed, mark_failed(), and set_pending_upload() overwriting a live
    record. Every one was found by enumerating mutation sites, and enumeration kept
    missing one, because a rule that has to be REMEMBERED at each site is exactly the kind
    a newly-added site silently omits.

    So the rule is enforced on the way out instead. _preserve_unresolved_obligation() runs
    on every committed write, comparing the pending record this transaction started with
    against the one it leaves behind. A mutation site cannot opt out, because it cannot
    persist anything without coming through here — and tests/test_instagram_state.py
    asserts mechanically, against the module source, that _write() is reachable from
    nowhere else.

    On the durability of that write, precisely: this transaction is atomic with respect to
    OTHER PROCESSES, because the exclusive flock is held across the whole read-modify-write
    and every mutation lands in a single _write() call. It is NOT crash-atomic. _write()
    overwrites and truncates the live file in place, so a crash mid-write can leave torn or
    truncated JSON, and an fsync failure leaves durability indeterminate. What makes that
    survivable is that _read() fails closed — on malformed JSON, on a present-but-zero-length
    file, on JSON that is not an object, and on an object carrying none of this module's
    keys. It is deliberately NOT claimed in general: a partial write that happens to parse
    AND carry a recognised key is not detectable there. Read _read() for the shape-by-shape
    account; asserting this as a general property is how it was got wrong once already.

    Making the write itself crash-atomic needs a write-temp-then-rename protocol, which
    interacts with the flock coordination here — replacing the inode invalidates locks held
    on the old one — and applies equally to facebook_state.py and state.py, which share this
    write pattern. Tracked as issue #79 rather than bolted on here.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f, created = _open_for_write(_WRITE_AUTHORISATION)
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            if created:
                # Initialise immediately, before yielding. A transaction that declines to
                # commit would otherwise leave the zero-length file it just created, and a
                # zero-length file is indistinguishable from an interrupted write — so
                # _read() refuses one. Writing the defaults here is what keeps "present but
                # empty" a genuine anomaly rather than an ordinary first-run leftover.
                data = copy.deepcopy(_DEFAULTS)
                _write(f, data, _WRITE_AUTHORISATION)
            else:
                data = _read(f)
            incoming = data.get("pending_instagram_upload")
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
    """True if record is asking an unanswered question about a specific container.

    The obligation is the PAIR — a container id, and a publish attempted against it that
    has not been settled. Either half alone is not an obligation: a container with no
    publish attempt cannot have gone live, and a marker with no container names nothing to
    reconcile. publish_settled_at is what closes it: Instagram reported that very container
    as never published, so the question has an answer and is no longer open.
    """
    if not record:
        return False
    return bool(record.get("container_id")) and bool(record.get("publish_attempted_at"))


def _preserve_unresolved_obligation(incoming: dict | None, data: dict) -> None:
    """Carry a departing record's unresolved publish into the quarantine list.

    Runs on every committed write. `incoming` is the pending record as this transaction
    found it; data["pending_instagram_upload"] is what the transaction is leaving there.

    The question this asks is NOT "is the same key still present" — that was the round-5
    hole. A record can keep its key and still lose its obligation: set_pending_upload()
    replacing key 42 (container_A, marker set) with a fresh key 42 (container_id=None, no
    marker) erased the obligation while looking, to a key comparison, like an in-place
    update. The question is whether the SPECIFIC OPEN QUESTION the incoming record was
    asking is still being tracked somewhere. It is, in exactly three cases:

      - the outgoing record is the same job, still holding the same container, and its
        publish is still open — an ordinary in-place update, nothing has left;
      - the outgoing record is the same job and same container with the publish SETTLED
        (publish_settled_at) — Instagram answered, so there is nothing to preserve;
      - the key has been recorded in published_idempotency_keys — the publish is now a
        fact, so any quarantine for it is retired rather than created.

    Anything else — removed, replaced, re-keyed, pointed at a different container, or
    silently stripped of its marker — means the obligation has left the record, and it is
    quarantined here. Removal, replacement and erasure are the same event as far as the
    obligation is concerned.
    """
    if not _has_open_publish(incoming):
        return
    key = incoming.get("idempotency_key")
    container_id = incoming.get("container_id")
    outgoing = data.get("pending_instagram_upload")

    if (
        outgoing is not None
        and outgoing.get("idempotency_key") == key
        and outgoing.get("container_id") == container_id
        and (_has_open_publish(outgoing) or outgoing.get("publish_settled_at"))
    ):
        return

    if key in data.get("published_idempotency_keys", []):
        _drop_publish_reconciliations_for_key(data, key)
        return

    _quarantine_unresolved_in_txn(incoming, data, datetime.now(timezone.utc).isoformat())


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

    The record-ingress API, and therefore a TRUST BOUNDARY: everything downstream that
    reasons about a job's publish state reads fields that arrive through here. It refuses
    a record carrying publish_attempted_at or publish_settled_at, because those are this
    module's account of what Meta did and not a caller's to assert (see _PROVENANCE_KEYS).

    Raises ValueError on missing keys, on a caller-supplied provenance field, on an
    idempotency_key that has already been published, and on one whose publish is still
    unresolved (the duplicate-post guards behind FR-011/SC-006).
    """
    missing = _REQUIRED_UPLOAD_KEYS - set(record.keys())
    if missing:
        raise ValueError(f"set_pending_upload: missing required keys: {missing}")
    # Provenance, not validation. publish_attempted_at and publish_settled_at are this
    # module's record of what Meta did; a caller supplying either is asserting a fact it
    # has no standing to assert. That matters because
    # _preserve_unresolved_obligation() reads publish_settled_at as proof the question was
    # answered — so accepting one here would let any caller forge an answer and quietly
    # release an unresolved publish. The sanctioned route is mark_publish_attempted() /
    # mark_publish_settled(), each of which writes the field only from a transition that
    # actually established the fact.
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
        record = data.get("pending_instagram_upload")
        if record is None or record.get("idempotency_key") != idempotency_key:
            return False
        updater(record, data)
        txn.commit()
        return True


def clear_pending_upload(expected_idempotency_key: str) -> bool:
    """Clear pending_instagram_upload back to null — but only if the CURRENT pending record's
    idempotency_key still matches expected_idempotency_key (compare-and-clear). Leaves
    published_idempotency_keys / published_history intact either way.

    Like every other transition that drops the record, an unresolved publish is carried
    into pending_publish_reconciliations rather than destroyed — by _transaction()'s
    chokepoint, not by anything written here. Nothing in the pipeline calls this function
    today, which is exactly why the guarantee must not depend on its call site.

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


def _add_publish_reconciliation(
    data: dict, *, container_id: str, project_name: str, idempotency_key: str, now: str
) -> bool:
    """Insert a quarantine entry into already-read state. Returns True if it was added.

    Operates on a `data` dict the caller has already read under the exclusive lock, which
    is the whole point: it lets a quarantine be created in the SAME transaction as the
    record removal that makes it necessary. Calling record_publish_reconciliation() from
    inside such a transaction would deadlock — it takes the same file lock again.

    Idempotent and non-counting: an entry that already exists is left exactly as it is.
    This is a safety net, not a reconciliation attempt, so it must not inflate the check
    count or disturb the alert schedule.

    New entries start at attempts=0 with last_alerted_at=None, mirroring
    record_share_intent(): nothing has been checked and nobody has been told yet, so the
    next drain tick performs the first real check and raises the first alert.
    """
    pending = data.setdefault("pending_publish_reconciliations", [])
    if any(entry.get("container_id") == container_id for entry in pending):
        return False
    pending.append({
        "container_id": container_id,
        "project_name": project_name,
        "idempotency_key": idempotency_key,
        "recorded_at": now,
        "last_attempt_at": None,
        "last_alerted_at": None,
        "attempts": 0,
    })
    return True


def _quarantine_unresolved_in_txn(record: dict, data: dict, now: str) -> bool:
    """Quarantine record's container, in-transaction, if its publish outcome is unknown.

    THE invariant this file maintains, and the reason it is enforced here rather than by
    convention at call sites: a pending record carries an unresolved publish marker
    (publish_attempted_at set, alongside the container_id it refers to) if and only if
    FieldKit asked Meta to publish that container and has not since established what
    happened. Any transaction that DROPS such a record without recording a publish must
    move the obligation into pending_publish_reconciliations in the same locked
    read-modify-write, or the obligation is lost at exactly the moment it starts to matter.

    That "same transaction" requirement is not theoretical. The exhausted path used to
    clear and fsync the pending record, return, and only then let the caller quarantine —
    so a process that died in between left neither a job nor a quarantine, and the next
    re-approval published a duplicate Reel.

    Called from ONE place: _preserve_unresolved_obligation(), which _transaction() runs on
    every committed write. Deliberately not called per-mutation-site any more — four
    separate sites were missed that way over successive reviews. Call sites may still
    quarantine explicitly through record_publish_reconciliation() when they want a timely
    alert; this is what makes the obligation survive when they don't, including at sites
    that do not exist yet.
    """
    if not _has_open_publish(record):
        return False
    container_id = record["container_id"]
    added = _add_publish_reconciliation(
        data,
        container_id=container_id,
        project_name=record.get("project_name", "unknown"),
        idempotency_key=record.get("idempotency_key", ""),
        now=now,
    )
    if added:
        logger.error(
            "quarantined an unresolved publish while discarding its job: container_id=%s "
            "key=%s — the Reel may be live; re-approval of this key is now blocked",
            container_id, record.get("idempotency_key"),
        )
    return added


def _drop_publish_reconciliations_for_key(data: dict, idempotency_key: str) -> int:
    """Remove any quarantine entries for idempotency_key from already-read state.

    The mirror of _quarantine_unresolved_in_txn(): recording a publish settles the
    question the quarantine existed to ask, so retiring the two together in one
    transaction is what stops a resolved obligation from outliving its own resolution.
    Returns how many entries were removed.
    """
    pending = data.get("pending_publish_reconciliations", [])
    remaining = [e for e in pending if e.get("idempotency_key") != idempotency_key]
    removed = len(pending) - len(remaining)
    if removed:
        data["pending_publish_reconciliations"] = remaining
    return removed


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
                           If the record carried an unresolved publish, it is moved into
                           pending_publish_reconciliations in the SAME transaction as the
                           clear — see _quarantine_unresolved_in_txn().
      "claimed"         — success: status is now 'uploading', attempt_count/last_attempt_at
                           already advanced for this attempt.
    """
    now_dt = datetime.now(timezone.utc)
    with _transaction() as txn:
        data = txn.data
        record = data.get("pending_instagram_upload")
        if record is None or record.get("idempotency_key") != idempotency_key:
            return "mismatch"

        # Each branch below that drops the record simply drops it. Carrying any unresolved
        # publish across that drop is _preserve_unresolved_obligation()'s job, applied to
        # every committed write on the way out — so the "exhausted" clear is atomic with
        # its quarantine instead of leaving the caller to follow up, and a branch added
        # here later inherits the same guarantee without having to know about it.
        if idempotency_key in data.get("published_idempotency_keys", []):
            # The chokepoint recognises this case and retires the quarantine rather than
            # creating one: the key is already in published_idempotency_keys, so
            # re-approval is permanently refused anyway and there is no duplicate to
            # prevent.
            data["pending_instagram_upload"] = None
            txn.commit()
            return "stale_published"

        if record.get("status") == "failed":
            data["pending_instagram_upload"] = None
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
            data["pending_instagram_upload"] = None
            txn.commit()
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
        txn.commit()
        return "claimed"


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
        record["publish_settled_at"] = None
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
        record["publish_settled_at"] = None
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
        # A confirmed publish answers the question any quarantine for this key was
        # asking, so the two are retired together rather than leaving a resolved
        # obligation behind to block the key forever.
        _drop_publish_reconciliations_for_key(data, idempotency_key)
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
    with _transaction() as txn:
        data = txn.data
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
        # This IS the answer the quarantine was waiting for, so retire it in the same
        # transaction that records the publish. Callers also clear it explicitly; doing
        # it here is what makes "recorded but still blocked" unrepresentable.
        _drop_publish_reconciliations_for_key(data, idempotency_key)
        record = data.get("pending_instagram_upload")
        if record is not None and record.get("idempotency_key") == idempotency_key:
            data["pending_instagram_upload"] = None
        txn.commit()
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

    Discarding the record does NOT discard an unresolved publish: _transaction()'s
    chokepoint moves it into pending_publish_reconciliations in this same write. Call
    sites that know the outcome should call mark_publish_settled() first (Instagram said
    it never published) or quarantine explicitly with an alert (outcome unknown), but the
    guarantee does not depend on their doing so.
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
    with _transaction() as txn:
        data = txn.data
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
        txn.commit()
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
    with _transaction() as txn:
        data = txn.data
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
            txn.commit()
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
        txn.commit()
        logger.error(
            "record_share_cleanup: file_id=%s project=%s — share link NOT revoked",
            file_id, project_name,
        )
        return dict(entry)


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

    with _transaction() as txn:
        data = txn.data
        removed = _remove(data)
        if removed:
            txn.commit()
            logger.info("clear_share_cleanup: file_id=%s revoked", file_id)
        return removed


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
# Instagram still satisfies FR-011.
#
# Facebook has the same latent exposure and is not addressed here. It cannot be
# fixed this way AS facebook_api.py IS CURRENTLY WRITTEN — its one-shot multipart
# upload knows no identifier until the response arrives, so there is no handle to
# reconcile against afterwards. That is a limit of this implementation, NOT of
# Meta's API: the sessionized video upload returns an upload_session_id and a
# video_id before any bytes move, which is exactly the durable pre-known handle
# this mechanism needs. Tracked as an urgent fast-follow in issue #78.


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


def mark_publish_settled(
    idempotency_key: str, container_id: str, observed_status: str
) -> None:
    """Clear the unresolved-publish marker: Instagram has said this container never published.

    The counterpart to mark_publish_attempted(), and what keeps the invariant in
    _quarantine_unresolved_in_txn() honest in BOTH directions. A container reported as
    FINISHED, ERROR or EXPIRED is definitively not live, so the question is answered and
    the record must stop looking like it has an open one — otherwise the next transaction
    to drop the record would quarantine it, blocking re-approval of a video Instagram has
    just confirmed was never posted.

    Records the answer rather than merely erasing the question: publish_settled_at is
    stamped as publish_attempted_at is cleared. That distinction is load-bearing, because
    _preserve_unresolved_obligation() cannot otherwise tell a legitimate settlement from a
    mutation that silently stripped the marker off a live record — and treating those the
    same is how the same-key escape got in. A settlement says so on the record; an erasure
    does not, and gets quarantined.

    container_id stays on the record, because it is still the handle for the container this
    job used and remains useful for debugging and for a retry's own reconciliation.

    Takes the EVIDENCE, not just the key. The caller must name the container it is settling
    and the Graph API status it actually observed, and both are checked: the status must be
    one that means "did not publish", and the container must be the one the record is
    holding. That is a runtime replacement for trusting the call site — a source scan only
    sees calls spelled `mark_publish_settled(...)`, so an alias assigned in another module
    was invisible to the call-site guard, and a reviewer used exactly that to forge a
    settlement and lose an obligation. Requiring the evidence means an aliased or
    accidentally-refactored call cannot settle at all (wrong arity), and no call can settle
    a container other than the one in play.

    What this does NOT do is prove Meta was consulted; nothing inside this module can,
    because that fact lives in the caller's control flow. It removes the accidental routes
    and confines the deliberate one to a call that has to state what it saw.

    Compare-and-update: a no-op if the current pending record's idempotency_key no longer matches.
    """
    if observed_status not in _NON_PUBLISHED_STATUSES:
        raise ValueError(
            f"mark_publish_settled: {observed_status!r} does not mean the container failed "
            f"to publish; expected one of {sorted(_NON_PUBLISHED_STATUSES)}"
        )
    now = datetime.now(timezone.utc).isoformat()
    mismatched = []

    def _update(record, data):
        if record.get("container_id") != container_id:
            # Settling a container the record is not holding would clear the marker for a
            # question that was never asked about that container.
            mismatched.append(record.get("container_id"))
            return
        record["publish_attempted_at"] = None
        record["publish_settled_at"] = now
    _update_pending(idempotency_key, _update)
    if mismatched:
        raise ValueError(
            f"mark_publish_settled: asked to settle container {container_id!r} but the "
            f"pending record holds {mismatched[0]!r}"
        )
    logger.info(
        "mark_publish_settled: key=%s container_id=%s status=%s",
        idempotency_key, container_id, observed_status,
    )


def record_publish_reconciliation(
    container_id: str, *, project_name: str, idempotency_key: str, counts_as_check: bool = True
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

    counts_as_check=False records an alert decision WITHOUT bumping the check count, for a
    caller that has something to say about the entry but did not actually manage to ask
    Instagram anything — the credential being absent, say. Inflating the count there would
    make the alert claim checks that never happened.

    Idempotent per container: re-recording bumps attempts rather than duplicating.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _transaction() as txn:
        data = txn.data
        pending = data.setdefault("pending_publish_reconciliations", [])
        for entry in pending:
            if entry.get("container_id") != container_id:
                continue
            if counts_as_check:
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_attempt_at"] = now
            should_alert = _has_elapsed(
                entry.get("last_alerted_at"),
                _PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS,
                now_dt,
            )
            if should_alert:
                entry["last_alerted_at"] = now
            txn.commit()
            logger.warning(
                "record_publish_reconciliation: container_id=%s still unresolved after "
                "%d checks (re-alerting=%s)",
                container_id, entry.get("attempts", 0), should_alert,
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
        txn.commit()
        logger.error(
            "record_publish_reconciliation: container_id=%s project=%s key=%s — publish "
            "outcome UNKNOWN; the Reel may be live. Re-approval of this key is blocked "
            "until Instagram is definitive.",
            container_id, project_name, idempotency_key,
        )
        return dict(entry)


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


def clear_publish_reconciliation(container_id: str, observed_status: str) -> bool:
    """Drop container_id from the unresolved list once Instagram has been definitive.

    Call this ONLY on a definitive answer — PUBLISHED (record it via
    record_recovered_publish() first), or FINISHED/ERROR/EXPIRED, all three of which mean
    the container was never published. Clearing on anything less would release the
    idempotency key while the Reel's fate is still unknown, which is the whole thing this
    list exists to prevent.

    Requires the observed status for the same reason mark_publish_settled() does: lifting a
    block asserts that Instagram answered, and a call-site guard cannot see an aliased call.
    Naming the status makes the assertion part of the call rather than of its location.

    Returns True if an entry was removed, False if there was nothing recorded for it.
    """
    if observed_status not in _DEFINITIVE_STATUSES:
        raise ValueError(
            f"clear_publish_reconciliation: {observed_status!r} is not a definitive "
            f"container status; expected one of {sorted(_DEFINITIVE_STATUSES)}"
        )
    with _transaction() as txn:
        data = txn.data
        pending = data.get("pending_publish_reconciliations", [])
        remaining = [e for e in pending if e.get("container_id") != container_id]
        if len(remaining) == len(pending):
            return False
        data["pending_publish_reconciliations"] = remaining
        txn.commit()
        logger.info(
            "clear_publish_reconciliation: container_id=%s resolved status=%s",
            container_id, observed_status,
        )
        return True
