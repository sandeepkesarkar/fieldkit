"""
Tests for scripts/check_approval.py.

All external calls (state, Telegram API, Drive, email) are mocked.
Tests call main() directly and verify behaviour through mock assertions.

Before issue #49, this script had a cron-based getUpdates polling path and a
--callback-data direct path (invoked by the manual /check_approval Hermes
command). #49 retired the poller entirely — the direct path's logic is now
the script's only code path, invoked by the /photo_approve and /photo_reject
Hermes skills (named with a `photo-` prefix, not the bare `approve`/`reject`
this issue originally specified, because `approve` collides with a built-in
Hermes core command — see platform/photo-agent/skills/photo-approve/SKILL.md's
naming note). Tests for the poller (getUpdates, offset tracking, callback
matching, button-tap acknowledgement/removal, the dedicated approval-bot
token) are gone along with that code; see git history for the pre-#49
versions of this file if that coverage is ever needed for reference.
"""

import pytest

from scripts.check_approval import main

_PROJECT = "test_project"
_CHAT_ID = "123456789"  # matches ADMIN_TELEGRAM_CHAT_ID in env fixture
_PENDING = {
    "project_name": _PROJECT,
    "drive_folder_id": "folder_id",
    "drive_video_file_id": "video_file_id",
    "drive_folder_link": "https://drive.google.com/drive/folders/folder_id",
    "video_local_path": "/tmp/fieldkit_test/video.mp4",
    "telegram_message_id": 42,
    "triggered_at": "2026-05-17T10:00:00Z",
}

_APPROVE_ARGS = ["--callback-data", "approve"]
_REJECT_ARGS = ["--callback-data", "reject"]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL", "agent@example.com")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)
    # Defense in depth against a live FB_PAGE_ID leaking in from the ambient
    # environment (e.g. a real client .env already loaded in-process): force
    # the FB-enqueue branch off by default for every test using this fixture,
    # regardless of what base_fb below re-enables it to.
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    # Same defense for Feature 005's Instagram enqueue branch (see base_ig).
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)


@pytest.fixture
def lock_mock(mocker):
    """Patch check_approval lock acquisition so tests don't create real lock files."""
    mock = mocker.MagicMock()
    mocker.patch("scripts.check_approval._try_acquire_check_lock", return_value=mock)
    mocker.patch("scripts.check_approval.fcntl.flock")
    return mock


@pytest.fixture
def base(mocker, env):
    """Mocks common to all tests: env loading, state, and all external calls."""
    mocker.patch("scripts.check_approval._load_env")
    # Simulate successfully acquiring the check_approval.lock.
    # MagicMock for the file obj; patch fcntl.flock so the release in main() finally block is a no-op.
    mock_lock = mocker.MagicMock()
    mocker.patch("scripts.check_approval._try_acquire_check_lock", return_value=mock_lock)
    mocker.patch("scripts.check_approval.fcntl.flock")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=_PENDING)
    mocker.patch("scripts.check_approval.state.clear_pending_approval")
    mocker.patch("scripts.check_approval.drive.delete")
    mocker.patch("scripts.check_approval._send_approval_email")
    mocker.patch("scripts.check_approval._notify_admin")
    mocker.patch("scripts.check_approval.activity_log.log_approved")
    mocker.patch("scripts.check_approval.activity_log.log_rejected")
    mocker.patch("scripts.check_approval.activity_log.log_error")
    # Mocked unconditionally, not just in base_fb below: the approve path
    # calls into these whenever FB_PAGE_ID is set, and a real (unmocked)
    # facebook_state.set_pending_upload() writes straight through to the
    # live FIELDKIT_DATA_DIR/facebook_state.json on whatever machine the
    # suite happens to run on. No test should be able to hit that file
    # regardless of which fixture it uses or what's in the ambient env.
    mocker.patch("scripts.check_approval.facebook_state.set_pending_upload")
    mocker.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    # Same reasoning for Feature 005's Instagram enqueue: never let an unmocked
    # instagram_state.set_pending_upload() reach the live instagram_state.json.
    mocker.patch("scripts.check_approval.instagram_state.set_pending_upload")
    mocker.patch("scripts.check_approval.instagram_state.is_published", return_value=False)
    return mocker


# ---------------------------------------------------------------------------
# No pending approval
# ---------------------------------------------------------------------------

def test_no_pending_approval_exits_immediately(mocker, env, lock_mock):
    """When no approval is pending, the script returns without calling Drive or email."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_drive_delete = mocker.patch("scripts.check_approval.drive.delete")
    mock_send_email = mocker.patch("scripts.check_approval._send_approval_email")
    main(_APPROVE_ARGS)
    mock_drive_delete.assert_not_called()
    mock_send_email.assert_not_called()


def test_no_pending_approval_does_not_clear_state(mocker, env, lock_mock):
    """When no approval is pending, clear_pending_approval is not called."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_clear = mocker.patch("scripts.check_approval.state.clear_pending_approval")
    main(_APPROVE_ARGS)
    mock_clear.assert_not_called()


def test_no_pending_approval_reject_exits_silently(mocker, env, lock_mock):
    """--callback-data reject with no pending approval exits without error."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    main(_REJECT_ARGS)  # must not raise


def test_no_pending_approval_prints_nothing_to_stdout(mocker, env, lock_mock, capsys):
    """Issue #63: the genuine no-pending-approval case must be the ONLY one that
    produces empty stdout — this is the signal Hermes's output-handling rule
    relies on to distinguish it from a successful approve/reject."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    main(_APPROVE_ARGS)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Approve path — actions
# ---------------------------------------------------------------------------

def test_approve_sends_email_with_correct_args(base):
    """_send_approval_email is called with agent_email, admin_email, project_name, folder_link."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca._send_approval_email.assert_called_once_with(
        "agent@example.com",
        "admin@example.com",
        _PROJECT,
        _PENDING["drive_folder_link"],
    )


def test_approve_sends_telegram_confirmation(base):
    """approve path sends a Telegram confirmation containing '✅' and the project name."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert "✅" in msg
    assert _PROJECT in msg


def test_approve_preserves_local_file(base, tmp_path, monkeypatch):
    """approve path leaves the local video file intact — upload_facebook.py deletes it after upload."""
    import scripts.check_approval as ca
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    pending = dict(_PENDING, video_local_path=str(local_file))
    ca.state.get_pending_approval.return_value = pending
    main(_APPROVE_ARGS)
    assert local_file.exists()


def test_approve_does_not_delete_local_file(base, mocker):
    """approve path does NOT delete the local video file — upload_facebook.py owns deletion."""
    import scripts.check_approval as ca
    mock_delete = mocker.patch("scripts.check_approval._delete_local_file")
    main(_APPROVE_ARGS)
    mock_delete.assert_not_called()


def test_approve_clears_pending_approval(base):
    """approve path calls state.clear_pending_approval()."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_approve_confirmation_not_printed_if_clear_pending_approval_fails(base, capsys):
    """Issue #63 follow-up: the 'Approved: <project>' confirmation must only ever
    reach stdout after state.clear_pending_approval() has actually succeeded — if
    it raises, the approval hasn't genuinely completed (the pending record is
    still there), so the confirmation must not be printed even though every
    other approve side effect (email, activity log, FB enqueue) already ran."""
    import scripts.check_approval as ca
    ca.state.clear_pending_approval.side_effect = RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        main(_APPROVE_ARGS)
    out = capsys.readouterr().out
    assert "Approved:" not in out


def test_reject_confirmation_not_printed_if_clear_pending_approval_fails(base, capsys):
    """Same guarantee as above, for the reject path."""
    import scripts.check_approval as ca
    ca.state.clear_pending_approval.side_effect = RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        main(_REJECT_ARGS)
    out = capsys.readouterr().out
    assert "Rejected:" not in out


def test_approve_logs_approved(base):
    """approve path calls activity_log.log_approved() with the project name."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca.activity_log.log_approved.assert_called_once_with(_PROJECT)


def test_approve_prints_unambiguous_confirmation_to_stdout(base, capsys):
    """Issue #63: a successful approval must print a one-line stdout confirmation
    so Hermes never mistakes it for the no-pending-approval no-op case."""
    main(_APPROVE_ARGS)
    out = capsys.readouterr().out
    assert out == f"Approved: {_PROJECT}\n"


# ---------------------------------------------------------------------------
# Reject path — actions
# ---------------------------------------------------------------------------

def test_reject_deletes_drive_file(base):
    """reject path calls drive.delete() with the video file ID from the pending record."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    ca.drive.delete.assert_called_once_with(_PENDING["drive_video_file_id"])


def test_reject_deletes_local_file(base, tmp_path, monkeypatch):
    """reject path deletes the local temp video file."""
    import scripts.check_approval as ca
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    pending = dict(_PENDING, video_local_path=str(local_file))
    ca.state.get_pending_approval.return_value = pending
    main(_REJECT_ARGS)
    assert not local_file.exists()


def test_reject_deletes_local_file_with_relative_video_tmp_dir(base, tmp_path, monkeypatch):
    """reject path deletes a video the producer wrote under a RELATIVE VIDEO_TMP_DIR
    resolved against FIELDKIT_DATA_DIR — the producer/consumer resolution mismatch
    that #47's review follow-up caught: this script used to resolve the same
    relative value against the shared repo root instead, so it refused to delete
    files the fixed producer actually wrote."""
    import scripts.check_approval as ca
    client_data_dir = tmp_path / "clients" / "mercury" / "data"
    monkeypatch.setenv("VIDEO_TMP_DIR", "data/photo-agent/tmp")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(client_data_dir))

    video_dir = client_data_dir / "data" / "photo-agent" / "tmp" / _PROJECT
    video_dir.mkdir(parents=True)
    local_file = video_dir / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    pending = dict(_PENDING, video_local_path=str(local_file))
    ca.state.get_pending_approval.return_value = pending
    main(_REJECT_ARGS)
    assert not local_file.exists()


def test_reject_sends_telegram_notification(base):
    """reject path sends a Telegram rejection notification containing '❌'."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert "❌" in msg


def test_reject_message_instructs_to_update_and_retrigger(base):
    """Telegram rejection message instructs the admin to update photos and re-trigger."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    msg = ca._notify_admin.call_args.args[0]
    assert "process_photos" in msg


def test_reject_clears_pending_approval(base):
    """reject path calls state.clear_pending_approval()."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_reject_logs_rejected(base):
    """reject path calls activity_log.log_rejected() with the project name."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    ca.activity_log.log_rejected.assert_called_once_with(_PROJECT)


def test_reject_prints_unambiguous_confirmation_to_stdout(base, capsys):
    """Issue #63: a successful rejection must print a one-line stdout confirmation
    so Hermes never mistakes it for the no-pending-approval no-op case."""
    main(_REJECT_ARGS)
    out = capsys.readouterr().out
    assert out == f"Rejected: {_PROJECT}\n"


def test_approve_and_no_pending_stdout_are_distinguishable(mocker, env, lock_mock, capsys):
    """Issue #63 end-to-end: capture stdout for a successful approve and for the
    genuine no-pending-approval case in the same test and assert they differ —
    this is exactly the ambiguity Hermes's output-handling rule was fooled by
    before check_approval.py printed anything on success."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=_PENDING)
    mocker.patch("scripts.check_approval.state.clear_pending_approval")
    mocker.patch("scripts.check_approval.drive.delete")
    mocker.patch("scripts.check_approval._send_approval_email")
    mocker.patch("scripts.check_approval._notify_admin")
    mocker.patch("scripts.check_approval.activity_log.log_approved")
    mocker.patch("scripts.check_approval.facebook_state.set_pending_upload")
    mocker.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    main(_APPROVE_ARGS)
    success_out = capsys.readouterr().out
    assert success_out != ""

    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    main(_APPROVE_ARGS)
    no_pending_out = capsys.readouterr().out

    assert no_pending_out == ""
    assert success_out != no_pending_out


# ---------------------------------------------------------------------------
# Email failure on approve
# ---------------------------------------------------------------------------

def test_email_failure_sends_fallback_with_folder_link(base):
    """On email failure, a Telegram fallback message containing the Drive folder link is sent."""
    import scripts.check_approval as ca
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main(_APPROVE_ARGS)
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert _PENDING["drive_folder_link"] in msg


def test_email_failure_fallback_indicates_delivery_failed(base):
    """Telegram fallback on email failure contains a word indicating the failure."""
    import scripts.check_approval as ca
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main(_APPROVE_ARGS)
    msg = ca._notify_admin.call_args.args[0]
    assert "failed" in msg.lower() or "email" in msg.lower()


def test_email_failure_state_still_cleared(base):
    """State is cleared even when email delivery fails."""
    import scripts.check_approval as ca
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_email_failure_fallback_does_not_send_success_confirmation(base):
    """Telegram fallback on email failure must not contain the ✅ success confirmation."""
    import scripts.check_approval as ca
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main(_APPROVE_ARGS)
    msg = ca._notify_admin.call_args.args[0]
    assert "✅" not in msg


# ---------------------------------------------------------------------------
# Drive delete failure on reject
# ---------------------------------------------------------------------------

def test_drive_delete_failure_is_logged(base):
    """Drive delete failure on reject calls activity_log.log_error() with the project name."""
    import scripts.check_approval as ca
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main(_REJECT_ARGS)
    ca.activity_log.log_error.assert_called_once()
    assert ca.activity_log.log_error.call_args.args[0] == _PROJECT


def test_drive_delete_failure_rejection_still_sent(base):
    """Telegram rejection notification is still sent when Drive delete fails."""
    import scripts.check_approval as ca
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main(_REJECT_ARGS)
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert "❌" in msg


def test_drive_delete_failure_state_cleared(base):
    """State is cleared even when Drive delete fails."""
    import scripts.check_approval as ca
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main(_REJECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


# ---------------------------------------------------------------------------
# Path traversal guard (C2)
# ---------------------------------------------------------------------------

def test_delete_local_file_refuses_path_outside_tmp(tmp_path, tmp_path_factory, monkeypatch):
    """_delete_local_file refuses to delete a file outside the allowed tmp directory."""
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    other_tmp = tmp_path_factory.mktemp("other")
    outside_file = other_tmp / "evil.txt"
    outside_file.write_text("important data")

    from scripts.check_approval import _delete_local_file
    _delete_local_file(str(outside_file), "test_project")
    assert outside_file.exists()  # must NOT have been deleted


# ---------------------------------------------------------------------------
# argparse — --callback-data is required and validated
# ---------------------------------------------------------------------------

def test_invalid_callback_data_rejected_by_argparse(mocker, env, lock_mock):
    """argparse rejects --callback-data values other than 'approve' or 'reject'."""
    mocker.patch("scripts.check_approval._load_env")
    with pytest.raises(SystemExit):
        main(["--callback-data", "Approve"])  # capital A — not in choices


def test_missing_callback_data_rejected_by_argparse(mocker, env, lock_mock):
    """argparse rejects invocation with no --callback-data at all — required, not optional."""
    mocker.patch("scripts.check_approval._load_env")
    with pytest.raises(SystemExit):
        main([])


# ---------------------------------------------------------------------------
# Facebook upload enqueueing on approve path (Feature 003)
# ---------------------------------------------------------------------------

_FB_PAGE_ID = "123456789"


@pytest.fixture
def base_fb(base, monkeypatch):
    """Extends base by re-enabling FB_PAGE_ID for FB enqueue tests.

    facebook_state.set_pending_upload/is_published are already mocked
    unconditionally by `base` above; re-patch is_published here only to
    pin its return value for these tests' own assertions.
    """
    monkeypatch.setenv("FB_PAGE_ID", _FB_PAGE_ID)
    base.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    return base


def test_approve_enqueues_facebook_upload(base_fb):
    """approve path calls facebook_state.set_pending_upload with correct required fields."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca.facebook_state.set_pending_upload.assert_called_once()
    record = ca.facebook_state.set_pending_upload.call_args.args[0]
    assert record["project_name"] == _PROJECT
    assert record["video_local_path"] == _PENDING["video_local_path"]
    assert record["page_id"] == _FB_PAGE_ID
    assert record["idempotency_key"] == str(_PENDING["telegram_message_id"])


def test_approve_enqueue_idempotency_skip(base_fb):
    """If is_published(key) returns True, set_pending_upload is NOT called."""
    import scripts.check_approval as ca
    ca.facebook_state.is_published.return_value = True
    main(_APPROVE_ARGS)
    ca.facebook_state.set_pending_upload.assert_not_called()


def test_approve_enqueue_skipped_without_fb_page_id(base, mocker, monkeypatch):
    """When FB_PAGE_ID is not set, facebook_state.set_pending_upload is never called."""
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    mock_set = mocker.patch("scripts.check_approval.facebook_state.set_pending_upload")
    main(_APPROVE_ARGS)
    mock_set.assert_not_called()


def test_approve_enqueue_failure_does_not_abort_approve_flow(base_fb):
    """A facebook_state exception during enqueue is caught — the approve flow still completes."""
    import scripts.check_approval as ca
    ca.facebook_state.set_pending_upload.side_effect = Exception("state error")
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


# ---------------------------------------------------------------------------
# _notify_admin — direct Telegram Bot API notification
# ---------------------------------------------------------------------------
#
# These tests exercise the real _notify_admin() body (unlike the tests above,
# which mock it out entirely). telegram_api.send_message is always mocked —
# never a real HTTP call — so no test here can reach live Telegram.

def test_notify_admin_sends_via_telegram_api(env, mocker):
    """_notify_admin() calls telegram_api.send_message with the configured chat_id and message,
    on the single TELEGRAM_BOT_TOKEN (issue #49 — no dedicated approval-bot token anymore)."""
    import scripts.check_approval as ca
    mock_send = mocker.patch("scripts.check_approval.telegram_api.send_message")
    ca._notify_admin("hello admin")
    mock_send.assert_called_once_with(_CHAT_ID, "hello admin")


def test_notify_admin_swallows_telegram_failure(env, mocker):
    """A RuntimeError from telegram_api.send_message is logged, not raised."""
    import scripts.check_approval as ca
    mocker.patch(
        "scripts.check_approval.telegram_api.send_message",
        side_effect=RuntimeError("Telegram HTTP error 500"),
    )
    ca._notify_admin("hello admin")  # must not raise


def test_notify_admin_no_chat_id_skips_send(monkeypatch, mocker):
    """With ADMIN_TELEGRAM_CHAT_ID unset, _notify_admin() does not call telegram_api at all."""
    import scripts.check_approval as ca
    monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mock_send = mocker.patch("scripts.check_approval.telegram_api.send_message")
    ca._notify_admin("hello admin")
    mock_send.assert_not_called()


def test_notify_admin_does_not_leak_token_on_network_failure(env, mocker, caplog):
    """The bot token must not appear in _notify_admin's warning log on a connection
    failure. requests exceptions embed the request URL (including /bot<TOKEN>/...)
    in their string representation — telegram_api.send_message must redact it
    before _notify_admin logs the exception verbatim."""
    import requests
    import scripts.check_approval as ca
    mocker.patch(
        "scripts.check_approval.telegram_api.requests.post",
        side_effect=requests.exceptions.ConnectionError(
            "Max retries exceeded with url: /bottest_token/sendMessage"
        ),
    )
    with caplog.at_level("WARNING"):
        ca._notify_admin("hello admin")  # must not raise
    assert "test_token" not in caplog.text


def test_notify_admin_swallows_non_runtime_error(env, mocker):
    """A non-RuntimeError from telegram_api.send_message (e.g. a malformed Telegram
    response triggering an AttributeError deep in telegram_api) is also logged, not
    raised — _notify_admin's best-effort guarantee must not depend on the exception type."""
    import scripts.check_approval as ca
    mocker.patch(
        "scripts.check_approval.telegram_api.send_message",
        side_effect=AttributeError("'NoneType' object has no attribute 'get'"),
    )
    ca._notify_admin("hello admin")  # must not raise


def test_approve_completes_despite_non_runtime_error_from_notify(mocker, env, lock_mock):
    """approve path still clears pending state even when _notify_admin's underlying
    Telegram call raises a non-RuntimeError — the notification is best-effort and
    must never block approval completion."""
    import scripts.check_approval as ca
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=_PENDING)
    mock_clear = mocker.patch("scripts.check_approval.state.clear_pending_approval")
    mocker.patch(
        "scripts.check_approval.telegram_api.send_message",
        side_effect=AttributeError("'list' object has no attribute 'get'"),
    )
    mocker.patch("scripts.check_approval.drive.delete")
    mocker.patch("scripts.check_approval._send_approval_email")
    mocker.patch("scripts.check_approval.activity_log.log_approved")
    mocker.patch("scripts.check_approval.facebook_state.set_pending_upload")
    mocker.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    main(_APPROVE_ARGS)
    mock_clear.assert_called_once()


# ---------------------------------------------------------------------------
# _send_approval_email — Gmail REST API implementation
# ---------------------------------------------------------------------------

def test_send_approval_email_calls_gmail_api(mocker):
    """_send_approval_email POSTs to the Gmail messages/send endpoint."""
    from scripts.check_approval import _send_approval_email
    mocker.patch("scripts.check_approval.drive._get_access_token", return_value="tok")
    mock_post = mocker.patch("scripts.check_approval.requests.post")
    mock_post.return_value.ok = True

    _send_approval_email("agent@x.com", "admin@x.com", "proj", "https://drive.google.com/x")

    assert mock_post.called
    url = mock_post.call_args.args[0]
    assert "gmail" in url and "messages/send" in url


def test_send_approval_email_raises_on_http_error(mocker):
    """_send_approval_email raises RuntimeError on Gmail API HTTP error."""
    from scripts.check_approval import _send_approval_email
    mocker.patch("scripts.check_approval.drive._get_access_token", return_value="tok")
    mock_post = mocker.patch("scripts.check_approval.requests.post")
    mock_post.return_value.ok = False
    mock_post.return_value.status_code = 403

    with pytest.raises(RuntimeError, match="Gmail send failed"):
        _send_approval_email("agent@x.com", "admin@x.com", "proj", "https://drive.google.com/x")


def test_send_approval_email_raises_on_token_failure(mocker):
    """_send_approval_email raises RuntimeError when _get_access_token fails."""
    from scripts.check_approval import _send_approval_email
    mocker.patch(
        "scripts.check_approval.drive._get_access_token",
        side_effect=RuntimeError("token refresh failed"),
    )
    with pytest.raises(RuntimeError, match="Gmail access token"):
        _send_approval_email("agent@x.com", "admin@x.com", "proj", "https://drive.google.com/x")


# ---------------------------------------------------------------------------
# Activity log failure guard — clear_pending_approval must run even if log raises
# ---------------------------------------------------------------------------

def test_activity_log_error_on_approve_does_not_block_state_clear(base, mocker):
    """ValueError from activity_log.log_approved is caught — state is still cleared."""
    import scripts.check_approval as ca
    ca.activity_log.log_approved.side_effect = ValueError("bad project_name")
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_activity_log_oserror_on_approve_does_not_block_state_clear(base, mocker):
    """OSError from activity_log.log_approved is caught — state is still cleared."""
    import scripts.check_approval as ca
    ca.activity_log.log_approved.side_effect = OSError("disk full")
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_activity_log_error_on_reject_does_not_block_state_clear(base, mocker):
    """ValueError from activity_log.log_rejected is caught — state is still cleared."""
    import scripts.check_approval as ca
    ca.activity_log.log_rejected.side_effect = ValueError("bad project_name")
    main(_REJECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


# ---------------------------------------------------------------------------
# Cron re-entrancy lock — retained even though the cron poller is gone: two
# /photo_approve (or /photo_approve + /photo_reject) commands could still be
# dispatched by Hermes in close succession, and this guards the same
# state.json race.
# ---------------------------------------------------------------------------

def test_lock_contention_takes_no_action(mocker, env):
    """If check_approval.lock is held by another instance, main() exits without taking action."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval._try_acquire_check_lock", return_value=None)
    mock_get_pending = mocker.patch("scripts.check_approval.state.get_pending_approval")
    main(_APPROVE_ARGS)
    mock_get_pending.assert_not_called()


def test_lock_contention_prints_distinct_nonempty_stdout(mocker, env, capsys):
    """Issue #63 follow-up: lock contention must NOT be silent like the genuine
    no-pending-approval case — it means another decision is actively being
    processed, not that nothing is pending. It needs its own unambiguous,
    non-empty stdout signal, distinct from both an approve/reject confirmation
    and the empty-stdout no-pending-approval case."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval._try_acquire_check_lock", return_value=None)
    main(_APPROVE_ARGS)
    out = capsys.readouterr().out
    assert out == "Already processing — try again in a moment.\n"


# ---------------------------------------------------------------------------
# Instagram upload enqueueing on approve path (Feature 005)
# ---------------------------------------------------------------------------
#
# The single Telegram approval must enqueue BOTH platform jobs (FR-002): the
# owner is never asked to approve the same video twice. These tests pin that,
# plus the per-client gate (FR-016) and the "Instagram failure never touches the
# Facebook enqueue" independence rule (FR-013).

_IG_ACCOUNT_ID = "17841400000000000"


@pytest.fixture
def base_ig(base, monkeypatch):
    """Extends base by enabling IG_BUSINESS_ACCOUNT_ID (and FB_PAGE_ID) for enqueue tests."""
    monkeypatch.setenv("FB_PAGE_ID", _FB_PAGE_ID)
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", _IG_ACCOUNT_ID)
    base.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    base.patch("scripts.check_approval.instagram_state.is_published", return_value=False)
    return base


def test_approve_enqueues_instagram_upload(base_ig):
    """approve path calls instagram_state.set_pending_upload with the correct record."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca.instagram_state.set_pending_upload.assert_called_once()
    record = ca.instagram_state.set_pending_upload.call_args.args[0]
    assert record["project_name"] == _PROJECT
    assert record["video_local_path"] == _PENDING["video_local_path"]
    assert record["ig_business_account_id"] == _IG_ACCOUNT_ID
    assert record["idempotency_key"] == str(_PENDING["telegram_message_id"])
    assert record["status"] == "pending"
    assert record["attempt_count"] == 0
    assert record["last_attempt_at"] is None
    assert record["container_id"] is None
    assert record["ig_post_id"] is None
    assert record["triggered_at"]


def test_approve_enqueues_both_platforms_from_one_approval(base_ig):
    """FR-002: one approval, both jobs — no second Instagram-specific approval step."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    ca.facebook_state.set_pending_upload.assert_called_once()
    ca.instagram_state.set_pending_upload.assert_called_once()


def test_both_platform_jobs_share_one_idempotency_key(base_ig):
    """The two jobs are correlated only by sharing the approval's idempotency key."""
    import scripts.check_approval as ca
    main(_APPROVE_ARGS)
    fb_record = ca.facebook_state.set_pending_upload.call_args.args[0]
    ig_record = ca.instagram_state.set_pending_upload.call_args.args[0]
    assert fb_record["idempotency_key"] == ig_record["idempotency_key"]
    assert ig_record["idempotency_key"] == str(_PENDING["telegram_message_id"])


def test_approve_instagram_enqueue_idempotency_skip(base_ig):
    """FR-011: an already-published key is not re-enqueued for Instagram."""
    import scripts.check_approval as ca
    ca.instagram_state.is_published.return_value = True
    main(_APPROVE_ARGS)
    ca.instagram_state.set_pending_upload.assert_not_called()


def test_instagram_idempotency_skip_does_not_block_facebook(base_ig):
    """An already-published Instagram job still lets the Facebook job enqueue (FR-013)."""
    import scripts.check_approval as ca
    ca.instagram_state.is_published.return_value = True
    main(_APPROVE_ARGS)
    ca.facebook_state.set_pending_upload.assert_called_once()


def test_approve_instagram_enqueue_skipped_without_account_id(base, mocker, monkeypatch):
    """FR-016: no IG_BUSINESS_ACCOUNT_ID means no Instagram behaviour at all."""
    import scripts.check_approval as ca
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("FB_PAGE_ID", _FB_PAGE_ID)
    main(_APPROVE_ARGS)
    ca.instagram_state.set_pending_upload.assert_not_called()
    ca.instagram_state.is_published.assert_not_called()


def test_approve_instagram_enqueue_skipped_when_account_id_is_empty(base, mocker, monkeypatch):
    """An empty IG_BUSINESS_ACCOUNT_ID (as shipped in .env.example) also disables the path."""
    import scripts.check_approval as ca
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "")
    monkeypatch.setenv("FB_PAGE_ID", _FB_PAGE_ID)
    main(_APPROVE_ARGS)
    ca.instagram_state.set_pending_upload.assert_not_called()


def test_instagram_enqueue_failure_does_not_abort_approve_flow(base_ig):
    """FR-013: an instagram_state exception is caught — the approve flow still completes."""
    import scripts.check_approval as ca
    ca.instagram_state.set_pending_upload.side_effect = Exception("state error")
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_instagram_enqueue_failure_does_not_abort_facebook_enqueue(base_ig):
    """FR-013: a failed Instagram enqueue leaves the Facebook enqueue intact."""
    import scripts.check_approval as ca
    ca.instagram_state.set_pending_upload.side_effect = Exception("state error")
    main(_APPROVE_ARGS)
    ca.facebook_state.set_pending_upload.assert_called_once()


def test_facebook_enqueue_failure_does_not_abort_instagram_enqueue(base_ig):
    """FR-013 in the other direction: a Facebook failure must not skip Instagram."""
    import scripts.check_approval as ca
    ca.facebook_state.set_pending_upload.side_effect = Exception("state error")
    main(_APPROVE_ARGS)
    ca.instagram_state.set_pending_upload.assert_called_once()


def test_reject_does_not_enqueue_instagram(base_ig):
    """Only an approval enqueues an Instagram job — a rejection never publishes."""
    import scripts.check_approval as ca
    main(_REJECT_ARGS)
    ca.instagram_state.set_pending_upload.assert_not_called()


def test_instagram_enqueue_logs_enqueued_event(base_ig, mocker):
    """FR-012: the enqueue is recorded in the activity log."""
    import scripts.check_approval as ca
    mock_log = mocker.patch("scripts.check_approval.instagram_logger.log_upload_enqueued")
    main(_APPROVE_ARGS)
    mock_log.assert_called_once_with(_PROJECT)


def test_instagram_enqueue_log_failure_does_not_abort_approve_flow(base_ig, mocker):
    """A logging failure must not cost the owner their approval."""
    import scripts.check_approval as ca
    mocker.patch(
        "scripts.check_approval.instagram_logger.log_upload_enqueued",
        side_effect=OSError("disk full"),
    )
    main(_APPROVE_ARGS)
    ca.state.clear_pending_approval.assert_called_once()
