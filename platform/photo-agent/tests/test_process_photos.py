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
    mocker.patch("scripts.process_photos.activity_log.log_command")
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
        "scripts.process_photos.telegram_api.send_message", return_value=42
    )
    base.patch("scripts.process_photos.state.set_pending_approval")
    base.patch("scripts.process_photos.activity_log.log_downloaded")
    base.patch("scripts.process_photos.activity_log.log_generated")
    base.patch("scripts.process_photos.activity_log.log_uploaded")
    base.patch("scripts.process_photos.activity_log.log_approval_req")

    def _fake_generate(photos, cfg, out):
        out.write_bytes(b"\x00" * 100)
        return out

    gen_cls = base.patch("scripts.process_photos.FFmpegVideoGenerator")
    gen_cls.return_value.generate.side_effect = _fake_generate
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
# Project name validation (C3)
# ---------------------------------------------------------------------------

def test_invalid_project_name_exits(mocker, env):
    """A project name with disallowed characters sends a Telegram error and exits."""
    mocker.patch("scripts.process_photos._load_env")
    mock_err = mocker.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", "bad/name"])
    mock_err.assert_called_once()
    assert "Invalid project name" in mock_err.call_args.args[0]


# ---------------------------------------------------------------------------
# Missing required env vars (M7)
# ---------------------------------------------------------------------------

def test_missing_chat_id_exits(base):
    """Missing ADMIN_TELEGRAM_CHAT_ID sends a Telegram error and exits."""
    base._mocker.stopall if hasattr(base, "_mocker") else None
    # Re-use base but unset the env var via a fresh monkeypatch approach:
    # The env fixture already set it, so we patch the environ lookup result.
    import os
    orig = os.environ.pop("ADMIN_TELEGRAM_CHAT_ID", None)
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    try:
        with pytest.raises(SystemExit):
            main(["--project", _PROJECT])
    finally:
        if orig is not None:
            os.environ["ADMIN_TELEGRAM_CHAT_ID"] = orig
    mock_err.assert_called_once()
    assert "ADMIN_TELEGRAM_CHAT_ID" in mock_err.call_args.args[0]


# ---------------------------------------------------------------------------
# Invalid SECONDS_PER_PHOTO (H5)
# ---------------------------------------------------------------------------

def test_invalid_seconds_per_photo_exits(base, monkeypatch):
    """Non-integer SECONDS_PER_PHOTO sends a Telegram error and exits."""
    monkeypatch.setenv("SECONDS_PER_PHOTO", "fast")
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "SECONDS_PER_PHOTO" in mock_err.call_args.args[0]


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
# Invalid photo filename (C4)
# ---------------------------------------------------------------------------

def test_invalid_photo_filename_exits(base):
    """A photo with a non-image extension sends a Telegram error and exits."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch(
        "scripts.process_photos.drive.list_photos",
        return_value=[
            {"id": "f1", "name": "photo01.jpg"},
            {"id": "f2", "name": "malicious.sh"},
        ],
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()


def test_duplicate_photo_filename_exits(base):
    """Duplicate photo filenames in Drive send a Telegram error and exit."""
    base.patch("scripts.process_photos.drive.find_folder", return_value="folder_id")
    base.patch(
        "scripts.process_photos.drive.list_photos",
        return_value=[
            {"id": "f1", "name": "photo01.jpg"},
            {"id": "f2", "name": "photo01.jpg"},  # duplicate
        ],
    )
    mock_err = base.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    assert "duplicate" in mock_err.call_args.args[0].lower()


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

    def _fake_generate(photos, cfg, out):
        out.write_bytes(b"\x00" * 100)
        return out

    gen_cls = base.patch("scripts.process_photos.FFmpegVideoGenerator")
    gen_cls.return_value.generate.side_effect = _fake_generate
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
# Telegram send failure (M6)
# ---------------------------------------------------------------------------

def test_telegram_send_failure_exits_and_does_not_set_state(happy, env):
    """Telegram send failure sends a Telegram error; state.set_pending_approval is not called."""
    import scripts.process_photos as proc
    happy.patch(
        "scripts.process_photos.telegram_api.send_message",
        side_effect=RuntimeError("chat not found"),
    )
    mock_set_state = happy.patch("scripts.process_photos.state.set_pending_approval")
    mock_err = happy.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    assert "approval message" in mock_err.call_args.args[0].lower()
    mock_set_state.assert_not_called()


# ---------------------------------------------------------------------------
# State write failure (M8)
# ---------------------------------------------------------------------------

def test_state_write_failure_exits(happy, env):
    """state.set_pending_approval failure sends a Telegram error and exits."""
    happy.patch(
        "scripts.process_photos.state.set_pending_approval",
        side_effect=RuntimeError("disk full"),
    )
    mock_err = happy.patch(
        "scripts.process_photos._telegram_error", side_effect=SystemExit(1)
    )
    with pytest.raises(SystemExit):
        main(["--project", _PROJECT])
    mock_err.assert_called_once()
    msg = mock_err.call_args.args[0]
    assert "state write failed" in msg.lower()


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
    proc.telegram_api.send_message.assert_called_once()
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


def test_happy_path_approval_message_has_no_inline_buttons(happy, env):
    """send_message() (plain text, chat_id + text + parse_mode only) is called — issue #49
    removed the inline Approve/Reject keyboard entirely, so there is no buttons/reply_markup
    argument left for it to appear in."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    proc.telegram_api.send_message.assert_called_once()
    args, kwargs = proc.telegram_api.send_message.call_args
    assert len(args) == 2  # chat_id, text — no positional buttons argument
    assert set(kwargs) <= {"parse_mode"}


def test_happy_path_approval_message_instructs_approve_or_reject_commands(happy, env):
    """Approval message text tells the admin to reply /photo_approve or
    /photo_reject (issue #49) — not to tap a button. Not the shorter
    /approve — that collides with a Hermes core command; see
    test_photo_approve_dispatch.py."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    _, text = proc.telegram_api.send_message.call_args.args
    assert "/photo_approve" in text
    assert "/photo_reject" in text


def test_happy_path_approval_message_sent_as_plain_text(happy, env):
    """send_message() is called with no parse_mode — issue #54: Telegram's legacy
    'Markdown' parse_mode treats unescaped '_' as an italic delimiter, which
    silently stripped the underscores from /photo_approve and /photo_reject
    ('Reply /photoapprove or /photoreject.'). Plain text sidesteps that class of
    bug entirely rather than requiring escaping to be kept correct forever."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    kwargs = proc.telegram_api.send_message.call_args.kwargs
    assert "parse_mode" not in kwargs


def _render_legacy_markdown_italics(text: str) -> str:
    """Reimplements Telegram's legacy 'Markdown' parse_mode italics extraction,
    per https://core.telegram.org/bots/api#markdown-style: an unescaped '_' opens
    an italic span and the next unescaped '_' closes it, and both delimiter
    characters are stripped from the rendered output — regardless of what's
    between them or whether it was an intentional italic span. '\\_' escapes a
    literal underscore. This is what actually produced the issue #54 corruption
    and lets tests assert against Telegram's own rendering, not just our code's
    parse_mode flag."""
    out: list[str] = []
    i = 0
    in_italic = False
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "_":
            in_italic = not in_italic
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_legacy_markdown_italics_simulator_matches_observed_corruption():
    """Sanity-checks the simulator itself against the exact corruption reported
    live in issue #54, so the regression test below can be trusted."""
    assert (
        _render_legacy_markdown_italics("Reply /photo_approve or /photo_reject.")
        == "Reply /photoapprove or /photoreject."
    )


def test_happy_path_approval_message_survives_legacy_markdown_rendering(happy, env):
    """Regression test for issue #54: even if a future change reintroduces a
    Markdown-family parse_mode on this call without correct escaping, the message
    text must not rely on unescaped underscores that Telegram's legacy Markdown
    would strip. Runs the actual sent text through a reimplementation of
    Telegram's own legacy-Markdown rendering and asserts the command names
    survive intact."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    args, kwargs = proc.telegram_api.send_message.call_args
    _, text = args
    # Only legacy "Markdown" actually runs this entity extraction on Telegram's
    # side; plain text (today's fix) and MarkdownV2 (which uses different escaping
    # rules entirely) are unaffected, so only simulate rendering in that case.
    rendered = _render_legacy_markdown_italics(text) if kwargs.get("parse_mode") == "Markdown" else text
    assert "/photo_approve" in rendered
    assert "/photo_reject" in rendered


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
    _, text = proc.telegram_api.send_message.call_args.args
    assert _PROJECT in text
    assert "2" in text          # photo count
    assert "7.5" in text        # duration: 2 × 4s − 1 × 0.5s = 7.5s


def test_happy_path_scrub_is_called(happy, env):
    """scrub() is called on the downloaded photos before video generation."""
    import scripts.process_photos as proc
    mock_scrub = happy.patch(
        "scripts.process_photos.scrub", side_effect=lambda photos: photos
    )
    main(["--project", _PROJECT])
    mock_scrub.assert_called_once()
    scrubbed = mock_scrub.call_args.args[0]
    assert len(scrubbed) == 2


# ---------------------------------------------------------------------------
# _telegram_error — direct Telegram Bot API notification (replaces openclaw CLI, #14)
# ---------------------------------------------------------------------------
#
# These tests exercise the real _telegram_error() body directly (unlike the tests
# above, which mock it out entirely via base()). telegram_api.send_message is
# always mocked — never a real HTTP call — so no test here can reach live Telegram.

def test_telegram_error_sends_via_telegram_api_and_exits(env, mocker):
    """_telegram_error() calls telegram_api.send_message with chat_id + message, then exits 1.

    Deliberately stays on the default token (Hermes's TELEGRAM_BOT_TOKEN, not the
    approval bot from issue #29) — pipeline-failure notifications aren't part of the
    button-callback race and relaying them through Hermes's usual bot keeps error
    visibility consistent with every other Hermes-relayed message.
    """
    import scripts.process_photos as proc
    mock_send = mocker.patch("scripts.process_photos.telegram_api.send_message")
    with pytest.raises(SystemExit) as exc_info:
        proc._telegram_error("something broke")
    mock_send.assert_called_once_with("12345", "something broke")
    assert exc_info.value.code == 1


def test_telegram_error_exits_even_when_send_fails(env, mocker):
    """A RuntimeError from telegram_api.send_message is swallowed — exit(1) still happens."""
    import scripts.process_photos as proc
    mocker.patch(
        "scripts.process_photos.telegram_api.send_message",
        side_effect=RuntimeError("Telegram HTTP error 500"),
    )
    with pytest.raises(SystemExit) as exc_info:
        proc._telegram_error("something broke")
    assert exc_info.value.code == 1


def test_telegram_error_exits_even_when_send_fails_with_non_runtime_error(env, mocker):
    """A non-RuntimeError from telegram_api.send_message (e.g. a malformed Telegram
    response triggering an AttributeError deep in telegram_api) is also swallowed —
    exit(1) must not depend on the exception type raised by the notification call."""
    import scripts.process_photos as proc
    mocker.patch(
        "scripts.process_photos.telegram_api.send_message",
        side_effect=AttributeError("'NoneType' object has no attribute 'get'"),
    )
    with pytest.raises(SystemExit) as exc_info:
        proc._telegram_error("something broke")
    assert exc_info.value.code == 1


def test_telegram_error_no_chat_id_skips_send_but_still_exits(monkeypatch, mocker):
    """With ADMIN_TELEGRAM_CHAT_ID unset, _telegram_error() skips the API call but still exits 1."""
    import scripts.process_photos as proc
    monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)
    mock_send = mocker.patch("scripts.process_photos.telegram_api.send_message")
    with pytest.raises(SystemExit) as exc_info:
        proc._telegram_error("something broke")
    mock_send.assert_not_called()
    assert exc_info.value.code == 1
