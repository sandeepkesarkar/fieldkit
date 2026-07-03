"""
Tests for tools/facebook_state.py — the Facebook upload state manager.

Covers: set_pending_upload (success, missing keys, idempotency check),
get_pending_upload, mark_uploading, mark_published, mark_failed,
increment_attempt, is_published, fcntl exclusive locking, and FIELDKIT_DATA_DIR
env override.
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


# --- mark_uploading ---

def test_mark_uploading_sets_status(valid_record):
    """mark_uploading() transitions the pending record's status to 'uploading'."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_uploading(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["status"] == "uploading"


def test_mark_uploading_preserves_other_fields(valid_record):
    """mark_uploading() does not alter fields other than status."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_uploading(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["project_name"] == valid_record["project_name"]
    assert record["idempotency_key"] == valid_record["idempotency_key"]


# --- mark_published ---

def test_mark_published_sets_status_and_post_id(valid_record):
    """mark_published() sets status=published and stores fb_post_id."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_published(valid_record["idempotency_key"], "fb_post_999")
    record = fb_state.get_pending_upload()
    assert record["status"] == "published"
    assert record["fb_post_id"] == "fb_post_999"


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


# --- mark_failed ---

def test_mark_failed_sets_status(valid_record):
    """mark_failed() transitions status to 'failed'."""
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_failed(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["status"] == "failed"


def test_mark_failed_preserves_attempt_count(valid_record):
    """mark_failed() does not reset attempt_count."""
    valid_record["attempt_count"] = 2
    fb_state.set_pending_upload(valid_record)
    fb_state.mark_failed(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["attempt_count"] == 2


# --- increment_attempt ---

def test_increment_attempt_increments_count(valid_record):
    """increment_attempt() increments attempt_count by 1."""
    fb_state.set_pending_upload(valid_record)
    fb_state.increment_attempt(valid_record["idempotency_key"])
    assert fb_state.get_pending_upload()["attempt_count"] == 1


def test_increment_attempt_sets_last_attempt_at(valid_record):
    """increment_attempt() sets last_attempt_at to a non-null ISO-8601 string."""
    fb_state.set_pending_upload(valid_record)
    fb_state.increment_attempt(valid_record["idempotency_key"])
    record = fb_state.get_pending_upload()
    assert record["last_attempt_at"] is not None
    from datetime import datetime
    datetime.fromisoformat(record["last_attempt_at"])


def test_increment_attempt_twice_accumulates(valid_record):
    """Calling increment_attempt() twice yields attempt_count == 2."""
    fb_state.set_pending_upload(valid_record)
    fb_state.increment_attempt(valid_record["idempotency_key"])
    fb_state.increment_attempt(valid_record["idempotency_key"])
    assert fb_state.get_pending_upload()["attempt_count"] == 2


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
    """Concurrent get_pending_upload and increment_attempt leave the state file valid."""
    fb_state.set_pending_upload(valid_record)
    errors = []

    def do_read():
        try:
            fb_state.get_pending_upload()
        except Exception as exc:
            errors.append(exc)

    def do_write():
        try:
            fb_state.increment_attempt(valid_record["idempotency_key"])
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
