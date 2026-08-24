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
    """Common mocks for upload_facebook tests. Default: no pending job.

    claim_pending_upload defaults to 'claimed' — irrelevant to tests where get_pending_upload
    returns None (main() returns before _process_upload is ever reached), and the right default
    for tests that DO set a pending record and want to exercise the upload attempt itself.
    Override claim_pending_upload's return_value/side_effect directly to test the other claim
    outcomes (mismatch/in_flight/cooldown/stale_published/stale_failed/exhausted) — that
    decision logic lives in facebook_state.claim_pending_upload() and is covered directly in
    test_facebook_state.py; these tests only need to verify upload_facebook.py's own dispatch on
    each outcome.
    """
    import scripts.upload_facebook as uf
    mock_lock = mocker.MagicMock()
    mocker.patch.object(uf, "_try_acquire_upload_lock", return_value=mock_lock)
    mocker.patch.object(uf.fcntl, "flock")
    mocker.patch.object(uf.facebook_state, "get_pending_upload", return_value=None)
    mocker.patch.object(uf.facebook_state, "claim_pending_upload", return_value="claimed")
    mocker.patch.object(uf.facebook_state, "release_claim")
    mocker.patch.object(uf.facebook_state, "mark_published")
    mocker.patch.object(uf.facebook_state, "mark_failed")
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
    """When there is no pending job, claim_pending_upload and mark_published are not called."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_state.claim_pending_upload.assert_not_called()
    uf.facebook_state.mark_published.assert_not_called()


def test_happy_path_calls_claim_pending_upload(with_pending):
    """Happy-path upload claims the job (atomically, via claim_pending_upload) before ever
    calling the Facebook API — see the issue #34 follow-up regression tests below for why this
    must be the ONE atomic gate rather than separate get_pending_upload/mark_uploading calls."""
    import scripts.upload_facebook as uf
    main([])
    uf.facebook_state.claim_pending_upload.assert_called_once_with(
        _IDEM_KEY,
        cooldown_seconds=uf._COOLDOWN_SECONDS,
        max_attempts=uf._MAX_ATTEMPTS,
        lease_seconds=uf._UPLOAD_LEASE_SECONDS,
    )


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
# Cron re-entrancy lock (issue #34 follow-up) — mirrors check_approval.py's
# test_cron_lock_contention_exits_silently
# ---------------------------------------------------------------------------

def test_lock_contention_exits_silently_without_touching_state(base):
    """If upload_facebook.lock is held by another instance, main() exits without ever reading
    or touching facebook_state at all — not just without calling the Facebook API twice."""
    import scripts.upload_facebook as uf
    uf._try_acquire_upload_lock.return_value = None

    main([])

    uf.facebook_state.get_pending_upload.assert_not_called()
    uf.facebook_state.claim_pending_upload.assert_not_called()
    uf.facebook_api.upload_video.assert_not_called()


# ---------------------------------------------------------------------------
# T014: US3 — Retry and failure-recovery paths
#
# Cooldown/attempt-budget/claiming decisions now live entirely in
# facebook_state.claim_pending_upload() (covered directly in test_facebook_state.py) — these
# tests drive claim_pending_upload's mocked return value to verify upload_facebook.py's OWN
# dispatch on each outcome, rather than simulating cooldown via the pending record's fields.
# ---------------------------------------------------------------------------

def test_upload_error_logs_attempt_failed(base, tmp_path):
    """A FacebookUploadError (claim succeeded, below the attempt budget) logs the failed attempt."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("connection timeout")

    main([])

    uf.facebook_logger.log_upload_attempt_failed.assert_called_once()


def test_upload_error_below_max_releases_claim_and_does_not_send_alert(base, tmp_path):
    """A FacebookUploadError with retries remaining (attempt_number < _MAX_ATTEMPTS) releases the
    claim (so the next attempt is cooldown-gated, not lease-gated — see release_claim's
    docstring) and does not send a Telegram alert or mark the job failed."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video), attempt_count=0)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("timeout")

    main([])

    uf.facebook_state.release_claim.assert_called_once_with(_IDEM_KEY)
    uf.facebook_state.mark_failed.assert_not_called()
    uf.telegram_api.send_message.assert_not_called()


def test_claim_cooldown_does_not_call_upload(base, tmp_path):
    """When claim_pending_upload() declines with 'cooldown', upload_video is never called."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "cooldown"

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    uf.facebook_state.mark_published.assert_not_called()
    uf.facebook_state.mark_failed.assert_not_called()


def test_claim_mismatch_does_not_call_upload(base, tmp_path):
    """When claim_pending_upload() declines with 'mismatch' (the job changed since main()'s
    snapshot), upload_video is never called."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "mismatch"

    main([])

    uf.facebook_api.upload_video.assert_not_called()


def test_claim_in_flight_does_not_call_upload(base, tmp_path):
    """When claim_pending_upload() declines with 'in_flight' (another invocation already
    claimed it — the actual issue #34 check/use race fix), upload_video is never called."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "in_flight"

    main([])

    uf.facebook_api.upload_video.assert_not_called()


def test_claim_stale_published_does_not_call_upload_or_mark_failed(base, tmp_path):
    """When claim_pending_upload() declines with 'stale_published' (already self-healed
    internally), upload_video is never called and mark_failed is never called either — the
    exact protection against silently stomping a real, live fb_post_id (issue #34)."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "stale_published"

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    uf.facebook_state.mark_failed.assert_not_called()


def test_claim_stale_failed_does_not_call_upload(base, tmp_path):
    """When claim_pending_upload() declines with 'stale_failed' (already self-healed
    internally), upload_video is never called."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "stale_failed"

    main([])

    uf.facebook_api.upload_video.assert_not_called()


def test_claim_exhausted_alerts_without_calling_upload_or_mark_failed(base, tmp_path):
    """When claim_pending_upload() declines with 'exhausted' (attempt_count already at the cap
    from a prior tick — cleared internally by the claim itself), upload_facebook.py still logs
    and alerts, but never calls upload_video, and never calls mark_failed again (already
    cleared — calling it again would be a stale-caller/compare-and-update no-op regardless, but
    there's no reason to call it at all)."""
    import scripts.upload_facebook as uf
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_state.claim_pending_upload.return_value = "exhausted"

    main([])

    uf.facebook_api.upload_video.assert_not_called()
    uf.facebook_state.mark_failed.assert_not_called()
    uf.facebook_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)
    uf.telegram_api.send_message.assert_called_once()
    text = uf.telegram_api.send_message.call_args.args[1]
    assert "⚠️" in text
    assert _PROJECT in text


def test_third_failure_marks_failed(base, tmp_path):
    """A FacebookUploadError on the attempt that reaches _MAX_ATTEMPTS (claim succeeded — this
    IS attempt 3) marks the job failed."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video), attempt_count=2)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    main([])

    uf.facebook_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    uf.facebook_state.release_claim.assert_not_called()


def test_third_failure_logs_exhausted(base, tmp_path):
    """After the 3rd failed attempt, log_upload_exhausted is called."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video), attempt_count=2)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    main([])

    uf.facebook_logger.log_upload_exhausted.assert_called_once_with(_PROJECT)


def test_third_failure_sends_telegram_alert(base, tmp_path):
    """After the 3rd failed attempt, a Telegram alert is sent to the admin."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video), attempt_count=2)
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("server error")

    main([])

    uf.telegram_api.send_message.assert_called_once()
    text = uf.telegram_api.send_message.call_args.args[1]
    assert "⚠️" in text
    assert _PROJECT in text


def test_token_error_marks_failed_immediately(base, tmp_path):
    """FacebookTokenError immediately marks the job failed after just this one attempt — it does
    not wait for the retry budget (_MAX_ATTEMPTS) to exhaust, unlike FacebookUploadError, and
    does not release the claim for a future retry either (mark_failed is unconditional here, not
    attempt-count-gated)."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookTokenError
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    uf.facebook_state.get_pending_upload.return_value = record
    uf.facebook_api.upload_video.side_effect = FacebookTokenError("token expired")

    main([])

    uf.facebook_state.mark_failed.assert_called_once_with(_IDEM_KEY)
    uf.facebook_state.release_claim.assert_not_called()


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

    FIELDKIT_DATA_DIR is also redirected here (not just fb_state.DATA_DIR/STATE_FILE), since
    upload_facebook.py's real _try_acquire_upload_lock() reads it directly to place
    upload_facebook.lock — these tests use the real lock too, not a mocked one.
    """
    import scripts.upload_facebook as uf
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
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


def test_reprocessing_after_publish_does_not_mark_failed_again(real_state, tmp_path, mocker):
    """A second tick against a resolved job must not call mark_failed either — the live incident
    was this exact call stomping status to 'failed' while a real, live fb_post_id stayed attached.

    Spies on facebook_state.mark_failed (real implementation still runs) rather than only
    asserting on the resulting persisted state, so this actually proves the call itself never
    happens on the second tick — not just that its effects didn't happen to show up.
    """
    import scripts.upload_facebook as uf
    mark_failed_spy = mocker.spy(real_state, "mark_failed")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)

    main([])
    idem_key = record["idempotency_key"]
    assert real_state.is_published(idem_key) is True
    mark_failed_spy.assert_not_called()

    main([])  # second tick must not touch state at all

    mark_failed_spy.assert_not_called()
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
    """Defense in depth (claim_pending_upload()'s stale_published check): even if
    pending_facebook_upload is somehow non-null for a key already in published_idempotency_keys
    — e.g. a state file written before this fix — the claim must clear it and never call
    upload_video, and must never overwrite the real fb_post_id via mark_failed."""
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
    the FacebookUploadError handler at all — so release_claim() never runs, leaving status
    stuck at 'uploading') must still stop retrying once _MAX_ATTEMPTS is reached. Each crashed
    tick leaves the claim looking 'in_flight' until _UPLOAD_LEASE_SECONDS elapses (patched to 0
    here so the test doesn't sleep) — that's the lease-expiry path that lets an abandoned claim
    become reclaimable again, subject to the same attempt-budget check as any other retry."""
    import scripts.upload_facebook as uf
    monkeypatch.setattr(uf, "_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(uf, "_UPLOAD_LEASE_SECONDS", 0)
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


def test_overlapping_main_invocations_only_one_calls_upload_video(real_state, tmp_path, mocker):
    """The exact check/use race a cross-review flagged: two overlapping cron invocations of
    upload_facebook.py (e.g. a slow video upload still running when the next minute's tick
    starts) must not both call facebook_api.upload_video() for the same job — a real duplicate
    Facebook post.

    Reproduced end-to-end with two real threads: thread 1 is held INSIDE upload_video()
    (simulating a slow upload) while thread 2 runs main() to completion. thread 2 must be
    declined by the upload_facebook.lock re-entrancy lock BEFORE it ever reads
    facebook_state at all — a lease-timeout-based reclaim alone (see
    facebook_state.claim_pending_upload) cannot tell a merely-slow-but-live holder from a
    crashed one, so the OS-level flock (not the state-level claim) is what must block thread 2
    here. get_pending_upload is spied (real implementation still runs) to prove thread 2 never
    reaches it, not just that it never called upload_video.
    """
    import threading
    import scripts.upload_facebook as uf
    get_pending_spy = mocker.spy(real_state, "get_pending_upload")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 64)
    record = dict(_PENDING_RECORD, video_local_path=str(video))
    real_state.set_pending_upload(record)
    get_pending_spy.reset_mock()  # ignore the set_pending_upload-adjacent setup above

    call_started = threading.Event()
    release_call = threading.Event()

    def slow_upload_video(*args, **kwargs):
        call_started.set()
        assert release_call.wait(timeout=5), "test deadlocked waiting to release upload_video"
        return _POST_ID

    uf.facebook_api.upload_video.side_effect = slow_upload_video

    thread1 = threading.Thread(target=main, args=([],))
    thread1.start()
    assert call_started.wait(timeout=5), "thread1 never entered upload_video"

    thread2 = threading.Thread(target=main, args=([],))
    thread2.start()
    thread2.join(timeout=5)
    assert not thread2.is_alive(), "thread2 (should have been declined) is still running"

    # thread2 must have been blocked by the OS lock, not by reaching claim_pending_upload and
    # losing a state-level race — only thread1's own call should be recorded here (thread2 must
    # never have read facebook_state at all).
    assert get_pending_spy.call_count == 1, "thread2 read facebook_state before being declined"

    release_call.set()
    thread1.join(timeout=5)
    assert not thread1.is_alive(), "thread1 never finished"

    assert uf.facebook_api.upload_video.call_count == 1
    assert real_state.get_pending_upload() is None  # thread1 published successfully
    assert real_state.is_published(_IDEM_KEY) is True
