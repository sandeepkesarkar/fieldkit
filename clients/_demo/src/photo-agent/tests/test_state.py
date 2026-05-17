"""
Tests for tools/state.py — the photo-video agent's persistent state manager.

Covers pending approval read/write/clear, Telegram offset tracking, file
auto-creation, corrupt-file handling, and concurrent-access safety.
All file I/O is redirected to a tmp directory via the autouse fixture so tests
never touch the real Mac Mini data directory.
"""

import json
import threading

import pytest

import tools.state as state


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    """
    Redirect DATA_DIR and STATE_FILE to an isolated tmp directory.

    DATA_DIR is pre-created; state.py functions call mkdir themselves, so the
    fixture only needs to point them at a safe location.
    """
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(state, "DATA_DIR", data_dir)
    monkeypatch.setattr(state, "STATE_FILE", data_dir / "state.json")


@pytest.fixture
def valid_record():
    """Return a fresh pending approval record for each test."""
    return {
        "project_name": "kitchen_remodel",
        "drive_folder_id": "1abc",
        "drive_video_file_id": "1xyz",
        "drive_folder_link": "https://drive.google.com/drive/folders/1abc",
        "video_local_path": "/tmp/kitchen_remodel/out.mp4",
        "telegram_message_id": 42,
        "triggered_at": "2026-05-16T10:00:00Z",
    }


# --- get_pending_approval ---

def test_get_pending_approval_returns_none_when_file_missing():
    """get_pending_approval() returns None when state.json does not exist."""
    assert not state.STATE_FILE.exists()
    assert state.get_pending_approval() is None


def test_get_pending_approval_returns_none_when_null_in_file():
    """get_pending_approval() returns None when pending_approval is null in the file."""
    state.STATE_FILE.write_text(json.dumps({"telegram_update_offset": 0, "pending_approval": None}))
    assert state.get_pending_approval() is None


def test_get_pending_approval_raises_on_corrupt_file():
    """get_pending_approval() raises RuntimeError when state.json is corrupt."""
    state.STATE_FILE.write_text("{corrupt: not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        state.get_pending_approval()


# --- set_pending_approval ---

def test_set_pending_approval_writes_and_get_returns_it(valid_record):
    """set_pending_approval() writes the record; get_pending_approval() returns it."""
    state.set_pending_approval(valid_record)
    assert state.get_pending_approval() == valid_record


def test_set_pending_approval_raises_on_missing_keys():
    """set_pending_approval() raises ValueError when required keys are absent."""
    with pytest.raises(ValueError, match="missing required keys"):
        state.set_pending_approval({"project_name": "test"})


# --- clear_pending_approval ---

def test_clear_pending_approval_nulls_the_record(valid_record):
    """clear_pending_approval() sets pending_approval to null; get returns None."""
    state.set_pending_approval(valid_record)
    state.clear_pending_approval()
    assert state.get_pending_approval() is None


def test_clear_pending_approval_writes_null_in_file(valid_record):
    """clear_pending_approval() persists null to disk (not an absent key)."""
    state.set_pending_approval(valid_record)
    state.clear_pending_approval()
    data = json.loads(state.STATE_FILE.read_text())
    assert "pending_approval" in data
    assert data["pending_approval"] is None


# --- get_telegram_offset ---

def test_get_telegram_offset_returns_0_when_file_missing():
    """get_telegram_offset() returns 0 when state.json does not exist."""
    assert not state.STATE_FILE.exists()
    assert state.get_telegram_offset() == 0


def test_get_telegram_offset_returns_stored_value():
    """get_telegram_offset() returns the value written by set_telegram_offset()."""
    state.set_telegram_offset(999)
    assert state.get_telegram_offset() == 999


def test_get_telegram_offset_raises_on_corrupt_file():
    """get_telegram_offset() raises RuntimeError when state.json is corrupt."""
    state.STATE_FILE.write_text("{corrupt: not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        state.get_telegram_offset()


# --- set_telegram_offset ---

def test_set_telegram_offset_does_not_overwrite_pending_approval(valid_record):
    """set_telegram_offset() updates only the offset field; pending_approval survives."""
    state.set_pending_approval(valid_record)
    state.set_telegram_offset(77)
    assert state.get_pending_approval() == valid_record
    assert state.get_telegram_offset() == 77


def test_set_telegram_offset_rejects_non_integer():
    """set_telegram_offset() raises TypeError when passed a non-integer."""
    with pytest.raises(TypeError, match="offset must be int"):
        state.set_telegram_offset(99.9)  # type: ignore[arg-type]


def test_set_telegram_offset_rejects_negative():
    """set_telegram_offset() raises ValueError when passed a negative offset."""
    with pytest.raises(ValueError, match="offset must be >= 0"):
        state.set_telegram_offset(-1)


# --- file creation ---

def test_state_file_created_if_missing_on_set_pending_approval(valid_record):
    """set_pending_approval() creates state.json when the file does not exist."""
    assert not state.STATE_FILE.exists()
    state.set_pending_approval(valid_record)
    assert state.STATE_FILE.exists()


def test_state_file_created_if_missing_on_set_telegram_offset():
    """set_telegram_offset() creates state.json when the file does not exist."""
    assert not state.STATE_FILE.exists()
    state.set_telegram_offset(5)
    assert state.STATE_FILE.exists()


# --- concurrency ---

def test_concurrent_mixed_calls_do_not_corrupt_file(valid_record):
    """Mixed set_pending_approval and set_telegram_offset threads produce a valid state file.

    This exercises the real dangerous race: thread A reads stale data and overwrites
    thread B's write. With correct LOCK_EX serialisation both fields must survive intact.
    """
    state.set_pending_approval(valid_record)
    errors = []

    def writer_offset(i):
        try:
            state.set_telegram_offset(i)
        except Exception as e:
            errors.append(e)

    def writer_approval():
        try:
            state.set_pending_approval(valid_record)
        except Exception as e:
            errors.append(e)

    threads = (
        [threading.Thread(target=writer_offset, args=(i,)) for i in range(5)]
        + [threading.Thread(target=writer_approval) for _ in range(5)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors in threads: {errors}"
    data = json.loads(state.STATE_FILE.read_text())
    assert data["pending_approval"] == valid_record
    assert isinstance(data["telegram_update_offset"], int)
    assert data["telegram_update_offset"] in range(5)
