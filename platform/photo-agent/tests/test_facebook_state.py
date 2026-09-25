"""
Tests for tools/facebook_state.py — the Facebook upload state manager.

Covers: set_pending_upload (success, missing keys, idempotency check),
get_pending_upload, claim_pending_upload (including the concurrent-claim
regression for issue #34's check/use race), clear_pending_upload
(compare-and-clear, including the exact stale-clear-destroys-a-newer-job
regression), mark_published, find_published, mark_failed, is_published,
fcntl exclusive locking, and FIELDKIT_DATA_DIR env override.
"""

import json
import threading

import pytest

import tools.facebook_state as fb_state


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    """Redirect DATA_DIR and STATE_FILE to an isolated tmp directory."""
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")


@pytest.fixture
def valid_record():
    """Return a fresh VideoUploadJob record for each test."""
    return {
        "project_name": "kitchen_remodel",
        "video_local_path": "/tmp/kitchen_remodel/video.mp4",
        "page_id": "123456789",
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": None,
        "triggered_at": "2026-05-30T14:00:00Z",
        "idempotency_key": "42",
        "fb_post_id": None,
    }


# --- get_pending_upload ---

def test_get_pending_upload_returns_none_when_file_missing():
    """get_pending_upload() returns None when facebook_state.json does not exist."""
    assert not fb_state.STATE_FILE.exists()
    assert fb_state.get_pending_upload() is None


def test_get_pending_upload_returns_none_when_null():
    """get_pending_upload() returns None when pending_facebook_upload is null."""
    fb_state.STATE_FILE.write_text(
        json.dumps({"pending_facebook_upload": None, "published_idempotency_keys": []})
    )
    assert fb_state.get_pending_upload() is None


def test_get_pending_upload_returns_stored_record(valid_record):
    """get_pending_upload() returns the exact record that was stored."""
    fb_state.STATE_FILE.write_text(
        json.dumps({"pending_facebook_upload": valid_record, "published_idempotency_keys": []})
    )
    assert fb_state.get_pending_upload() == valid_record


# --- set_pending_upload ---

def test_set_pending_upload_writes_record(valid_record):
    """set_pending_upload() writes the record and it is readable via get_pending_upload()."""
    fb_state.set_pending_upload(valid_record)
    assert fb_state.get_pending_upload() == valid_record


def test_set_pending_upload_missing_key_raises(valid_record):
    """set_pending_upload() raises ValueError when a required key is missing."""
    del valid_record["project_name"]
    with pytest.raises(ValueError, match="missing required keys"):
        fb_state.set_pending_upload(valid_record)


def test_set_pending_upload_all_required_keys():
    """set_pending_upload() raises ValueError if ANY required key is absent."""
    required_keys = [
        "project_name", "video_local_path", "page_id", "status",
        "attempt_count", "last_attempt_at", "triggered_at", "idempotency_key", "fb_post_id",
    ]
    base_record = {
        "project_name": "proj",
        "video_local_path": "/tmp/v.mp4",
        "page_id": "111",
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": None,
        "triggered_at": "2026-05-30T00:00:00Z",
        "idempotency_key": "1",
        "fb_post_id": None,
    }
    for key in required_keys:
        partial = {k: v for k, v in base_record.items() if k != key}
        with pytest.raises(ValueError):
            fb_state.set_pending_upload(partial)


def test_set_pending_upload_idempotency_skip_raises(valid_record):
    """set_pending_upload() raises ValueError if idempotency_key is already in published_idempotency_keys."""
    fb_state.STATE_FILE.write_text(
        json.dumps({
            "pending_facebook_upload": None,
            "published_idempotency_keys": ["42"],
        })
    )
    with pytest.raises(ValueError, match="idempotency_key"):
        fb_state.set_pending_upload(valid_record)


def test_set_pending_upload_preserves_published_keys(valid_record):
    """set_pending_upload() does not clear existing published_idempotency_keys."""
    fb_state.STATE_FILE.write_text(
        json.dumps({
            "pending_facebook_upload": None,
            "published_idempotency_keys": ["99", "100"],
        })
    )
    fb_state.set_pending_upload(valid_record)
    data = json.loads(fb_state.STATE_FILE.read_text())
    assert "99" in data["published_idempotency_keys"]
    assert "100" in data["published_idempotency_keys"]


# --- claim_pending_upload ---

def test_claim_pending_upload_claims_a_fresh_job(valid_record):
    """claim_pending_upload() transitions a fresh job to 'uploading' and advances
    attempt_count/last_attempt_at, all as part of the single 'claimed' transaction."""
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "claimed"
    record = fb_state.get_pending_upload()
    assert record["status"] == "uploading"
    assert record["attempt_count"] == 1
    assert record["last_attempt_at"] is not None
    from datetime import datetime
    datetime.fromisoformat(record["last_attempt_at"])


def test_claim_pending_upload_preserves_other_fields(valid_record):
    """claim_pending_upload() does not alter fields other than status/attempt_count/last_attempt_at."""
    fb_state.set_pending_upload(valid_record)
    fb_state.claim_pending_upload(valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900)
    record = fb_state.get_pending_upload()
    assert record["project_name"] == valid_record["project_name"]
    assert record["idempotency_key"] == valid_record["idempotency_key"]


def test_claim_pending_upload_mismatch_when_nothing_pending():
    """claim_pending_upload() returns 'mismatch' when there is no pending record at all."""
    assert fb_state.claim_pending_upload("some_key", cooldown_seconds=60, max_attempts=3, lease_seconds=900) == "mismatch"


def test_claim_pending_upload_mismatch_when_key_differs(valid_record):
    """claim_pending_upload() returns 'mismatch' (and does not touch state) if the current
    pending record's idempotency_key differs from the one the caller expects — e.g. the caller's
    snapshot is stale because the job already resolved and a different job was enqueued."""
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload("some_other_key", cooldown_seconds=60, max_attempts=3, lease_seconds=900)
    assert result == "mismatch"
    assert fb_state.get_pending_upload() == valid_record


def test_claim_pending_upload_in_flight_when_already_uploading(valid_record):
    """claim_pending_upload() declines ('in_flight') a job whose status is already 'uploading'
    and whose lease hasn't expired — this is the actual fix for issue #34's check/use race: a
    second overlapping invocation must not be able to claim a job another still-running
    invocation already claimed."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    valid_record["status"] = "uploading"
    valid_record["last_attempt_at"] = recent
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "in_flight"
    record = fb_state.get_pending_upload()
    assert record["attempt_count"] == 0  # untouched — not double-counted


def test_claim_pending_upload_reclaims_after_lease_expires(valid_record):
    """A claim stuck at status='uploading' (e.g. the claiming process crashed without calling
    release_claim()/mark_published()/mark_failed()) becomes reclaimable once lease_seconds has
    elapsed — otherwise a crashed process would wedge the job at 'in_flight' forever, never
    retried and never alerting anyone. Reclaiming is still gated by the normal attempt budget.
    """
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    valid_record["status"] = "uploading"
    valid_record["attempt_count"] = 1
    valid_record["last_attempt_at"] = old
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "claimed"
    record = fb_state.get_pending_upload()
    assert record["attempt_count"] == 2


def test_claim_pending_upload_lease_expired_but_exhausted_clears(valid_record):
    """A crashed claim whose lease has expired AND whose attempt_count is already at the cap is
    cleared as exhausted, not reclaimed — the lease only re-opens the normal attempt-budget path,
    it doesn't grant extra retries."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    valid_record["status"] = "uploading"
    valid_record["attempt_count"] = 3
    valid_record["last_attempt_at"] = old
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "exhausted"
    assert fb_state.get_pending_upload() is None


# --- release_claim ---

def test_release_claim_resets_status_to_pending(valid_record):
    """release_claim() resets status back to 'pending' after a claim, without touching
    attempt_count/last_attempt_at, so the next claim_pending_upload() call is gated by the
    short cooldown rather than the long abandoned-claim lease."""
    fb_state.set_pending_upload(valid_record)
    fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    fb_state.release_claim(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["status"] == "pending"
    assert record["attempt_count"] == 1


def test_release_claim_does_not_destroy_a_newer_job(valid_record):
    """release_claim() is compare-and-update: a stale caller must not reset a DIFFERENT, newer
    job's status."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_A")

    job_b = dict(valid_record, project_name="job_b", idempotency_key="99")
    fb_state.set_pending_upload(job_b)

    fb_state.release_claim(valid_record["idempotency_key"])  # stale caller, A's key

    assert fb_state.get_pending_upload() == job_b


def test_claim_pending_upload_concurrent_calls_only_one_claims(valid_record):
    """Two overlapping claim_pending_upload() calls for the SAME job must not both succeed.

    This directly reproduces the issue #34 follow-up finding: two overlapping cron invocations
    (e.g. a slow upload still running when the next minute's tick starts) could otherwise both
    observe an unclaimed job and both call the Facebook API — a real duplicate post. The fix is
    that the read + staleness check + status transition all happen under ONE exclusive-lock
    acquisition, so the second concurrent caller's read-modify-write sees the FIRST caller's
    already-'uploading' status, not a stale earlier snapshot.
    """
    fb_state.set_pending_upload(valid_record)
    results = []
    barrier = threading.Barrier(2)

    def do_claim():
        barrier.wait()
        results.append(fb_state.claim_pending_upload(
            valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
        ))

    threads = [threading.Thread(target=do_claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["claimed", "in_flight"]
    record = fb_state.get_pending_upload()
    assert record["attempt_count"] == 1  # only the winning claim advanced it


def test_claim_pending_upload_cooldown_blocks_recent_attempt(valid_record):
    """claim_pending_upload() declines ('cooldown') and does not touch state if the last attempt
    was within cooldown_seconds."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    valid_record["attempt_count"] = 1
    valid_record["last_attempt_at"] = recent
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "cooldown"
    assert fb_state.get_pending_upload()["attempt_count"] == 1


def test_claim_pending_upload_proceeds_after_cooldown_elapsed(valid_record):
    """claim_pending_upload() claims successfully once cooldown_seconds has elapsed."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    valid_record["attempt_count"] = 1
    valid_record["last_attempt_at"] = old
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "claimed"
    assert fb_state.get_pending_upload()["attempt_count"] == 2


def test_claim_pending_upload_unparseable_last_attempt_at_proceeds(valid_record):
    """claim_pending_upload() treats an unparseable last_attempt_at as if cooldown had elapsed."""
    valid_record["attempt_count"] = 1
    valid_record["last_attempt_at"] = "not-a-real-timestamp"
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "claimed"


def test_claim_pending_upload_stale_published_clears(valid_record):
    """claim_pending_upload() returns 'stale_published' and clears the pending slot if the
    idempotency_key is already in published_idempotency_keys — self-healing a stale/pre-fix
    state file instead of ever calling the Facebook API again for it."""
    fb_state.STATE_FILE.write_text(json.dumps({
        "pending_facebook_upload": valid_record,
        "published_idempotency_keys": [valid_record["idempotency_key"]],
    }))
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "stale_published"
    assert fb_state.get_pending_upload() is None


def test_claim_pending_upload_stale_failed_clears(valid_record):
    """claim_pending_upload() returns 'stale_failed' and clears the pending slot if status is
    already 'failed'."""
    valid_record["status"] = "failed"
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "stale_failed"
    assert fb_state.get_pending_upload() is None


def test_claim_pending_upload_exhausted_clears(valid_record):
    """claim_pending_upload() returns 'exhausted' and clears the pending slot if attempt_count
    already reached max_attempts — no further claim is ever handed out for it."""
    valid_record["attempt_count"] = 3
    fb_state.set_pending_upload(valid_record)
    result = fb_state.claim_pending_upload(
        valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert result == "exhausted"
    assert fb_state.get_pending_upload() is None


# --- clear_pending_upload ---

def test_clear_pending_upload_clears_when_key_matches(valid_record):
    """clear_pending_upload() clears the pending slot when the expected key still matches."""
    fb_state.set_pending_upload(valid_record)
    cleared = fb_state.clear_pending_upload(valid_record["idempotency_key"])
    assert cleared is True
    assert fb_state.get_pending_upload() is None


def test_clear_pending_upload_noop_when_nothing_pending():
    """clear_pending_upload() returns False and does nothing when there is no pending record."""
    assert fb_state.clear_pending_upload("some_key") is False


def test_clear_pending_upload_does_not_destroy_a_newer_job(valid_record):
    """Compare-and-clear regression (issue #34 follow-up): a caller holding an earlier
    snapshot's idempotency_key must not be able to destroy a DIFFERENT, newer job that's since
    taken the pending slot's place.

    Reproduces the exact interleaving: job A resolves (mark_published clears pending), a new
    job B is enqueued, and only THEN does a stale caller try to clear using A's key.
    """
    fb_state.set_pending_upload(valid_record)  # job A
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_A")  # pending now null

    job_b = dict(valid_record, project_name="job_b", idempotency_key="99")
    fb_state.set_pending_upload(job_b)  # a NEW job B takes the pending slot

    cleared = fb_state.clear_pending_upload(valid_record["idempotency_key"])  # stale caller, A's key

    assert cleared is False
    assert fb_state.get_pending_upload() == job_b  # B survives untouched


# --- mark_published ---

def test_mark_published_clears_pending_upload(valid_record):
    """mark_published() clears pending_facebook_upload back to null (issue #34 regression).

    Before this fix, the pending record was never cleared after a successful
    publish, so get_pending_upload() kept returning the same resolved job on
    every subsequent cron tick — see test_upload_facebook.py's
    test_reprocessing_after_publish_does_not_call_upload_video for the
    end-to-end reprocessing-loop regression this closes.
    """
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_999")
    assert fb_state.get_pending_upload() is None


def test_mark_published_adds_key_to_published_list(valid_record):
    """mark_published() appends idempotency_key to published_idempotency_keys."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_999")
    data = json.loads(fb_state.STATE_FILE.read_text())
    assert valid_record["idempotency_key"] in data["published_idempotency_keys"]


def test_mark_published_appends_to_existing_keys(valid_record):
    """mark_published() appends rather than replacing existing published keys."""
    fb_state.STATE_FILE.write_text(
        json.dumps({
            "pending_facebook_upload": valid_record,
            "published_idempotency_keys": ["old_key"],
        })
    )
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_999")
    data = json.loads(fb_state.STATE_FILE.read_text())
    assert "old_key" in data["published_idempotency_keys"]
    assert valid_record["idempotency_key"] in data["published_idempotency_keys"]


# --- find_published ---

def test_find_published_returns_none_when_nothing_published():
    """find_published() returns None when no job has ever been published."""
    assert fb_state.find_published("some_project") is None


def test_find_published_returns_project_and_post_id(valid_record):
    """find_published() reflects a mark_published() call for that project_name.

    Callers that need to observe a publish's outcome (e.g. the e2e test rig's
    Stage 5, scripts/e2e_stage5_await_facebook.py) must use this instead of
    polling get_pending_upload(), since mark_published() clears the pending
    record as soon as the job resolves (issue #34).
    """
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_999")
    found = fb_state.find_published(valid_record["project_name"])
    assert found["project_name"] == valid_record["project_name"]
    assert found["idempotency_key"] == valid_record["idempotency_key"]
    assert found["fb_post_id"] == "fb_post_999"


def test_find_published_ignores_a_different_projects_publish(valid_record):
    """find_published() only matches its own project_name, not whatever published most recently.

    A single overwritable 'last published' slot would let an unrelated publish landing in
    between hide an earlier one a caller is still polling for — published_history is a capped
    list precisely so find_published() can search by project_name instead.
    """
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_first")

    second_record = dict(valid_record, project_name="second_project", idempotency_key="99")
    fb_state.set_pending_upload(second_record)
    fb_state.mark_published("99", "fb_post_second")

    found = fb_state.find_published(valid_record["project_name"])
    assert found["fb_post_id"] == "fb_post_first"
    assert fb_state.find_published("second_project")["fb_post_id"] == "fb_post_second"
    assert fb_state.find_published("no_such_project") is None


def test_mark_published_caps_published_history(valid_record):
    """mark_published() trims published_history to _PUBLISH_HISTORY_LIMIT entries."""
    for i in range(fb_state._PUBLISH_HISTORY_LIMIT + 5):
        record = dict(valid_record, project_name=f"project_{i}", idempotency_key=str(i))
        fb_state.set_pending_upload(record)
        fb_state.mark_published(str(i), f"fb_post_{i}")

    data = json.loads(fb_state.STATE_FILE.read_text())
    assert len(data["published_history"]) == fb_state._PUBLISH_HISTORY_LIMIT
    # oldest entries were trimmed; the most recent ones survive
    assert fb_state.find_published("project_0") is None
    assert fb_state.find_published(f"project_{fb_state._PUBLISH_HISTORY_LIMIT + 4}") is not None


# --- mark_failed ---

def test_mark_failed_clears_pending_upload(valid_record):
    """mark_failed() clears pending_facebook_upload back to null (issue #34 regression).

    Every call site treats mark_failed as terminal (no further retries follow
    it). Before this fix, a terminally-failed job kept being reprocessed by
    the cron entrypoint forever with no backoff.
    """
    valid_record["attempt_count"] = 2
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_failed(valid_record["idempotency_key"])
    assert fb_state.get_pending_upload() is None


def test_mark_failed_does_not_destroy_a_newer_job(valid_record):
    """mark_failed() is also compare-and-update (via _update_pending): a stale caller acting on
    an old idempotency_key must not clear a different, newer job enqueued in its place."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_A")

    job_b = dict(valid_record, project_name="job_b", idempotency_key="99")
    fb_state.set_pending_upload(job_b)

    fb_state.mark_failed(valid_record["idempotency_key"])  # stale caller, A's key

    assert fb_state.get_pending_upload() == job_b


# --- is_published ---

def test_is_published_false_when_file_missing():
    """is_published() returns False when facebook_state.json does not exist."""
    assert fb_state.is_published("some_key") is False


def test_is_published_false_when_key_absent():
    """is_published() returns False when the key is not in published_idempotency_keys."""
    fb_state.STATE_FILE.write_text(
        json.dumps({
            "pending_facebook_upload": None,
            "published_idempotency_keys": ["42"],
        })
    )
    assert fb_state.is_published("99") is False


def test_is_published_true_when_key_present():
    """is_published() returns True when the key is in published_idempotency_keys."""
    fb_state.STATE_FILE.write_text(
        json.dumps({
            "pending_facebook_upload": None,
            "published_idempotency_keys": ["42", "99"],
        })
    )
    assert fb_state.is_published("42") is True


def test_is_published_true_after_mark_published(valid_record):
    """is_published() returns True for a key that was marked published via mark_published()."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_1")
    assert fb_state.is_published(valid_record["idempotency_key"]) is True


# --- fcntl exclusive locking ---

def test_concurrent_read_write_does_not_corrupt(valid_record):
    """Concurrent get_pending_upload and claim_pending_upload leave the state file valid."""
    fb_state.set_pending_upload(valid_record)
    errors = []

    def do_read():
        try:
            fb_state.get_pending_upload()
        except Exception as exc:
            errors.append(exc)

    def do_write():
        try:
            fb_state.claim_pending_upload(
                valid_record["idempotency_key"], cooldown_seconds=60, max_attempts=3, lease_seconds=900
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=do_read), threading.Thread(target=do_write)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    data = json.loads(fb_state.STATE_FILE.read_text())
    assert "pending_facebook_upload" in data


# --- FIELDKIT_DATA_DIR env override ---

def test_data_dir_env_override_writes_to_alt_path(tmp_path, monkeypatch, valid_record):
    """STATE_FILE respects the DATA_DIR override (simulating FIELDKIT_DATA_DIR)."""
    alt_dir = tmp_path / "alt_data" / "photo-agent"
    alt_dir.mkdir(parents=True)
    alt_state = alt_dir / "facebook_state.json"
    monkeypatch.setattr(fb_state, "DATA_DIR", alt_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", alt_state)
    fb_state.set_pending_upload(valid_record)
    assert alt_state.exists()
    assert json.loads(alt_state.read_text())["pending_facebook_upload"] == valid_record


# ---------------------------------------------------------------------------
# has_outstanding_job — cross-platform cleanup coordination (Feature 005)
# ---------------------------------------------------------------------------

def test_has_outstanding_job_true_while_pending(valid_record):
    """A freshly enqueued job is outstanding."""
    fb_state.set_pending_upload(valid_record)
    assert fb_state.has_outstanding_job("42") is True


def test_has_outstanding_job_true_while_claimed(valid_record):
    """A job mid-upload is still outstanding — the other platform must wait."""
    fb_state.set_pending_upload(valid_record)
    fb_state.claim_pending_upload(
        "42", cooldown_seconds=60, max_attempts=3, lease_seconds=900
    )
    assert fb_state.has_outstanding_job("42") is True


def test_has_outstanding_job_false_when_nothing_enqueued():
    """A key that was never enqueued is not outstanding."""
    assert fb_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_after_published(valid_record):
    """Publishing resolves the job."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published("42", "post_1")
    assert fb_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_after_failed(valid_record):
    """A terminal failure resolves the job just as much as a publish does."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_failed("42")
    assert fb_state.has_outstanding_job("42") is False


def test_has_outstanding_job_false_for_a_different_key(valid_record):
    """A pending job under another key says nothing about this one."""
    fb_state.set_pending_upload(valid_record)
    assert fb_state.has_outstanding_job("999") is False


# ---------------------------------------------------------------------------
# Torn or truncated state fails CLOSED (mirrors tools/instagram_state.py)
# ---------------------------------------------------------------------------
#
# Applied here because facebook_state.py shares instagram_state.py's in-place write
# pattern, and the same misreading applies: an ABSENT file means a fresh client, but
# a PRESENT ZERO-LENGTH one cannot arise in normal operation. Reading one as fresh
# state would silently discard published_idempotency_keys — the list that stops a
# re-approval from posting the same video to the Page twice.

def test_an_absent_state_file_is_fresh_state():
    """The genuinely-fresh case still works."""
    assert fb_state.STATE_FILE.exists() is False
    assert fb_state.get_pending_upload() is None
    assert fb_state.is_published("42") is False


def test_a_present_but_zero_length_state_file_fails_closed():
    fb_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fb_state.STATE_FILE.write_text("")
    with pytest.raises(RuntimeError, match="present but empty"):
        fb_state.is_published("42")


def test_malformed_json_fails_closed():
    fb_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fb_state.STATE_FILE.write_text('{"pending_facebook_upload": {"idem')
    with pytest.raises(RuntimeError, match="corrupt"):
        fb_state.is_published("42")


def test_an_object_with_no_recognised_keys_fails_closed():
    """`{}` parses and would otherwise present an empty published-keys list."""
    fb_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fb_state.STATE_FILE.write_text("{}")
    with pytest.raises(RuntimeError, match="does not look like state"):
        fb_state.is_published("42")


def test_a_state_file_missing_only_newer_keys_still_loads():
    """Forward migration is not collateral damage from the shape check."""
    fb_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fb_state.STATE_FILE.write_text(json.dumps({"published_idempotency_keys": ["7"]}))
    assert fb_state.is_published("7") is True
    assert fb_state.get_pending_upload() is None


def test_a_declining_writer_never_leaves_a_zero_length_file():
    """The one legitimate producer of zero-length files, removed here too.

    _open_for_write()'s O_CREAT used to leave one whenever a caller took the lock and
    then decided not to write — mark_failed() against a fresh client is enough.
    """
    fb_state.mark_failed("no-such-key")
    assert fb_state.STATE_FILE.exists()
    assert fb_state.STATE_FILE.stat().st_size > 0
    assert fb_state.get_pending_upload() is None


# ---------------------------------------------------------------------------
# Issue #78 — sessionized-upload handle, publish marker, and quarantine
# ---------------------------------------------------------------------------

_VID = "vid_1"


def _attempted(valid_record, video_id=_VID):
    """Enqueue valid_record, claim it, record a session and a publish attempt."""
    fb_state.set_pending_upload(valid_record)
    key = valid_record["idempotency_key"]
    assert fb_state.claim_pending_upload(
        key, cooldown_seconds=0, max_attempts=3, lease_seconds=900
    ) == "claimed"
    fb_state.set_upload_session(key, video_id, "sess_1")
    fb_state.mark_publish_attempted(key, video_id)
    return key


def _state():
    return json.loads(fb_state.STATE_FILE.read_text())


def test_write_is_refused_outside_a_transaction(tmp_path):
    """The chokepoint is enforced at runtime, not by convention."""
    with open(tmp_path / "x.json", "w+") as f:
        with pytest.raises(RuntimeError, match="outside _transaction"):
            fb_state._write(f, {})
    with pytest.raises(RuntimeError, match="outside _transaction"):
        fb_state._open_for_write()
    aliased = fb_state._write
    with open(tmp_path / "y.json", "w+") as f:
        with pytest.raises(RuntimeError):
            aliased(f, {}, object())


@pytest.mark.parametrize("field", ["publish_attempted_at", "publish_settled_at"])
def test_set_pending_upload_refuses_provenance_fields(valid_record, field):
    """A caller cannot forge what Meta did."""
    with pytest.raises(ValueError, match="may not be supplied"):
        fb_state.set_pending_upload(dict(valid_record, **{field: "2026-01-01T00:00:00+00:00"}))


def test_set_upload_session_records_handle_and_clears_marker(valid_record):
    key = _attempted(valid_record, "vid_old")
    fb_state.mark_publish_settled(key, "vid_old", "video_status_error")
    fb_state.set_upload_session(key, "vid_new", "sess_2")
    rec = fb_state.get_pending_upload()
    assert rec["video_id"] == "vid_new"
    assert rec["upload_session_id"] == "sess_2"
    assert rec["publish_attempted_at"] is None
    assert rec["publish_settled_at"] is None


def test_set_upload_session_raises_without_matching_record():
    with pytest.raises(RuntimeError, match="no pending record"):
        fb_state.set_upload_session("42", _VID, "s")


def test_mark_publish_attempted_raises_when_not_written(valid_record):
    """If the marker can't be written, the caller must not send FINISH."""
    with pytest.raises(RuntimeError, match="refusing to let FINISH proceed"):
        fb_state.mark_publish_attempted("42", _VID)
    fb_state.set_pending_upload(valid_record)
    fb_state.set_upload_session("42", _VID, "s")
    with pytest.raises(RuntimeError, match="refusing to let FINISH proceed"):
        fb_state.mark_publish_attempted("42", "some_other_video")
    assert fb_state.get_pending_upload()["publish_attempted_at"] is None


def test_mark_failed_with_open_publish_quarantines_and_blocks_the_key(valid_record):
    """The terminal-failure half of issue #78: the record goes, the obligation stays."""
    key = _attempted(valid_record)
    fb_state.mark_failed(key)

    assert fb_state.get_pending_upload() is None
    entries = fb_state.list_publish_reconciliations()
    assert [(e["video_id"], e["idempotency_key"]) for e in entries] == [(_VID, key)]
    assert fb_state.has_unresolved_publish(key) is True
    with pytest.raises(ValueError, match="unresolved publish"):
        fb_state.set_pending_upload(valid_record)


def test_mark_failed_without_publish_attempt_does_not_quarantine(valid_record):
    """A session that never reached FINISH cannot have published."""
    fb_state.set_pending_upload(valid_record)
    fb_state.set_upload_session("42", _VID, "s")
    fb_state.mark_failed("42")
    assert fb_state.list_publish_reconciliations() == []
    fb_state.set_pending_upload(valid_record)  # re-approval allowed


def test_exhausted_claim_quarantines_in_the_same_write(valid_record):
    key = _attempted(valid_record)
    data = _state()
    data["pending_facebook_upload"]["attempt_count"] = 3
    data["pending_facebook_upload"]["status"] = "pending"
    fb_state.STATE_FILE.write_text(json.dumps(data))

    assert fb_state.claim_pending_upload(
        key, cooldown_seconds=0, max_attempts=3, lease_seconds=0
    ) == "exhausted"
    after = _state()
    assert after["pending_facebook_upload"] is None
    assert [e["video_id"] for e in after["pending_publish_reconciliations"]] == [_VID]


def test_stale_failed_claim_quarantines(valid_record):
    key = _attempted(valid_record)
    data = _state()
    data["pending_facebook_upload"]["status"] = "failed"
    fb_state.STATE_FILE.write_text(json.dumps(data))
    assert fb_state.claim_pending_upload(
        key, cooldown_seconds=0, max_attempts=3, lease_seconds=0
    ) == "stale_failed"
    assert fb_state.has_unresolved_publish(key)


def test_same_key_replacement_does_not_erase_the_obligation(valid_record):
    """Replacing a marker-bearing record with a fresh one under the SAME key quarantines
    the old video — a key comparison alone would have let it through."""
    key = _attempted(valid_record)
    fb_state.set_pending_upload(dict(valid_record))  # fresh record, same key, no video_id
    assert fb_state.has_unresolved_publish(key)
    assert [e["video_id"] for e in fb_state.list_publish_reconciliations()] == [_VID]


def test_new_session_over_an_open_publish_quarantines_the_old_video(valid_record):
    key = _attempted(valid_record, "vid_old")
    fb_state.set_upload_session(key, "vid_new", "sess_2")
    assert [e["video_id"] for e in fb_state.list_publish_reconciliations()] == ["vid_old"]


def test_quarantine_survives_terminal_failure_and_same_key_replacement(valid_record):
    """Once quarantined, neither another failure nor another enqueue can lift it."""
    key = _attempted(valid_record)
    fb_state.mark_failed(key)
    for _ in range(3):
        with pytest.raises(ValueError):
            fb_state.set_pending_upload(dict(valid_record))
        fb_state.mark_failed(key)
        fb_state.clear_pending_upload(key)
    assert fb_state.has_unresolved_publish(key)


def test_settled_publish_is_not_quarantined(valid_record):
    key = _attempted(valid_record)
    fb_state.mark_publish_settled(key, _VID, "video_status_error")
    fb_state.mark_failed(key)
    assert fb_state.list_publish_reconciliations() == []


@pytest.mark.parametrize("observed", ["publish_status_published", "processing", "", "recent_video_match"])
def test_mark_publish_settled_requires_a_not_published_observation(valid_record, observed):
    key = _attempted(valid_record)
    with pytest.raises(ValueError):
        fb_state.mark_publish_settled(key, _VID, observed)
    assert fb_state.get_pending_upload()["publish_attempted_at"] is not None


def test_mark_publish_settled_refuses_a_different_video(valid_record):
    key = _attempted(valid_record)
    with pytest.raises(ValueError, match="holds"):
        fb_state.mark_publish_settled(key, "other", "video_status_error")
    assert fb_state.get_pending_upload()["publish_attempted_at"] is not None


def test_stripping_the_marker_is_quarantined_not_accepted(valid_record):
    """An in-place erasure (marker gone, nothing settled) is treated as removal."""
    key = _attempted(valid_record)

    def _strip(record, data):
        record["publish_attempted_at"] = None
    fb_state._update_pending(key, _strip)
    assert fb_state.has_unresolved_publish(key)


def test_mark_published_retires_the_quarantine(valid_record):
    key = _attempted(valid_record)
    with fb_state._transaction() as txn:  # an entry already exists for the key
        fb_state._add_publish_reconciliation(
            txn.data, video_id="vid_x", project_name="p", idempotency_key=key, now="t"
        )
        txn.commit()
    fb_state.mark_published(key, _VID)
    assert fb_state.list_publish_reconciliations() == []
    assert fb_state.is_published(key)


def test_record_recovered_publish_requires_the_published_observation(valid_record):
    key = _attempted(valid_record)
    fb_state.mark_failed(key)
    with pytest.raises(ValueError):
        fb_state.record_recovered_publish(key, "kitchen_remodel", _VID, "video_status_error")
    assert not fb_state.is_published(key)

    fb_state.record_recovered_publish(key, "kitchen_remodel", _VID, "publish_status_published")
    assert fb_state.is_published(key)
    assert fb_state.list_publish_reconciliations() == []
    entry = fb_state.find_published("kitchen_remodel")
    assert entry["fb_post_id"] == _VID and entry["recovered"] is True


def test_clear_publish_reconciliation_requires_a_definitive_observation(valid_record):
    key = _attempted(valid_record)
    fb_state.mark_failed(key)
    for observed in ("processing", "draft", "recent_video_match", ""):
        with pytest.raises(ValueError):
            fb_state.clear_publish_reconciliation(_VID, observed)
    assert fb_state.has_unresolved_publish(key)
    assert fb_state.clear_publish_reconciliation(_VID, "video_status_expired") is True
    assert not fb_state.has_unresolved_publish(key)


def test_record_publish_reconciliation_alerts_first_then_throttles(valid_record):
    first = fb_state.record_publish_reconciliation(_VID, project_name="p", idempotency_key="42")
    assert first is not None and first["attempts"] == 1
    again = fb_state.record_publish_reconciliation(_VID, project_name="p", idempotency_key="42")
    assert again is None
    assert fb_state.list_publish_reconciliations()[0]["attempts"] == 2


def test_clear_accepts_the_two_operator_releases_only(valid_record):
    key = _attempted(valid_record)
    fb_state.mark_failed(key)
    assert fb_state.clear_publish_reconciliation(_VID, fb_state.OPERATOR_DELETED) is True
    with fb_state._transaction() as txn:
        fb_state._add_publish_reconciliation(
            txn.data, video_id=_VID, project_name="p", idempotency_key=key, now="t"
        )
        txn.commit()
    assert fb_state.clear_publish_reconciliation(_VID, fb_state.OPERATOR_OVERRIDE) is True


@pytest.mark.parametrize("reason", ["operator_deleted_video", "operator_override_accepts_duplicate_risk"])
def test_operator_releases_cannot_settle_a_live_record(valid_record, reason):
    """Operator reasons clear a quarantine entry; they are not a Meta observation."""
    key = _attempted(valid_record)
    with pytest.raises(ValueError):
        fb_state.mark_publish_settled(key, _VID, reason)
    with pytest.raises(ValueError):
        fb_state.record_recovered_publish(key, "p", _VID, reason)
