import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.state as state


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "email-agent"
    data_dir.mkdir()
    monkeypatch.setattr(state, "DATA_DIR", data_dir)
    monkeypatch.setattr(state, "STATE_FILE", data_dir / "state.json")
    monkeypatch.setattr(state, "PENDING_FILE", data_dir / "pending.json")


# --- Ref ID tests ---

def test_new_message_starts_at_0001():
    ref_id = state.get_ref_id_for_message("msg001")
    assert ref_id == "#0001"


def test_ref_id_increments_across_calls():
    assert state.get_ref_id_for_message("msg001") == "#0001"
    assert state.get_ref_id_for_message("msg002") == "#0002"
    assert state.get_ref_id_for_message("msg003") == "#0003"


def test_known_message_returns_existing_ref_id():
    first = state.get_ref_id_for_message("msgABC")
    second = state.get_ref_id_for_message("msgABC")
    assert first == second == "#0001"
    assert state.read_last_ref_id() == 1


def test_zero_padding():
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
    assert state.get_label_id() is None


def test_get_label_id_returns_value_after_save():
    state.save_label_id("Label_99999")
    assert state.get_label_id() == "Label_99999"


# --- Pending queue tests ---

def test_enqueue_adds_entry_with_correct_fields():
    state.enqueue_pending("#0001", "msgXYZ", "admin@example.com", "Job #42")
    data = json.loads(state.PENDING_FILE.read_text())
    assert len(data["pending"]) == 1
    entry = data["pending"][0]
    assert entry["ref_id"] == "#0001"
    assert entry["gmail_message_id"] == "msgXYZ"
    assert entry["from"] == "admin@example.com"
    assert entry["subject"] == "Job #42"
    assert entry["queued_at"].endswith("Z")
    # Verify the timestamp is valid UTC ISO format
    dt = datetime.fromisoformat(entry["queued_at"].replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - dt).total_seconds() < 5


def test_dequeue_removes_only_matching_ref_id():
    state.enqueue_pending("#0001", "msg001", "a@example.com", "First")
    state.enqueue_pending("#0002", "msg002", "a@example.com", "Second")
    state.dequeue_pending("#0001")
    data = json.loads(state.PENDING_FILE.read_text())
    assert len(data["pending"]) == 1
    assert data["pending"][0]["ref_id"] == "#0002"


def test_get_stale_pending_returns_old_ignores_fresh():
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
    assert not state.STATE_FILE.exists()
    state.get_ref_id_for_message("msgNew")
    assert state.STATE_FILE.exists()


def test_functions_create_pending_file_if_missing():
    assert not state.PENDING_FILE.exists()
    state.enqueue_pending("#0001", "msg001", "a@example.com", "Test")
    assert state.PENDING_FILE.exists()


def test_get_stale_pending_returns_empty_when_file_missing():
    assert not state.PENDING_FILE.exists()
    assert state.get_stale_pending() == []


# --- Concurrency test ---

def test_concurrent_calls_produce_unique_ref_ids():
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
