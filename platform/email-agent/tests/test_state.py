"""
Tests for tools/state.py — the email agent's persistent state manager.

Covers ref ID assignment, label ID caching, the pending queue (enqueue/dequeue/stale
check), file auto-creation, and concurrent access safety. All file I/O is redirected
to a tmp directory via the autouse fixture so tests never touch the real Mac Mini
data directory.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.state as state


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    """
    Redirect DATA_DIR, STATE_FILE, and PENDING_FILE to an isolated tmp directory.

    The directory is pre-created here (unlike the logger fixture) because state.py
    functions call DATA_DIR.mkdir() themselves and the fixture only needs to point
    them at a safe location, not test directory creation behaviour.
    """
    data_dir = tmp_path / "email-agent"
    data_dir.mkdir()
    monkeypatch.setattr(state, "DATA_DIR", data_dir)
    monkeypatch.setattr(state, "STATE_FILE", data_dir / "state.json")
    monkeypatch.setattr(state, "PENDING_FILE", data_dir / "pending.json")


# --- Ref ID tests ---

def test_new_message_starts_at_0001():
    """
    The first ref ID assigned to any message is always #0001.

    The counter in state.json is initialised to 0 on first read; the first
    increment produces 1, zero-padded to 4 digits per spec.
    """
    ref_id = state.get_ref_id_for_message("msg001")
    assert ref_id == "#0001"


def test_ref_id_increments_across_calls():
    """
    Each distinct gmail_message_id receives the next sequential ref ID.

    Verifies that the counter persists across calls within the same test (i.e.
    reads and writes to the same state.json file each time).
    """
    assert state.get_ref_id_for_message("msg001") == "#0001"
    assert state.get_ref_id_for_message("msg002") == "#0002"
    assert state.get_ref_id_for_message("msg003") == "#0003"


def test_known_message_returns_existing_ref_id():
    """
    Calling with the same gmail_message_id twice returns the same ref ID without
    incrementing the counter.

    Critical for idempotency: a cron re-run that sees a message it already processed
    must not assign a second ref ID or inflate the counter.
    """
    first = state.get_ref_id_for_message("msgABC")
    second = state.get_ref_id_for_message("msgABC")
    assert first == second == "#0001"
    assert state.read_last_ref_id() == 1


def test_zero_padding():
    """
    Ref IDs are zero-padded to 4 digits at all boundary values: #0010, #0100, #1000.

    The format string uses :04d, which pads to 4 digits for numbers 1–9999 and
    produces wider strings beyond that. This test documents the expected behaviour
    at the three 1-digit-width boundaries the spec cares about.
    """
    for i in range(1, 10):
        state.get_ref_id_for_message(f"msg{i:04d}")
    assert state.get_ref_id_for_message("msg0010") == "#0010"
    for i in range(11, 100):
        state.get_ref_id_for_message(f"msg{i:04d}")
    assert state.get_ref_id_for_message("msg0100") == "#0100"
    for i in range(101, 1000):
        state.get_ref_id_for_message(f"msg{i:04d}")
    assert state.get_ref_id_for_message("msg1000") == "#1000"


# --- Label ID tests ---

def test_get_label_id_returns_none_when_not_cached():
    """
    get_label_id() returns None before the Gmail label has been resolved.

    The caller uses None as the signal to make a Gmail API call and then
    save_label_id() the result. If this returned a stale value, every cycle
    would skip the API call and never cache the real label ID.
    """
    assert state.get_label_id() is None


def test_get_label_id_returns_value_after_save():
    """
    A label ID saved with save_label_id() is returned by the next get_label_id() call.

    Verifies the round-trip through state.json without other fields being clobbered
    (save_label_id does a read-modify-write, not a full overwrite).
    """
    state.save_label_id("Label_99999")
    assert state.get_label_id() == "Label_99999"


# --- Pending queue tests ---

def test_enqueue_adds_entry_with_correct_fields():
    """
    enqueue_pending() writes all five required fields to pending.json.

    queued_at must be UTC ISO 8601 with a Z suffix (not +00:00) — the stale-check
    parser strips Z before calling fromisoformat(), so the suffix form is load-bearing.
    The timestamp must be recent (within 5 seconds) to catch clock/timezone mistakes.
    """
    state.enqueue_pending("#0001", "msgXYZ", "admin@example.com", "Job #42")
    data = json.loads(state.PENDING_FILE.read_text())
    assert len(data["pending"]) == 1
    entry = data["pending"][0]
    assert entry["ref_id"] == "#0001"
    assert entry["gmail_message_id"] == "msgXYZ"
    assert entry["from"] == "admin@example.com"
    assert entry["subject"] == "Job #42"
    assert entry["queued_at"].endswith("Z")
    # Timestamp must be valid UTC ISO format and within 5 seconds of now.
    dt = datetime.fromisoformat(entry["queued_at"].replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - dt).total_seconds() < 5


def test_dequeue_removes_only_matching_ref_id():
    """
    dequeue_pending() removes exactly one entry by ref_id; all others survive.

    Enqueues two entries, dequeues the first, and confirms only the second remains.
    An incorrect filter (e.g. clearing the whole list) would leave zero entries.
    """
    state.enqueue_pending("#0001", "msg001", "a@example.com", "First")
    state.enqueue_pending("#0002", "msg002", "a@example.com", "Second")
    state.dequeue_pending("#0001")
    data = json.loads(state.PENDING_FILE.read_text())
    assert len(data["pending"]) == 1
    assert data["pending"][0]["ref_id"] == "#0002"


def test_get_stale_pending_returns_old_ignores_fresh():
    """
    get_stale_pending() returns only entries older than the threshold (default 15 min).

    Places one entry 20 minutes old (above threshold) and one 5 minutes old (below).
    Only the older one should be returned. Uses the default threshold of 15 min,
    which matches the production cron poll interval.
    """
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_time = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    pending_data = {
        "pending": [
            {"ref_id": "#0012", "gmail_message_id": "old1", "from": "a@b.com", "subject": "Old", "queued_at": old_time},
            {"ref_id": "#0013", "gmail_message_id": "new1", "from": "a@b.com", "subject": "Fresh", "queued_at": fresh_time},
        ]
    }
    state.PENDING_FILE.write_text(json.dumps(pending_data))

    stale = state.get_stale_pending(threshold_minutes=15)
    assert len(stale) == 1
    assert stale[0]["ref_id"] == "#0012"


# --- File creation tests ---

def test_functions_create_state_file_if_missing():
    """
    get_ref_id_for_message() creates state.json on first call when the file is absent.

    The fixture creates DATA_DIR but not state.json. Verifies the function does not
    crash on a missing file and leaves the file in place after writing.
    """
    assert not state.STATE_FILE.exists()
    state.get_ref_id_for_message("msgNew")
    assert state.STATE_FILE.exists()


def test_functions_create_pending_file_if_missing():
    """
    enqueue_pending() creates pending.json on first call when the file is absent.

    Same pattern as the state.json test — DATA_DIR exists, the file does not,
    and the function must create it rather than raising FileNotFoundError.
    """
    assert not state.PENDING_FILE.exists()
    state.enqueue_pending("#0001", "msg001", "a@example.com", "Test")
    assert state.PENDING_FILE.exists()


def test_get_stale_pending_returns_empty_when_file_missing():
    """
    get_stale_pending() returns [] when pending.json does not exist.

    On a fresh install, or after a restart before any messages are queued, the
    pending file won't exist. The function must treat this as 'no stale entries'
    rather than raising an exception.
    """
    assert not state.PENDING_FILE.exists()
    assert state.get_stale_pending() == []


# --- Concurrency test ---

def test_concurrent_calls_produce_unique_ref_ids():
    """
    10 threads calling get_ref_id_for_message() simultaneously each get a unique ref ID.

    This is the primary test for the fcntl.LOCK_EX file locking in state.py. Without
    the lock, two threads could read the same last_ref_id value before either has
    written back, producing duplicate ref IDs. The final counter must equal the thread
    count — no increments lost, no increments doubled.
    """
    results = []
    errors = []
    num_threads = 10

    def assign_ref(msg_id):
        try:
            ref = state.get_ref_id_for_message(msg_id)
            results.append(ref)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=assign_ref, args=(f"concurrent_msg_{i}",)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors in threads: {errors}"
    assert len(results) == num_threads
    assert len(set(results)) == num_threads, f"Duplicate ref IDs: {results}"
    assert state.read_last_ref_id() == num_threads
