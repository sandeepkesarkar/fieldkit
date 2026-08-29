"""
Integration tests for FFmpegVideoGenerator — actually runs FFmpeg.

These tests are skipped automatically if ffmpeg is not found on PATH (e.g.
on the dev machine before T10 setup). They run on the Mac Mini where ffmpeg
is installed as part of the T10 checklist.

Test images are generated using FFmpeg's built-in lavfi color source so no
external image files or Python imaging libraries are required. A small
resolution (108×192) and short duration keep each test fast (~1–2 seconds).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video_generator import FFmpegVideoGenerator, VideoConfig, VideoGenerationError

FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(
    FFMPEG is None,
    reason="ffmpeg not installed — run these tests on the Mac Mini after T10 setup",
)

# Small config keeps test execution fast without sacrificing coverage.
_TEST_CONFIG = VideoConfig(
    width=108,
    height=192,
    fps=10,
    seconds_per_photo=2,
    crossfade_duration=0.5,
    bitrate="500k",
)

_COLORS = ["red", "green", "blue", "yellow", "cyan"]


def _make_test_image(path: Path, color: str, config: VideoConfig) -> None:
    """Write a solid-color JPEG using FFmpeg's lavfi color source."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:size={config.width}x{config.height}:rate=1",
            "-frames:v", "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def gen():
    """Return a fresh FFmpegVideoGenerator."""
    return FFmpegVideoGenerator()


@pytest.fixture
def single_image(tmp_path):
    """One solid-color JPEG test image."""
    path = tmp_path / "red.jpg"
    _make_test_image(path, "red", _TEST_CONFIG)
    return [path]


@pytest.fixture
def three_images(tmp_path):
    """Three solid-color JPEG test images."""
    images = []
    for color in _COLORS[:3]:
        path = tmp_path / f"{color}.jpg"
        _make_test_image(path, color, _TEST_CONFIG)
        images.append(path)
    return images


# ---------------------------------------------------------------------------
# Output file existence and size
# ---------------------------------------------------------------------------

def test_n1_produces_nonempty_mp4(tmp_path, gen, single_image):
    """For N=1, a non-empty MP4 file is written to output_path."""
    output = tmp_path / "out.mp4"
    result = gen.generate(single_image, _TEST_CONFIG, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_n3_produces_nonempty_mp4(tmp_path, gen, three_images):
    """For N=3, a non-empty MP4 file is written to output_path."""
    output = tmp_path / "out.mp4"
    result = gen.generate(three_images, _TEST_CONFIG, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


# ---------------------------------------------------------------------------
# Video stream validation via ffprobe
# ---------------------------------------------------------------------------

def test_output_contains_h264_video_stream(tmp_path, gen, three_images):
    """The generated MP4 contains an H.264 video stream (verified via ffprobe)."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    output = tmp_path / "out.mp4"
    gen.generate(three_images, _TEST_CONFIG, output)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert "h264" in probe.stdout


def test_output_has_no_audio_stream(tmp_path, gen, three_images):
    """The generated MP4 has no audio stream (-an flag is honoured)."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    output = tmp_path / "out.mp4"
    gen.generate(three_images, _TEST_CONFIG, output)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------

def test_nonexistent_input_raises_video_generation_error(tmp_path, gen):
    """Passing a non-existent photo path causes FFmpeg to fail and raises VideoGenerationError."""
    missing = [tmp_path / "ghost.jpg"]
    with pytest.raises(VideoGenerationError):
        gen.generate(missing, _TEST_CONFIG, tmp_path / "out.mp4")


# ---------------------------------------------------------------------------
# Watermark integration
# ---------------------------------------------------------------------------

def _has_drawtext_filter() -> bool:
    """Check if FFmpeg has the drawtext filter available (requires libfreetype)."""
    if FFMPEG is None:
        return False
    result = subprocess.run(
        ["ffmpeg", "-filters"],
        capture_output=True,
        text=True,
    )
    return "drawtext" in result.stdout


def test_watermark_produces_valid_output_single_photo(tmp_path, gen, single_image):
    """N=1 with watermark configured produces a valid MP4 file."""
    if not _has_drawtext_filter():
        pytest.skip("drawtext filter not available — FFmpeg needs libfreetype support")
    cfg = VideoConfig(
        width=108, height=192, fps=10, seconds_per_photo=2,
        watermark_text="Demo Client",
        watermark_font_path="/System/Library/Fonts/Helvetica.ttc"
    )
    output = tmp_path / "out.mp4"
    result = gen.generate(single_image, cfg, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_watermark_produces_valid_output_multi_photo(tmp_path, gen, three_images):
    """N=3 with watermark configured produces a valid MP4 file."""
    if not _has_drawtext_filter():
        pytest.skip("drawtext filter not available — FFmpeg needs libfreetype support")
    cfg = VideoConfig(
        width=108, height=192, fps=10, seconds_per_photo=2,
        watermark_text="Construction Co",
        watermark_font_path="/System/Library/Fonts/Helvetica.ttc"
    )
    output = tmp_path / "out.mp4"
    result = gen.generate(three_images, cfg, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_watermark_with_special_characters_produces_valid_output(tmp_path, gen, single_image):
    """Watermark text containing apostrophe and other metacharacters works (BLOCKING FIX #1).

    This is the exact test case from the reviewer's report that exposed the
    apostrophe escaping bug. With the fix, this must produce valid output.
    """
    if not _has_drawtext_filter():
        pytest.skip("drawtext filter not available — FFmpeg needs libfreetype support")
    cfg = VideoConfig(
        width=108, height=192, fps=10, seconds_per_photo=2,
        watermark_text="Foo's Bar: 100% \\Cool\\",
        watermark_font_path="/System/Library/Fonts/Helvetica.ttc",
        freeze_duration=0  # Disable freeze for faster test
    )
    output = tmp_path / "out.mp4"
    result = gen.generate(single_image, cfg, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_watermark_with_font_path_containing_special_chars(tmp_path, gen, single_image):
    """Font path with : and space works correctly (BLOCKING FIX #2)."""
    if not _has_drawtext_filter():
        pytest.skip("drawtext filter not available — FFmpeg needs libfreetype support")

    # Create a font directory with problematic characters
    font_dir = tmp_path / "test: fonts"
    font_dir.mkdir()
    # Copy the system font to our test location
    import shutil
    system_font = Path("/System/Library/Fonts/Helvetica.ttc")
    if not system_font.exists():
        pytest.skip("System font not available")
    test_font = font_dir / "Test Font.ttc"
    shutil.copy(system_font, test_font)

    cfg = VideoConfig(
        width=108, height=192, fps=10, seconds_per_photo=1,
        watermark_text="Demo",
        watermark_font_path=str(test_font),
        freeze_duration=0
    )
    output = tmp_path / "out.mp4"
    result = gen.generate(single_image, cfg, output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_watermark_empty_font_path_skips_gracefully(tmp_path, gen, single_image, caplog):
    """Empty font path skips watermark gracefully (BLOCKING FIX #3)."""
    import logging
    caplog.set_level(logging.WARNING)
    cfg = VideoConfig(
        width=108, height=192, fps=10, seconds_per_photo=1,
        watermark_text="Demo",
        watermark_font_path="",
        freeze_duration=0
    )
    output = tmp_path / "out.mp4"
    # Should succeed (no watermark, but video is generated)
    result = gen.generate(single_image, cfg, output)
    assert result == output
    assert output.exists()
    assert "Watermark font path is empty" in caplog.text
