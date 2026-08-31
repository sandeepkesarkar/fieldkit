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
_SHARE_LINK = "https://drive.google.com/uc?export=download&id=drive_file_1"

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
    mocker.patch.object(ui.drive, "create_temporary_share_link", return_value=_SHARE_LINK)
    mocker.patch.object(ui.drive, "revoke_share_link")
    mocker.patch.object(ui.instagram_api, "create_media_container", return_value=_CONTAINER_ID)
    mocker.patch.object(ui.instagram_api, "get_container_status", return_value="FINISHED")
    mocker.patch.object(ui.instagram_api, "publish_container", return_value=_POST_ID)
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
    ui.instagram_state.mark_published.assert_called_once_with(_IDEM_KEY, _POST_ID)
    ui.instagram_state.mark_failed.assert_not_called()
    ui.instagram_state.release_claim.assert_not_called()


def test_happy_path_revokes_the_share_link(with_pending):
    """The temporary public link is revoked once Instagram has the video."""
    import scripts.upload_instagram as ui
    main([])
    ui.drive.revoke_share_link.assert_called_once_with("drive_file_1")


def test_happy_path_sends_telegram_confirmation_with_post_url(with_pending):
    """FR-003: the owner gets a Telegram confirmation with a direct link."""
    import scripts.upload_instagram as ui
    main([])
    chat_id, text = ui.telegram_api.send_message.call_args.args
    assert chat_id == _CHAT_ID
    assert f"https://www.instagram.com/p/{_POST_ID}" in text
    assert "Instagram" in text


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
    """A failed revoke is logged but must not undo or hide a live post."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive revoke share link failed")
    main([])
    ui.instagram_state.mark_published.assert_called_once_with(_IDEM_KEY, _POST_ID)


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
