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


class _TelegramEntityParseError(Exception):
    """Models the 400 'Can't parse entities' error Telegram's Bot API returns for
    an unmatched Markdown-style entity delimiter. The real API rejects the whole
    sendMessage call in this case rather than rendering something — it does not
    silently drop or ignore an unclosed entity."""


_LEGACY_MARKDOWN_ESCAPABLE = frozenset("_*`[")


def _render_legacy_markdown_v1_underscores(text: str) -> str:
    """Narrow, docs-grounded model of ONE piece of Telegram's legacy 'Markdown'
    parse_mode (https://core.telegram.org/bots/api#markdown-style): '_' pairing/
    stripping for italics, plus the escape-character set shared by all legacy
    Markdown entities. It is NOT a general reimplementation of Telegram's parser
    — it does not track '*' bold, '`' code, or '[...](...)' links as spans, and
    entity nesting is out of scope (the docs say nesting isn't supported anyway).
    It exists only to reason about, and regression-test, this underscore bug.

    Modeled, per the docs and TDLib's actual parser (the Bot API's underlying
    implementation:
    https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/telegram/MessageEntity.cpp#L1929-L2045):
    - An unescaped '_' toggles an italic span; on a matched pair, both delimiter
      characters are stripped from the output (this is what corrupted the issue
      #54 message: 'Reply /photo_approve or /photo_reject.' has exactly two
      unescaped '_', so the first opens and the second closes, both vanish).
    - Backslash-escaping is state-aware and only recognized OUTSIDE any open
      entity: "to escape characters '_', '*', '`', '[' outside of an entity,
      prepend the character '\\'" — and "escaping inside entities is not
      allowed, so entity must be closed first and reopened again". So once a '_'
      has opened an italic span, a backslash has no special meaning inside it —
      it's just a literal character — and the very next '_' always closes the
      entity, escaped or not. The docs' own worked example for an italicized
      string containing a literal underscore therefore closes the entity first,
      escapes the literal '_' in the outer scan, then reopens: '_snake_\\__case_'
      -> 'snake_case'. That round trip, and the negative case of trying to escape
      *inside* an already-open entity ('_snake\\_case_', which does NOT produce
      'snake_case'), are both used below as fixtures.
    - An unmatched (odd count of) unescaped '_' raises _TelegramEntityParseError,
      matching Telegram's documented/widely-observed behavior of rejecting the
      whole message with a 400 rather than rendering it some other way.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_entity = False
    while i < n:
        ch = text[i]
        if in_entity:
            # No escaping recognized inside an open entity — a backslash here is
            # just a literal character, and the next '_' always closes the span.
            if ch == "_":
                in_entity = False
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n and text[i + 1] in _LEGACY_MARKDOWN_ESCAPABLE:
            out.append(text[i + 1])
            i += 2
            continue
        if ch == "_":
            in_entity = True
            i += 1
            continue
        out.append(ch)
        i += 1
    if in_entity:
        raise _TelegramEntityParseError("Can't parse entities: can't find end of Italic entity")
    return "".join(out)


def test_legacy_markdown_v1_underscore_simulator_matches_observed_corruption():
    """Sanity-checks the simulator against the exact corruption reported live in
    issue #54, so the end-to-end regression test below can be trusted."""
    assert (
        _render_legacy_markdown_v1_underscores("Reply /photo_approve or /photo_reject.")
        == "Reply /photoapprove or /photoreject."
    )


def test_legacy_markdown_v1_underscore_simulator_matches_docs_escaping_example():
    """Sanity-checks escape-scoping against Telegram's own docs example for
    italicizing a string with a literal underscore in it: '_snake_\\__case_'
    (close the entity, escape the literal '_', reopen) renders as 'snake_case'."""
    assert _render_legacy_markdown_v1_underscores("_snake_\\__case_") == "snake_case"


def test_legacy_markdown_v1_underscore_simulator_rejects_escape_inside_open_entity():
    """Counterexample to the docs escaping example above: a backslash does NOT
    escape a '_' while already inside an open entity, only in the outer scan
    before an entity is opened (per the docs' 'escaping inside entities is not
    allowed' note and TDLib's real parser, linked above). So r'_snake\\_case_'
    does NOT render as 'snake_case' the way the properly-closed-and-reopened
    '_snake_\\__case_' does: the first '_' opens italics, the backslash inside it
    is just a literal character, the '_' right after it closes the entity
    regardless of that backslash, leaving a final, now-unmatched trailing '_' —
    an unclosed-entity rejection, not a successful escape."""
    with pytest.raises(_TelegramEntityParseError):
        _render_legacy_markdown_v1_underscores(r"_snake\_case_")


def test_legacy_markdown_v1_underscore_simulator_rejects_unmatched_underscore():
    """An odd number of unescaped underscores is an unclosed entity — Telegram's
    real API rejects the whole sendMessage call for this, it does not silently
    render around it. Relevant here because project names may contain
    underscores (_PROJECT_NAME_RE), so a project name with one underscore
    combined with the two in '/photo_approve'/'/photo_reject' would have made
    the old parse_mode='Markdown' call fail outright, not just corrupt text —
    a second, independent argument for the plain-text fix."""
    with pytest.raises(_TelegramEntityParseError):
        _render_legacy_markdown_v1_underscores("my_project: reply /photo_approve or /photo_reject.")


def test_happy_path_approval_message_survives_legacy_markdown_rendering(happy, env):
    """Regression test for issue #54. Asserts the invariant directly (no
    Markdown-family parse_mode is used for this call, so Telegram never runs
    entity parsing on it) and, in case a future change reintroduces one, also
    runs the actual sent text through the underscore simulator above so an
    unescaped-underscore regression would still be caught."""
    import scripts.process_photos as proc
    main(["--project", _PROJECT])
    args, kwargs = proc.telegram_api.send_message.call_args
    _, text = args
    assert kwargs.get("parse_mode") is None
    rendered = _render_legacy_markdown_v1_underscores(text) if kwargs.get("parse_mode") == "Markdown" else text
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
    assert "9" in text          # duration: 2 × 4s − 1 × 0.5s + 1.5s freeze = 9s


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
# VIDEO_TMP_DIR resolution — relative paths resolve per-client, not against the
# shared repo checkout (#47)
# ---------------------------------------------------------------------------

def test_relative_video_tmp_dir_resolves_against_fieldkit_data_dir(happy, monkeypatch, tmp_path):
    """A relative VIDEO_TMP_DIR (the formerly shipped .env.example default, now
    commented out — see #47) resolves under FIELDKIT_DATA_DIR, not the shared
    fieldkit repo checkout."""
    import scripts.process_photos as proc

    client_data_dir = tmp_path / "clients" / "mercury" / "data"
    monkeypatch.setenv("VIDEO_TMP_DIR", "data/photo-agent/tmp")  # the formerly shipped relative default
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(client_data_dir))

    main(["--project", _PROJECT])

    video_local_path = proc.state.set_pending_approval.call_args.args[0]["video_local_path"]
    repo_root = Path(proc.__file__).resolve().parents[3]
    assert video_local_path.startswith(str(client_data_dir / "data" / "photo-agent" / "tmp"))
    assert not video_local_path.startswith(str(repo_root / "data"))


def test_two_clients_same_relative_video_tmp_dir_do_not_collide(happy, monkeypatch, tmp_path):
    """Two clients that set the same relative VIDEO_TMP_DIR value (as mercury, venus,
    and _construction_co's .env.example formerly did, uncommented — see #47) must
    resolve to different absolute tmp directories — the cross-client collision at the
    heart of #47."""
    import scripts.process_photos as proc

    monkeypatch.setenv("VIDEO_TMP_DIR", "data/photo-agent/tmp")

    tmp_bases = {}
    for client in ("mercury", "venus"):
        monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "clients" / client / "data"))
        main(["--project", _PROJECT])
        video_local_path = proc.state.set_pending_approval.call_args.args[0]["video_local_path"]
        # video_local_path == tmp_base/<project_name>/<file>.mp4
        tmp_bases[client] = Path(video_local_path).parent.parent

    assert tmp_bases["mercury"] != tmp_bases["venus"]


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
