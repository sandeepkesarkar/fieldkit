"""
Tests for scripts/check_approval.py.

All external calls (state, Telegram API, Drive, email, openclaw) are mocked.
Tests call main() directly and verify behaviour through mock assertions.
"""

import pytest

from scripts.check_approval import main

_PROJECT = "test_project"
_PENDING = {
    "project_name": _PROJECT,
    "drive_folder_id": "folder_id",
    "drive_video_file_id": "video_file_id",
    "drive_folder_link": "https://drive.google.com/drive/folders/folder_id",
    "video_local_path": "/tmp/fieldkit_test/video.mp4",
    "telegram_message_id": 42,
    "triggered_at": "2026-05-17T10:00:00Z",
}

# update_id values chosen so new_offset assertions are unambiguous
_APPROVE_UPDATE = {
    "update_id": 100,
    "callback_query": {
        "id": "cq_approve",
        "data": "approve",
        "message": {"message_id": 42},
    },
}

_REJECT_UPDATE = {
    "update_id": 101,
    "callback_query": {
        "id": "cq_reject",
        "data": "reject",
        "message": {"message_id": 42},
    },
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL", "agent@example.com")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")


@pytest.fixture
def base(mocker, env):
    """Mocks common to all tests: env loading, state, and all external calls."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=_PENDING)
    mocker.patch("scripts.check_approval.state.get_telegram_offset", return_value=50)
    mocker.patch("scripts.check_approval.state.set_telegram_offset")
    mocker.patch("scripts.check_approval.state.clear_pending_approval")
    mocker.patch("scripts.check_approval.telegram_api.get_updates", return_value=[])
    mocker.patch("scripts.check_approval.telegram_api.answer_callback_query")
    mocker.patch("scripts.check_approval.drive.delete")
    mocker.patch("scripts.check_approval._send_approval_email")
    mocker.patch("scripts.check_approval._openclaw_send")
    mocker.patch("scripts.check_approval.activity_log.log_approved")
    mocker.patch("scripts.check_approval.activity_log.log_rejected")
    mocker.patch("scripts.check_approval.activity_log.log_error")
    return mocker


# ---------------------------------------------------------------------------
# No pending approval
# ---------------------------------------------------------------------------

def test_no_pending_approval_exits_immediately(mocker, env):
    """When no approval is pending, the script returns without calling Telegram or Drive."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_get_updates = mocker.patch("scripts.check_approval.telegram_api.get_updates")
    mock_drive_delete = mocker.patch("scripts.check_approval.drive.delete")
    main([])
    mock_get_updates.assert_not_called()
    mock_drive_delete.assert_not_called()


def test_no_pending_approval_does_not_modify_state(mocker, env):
    """When no approval is pending, set_telegram_offset and clear_pending_approval are not called."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_set_offset = mocker.patch("scripts.check_approval.state.set_telegram_offset")
    mock_clear = mocker.patch("scripts.check_approval.state.clear_pending_approval")
    main([])
    mock_set_offset.assert_not_called()
    mock_clear.assert_not_called()


def test_source_cron_no_pending_exits_silently(mocker, env):
    """With --source cron and no pending approval, the script exits without error."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.check_approval.telegram_api.get_updates")
    main(["--source", "cron"])  # must not raise


# ---------------------------------------------------------------------------
# No matching callback
# ---------------------------------------------------------------------------

def test_no_updates_sets_offset_and_exits(base):
    """When getUpdates returns an empty list, the offset is written and state is not cleared."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = []
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(50)  # no updates → offset unchanged
    ca.state.clear_pending_approval.assert_not_called()
    ca.telegram_api.answer_callback_query.assert_not_called()


def test_stale_callback_updates_offset_but_does_not_process(base):
    """A callback_query for a different message_id advances the offset but takes no action."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {
            "update_id": 200,
            "callback_query": {
                "id": "cq_stale",
                "data": "approve",
                "message": {"message_id": 99},  # not 42
            },
        }
    ]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(201)
    ca.state.clear_pending_approval.assert_not_called()
    ca.telegram_api.answer_callback_query.assert_not_called()


def test_non_callback_update_advances_offset(base):
    """A regular message update (no callback_query) advances the offset without taking action."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {"update_id": 300, "message": {"text": "hello"}}
    ]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(301)
    ca.state.clear_pending_approval.assert_not_called()


# ---------------------------------------------------------------------------
# Offset calculation
# ---------------------------------------------------------------------------

def test_offset_is_max_update_id_plus_one(base):
    """new_offset = max(update_id) + 1 across all updates, regardless of type."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {"update_id": 100, "callback_query": {"id": "cq1", "data": "approve", "message": {"message_id": 42}}},
        {"update_id": 105, "message": {"text": "other"}},
    ]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(106)


def test_offset_unchanged_when_no_updates(base):
    """When updates is empty, new_offset equals the stored offset."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = []
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(50)


# ---------------------------------------------------------------------------
# Approve path — ordering
# ---------------------------------------------------------------------------

def test_approve_answer_callback_query_called_first(base):
    """answer_callback_query is called before the approval email or any Telegram message."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]

    call_order = []
    ca.telegram_api.answer_callback_query.side_effect = lambda *a, **kw: call_order.append("answer")
    ca._send_approval_email.side_effect = lambda *a, **kw: call_order.append("email")
    ca._openclaw_send.side_effect = lambda *a, **kw: call_order.append("openclaw")

    main([])
    assert call_order[0] == "answer"
    assert "email" in call_order
    assert call_order.index("answer") < call_order.index("email")


# ---------------------------------------------------------------------------
# Approve path — actions
# ---------------------------------------------------------------------------

def test_approve_sends_email_with_correct_args(base):
    """_send_approval_email is called with agent_email, admin_email, project_name, folder_link."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca._send_approval_email.assert_called_once_with(
        "agent@example.com",
        "admin@example.com",
        _PROJECT,
        _PENDING["drive_folder_link"],
    )


def test_approve_sends_telegram_confirmation(base):
    """approve path sends a Telegram confirmation containing '✅' and the project name."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca._openclaw_send.assert_called_once()
    msg = ca._openclaw_send.call_args.args[0]
    assert "✅" in msg
    assert _PROJECT in msg


def test_approve_deletes_local_file(base, tmp_path, monkeypatch):
    """approve path deletes the local temp video file."""
    import scripts.check_approval as ca
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    pending = dict(_PENDING, video_local_path=str(local_file))
    ca.state.get_pending_approval.return_value = pending
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    assert not local_file.exists()


def test_approve_clears_pending_approval(base):
    """approve path calls state.clear_pending_approval()."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_approve_updates_offset(base):
    """approve path sets the Telegram offset to update_id + 1."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(101)  # update_id=100 → 101


def test_approve_logs_approved(base):
    """approve path calls activity_log.log_approved() with the project name."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca.activity_log.log_approved.assert_called_once_with(_PROJECT)


# ---------------------------------------------------------------------------
# Reject path — ordering
# ---------------------------------------------------------------------------

def test_reject_answer_callback_query_called_first(base):
    """answer_callback_query is called before Drive delete or any Telegram message."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]

    call_order = []
    ca.telegram_api.answer_callback_query.side_effect = lambda *a, **kw: call_order.append("answer")
    ca.drive.delete.side_effect = lambda *a, **kw: call_order.append("drive_delete")
    ca._openclaw_send.side_effect = lambda *a, **kw: call_order.append("openclaw")

    main([])
    assert call_order[0] == "answer"
    assert "drive_delete" in call_order
    assert call_order.index("answer") < call_order.index("drive_delete")


# ---------------------------------------------------------------------------
# Reject path — actions
# ---------------------------------------------------------------------------

def test_reject_deletes_drive_file(base):
    """reject path calls drive.delete() with the video file ID from the pending record."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca.drive.delete.assert_called_once_with(_PENDING["drive_video_file_id"])


def test_reject_deletes_local_file(base, tmp_path, monkeypatch):
    """reject path deletes the local temp video file."""
    import scripts.check_approval as ca
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    pending = dict(_PENDING, video_local_path=str(local_file))
    ca.state.get_pending_approval.return_value = pending
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    assert not local_file.exists()


def test_reject_sends_telegram_notification(base):
    """reject path sends a Telegram rejection notification containing '❌'."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca._openclaw_send.assert_called_once()
    msg = ca._openclaw_send.call_args.args[0]
    assert "❌" in msg


def test_reject_message_instructs_to_update_and_retrigger(base):
    """Telegram rejection message instructs the admin to update photos and re-trigger."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    msg = ca._openclaw_send.call_args.args[0]
    assert "process_photos" in msg


def test_reject_clears_pending_approval(base):
    """reject path calls state.clear_pending_approval()."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_reject_updates_offset(base):
    """reject path sets the Telegram offset to update_id + 1."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(102)  # update_id=101 → 102


def test_reject_logs_rejected(base):
    """reject path calls activity_log.log_rejected() with the project name."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca.activity_log.log_rejected.assert_called_once_with(_PROJECT)


# ---------------------------------------------------------------------------
# Email failure on approve
# ---------------------------------------------------------------------------

def test_email_failure_sends_fallback_with_folder_link(base):
    """On email failure, a Telegram fallback message containing the Drive folder link is sent."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    ca._openclaw_send.assert_called_once()
    msg = ca._openclaw_send.call_args.args[0]
    assert _PENDING["drive_folder_link"] in msg


def test_email_failure_fallback_indicates_delivery_failed(base):
    """Telegram fallback on email failure contains a word indicating the failure."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    msg = ca._openclaw_send.call_args.args[0]
    assert "failed" in msg.lower() or "email" in msg.lower()


def test_email_failure_state_still_cleared(base):
    """State is cleared even when email delivery fails."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_email_failure_offset_still_updated(base):
    """Offset is updated even when email delivery fails."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(101)


# ---------------------------------------------------------------------------
# Drive delete failure on reject
# ---------------------------------------------------------------------------

def test_drive_delete_failure_is_logged(base):
    """Drive delete failure on reject calls activity_log.log_error() with the project name."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main([])
    ca.activity_log.log_error.assert_called_once()
    assert ca.activity_log.log_error.call_args.args[0] == _PROJECT


def test_drive_delete_failure_rejection_still_sent(base):
    """Telegram rejection notification is still sent when Drive delete fails."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main([])
    ca._openclaw_send.assert_called_once()
    msg = ca._openclaw_send.call_args.args[0]
    assert "❌" in msg


def test_drive_delete_failure_state_cleared(base):
    """State is cleared even when Drive delete fails."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_drive_delete_failure_offset_updated(base):
    """Offset is updated even when Drive delete fails."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    ca.drive.delete.side_effect = RuntimeError("permission denied")
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(102)


# ---------------------------------------------------------------------------
# get_updates network failure (M2)
# ---------------------------------------------------------------------------

def test_get_updates_failure_does_not_modify_state(base):
    """When get_updates raises RuntimeError, state is not modified and no action is taken."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.side_effect = RuntimeError("network error")
    main([])
    ca.state.clear_pending_approval.assert_not_called()
    ca.state.set_telegram_offset.assert_not_called()
    ca._send_approval_email.assert_not_called()
    ca.drive.delete.assert_not_called()


# ---------------------------------------------------------------------------
# answer_callback_query failure (M3 / C3)
# ---------------------------------------------------------------------------

def test_answer_callback_query_failure_does_not_clear_state(base):
    """If answer_callback_query raises, the pending approval is not cleared."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main([])
    ca.state.clear_pending_approval.assert_not_called()
    ca._send_approval_email.assert_not_called()


def test_answer_callback_query_failure_advances_offset(base):
    """If answer_callback_query raises, the offset is advanced to skip this callback."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(101)


# ---------------------------------------------------------------------------
# Unknown callback_data (M4 / C1)
# ---------------------------------------------------------------------------

def test_unknown_callback_data_does_not_clear_state(base):
    """An unrecognised callback_data value does not clear the pending approval."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {
            "update_id": 100,
            "callback_query": {
                "id": "cq_unknown",
                "data": "tampered_value",
                "message": {"message_id": 42},
            },
        }
    ]
    main([])
    ca.state.clear_pending_approval.assert_not_called()
    ca._send_approval_email.assert_not_called()
    ca.drive.delete.assert_not_called()


def test_unknown_callback_data_advances_offset(base):
    """An unrecognised callback_data still advances the offset past that update."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {
            "update_id": 100,
            "callback_query": {
                "id": "cq_unknown",
                "data": "tampered_value",
                "message": {"message_id": 42},
            },
        }
    ]
    main([])
    ca.state.set_telegram_offset.assert_called_once_with(101)


# ---------------------------------------------------------------------------
# Email failure: ✅ confirmation must NOT appear in fallback message (M6)
# ---------------------------------------------------------------------------

def test_email_failure_fallback_does_not_send_success_confirmation(base):
    """Telegram fallback on email failure must not contain the ✅ success confirmation."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    msg = ca._openclaw_send.call_args.args[0]
    assert "✅" not in msg


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
# Direct callback path (OpenClaw passes callback data as CLI args)
# ---------------------------------------------------------------------------

_DIRECT_ARGS = [
    "--callback-query-id", "cq_direct",
    "--callback-data", "approve",
    "--message-id", "42",
]

_DIRECT_REJECT_ARGS = [
    "--callback-query-id", "cq_direct_reject",
    "--callback-data", "reject",
    "--message-id", "42",
]


def test_direct_path_does_not_call_get_updates(base):
    """Direct callback path skips getUpdates — OpenClaw already consumed the update."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.telegram_api.get_updates.assert_not_called()


def test_direct_path_approve_calls_answer_callback_query(base):
    """Direct approve path still calls answer_callback_query with the provided ID."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.telegram_api.answer_callback_query.assert_called_once_with("cq_direct")


def test_direct_path_approve_sends_email(base):
    """Direct approve path calls _send_approval_email with the correct args."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca._send_approval_email.assert_called_once_with(
        "agent@example.com",
        "admin@example.com",
        _PROJECT,
        _PENDING["drive_folder_link"],
    )


def test_direct_path_approve_clears_pending_approval(base):
    """Direct approve path clears the pending approval from state."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()


def test_direct_path_approve_does_not_set_telegram_offset(base):
    """Direct path does not touch the Telegram offset — OpenClaw manages it."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.state.set_telegram_offset.assert_not_called()


def test_direct_path_reject_calls_drive_delete(base):
    """Direct reject path calls drive.delete with the video file ID."""
    import scripts.check_approval as ca
    main(_DIRECT_REJECT_ARGS)
    ca.drive.delete.assert_called_once_with(_PENDING["drive_video_file_id"])


def test_direct_path_reject_sends_telegram_notification(base):
    """Direct reject path sends the Telegram rejection notification."""
    import scripts.check_approval as ca
    main(_DIRECT_REJECT_ARGS)
    ca._openclaw_send.assert_called_once()
    assert "❌" in ca._openclaw_send.call_args.args[0]


def test_direct_path_wrong_message_id_returns_early(base):
    """Direct path with mismatched message_id returns without processing."""
    import scripts.check_approval as ca
    main(["--callback-query-id", "cq_x", "--callback-data", "approve", "--message-id", "999"])
    ca.telegram_api.answer_callback_query.assert_not_called()
    ca._send_approval_email.assert_not_called()
    ca.state.clear_pending_approval.assert_not_called()


def test_direct_path_answer_failure_does_not_clear_state(base):
    """If answer_callback_query fails on direct path, state is not cleared."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_ARGS)
    ca.state.clear_pending_approval.assert_not_called()
    ca._send_approval_email.assert_not_called()


def test_direct_path_answer_failure_does_not_set_offset(base):
    """If answer_callback_query fails on direct path, offset is not touched."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_ARGS)
    ca.state.set_telegram_offset.assert_not_called()


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
