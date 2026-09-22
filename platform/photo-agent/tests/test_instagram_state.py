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
