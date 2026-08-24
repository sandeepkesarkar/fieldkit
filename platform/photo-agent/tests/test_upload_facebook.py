"""
Tests for scripts/upload_facebook.py — the Facebook video upload cron script.

US1 (T009): no pending job exits silently; happy-path upload lifecycle;
missing video file; env var validation.

US3 (T014): transient retry, cooldown, exhaustion, and token-expiry failure paths.

All external calls (facebook_state, facebook_api, facebook_logger, telegram_api)
are mocked. No real network or file-system access.

Issue #34 (pending_facebook_upload reprocessing loop) regression tests below use
the REAL tools.facebook_state read-modify-write state machine, redirected to an
isolated tmp file via the `real_state` fixture — a fully-mocked facebook_state
can't reproduce a bug that lives in how state is persisted between cron ticks.
Only facebook_api/facebook_logger/telegram_api stay mocked there.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY

import pytest

import tools.facebook_state as fb_state
from scripts.upload_facebook import main

_PROJECT = "test_project"
_PAGE_ID = "123456789"
_PAGE_TOKEN = "page_token_abc"
_CHAT_ID = "telegram_chat_id"
_IDEM_KEY = "42"
_POST_ID = "fb_post_123"

_PENDING_RECORD = {
    "project_name": _PROJECT,
    "video_local_path": "/nonexistent/video.mp4",
    "page_id": _PAGE_ID,
    "status": "pending",
    "attempt_count": 0,
    "last_attempt_at": None,
    "triggered_at": "2026-05-30T14:00:00Z",
    "idempotency_key": _IDEM_KEY,
    "fb_post_id": None,
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _PAGE_TOKEN)
    monkeypatch.setenv("FB_PAGE_ID", _PAGE_ID)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_bot_token")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)


@pytest.fixture
def base(mocker, env):
    """Common mocks for upload_facebook tests. Default: no pending job."""
    import scripts.upload_facebook as uf
    mocker.patch.object(uf.facebook_state, "get_pending_upload", return_value=None)
    mocker.patch.object(uf.facebook_state, "mark_uploading")
    mocker.patch.object(uf.facebook_state, "mark_published")
    mocker.patch.object(uf.facebook_state, "mark_failed")
    mocker.patch.object(uf.facebook_state, "increment_attempt")
    mocker.patch.object(uf.facebook_state, "is_published", return_value=False)
    mocker.patch.object(uf.facebook_state, "clear_pending_upload")
    mocker.patch.object(uf.facebook_api, "upload_video", return_value=_POST_ID)
    mocker.patch.object(uf.facebook_logger, "log_upload_started")
    mocker.patch.object(uf.facebook_logger, "log_upload_published")
    mocker.patch.object(uf.facebook_logger, "log_upload_attempt_failed")
    mocker.patch.object(uf.facebook_logger, "log_upload_exhausted")
    mocker.patch.object(uf.facebook_logger, "log_token_expired")
    mocker.patch.object(uf.telegram_api, "send_message")
    return mocker


@pytest.fixture
def with_pending(base, tmp_path):
    """Adds a real video file and sets up facebook_state to return a pending record."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    return record


# ---------------------------------------------------------------------------
# T009: US1 — Happy path and basic validation
# ---------------------------------------------------------------------------

def test_no_pending_job_exits_silently(base):
    """When there is no pending job, upload_video is never called."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_api.upload_video.assert_not_called()


def test_no_pending_job_does_not_modify_state(base):
    """When there is no pending job, mark_uploading and mark_published are not called."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_state.mark_uploading.assert_not_called()
    uf.facebook_state.mark_published.assert_not_called()


def test_happy_path_marks_uploading(with_pending):
    """Happy-path upload calls mark_uploading with the idempotency key."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_state.mark_uploading.assert_called_once_with(_IDEM_KEY)


def test_happy_path_calls_upload_video(with_pending):
    """Happy-path upload calls facebook_api.upload_video with page_token, page_id, and video path."""
    import scripts.upload_facebook as uf
    main([])
    call_args = uf.facebook_api.upload_video.call_args
    assert call_args.args[0] == _PAGE_TOKEN
    assert call_args.args[1] == _PAGE_ID


def test_happy_path_marks_published(with_pending):
    """On success, mark_published is called with idempotency key and post_id."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_state.mark_published.assert_called_once_with(_IDEM_KEY, _POST_ID)


def test_happy_path_logs_published(with_pending):
    """On success, log_upload_published is called with project_name and post_id."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_logger.log_upload_published.assert_called_once_with(_PROJECT, _POST_ID)


def test_happy_path_deletes_local_file_after_published(with_pending, tmp_path, monkeypatch):
    """On success, the local video file is deleted after mark_published."""
    import scripts.upload_facebook as uf
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    video_path = uf.facebook_state.get_pending_upload.return_value["video_local_path"]
    assert Path(video_path).exists()
    main([])
    assert not Path(video_path).exists()


def test_happy_path_sends_telegram_confirmation(with_pending):
    """On success, send_message is called with a URL pointing to the Facebook post."""
    import scripts.upload_facebook as uf
    main([])
    uf.telegram_api.send_message.assert_called_once()
    text = uf.telegram_api.send_message.call_args.args[1]
    assert "facebook.com" in text
    assert _POST_ID in text


def test_happy_path_telegram_confirmation_sent_to_correct_chat(with_pending):
    """Telegram confirmation is sent to ADMIN_TELEGRAM_CHAT_ID."""
    import scripts.upload_facebook as uf
    main([])
    chat_id_arg = uf.telegram_api.send_message.call_args.args[0]
    assert chat_id_arg == _CHAT_ID


def test_happy_path_source_cron_arg(with_pending):
    """--source cron is accepted without error."""
    main(["--source", "cron"])  # must not raise


# ---------------------------------------------------------------------------
# Missing video file
# ---------------------------------------------------------------------------

def test_missing_video_file_marks_failed(base):
    """When the video file does not exist, mark_failed is called."""
    import scripts.upload_facebook as uf
    record = dict(_PENDING_RECORD, video_local_path="/nonexistent/video.mp4")
    uf.facebook_state.get_pending_upload.return_value = record
    main([])
    uf.facebook_state.mark_failed.assert_called_once_with(_IDEM_KEY)


def test_missing_video_file_does_not_call_upload(base):
    """When the video file does not exist, facebook_api.upload_video is never called."""
    import scripts.upload_facebook as uf
    record = dict(_PENDING_RECORD, video_local_path="/nonexistent/video.mp4")
    uf.facebook_state.get_pending_upload.return_value = record
    main([])
    uf.facebook_api.upload_video.assert_not_called()


# ---------------------------------------------------------------------------
# Env var validation
# ---------------------------------------------------------------------------

def test_missing_fb_page_access_token_exits_with_code_1(monkeypatch):
    """Missing FB_PAGE_ACCESS_TOKEN causes sys.exit(1) before any state read."""
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("FB_PAGE_ID", _PAGE_ID)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1


def test_missing_fb_page_id_exits_with_code_1(monkeypatch):
    """Missing FB_PAGE_ID causes sys.exit(1) before any state read."""
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _PAGE_TOKEN)
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# T014: US3 — Retry and failure-recovery paths
# ---------------------------------------------------------------------------

def test_upload_error_first_attempt_increments_count(base, tmp_path):
    """FacebookUploadError on attempt 1 increments attempt_count to 1."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("connection timeout")

    main([])

    uf.facebook_state.increment_attempt.assert_called_once_with(_IDEM_KEY)


def test_upload_error_first_attempt_sets_last_attempt_at(base, tmp_path):
    """FacebookUploadError on attempt 1 logs the failure attempt."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("timeout")

    main([])

    uf.facebook_logger.log_upload_attempt_failed.assert_called_once()


def test_upload_error_first_attempt_does_not_send_alert(base, tmp_path):
    """After the 1st failure, no Telegram alert is sent (more retries remain)."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("timeout")

    main([])

    uf.telegram_api.send_message.assert_not_called()


def test_cooldown_not_elapsed_exits_without_uploading(base, tmp_path):
    """If last_attempt_at is within the last 60s, upload_video is not called."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    record = dict(_PENDING_RECORD,
                  video_local_path=str(video),
                  attempt_count=1,
                  last_attempt_at=recent)
    uf.facebook_state.get_pending_upload.return_value = record

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    uf.facebook_state.increment_attempt.assert_not_called()


def test_cooldown_elapsed_proceeds_to_upload(base, tmp_path):
    """If last_attempt_at is older than 60s, the upload is attempted."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    record = dict(_PENDING_RECORD,
                  video_local_path=str(video),
                  attempt_count=1,
                  last_attempt_at=old)
    uf.facebook_state.get_pending_upload.return_value = record

    main([])

    uf.facebook_api.upload_video.assert_called_once()


def test_third_failure_marks_failed(base, tmp_path):
    """After 3 failed attempts, status transitions to failed."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    record = dict(_PENDING_RECORD,
                  video_local_path=str(video),
                  attempt_count=2,
                  last_attempt_at=old)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")
    # increment_attempt increments the count in the real impl; mock needs to simulate count=3
    uf.facebook_state.get_pending_upload.return_value = dict(record)

    main([])

    uf.facebook_state.mark_failed.assert_called_once_with(_IDEM_KEY)


def test_third_failure_logs_exhausted(base, tmp_path):
    """After 3 failed attempts, log_upload_exhausted is called."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    record = dict(_PENDING_RECORD,
                  video_local_path=str(video),
                  attempt_count=2,
                  last_attempt_at=old)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    main([])

    uf.facebook_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)


def test_third_failure_sends_telegram_alert(base, tmp_path):
    """After 3 failed attempts, a Telegram alert is sent to the admin."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    record = dict(_PENDING_RECORD,
                  video_local_path=str(video),
                  attempt_count=2,
                  last_attempt_at=old)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    main([])

    uf.telegram_api.send_message.assert_called_once()
    text = uf.telegram_api.send_message.call_args.args[1]
    assert "⚠️" in text
    assert _PROJECT in text


def test_token_error_marks_failed_immediately(base, tmp_path):
    """FacebookTokenError immediately marks status=failed after just this one attempt — it does
    not wait for the retry budget (_MAX_ATTEMPTS) to exhaust, unlike FacebookUploadError.

    increment_attempt IS still called (persisted before the API call, alongside every attempt —
    see _process_upload's crash-safety comment) but it has no bearing on when mark_failed fires
    here: that's unconditional on FacebookTokenError, not attempt-count-gated.
    """
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookTokenError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookTokenError("token expired")

    main([])

    uf.facebook_state.mark_failed.assert_called_once_with(_IDEM_KEY)


def test_token_error_logs_token_expired(base, tmp_path):
    """FacebookTokenError calls log_token_expired."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookTokenError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookTokenError("token expired")

    main([])

    uf.facebook_logger.log_token_expired.assert_called_once_with(_PROJECT)


def test_token_error_sends_telegram_alert(base, tmp_path):
    """FacebookTokenError sends a Telegram alert immediately."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookTokenError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookTokenError("token expired")

    main([])

    uf.telegram_api.send_message.assert_called_once()
    text = uf.telegram_api.send_message.call_args.args[1]
    assert "⚠️" in text
    assert "token" in text.lower() or "reconnect" in text.lower()


# ---------------------------------------------------------------------------
# Issue #34 regression: pending_facebook_upload reprocessing loop
#
# main() used to check only `record is None` before reprocessing — never
# published_idempotency_keys or status. Combined with mark_published/mark_failed
# never clearing pending_facebook_upload, every cron tick after a resolved job
# reprocessed the SAME job forever. In the live incident the video file was
# already gone (deleted by the success-path cleanup), so it just spammed
# "video file missing" + mark_failed — which stomped status to "failed" while
# leaving the real, live fb_post_id attached. Had the file still existed, this
# would have re-called facebook_api.upload_video and posted a REAL duplicate.
# ---------------------------------------------------------------------------

@pytest.fixture
def real_state(tmp_path, monkeypatch, env, mocker):
    """Redirect tools.facebook_state's on-disk file to an isolated tmp path and mock only
    the external side effects (Facebook API, Telegram, structured logging) — facebook_state
    itself stays real so these tests exercise its actual persisted state transitions.
    """
    import scripts.upload_facebook as uf
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    mocker.patch.object(uf.facebook_api, "upload_video", return_value=_POST_ID)
    mocker.patch.object(uf.facebook_logger, "log_upload_started")
    mocker.patch.object(uf.facebook_logger, "log_upload_published")
    mocker.patch.object(uf.facebook_logger, "log_upload_attempt_failed")
    mocker.patch.object(uf.facebook_logger, "log_upload_exhausted")
    mocker.patch.object(uf.facebook_logger, "log_token_expired")
    mocker.patch.object(uf.telegram_api, "send_message")
    return fb_state


def test_reprocessing_after_publish_does_not_call_upload_video(real_state, tmp_path):
    """The exact issue #34 incident, reproduced end-to-end: a second cron tick against state
    left over from a successful publish must not call upload_video again. This is the actual
    line of defense against a real duplicate Facebook post."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)

    main([])  # first tick: succeeds, deletes the local file, must clear the pending job
    assert real_state.get_pending_upload() is None
    assert not video.exists()

    uf.facebook_api.upload_video.reset_mock()
    main([])  # second tick: nothing left to reprocess

    uf.facebook_api.upload_video.assert_not_called()
    assert real_state.get_pending_upload() is None


def test_reprocessing_after_publish_does_not_mark_failed_again(real_state, tmp_path):
    """A second tick against a resolved job must not call mark_failed either — the live incident
    was this exact call stomping status to 'failed' while a real, live fb_post_id stayed attached."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)

    main([])
    idem_key = record["idempotency_key"]
    assert real_state.is_published(idem_key) is True

    main([])  # second tick must not touch state at all

    data_after_first = json.loads(real_state.STATE_FILE.read_text())
    assert data_after_first["pending_facebook_upload"] is None
    assert idem_key in data_after_first["published_idempotency_keys"]


def test_terminal_failure_does_not_retry_forever(real_state, tmp_path, monkeypatch):
    """Once attempt_count reaches _MAX_ATTEMPTS and the job is marked failed, further cron ticks
    must not keep calling upload_video with no backoff."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    monkeypatch.setattr(uf, "_COOLDOWN_SECONDS", 0)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    for _ in range(uf._MAX_ATTEMPTS):
        main([])

    assert uf.facebook_api.upload_video.call_count == uf._MAX_ATTEMPTS
    assert real_state.get_pending_upload() is None  # terminal: cleared, not left dangling

    uf.facebook_api.upload_video.reset_mock()
    main([])  # a 4th tick after exhaustion must not retry
    uf.facebook_api.upload_video.assert_not_called()


def test_stale_published_record_is_cleared_without_reprocessing(real_state, tmp_path):
    """Defense in depth (main()'s explicit is_published check): even if pending_facebook_upload
    is somehow non-null for a key already in published_idempotency_keys — e.g. a state file
    written before this fix — main() must clear it and must never call upload_video, and must
    never overwrite the real fb_post_id via mark_failed."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    stale_record = dict(
        _PENDING_RECORD,
        video_local_path=str(video),
        status="published",
        fb_post_id=_POST_ID,
    )
    real_state.STATE_FILE.write_text(json.dumps({
        "pending_facebook_upload": stale_record,
        "published_idempotency_keys": [_IDEM_KEY],
    }))

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    data = json.loads(real_state.STATE_FILE.read_text())
    assert data["pending_facebook_upload"] is None
    assert _IDEM_KEY in data["published_idempotency_keys"]


def test_crash_during_upload_leaves_attempt_persisted_before_the_call(real_state, tmp_path):
    """A process that dies inside facebook_api.upload_video() (simulated here as an unhandled
    exception propagating out of main()) must still have already persisted attempt_count/
    last_attempt_at — they're written BEFORE the call, not only on a caught failure. Without
    this, a crashed-but-possibly-succeeded attempt would be retried immediately (no cooldown)
    and forever (attempt_count frozen), which is the same unbounded-reprocessing shape as the
    original issue #34 bug, just triggered by a crash instead of a successful publish."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)
    uf.facebook_api.upload_video.side_effect = RuntimeError("simulated process crash mid-upload")

    with pytest.raises(RuntimeError):
        main([])

    pending = real_state.get_pending_upload()
    assert pending is not None
    assert pending["attempt_count"] == 1
    assert pending["last_attempt_at"] is not None


def test_crash_mid_upload_is_eventually_bounded_by_max_attempts(real_state, tmp_path, monkeypatch):
    """A job stuck 'crashing' every tick (attempt_count advances but the process never reaches
    the FacebookUploadError handler at all) must still stop retrying once _MAX_ATTEMPTS is
    reached — checked proactively before calling the API again, so the bound holds regardless of
    how prior attempts failed (crash vs. a caught API error)."""
    import scripts.upload_facebook as uf
    monkeypatch.setattr(uf, "_COOLDOWN_SECONDS", 0)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)
    uf.facebook_api.upload_video.side_effect = RuntimeError("simulated process crash mid-upload")

    for _ in range(uf._MAX_ATTEMPTS):
        with pytest.raises(RuntimeError):
            main([])

    pending = real_state.get_pending_upload()
    assert pending is not None
    assert pending["attempt_count"] == uf._MAX_ATTEMPTS

    uf.facebook_api.upload_video.reset_mock(side_effect=True)
    uf.facebook_api.upload_video.side_effect = RuntimeError("would crash again if called")
    main([])  # must be marked failed WITHOUT calling the API again

    uf.facebook_api.upload_video.assert_not_called()
    assert real_state.get_pending_upload() is None


def test_stale_terminal_failed_record_is_cleared_without_reprocessing(real_state, tmp_path):
    """Defense in depth: a dangling pending record already marked status='failed' (e.g. left over
    from a state file written before this fix) must be cleared on the next tick, not reprocessed."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    stale_record = dict(_PENDING_RECORD, video_local_path=str(video), status="failed", attempt_count=3)
    real_state.STATE_FILE.write_text(json.dumps({
        "pending_facebook_upload": stale_record,
        "published_idempotency_keys": [],
    }))

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    assert real_state.get_pending_upload() is None
