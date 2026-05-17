"""
Tests for tools/video_generator.py — FFmpeg command construction and error handling.

All tests mock subprocess.run so no actual video is generated and FFmpeg is not
required to be installed. Tests verify the exact filter_complex structure,
offset formula correctness, N=1 special case, output flags, and error propagation.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.video_generator import (
    FFmpegVideoGenerator,
    VideoConfig,
    VideoGenerationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_photos(tmp_path: Path, n: int) -> list[Path]:
    """Return n dummy photo paths; files are not created because subprocess.run is mocked."""
    return [tmp_path / f"photo{i:02d}.jpg" for i in range(n)]


def get_cmd(mock_run) -> list[str]:
    """Extract the FFmpeg command list from the mock subprocess.run call."""
    return mock_run.call_args.args[0]


def get_filter_complex(cmd: list[str]) -> str:
    """Extract the -filter_complex value from the command list."""
    idx = cmd.index("-filter_complex")
    return cmd[idx + 1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ffmpeg_ok():
    """Patch subprocess.run to simulate a successful FFmpeg run."""
    with patch("tools.video_generator.subprocess.run") as mock:
        mock.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        yield mock


@pytest.fixture
def gen():
    """Return a fresh FFmpegVideoGenerator instance."""
    return FFmpegVideoGenerator()


# ---------------------------------------------------------------------------
# VideoConfig defaults
# ---------------------------------------------------------------------------

def test_videoconfig_defaults():
    """VideoConfig defaults match spec: 1080×1920, 30fps, 4s/photo, 0.5s xfade, 3M bitrate."""
    cfg = VideoConfig()
    assert cfg.width == 1080
    assert cfg.height == 1920
    assert cfg.fps == 30
    assert cfg.seconds_per_photo == 4
    assert cfg.crossfade_duration == 0.5
    assert cfg.bitrate == "3M"


# ---------------------------------------------------------------------------
# N=0 empty list
# ---------------------------------------------------------------------------

def test_empty_photos_raises(tmp_path, gen):
    """generate() raises VideoGenerationError immediately for an empty photo list."""
    with pytest.raises(VideoGenerationError, match="empty"):
        gen.generate([], VideoConfig(), tmp_path / "out.mp4")


# ---------------------------------------------------------------------------
# N=1 single photo
# ---------------------------------------------------------------------------

def test_n1_includes_loop_and_t_flag(tmp_path, gen, mock_ffmpeg_ok):
    """For N=1, the command includes -loop 1 and -t {seconds_per_photo} before -i."""
    cfg = VideoConfig()
    gen.generate(make_photos(tmp_path, 1), cfg, tmp_path / "out.mp4")
    cmd = get_cmd(mock_ffmpeg_ok)
    assert "-loop" in cmd and cmd[cmd.index("-loop") + 1] == "1"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == str(cfg.seconds_per_photo)


def test_n1_no_xfade(tmp_path, gen, mock_ffmpeg_ok):
    """For N=1, the filter_complex does not contain 'xfade'."""
    gen.generate(make_photos(tmp_path, 1), VideoConfig(), tmp_path / "out.mp4")
    fc = get_filter_complex(get_cmd(mock_ffmpeg_ok))
    assert "xfade" not in fc


def test_n1_maps_v0_not_xout(tmp_path, gen, mock_ffmpeg_ok):
    """For N=1, the -map flag uses [v0], not [xout] (there is no xfade output)."""
    gen.generate(make_photos(tmp_path, 1), VideoConfig(), tmp_path / "out.mp4")
    cmd = get_cmd(mock_ffmpeg_ok)
    assert cmd[cmd.index("-map") + 1] == "[v0]"
    assert "[xout]" not in cmd


# ---------------------------------------------------------------------------
# N=2
# ---------------------------------------------------------------------------

def test_n2_one_xfade(tmp_path, gen, mock_ffmpeg_ok):
    """For N=2, the filter_complex contains exactly one xfade filter."""
    gen.generate(make_photos(tmp_path, 2), VideoConfig(), tmp_path / "out.mp4")
    assert get_filter_complex(get_cmd(mock_ffmpeg_ok)).count("xfade") == 1


def test_n2_xfade_offset(tmp_path, gen, mock_ffmpeg_ok):
    """For N=2, xfade offset = seconds_per_photo − crossfade_duration (3.5 with defaults)."""
    cfg = VideoConfig()
    gen.generate(make_photos(tmp_path, 2), cfg, tmp_path / "out.mp4")
    fc = get_filter_complex(get_cmd(mock_ffmpeg_ok))
    expected = cfg.seconds_per_photo - cfg.crossfade_duration  # 3.5
    assert f"offset={expected:g}" in fc


# ---------------------------------------------------------------------------
# N=5
# ---------------------------------------------------------------------------

def test_n5_four_xfades(tmp_path, gen, mock_ffmpeg_ok):
    """For N=5, the filter_complex contains exactly four xfade filters."""
    gen.generate(make_photos(tmp_path, 5), VideoConfig(), tmp_path / "out.mp4")
    assert get_filter_complex(get_cmd(mock_ffmpeg_ok)).count("xfade") == 4


def test_n5_xfade_offsets_match_formula(tmp_path, gen, mock_ffmpeg_ok):
    """For N=5, each offset[i] = (i+1) × (seconds_per_photo − crossfade_duration)."""
    cfg = VideoConfig()
    gen.generate(make_photos(tmp_path, 5), cfg, tmp_path / "out.mp4")
    fc = get_filter_complex(get_cmd(mock_ffmpeg_ok))
    step = cfg.seconds_per_photo - cfg.crossfade_duration  # 3.5
    for i in range(4):
        expected = (i + 1) * step
        assert f"offset={expected:g}" in fc, f"Expected offset={expected:g} in filter_complex"


# ---------------------------------------------------------------------------
# N=11 label uniqueness
# ---------------------------------------------------------------------------

def test_n11_unique_filter_labels(tmp_path, gen, mock_ffmpeg_ok):
    """For N=11, every xfade output label is unique — no label produced twice.

    Each intermediate label appears twice in the filter_complex (once as output,
    once as the next xfade's input), so uniqueness is checked on output labels only:
    the last [...] in each semicolon-separated filter segment.
    """
    gen.generate(make_photos(tmp_path, 11), VideoConfig(), tmp_path / "out.mp4")
    fc = get_filter_complex(get_cmd(mock_ffmpeg_ok))
    output_labels = [
        re.search(r'\[([^\]]+)\]$', seg.strip()).group(0)
        for seg in fc.split(";")
        if re.search(r'\[([^\]]+)\]$', seg.strip())
    ]
    xfade_outputs = [l for l in output_labels if l.startswith("[x")]
    assert len(xfade_outputs) == len(set(xfade_outputs)), \
        f"Duplicate xfade output labels: {xfade_outputs}"


# ---------------------------------------------------------------------------
# Scale/crop filter
# ---------------------------------------------------------------------------

def test_scale_crop_filter_default_resolution(tmp_path, gen, mock_ffmpeg_ok):
    """Scale/crop filter uses 1080×1920, force_original_aspect_ratio=increase, setsar=1, fps=30."""
    gen.generate(make_photos(tmp_path, 2), VideoConfig(), tmp_path / "out.mp4")
    fc = get_filter_complex(get_cmd(mock_ffmpeg_ok))
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in fc
    assert "crop=1080:1920" in fc
    assert "setsar=1" in fc
    assert f"fps={VideoConfig().fps}" in fc


# ---------------------------------------------------------------------------
# Output flags
# ---------------------------------------------------------------------------

def test_output_flags(tmp_path, gen, mock_ffmpeg_ok):
    """Output flags include -c:v libx264, -pix_fmt yuv420p, and -an."""
    gen.generate(make_photos(tmp_path, 2), VideoConfig(), tmp_path / "out.mp4")
    cmd = " ".join(str(x) for x in get_cmd(mock_ffmpeg_ok))
    assert "-c:v libx264" in cmd
    assert "-pix_fmt yuv420p" in cmd
    assert "-an" in cmd


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------

def test_generate_returns_output_path(tmp_path, gen, mock_ffmpeg_ok):
    """generate() returns the exact output_path passed in."""
    output = tmp_path / "out.mp4"
    result = gen.generate(make_photos(tmp_path, 2), VideoConfig(), output)
    assert result == output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_generate_raises_on_nonzero_exit(tmp_path, gen, mock_ffmpeg_ok):
    """generate() raises VideoGenerationError when FFmpeg exits non-zero."""
    mock_ffmpeg_ok.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="some error"
    )
    with pytest.raises(VideoGenerationError):
        gen.generate(make_photos(tmp_path, 2), VideoConfig(), tmp_path / "out.mp4")


def test_generate_includes_stderr_in_error(tmp_path, gen, mock_ffmpeg_ok):
    """generate() includes FFmpeg stderr text in the VideoGenerationError message."""
    mock_ffmpeg_ok.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="codec not found"
    )
    with pytest.raises(VideoGenerationError, match="codec not found"):
        gen.generate(make_photos(tmp_path, 2), VideoConfig(), tmp_path / "out.mp4")
