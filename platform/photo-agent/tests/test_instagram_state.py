"""
Tests for tools/instagram_state.py — the Instagram upload state manager.

Modeled directly on tests/test_facebook_state.py, since instagram_state.py
mirrors facebook_state.py's atomic-claim state machine with Instagram-adapted
field names (ig_business_account_id, container_id, ig_post_id).

Covers: set_pending_upload (success, missing keys, duplicate idempotency key),
get_pending_upload, claim_pending_upload (every documented return value),
release_claim, clear_pending_upload, set_container_id, mark_published,
mark_failed, is_published, find_published, fcntl exclusive locking, and the
FIELDKIT_DATA_DIR import-time requirement.
"""

import importlib
import json
import threading

import pytest

import tools.instagram_state as ig_state


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    """Redirect DATA_DIR and STATE_FILE to an isolated tmp directory."""
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")


@pytest.fixture
def valid_record():
    """Return a fresh InstagramUploadJob record for each test."""
    return {
        "project_name": "kitchen_remodel",
        "video_local_path": "/tmp/kitchen_remodel/video.mp4",
        "ig_business_account_id": "17841400000000000",
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": None,
        "triggered_at": "2026-08-31T14:00:00Z",
        "idempotency_key": "42",
        "container_id": None,
        "ig_post_id": None,
    }


def _claim(key, *, cooldown_seconds=60, max_attempts=3, lease_seconds=900):
    return ig_state.claim_pending_upload(
        key,
        cooldown_seconds=cooldown_seconds,
        max_attempts=max_attempts,
        lease_seconds=lease_seconds,
    )


# --- get_pending_upload ---

def test_get_pending_upload_returns_none_when_file_missing():
    """get_pending_upload() returns None when instagram_state.json does not exist."""
    assert not ig_state.STATE_FILE.exists()
    assert ig_state.get_pending_upload() is None


def test_get_pending_upload_returns_none_when_null():
    """get_pending_upload() returns None when pending_instagram_upload is null."""
    ig_state.STATE_FILE.write_text(
        json.dumps({"pending_instagram_upload": None, "published_idempotency_keys": []})
    )
    assert ig_state.get_pending_upload() is None


def test_get_pending_upload_returns_stored_record(valid_record):
    """get_pending_upload() returns the exact record that was stored."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.get_pending_upload() == valid_record


# --- set_pending_upload ---

def test_set_pending_upload_writes_record(valid_record):
    """set_pending_upload() writes a record readable via get_pending_upload()."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.get_pending_upload() == valid_record


@pytest.mark.parametrize("missing_key", [
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
])
def test_set_pending_upload_missing_key_raises(valid_record, missing_key):
    """set_pending_upload() raises ValueError when any required key is missing."""
    del valid_record[missing_key]
    with pytest.raises(ValueError, match="missing required keys"):
        ig_state.set_pending_upload(valid_record)


def test_set_pending_upload_duplicate_published_key_raises(valid_record):
    """set_pending_upload() refuses a key already in published_idempotency_keys."""
    ig_state.STATE_FILE.write_text(json.dumps({
        "pending_instagram_upload": None,
        "published_idempotency_keys": ["42"],
        "published_history": [],
    }))
    with pytest.raises(ValueError, match="already in published_idempotency_keys"):
        ig_state.set_pending_upload(valid_record)


# --- claim_pending_upload ---

def test_claim_returns_mismatch_when_no_pending_record():
    """claim_pending_upload() returns 'mismatch' when nothing is pending."""
    assert _claim("42") == "mismatch"


def test_claim_returns_mismatch_when_key_differs(valid_record):
    """claim_pending_upload() declines when the pending key is not the caller's."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("999") == "mismatch"


def test_claim_returns_claimed_and_transitions_record(valid_record):
    """A granted claim sets status=uploading and advances attempt_count."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("42") == "claimed"
    record = ig_state.get_pending_upload()
    assert record["status"] == "uploading"
    assert record["attempt_count"] == 1
    assert record["last_attempt_at"] is not None


def test_claim_returns_in_flight_while_lease_holds(valid_record):
    """A second claim during an unexpired lease returns 'in_flight'."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("42") == "claimed"
    assert _claim("42", lease_seconds=900) == "in_flight"


def test_claim_returns_cooldown_when_not_elapsed(valid_record):
    """After a released claim, a retry inside the cooldown window is declined."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("42") == "claimed"
    ig_state.release_claim("42")
    assert _claim("42", cooldown_seconds=60) == "cooldown"


def test_claim_allows_retry_once_cooldown_elapsed(valid_record):
    """With cooldown_seconds=0 the released job is immediately reclaimable."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("42") == "claimed"
    ig_state.release_claim("42")
    assert _claim("42", cooldown_seconds=0) == "claimed"
    assert ig_state.get_pending_upload()["attempt_count"] == 2


def test_claim_reclaims_after_lease_expires(valid_record):
    """An abandoned claim (expired lease) is reclaimable, not stuck in_flight."""
    ig_state.set_pending_upload(valid_record)
    assert _claim("42") == "claimed"
    assert _claim("42", cooldown_seconds=0, lease_seconds=0) == "claimed"


def test_claim_returns_stale_published_and_clears(valid_record):
    """An already-published key found still pending is cleared, not reprocessed."""
    ig_state.set_pending_upload(valid_record)
    ig_state.STATE_FILE.write_text(json.dumps({
        "pending_instagram_upload": valid_record,
        "published_idempotency_keys": ["42"],
        "published_history": [],
    }))
    assert _claim("42") == "stale_published"
    assert ig_state.get_pending_upload() is None


def test_claim_returns_stale_failed_and_clears(valid_record):
    """A record already marked failed is cleared, not reprocessed."""
    valid_record["status"] = "failed"
    ig_state.STATE_FILE.write_text(json.dumps({
        "pending_instagram_upload": valid_record,
        "published_idempotency_keys": [],
        "published_history": [],
    }))
    assert _claim("42") == "stale_failed"
    assert ig_state.get_pending_upload() is None


def test_claim_returns_exhausted_at_max_attempts_and_clears(valid_record):
    """attempt_count already at max_attempts yields 'exhausted' and clears the record."""
    valid_record["attempt_count"] = 3
    valid_record["last_attempt_at"] = None
    ig_state.set_pending_upload(valid_record)
    assert _claim("42", max_attempts=3) == "exhausted"
    assert ig_state.get_pending_upload() is None


def test_claim_three_attempts_then_exhausted(valid_record):
    """Three successive claims are granted; the fourth is 'exhausted'."""
    ig_state.set_pending_upload(valid_record)
    for _ in range(3):
        assert _claim("42", cooldown_seconds=0) == "claimed"
        ig_state.release_claim("42")
    assert _claim("42", cooldown_seconds=0, max_attempts=3) == "exhausted"


def test_concurrent_claims_grant_exactly_one(valid_record):
    """Two threads racing to claim the same job: exactly one wins."""
    ig_state.set_pending_upload(valid_record)
    results = []
    lock = threading.Lock()

    def worker():
        outcome = _claim("42")
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("claimed") == 1


# --- release_claim ---

def test_release_claim_resets_status_to_pending(valid_record):
    """release_claim() returns status to 'pending' without touching attempt_count."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.release_claim("42")
    record = ig_state.get_pending_upload()
    assert record["status"] == "pending"
    assert record["attempt_count"] == 1


def test_release_claim_keeps_container_id_for_reconciliation(valid_record):
    """release_claim() PRESERVES container_id — it is the evidence FR-011 depends on.

    This asserts the opposite of what it originally did. Clearing container_id per attempt
    was the bug: publish_container() is the irreversible side effect and mark_published()
    is the durable record of it, so a crash in between leaves a live Reel unrecorded — and
    discarding the container id threw away the only handle that could reveal it, letting
    the next attempt publish the same video a second time. The next attempt must be able to
    ask Instagram what became of this container before it publishes anything.
    """
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.set_container_id("42", "container_abc")
    assert ig_state.get_pending_upload()["container_id"] == "container_abc"
    ig_state.release_claim("42")
    assert ig_state.get_pending_upload()["container_id"] == "container_abc"
    assert ig_state.get_pending_upload()["status"] == "pending"


def test_claim_keeps_container_id_from_an_abandoned_attempt(valid_record):
    """A reclaimed abandoned attempt keeps its container id, for the same reason.

    The abandoned-claim path is precisely the crash path: whatever killed the previous
    attempt may have killed it after Instagram published. claim_pending_upload() used to
    reset container_id to None here, which is the same loss of evidence from the other
    direction.
    """
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.set_container_id("42", "container_abc")
    # Reclaim after the lease expires, as an abandoned attempt would be.
    claim = ig_state.claim_pending_upload(
        "42", cooldown_seconds=0, max_attempts=5, lease_seconds=0
    )
    assert claim == "claimed"
    assert ig_state.get_pending_upload()["container_id"] == "container_abc"


def test_release_claim_ignores_mismatched_key(valid_record):
    """release_claim() with a stale key leaves a newer pending job untouched."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.release_claim("999")
    assert ig_state.get_pending_upload()["status"] == "uploading"


# --- set_container_id ---

def test_set_container_id_stores_value(valid_record):
    """set_container_id() records the in-flight container on the pending job."""
    ig_state.set_pending_upload(valid_record)
    ig_state.set_container_id("42", "17890000000000000")
    assert ig_state.get_pending_upload()["container_id"] == "17890000000000000"


def test_set_container_id_ignores_mismatched_key(valid_record):
    """set_container_id() is a no-op when the pending key has changed."""
    ig_state.set_pending_upload(valid_record)
    ig_state.set_container_id("999", "17890000000000000")
    assert ig_state.get_pending_upload()["container_id"] is None


# --- clear_pending_upload ---

def test_clear_pending_upload_clears_matching_key(valid_record):
    """clear_pending_upload() clears the record and reports True."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.clear_pending_upload("42") is True
    assert ig_state.get_pending_upload() is None


def test_clear_pending_upload_refuses_mismatched_key(valid_record):
    """clear_pending_upload() never destroys a newer job under a different key."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.clear_pending_upload("999") is False
    assert ig_state.get_pending_upload() == valid_record


# --- mark_published ---

def test_mark_published_records_key_and_clears_pending(valid_record):
    """mark_published() adds the key to published_idempotency_keys and clears pending."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "ig_post_1")
    assert ig_state.get_pending_upload() is None
    assert ig_state.is_published("42") is True


def test_mark_published_clears_container_id_via_pending_clear(valid_record):
    """A published job leaves no container_id behind — the whole record is cleared."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.set_container_id("42", "container_abc")
    ig_state.mark_published("42", "ig_post_1")
    assert ig_state.get_pending_upload() is None


def test_mark_published_writes_history_entry(valid_record):
    """mark_published() appends a published_history entry carrying ig_post_id."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "ig_post_1")
    data = json.loads(ig_state.STATE_FILE.read_text())
    entry = data["published_history"][-1]
    assert entry["project_name"] == "kitchen_remodel"
    assert entry["ig_post_id"] == "ig_post_1"
    assert entry["idempotency_key"] == "42"


def test_mark_published_ignores_mismatched_key(valid_record):
    """mark_published() with a stale key does not mutate a newer pending job."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("999", "ig_post_1")
    assert ig_state.get_pending_upload() == valid_record
    assert ig_state.is_published("999") is False


def test_published_history_is_capped(valid_record):
    """published_history never grows past _PUBLISH_HISTORY_LIMIT entries."""
    limit = ig_state._PUBLISH_HISTORY_LIMIT
    for i in range(limit + 5):
        record = dict(valid_record, idempotency_key=str(i))
        ig_state.set_pending_upload(record)
        ig_state.mark_published(str(i), f"post_{i}")
    data = json.loads(ig_state.STATE_FILE.read_text())
    assert len(data["published_history"]) == limit


# --- mark_failed ---

def test_mark_failed_clears_pending(valid_record):
    """mark_failed() clears the pending record — failure is terminal."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_failed("42")
    assert ig_state.get_pending_upload() is None


def test_mark_failed_does_not_mark_published(valid_record):
    """A failed job never enters published_idempotency_keys."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_failed("42")
    assert ig_state.is_published("42") is False


def test_mark_failed_ignores_mismatched_key(valid_record):
    """mark_failed() with a stale key leaves a newer pending job intact."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_failed("999")
    assert ig_state.get_pending_upload() == valid_record


# --- is_published / find_published ---

def test_is_published_false_when_file_missing():
    """is_published() returns False when the state file does not exist."""
    assert ig_state.is_published("42") is False


def test_find_published_returns_none_when_never_published():
    """find_published() returns None for a project with no publish history."""
    assert ig_state.find_published("kitchen_remodel") is None


def test_find_published_returns_most_recent_entry(valid_record):
    """find_published() returns the newest history entry for the project."""
    for i, post in enumerate(["post_a", "post_b"]):
        ig_state.set_pending_upload(dict(valid_record, idempotency_key=str(i)))
        ig_state.mark_published(str(i), post)
    entry = ig_state.find_published("kitchen_remodel")
    assert entry["ig_post_id"] == "post_b"


def test_find_published_ignores_other_projects(valid_record):
    """find_published() does not return another project's publish."""
    ig_state.set_pending_upload(dict(valid_record, project_name="other_project"))
    ig_state.mark_published("42", "post_other")
    assert ig_state.find_published("kitchen_remodel") is None


# --- corruption / locking / env ---

def test_corrupt_state_file_raises_runtime_error():
    """A corrupt instagram_state.json raises RuntimeError, not JSONDecodeError."""
    ig_state.STATE_FILE.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        ig_state.get_pending_upload()


def test_claim_acquires_exclusive_lock(valid_record, mocker):
    """claim_pending_upload() takes an fcntl.LOCK_EX lock before mutating."""
    import fcntl as _fcntl
    ig_state.set_pending_upload(valid_record)
    spy = mocker.spy(ig_state.fcntl, "flock")
    _claim("42")
    assert any(call.args[1] == _fcntl.LOCK_EX for call in spy.call_args_list)


def test_get_pending_upload_acquires_shared_lock(valid_record, mocker):
    """get_pending_upload() takes an fcntl.LOCK_SH lock for its read."""
    import fcntl as _fcntl
    ig_state.set_pending_upload(valid_record)
    spy = mocker.spy(ig_state.fcntl, "flock")
    ig_state.get_pending_upload()
    assert any(call.args[1] == _fcntl.LOCK_SH for call in spy.call_args_list)


def test_state_file_path_derives_from_fieldkit_data_dir(monkeypatch, tmp_path):
    """STATE_FILE resolves under $FIELDKIT_DATA_DIR/photo-agent/ at import time."""
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "clientdata"))
    reloaded = importlib.reload(ig_state)
    try:
        assert reloaded.STATE_FILE == tmp_path / "clientdata" / "photo-agent" / "instagram_state.json"
    finally:
        importlib.reload(ig_state)


def test_import_without_fieldkit_data_dir_raises(monkeypatch):
    """Importing instagram_state without FIELDKIT_DATA_DIR raises, like facebook_state."""
    monkeypatch.delenv("FIELDKIT_DATA_DIR", raising=False)
    with pytest.raises(RuntimeError, match="FIELDKIT_DATA_DIR is not set"):
        importlib.reload(ig_state)
    monkeypatch.undo()
    importlib.reload(ig_state)


# --- has_outstanding_job (cross-platform cleanup coordination) ---

def test_has_outstanding_job_true_while_pending(valid_record):
    """A freshly enqueued job is outstanding."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.has_outstanding_job("42") is True


def test_has_outstanding_job_true_while_claimed(valid_record):
    """A job mid-upload is still outstanding — the other platform must wait."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    assert ig_state.has_outstanding_job("42") is True


def test_has_outstanding_job_false_when_nothing_enqueued():
    """A key that was never enqueued is not outstanding."""
    assert ig_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_after_published(valid_record):
    """Publishing resolves the job."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "post_1")
    assert ig_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_after_failed(valid_record):
    """A terminal failure resolves the job just as much as a publish does."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_failed("42")
    assert ig_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_for_a_different_key(valid_record):
    """A pending job under another key says nothing about this one."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.has_outstanding_job("999") is False


# --- mark_published permalink ---

def test_mark_published_stores_the_permalink(valid_record):
    """The permalink is persisted alongside the media id."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "post_1", permalink="https://www.instagram.com/reel/Abc/")
    entry = ig_state.find_published("kitchen_remodel")
    assert entry["ig_post_id"] == "post_1"
    assert entry["ig_permalink"] == "https://www.instagram.com/reel/Abc/"


def test_mark_published_permalink_defaults_to_none(valid_record):
    """A publish whose permalink lookup failed is still recorded."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "post_1")
    assert ig_state.find_published("kitchen_remodel")["ig_permalink"] is None


# --- pending share-link cleanups ---

def test_record_share_cleanup_adds_entry():
    """A failed revoke is recorded durably with its file id and project."""
    assert ig_state.record_share_cleanup("file_1", "kitchen_remodel") is not None
    entries = ig_state.list_share_cleanups()
    assert len(entries) == 1
    assert entries[0]["file_id"] == "file_1"
    assert entries[0]["project_name"] == "kitchen_remodel"
    assert entries[0]["attempts"] == 1


def test_record_share_cleanup_first_failure_returns_entry_to_alert_on():
    """The first failure always alerts, and hands back the entry to describe it."""
    entry = ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    assert entry["file_id"] == "file_1"
    assert entry["project_name"] == "kitchen_remodel"
    assert entry["attempts"] == 1


def test_record_share_cleanup_is_idempotent_per_file():
    """Re-recording the same file bumps its attempt count instead of duplicating it."""
    assert ig_state.record_share_cleanup("file_1", "kitchen_remodel") is not None
    assert ig_state.record_share_cleanup("file_1", "kitchen_remodel") is None
    entries = ig_state.list_share_cleanups()
    assert len(entries) == 1
    assert entries[0]["attempts"] == 2


def test_record_share_cleanup_returns_none_inside_the_alert_interval():
    """Within the re-alert window, repeated failures are silent (but still counted)."""
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    for _ in range(5):
        assert ig_state.record_share_cleanup("file_1", "kitchen_remodel") is None
    assert ig_state.list_share_cleanups()[0]["attempts"] == 6


def test_record_share_cleanup_reescalates_after_the_interval(monkeypatch):
    """A link that keeps failing must eventually re-alert, not go quiet forever.

    The one-alert-ever behaviour this replaces meant a link that never got revoked was
    mentioned once and then silently retried indefinitely.
    """
    monkeypatch.setattr(ig_state, "_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS", 0)
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    entry = ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    assert entry is not None
    assert entry["attempts"] == 2


def test_reescalation_reports_the_growing_attempt_count(monkeypatch):
    """Each re-alert carries how many attempts have failed, so escalation is legible."""
    monkeypatch.setattr(ig_state, "_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS", 0)
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    counts = [ig_state.record_share_cleanup("file_1", "kitchen_remodel")["attempts"]
              for _ in range(3)]
    assert counts == [2, 3, 4]


def test_reescalation_stamps_last_alerted_at(monkeypatch):
    """The alert clock is persisted, so the interval survives across cron ticks."""
    monkeypatch.setattr(ig_state, "_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS", 0)
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    first = ig_state.list_share_cleanups()[0]["last_alerted_at"]
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    assert ig_state.list_share_cleanups()[0]["last_alerted_at"] >= first


def test_default_alert_interval_is_daily():
    """A dangling public link is worth a daily reminder, not a per-minute one."""
    assert ig_state._SHARE_CLEANUP_ALERT_INTERVAL_SECONDS == 24 * 60 * 60


def test_record_share_cleanup_tracks_multiple_files():
    """Two different dangling links are tracked independently."""
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    ig_state.record_share_cleanup("file_2", "bathroom_remodel")
    assert {e["file_id"] for e in ig_state.list_share_cleanups()} == {"file_1", "file_2"}


def test_clear_share_cleanup_removes_entry():
    """A successful retry clears the record."""
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    assert ig_state.clear_share_cleanup("file_1") is True
    assert ig_state.list_share_cleanups() == []


def test_clear_share_cleanup_leaves_other_entries(valid_record):
    """Clearing one dangling link does not forget the others."""
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    ig_state.record_share_cleanup("file_2", "bathroom_remodel")
    ig_state.clear_share_cleanup("file_1")
    assert [e["file_id"] for e in ig_state.list_share_cleanups()] == ["file_2"]


def test_clear_share_cleanup_unknown_file_returns_false():
    """Clearing something never recorded is a no-op, not an error."""
    assert ig_state.clear_share_cleanup("never_seen") is False


def test_list_share_cleanups_empty_when_file_missing():
    """No state file means nothing pending."""
    assert ig_state.list_share_cleanups() == []


def test_share_cleanups_survive_job_resolution(valid_record):
    """The cleanup list is independent of the upload job's lifecycle."""
    ig_state.set_pending_upload(valid_record)
    ig_state.record_share_cleanup("file_1", "kitchen_remodel")
    ig_state.mark_published("42", "post_1")
    assert len(ig_state.list_share_cleanups()) == 1


# ---------------------------------------------------------------------------
# record_share_intent — the cleanup obligation registered before exposure exists
# ---------------------------------------------------------------------------

def test_record_share_intent_registers_a_cleanup_obligation():
    """A file id written down before the file is ever made public."""
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    entries = ig_state.list_share_cleanups()
    assert [e["file_id"] for e in entries] == ["drive_file_1"]
    assert entries[0]["project_name"] == "kitchen_remodel"


def test_record_share_intent_does_not_count_as_a_failed_attempt():
    """Nothing has gone wrong yet, so it must not look like a failure.

    attempts starts at 0 and last_alerted_at at None, which is what keeps the admin
    from being alerted about a link that is about to be revoked in the normal way
    moments later — and what makes the FIRST genuine revoke failure still alert.
    """
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    entry = ig_state.list_share_cleanups()[0]
    assert entry["attempts"] == 0
    assert entry["last_alerted_at"] is None
    assert entry["last_attempt_at"] is None


def test_the_first_real_failure_after_an_intent_still_alerts():
    """The intent must not swallow the alert a genuine revoke failure has to produce."""
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    alert = ig_state.record_share_cleanup("drive_file_1", "kitchen_remodel")
    assert alert is not None
    assert alert["attempts"] == 1


def test_record_share_intent_is_idempotent():
    """A retry that re-registers the same file must not duplicate or reset it."""
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    ig_state.record_share_cleanup("drive_file_1", "kitchen_remodel")   # attempts -> 1
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    entries = ig_state.list_share_cleanups()
    assert len(entries) == 1
    assert entries[0]["attempts"] == 1        # history preserved, not reset


def test_an_intent_is_cleared_by_a_successful_revoke():
    """The normal path: registered, revoked, retired — no residue."""
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    assert ig_state.clear_share_cleanup("drive_file_1") is True
    assert ig_state.list_share_cleanups() == []


def test_intents_for_different_files_coexist():
    """Two attempts in flight across ticks must not overwrite each other."""
    ig_state.record_share_intent("drive_file_1", "kitchen_remodel")
    ig_state.record_share_intent("drive_file_2", "bathroom")
    assert {e["file_id"] for e in ig_state.list_share_cleanups()} == {"drive_file_1", "drive_file_2"}


# ---------------------------------------------------------------------------
# record_recovered_publish — FR-011 across a crash
# ---------------------------------------------------------------------------

def test_record_recovered_publish_retires_the_idempotency_key(valid_record):
    """The single most important effect: a re-approval can never post a second Reel."""
    ig_state.set_pending_upload(valid_record)
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    assert ig_state.is_published("42") is True
    with pytest.raises(ValueError):
        ig_state.set_pending_upload(valid_record)


def test_record_recovered_publish_clears_a_matching_pending_record(valid_record):
    """The job is terminal and successful, so it must stop being picked up."""
    ig_state.set_pending_upload(valid_record)
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    assert ig_state.get_pending_upload() is None


def test_record_recovered_publish_works_with_no_pending_record_at_all():
    """The case mark_published() cannot serve, and the reason this function exists.

    When the final attempt is the one that crashed, claim_pending_upload() has already
    cleared the record as "exhausted" before anything gets to reconcile. If recording
    the publish depended on that record still existing, the key would never be retired
    and a re-approval would duplicate the Reel.
    """
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    assert ig_state.is_published("42") is True


def test_record_recovered_publish_does_not_disturb_a_different_pending_job(valid_record):
    """Compare-and-clear, like every other mutator here."""
    newer = dict(valid_record, idempotency_key="99")
    ig_state.set_pending_upload(newer)
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    assert ig_state.get_pending_upload()["idempotency_key"] == "99"
    assert ig_state.is_published("42") is True


def test_the_recovered_history_entry_is_honest_about_what_is_unknown(valid_record):
    """No fabricated post id or permalink — the media id is genuinely unknowable.

    The Graph API offers no container -> media lookup, and guessing from the account's
    recent media could latch onto something a human posted. The entry records the
    container it DOES know and flags itself as recovered.
    """
    ig_state.set_pending_upload(valid_record)
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    entry = ig_state.find_published("kitchen_remodel")
    assert entry["ig_post_id"] is None
    assert entry["ig_permalink"] is None
    assert entry["ig_container_id"] == "container_abc"
    assert entry["recovered"] is True


def test_a_recovered_publish_makes_a_later_claim_stale_published(valid_record):
    """Belt and braces: even a record that survives somehow will not be reprocessed."""
    ig_state.set_pending_upload(valid_record)
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    ig_state.set_pending_upload(dict(valid_record, idempotency_key="99"))
    # Re-point the pending record at the recovered key to simulate a stale file.
    import json
    raw = json.loads(ig_state.STATE_FILE.read_text())
    raw["pending_instagram_upload"]["idempotency_key"] = "42"
    ig_state.STATE_FILE.write_text(json.dumps(raw))
    assert _claim("42") == "stale_published"


def test_recovered_entries_respect_the_history_cap(valid_record):
    """published_history stays bounded however the entries got there."""
    for i in range(ig_state._PUBLISH_HISTORY_LIMIT + 10):
        ig_state.record_recovered_publish(f"key_{i}", "kitchen_remodel", f"container_{i}")
    import json
    history = json.loads(ig_state.STATE_FILE.read_text())["published_history"]
    assert len(history) == ig_state._PUBLISH_HISTORY_LIMIT


# ---------------------------------------------------------------------------
# pending_publish_reconciliations — FR-011 across TERMINAL failure
# ---------------------------------------------------------------------------
#
# Keeping container_id alive across attempts closes the crash window within a job.
# It does not close it across the end of the job: if the publish lands, its response
# is lost, and the container then cannot be reconciled for the whole retry budget,
# mark_failed() discards the record and container_id with it. Nothing can check
# again, and a re-approval is accepted because the key never reached
# published_idempotency_keys. These tests pin the durable quarantine that closes it.

def test_record_publish_reconciliation_registers_the_container():
    """An unknown publish outcome is written down, not merely warned about."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    entries = ig_state.list_publish_reconciliations()
    assert [e["container_id"] for e in entries] == ["container_abc"]
    assert entries[0]["idempotency_key"] == "42"
    assert entries[0]["project_name"] == "kitchen_remodel"


def test_an_unresolved_publish_survives_mark_failed(valid_record):
    """THE property the round-2 fix was missing.

    mark_failed() discards the whole record — deliberately, mirroring facebook_state —
    so anything stored ON the record dies with it. The quarantine is stored outside the
    record precisely so that the two can both be true.
    """
    ig_state.set_pending_upload(valid_record)
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    ig_state.mark_failed("42")
    assert ig_state.get_pending_upload() is None          # record really is gone
    assert ig_state.has_unresolved_publish("42") is True  # obligation really is not


def test_an_unresolved_publish_blocks_re_approval(valid_record):
    """The control itself: the same video cannot be queued again while its fate is unknown."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    with pytest.raises(ValueError, match="unresolved publish"):
        ig_state.set_pending_upload(valid_record)


def test_the_block_is_scoped_to_the_held_key(valid_record):
    """A different video is not collateral damage — only the unresolved one is held."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    other = dict(valid_record, idempotency_key="99")
    ig_state.set_pending_upload(other)                    # must not raise
    assert ig_state.get_pending_upload()["idempotency_key"] == "99"
    assert ig_state.has_unresolved_publish("99") is False


def test_clearing_the_quarantine_releases_the_key(valid_record):
    """Once Instagram says it never published, the video must become re-approvable."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    assert ig_state.clear_publish_reconciliation("container_abc") is True
    assert ig_state.has_unresolved_publish("42") is False
    ig_state.set_pending_upload(valid_record)             # must not raise


def test_clearing_an_unknown_container_reports_false():
    """Distinguishes "resolved it" from "there was nothing there"."""
    assert ig_state.clear_publish_reconciliation("never_recorded") is False


def test_recording_a_publish_recovery_also_permanently_retires_the_key(valid_record):
    """The PUBLISHED resolution is doubly safe: quarantine cleared, key in the published list."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    ig_state.clear_publish_reconciliation("container_abc")
    assert ig_state.has_unresolved_publish("42") is False
    with pytest.raises(ValueError, match="already in published_idempotency_keys"):
        ig_state.set_pending_upload(valid_record)


def test_re_recording_bumps_the_check_count_rather_than_duplicating():
    """The drain re-records on every failed check; the list must not grow per tick."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    entries = ig_state.list_publish_reconciliations()
    assert len(entries) == 1
    assert entries[0]["attempts"] == 2


def test_the_admin_is_alerted_on_the_first_record():
    """A Reel that may be live on a client account is not something to notice silently."""
    assert ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    ) is not None


def test_the_admin_is_not_re_alerted_within_the_reminder_window():
    """The drain runs every minute; alerting every minute would be noise, not signal."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    assert ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    ) is None


def test_the_admin_is_re_alerted_once_the_window_elapses(monkeypatch):
    """A permanently-unresolvable container must keep surfacing, not be mentioned once."""
    monkeypatch.setattr(ig_state, "_PUBLISH_RECONCILE_ALERT_INTERVAL_SECONDS", 0)
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    for _ in range(3):
        assert ig_state.record_publish_reconciliation(
            "container_abc", project_name="kitchen_remodel", idempotency_key="42"
        ) is not None


def test_has_unresolved_publish_is_false_with_no_state_file():
    """Read on every approval, including the very first one on a new client."""
    assert ig_state.has_unresolved_publish("42") is False


def test_quarantines_for_different_containers_coexist():
    """Two videos can be unresolved at once without either masking the other."""
    ig_state.record_publish_reconciliation(
        "container_a", project_name="kitchen", idempotency_key="1"
    )
    ig_state.record_publish_reconciliation(
        "container_b", project_name="bathroom", idempotency_key="2"
    )
    assert ig_state.has_unresolved_publish("1") is True
    assert ig_state.has_unresolved_publish("2") is True
    ig_state.clear_publish_reconciliation("container_a")
    assert ig_state.has_unresolved_publish("1") is False
    assert ig_state.has_unresolved_publish("2") is True


# --- mark_publish_attempted: the marker that makes the quarantine precise ---

def test_mark_publish_attempted_records_the_marker(valid_record):
    """Written BEFORE publish_container(), so it exists even if that call never returns."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.mark_publish_attempted("42")
    assert ig_state.get_pending_upload()["publish_attempted_at"] is not None


def test_the_publish_marker_survives_a_released_claim(valid_record):
    """The next attempt has to know an earlier one already asked Meta to publish."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.set_container_id("42", "container_abc")
    ig_state.mark_publish_attempted("42")
    ig_state.release_claim("42")
    assert ig_state.get_pending_upload()["publish_attempted_at"] is not None
    assert ig_state.get_pending_upload()["container_id"] == "container_abc"


def test_a_new_container_clears_the_publish_marker(valid_record):
    """A stale marker on a fresh container would quarantine a job for no reason.

    The two fields are one fact — which container is in play, and whether anything has
    been published from it — so set_container_id() resets the marker in the same
    transaction.
    """
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.set_container_id("42", "container_abc")
    ig_state.mark_publish_attempted("42")
    ig_state.set_container_id("42", "container_xyz")
    assert ig_state.get_pending_upload()["publish_attempted_at"] is None
    assert ig_state.get_pending_upload()["container_id"] == "container_xyz"


def test_a_record_without_the_marker_reads_as_not_attempted(valid_record):
    """Back-compatibility: a job enqueued before this field existed must still work."""
    ig_state.set_pending_upload(valid_record)
    assert ig_state.get_pending_upload().get("publish_attempted_at") is None


def test_mark_publish_attempted_ignores_a_mismatched_key(valid_record):
    """Compare-and-update, like every other mutator here."""
    ig_state.set_pending_upload(valid_record)
    _claim("42")
    ig_state.mark_publish_attempted("999")
    assert ig_state.get_pending_upload().get("publish_attempted_at") is None


# ---------------------------------------------------------------------------
# The quarantine is created ATOMICALLY with the transition that requires it
# ---------------------------------------------------------------------------
#
# Round 3 made the quarantine survive mark_failed(). Round 4 closes the other half:
# CREATING the entry has to happen in the same locked read-modify-write as the
# removal that necessitates it. claim_pending_upload() used to clear and fsync the
# pending record, return "exhausted", and leave the caller to quarantine — so a
# process that died in between left neither a job nor an obligation, and the next
# re-approval could publish a duplicate Reel.
#
# These tests assert on state AFTER the state call alone, with no caller
# cooperation whatsoever. That is the point: if the guarantee needed a caller to
# follow up, it would not be a guarantee.

def _unresolved_record(valid_record, attempts=3):
    """A record that asked Meta to publish and never found out what happened."""
    return dict(
        valid_record,
        attempt_count=attempts,
        container_id="container_abc",
        publish_attempted_at="2026-08-31T14:05:00Z",
    )


def test_the_exhausted_transition_quarantines_in_the_same_transaction(valid_record):
    """THE round-4 fix: the entry exists the instant "exhausted" is returned."""
    ig_state.set_pending_upload(_unresolved_record(valid_record))
    assert _claim("42") == "exhausted"
    # No caller has run. The obligation is already durable.
    assert ig_state.get_pending_upload() is None
    assert ig_state.has_unresolved_publish("42") is True
    assert [e["container_id"] for e in ig_state.list_publish_reconciliations()] == ["container_abc"]


def test_a_crash_right_after_the_exhausted_claim_still_blocks_re_approval(valid_record):
    """Step 6 of the surviving sequence, made impossible.

    Simulates the process dying between claim_pending_upload() returning and anything
    else happening — the state file is all that is left, and it must already refuse the
    re-approval on its own.
    """
    ig_state.set_pending_upload(_unresolved_record(valid_record))
    _claim("42")
    with pytest.raises(ValueError, match="unresolved publish"):
        ig_state.set_pending_upload(valid_record)


def test_the_exhausted_quarantine_starts_unchecked_and_unannounced(valid_record):
    """attempts=0 / last_alerted_at=None, so the very next drain tick checks AND alerts.

    Stamping it as already-alerted would mean a crash here bought 24 hours of silence
    about a Reel that may be live.
    """
    ig_state.set_pending_upload(_unresolved_record(valid_record))
    _claim("42")
    entry = ig_state.list_publish_reconciliations()[0]
    assert entry["attempts"] == 0
    assert entry["last_alerted_at"] is None
    # ...and the next real check does alert.
    assert ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    ) is not None


def test_an_exhausted_job_that_never_published_is_not_quarantined(valid_record):
    """No publish attempt, no obligation — the stuck-container case stays re-approvable."""
    ig_state.set_pending_upload(dict(valid_record, attempt_count=3, container_id="container_abc"))
    assert _claim("42") == "exhausted"
    assert ig_state.list_publish_reconciliations() == []
    ig_state.set_pending_upload(valid_record)            # must not raise


def test_a_stale_failed_record_is_quarantined_too(valid_record):
    """Same class of transition, same guarantee — found by looking, not by a later review."""
    ig_state.set_pending_upload(_unresolved_record(valid_record, attempts=0))
    import json
    raw = json.loads(ig_state.STATE_FILE.read_text())
    raw["pending_instagram_upload"]["status"] = "failed"
    ig_state.STATE_FILE.write_text(json.dumps(raw))
    assert _claim("42") == "stale_failed"
    assert ig_state.has_unresolved_publish("42") is True


def test_mark_failed_quarantines_without_caller_cooperation(valid_record):
    """The round-3 call-site convention is now a backstop, not the mechanism."""
    ig_state.set_pending_upload(_unresolved_record(valid_record))
    ig_state.mark_failed("42")
    assert ig_state.get_pending_upload() is None
    assert ig_state.has_unresolved_publish("42") is True


def test_clear_pending_upload_quarantines_too(valid_record):
    """Nothing calls this today — which is exactly why the guarantee belongs in the module."""
    ig_state.set_pending_upload(_unresolved_record(valid_record))
    assert ig_state.clear_pending_upload("42") is True
    assert ig_state.has_unresolved_publish("42") is True


def test_a_published_key_is_not_quarantined_on_a_stale_claim(valid_record):
    """Deliberately exempt: the key is already retired, so there is no duplicate to prevent."""
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "ig_post_1")
    ig_state.set_pending_upload(dict(_unresolved_record(valid_record), idempotency_key="99"))
    import json
    raw = json.loads(ig_state.STATE_FILE.read_text())
    raw["pending_instagram_upload"]["idempotency_key"] = "42"
    ig_state.STATE_FILE.write_text(json.dumps(raw))
    assert _claim("42") == "stale_published"
    assert ig_state.list_publish_reconciliations() == []


# --- mark_publish_settled keeps the invariant honest in the other direction ---

def test_a_settled_publish_is_not_quarantined_by_a_later_mark_failed(valid_record):
    """Instagram said FINISHED, so blocking the re-approval would be wrong.

    Without clearing the marker, the safety net in mark_failed() would quarantine a video
    Instagram has just confirmed was never posted — trading one failure mode for another.
    """
    ig_state.set_pending_upload(_unresolved_record(valid_record, attempts=0))
    ig_state.mark_publish_settled("42")
    ig_state.mark_failed("42")
    assert ig_state.list_publish_reconciliations() == []
    ig_state.set_pending_upload(valid_record)            # re-approvable again


def test_mark_publish_settled_keeps_the_container_id(valid_record):
    """Only the open question is closed; the handle stays for debugging and retries."""
    ig_state.set_pending_upload(_unresolved_record(valid_record, attempts=0))
    ig_state.mark_publish_settled("42")
    record = ig_state.get_pending_upload()
    assert record["publish_attempted_at"] is None
    assert record["container_id"] == "container_abc"


# --- recording a publish retires the obligation in the same transaction ---

def test_recording_a_recovered_publish_drops_its_quarantine(valid_record):
    """A resolved obligation must not outlive its own resolution."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    ig_state.record_recovered_publish("42", "kitchen_remodel", "container_abc")
    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.has_unresolved_publish("42") is False


def test_mark_published_drops_a_quarantine_for_the_same_key(valid_record):
    """The ordinary success path closes the question too, and must not leave a block behind."""
    ig_state.set_pending_upload(valid_record)
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    ig_state.mark_published("42", "ig_post_1")
    assert ig_state.list_publish_reconciliations() == []


def test_a_quarantine_for_a_different_key_survives_an_unrelated_publish(valid_record):
    """Retiring one key must not release another video's block."""
    ig_state.record_publish_reconciliation(
        "container_other", project_name="bathroom", idempotency_key="99"
    )
    ig_state.set_pending_upload(valid_record)
    ig_state.mark_published("42", "ig_post_1")
    assert ig_state.has_unresolved_publish("99") is True


# --- counts_as_check ---

def test_a_non_check_record_does_not_inflate_the_check_count():
    """An alert about a credential being absent is not evidence FieldKit tried anything."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    before = ig_state.list_publish_reconciliations()[0]["attempts"]
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42",
        counts_as_check=False,
    )
    assert ig_state.list_publish_reconciliations()[0]["attempts"] == before


def test_a_non_check_record_still_advances_the_alert_schedule(monkeypatch):
    """It is still an alert, so it must not re-fire every minute."""
    ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42"
    )
    assert ig_state.record_publish_reconciliation(
        "container_abc", project_name="kitchen_remodel", idempotency_key="42",
        counts_as_check=False,
    ) is None


# ---------------------------------------------------------------------------
# The obligation invariant is enforced at ONE chokepoint, not per mutation site
# ---------------------------------------------------------------------------
#
# Four separate mutation sites have now lost the unresolved-publish obligation
# across successive reviews: the exhausted claim, stale_failed, mark_failed(), and
# set_pending_upload() overwriting a live record. Every one was found by
# enumerating sites, and enumeration kept missing one. So the rule moved into
# _transaction(), which every write must pass through, and these tests assert that
# STRUCTURALLY — against the module source and across every mutation entry point —
# rather than against the sites anyone happened to remember.

def test_overwriting_a_marker_bearing_record_preserves_its_obligation(valid_record):
    """The round-5 reproduction: job B over unresolved job A must not destroy A's marker.

    Overwriting is as much a removal as clearing is. This was the fourth instance of the
    class, and the reason the guarantee stopped being per-site.
    """
    job_a = dict(
        valid_record, container_id="container_A", publish_attempted_at="2026-08-31T14:05:00Z"
    )
    ig_state.set_pending_upload(job_a)
    ig_state.set_pending_upload(dict(valid_record, idempotency_key="99"))

    assert ig_state.get_pending_upload()["idempotency_key"] == "99"
    assert ig_state.has_unresolved_publish("42") is True
    assert [e["container_id"] for e in ig_state.list_publish_reconciliations()] == ["container_A"]


def test_the_overwriting_job_is_accepted_not_refused(valid_record):
    """An unrelated video must not be blocked by another video's unresolved publish.

    Refusing the overwrite was the other option here. It punishes the wrong job: job B has
    no duplicate risk of its own, and quarantining A loses nothing while blocking exactly
    the key that needs blocking.
    """
    ig_state.set_pending_upload(
        dict(valid_record, container_id="container_A", publish_attempted_at="2026-08-31T14:05:00Z")
    )
    ig_state.set_pending_upload(dict(valid_record, idempotency_key="99"))
    assert ig_state.has_unresolved_publish("99") is False


def test_the_overwritten_job_cannot_be_re_approved(valid_record):
    """And the block that matters actually holds."""
    job_a = dict(
        valid_record, container_id="container_A", publish_attempted_at="2026-08-31T14:05:00Z"
    )
    ig_state.set_pending_upload(job_a)
    ig_state.set_pending_upload(dict(valid_record, idempotency_key="99"))
    with pytest.raises(ValueError, match="unresolved publish"):
        ig_state.set_pending_upload(job_a)


def test_overwriting_a_record_with_no_marker_quarantines_nothing(valid_record):
    """No open question, no obligation — ordinary re-queueing stays ordinary."""
    ig_state.set_pending_upload(valid_record)
    ig_state.set_pending_upload(dict(valid_record, idempotency_key="99"))
    assert ig_state.list_publish_reconciliations() == []


# --- structural: every write really does go through the one chokepoint ---

def _module_functions_calling(name):
    """Return the names of module-level functions whose body calls `name`."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ig_state))
    out = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name:
                out.add(node.name)
    return out


def test_the_state_file_is_written_from_exactly_one_place():
    """The structural claim, checked against the source rather than asserted in prose.

    If a future mutator calls _write() directly it bypasses
    _preserve_unresolved_obligation() entirely — silently, and in exactly the way four
    previous ones did. This is the test that makes that impossible to do by accident.
    """
    assert _module_functions_calling("_write") == {"_transaction"}


def test_the_state_file_is_opened_for_writing_from_exactly_one_place():
    """Same reasoning one level down: no mutator may take the exclusive lock on its own."""
    assert _module_functions_calling("_open_for_write") == {"_transaction"}


def test_the_obligation_check_runs_from_exactly_one_place():
    """_quarantine_unresolved_in_txn() is the chokepoint's implementation, not a helper.

    Per-site calls are what enumeration kept getting wrong, so there are none left.
    """
    assert _module_functions_calling("_quarantine_unresolved_in_txn") == {
        "_preserve_unresolved_obligation"
    }


# --- mechanical: the invariant holds across EVERY mutation entry point ---

def _marker_bearing(valid_record):
    return dict(
        valid_record,
        attempt_count=1,
        container_id="container_abc",
        publish_attempted_at="2026-08-31T14:05:00Z",
    )


# Every public function that can change the state file, with a call that exercises it
# against a marker-bearing record for key "42". test_every_mutating_entry_point_is_covered
# below fails if this table ever falls behind the module.
_MUTATORS = {
    "set_pending_upload": lambda r: ig_state.set_pending_upload(dict(r, idempotency_key="99")),
    "claim_pending_upload": lambda r: ig_state.claim_pending_upload(
        "42", cooldown_seconds=0, max_attempts=1, lease_seconds=0
    ),
    "release_claim": lambda r: ig_state.release_claim("42"),
    "clear_pending_upload": lambda r: ig_state.clear_pending_upload("42"),
    "set_container_id": lambda r: ig_state.set_container_id("42", "container_xyz"),
    "mark_published": lambda r: ig_state.mark_published("42", "ig_post_1"),
    "mark_failed": lambda r: ig_state.mark_failed("42"),
    "record_recovered_publish": lambda r: ig_state.record_recovered_publish(
        "42", "kitchen_remodel", "container_abc"
    ),
    "mark_publish_attempted": lambda r: ig_state.mark_publish_attempted("42"),
    "mark_publish_settled": lambda r: ig_state.mark_publish_settled("42"),
    "record_publish_reconciliation": lambda r: ig_state.record_publish_reconciliation(
        "container_other", project_name="other", idempotency_key="99"
    ),
    "clear_publish_reconciliation": lambda r: ig_state.clear_publish_reconciliation("nope"),
    "record_share_intent": lambda r: ig_state.record_share_intent("file_1", "kitchen_remodel"),
    "record_share_cleanup": lambda r: ig_state.record_share_cleanup("file_1", "kitchen_remodel"),
    "clear_share_cleanup": lambda r: ig_state.clear_share_cleanup("file_1"),
}


def test_every_mutating_entry_point_is_covered_by_the_invariant_test():
    """Derives the list of mutators from the MODULE, so the table cannot fall behind.

    The reviewer's instruction was to stop enumerating from memory. This computes which
    public functions can reach _transaction() — directly or through _update_pending() —
    and fails if any of them is missing from _MUTATORS below. A new mutator added without
    a thought for the obligation fails here rather than in production.
    """
    direct = _module_functions_calling("_transaction")
    indirect = _module_functions_calling("_update_pending")
    reaches_write = (direct | indirect) - {"_update_pending"}
    public = {name for name in reaches_write if not name.startswith("_")}
    assert public == set(_MUTATORS), (
        f"missing from _MUTATORS: {sorted(public - set(_MUTATORS))}; "
        f"stale entries: {sorted(set(_MUTATORS) - public)}"
    )


@pytest.mark.parametrize("name", sorted(_MUTATORS))
def test_no_mutation_entry_point_can_destroy_an_unresolved_obligation(name, valid_record):
    """THE invariant, asserted against every mutator rather than the ones I remembered.

    If a call removes or replaces a record that carried an unresolved publish, the
    obligation must survive — either as a quarantine entry, or as a retired idempotency
    key, which answers the question permanently. Nothing may simply make it disappear.
    """
    ig_state.set_pending_upload(_marker_bearing(valid_record))
    _MUTATORS[name](_marker_bearing(valid_record))

    record = ig_state.get_pending_upload()
    # "Still there" has to mean the same OPEN QUESTION is still tracked — same job, same
    # container, publish still open or explicitly settled. Checking only the key was the
    # round-5 hole: set_pending_upload() could replace key 42 (container_A, marker) with a
    # fresh key 42 (no container, no marker) and this test would have waved it through.
    still_tracked = (
        record is not None
        and record.get("idempotency_key") == "42"
        and record.get("container_id") == "container_abc"
        and (record.get("publish_attempted_at") or record.get("publish_settled_at"))
    )
    if still_tracked:
        return
    assert ig_state.has_unresolved_publish("42") or ig_state.is_published("42"), (
        f"{name}() removed, replaced or erased a marker-bearing record and lost its "
        "obligation"
    )


# ---------------------------------------------------------------------------
# Torn or truncated state fails CLOSED
# ---------------------------------------------------------------------------
#
# The transaction above is atomic with respect to other processes — one flock held
# across the whole read-modify-write, one _write() call — but _write() overwrites
# and truncates in place, so it is NOT crash-atomic: a crash mid-write can leave
# torn JSON. What makes that survivable rather than catastrophic is that a torn
# file RAISES on read instead of silently reading as defaults. If it defaulted, a
# torn write would make the quarantine list vanish and a duplicate publish would
# follow. These tests pin that property, because the safety argument rests on it.

_TORN = '{"pending_instagram_upload": {"idempotency_key": "42", "contai'


def _write_torn_state():
    ig_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ig_state.STATE_FILE.write_text(_TORN)


@pytest.mark.parametrize("call", [
    lambda: ig_state.get_pending_upload(),
    lambda: ig_state.is_published("42"),
    lambda: ig_state.find_published("kitchen_remodel"),
    lambda: ig_state.has_outstanding_job("42"),
    lambda: ig_state.has_unresolved_publish("42"),
    lambda: ig_state.list_publish_reconciliations(),
    lambda: ig_state.list_share_cleanups(),
])
def test_every_read_path_raises_on_torn_state(call):
    """No read may silently present an empty quarantine list built from defaults."""
    _write_torn_state()
    with pytest.raises(RuntimeError, match="corrupt"):
        call()


def test_a_write_path_raises_on_torn_state(valid_record):
    """A mutator cannot proceed either — it would overwrite state it could not read."""
    _write_torn_state()
    with pytest.raises(RuntimeError, match="corrupt"):
        ig_state.set_pending_upload(valid_record)


def test_a_torn_file_is_not_silently_replaced(valid_record):
    """Failing closed means leaving the evidence alone for a human, not healing over it."""
    _write_torn_state()
    with pytest.raises(RuntimeError):
        ig_state.mark_failed("42")
    assert ig_state.STATE_FILE.read_text() == _TORN


# --- which torn shapes fail closed, stated one shape at a time ---
#
# This replaces a test that asserted the opposite: that a present-but-empty file is
# "a fresh client, not damage". That was pinning the unsafe behaviour. An ABSENT
# file means a fresh client; a PRESENT ZERO-LENGTH one cannot arise in normal
# operation once _transaction() initialises what it creates, so reading it as fresh
# state would present an empty quarantine and let a duplicate through.

def test_an_absent_state_file_is_fresh_state():
    """The genuinely-fresh case still works — this is the distinction that matters."""
    assert ig_state.STATE_FILE.exists() is False
    assert ig_state.get_pending_upload() is None
    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.has_unresolved_publish("42") is False


def test_a_present_but_zero_length_state_file_fails_closed():
    """The shape the round-5 determination missed."""
    ig_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ig_state.STATE_FILE.write_text("")
    with pytest.raises(RuntimeError, match="present but empty"):
        ig_state.has_unresolved_publish("42")


def test_a_transaction_never_leaves_a_zero_length_file(valid_record):
    """Removes the one legitimate producer of zero-length files.

    _open_for_write()'s O_CREAT used to leave one behind whenever a transaction declined
    to commit — mark_failed() against a fresh client was enough. That is what made "empty
    means fresh" unsafe to assume, and why the next read would have failed closed on a
    file nothing was wrong with.
    """
    ig_state.mark_failed("no-such-key")          # creates the file, commits nothing
    assert ig_state.STATE_FILE.exists()
    assert ig_state.STATE_FILE.stat().st_size > 0
    assert ig_state.get_pending_upload() is None  # and it still reads as fresh state


def test_json_that_is_not_an_object_fails_closed():
    """A list or a bare scalar is not state, however well it parses."""
    ig_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ig_state.STATE_FILE.write_text("[1, 2, 3]")
    with pytest.raises(RuntimeError, match="does not look like state"):
        ig_state.has_unresolved_publish("42")


def test_an_object_with_no_recognised_keys_fails_closed():
    """`{}` parses and would otherwise present an EMPTY quarantine — the exact hazard."""
    ig_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ig_state.STATE_FILE.write_text("{}")
    with pytest.raises(RuntimeError, match="does not look like state"):
        ig_state.has_unresolved_publish("42")


def test_a_state_file_missing_only_newer_keys_still_loads():
    """Forward migration must not be collateral damage from the shape check.

    A file written before pending_publish_reconciliations existed carries the older keys
    and none of the new one. It has to keep working — absent individual keys fall back to
    defaults, as they always have.
    """
    import json
    ig_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ig_state.STATE_FILE.write_text(json.dumps({
        "pending_instagram_upload": None,
        "published_idempotency_keys": ["7"],
        "published_history": [],
    }))
    assert ig_state.is_published("7") is True
    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.has_unresolved_publish("7") is False


def test_a_write_truncates_only_after_writing():
    """Why a torn write yields malformed JSON rather than a plausible one.

    _write() does seek -> write -> truncate, so a process killed before the write leaves
    the previous content wholly intact and one killed mid-write leaves new-prefix +
    old-tail, which does not parse. It can never shrink a populated file to zero. That
    ordering is load-bearing for the fail-closed argument, so it is pinned here rather
    than left as a comment.
    """
    import inspect
    body = inspect.getsource(ig_state._write)
    assert body.index("seek(0)") < body.index("write(content)") < body.index("truncate()")


# ---------------------------------------------------------------------------
# The chokepoint, over a GENERATED matrix of transitions
# ---------------------------------------------------------------------------
#
# Round 5's guards were derived — the mutator table came from the module, the
# write-path check came from the AST — but the SCENARIOS were still hand-chosen,
# and every hand-chosen set_pending_upload case replaced key 42 with key 99. So
# the different-key path was covered and the same-key path was not, and the
# derivation did not save us.
#
# This enumerates the input space instead of sampling it: every combination of
# what the transaction found and what it is leaving behind. A shape nobody thought
# of is covered because it is generated, not because it was remembered.

_OPEN = {"container_id": "container_A", "publish_attempted_at": "2026-08-31T14:05:00Z"}


def _rec(key="42", container=..., attempted=None, settled=None):
    r = {"idempotency_key": key}
    if container is not ...:
        r["container_id"] = container
    if attempted:
        r["publish_attempted_at"] = attempted
    if settled:
        r["publish_settled_at"] = settled
    return r


# What the transaction FOUND in the pending slot.
_INCOMING = {
    "open_publish": _rec(container="container_A", attempted="T1"),
    "container_but_no_attempt": _rec(container="container_A"),
    "attempt_but_no_container": _rec(container=None, attempted="T1"),
    "bare": _rec(container=None),
    "absent": None,
}

# What the transaction is LEAVING in the pending slot.
_OUTGOING = {
    "removed": None,
    "same_job_still_open": _rec(container="container_A", attempted="T1"),
    "same_job_settled": _rec(container="container_A", settled="T2"),
    "same_job_marker_erased": _rec(container="container_A"),
    "same_key_container_dropped": _rec(container=None),
    "same_key_different_container": _rec(container="container_B"),
    "different_key": _rec(key="99", container=None),
    "different_key_own_open_publish": _rec(key="99", container="container_Z", attempted="T1"),
}


def _obligation_was_answered(incoming, outgoing, published):
    """Domain oracle: was the question the incoming record asked actually ANSWERED?

    Three answers exist, and nothing else counts. The publish became a recorded fact;
    Instagram reported that specific container as never published (a settlement, stamped
    on the record); or the record still carries that same container with the question
    still open, so nothing has left and there is nothing yet to preserve.
    """
    if published:
        return True
    if outgoing is None:
        return False
    if outgoing.get("idempotency_key") != incoming.get("idempotency_key"):
        return False
    if outgoing.get("container_id") != incoming.get("container_id"):
        return False
    return bool(outgoing.get("publish_attempted_at") or outgoing.get("publish_settled_at"))


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("out_name", sorted(_OUTGOING))
@pytest.mark.parametrize("in_name", sorted(_INCOMING))
def test_the_chokepoint_preserves_every_unanswered_obligation(in_name, out_name, published):
    """Across the whole generated matrix: an unanswered obligation is never lost."""
    import copy as _copy
    incoming = _copy.deepcopy(_INCOMING[in_name])
    data = {
        "pending_instagram_upload": _copy.deepcopy(_OUTGOING[out_name]),
        "published_idempotency_keys": ["42"] if published else [],
        "pending_publish_reconciliations": [],
    }

    ig_state._preserve_unresolved_obligation(incoming, data)
    quarantined = [e["container_id"] for e in data["pending_publish_reconciliations"]]

    had_open_question = bool(
        incoming
        and incoming.get("container_id")
        and incoming.get("publish_attempted_at")
    )
    if not had_open_question:
        assert quarantined == [], f"{in_name}->{out_name}: quarantined with no open question"
        return
    if _obligation_was_answered(incoming, data["pending_instagram_upload"], published):
        assert quarantined == [], f"{in_name}->{out_name}: quarantined an answered question"
    else:
        assert quarantined == ["container_A"], (
            f"{in_name}->{out_name} (published={published}): the open question about "
            "container_A was lost"
        )


def test_the_matrix_actually_contains_the_shapes_that_were_missed():
    """Guards the generator itself: coverage claims are worthless if the cases are absent.

    Both historic escapes must be in the matrix by construction — the round-4 different-key
    replacement, and the round-5 same-key replacement that dropped the container.
    """
    assert _OUTGOING["different_key"]["idempotency_key"] != "42"
    same_key_erasures = [
        name for name, rec in _OUTGOING.items()
        if rec is not None
        and rec.get("idempotency_key") == "42"
        and not (rec.get("publish_attempted_at") or rec.get("publish_settled_at"))
    ]
    assert "same_key_container_dropped" in same_key_erasures
    assert "same_job_marker_erased" in same_key_erasures


def test_a_settlement_is_distinguishable_from_an_erasure():
    """The distinction the same-key fix rests on, asserted directly.

    Both leave a same-key, same-container record with no open marker. Only one of them
    recorded an answer, and only the other may be quarantined.
    """
    import copy as _copy
    for out_name, expect_quarantine in (("same_job_settled", False),
                                        ("same_job_marker_erased", True)):
        data = {
            "pending_instagram_upload": _copy.deepcopy(_OUTGOING[out_name]),
            "published_idempotency_keys": [],
            "pending_publish_reconciliations": [],
        }
        ig_state._preserve_unresolved_obligation(_copy.deepcopy(_INCOMING["open_publish"]), data)
        assert bool(data["pending_publish_reconciliations"]) is expect_quarantine, out_name
