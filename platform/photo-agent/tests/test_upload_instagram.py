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
    # Cross-platform deletion coordination: default to "no other platform is waiting",
    # so the tests that don't care about coordination behave as before. The tests that
    # DO care override this, and test_dual_platform_integration.py exercises the real
    # thing end to end.
    mocker.patch.object(ui.upload_cleanup, "other_platforms_pending", return_value=[])
    mocker.patch.object(ui, "_delete_local_file")
    mocker.patch.object(ui.drive, "create_temporary_share_link", return_value=_SHARE_LINK)
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
    """FR-016: an unconfigured client exercises no Instagram code path at all."""
    import scripts.upload_instagram as ui
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    main([])
    ui.instagram_state.get_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


def test_empty_ig_account_id_exits_silently(base, monkeypatch):
    """An empty IG_BUSINESS_ACCOUNT_ID (as shipped in .env.example) also disables the script."""
    import scripts.upload_instagram as ui
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "")
    main([])
    ui.instagram_state.get_pending_upload.assert_not_called()


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
    ui.drive.create_temporary_share_link.assert_called_once_with(with_pending["video_local_path"])
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
    ui.drive.create_temporary_share_link.assert_called_once_with(with_pending["video_local_path"])


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
    ui.instagram_state.get_pending_upload.assert_not_called()
    ui.instagram_api.create_media_container.assert_not_called()


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
    ui.instagram_state.get_pending_upload.assert_not_called()


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
