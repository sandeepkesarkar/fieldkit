"""
Tests for scripts/upload_instagram.py — the Instagram Reel upload cron script.

US1 (T011): feature-disabled exit, lock contention, no pending job, claim
dispatch, the happy-path publish lifecycle, missing video file, env validation.

US3 (T016): transient retry, cooldown, exhaustion, token expiry, and the
container-poll timeout.

All external calls (instagram_state, instagram_api, instagram_logger, drive,
telegram_api) are mocked. No real network, Drive, or state-file access.

Modeled on tests/test_upload_facebook.py — upload_instagram.py mirrors
upload_facebook.py's claim-based structure, so the two test suites should read
alike where the behaviour is the same and differ only where Instagram's
container flow and Drive share link genuinely differ.
"""

from pathlib import Path

import pytest

from scripts.upload_instagram import main
from tools.instagram_api import (
    InstagramTokenError,
    InstagramUploadError,
)
# Bound at import time, BEFORE any fixture replaces the module attribute with a mock —
# the end-to-end redaction test needs the genuine writer, not base's stand-in.
from tools.instagram_logger import log_upload_attempt_failed as _real_log_attempt_failed

_PROJECT = "test_project"
_IG_ACCOUNT_ID = "17841400000000000"
_PAGE_TOKEN = "page_token_abc"
_CHAT_ID = "telegram_chat_id"
_IDEM_KEY = "42"
_CONTAINER_ID = "container_99"
_POST_ID = "ig_post_123"
_PERMALINK = "https://www.instagram.com/reel/AbCdEfGhIjK/"
_SHARE_LINK = "https://drive.google.com/uc?export=download&id=drive_file_1"
_SHARE_FILE_ID = "drive_file_1"

_PENDING_RECORD = {
    "project_name": _PROJECT,
    "video_local_path": "/nonexistent/video.mp4",
    "ig_business_account_id": _IG_ACCOUNT_ID,
    "status": "pending",
    "attempt_count": 0,
    "last_attempt_at": None,
    "triggered_at": "2026-08-31T14:00:00Z",
    "idempotency_key": _IDEM_KEY,
    "container_id": None,
    "ig_post_id": None,
}


@pytest.fixture(autouse=True)
def isolated_activity_log(tmp_path, monkeypatch):
    """Keep the activity log out of the developer's real client log directory.

    instagram_logger resolves LOG_DIR from FIELDKIT_LOG_DIR at IMPORT time, and the
    scripts under test load the real client .env at import — so any logging call that
    is not individually mocked appends to the actual checkout's photo-agent.log. Relying
    on the mock list staying exhaustive is what let that happen; patching the path makes
    it structural, and keeps newly-added log events from silently reintroducing it.
    """
    import tools.instagram_logger as ig_logger
    log_dir = tmp_path / "activity_log"
    monkeypatch.setattr(ig_logger, "LOG_DIR", log_dir)
    monkeypatch.setattr(ig_logger, "LOG_FILE", log_dir / "photo-agent.log")
    return ig_logger


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    """Keep instagram_state's file out of the developer's real client data directory.

    Most state calls are mocked in `base`, but "most" is exactly the problem: a code path
    that reaches an UNMOCKED state function writes to whatever STATE_FILE resolved to at
    import, and upload_instagram.py loads the real client .env at import. That is how a
    test run came to leave real entries in clients/_demo/data. Patching the path makes the
    isolation structural instead of depending on the mock list staying exhaustive.
    """
    import tools.instagram_state as ig_state
    data_dir = tmp_path / "state"
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    return ig_state


@pytest.fixture(autouse=True)
def isolated_worker_health(tmp_path, monkeypatch):
    """Keep heartbeats out of the developer's real client data directory.

    worker_health resolves its file path from FIELDKIT_DATA_DIR at IMPORT time, and
    upload_instagram.py loads the real client .env at import — so without this every
    `main([])` in this file would write a heartbeat into the actual checkout.
    """
    import tools.worker_health as wh
    data_dir = tmp_path / "health"
    monkeypatch.setattr(wh, "DATA_DIR", data_dir)
    monkeypatch.setattr(wh, "HEALTH_FILE", data_dir / "worker_health.json")
    return wh


@pytest.fixture(autouse=True)
def isolated_video_tmp_dir(tmp_path, monkeypatch):
    """Pin VIDEO_TMP_DIR so an ambient setting cannot change what these tests exercise.

    The orphan sweep reads state and walks this directory on every tick. Left to the
    environment, whether it does either depends on the developer's shell, which made four
    assertions below pass or fail according to ambient configuration rather than behaviour.
    """
    root = tmp_path / "video_tmp"
    root.mkdir()
    monkeypatch.setenv("VIDEO_TMP_DIR", str(root))
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _PAGE_TOKEN)
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", _IG_ACCOUNT_ID)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_bot_token")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FIELDKIT_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def base(mocker, env):
    """Common mocks. Default: no pending job, claim would be granted.

    claim_pending_upload defaults to 'claimed'; override its return_value to
    exercise upload_instagram.py's dispatch on the other outcomes. The decision
    logic itself lives in instagram_state.claim_pending_upload() and is covered
    directly in test_instagram_state.py.
    """
    import scripts.upload_instagram as ui
    mock_lock = mocker.MagicMock()
    mocker.patch.object(ui, "_try_acquire_upload_lock", return_value=mock_lock)
    mocker.patch.object(ui.fcntl, "flock")
    mocker.patch.object(ui.instagram_state, "get_pending_upload", return_value=None)
    mocker.patch.object(ui.instagram_state, "claim_pending_upload", return_value="claimed")
    mocker.patch.object(ui.instagram_state, "release_claim")
    mocker.patch.object(ui.instagram_state, "set_container_id")
    mocker.patch.object(ui.instagram_state, "mark_published")
    mocker.patch.object(ui.instagram_state, "mark_failed")
    mocker.patch.object(
        ui.instagram_state,
        "record_share_cleanup",
        return_value={
            "file_id": _SHARE_FILE_ID, "project_name": _PROJECT,
            "attempts": 1, "recorded_at": "2026-08-31T14:00:00Z",
        },
    )
    mocker.patch.object(ui.instagram_state, "list_share_cleanups", return_value=[])
    mocker.patch.object(ui.instagram_state, "clear_share_cleanup", return_value=True)
    mocker.patch.object(ui.instagram_state, "record_share_intent")
    # The FR-011 quarantine surface. Mocked by default so ordinary tests neither write it
    # nor read a leftover from a previous test; the tests that care override these.
    mocker.patch.object(ui.instagram_state, "mark_publish_attempted")
    mocker.patch.object(ui.instagram_state, "mark_publish_settled")
    mocker.patch.object(ui.instagram_state, "list_publish_reconciliations", return_value=[])
    mocker.patch.object(
        ui.instagram_state,
        "record_publish_reconciliation",
        return_value={
            "container_id": _CONTAINER_ID, "project_name": _PROJECT,
            "idempotency_key": _IDEM_KEY, "attempts": 1,
            "recorded_at": "2026-08-31T14:00:00Z",
        },
    )
    mocker.patch.object(ui.instagram_state, "clear_publish_reconciliation", return_value=True)
    mocker.patch.object(ui.instagram_state, "record_recovered_publish")
    mocker.patch.object(ui.instagram_logger, "log_publish_unresolved")
    mocker.patch.object(ui.instagram_logger, "log_publish_resolved")
    mocker.patch.object(ui.instagram_logger, "log_upload_recovered")
    # Cross-platform deletion coordination: default to "no other platform is waiting",
    # so the tests that don't care about coordination behave as before. The tests that
    # DO care override this, and test_dual_platform_integration.py exercises the real
    # thing end to end.
    mocker.patch.object(ui.upload_cleanup, "other_platforms_pending", return_value=[])
    mocker.patch.object(ui, "_delete_local_file")
    # A faithful fake, not a bare return_value: the real
    # drive.create_temporary_share_link() hands the caller the new file's id through
    # on_file_id BEFORE it grants the public permission, and upload_instagram.py depends
    # on that to register its cleanup obligation. A mock that skipped the callback would
    # make every revoke assertion below pass vacuously against code that never revokes.
    def _fake_share_link(video_path, on_file_id=None):
        if on_file_id is not None:
            on_file_id(_SHARE_FILE_ID)
        return _SHARE_LINK

    mocker.patch.object(
        ui.drive, "create_temporary_share_link", side_effect=_fake_share_link
    )
    mocker.patch.object(ui.drive, "revoke_share_link")
    mocker.patch.object(ui.instagram_api, "create_media_container", return_value=_CONTAINER_ID)
    mocker.patch.object(ui.instagram_api, "get_container_status", return_value="FINISHED")
    mocker.patch.object(ui.instagram_api, "publish_container", return_value=_POST_ID)
    mocker.patch.object(ui.instagram_api, "get_media_permalink", return_value=_PERMALINK)
    mocker.patch.object(ui.instagram_api.time, "sleep")
    mocker.patch.object(ui.instagram_logger, "log_upload_started")
    mocker.patch.object(ui.instagram_logger, "log_container_created")
    mocker.patch.object(ui.instagram_logger, "log_container_ready")
    mocker.patch.object(ui.instagram_logger, "log_upload_published")
    mocker.patch.object(ui.instagram_logger, "log_upload_attempt_failed")
    mocker.patch.object(ui.instagram_logger, "log_upload_exhausted")
    mocker.patch.object(ui.instagram_logger, "log_token_expired")
    mocker.patch.object(ui.telegram_api, "send_message")
    return mocker


@pytest.fixture
def with_pending(base, tmp_path):
    """Adds a real video file and a pending record referring to it."""
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    ui.instagram_state.get_pending_upload.return_value = record
    return record


# ---------------------------------------------------------------------------
# US1 — feature gate, lock, and no-work exits
# ---------------------------------------------------------------------------

def test_missing_ig_account_id_exits_silently(base, monkeypatch):
    """FR-016: an unconfigured client exercises no Instagram UPLOAD code path at all.

    Asserts on claim_pending_upload() rather than get_pending_upload(), because the two
    mean different things now. Claiming is the gateway to doing any work on a job, and is
    what must not happen. Merely READING the pending record is something the unconditional
    orphan sweep does on every tick to find out which videos are still in use — legitimate,
    unrelated to Instagram, and not what FR-016 is about. Pinning the read made this test's
    result depend on whether VIDEO_TMP_DIR happened to exist in the developer's shell.
    """
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_empty_ig_account_id_exits_silently(base, monkeypatch):
    """An empty IG_BUSINESS_ACCOUNT_ID (as shipped in .env.example) also disables the script."""
    import scripts.upload_instagram as ui
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "")
    main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_missing_ig_account_id_does_not_touch_state(base, monkeypatch):
    """The disabled path must not claim, publish, or fail any job."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_state.mark_published.assert_not_called()


def test_lock_already_held_exits_silently(base, mocker):
    """A second concurrent invocation exits before touching instagram_state."""
    import scripts.upload_instagram as ui
    mocker.patch.object(ui, "_try_acquire_upload_lock", return_value=None)
    main([])
    ui.instagram_state.get_pending_upload.assert_not_called()


def test_lock_is_released_on_exit(base, mocker):
    """The lock file object is closed once processing completes."""
    import scripts.upload_instagram as ui
    mock_lock = mocker.MagicMock()
    mocker.patch.object(ui, "_try_acquire_upload_lock", return_value=mock_lock)
    main([])
    mock_lock.close.assert_called_once()


def test_lock_file_is_instagram_specific(env, tmp_path):
    """FR-013: Instagram uses its own lock file, never upload_facebook.lock.

    Exercises the REAL _try_acquire_upload_lock (no `base` fixture, which patches it),
    so this asserts the actual on-disk lock path rather than a mock's configuration.
    """
    import scripts.upload_instagram as ui
    lock_f = ui._try_acquire_upload_lock()
    try:
        assert Path(lock_f.name).name == "upload_instagram.lock"
        assert Path(lock_f.name).parent == tmp_path / "data" / "photo-agent"
    finally:
        lock_f.close()


def test_second_lock_acquisition_is_refused(env):
    """The real lock genuinely excludes a second concurrent holder."""
    import scripts.upload_instagram as ui
    first = ui._try_acquire_upload_lock()
    try:
        assert ui._try_acquire_upload_lock() is None
    finally:
        first.close()


def test_no_pending_job_exits_silently(base):
    """No pending job means no Drive or Instagram API calls."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.create_temporary_share_link.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_no_pending_job_does_not_claim(base):
    """No pending job means the claim is never attempted."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()


# ---------------------------------------------------------------------------
# US1 — claim dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [
    "mismatch", "in_flight", "cooldown", "stale_published", "stale_failed",
])
def test_ungranted_claim_makes_no_external_calls(with_pending, outcome):
    """Anything but 'claimed' exits without a Drive or Instagram API call."""
    import scripts.upload_instagram as ui
    ui.instagram_state.claim_pending_upload.return_value = outcome
    main([])
    ui.drive.create_temporary_share_link.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()
    ui.instagram_api.publish_container.assert_not_called()


@pytest.mark.parametrize("outcome", ["mismatch", "in_flight", "cooldown"])
def test_declined_claim_does_not_resolve_the_job(with_pending, outcome):
    """A declined claim leaves the job pending for a later tick."""
    import scripts.upload_instagram as ui
    ui.instagram_state.claim_pending_upload.return_value = outcome
    main([])
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_state.mark_published.assert_not_called()


def test_claim_uses_60s_cooldown_and_3_attempts(with_pending):
    """FR-007: the retry policy is passed to the claim, not hand-rolled here."""
    import scripts.upload_instagram as ui
    main([])
    kwargs = ui.instagram_state.claim_pending_upload.call_args.kwargs
    assert kwargs["cooldown_seconds"] == 60
    assert kwargs["max_attempts"] == 3
    assert kwargs["lease_seconds"] > 300  # must outlast the container poll cap


def test_claim_uses_the_pending_records_key(with_pending):
    """The claim is made against the key the script actually observed."""
    import scripts.upload_instagram as ui
    main([])
    assert ui.instagram_state.claim_pending_upload.call_args.args[0] == _IDEM_KEY


# ---------------------------------------------------------------------------
# US1 — happy path
# ---------------------------------------------------------------------------

def test_happy_path_runs_the_full_container_flow(with_pending):
    """Share link -> container -> poll -> publish, in order."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.create_temporary_share_link.assert_called_once()
    assert ui.drive.create_temporary_share_link.call_args[0][0] == with_pending["video_local_path"]
    ui.instagram_api.create_media_container.assert_called_once_with(
        _PAGE_TOKEN, _IG_ACCOUNT_ID, _SHARE_LINK
    )
    ui.instagram_api.get_container_status.assert_called()
    ui.instagram_api.publish_container.assert_called_once_with(
        _PAGE_TOKEN, _IG_ACCOUNT_ID, _CONTAINER_ID
    )


def test_happy_path_polls_until_finished(with_pending):
    """The script keeps polling while the container is still processing."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.side_effect = [
        "IN_PROGRESS", "IN_PROGRESS", "FINISHED",
    ]
    main([])
    assert ui.instagram_api.get_container_status.call_count == 3
    ui.instagram_api.publish_container.assert_called_once()


def test_happy_path_marks_published(with_pending):
    """A successful publish is recorded terminally in state."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.mark_published.assert_called_once_with(
        _IDEM_KEY, _POST_ID, permalink=_PERMALINK
    )
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_state.release_claim.assert_not_called()


def test_happy_path_revokes_the_share_link(with_pending):
    """The temporary public link is revoked once Instagram has the video."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("drive_file_1")


def test_happy_path_sends_telegram_confirmation_with_real_permalink(with_pending):
    """FR-003: the confirmation carries the permalink fetched from the API.

    The Graph API media ID is NOT a shareable URL — a link interpolating it into
    /p/{id} does not resolve — so the message must contain the fetched permalink and
    must never contain a URL built from the media ID.
    """
    import scripts.upload_instagram as ui
    main([])
    chat_id, text = ui.telegram_api.send_message.call_args.args
    assert chat_id == _CHAT_ID
    assert _PERMALINK in text
    assert f"instagram.com/p/{_POST_ID}" not in text
    assert "Instagram" in text


def test_happy_path_fetches_the_permalink_for_the_published_media(with_pending):
    """The permalink is looked up against the media ID publish_container returned."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_api.get_media_permalink.assert_called_once_with(_PAGE_TOKEN, _POST_ID)


def test_permalink_lookup_failure_still_marks_published(with_pending):
    """The Reel is already live — a failed permalink lookup must not fail the job."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_media_permalink.side_effect = InstagramUploadError("HTTP 500")
    main([])
    ui.instagram_state.mark_published.assert_called_once_with(
        _IDEM_KEY, _POST_ID, permalink=None
    )
    ui.instagram_state.release_claim.assert_not_called()
    ui.instagram_state.mark_failed.assert_not_called()


def test_permalink_lookup_failure_does_not_fabricate_a_link(with_pending):
    """Rather than invent a URL that would 404, say the link could not be fetched."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_media_permalink.side_effect = InstagramUploadError("HTTP 500")
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "instagram.com/p/" not in text
    assert "instagram.com/reel/" not in text
    assert _POST_ID in text
    assert "Reel live on Instagram" in text


def test_permalink_token_error_is_not_fatal(with_pending):
    """A 190 on the permalink lookup alone must not undo an already-published Reel."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_media_permalink.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_state.mark_published.assert_called_once_with(
        _IDEM_KEY, _POST_ID, permalink=None
    )
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_logger.log_token_expired.assert_not_called()


def test_happy_path_logs_the_lifecycle(with_pending):
    """FR-012: started, container created, container ready, and published are all logged."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_logger.log_upload_started.assert_called_once_with(_PROJECT, 1)
    ui.instagram_logger.log_container_created.assert_called_once_with(_PROJECT, _CONTAINER_ID)
    ui.instagram_logger.log_container_ready.assert_called_once_with(_PROJECT, _CONTAINER_ID)
    ui.instagram_logger.log_upload_published.assert_called_once_with(_PROJECT, _POST_ID)


def test_happy_path_records_the_container_id_in_state(with_pending):
    """The in-flight container is persisted for operational visibility."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.set_container_id.assert_called_once_with(_IDEM_KEY, _CONTAINER_ID)


def test_happy_path_does_not_delete_the_local_video(with_pending):
    """FR-013/FR-014: the shared asset stays for upload_facebook.py to post and clean up.

    Deleting it here would break the Facebook upload for the same approved video —
    exactly the cross-platform coupling FR-013 forbids.
    """
    assert Path(with_pending["video_local_path"]).exists()
    main([])
    assert Path(with_pending["video_local_path"]).exists()


def test_happy_path_reuses_the_already_stripped_video(with_pending):
    """FR-014: the approved file is shared as-is — never re-encoded or re-processed."""
    import scripts.upload_instagram as ui
    before = Path(with_pending["video_local_path"]).read_bytes()
    main([])
    assert Path(with_pending["video_local_path"]).read_bytes() == before
    ui.drive.create_temporary_share_link.assert_called_once()
    assert ui.drive.create_temporary_share_link.call_args[0][0] == with_pending["video_local_path"]


def test_revoke_failure_does_not_lose_a_successful_publish(with_pending):
    """A failed revoke must not undo or hide a live post — the two are separate concerns."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    main([])
    ui.instagram_state.mark_published.assert_called_once_with(
        _IDEM_KEY, _POST_ID, permalink=_PERMALINK
    )


def test_revoke_failure_is_recorded_durably(with_pending):
    """A dangling public link is written down, not swallowed as an acceptable success."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    main([])
    ui.instagram_state.record_share_cleanup.assert_called_once_with(_SHARE_FILE_ID, _PROJECT)


def test_revoke_failure_alerts_the_admin_with_the_file_id(with_pending):
    """The alert names the specific Drive file, so manual cleanup is actionable."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    main([])
    texts = [c.args[1] for c in ui.telegram_api.send_message.call_args_list]
    alert = [x for x in texts if "could not remove the temporary public link" in x]
    assert len(alert) == 1
    assert _SHARE_FILE_ID in alert[0]
    assert _PROJECT in alert[0]


def test_revoke_failure_inside_alert_interval_stays_quiet(with_pending):
    """record_share_cleanup() returning None (too soon to re-alert) suppresses the message."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    ui.instagram_state.record_share_cleanup.return_value = None
    main([])
    texts = [c.args[1] for c in ui.telegram_api.send_message.call_args_list]
    assert not any("could not remove the temporary public link" in x for x in texts)


def test_cleanup_retry_reescalates_when_state_says_so(base):
    """A still-failing cleanup re-alerts whenever the state module says it is due."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = None
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 500},
    ]
    ui.drive.revoke_share_link.side_effect = RuntimeError("still down")
    ui.instagram_state.record_share_cleanup.return_value = {
        "file_id": "stale_file_1", "project_name": _PROJECT,
        "attempts": 501, "recorded_at": "2026-08-01T00:00:00Z",
    }
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "could not remove the temporary public link" in text
    assert "stale_file_1" in text
    assert "501" in text


def test_alert_wording_promises_only_what_is_delivered(with_pending):
    """The message must describe the retry + reminder behaviour that actually happens."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    main([])
    text = [c.args[1] for c in ui.telegram_api.send_message.call_args_list
            if "could not remove the temporary public link" in c.args[1]][0]
    assert "remind you daily" in text
    assert "Failed attempts:" in text
    assert "Anyone with the link" in text


def test_pending_cleanups_are_retried_every_tick(base):
    """A previously-failed revoke is retried on the next tick and cleared when it works."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("stale_file_1")
    ui.instagram_state.clear_share_cleanup.assert_called_once_with("stale_file_1")


def test_pending_cleanups_are_retried_even_with_no_job(base):
    """A dangling link outlives the job that made it — cleanup can't wait for new work."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = None
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 3},
    ]
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("stale_file_1")


def test_failed_cleanup_retry_stays_recorded(base):
    """A retry that fails again keeps the entry for the following tick."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = None
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    ui.drive.revoke_share_link.side_effect = RuntimeError("still down")
    main([])
    ui.instagram_state.clear_share_cleanup.assert_not_called()
    ui.instagram_state.record_share_cleanup.assert_called_once_with("stale_file_1", _PROJECT)


# ---------------------------------------------------------------------------
# Cleanup must not be gated on Instagram being configured (gap 2a)
# ---------------------------------------------------------------------------

def test_cleanup_drains_when_instagram_is_disabled(base, monkeypatch):
    """Clearing IG_BUSINESS_ACCOUNT_ID must not strand an already-dangling public link."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("stale_file_1")
    ui.instagram_state.clear_share_cleanup.assert_called_once_with("stale_file_1")


def test_cleanup_drains_when_page_token_is_missing(base, monkeypatch):
    """An expired/removed Meta token must not strand cleanup either — Drive is separate."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1  # still reports the misconfiguration...
    ui.drive.revoke_share_link.assert_called_once_with("stale_file_1")  # ...but cleans up first


def test_cleanup_still_drains_with_instagram_disabled_and_no_token(base, monkeypatch):
    """Neither Instagram config nor a Meta token is required to revoke a Drive permission."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 9},
    ]
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("stale_file_1")


def test_disabled_instagram_still_publishes_nothing(base, monkeypatch):
    """Ungating cleanup must not accidentally ungate publishing (FR-016)."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()
    ui.instagram_api.publish_container.assert_not_called()


def test_cleanup_is_skipped_when_the_lock_is_held(base, mocker):
    """A concurrent instance already owns the drain — don't double-revoke."""
    import scripts.upload_instagram as ui
    mocker.patch.object(ui, "_try_acquire_upload_lock", return_value=None)
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    main([])
    ui.drive.revoke_share_link.assert_not_called()


@pytest.mark.parametrize("var", ["FIELDKIT_DATA_DIR", "FIELDKIT_LOG_DIR"])
def test_missing_fieldkit_dirs_still_exit_1_before_cleanup(base, monkeypatch, var):
    """The data/log dirs gate everything — cleanup reads and writes state too."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1
    ui.drive.revoke_share_link.assert_not_called()


def test_cleanup_drain_runs_before_the_upload(with_pending):
    """Cleanup is attempted even on a tick that also publishes."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_share_cleanups.return_value = [
        {"file_id": "stale_file_1", "project_name": _PROJECT, "attempts": 1},
    ]
    main([])
    revoked = [c.args[0] for c in ui.drive.revoke_share_link.call_args_list]
    assert "stale_file_1" in revoked
    assert _SHARE_FILE_ID in revoked


def test_no_telegram_chat_id_still_publishes(with_pending, monkeypatch):
    """A missing chat id degrades the notification, not the publish."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)
    main([])
    ui.instagram_state.mark_published.assert_called_once()
    ui.telegram_api.send_message.assert_not_called()


def test_telegram_failure_does_not_raise(with_pending):
    """A Telegram outage must not crash the cron script after a successful publish."""
    import scripts.upload_instagram as ui
    ui.telegram_api.send_message.side_effect = RuntimeError("Telegram HTTP error 500")
    main([])  # must not raise
    ui.instagram_state.mark_published.assert_called_once()


# ---------------------------------------------------------------------------
# US1 — missing video file
# ---------------------------------------------------------------------------

def test_missing_video_marks_failed_without_api_calls(base):
    """A vanished video file fails the job without touching Drive or Instagram."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = dict(_PENDING_RECORD)
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.drive.create_temporary_share_link.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


# ---------------------------------------------------------------------------
# US1 — env validation
# ---------------------------------------------------------------------------

def test_missing_page_token_exits_1(base, monkeypatch):
    """FB_PAGE_ACCESS_TOKEN is required — Instagram publishing reuses it."""
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


@pytest.mark.parametrize("var", ["FIELDKIT_DATA_DIR", "FIELDKIT_LOG_DIR"])
def test_missing_fieldkit_dirs_exit_1(base, monkeypatch, var):
    """The per-client data/log dirs are required, matching every other entrypoint."""
    monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


def test_env_validation_runs_before_any_state_access(base, monkeypatch):
    """A misconfigured environment fails fast, without claiming a job."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main([])
    ui.instagram_state.claim_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_disabled_client_exits_before_token_validation(base, monkeypatch):
    """FR-016 outranks env validation: an unconfigured client exits 0, not 1."""
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    main([])  # must not raise SystemExit


def test_source_argument_is_accepted(with_pending):
    """--source cron is accepted as an informational label."""
    import scripts.upload_instagram as ui
    main(["--source", "cron"])
    ui.instagram_state.mark_published.assert_called_once()


# ---------------------------------------------------------------------------
# US3 (T016) — retry, exhaustion, token expiry, container timeout
# ---------------------------------------------------------------------------

@pytest.fixture
def failing(with_pending):
    """A pending job whose container creation always fails transiently."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")
    return with_pending


def _set_attempt(record, n):
    """Set the record's PRE-claim attempt_count, i.e. n attempts already made."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = dict(record, attempt_count=n)


# --- transient failure, retries remaining ---

def test_transient_failure_releases_the_claim(failing):
    """FR-007: a retryable failure releases the claim so the next tick can retry."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)
    ui.instagram_state.mark_failed.assert_not_called()


def test_transient_failure_revokes_the_share_link(failing):
    """The public link is revoked even when the attempt fails."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("drive_file_1")


def test_transient_failure_sends_no_alert(failing):
    """SC-003: the owner is not alerted while automatic recovery is still possible."""
    import scripts.upload_instagram as ui
    main([])
    ui.telegram_api.send_message.assert_not_called()


def test_transient_failure_is_logged(failing):
    """FR-012: each failed attempt is recorded with its attempt number."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_logger.log_upload_attempt_failed.assert_called_once()
    args = ui.instagram_logger.log_upload_attempt_failed.call_args.args
    assert args[0] == _PROJECT
    assert args[1] == 1
    ui.instagram_logger.log_upload_exhausted.assert_not_called()


@pytest.mark.parametrize("failing_step,exc", [
    ("create_media_container", InstagramUploadError("create failed")),
    ("publish_container", InstagramUploadError("publish failed")),
])
def test_failure_at_any_step_releases_the_claim(with_pending, failing_step, exc):
    """A failure at container creation or publish is handled identically."""
    import scripts.upload_instagram as ui
    getattr(ui.instagram_api, failing_step).side_effect = exc
    main([])
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)


def test_poll_error_status_releases_the_claim(with_pending):
    """A container reporting ERROR is an ordinary retryable failure."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "ERROR"
    main([])
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)
    ui.instagram_api.publish_container.assert_not_called()


def test_drive_share_failure_is_treated_as_transient(with_pending):
    """A Drive failure is as retryable as an Instagram one — and calls no Instagram API."""
    import scripts.upload_instagram as ui
    ui.drive.create_temporary_share_link.side_effect = RuntimeError("Drive upload failed: HTTP 500")
    main([])
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)
    ui.instagram_api.create_media_container.assert_not_called()


def test_drive_share_failure_revokes_nothing(with_pending):
    """No link was created, so there is nothing to revoke."""
    import scripts.upload_instagram as ui
    ui.drive.create_temporary_share_link.side_effect = RuntimeError("Drive upload failed")
    main([])
    ui.drive.revoke_share_link.assert_not_called()


# --- cooldown ---

def test_cooldown_makes_no_api_calls(with_pending):
    """FR-007: a retry inside the 60s cooldown does nothing and exits silently."""
    import scripts.upload_instagram as ui
    ui.instagram_state.claim_pending_upload.return_value = "cooldown"
    main([])
    ui.drive.create_temporary_share_link.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()
    ui.telegram_api.send_message.assert_not_called()


def test_cooldown_does_not_consume_an_attempt(with_pending):
    """A declined claim leaves the attempt budget untouched."""
    import scripts.upload_instagram as ui
    ui.instagram_state.claim_pending_upload.return_value = "cooldown"
    main([])
    ui.instagram_logger.log_upload_started.assert_not_called()


# --- exhaustion ---

def test_third_failed_attempt_marks_failed(failing, with_pending):
    """FR-007: after the 3rd attempt fails, the job is terminal."""
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 2)  # this attempt is the 3rd
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.instagram_state.release_claim.assert_not_called()


def test_third_failed_attempt_logs_exhausted(failing, with_pending):
    """FR-012: exhaustion is its own log event."""
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 2)
    main([])
    ui.instagram_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)


def test_third_failed_attempt_alerts_the_owner(failing, with_pending):
    """FR-009: the owner is told the Instagram upload failed for good."""
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 2)
    main([])
    chat_id, text = ui.telegram_api.send_message.call_args.args
    assert chat_id == _CHAT_ID
    assert "Instagram upload failed" in text


def test_exhausted_claim_alerts_and_makes_no_api_calls(with_pending):
    """A claim already past the budget alerts without another upload attempt."""
    import scripts.upload_instagram as ui
    ui.instagram_state.claim_pending_upload.return_value = "exhausted"
    main([])
    ui.instagram_api.create_media_container.assert_not_called()
    ui.instagram_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)
    assert "Instagram upload failed" in ui.telegram_api.send_message.call_args.args[1]


def test_second_failed_attempt_still_retries(failing, with_pending):
    """Only the 3rd failure is terminal — the 2nd still releases for retry."""
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 1)
    main([])
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)
    ui.instagram_state.mark_failed.assert_not_called()
    ui.telegram_api.send_message.assert_not_called()


def test_three_successive_failures_end_in_failed_with_alert(base, tmp_path):
    """SC-003/FR-009: three cron ticks, three failures, then one alert.

    Drives three separate claim -> attempt -> release cycles the way the cron would,
    advancing attempt_count between them exactly as claim_pending_upload() does.
    """
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")

    for already_attempted in (0, 1, 2):
        ui.instagram_state.get_pending_upload.return_value = dict(
            record, attempt_count=already_attempted
        )
        main([])

    assert ui.instagram_state.release_claim.call_count == 2  # attempts 1 and 2
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.instagram_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)
    assert ui.telegram_api.send_message.call_count == 1
    assert "Instagram upload failed" in ui.telegram_api.send_message.call_args.args[1]


def test_retry_success_after_failure_sends_no_alert(base, tmp_path):
    """SC-003: a retry that succeeds gets the normal confirmation and no alert."""
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))

    ui.instagram_state.get_pending_upload.return_value = dict(record, attempt_count=0)
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")
    main([])

    ui.instagram_api.create_media_container.side_effect = None
    ui.instagram_api.create_media_container.return_value = _CONTAINER_ID
    ui.instagram_state.get_pending_upload.return_value = dict(record, attempt_count=1)
    main([])

    ui.instagram_state.mark_published.assert_called_once_with(
        _IDEM_KEY, _POST_ID, permalink=_PERMALINK
    )
    ui.instagram_logger.log_upload_exhausted.assert_not_called()
    assert ui.telegram_api.send_message.call_count == 1
    assert _PERMALINK in ui.telegram_api.send_message.call_args.args[1]


# --- token expiry ---

def test_token_error_marks_failed_immediately(with_pending):
    """FR-008: token expiry is terminal after one attempt — retrying cannot help."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.instagram_state.release_claim.assert_not_called()


def test_token_error_does_not_consume_the_retry_budget(with_pending):
    """A token failure is not logged as a retryable attempt failure."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_logger.log_upload_attempt_failed.assert_not_called()
    ui.instagram_logger.log_upload_exhausted.assert_not_called()


def test_token_error_logs_token_expired(with_pending):
    """FR-012: token expiry has its own log event."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_logger.log_token_expired.assert_called_once_with(_PROJECT)


def test_token_error_alerts_the_owner_to_reconnect(with_pending):
    """FR-008: the alert tells the owner to reconnect, not just that it failed."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "Instagram token expired" in text
    assert _PROJECT in text


def test_token_error_revokes_the_share_link(with_pending):
    """Even on the terminal token path, nothing is left publicly reachable."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("drive_file_1")


def test_token_error_during_poll_is_terminal(with_pending):
    """A 190 surfacing mid-poll takes the token path, not the retry path."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.instagram_logger.log_token_expired.assert_called_once_with(_PROJECT)


def test_token_error_during_publish_is_terminal(with_pending):
    """A 190 surfacing at publish takes the token path too."""
    import scripts.upload_instagram as ui
    ui.instagram_api.publish_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    ui.instagram_logger.log_token_expired.assert_called_once_with(_PROJECT)


# --- container poll timeout ---

def test_stuck_container_times_out_and_retries(with_pending):
    """A container that never finishes is handled like any other transient failure."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "IN_PROGRESS"
    main([])
    ui.instagram_api.publish_container.assert_not_called()
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)
    ui.drive.revoke_share_link.assert_called_once_with("drive_file_1")


def test_stuck_container_polls_the_full_300_second_cap(with_pending):
    """spec.md's stuck-container edge case is bounded at 60 attempts x 5s."""
    import scripts.upload_instagram as ui
    import tools.instagram_api as api
    ui.instagram_api.get_container_status.return_value = "IN_PROGRESS"
    main([])
    assert ui.instagram_api.get_container_status.call_count == api._MAX_POLL_ATTEMPTS
    assert api._MAX_POLL_ATTEMPTS * api._POLL_INTERVAL_SECONDS == 300


def test_stuck_container_on_final_attempt_exhausts(with_pending):
    """A timeout on the 3rd attempt ends the job and alerts, like any other failure."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "IN_PROGRESS"
    _set_attempt(with_pending, 2)
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    assert "Instagram upload failed" in ui.telegram_api.send_message.call_args.args[1]


def test_stuck_container_logs_the_timeout_reason(with_pending):
    """The logged error names the timeout, so the log explains the failure."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "IN_PROGRESS"
    main([])
    error_text = ui.instagram_logger.log_upload_attempt_failed.call_args.args[2]
    assert "did not finish processing" in error_text


# --- platform independence (FR-013) ---

def test_instagram_failure_never_touches_facebook_state(failing, mocker):
    """FR-013: the Instagram failure path imports and mutates no Facebook state."""
    import scripts.upload_instagram as ui
    import tools.facebook_state as fb_state
    for name in ("claim_pending_upload", "mark_failed", "mark_published", "release_claim"):
        mocker.patch.object(fb_state, name)
    main([])
    for name in ("claim_pending_upload", "mark_failed", "mark_published", "release_claim"):
        getattr(fb_state, name).assert_not_called()


def test_upload_instagram_does_not_import_facebook_modules():
    """FR-013 structurally: this script has no Facebook dependency to couple through."""
    import scripts.upload_instagram as ui
    assert not hasattr(ui, "facebook_state")
    assert not hasattr(ui, "facebook_api")
    assert not hasattr(ui, "facebook_logger")


def test_instagram_failure_leaves_the_shared_video_for_facebook(failing):
    """FR-013/FR-014: a failed Instagram attempt must not delete the shared asset."""
    assert Path(failing["video_local_path"]).exists()
    main([])
    assert Path(failing["video_local_path"]).exists()


# ---------------------------------------------------------------------------
# Cross-platform cleanup coordination (Feature 005 fix)
# ---------------------------------------------------------------------------

def test_does_not_delete_while_facebook_job_is_outstanding(with_pending, mocker):
    """Instagram must not delete a file Facebook's pending job still needs."""
    import scripts.upload_instagram as ui
    ui.upload_cleanup.other_platforms_pending.return_value = ["facebook"]
    main([])
    ui._delete_local_file.assert_not_called()
    ui.instagram_state.mark_published.assert_called_once()


def test_deletes_once_facebook_has_resolved(with_pending):
    """When Facebook is already done, Instagram is last out and cleans up."""
    import scripts.upload_instagram as ui
    main([])
    ui._delete_local_file.assert_called_once_with(with_pending["video_local_path"], _PROJECT)


def test_coordination_check_happens_after_mark_published(with_pending):
    """Our own terminal state must be durable before we read the other platform's."""
    import scripts.upload_instagram as ui
    calls = []
    ui.instagram_state.mark_published.side_effect = lambda *a, **k: calls.append("mark")
    ui.upload_cleanup.other_platforms_pending.side_effect = (
        lambda *a, **k: calls.append("check") or []
    )
    main([])
    assert calls == ["mark", "check"]


def test_exhausted_job_also_releases_the_file(failing, with_pending):
    """A terminally failed Instagram job must not strand the file for Facebook either."""
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 2)
    main([])
    ui.instagram_state.mark_failed.assert_called_once()
    ui._delete_local_file.assert_called_once()


def test_retryable_failure_keeps_the_file(failing):
    """A retry still needs the video — only a terminal outcome may release it."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.release_claim.assert_called_once()
    ui._delete_local_file.assert_not_called()


def test_token_failure_releases_the_file(with_pending):
    """Token expiry is terminal after one attempt, so it releases the file too."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("expired")
    main([])
    ui.instagram_state.mark_failed.assert_called_once()
    ui._delete_local_file.assert_called_once()


def test_missing_video_alerts_instead_of_failing_silently(base):
    """The bug's symptom, made loud: a missing file must not fail silently."""
    import scripts.upload_instagram as ui
    ui.instagram_state.get_pending_upload.return_value = dict(_PENDING_RECORD)
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "Instagram upload failed" in text
    assert "missing on disk" in text


# ---------------------------------------------------------------------------
# FR-011 — a crash between publish and mark_published must not duplicate a Reel
# ---------------------------------------------------------------------------
#
# publish_container() is the irreversible external side effect; mark_published() is
# the durable record of it. A crash, kill, or lost HTTP response in between leaves
# Meta holding a live Reel this system has no record of. The re-entrancy lock cannot
# help — the holder is already dead. So the container id survives across attempts and
# is reconciled against Instagram's own view before anything is published.

@pytest.fixture
def after_interrupted_publish(base, tmp_path):
    """A pending job left holding the container id of an attempt that was interrupted."""
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(
        _PENDING_RECORD,
        video_local_path=str(video),
        container_id=_CONTAINER_ID,
        attempt_count=1,
    )
    ui.instagram_state.get_pending_upload.return_value = record
    ui.instagram_state.record_recovered_publish = base.MagicMock()
    ui.instagram_logger.log_upload_recovered = base.MagicMock()
    return record


def test_a_container_already_published_is_never_published_again(after_interrupted_publish):
    """THE duplicate-post guard. A duplicate Reel on a client account is irreversible."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    ui.instagram_api.publish_container.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_an_already_published_container_is_recorded_as_published(after_interrupted_publish):
    """Not republishing is only half of it — the key must be retired, or a re-approval duplicates."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    ui.instagram_state.record_recovered_publish.assert_called_once_with(
        _IDEM_KEY, _PROJECT, _CONTAINER_ID
    )
    ui.instagram_logger.log_upload_recovered.assert_called_once_with(_PROJECT, _CONTAINER_ID)


def test_a_recovered_publish_releases_the_shared_video(after_interrupted_publish):
    """The job is terminal and successful, so it takes the success path in every respect."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    ui._delete_local_file.assert_called_once()


def test_a_recovered_publish_tells_the_owner_the_truth(after_interrupted_publish):
    """The Reel IS live, and FieldKit cannot link to it — both facts have to be said.

    Claiming a normal success would be a lie (there is no link), and reporting a failure
    would invite a re-approval, which is exactly how the duplicate gets posted.
    """
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "live on Instagram" in text
    assert "Nothing was posted twice" in text


def test_a_finished_container_is_reused_rather_than_rebuilt(after_interrupted_publish):
    """Ingested but not published: publishing THIS container is the duplicate-free finish."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    main([])
    ui.instagram_api.create_media_container.assert_not_called()
    ui.drive.create_temporary_share_link.assert_not_called()
    ui.instagram_api.publish_container.assert_called_once_with(
        _PAGE_TOKEN, _IG_ACCOUNT_ID, _CONTAINER_ID
    )


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED"])
def test_a_dead_container_is_discarded_and_rebuilt(after_interrupted_publish, status):
    """Definitively never published and no longer usable, so a fresh container is safe."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.side_effect = [status, "FINISHED"]
    main([])
    ui.instagram_api.create_media_container.assert_called_once()
    ui.instagram_state.mark_published.assert_called_once()


@pytest.mark.parametrize("status", ["IN_PROGRESS", "SOMETHING_META_ADDED_LATER", None])
def test_an_undetermined_container_is_never_replaced_by_a_new_one(after_interrupted_publish, status):
    """The strict reading: not knowing a container's fate is never grounds for a second one.

    An unrecognised status could be a publish state Meta has added since this was written,
    so treating it as "safe to build a new container" could duplicate a post. Failing the
    attempt costs a retry from a bounded budget; guessing costs a client a duplicate Reel.
    """
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.return_value = status
    main([])
    ui.instagram_api.create_media_container.assert_not_called()
    ui.instagram_api.publish_container.assert_not_called()
    ui.instagram_state.release_claim.assert_called_once_with(_IDEM_KEY)


def test_an_unreachable_container_status_fails_the_attempt_instead_of_publishing(
    after_interrupted_publish,
):
    """A network failure reconciling is still "unknown", and unknown must not publish."""
    import scripts.upload_instagram as ui
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("network down")
    main([])
    ui.instagram_api.publish_container.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_a_job_with_no_prior_container_is_unaffected(with_pending):
    """The ordinary first attempt must not pay for any of this."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.create_temporary_share_link.assert_called_once()
    ui.instagram_api.create_media_container.assert_called_once()
    ui.instagram_state.mark_published.assert_called_once()


def test_an_exhausted_job_still_reconciles_its_last_container(base, tmp_path):
    """The final attempt can crash after publishing exactly like any other.

    claim_pending_upload() has already cleared the record by the time this is reached, so
    if the check were skipped here a live Reel would go unrecorded and its key unretired —
    and the owner, told it failed, would re-approve and post a duplicate.
    """
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    ui.instagram_state.get_pending_upload.return_value = dict(
        _PENDING_RECORD, video_local_path=str(video), container_id=_CONTAINER_ID,
        attempt_count=3, publish_attempted_at="2026-08-31T14:05:00Z",
    )
    ui.instagram_state.claim_pending_upload.return_value = "exhausted"
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"

    main([])
    ui.instagram_state.record_recovered_publish.assert_called_once_with(
        _IDEM_KEY, _PROJECT, _CONTAINER_ID
    )
    ui.instagram_logger.log_upload_exhausted.assert_not_called()


def test_an_exhausted_job_that_cannot_be_reconciled_warns_about_a_possible_live_reel(
    base, tmp_path
):
    """"Failed" and "might be live" call for very different follow-up.

    An owner who believes nothing was posted will re-approve. If the last attempt did
    reach publish and only lost its response, that re-approval is how the duplicate lands.
    """
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    ui.instagram_state.get_pending_upload.return_value = dict(
        _PENDING_RECORD, video_local_path=str(video), container_id=_CONTAINER_ID,
        attempt_count=3, publish_attempted_at="2026-08-31T14:05:00Z",
    )
    ui.instagram_state.claim_pending_upload.return_value = "exhausted"
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("network down")

    main([])
    # The control is the durable quarantine, not the message. On this path the ENTRY is
    # created inside claim_pending_upload()'s transaction (mocked here); what
    # _handle_exhausted() adds is the check that failed, the activity-log line, and the
    # alert. test_dual_platform_integration.py exercises the atomic insert for real.
    ui.instagram_state.record_publish_reconciliation.assert_called_once_with(
        _CONTAINER_ID, project_name=_PROJECT, idempotency_key=_IDEM_KEY
    )
    ui.instagram_logger.log_publish_unresolved.assert_called_once_with(_PROJECT, _CONTAINER_ID)
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" in text
    assert "blocked" in text
    ui.instagram_logger.log_upload_exhausted.assert_called_once()


def test_an_ordinary_exhaustion_does_not_cry_wolf(base, tmp_path):
    """A job that never reached publish must keep the plain, actionable failure message."""
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    ui.instagram_state.get_pending_upload.return_value = dict(
        _PENDING_RECORD, video_local_path=str(video), attempt_count=3
    )
    ui.instagram_state.claim_pending_upload.return_value = "exhausted"
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" not in text
    assert "check logs" in text


def test_a_lost_publish_response_that_cannot_be_settled_is_quarantined(with_pending):
    """The defect round 2 left open: a publish lands, and nothing can confirm it.

    Three attempts exhaust without Instagram ever giving a definitive answer. Without a
    durable quarantine, mark_failed() would discard the container id, a re-approval would
    be accepted, and a second Reel would go onto the client's real account.
    """
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2          # this attempt is the third and last
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    # The settle call at terminal time cannot get an answer either.
    ui.instagram_api.get_container_status.side_effect = [
        "FINISHED",                                  # the pre-publish poll
        InstagramUploadError("graph api unavailable"),  # the terminal settle
    ]
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_called_once_with(
        _CONTAINER_ID, project_name=_PROJECT, idempotency_key=_IDEM_KEY
    )
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" in text
    assert "blocked" in text


def test_a_lost_publish_response_that_instagram_settles_is_not_quarantined(with_pending):
    """A definitive FINISHED means the publish did NOT land — an ordinary failure.

    Quarantining here would block a re-approval for no reason. Instagram's own word for
    "ingested but not published" is authoritative, so the owner is told plainly that
    nothing went live and the video can be re-approved.
    """
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_not_called()
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" not in text
    assert "can be re-approved" in text


def test_a_terminal_failure_that_settles_as_published_is_recorded_not_failed(with_pending):
    """If the settle call says PUBLISHED, the job succeeded — however the attempt went."""
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    ui.instagram_api.get_container_status.side_effect = ["FINISHED", "PUBLISHED"]
    main([])
    ui.instagram_state.record_recovered_publish.assert_called_once_with(
        _IDEM_KEY, _PROJECT, _CONTAINER_ID
    )
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_state.record_publish_reconciliation.assert_not_called()


def test_a_failure_before_publish_does_not_warn_on_the_final_attempt(with_pending):
    """Only a failure at or after the publish call is ambiguous."""
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("HTTP 500")
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" not in text


# ---------------------------------------------------------------------------
# The queued account is the one that gets published to
# ---------------------------------------------------------------------------

def test_a_changed_account_configuration_cancels_the_job(with_pending, monkeypatch):
    """Reconfiguring a client between approval and publish must not post to a new account.

    The video was approved FOR a specific Instagram account. Publishing it to whatever
    IG_BUSINESS_ACCOUNT_ID happens to hold at cron time would be a wrong, irreversible
    post on someone's real account.
    """
    import scripts.upload_instagram as ui
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "17841499999999999")
    main([])
    ui.instagram_api.create_media_container.assert_not_called()
    ui.instagram_api.publish_container.assert_not_called()


def test_an_account_mismatch_is_terminal_and_alerts(with_pending, monkeypatch):
    """Failed loudly rather than silently preferring either value.

    Neither value can be shown to be the intended one, so the only correct action is to
    publish nothing and say so — naming both accounts, since that is what the owner needs
    in order to decide.
    """
    import scripts.upload_instagram as ui
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "17841499999999999")
    main([])
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "cancelled" in text
    assert _IG_ACCOUNT_ID in text
    assert "17841499999999999" in text
    assert "Nothing was published" in text


def test_an_account_mismatch_releases_the_shared_video(with_pending, monkeypatch):
    """Terminal is terminal: the other platform must not wait on this job forever."""
    import scripts.upload_instagram as ui
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "17841499999999999")
    main([])
    ui._delete_local_file.assert_called_once()


def test_a_job_with_no_recorded_account_is_cancelled(with_pending):
    """A record that cannot name its target account is not safe to publish either."""
    import scripts.upload_instagram as ui
    with_pending["ig_business_account_id"] = ""
    main([])
    ui.instagram_api.publish_container.assert_not_called()
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)


def test_the_queued_account_is_the_one_published_to(with_pending):
    """When they agree, it is still the RECORD's value that is used, not the env's."""
    import scripts.upload_instagram as ui
    main([])
    assert ui.instagram_api.create_media_container.call_args.args[1] == _IG_ACCOUNT_ID
    assert ui.instagram_api.publish_container.call_args.args[1] == _IG_ACCOUNT_ID


# ---------------------------------------------------------------------------
# The share-link cleanup obligation is recorded before the link can exist
# ---------------------------------------------------------------------------

def test_the_cleanup_obligation_is_registered_before_the_file_is_shared(with_pending, mocker):
    """drive.create_temporary_share_link hands over the id before granting the permission."""
    import scripts.upload_instagram as ui
    intent = mocker.patch.object(ui.instagram_state, "record_share_intent")
    main([])
    intent.assert_called_once_with(_SHARE_FILE_ID, _PROJECT)


def test_a_share_call_that_raises_after_creating_the_permission_is_still_revocable(
    with_pending, mocker
):
    """The unrecoverable case, made recoverable.

    If the permission POST succeeds server-side and its response is then lost, the link is
    real, the call raises, and a caller that only learns the id from the returned URL holds
    nothing to revoke — an untracked public link, forever. The id arrives through the
    callback before any of that, so the revoke still happens.
    """
    import scripts.upload_instagram as ui
    intent = mocker.patch.object(ui.instagram_state, "record_share_intent")

    def _share_then_lose_the_response(video_path, on_file_id=None):
        on_file_id(_SHARE_FILE_ID)                    # file exists, still private
        raise RuntimeError("Drive share permission request failed: read timed out")

    ui.drive.create_temporary_share_link.side_effect = _share_then_lose_the_response
    main([])
    intent.assert_called_once_with(_SHARE_FILE_ID, _PROJECT)
    ui.drive.revoke_share_link.assert_called_once_with(_SHARE_FILE_ID)


def test_a_successful_revoke_retires_the_obligation(with_pending):
    """Otherwise the drain would keep retrying a link that is already gone, forever."""
    import scripts.upload_instagram as ui
    main([])
    ui.instagram_state.clear_share_cleanup.assert_called_once_with(_SHARE_FILE_ID)


def test_a_failed_revoke_does_not_retire_the_obligation(with_pending):
    """A link that is still public must stay on the list until it really is not."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke failed: HTTP 503")
    main([])
    ui.instagram_state.clear_share_cleanup.assert_not_called()
    ui.instagram_state.record_share_cleanup.assert_called_once_with(_SHARE_FILE_ID, _PROJECT)


def test_an_upload_that_never_shares_anything_records_no_obligation(with_pending, mocker):
    """No file, no exposure, no entry to clear."""
    import scripts.upload_instagram as ui
    intent = mocker.patch.object(ui.instagram_state, "record_share_intent")
    ui.drive.create_temporary_share_link.side_effect = RuntimeError("Drive upload failed: HTTP 500")
    main([])
    intent.assert_not_called()
    ui.drive.revoke_share_link.assert_not_called()


# ---------------------------------------------------------------------------
# Deployment heartbeat and the orphan sweep
# ---------------------------------------------------------------------------

def test_every_tick_stamps_a_deployment_heartbeat(base, isolated_worker_health):
    """The heartbeat attests the cron ENTRY fired — that is what check_approval.py reads."""
    main([])
    assert isolated_worker_health.is_deployed("instagram") is True


def test_the_heartbeat_is_stamped_even_when_instagram_is_disabled(base, monkeypatch,
                                                                 isolated_worker_health):
    """"Deployed" and "enabled" are different facts and must not be collapsed.

    A client with Instagram switched off still proves its cron is installed, so switching
    the feature ON later works immediately instead of waiting a tick for the first approval
    to be refused.
    """
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    assert isolated_worker_health.is_deployed("instagram") is True


def test_the_heartbeat_is_stamped_before_the_lock_is_even_attempted(base, mocker,
                                                                   isolated_worker_health):
    """A tick that loses the lock race still fired, and still proves deployment."""
    import scripts.upload_instagram as ui
    mocker.patch.object(ui, "_try_acquire_upload_lock", return_value=None)
    main([])
    assert isolated_worker_health.is_deployed("instagram") is True


def test_every_tick_runs_the_orphan_sweep(base, mocker):
    """Recovery for files the coordinated delete could not reach — see tools/upload_cleanup.py."""
    import scripts.upload_instagram as ui
    sweep = mocker.patch.object(ui.upload_cleanup, "sweep_orphaned_videos", return_value=[])
    main([])
    sweep.assert_called_once()


def test_the_sweep_runs_even_when_instagram_is_disabled(base, mocker, monkeypatch):
    """Like the share-link drain, it exists to catch what no job will ever carry."""
    import scripts.upload_instagram as ui
    sweep = mocker.patch.object(ui.upload_cleanup, "sweep_orphaned_videos", return_value=[])
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    sweep.assert_called_once()


# ---------------------------------------------------------------------------
# End to end: a token can never reach the durable activity log
# ---------------------------------------------------------------------------

def test_a_token_bearing_api_error_never_reaches_the_activity_log(with_pending, mocker, tmp_path,
                                                                 caplog):
    """The whole leak path, through the real logger: exception -> error message -> log file.

    The per-function defences are tested in test_instagram_api.py and
    test_instagram_logger.py. This one wires the real writer into a real upload failure,
    because the two could each be correct while the path between them is not.
    """
    import scripts.upload_instagram as ui
    import tools.instagram_logger as ig_logger
    log_dir = tmp_path / "real_logs"
    mocker.patch.object(ig_logger, "LOG_DIR", log_dir)
    mocker.patch.object(ig_logger, "LOG_FILE", log_dir / "photo-agent.log")
    mocker.patch.object(ui.instagram_logger, "log_upload_attempt_failed",
                        _real_log_attempt_failed)

    leaked = "EAABsbCS1iHgBO7ZAZCxyzQWERTY1234567890abcdefGHIJKLmnop"
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError(
        "Container creation request failed: HTTPSConnectionPool(host='graph.facebook.com', "
        f"port=443): url /v25.0/media?access_token={leaked}"
    )
    main([])

    written = (log_dir / "photo-agent.log").read_text()
    assert "IG_FAILED" in written            # the failure really was logged
    assert leaked not in written
    assert "***REDACTED***" in written

    # The activity log is the durable sink, but it is not the only one: the same
    # exception text is emitted to stderr, which under cron becomes mail or a captured
    # job log. Both have to be clean.
    assert leaked not in caplog.text


def test_a_token_that_expires_at_the_publish_step_warns_about_a_possible_live_reel(with_pending):
    """The one case reconciliation cannot rescue, so the owner becomes the check.

    Asking Instagram what became of the container is exactly the call that just returned
    "your token is invalid", and mark_failed() then discards the container id — so nothing
    will ever check again. Telling the owner before they re-approve is all that is left.
    """
    import scripts.upload_instagram as ui
    ui.instagram_api.publish_container.side_effect = InstagramTokenError("code 190")
    main([])
    # No classification is even attempted — the call that would answer is the one that
    # just failed — so this quarantines directly and lets the drain ask once the Page
    # is reconnected.
    ui.instagram_state.record_publish_reconciliation.assert_called_once_with(
        _CONTAINER_ID, project_name=_PROJECT, idempotency_key=_IDEM_KEY
    )
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "token expired" in text
    assert "MAY already be live" in text
    assert "blocked" in text


def test_a_token_that_expires_before_the_publish_step_does_not_cry_wolf(with_pending):
    """Nothing was asked of Meta, so there is nothing ambiguous to warn about."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramTokenError("code 190")
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_not_called()
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "token expired" in text
    assert "MAY already be live" not in text


# ---------------------------------------------------------------------------
# The quarantine drain — keep asking until Instagram is definitive
# ---------------------------------------------------------------------------

_QUARANTINE_ENTRY = {
    "container_id": _CONTAINER_ID,
    "project_name": _PROJECT,
    "idempotency_key": _IDEM_KEY,
    "recorded_at": "2026-08-31T14:00:00Z",
    "last_attempt_at": "2026-08-31T14:00:00Z",
    "last_alerted_at": "2026-08-31T14:00:00Z",
    "attempts": 1,
}


@pytest.fixture
def with_quarantine(base):
    """One container whose publish outcome is unknown, and no pending job."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [dict(_QUARANTINE_ENTRY)]
    return ui


def test_the_drain_resolves_a_container_instagram_reports_as_published(with_quarantine):
    """The Reel was live all along: recorded, key retired, quarantine lifted."""
    ui = with_quarantine
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    ui.instagram_state.record_recovered_publish.assert_called_once_with(
        _IDEM_KEY, _PROJECT, _CONTAINER_ID
    )
    ui.instagram_state.clear_publish_reconciliation.assert_called_once_with(_CONTAINER_ID)
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "IS live" in text
    assert "posted twice" in text


@pytest.mark.parametrize("status", ["FINISHED", "ERROR", "EXPIRED"])
def test_the_drain_releases_a_container_that_never_published(with_quarantine, status):
    """All three mean the same thing — it never went live — so the key is released."""
    ui = with_quarantine
    ui.instagram_api.get_container_status.return_value = status
    main([])
    ui.instagram_state.clear_publish_reconciliation.assert_called_once_with(_CONTAINER_ID)
    ui.instagram_state.record_recovered_publish.assert_not_called()
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "NOT published" in text
    assert "re-approve" in text


@pytest.mark.parametrize("status", ["IN_PROGRESS", "SOMETHING_NEW"])
def test_the_drain_keeps_an_undetermined_container_quarantined(with_quarantine, status):
    """Anything short of a definitive answer leaves the block in place. That is the point."""
    ui = with_quarantine
    ui.instagram_api.get_container_status.return_value = status
    main([])
    ui.instagram_state.clear_publish_reconciliation.assert_not_called()
    ui.instagram_state.record_publish_reconciliation.assert_called_once_with(
        _CONTAINER_ID, project_name=_PROJECT, idempotency_key=_IDEM_KEY
    )


def test_the_drain_keeps_an_unreachable_container_quarantined(with_quarantine):
    """A Graph outage is not an answer — it is the reason the quarantine exists."""
    ui = with_quarantine
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    main([])
    ui.instagram_state.clear_publish_reconciliation.assert_not_called()
    ui.instagram_state.record_publish_reconciliation.assert_called_once()


def test_the_drain_runs_even_when_instagram_is_disabled(with_quarantine, monkeypatch):
    """Disabling the feature must not strand a Reel that may be live on the account.

    Same reasoning as the share-link drain: the obligation outlives the feature being
    switched on, and reading a container's status needs only the Page token.
    """
    ui = with_quarantine
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    main([])
    ui.instagram_state.record_recovered_publish.assert_called_once()


def test_the_drain_is_skipped_without_a_page_token(base, monkeypatch):
    """Nothing to ask with. Not an error — the entry simply waits for the next tick."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [dict(_QUARANTINE_ENTRY)]
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    main([])                                  # must not raise
    ui.instagram_api.get_container_status.assert_not_called()
    ui.instagram_state.clear_publish_reconciliation.assert_not_called()


def test_the_drain_runs_before_any_upload_work(with_pending):
    """Resolving an unknown publish is what UNBLOCKS the queue, so it goes first.

    Ordered against claim_pending_upload() — the gateway to actually working a job —
    rather than against reading state, which the unconditional orphan sweep does first
    on every tick for its own unrelated reasons.
    """
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [dict(_QUARANTINE_ENTRY)]
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    order = []
    ui.instagram_state.clear_publish_reconciliation.side_effect = (
        lambda *a, **k: order.append("drained") or True
    )
    ui.instagram_state.claim_pending_upload.side_effect = (
        lambda *a, **k: order.append("claimed") or "claimed"
    )
    main([])
    assert order[:2] == ["drained", "claimed"]


def test_an_entry_with_no_container_id_is_skipped(base):
    """Defensive: a malformed entry must not crash the tick for everything else."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [
        {"project_name": _PROJECT, "idempotency_key": _IDEM_KEY}
    ]
    main([])                                  # must not raise
    ui.instagram_api.get_container_status.assert_not_called()


# ---------------------------------------------------------------------------
# What is and is not quarantined
# ---------------------------------------------------------------------------

def test_a_container_that_never_reached_publish_is_not_quarantined(with_pending):
    """The stuck-container case: 3 poll timeouts, no publish call, no block.

    publish_container() was never invoked, so Meta cannot have published it. Quarantining
    would hold a re-approval hostage for a video that demonstrably never went live.
    """
    import scripts.upload_instagram as ui
    _set_attempt(with_pending, 2)
    ui.instagram_api.get_container_status.return_value = "IN_PROGRESS"
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_not_called()
    ui.instagram_state.mark_failed.assert_called_once_with(_IDEM_KEY)


def test_a_prior_attempts_publish_marker_drives_the_terminal_decision(base, tmp_path):
    """The second round-2 defect: the alert read only THIS attempt's flag.

    Attempt 3 fails before reaching publish, but attempt 2 already asked Meta to publish
    and never heard back. The durable marker is what carries that across, so the job is
    quarantined and the operator is told the Reel may be live — instead of being told this
    was an ordinary failure at the precise moment that distinction matters most.
    """
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    ui.instagram_state.get_pending_upload.return_value = dict(
        _PENDING_RECORD, video_local_path=str(video), container_id=_CONTAINER_ID,
        attempt_count=2, publish_attempted_at="2026-08-31T14:05:00Z",
    )
    # This attempt dies while classifying, long before any publish call of its own.
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_called_once_with(
        _CONTAINER_ID, project_name=_PROJECT, idempotency_key=_IDEM_KEY
    )
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "MAY already be live" in text


def test_a_finished_container_clears_a_stale_publish_marker(with_pending):
    """Instagram saying FINISHED is authoritative: nothing was published from it.

    So an earlier attempt's marker must not survive to quarantine the job later — the
    question has been answered.
    """
    import scripts.upload_instagram as ui
    with_pending["container_id"] = _CONTAINER_ID
    with_pending["publish_attempted_at"] = "2026-08-31T14:05:00Z"
    _set_attempt(with_pending, 2)
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    # The terminal settle also reports FINISHED -> definitively unpublished.
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_not_called()


def test_the_publish_marker_is_written_before_the_publish_call(with_pending):
    """Ordering is the whole guarantee — afterwards would miss the case it describes."""
    import scripts.upload_instagram as ui
    order = []
    ui.instagram_state.mark_publish_attempted.side_effect = lambda *a: order.append("marked")
    ui.instagram_api.publish_container.side_effect = lambda *a: (
        order.append("published") or _POST_ID
    )
    main([])
    assert order == ["marked", "published"]


# ---------------------------------------------------------------------------
# A quarantine that cannot even be CHECKED must not be silent
# ---------------------------------------------------------------------------

def test_quarantines_are_reported_when_there_is_no_page_token(base, monkeypatch):
    """The last quiet failure mode: the entry keeps blocking, and nothing says why.

    Every other failure — an invalid token, a Graph outage — retries every tick and
    re-alerts daily. A token removed entirely used to leave a video permanently
    un-postable with no reconciliation attempts and no alerts at all.
    """
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [dict(_QUARANTINE_ENTRY)]
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "cannot find out" in text
    assert "FB_PAGE_ACCESS_TOKEN" in text
    assert "stays blocked" in text


def test_the_credential_absent_report_does_not_claim_a_check_happened(base, monkeypatch):
    """counts_as_check=False — nothing was asked of Instagram, so nothing is counted."""
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = [dict(_QUARANTINE_ENTRY)]
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    main([])
    _args, kwargs = ui.instagram_state.record_publish_reconciliation.call_args
    assert kwargs["counts_as_check"] is False
    ui.instagram_api.get_container_status.assert_not_called()


def test_nothing_is_reported_when_there_are_no_quarantines(base, monkeypatch):
    """No obligation, no noise — a client with no Instagram token is not misconfigured."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    main([])
    ui.instagram_state.record_publish_reconciliation.assert_not_called()
    ui.telegram_api.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# A growing quarantine list is visible, and never silently trimmed
# ---------------------------------------------------------------------------

def _quarantine_entries(n):
    return [
        dict(_QUARANTINE_ENTRY, container_id=f"container_{i}", idempotency_key=str(i))
        for i in range(n)
    ]


def test_a_backlog_is_reported_in_the_alert(base, caplog):
    """One stuck upload and a month of accumulation read identically otherwise."""
    import scripts.upload_instagram as ui
    entries = _quarantine_entries(ui._QUARANTINE_BACKLOG_THRESHOLD)
    ui.instagram_state.list_publish_reconciliations.return_value = entries
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    main([])
    text = ui.telegram_api.send_message.call_args.args[1]
    assert f"{len(entries)} Instagram uploads are now in this state" in text
    assert "never dropped" in text or "Nothing is ever dropped" in text


def test_a_backlog_is_logged_every_tick(base, caplog):
    """Visible without anyone reading the state file."""
    import logging
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = _quarantine_entries(
        ui._QUARANTINE_BACKLOG_THRESHOLD
    )
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    with caplog.at_level(logging.ERROR):
        main([])
    assert "quarantine backlog" in caplog.text


def test_a_small_number_of_quarantines_does_not_trigger_the_backlog_warning(base, caplog):
    """The threshold exists so one unlucky upload does not read like a systemic problem."""
    import logging
    import scripts.upload_instagram as ui
    ui.instagram_state.list_publish_reconciliations.return_value = _quarantine_entries(1)
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    with caplog.at_level(logging.ERROR):
        main([])
    assert "quarantine backlog" not in caplog.text
    text = ui.telegram_api.send_message.call_args.args[1]
    assert "are now in this state" not in text


def test_the_drain_never_drops_an_entry_to_stay_short(base):
    """Safety over tidiness: a dropped entry releases a key while the Reel's fate is unknown."""
    import scripts.upload_instagram as ui
    entries = _quarantine_entries(ui._QUARANTINE_BACKLOG_THRESHOLD + 3)
    ui.instagram_state.list_publish_reconciliations.return_value = entries
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    main([])
    ui.instagram_state.clear_publish_reconciliation.assert_not_called()
    assert ui.instagram_state.record_publish_reconciliation.call_count == len(entries)


# ---------------------------------------------------------------------------
# The terminal path settles the marker rather than leaving it to be quarantined
# ---------------------------------------------------------------------------

def test_a_definitively_unpublished_container_clears_its_marker(with_pending):
    """Otherwise mark_failed()'s safety net would block a video Instagram cleared."""
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    main([])
    ui.instagram_state.mark_publish_settled.assert_called_once_with(_IDEM_KEY)
    ui.instagram_state.record_publish_reconciliation.assert_not_called()


def test_an_unresolved_container_does_not_clear_its_marker(with_pending):
    """The question is still open, so the record must keep looking like it has one."""
    import scripts.upload_instagram as ui
    with_pending["attempt_count"] = 2
    ui.instagram_api.publish_container.side_effect = InstagramUploadError("connection reset")
    ui.instagram_api.get_container_status.side_effect = [
        "FINISHED", InstagramUploadError("graph down"),
    ]
    main([])
    ui.instagram_state.mark_publish_settled.assert_not_called()
    ui.instagram_state.record_publish_reconciliation.assert_called_once()


def test_the_exhausted_path_still_writes_the_activity_log_line(base, tmp_path):
    """claim_pending_upload() creates the entry but knows nothing about logging.

    IG_UNKNOWN is written here instead, once, when the container becomes unresolved —
    not by the drain, which would repeat it every tick and bury it.
    """
    import scripts.upload_instagram as ui
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    ui.instagram_state.get_pending_upload.return_value = dict(
        _PENDING_RECORD, video_local_path=str(video), container_id=_CONTAINER_ID,
        attempt_count=3, publish_attempted_at="2026-08-31T14:05:00Z",
    )
    ui.instagram_state.claim_pending_upload.return_value = "exhausted"
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    main([])
    ui.instagram_logger.log_publish_unresolved.assert_called_once_with(_PROJECT, _CONTAINER_ID)
