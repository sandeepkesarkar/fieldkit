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
