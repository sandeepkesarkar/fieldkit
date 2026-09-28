"""
Unit tests for tools/share_cleanup.py — the share-link drain shared by both cron workers
(issue #80).

Uses the REAL instagram_state against an isolated tmp file and REAL flock (owner fences,
the drain lock), so ownership and locking are exercised as they run in production. flock
locks belong to an open file description, so a fence held by this process is seen as held
by a second open of the same file — exactly how a drain sees another process's attempt.
Only drive.delete_temporary_share is mocked. No network, Drive, Telegram, or client data.
"""

import ast
import contextlib
import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.instagram_state as ig_state
from tools import share_cleanup

_PROJECT = "kitchen_remodel"
_NO_ALERTS = lambda text: None  # noqa: E731


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "photo-agent"
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    return data_dir


@pytest.fixture
def revoke(mocker):
    return mocker.patch.object(share_cleanup.drive, "delete_temporary_share")


@pytest.fixture
def live_owner():
    """An attempt that is still running: its fence is held for the whole test."""
    with contextlib.ExitStack() as stack:
        yield stack, stack.enter_context(share_cleanup.share_owner())


def _dead_owner():
    """An attempt that has finished (or died): its fence was released."""
    with share_cleanup.share_owner() as token:
        return token


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


_OVERDUE = share_cleanup.ORPHANED_INTENT_AFTER_SECONDS + 1


# ---------------------------------------------------------------------------
# The owner fence
# ---------------------------------------------------------------------------

def test_owner_is_alive_while_its_fence_is_held_and_done_after(isolated_state):
    with share_cleanup.share_owner() as token:
        assert share_cleanup._owner_is_alive(token) is True
    assert share_cleanup._owner_is_alive(token) is False
    assert not (isolated_state / share_cleanup.OWNER_DIRNAME / f"{token}.lock").exists()


def test_owner_fence_is_released_when_the_attempt_raises():
    with pytest.raises(KeyboardInterrupt):
        with share_cleanup.share_owner() as token:
            raise KeyboardInterrupt
    assert share_cleanup._owner_is_alive(token) is False


def test_a_killed_owner_leaves_its_file_but_not_its_lock(isolated_state):
    """Process death releases flock but runs no cleanup: the file is left, unlocked."""
    token = "ab" * 16
    owners = isolated_state / share_cleanup.OWNER_DIRNAME
    owners.mkdir(parents=True)
    (owners / f"{token}.lock").write_text("")
    assert share_cleanup._owner_is_alive(token) is False


@pytest.mark.parametrize("token", [None, "", "../../instagram_state", "ABC", 123])
def test_unusable_owner_token_means_ownership_unknown(token):
    assert share_cleanup._owner_is_alive(token) is None


# ---------------------------------------------------------------------------
# Live owner: revoke only when due, NEVER clear
# ---------------------------------------------------------------------------

def test_live_owners_fresh_link_is_left_alone(revoke, live_owner):
    _, token = live_owner
    ig_state.record_share_intent("live_file", _PROJECT, owner=token)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=True)
    revoke.assert_not_called()
    assert _file_ids() == ["live_file"]


def test_live_owners_link_just_under_the_threshold_is_left_alone(revoke, live_owner):
    _, token = live_owner
    ig_state.record_share_intent("live_file", _PROJECT, owner=token)
    _age_entry("live_file", share_cleanup.ORPHANED_INTENT_AFTER_SECONDS - 60)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    revoke.assert_not_called()


@pytest.mark.parametrize("holds_instagram_lock", [False, True])
def test_live_owners_overdue_copy_is_deleted_and_the_obligation_retired(
    revoke, live_owner, holds_instagram_lock
):
    """A stalled attempt's exposure is ended by deleting its copy. Retiring the obligation
    while the attempt still lives is safe ONLY because the deletion is confirmed: nothing
    can make a file that no longer exists public again."""
    _, token = live_owner
    ig_state.record_share_intent("stalled_file", _PROJECT, owner=token)
    _age_entry("stalled_file", _OVERDUE)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=holds_instagram_lock)
    revoke.assert_called_once_with("stalled_file")
    assert _file_ids() == []


def test_live_owners_failed_cleanup_is_due_at_once(revoke, live_owner):
    _, token = live_owner
    ig_state.record_share_intent("f", _PROJECT, owner=token)
    ig_state.record_share_cleanup("f", _PROJECT)  # the attempt's own cleanup failed
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    revoke.assert_called_once_with("f")
    assert _file_ids() == []


def test_unconfirmed_deletion_never_retires_an_obligation(mocker, live_owner):
    """Whoever owns it, and however often it is retried, an obligation outlives every
    deletion that is not confirmed."""
    _, token = live_owner
    ig_state.record_share_intent("f", _PROJECT, owner=token)
    ig_state.record_share_intent("g", _PROJECT, owner=_dead_owner())
    ig_state.record_share_intent("h", _PROJECT)  # no owner token
    for fid in ("f", "g", "h"):
        _age_entry(fid, _OVERDUE)
    mocker.patch.object(
        share_cleanup.drive, "delete_temporary_share",
        side_effect=RuntimeError("Drive file f still exists after delete"),
    )
    for holds in (False, True, False):
        share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=holds)
    assert sorted(_file_ids()) == ["f", "g", "h"]


# ---------------------------------------------------------------------------
# Provably-done owner: revoke at once, whatever the age, then clear
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("holds_instagram_lock", [False, True])
def test_done_owners_fresh_link_is_revoked_and_cleared_at_once(
    revoke, isolated_state, holds_instagram_lock
):
    token = _dead_owner()
    ig_state.record_share_intent("f", _PROJECT, owner=token)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=holds_instagram_lock)
    revoke.assert_called_once_with("f")
    assert _file_ids() == []


def test_killed_owners_leftover_file_is_removed_once_its_obligation_clears(
    revoke, isolated_state
):
    token = "cd" * 16
    owner_file = isolated_state / share_cleanup.OWNER_DIRNAME / f"{token}.lock"
    owner_file.parent.mkdir(parents=True)
    owner_file.write_text("")
    ig_state.record_share_intent("f", _PROJECT, owner=token)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    assert _file_ids() == []
    assert not owner_file.exists()


# ---------------------------------------------------------------------------
# Entries without an owner token (written before owners existed)
# ---------------------------------------------------------------------------

def test_ownerless_entry_is_cleared_by_the_instagram_lock_holder(revoke):
    ig_state.record_share_intent("legacy", _PROJECT)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=True)
    revoke.assert_called_once_with("legacy")
    assert _file_ids() == []


def test_ownerless_fresh_entry_is_left_alone_by_other_callers(revoke):
    ig_state.record_share_intent("legacy", _PROJECT)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    revoke.assert_not_called()


def test_ownerless_overdue_entry_is_deleted_and_retired_by_other_callers(revoke):
    ig_state.record_share_intent("legacy", _PROJECT)
    _age_entry("legacy", _OVERDUE)
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    revoke.assert_called_once_with("legacy")
    assert _file_ids() == []


# ---------------------------------------------------------------------------
# When a link is due while its owner may be alive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recorded_at", [None, "not-a-timestamp"])
def test_unusable_timestamp_counts_as_due(recorded_at):
    entry = {"file_id": "f", "attempts": 0, "recorded_at": recorded_at}
    assert share_cleanup._is_due(entry, datetime.now(timezone.utc)) is True


def test_naive_timestamp_is_read_as_utc():
    now = datetime.now(timezone.utc)
    fresh = {"attempts": 0, "recorded_at": now.replace(tzinfo=None).isoformat()}
    assert share_cleanup._is_due(fresh, now) is False


def test_threshold_covers_the_instagram_claim_lease():
    """An attempt can legitimately run for up to the lease — the threshold must not be shorter.

    This bounds only when a LIVE attempt's link may be revoked (and so how often a slow but
    healthy publish is disturbed). It is not what makes clearing safe — the owner fence is.
    """
    import scripts.upload_instagram as ui
    assert share_cleanup.ORPHANED_INTENT_AFTER_SECONDS >= ui._UPLOAD_LEASE_SECONDS


# ---------------------------------------------------------------------------
# Failure handling and alerts
# ---------------------------------------------------------------------------

def test_failed_retry_keeps_the_entry_and_alerts_once(revoke):
    ig_state.record_share_intent("f", _PROJECT, owner=_dead_owner())
    revoke.side_effect = RuntimeError("Drive down")
    alerts = []
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    share_cleanup.drain(alerts.append, holds_instagram_lock=False)
    assert _file_ids() == ["f"]
    assert len(alerts) == 1
    assert "f" in alerts[0] and _PROJECT in alerts[0]
    assert ig_state.list_share_cleanups()[0]["attempts"] == 2


def test_retry_failure_does_not_resurrect_an_entry_another_worker_cleared(mocker):
    """The other drain revoked and cleared it between our read and our failed retry."""
    ig_state.record_share_intent("raced", _PROJECT, owner=_dead_owner())

    def _other_worker_cleared_it_then_drive_failed(file_id):
        ig_state.clear_share_cleanup(file_id)
        raise RuntimeError("Drive down")

    mocker.patch.object(
        share_cleanup.drive, "delete_temporary_share",
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
    ig_state.record_share_intent("f1", _PROJECT, owner=_dead_owner())
    revoke.side_effect = RuntimeError("failed access_token=SECRETVALUE123")
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    assert "SECRETVALUE123" not in caplog.text


# ---------------------------------------------------------------------------
# The drain lock, and creating nothing when there is nothing to do
# ---------------------------------------------------------------------------

def _hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_drain_skips_without_waiting_when_another_drain_holds_the_lock(revoke, isolated_state):
    ig_state.record_share_intent("f1", _PROJECT, owner=_dead_owner())
    held = _hold(isolated_state / share_cleanup.LOCK_FILENAME)
    try:
        share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=True)
        revoke.assert_not_called()
        assert _file_ids() == ["f1"]
    finally:
        held.close()
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=True)
    revoke.assert_called_once_with("f1")


def test_drain_lock_is_released_after_a_drain_that_raised(mocker, isolated_state):
    ig_state.record_share_intent("f1", _PROJECT, owner=_dead_owner())
    mocker.patch.object(
        share_cleanup.drive, "delete_temporary_share", side_effect=ValueError("unexpected")
    )
    with pytest.raises(ValueError):
        share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    held = _hold(isolated_state / share_cleanup.LOCK_FILENAME)  # would raise if still held
    held.close()


def test_drain_does_not_need_the_instagram_upload_lock(revoke, isolated_state):
    """An in-flight Instagram publish holding its own lock does not stop a due revoke."""
    ig_state.record_share_intent("f1", _PROJECT, owner=_dead_owner())
    held = _hold(isolated_state / "upload_instagram.lock")
    try:
        share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    finally:
        held.close()
    revoke.assert_called_once_with("f1")


def test_drain_with_no_instagram_state_creates_nothing(revoke, isolated_state):
    """A Facebook-only client: not the state file, not the lock, not even the directory."""
    assert not isolated_state.exists()
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    assert not isolated_state.exists()
    revoke.assert_not_called()


def test_drain_with_an_empty_list_takes_no_lock(revoke, isolated_state):
    ig_state.record_share_intent("f", _PROJECT, owner=_dead_owner())
    ig_state.clear_share_cleanup("f")
    share_cleanup.drain(_NO_ALERTS, holds_instagram_lock=False)
    assert not (isolated_state / share_cleanup.LOCK_FILENAME).exists()


# ---------------------------------------------------------------------------
# No write path of its own
# ---------------------------------------------------------------------------

def test_module_uses_only_instagram_states_public_api():
    """Every mutation must go through instagram_state's transactional functions."""
    source = Path(share_cleanup.__file__).read_text()
    tree = ast.parse(source)
    private = [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "instagram_state"
        and node.attr.startswith("_")
    ]
    assert private == []
    assert "STATE_FILE" not in source


# ---------------------------------------------------------------------------
# Sweeping owner files left by attempts killed before registering anything
# ---------------------------------------------------------------------------

def _owner_file(isolated_state, token, age_seconds):
    import os
    path = isolated_state / share_cleanup.OWNER_DIRNAME / f"{token}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    then = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(path, (then, then))
    return path


def test_sweep_removes_old_unlocked_unreferenced_owner_files(isolated_state):
    path = _owner_file(isolated_state, "ab" * 16, _OVERDUE)
    assert share_cleanup.sweep_dead_owner_files() == 1
    assert not path.exists()


def test_sweep_keeps_young_referenced_or_live_owner_files(isolated_state, live_owner):
    young = _owner_file(isolated_state, "cd" * 16, 60)          # may be mid-creation
    referenced = _owner_file(isolated_state, "ef" * 16, _OVERDUE)
    ig_state.record_share_intent("f", _PROJECT, owner="ef" * 16)
    _, live_token = live_owner
    live = isolated_state / share_cleanup.OWNER_DIRNAME / f"{live_token}.lock"
    import os
    then = datetime.now(timezone.utc).timestamp() - _OVERDUE
    os.utime(live, (then, then))
    assert share_cleanup.sweep_dead_owner_files() == 0
    assert young.exists() and referenced.exists() and live.exists()


def test_sweep_creates_nothing_when_there_is_no_owner_directory(isolated_state):
    assert share_cleanup.sweep_dead_owner_files() == 0
    assert not isolated_state.exists()
