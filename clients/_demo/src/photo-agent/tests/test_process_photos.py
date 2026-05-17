"""
Tests for scripts/process_photos.py.

All external calls (Drive, FFmpeg, Telegram API, state, logger) are mocked.
Tests call main() directly and verify behaviour through mock assertions.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.process_photos import main

_PROJECT = "test_project"
_TWO_PHOTOS = [
    {"id": "f1", "name": "photo01.jpg"},
    {"id": "f2", "name": "photo02.jpg"},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    """Set required environment variables; use tmp_path as VIDEO_TMP_DIR."""
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("DRIVE_ROOT_FOLDER_ID", "root_folder_id")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def base(mocker, env):
    """Mocks common to all tests: env loading, run lock, and the no-pending-approval guard."""
    mocker.patch("scripts.process_photos._load_env")
    mocker.patch("scripts.process_photos._acquire_run_lock", return_value=MagicMock())
    mocker.patch("scripts.process_photos.fcntl.flock")
    mocker.patch("scripts.process_photos.state.get_pending_approval", return_value=None)
    return mocker


@pytest.fixture
def happy(base, env):
    """All mocks wired for a successful two-photo run."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch("scripts.process_photos.drive.list_photos", return_value=_TWO_PHOTOS)
    base.patch("scripts.process_photos.drive.download")
    base.patch("scripts.process_photos.drive.upload", return_value="video_file_id")
    base.patch(
        "scripts.process_photos.drive.folder_link",
        return_value="https://drive.google.com/drive/folders/x",
    )
    base.patch(
        "scripts.process_photos.telegram_api.send_message_with_buttons", return_value=42
    )
    base.patch("scripts.process_photos.state.set_pending_approval")
    base.patch("scripts.process_photos.activity_log.log_downloaded")
    base.patch("scripts.process_photos.activity_log.log_generated")
    base.patch("scripts.process_photos.activity_log.log_uploaded")
    base.patch("scripts.process_photos.activity_log.log_approval_req")
    gen_cls = base.patch("scripts.process_photos.FFmpegVideoGenerator")
    gen_cls.return_value.generate.side_effect = lambda photos, cfg, out: out
    return base


# ---------------------------------------------------------------------------
# Missing --project arg
# ---------------------------------------------------------------------------

def test_missing_project_arg_exits_nonzero(mocker, env):
    """No --project arg sends a Telegram usage error and exits non-zero."""
    mocker.patch("scripts.process_photos._load_env")
    mock_err = mocker.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1
    mock_err.assert_called_once()
    assert "Usage" in mock_err.call_args.args[0]


# ---------------------------------------------------------------------------
# Guard: existing pending approval
# ---------------------------------------------------------------------------

def test_existing_pending_approval_exits(base):
    """An existing pending approval sends a Telegram error and exits."""
    base.patch(
        "scripts.process_photos.state.get_pending_approval",
        return_value={"project_name": "other"},
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "already awaiting approval" in mock_err.call_args.args[0]


# ---------------------------------------------------------------------------
# Drive folder not found
# ---------------------------------------------------------------------------

def test_drive_folder_not_found_exits(base):
    """DriveFolderNotFoundError sends a Telegram error and exits."""
    from tools.drive import DriveFolderNotFoundError
    base.patch(
        "scripts.process_photos.drive.find_folder",
        side_effect=DriveFolderNotFoundError(
            "not found", name=_PROJECT, parent_id="root_folder_id"
        ),
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "folder not found" in mock_err.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# Photo count validation
# ---------------------------------------------------------------------------

def test_fewer_than_2_photos_exits(base):
    """Fewer than 2 photos sends a Telegram error including the count and exits."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch(
        "scripts.process_photos.drive.list_photos",
        return_value=[{"id": "f1", "name": "photo01.jpg"}],
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "1" in mock_err.call_args.args[0]


def test_more_than_30_photos_exits(base):
    """More than 30 photos sends a Telegram error and exits."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch(
        "scripts.process_photos.drive.list_photos",
        return_value=[{"id": str(i), "name": f"photo{i:02d}.jpg"} for i in range(31)],
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "31" in mock_err.call_args.args[0]


# ---------------------------------------------------------------------------
# Download failure
# ---------------------------------------------------------------------------

def test_download_failure_exits(base):
    """A download failure sends a Telegram error and exits."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch("scripts.process_photos.drive.list_photos", return_value=_TWO_PHOTOS)
    base.patch(
        "scripts.process_photos.drive.download",
        side_effect=RuntimeError("network error"),
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "download failed" in mock_err.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# FFmpeg failure
# ---------------------------------------------------------------------------

def test_ffmpeg_failure_exits_with_reason(base):
    """VideoGenerationError sends a Telegram error including the reason and exits."""
    from tools.video_generator import VideoGenerationError
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch("scripts.process_photos.drive.list_photos", return_value=_TWO_PHOTOS)
    base.patch("scripts.process_photos.drive.download")
    base.patch("scripts.process_photos.activity_log.log_downloaded")
    gen_cls = base.patch("scripts.process_photos.FFmpegVideoGenerator")
    gen_cls.return_value.generate.side_effect = VideoGenerationError("codec not found")
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    msg = mock_err.call_args.args[0]
    assert "video generation failed" in msg.lower()
    assert "codec not found" in msg


# ---------------------------------------------------------------------------
# Drive upload failure
# ---------------------------------------------------------------------------

def test_upload_failure_exits_and_does_not_set_state(base):
    """Upload failure sends a Telegram error; state.set_pending_approval is never called."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch("scripts.process_photos.drive.list_photos", return_value=_TWO_PHOTOS)
    base.patch("scripts.process_photos.drive.download")
    base.patch("scripts.process_photos.activity_log.log_downloaded")
    gen_cls = base.patch("scripts.process_photos.FFmpegVideoGenerator")
    gen_cls.return_value.generate.side_effect = lambda photos, cfg, out: out
    base.patch("scripts.process_photos.activity_log.log_generated")
    base.patch(
        "scripts.process_photos.drive.upload",
        side_effect=RuntimeError("quota exceeded"),
    )
    mock_set_state = base.patch("scripts.process_photos.state.set_pending_approval")
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "upload failed" in mock_err.call_args.args[0].lower()
    mock_set_state.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_calls_tools_in_sequence(happy, env):
    """Tools are called: discover → download × N → generate → upload → message → state."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    proc.drive.find_folder.assert_called_once_with(_PROJECT, "root_folder_id")
    proc.drive.list_photos.assert_called_once_with("folder_id")
    assert proc.drive.download.call_count == 2
    proc.FFmpegVideoGenerator.return_value.generate.assert_called_once()
    proc.drive.upload.assert_called_once()
    proc.telegram_api.send_message_with_buttons.assert_called_once()
    proc.state.set_pending_approval.assert_called_once()


def test_happy_path_state_set_with_required_fields(happy, env):
    """set_pending_approval() is called with all required keys and correct values."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    record = proc.state.set_pending_approval.call_args.args[0]
    for key in (
        "project_name", "drive_folder_id", "drive_video_file_id",
        "drive_folder_link", "video_local_path", "telegram_message_id", "triggered_at",
    ):
        assert key in record, f"Missing key: {key}"
    assert record["project_name"] == _PROJECT
    assert record["telegram_message_id"] == 42


def test_happy_path_approval_message_has_approve_reject_buttons(happy, env):
    """send_message_with_buttons() is called with ✅ Approve and ❌ Reject button labels."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    _, _, buttons = proc.telegram_api.send_message_with_buttons.call_args.args
    labels = [label for label, _ in buttons]
    assert "✅ Approve" in labels
    assert "❌ Reject" in labels


def test_happy_path_temp_dir_cleared_before_run(happy, env):
    """Stale project temp directory is removed and recreated at the start of each run."""
    project_tmp = env / _PROJECT
    project_tmp.mkdir()
    stale = project_tmp / "old_video.mp4"
    stale.write_text("stale")
    main(["--project", _PROJECT])
    assert project_tmp.exists()
    assert not stale.exists()


def test_happy_path_message_includes_project_name_count_and_duration(happy, env):
    """Approval message text includes project name, photo count, and video duration."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    _, text, _ = proc.telegram_api.send_message_with_buttons.call_args.args
    assert _PROJECT in text
    assert "2" in text          # photo count
    assert "7.5" in text        # duration: 2 × 4s − 1 × 0.5s = 7.5s
