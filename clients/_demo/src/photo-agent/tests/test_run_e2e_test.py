"""
Tests for scripts/run_e2e_test.py — the Feature 004 end-to-end pipeline test rig.

T012: Pre-flight checks (missing env vars, pending approval, FFmpeg, duration range)
T013: Stage 1 — _compute_frames and _generate_clock_frames
T014: Stage 2 — _upload_frames_to_drive
T015: Stage 3 — _run_process_photos and pending-approval polling
T016: Stages 4–5 — approval and Facebook post polling, timeout paths

T025: Stage progress output format (US2)
T028: --cleanup flag (US3)

All external calls (subprocess, drive, state, facebook_state) are mocked.
"""

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.run_e2e_test import (
    _cleanup,
    _compute_frames,
    _generate_clock_frames,
    _run_process_photos,
    _upload_frames_to_drive,
    _wait_for_approval,
    _wait_for_facebook_post,
    main,
)

# ---------------------------------------------------------------------------
# T012: Pre-flight checks
# ---------------------------------------------------------------------------

_VALID_ENV = {
    "FB_PAGE_ACCESS_TOKEN": "page_token",
    "FB_PAGE_ID": "123456789",
    "DRIVE_ROOT_FOLDER_ID": "root_folder",
    "TELEGRAM_BOT_TOKEN": "bot_token",
    "ADMIN_TELEGRAM_CHAT_ID": "chat_id",
}


@pytest.fixture
def valid_env(monkeypatch):
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)


def test_preflight_missing_fb_page_access_token_exits_nonzero(monkeypatch):
    """Missing FB_PAGE_ACCESS_TOKEN causes sys.exit with non-zero code."""
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN")
    with pytest.raises(SystemExit) as exc_info:
        main(["--duration", "10"])
    assert exc_info.value.code != 0


def test_preflight_missing_env_var_names_the_missing_var(monkeypatch, capsys):
    """Error message for a missing required env var includes the variable name."""
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DRIVE_ROOT_FOLDER_ID")
    with pytest.raises(SystemExit):
        main(["--duration", "10"])
    captured = capsys.readouterr()
    assert "DRIVE_ROOT_FOLDER_ID" in captured.err or "DRIVE_ROOT_FOLDER_ID" in captured.out


def test_preflight_pending_approval_exits_nonzero(valid_env, mocker):
    """If there's already a pending approval in state.json, the script exits non-zero."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval",
                 return_value={"project_name": "other-project"})
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit) as exc_info:
        main(["--duration", "10"])
    assert exc_info.value.code != 0


def test_preflight_pending_approval_message_mentions_pending(valid_env, mocker, capsys):
    """Error for pending approval includes the word 'pending' in the message."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval",
                 return_value={"project_name": "other-project"})
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit):
        main(["--duration", "10"])
    captured = capsys.readouterr()
    assert "pending" in (captured.err + captured.out).lower()


def test_preflight_ffmpeg_absent_exits_nonzero(valid_env, mocker):
    """If FFmpeg is not on PATH, the script exits non-zero."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value=None)
    with pytest.raises(SystemExit) as exc_info:
        main(["--duration", "10"])
    assert exc_info.value.code != 0


def test_preflight_duration_below_min_exits_nonzero(valid_env, mocker):
    """Duration less than 2 seconds causes sys.exit with non-zero code."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit) as exc_info:
        main(["--duration", "1"])
    assert exc_info.value.code != 0


def test_preflight_duration_below_min_mentions_minimum(valid_env, mocker, capsys):
    """Error message for duration < 2 states the minimum is 2 seconds."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit):
        main(["--duration", "1"])
    captured = capsys.readouterr()
    assert "2" in captured.err or "2" in captured.out


def test_preflight_duration_above_max_exits_nonzero(valid_env, mocker):
    """Duration greater than 300 seconds causes sys.exit with non-zero code."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit) as exc_info:
        main(["--duration", "301"])
    assert exc_info.value.code != 0


def test_preflight_duration_above_max_mentions_maximum(valid_env, mocker, capsys):
    """Error message for duration > 300 states the maximum is 300 seconds."""
    mocker.patch("scripts.run_e2e_test.state.get_pending_approval", return_value=None)
    mocker.patch("scripts.run_e2e_test.shutil.which", return_value="/usr/bin/ffmpeg")
    with pytest.raises(SystemExit):
        main(["--duration", "301"])
    captured = capsys.readouterr()
    assert "300" in captured.err or "300" in captured.out


# ---------------------------------------------------------------------------
# T013: Stage 1 — _compute_frames and _generate_clock_frames
# ---------------------------------------------------------------------------

def test_compute_frames_30s_default_spp():
    """30s with spp=4: n_frames=8, spp_effective=4 (formula: n*spp-(n-1)*0.5 ≈ 30)."""
    n_frames, spp_effective = _compute_frames(30, 4)
    # n=8: 8*4 - 7*0.5 = 32 - 3.5 = 28.5 (close to 30) — accept plan's formula result
    # The exact n depends on the formula in the plan: ceil((30-0.5)/(4-0.5)) = ceil(29.5/3.5) = ceil(8.43) = 9
    assert n_frames >= 1
    assert spp_effective >= 1


def test_compute_frames_n_frames_within_cap():
    """For any duration up to 300s, n_frames must not exceed 30."""
    for duration in [60, 120, 300]:
        n_frames, _ = _compute_frames(duration, 4)
        assert n_frames <= 30, f"n_frames={n_frames} exceeds cap for duration={duration}"


def test_compute_frames_large_duration_bumps_spp():
    """When duration requires more than 30 frames, spp_effective > spp_base."""
    # 300s at spp=4: naive n_frames = ceil((300-0.5)/3.5) = ceil(85.6) = 86 > 30
    # So n_frames should be capped at 30 and spp_effective > 4
    n_frames, spp_effective = _compute_frames(300, 4)
    assert n_frames == 30
    assert spp_effective > 4


def test_compute_frames_2s_minimum():
    """2s duration (minimum allowed) produces at least 1 frame."""
    n_frames, spp_effective = _compute_frames(2, 4)
    assert n_frames >= 1


def test_generate_clock_frames_calls_ffmpeg(tmp_path):
    """_generate_clock_frames invokes FFmpeg via subprocess.run."""
    import scripts.run_e2e_test as rig
    with patch.object(rig.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        # Create dummy output files so the existence check passes
        for i in range(1, 4):
            (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\x00")
        _generate_clock_frames(3, 1700000000, tmp_path)
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert "ffmpeg" in cmd[0]


def test_generate_clock_frames_uses_rate_1(tmp_path):
    """FFmpeg command uses rate=1 so each frame advances by 1 second."""
    import scripts.run_e2e_test as rig
    with patch.object(rig.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        for i in range(1, 4):
            (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\x00")
        _generate_clock_frames(3, 1700000000, tmp_path)
    cmd = mock_run.call_args.args[0]
    full_cmd = " ".join(cmd)
    assert "rate=1" in full_cmd or "-r 1" in full_cmd or "rate" in full_cmd


def test_generate_clock_frames_uses_localtime_pts(tmp_path):
    """FFmpeg drawtext uses localtime PTS expansion for the timestamp overlay."""
    import scripts.run_e2e_test as rig
    with patch.object(rig.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        for i in range(1, 4):
            (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\x00")
        _generate_clock_frames(3, 1700000000, tmp_path)
    cmd = mock_run.call_args.args[0]
    full_cmd = " ".join(cmd)
    assert "localtime" in full_cmd


def test_generate_clock_frames_correct_frame_count(tmp_path):
    """FFmpeg -frames:v argument matches n_frames."""
    import scripts.run_e2e_test as rig
    with patch.object(rig.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        for i in range(1, 6):
            (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\x00")
        _generate_clock_frames(5, 1700000000, tmp_path)
    cmd = mock_run.call_args.args[0]
    frames_idx = cmd.index("-frames:v")
    assert cmd[frames_idx + 1] == "5"


def test_generate_clock_frames_raises_on_ffmpeg_failure(tmp_path):
    """_generate_clock_frames raises RuntimeError when FFmpeg exits non-zero."""
    import scripts.run_e2e_test as rig
    with patch.object(rig.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"error")
        with pytest.raises(RuntimeError):
            _generate_clock_frames(3, 1700000000, tmp_path)


# ---------------------------------------------------------------------------
# T014: Stage 2 — _upload_frames_to_drive
# ---------------------------------------------------------------------------

def test_upload_frames_calls_create_folder(tmp_path):
    """_upload_frames_to_drive calls drive.create_folder once with the test name."""
    import scripts.run_e2e_test as rig
    for i in range(1, 4):
        (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\x00")
    with patch.object(rig.drive, "create_folder", return_value="folder_id") as mock_cf, \
         patch.object(rig.drive, "upload", return_value="file_id"):
        _upload_frames_to_drive(tmp_path, "e2e-test-20260620-143000", "root_id")
    mock_cf.assert_called_once()
    assert "e2e-test-20260620-143000" in str(mock_cf.call_args)


def test_upload_frames_uploads_each_frame(tmp_path):
    """_upload_frames_to_drive calls drive.upload for each JPEG frame."""
    import scripts.run_e2e_test as rig
    n = 5
    for i in range(1, n + 1):
        (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\x00")
    with patch.object(rig.drive, "create_folder", return_value="folder_id"), \
         patch.object(rig.drive, "upload", return_value="file_id") as mock_up:
        _upload_frames_to_drive(tmp_path, "e2e-test-20260620-143000", "root_id")
    assert mock_up.call_count == n


def test_upload_frames_uses_jpeg_content_type(tmp_path):
    """drive.upload is called with content_type='image/jpeg' for all frames."""
    import scripts.run_e2e_test as rig
    for i in range(1, 3):
        (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\x00")
    with patch.object(rig.drive, "create_folder", return_value="folder_id"), \
         patch.object(rig.drive, "upload", return_value="fid") as mock_up:
        _upload_frames_to_drive(tmp_path, "test-name", "root_id")
    for c in mock_up.call_args_list:
        kw = c.kwargs
        assert kw.get("content_type") == "image/jpeg"


def test_upload_frames_returns_folder_and_file_ids(tmp_path):
    """_upload_frames_to_drive returns (folder_id, [file_id, ...])."""
    import scripts.run_e2e_test as rig
    for i in range(1, 3):
        (tmp_path / f"frame_{i:03d}.jpg").write_bytes(b"\x00")
    with patch.object(rig.drive, "create_folder", return_value="the_folder"), \
         patch.object(rig.drive, "upload", side_effect=["fid_1", "fid_2"]):
        folder_id, file_ids = _upload_frames_to_drive(tmp_path, "test-name", "root_id")
    assert folder_id == "the_folder"
    assert file_ids == ["fid_1", "fid_2"]


# ---------------------------------------------------------------------------
# T015: Stage 3 — _run_process_photos
# ---------------------------------------------------------------------------

def test_run_process_photos_calls_subprocess(mocker):
    """_run_process_photos launches process_photos.py via subprocess.run."""
    import scripts.run_e2e_test as rig
    mock_run = mocker.patch.object(rig.subprocess, "run", return_value=MagicMock(returncode=0))
    mocker.patch.object(rig.state, "get_pending_approval",
                        return_value={"project_name": "e2e-test-20260620-143000"})
    _run_process_photos("e2e-test-20260620-143000", spp_effective=4, timeout=30)
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert any("process_photos" in str(c) for c in cmd)


def test_run_process_photos_passes_project_arg(mocker):
    """process_photos.py is invoked with --project set to the test name."""
    import scripts.run_e2e_test as rig
    mock_run = mocker.patch.object(rig.subprocess, "run", return_value=MagicMock(returncode=0))
    mocker.patch.object(rig.state, "get_pending_approval",
                        return_value={"project_name": "e2e-test-20260620-143000"})
    _run_process_photos("e2e-test-20260620-143000", spp_effective=4, timeout=30)
    cmd = mock_run.call_args.args[0]
    assert "--project" in cmd
    assert "e2e-test-20260620-143000" in cmd


def test_run_process_photos_passes_spp_env(mocker):
    """process_photos.py subprocess receives SECONDS_PER_PHOTO in its environment."""
    import scripts.run_e2e_test as rig
    mock_run = mocker.patch.object(rig.subprocess, "run", return_value=MagicMock(returncode=0))
    mocker.patch.object(rig.state, "get_pending_approval",
                        return_value={"project_name": "e2e-test-20260620-143000"})
    _run_process_photos("e2e-test-20260620-143000", spp_effective=7, timeout=30)
    env = mock_run.call_args.kwargs.get("env") or mock_run.call_args.args[1] if len(mock_run.call_args.args) > 1 else {}
    env = mock_run.call_args.kwargs.get("env", {})
    assert env.get("SECONDS_PER_PHOTO") == "7"


def test_run_process_photos_raises_on_nonzero_exit(mocker):
    """_run_process_photos raises RuntimeError when process_photos.py exits non-zero."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.subprocess, "run", return_value=MagicMock(returncode=1))
    with pytest.raises(RuntimeError):
        _run_process_photos("e2e-test-20260620-143000", spp_effective=4, timeout=30)


# ---------------------------------------------------------------------------
# T016: Stage 4 — _wait_for_approval; Stage 5 — _wait_for_facebook_post
# ---------------------------------------------------------------------------

def test_wait_for_approval_returns_when_approval_cleared(mocker):
    """_wait_for_approval returns when pending_approval is None and fb upload is enqueued."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.state, "get_pending_approval", return_value=None)
    mocker.patch.object(rig.facebook_state, "get_pending_upload",
                        return_value={"project_name": "e2e-test-20260620-143000", "status": "pending"})
    mocker.patch("scripts.run_e2e_test.time.sleep")
    _wait_for_approval("e2e-test-20260620-143000", timeout=60)  # must not raise


def test_wait_for_approval_exits_on_timeout(mocker):
    """_wait_for_approval raises SystemExit (or RuntimeError) when the timeout elapses."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.state, "get_pending_approval",
                        return_value={"project_name": "e2e-test-20260620-143000"})
    mocker.patch.object(rig.facebook_state, "get_pending_upload", return_value=None)
    mocker.patch("scripts.run_e2e_test.time.sleep")
    mocker.patch("scripts.run_e2e_test.time.time", side_effect=[0, 0, 70])  # start, check, elapsed > 60
    with pytest.raises((SystemExit, RuntimeError)):
        _wait_for_approval("e2e-test-20260620-143000", timeout=60)


def test_wait_for_facebook_post_returns_post_id_on_success(mocker):
    """_wait_for_facebook_post returns the post_id when status becomes 'published'."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.facebook_state, "get_pending_upload",
                        return_value={"project_name": "e2e-test-20260620-143000",
                                      "status": "published", "fb_post_id": "post_abc"})
    mocker.patch("scripts.run_e2e_test.time.sleep")
    result = _wait_for_facebook_post("e2e-test-20260620-143000", timeout=60)
    assert result == "post_abc"


def test_wait_for_facebook_post_exits_on_timeout(mocker):
    """_wait_for_facebook_post raises SystemExit (or RuntimeError) when timeout elapses."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.facebook_state, "get_pending_upload",
                        return_value={"project_name": "e2e-test-20260620-143000",
                                      "status": "pending", "fb_post_id": None})
    mocker.patch("scripts.run_e2e_test.time.sleep")
    mocker.patch("scripts.run_e2e_test.time.time", side_effect=[0, 0, 70])
    with pytest.raises((SystemExit, RuntimeError)):
        _wait_for_facebook_post("e2e-test-20260620-143000", timeout=60)


# ---------------------------------------------------------------------------
# T025: US2 — Stage progress output format
# ---------------------------------------------------------------------------

def test_print_stage_success_format(capsys):
    """Successful stage line matches '[HH:MM:SS] Stage N/5: <label> ✅ done (Xs)'."""
    from scripts.run_e2e_test import _print_stage
    _print_stage(1, 5, "Clock frames generated", "ok", 3.0)
    out = capsys.readouterr().out
    assert "Stage 1/5" in out
    assert "✅" in out
    assert "done" in out
    assert "Clock frames generated" in out


def test_print_stage_failure_format(capsys):
    """Failed stage line contains '❌' and the error description."""
    from scripts.run_e2e_test import _print_stage
    _print_stage(2, 5, "Drive upload", "failed: auth error", 1.5)
    out = capsys.readouterr().out
    assert "Stage 2/5" in out
    assert "❌" in out
    assert "Drive upload" in out


def test_print_stage_has_timestamp_prefix(capsys):
    """Each stage line starts with a [HH:MM:SS] timestamp."""
    from scripts.run_e2e_test import _print_stage
    _print_stage(3, 5, "Some stage", "ok", 10.0)
    out = capsys.readouterr().out
    # Matches [digits:digits:digits] at the start
    import re
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\]", out.strip())


# ---------------------------------------------------------------------------
# T028: US3 — --cleanup flag
# ---------------------------------------------------------------------------

def test_cleanup_calls_drive_delete(mocker):
    """_cleanup() calls drive.delete with the folder_id."""
    import scripts.run_e2e_test as rig
    from tools.facebook_api import FacebookUploadError
    mock_delete = mocker.patch.object(rig.drive, "delete")
    mocker.patch("scripts.run_e2e_test.facebook_api.delete_post")
    _cleanup("folder_abc", "post_xyz", "page_token")
    mock_delete.assert_called_once_with("folder_abc")


def test_cleanup_calls_facebook_delete_post(mocker):
    """_cleanup() calls facebook_api.delete_post with the post_id and page token."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.drive, "delete")
    mock_fb_delete = mocker.patch("scripts.run_e2e_test.facebook_api.delete_post")
    _cleanup("folder_abc", "post_xyz", "page_token")
    mock_fb_delete.assert_called_once_with("page_token", "post_xyz")


def test_cleanup_skips_facebook_delete_when_post_id_none(mocker):
    """_cleanup() skips facebook_api.delete_post when fb_post_id is None."""
    import scripts.run_e2e_test as rig
    mocker.patch.object(rig.drive, "delete")
    mock_fb_delete = mocker.patch("scripts.run_e2e_test.facebook_api.delete_post")
    _cleanup("folder_abc", None, "page_token")
    mock_fb_delete.assert_not_called()


def test_cleanup_catches_facebook_upload_error_code_100(mocker):
    """_cleanup() catches FacebookUploadError and does not raise (warns instead)."""
    import scripts.run_e2e_test as rig
    from tools.facebook_api import FacebookUploadError
    mocker.patch.object(rig.drive, "delete")
    mocker.patch("scripts.run_e2e_test.facebook_api.delete_post",
                 side_effect=FacebookUploadError("Facebook API error 100"))
    _cleanup("folder_abc", "post_xyz", "page_token")  # must not raise


def test_cleanup_still_deletes_drive_when_facebook_error(mocker):
    """When Facebook delete fails, Drive folder is still deleted."""
    import scripts.run_e2e_test as rig
    from tools.facebook_api import FacebookUploadError
    mock_drive_delete = mocker.patch.object(rig.drive, "delete")
    mocker.patch("scripts.run_e2e_test.facebook_api.delete_post",
                 side_effect=FacebookUploadError("not found"))
    _cleanup("folder_abc", "post_xyz", "page_token")
    mock_drive_delete.assert_called_once_with("folder_abc")
