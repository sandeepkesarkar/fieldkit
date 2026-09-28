"""
Unit tests for tools/share_cleanup.py — the share-link drain shared by both cron workers
(issue #80).

Uses the REAL instagram_state against an isolated tmp file and REAL fcntl locks, so the
which-entries rule and the drain lock are exercised as they run in production. Only
drive.revoke_share_link is mocked. No network, Drive, Telegram, or client data.
"""

import ast
import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.instagram_state as ig_state
from tools import share_cleanup

_PROJECT = "kitchen_remodel"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "photo-agent"
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    return data_dir


@pytest.fixture
def revoke(mocker):
    return mocker.patch.object(share_cleanup.drive, "revoke_share_link")


def _age_entry(file_id, seconds):
    """Rewind an entry's recorded_at by editing the state file directly (test harness only)."""
    raw = json.loads(ig_state.STATE_FILE.read_text())
    for entry in raw["pending_share_cleanups"]:
        if entry["file_id"] == file_id:
            then = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            entry["recorded_at"] = then.isoformat()
    ig_state.STATE_FILE.write_text(json.dumps(raw, indent=2))


def _file_ids():
    return [e["file_id"] for e in ig_state.list_share_cleanups()]


# ---------------------------------------------------------------------------
# Which entries a worker that does NOT hold upload_instagram.lock may revoke
# ---------------------------------------------------------------------------

def test_fresh_intent_is_not_revoked_without_the_instagram_lock(revoke):
    """A bare intent may be a link Instagram is still fetching — leave it alone."""
    ig_state.record_share_intent("live_file", _PROJECT)
    alerts = []
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    revoke.assert_not_called()
    assert _file_ids() == ["live_file"]
    assert alerts == []


def test_intent_just_under_the_threshold_is_still_left_alone(revoke):
    ig_state.record_share_intent("live_file", _PROJECT)
    _age_entry("live_file", share_cleanup.ORPHANED_INTENT_AFTER_SECONDS - 60)
    share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    revoke.assert_not_called()


def test_intent_older_than_threshold_is_revoked_and_cleared(revoke):
    """Older than any attempt can run: the attempt that made it is dead or finished."""
    ig_state.record_share_intent("orphan_file", _PROJECT)
    _age_entry("orphan_file", share_cleanup.ORPHANED_INTENT_AFTER_SECONDS + 1)
    share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    revoke.assert_called_once_with("orphan_file")
    assert _file_ids() == []


def test_already_failed_revoke_is_due_immediately(revoke):
    """attempts >= 1 means the attempt that used the link is over — due regardless of age."""
    ig_state.record_share_cleanup("failed_file", _PROJECT)
    share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    revoke.assert_called_once_with("failed_file")
    assert _file_ids() == []


def test_instagram_lock_holder_drains_fresh_intents_too(revoke):
    """Under upload_instagram.lock no attempt can be live, so every entry is orphaned."""
    ig_state.record_share_intent("killed_attempt_file", _PROJECT)
    share_cleanup.drain(lambda t: None, holds_instagram_lock=True)
    revoke.assert_called_once_with("killed_attempt_file")
    assert _file_ids() == []


@pytest.mark.parametrize("recorded_at", [None, "not-a-timestamp"])
def test_intent_with_unusable_timestamp_is_treated_as_orphaned(recorded_at):
    """No evidence of a live attempt is not a reason to leave a public link up."""
    entry = {"file_id": "f", "attempts": 0, "recorded_at": recorded_at}
    assert share_cleanup._is_orphaned(entry, datetime.now(timezone.utc)) is True


def test_naive_timestamp_is_read_as_utc():
    now = datetime.now(timezone.utc)
    fresh = {"attempts": 0, "recorded_at": now.replace(tzinfo=None).isoformat()}
    assert share_cleanup._is_orphaned(fresh, now) is False


def test_threshold_covers_the_instagram_claim_lease():
    """An attempt can legitimately run for up to the lease — the threshold must not be shorter."""
    import scripts.upload_instagram as ui
    assert share_cleanup.ORPHANED_INTENT_AFTER_SECONDS >= ui._UPLOAD_LEASE_SECONDS


# ---------------------------------------------------------------------------
# Failure handling and alerts
# ---------------------------------------------------------------------------

def test_failed_retry_keeps_the_entry_and_alerts_once(revoke):
    ig_state.record_share_intent("orphan_file", _PROJECT)
    _age_entry("orphan_file", share_cleanup.ORPHANED_INTENT_AFTER_SECONDS + 1)
    revoke.side_effect = RuntimeError("Drive down")
    alerts = []
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    assert _file_ids() == ["orphan_file"]
    assert len(alerts) == 1
    assert "orphan_file" in alerts[0] and _PROJECT in alerts[0]
    assert ig_state.list_share_cleanups()[0]["attempts"] == 2


def test_retry_failure_does_not_resurrect_an_entry_another_worker_cleared(mocker):
    """The other drain revoked and cleared it between our read and our failed retry."""
    ig_state.record_share_cleanup("raced_file", _PROJECT)

    def _other_worker_cleared_it_then_drive_failed(file_id):
        ig_state.clear_share_cleanup(file_id)
        raise RuntimeError("Drive down")

    mocker.patch.object(
        share_cleanup.drive, "revoke_share_link",
        side_effect=_other_worker_cleared_it_then_drive_failed,
    )
    alerts = []
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    assert _file_ids() == []
    assert alerts == []


def test_alert_text_names_file_not_url():
    text = share_cleanup.alert_text({
        "file_id": "drive_file_9", "project_name": _PROJECT, "attempts": 3,
        "recorded_at": "2026-09-01T00:00:00+00:00",
    })
    assert "drive_file_9" in text
    assert "http" not in text


def test_redacts_exception_text_in_logs(revoke, caplog):
    ig_state.record_share_cleanup("f1", _PROJECT)
    revoke.side_effect = RuntimeError("failed access_token=SECRETVALUE123")
    share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    assert "SECRETVALUE123" not in caplog.text


# ---------------------------------------------------------------------------
# The drain lock
# ---------------------------------------------------------------------------

def _hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_drain_skips_without_waiting_when_another_drain_holds_the_lock(revoke, isolated_state):
    ig_state.record_share_cleanup("f1", _PROJECT)
    held = _hold(isolated_state / share_cleanup.LOCK_FILENAME)
    try:
        share_cleanup.drain(lambda t: None, holds_instagram_lock=True)
        revoke.assert_not_called()
        assert _file_ids() == ["f1"]
    finally:
        held.close()
    share_cleanup.drain(lambda t: None, holds_instagram_lock=True)
    revoke.assert_called_once_with("f1")


def test_drain_lock_is_released_after_a_drain_that_raised(mocker, isolated_state):
    mocker.patch.object(
        share_cleanup.instagram_state, "list_share_cleanups", side_effect=ValueError("corrupt")
    )
    with pytest.raises(ValueError):
        share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    held = _hold(isolated_state / share_cleanup.LOCK_FILENAME)  # would raise if still held
    held.close()


def test_drain_does_not_need_the_instagram_upload_lock(revoke, isolated_state):
    """An in-flight Instagram publish holding its own lock does not stop a due revoke."""
    ig_state.record_share_cleanup("f1", _PROJECT)
    held = _hold(isolated_state / "upload_instagram.lock")
    try:
        share_cleanup.drain(lambda t: None, holds_instagram_lock=False)
    finally:
        held.close()
    revoke.assert_called_once_with("f1")


# ---------------------------------------------------------------------------
# No write path of its own
# ---------------------------------------------------------------------------

def test_module_uses_only_instagram_states_public_api():
    """Every mutation must go through instagram_state's transactional functions."""
    tree = ast.parse(Path(share_cleanup.__file__).read_text())
    private = [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "instagram_state"
        and node.attr.startswith("_")
    ]
    assert private == []
    assert "STATE_FILE" not in Path(share_cleanup.__file__).read_text()
