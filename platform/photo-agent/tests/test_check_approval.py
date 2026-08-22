"""
Tests for scripts/check_approval.py.

All external calls (state, Telegram API, Drive, email) are mocked.
Tests call main() directly and verify behaviour through mock assertions.
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

# update_id values chosen so new_offset assertions are unambiguous
_APPROVE_UPDATE = {
    "update_id": 100,
    "callback_query": {
        "id": "cq_approve",
        "data": "approve",
        "message": {"message_id": 42, "chat": {"id": int(_CHAT_ID)}},
    },
}

_REJECT_UPDATE = {
    "update_id": 101,
    "callback_query": {
        "id": "cq_reject",
        "data": "reject",
        "message": {"message_id": 42, "chat": {"id": int(_CHAT_ID)}},
    },
}


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
    mocker.patch("scripts.check_approval.state.get_telegram_offset", return_value=50)
    mocker.patch("scripts.check_approval.state.set_telegram_offset")
    mocker.patch("scripts.check_approval.state.clear_pending_approval")
    mocker.patch("scripts.check_approval.telegram_api.get_updates", return_value=[])
    mocker.patch("scripts.check_approval.telegram_api.answer_callback_query")
    mocker.patch("scripts.check_approval.telegram_api.edit_message_reply_markup")
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
    return mocker


# ---------------------------------------------------------------------------
# No pending approval
# ---------------------------------------------------------------------------

def test_no_pending_approval_exits_immediately(mocker, env, lock_mock):
    """When no approval is pending, the script returns without calling Telegram or Drive."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_get_updates = mocker.patch("scripts.check_approval.telegram_api.get_updates")
    mock_drive_delete = mocker.patch("scripts.check_approval.drive.delete")
    main([])
    mock_get_updates.assert_not_called()
    mock_drive_delete.assert_not_called()


def test_no_pending_approval_does_not_modify_state(mocker, env, lock_mock):
    """When no approval is pending, set_telegram_offset and clear_pending_approval are not called."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval.state.get_pending_approval", return_value=None)
    mock_set_offset = mocker.patch("scripts.check_approval.state.set_telegram_offset")
    mock_clear = mocker.patch("scripts.check_approval.state.clear_pending_approval")
    main([])
    mock_set_offset.assert_not_called()
    mock_clear.assert_not_called()


def test_source_cron_no_pending_exits_silently(mocker, env, lock_mock):
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
    ca._notify_admin.side_effect = lambda *a, **kw: call_order.append("notify")

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
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    assert local_file.exists()


def test_approve_does_not_delete_local_file(base, mocker):
    """approve path does NOT delete the local video file — upload_facebook.py owns deletion."""
    import scripts.check_approval as ca
    mock_delete = mocker.patch("scripts.check_approval._delete_local_file")
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    mock_delete.assert_not_called()


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
    ca._notify_admin.side_effect = lambda *a, **kw: call_order.append("notify")

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
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert "❌" in msg


def test_reject_message_instructs_to_update_and_retrigger(base):
    """Telegram rejection message instructs the admin to update photos and re-trigger."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    msg = ca._notify_admin.call_args.args[0]
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
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
    assert _PENDING["drive_folder_link"] in msg


def test_email_failure_fallback_indicates_delivery_failed(base):
    """Telegram fallback on email failure contains a word indicating the failure."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca._send_approval_email.side_effect = RuntimeError("SMTP error")
    main([])
    msg = ca._notify_admin.call_args.args[0]
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
    ca._notify_admin.assert_called_once()
    msg = ca._notify_admin.call_args.args[0]
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
    msg = ca._notify_admin.call_args.args[0]
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
# Direct callback path (the /check_approval skill passes callback data as CLI args)
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
    """Direct callback path skips getUpdates — it never observes the raw callback_query at all."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.telegram_api.get_updates.assert_not_called()


def test_direct_path_approve_calls_answer_callback_query(base):
    """Direct approve path calls answer_callback_query with the provided ID and toast text."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.telegram_api.answer_callback_query.assert_called_once_with("cq_direct", text="✅ Approving...")


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
    """Direct path does not touch the Telegram offset — there's no getUpdates poll to advance."""
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
    ca._notify_admin.assert_called_once()
    assert "❌" in ca._notify_admin.call_args.args[0]


def test_direct_path_wrong_message_id_returns_early(base):
    """Direct path with mismatched message_id returns without processing."""
    import scripts.check_approval as ca
    main(["--callback-query-id", "cq_x", "--callback-data", "approve", "--message-id", "999"])
    ca.telegram_api.answer_callback_query.assert_not_called()
    ca._send_approval_email.assert_not_called()
    ca.state.clear_pending_approval.assert_not_called()


def test_direct_path_answer_failure_is_nonfatal(base):
    """answer_callback_query failure on direct path is a warning — approval still proceeds."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()
    ca._send_approval_email.assert_called_once()


def test_direct_path_answer_failure_does_not_set_offset(base):
    """Even when answer_callback_query fails, the direct path does not touch the offset."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_ARGS)
    ca.state.set_telegram_offset.assert_not_called()


# ---------------------------------------------------------------------------
# Direct path — callback-data only (no callback-query-id or message-id)
# ---------------------------------------------------------------------------

_DATA_ONLY_APPROVE = ["--callback-data", "approve"]
_DATA_ONLY_REJECT = ["--callback-data", "reject"]


def test_data_only_does_not_call_get_updates(base):
    """--callback-data alone bypasses getUpdates."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca.telegram_api.get_updates.assert_not_called()


def test_data_only_does_not_call_answer_callback_query(base):
    """--callback-data alone skips answer_callback_query (no ID available; spinner auto-clears)."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca.telegram_api.answer_callback_query.assert_not_called()


def test_data_only_approve_sends_email(base):
    """--callback-data approve sends the approval email."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca._send_approval_email.assert_called_once()


def test_data_only_approve_clears_state(base):
    """--callback-data approve clears the pending approval."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca.state.clear_pending_approval.assert_called_once()


def test_data_only_approve_does_not_set_offset(base):
    """--callback-data approve does not touch the Telegram offset."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca.state.set_telegram_offset.assert_not_called()


def test_data_only_reject_deletes_drive_file(base):
    """--callback-data reject calls drive.delete."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_REJECT)
    ca.drive.delete.assert_called_once_with(_PENDING["drive_video_file_id"])


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
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.activity_log.log_approved.side_effect = ValueError("bad project_name")
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_activity_log_oserror_on_approve_does_not_block_state_clear(base, mocker):
    """OSError from activity_log.log_approved is caught — state is still cleared."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.activity_log.log_approved.side_effect = OSError("disk full")
    main([])
    ca.state.clear_pending_approval.assert_called_once()


def test_activity_log_error_on_reject_does_not_block_state_clear(base, mocker):
    """ValueError from activity_log.log_rejected is caught — state is still cleared."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    ca.activity_log.log_rejected.side_effect = ValueError("bad project_name")
    main([])
    ca.state.clear_pending_approval.assert_called_once()


# ---------------------------------------------------------------------------
# Direct path — answer_callback_query failure on reject (L5)
# ---------------------------------------------------------------------------

def test_direct_reject_answer_failure_is_nonfatal(base):
    """answer_callback_query failure on direct reject path is a warning — rejection still proceeds."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_REJECT_ARGS)
    ca.state.clear_pending_approval.assert_called_once()
    ca.drive.delete.assert_called_once_with(_PENDING["drive_video_file_id"])


# ---------------------------------------------------------------------------
# Button removal (edit_message_reply_markup)
# ---------------------------------------------------------------------------

def test_cron_approve_removes_buttons(base):
    """Cron approve path removes the inline keyboard immediately after acknowledging the tap."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_cron_reject_removes_buttons(base):
    """Cron reject path removes the inline keyboard immediately after acknowledging the tap."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_REJECT_UPDATE]
    main([])
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_direct_approve_removes_buttons(base):
    """Direct approve path removes the inline keyboard via _acknowledge_tap."""
    import scripts.check_approval as ca
    main(_DIRECT_ARGS)
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_direct_reject_removes_buttons(base):
    """Direct reject path removes the inline keyboard via _acknowledge_tap."""
    import scripts.check_approval as ca
    main(_DIRECT_REJECT_ARGS)
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_data_only_approve_removes_buttons(base):
    """--callback-data alone (no ID) still removes the inline keyboard."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_APPROVE)
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_data_only_reject_removes_buttons(base):
    """--callback-data reject still removes the inline keyboard."""
    import scripts.check_approval as ca
    main(_DATA_ONLY_REJECT)
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


def test_cron_answer_failure_does_not_remove_buttons(base):
    """Cron path early-exits on answer_callback_query failure — buttons are not removed."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main([])
    ca.telegram_api.edit_message_reply_markup.assert_not_called()


def test_direct_answer_failure_still_removes_buttons(base):
    """Direct path answer_callback_query failure is non-fatal — buttons are still removed."""
    import scripts.check_approval as ca
    ca.telegram_api.answer_callback_query.side_effect = RuntimeError("timeout")
    main(_DIRECT_ARGS)
    ca.telegram_api.edit_message_reply_markup.assert_called_once_with(
        _CHAT_ID, _PENDING["telegram_message_id"]
    )


# ---------------------------------------------------------------------------
# Cron re-entrancy lock
# ---------------------------------------------------------------------------

def test_cron_lock_contention_exits_silently(mocker, env):
    """If check_approval.lock is held by another instance, main() exits without taking action."""
    mocker.patch("scripts.check_approval._load_env")
    mocker.patch("scripts.check_approval._try_acquire_check_lock", return_value=None)
    mock_get_pending = mocker.patch("scripts.check_approval.state.get_pending_approval")
    main([])
    mock_get_pending.assert_not_called()


# ---------------------------------------------------------------------------
# Callback chat_id guard (_find_matching_callback)
# ---------------------------------------------------------------------------

def test_callback_wrong_chat_id_is_not_matched(base):
    """A callback_query from a different chat is not matched even if message_id is correct."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {
            "update_id": 100,
            "callback_query": {
                "id": "cq_wrong_chat",
                "data": "approve",
                "message": {
                    "message_id": 42,          # matches pending
                    "chat": {"id": 999999999},  # wrong chat
                },
            },
        }
    ]
    main([])
    ca.state.clear_pending_approval.assert_not_called()
    ca._send_approval_email.assert_not_called()


def test_callback_correct_chat_id_is_matched(base):
    """A callback_query from the correct chat is matched when chat_id matches."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [
        {
            "update_id": 100,
            "callback_query": {
                "id": "cq_right_chat",
                "data": "approve",
                "message": {
                    "message_id": 42,
                    "chat": {"id": int(_CHAT_ID)},  # correct chat
                },
            },
        }
    ]
    main([])
    ca.state.clear_pending_approval.assert_called_once()


# ---------------------------------------------------------------------------
# argparse choices — invalid --callback-data rejected
# ---------------------------------------------------------------------------

def test_invalid_callback_data_rejected_by_argparse(mocker, env, lock_mock):
    """argparse rejects --callback-data values other than 'approve' or 'reject'."""
    mocker.patch("scripts.check_approval._load_env")
    with pytest.raises(SystemExit):
        main(["--callback-data", "Approve"])  # capital A — not in choices


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
    import scripts.check_approval as ca
    base.patch("scripts.check_approval.facebook_state.is_published", return_value=False)
    return base


def test_approve_enqueues_facebook_upload(base_fb):
    """approve path calls facebook_state.set_pending_upload with correct required fields."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    main([])
    ca.facebook_state.set_pending_upload.assert_called_once()
    record = ca.facebook_state.set_pending_upload.call_args.args[0]
    assert record["project_name"] == _PROJECT
    assert record["video_local_path"] == _PENDING["video_local_path"]
    assert record["page_id"] == _FB_PAGE_ID
    assert record["idempotency_key"] == str(_PENDING["telegram_message_id"])


def test_approve_enqueue_idempotency_skip(base_fb):
    """If is_published(key) returns True, set_pending_upload is NOT called."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.facebook_state.is_published.return_value = True
    main([])
    ca.facebook_state.set_pending_upload.assert_not_called()


def test_approve_enqueue_skipped_without_fb_page_id(base, mocker, monkeypatch):
    """When FB_PAGE_ID is not set, facebook_state.set_pending_upload is never called."""
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    mock_set = mocker.patch("scripts.check_approval.facebook_state.set_pending_upload")
    main([])
    mock_set.assert_not_called()


def test_approve_enqueue_failure_does_not_abort_approve_flow(base_fb):
    """A facebook_state exception during enqueue is caught — the approve flow still completes."""
    import scripts.check_approval as ca
    ca.telegram_api.get_updates.return_value = [_APPROVE_UPDATE]
    ca.facebook_state.set_pending_upload.side_effect = Exception("state error")
    main([])
    ca.state.clear_pending_approval.assert_called_once()
    ca.state.set_telegram_offset.assert_called_once_with(101)


# ---------------------------------------------------------------------------
# _notify_admin — direct Telegram Bot API notification (replaces openclaw CLI, #14)
# ---------------------------------------------------------------------------
#
# These tests exercise the real _notify_admin() body (unlike the tests above,
# which mock it out entirely). telegram_api.send_message is always mocked —
# never a real HTTP call — so no test here can reach live Telegram.

def test_notify_admin_sends_via_telegram_api(env, mocker):
    """_notify_admin() calls telegram_api.send_message with the configured chat_id and message."""
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
