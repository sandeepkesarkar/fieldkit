"""
Video generator for the photo-video agent.

Defines the VideoGenerator protocol, VideoConfig dataclass,
FFmpegVideoGenerator implementation, and VideoGenerationError.
Generates 1080×1920 portrait MP4 slideshow videos from a list of photos
using FFmpeg via subprocess with crossfade transitions.

No files are read or written by this module directly; the caller provides
photo paths and an output path. FFmpeg must be installed on the host.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class VideoConfig:
    """Parameters controlling video dimensions, timing, and encoding."""
    width: int = 1080
    height: int = 1920
    fps: int = 30
    seconds_per_photo: int = 4
    crossfade_duration: float = 0.5
    bitrate: str = "3M"
    # Holds the final frame for this many extra seconds at the end of the video,
    # so a viewer looping playback gets a moment to read it before it restarts.
    # Set to 0 to disable.
    freeze_duration: float = 1.5
    # Optional watermark text to burn into the video (e.g. client display name).
    # When set, a drawtext filter is applied to the final composited output.
    watermark_text: str | None = None
    # Font file path for the watermark. Only used when watermark_text is set.
    # If the file doesn't exist at generation time, the watermark is skipped with a warning.
    watermark_font_path: str = "/System/Library/Fonts/Helvetica.ttc"

    def __post_init__(self) -> None:
        if self.crossfade_duration < 0:
            raise ValueError(f"crossfade_duration must be >= 0, got {self.crossfade_duration}")
        if self.crossfade_duration >= self.seconds_per_photo:
            raise ValueError(
                f"crossfade_duration ({self.crossfade_duration}) must be less than "
                f"seconds_per_photo ({self.seconds_per_photo}) — xfade offsets would be <= 0"
            )
        if self.freeze_duration < 0:
            raise ValueError(f"freeze_duration must be >= 0, got {self.freeze_duration}")


class VideoGenerationError(Exception):
    """Raised when FFmpeg exits with a non-zero return code."""
    pass


class VideoGenerator(Protocol):
    """Protocol for video generators — allows FFmpegVideoGenerator to be swapped out."""
    def generate(self, photos: list[Path], config: VideoConfig, output_path: Path) -> Path:
        """Generate a video slideshow from photos and write it to output_path."""
        ...


class FFmpegVideoGenerator:
    """Builds and runs a single FFmpeg command to produce a crossfade slideshow."""

    def generate(self, photos: list[Path], config: VideoConfig, output_path: Path) -> Path:
        """Run FFmpeg on photos and write the result to output_path."""
        if not photos:
            raise VideoGenerationError("photos list must not be empty")

        n = len(photos)
        if n == 1:
            cmd = _build_single_photo_cmd(photos[0], config, output_path)
        else:
            cmd = _build_multi_photo_cmd(photos, config, output_path)

        logger.debug("Running FFmpeg command with %d input(s)", n)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired as e:
            raise VideoGenerationError("FFmpeg timed out after 600s") from e
        if result.stdout:
            logger.debug("FFmpeg stdout: %s", result.stdout)
        if result.returncode != 0:
            raise VideoGenerationError(f"FFmpeg exited {result.returncode}: {result.stderr}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise VideoGenerationError(
                f"FFmpeg exited 0 but output is missing or empty — {result.stderr}"
            )
        return output_path


def _scale_crop_filter(i: int, config: VideoConfig, output_duration: float | None = None) -> str:
    """Build the scale/crop/setsar/zoompan filter segment for photo index i.

    Args:
        i: Photo index (for labeling in filter graph)
        config: Video configuration
        output_duration: Override output duration in seconds (default: seconds_per_photo).
                        For xfade chains, this should be seconds_per_photo + crossfade_duration.
    """
    W, H, fps = config.width, config.height, config.fps
    spp = config.seconds_per_photo
    duration = output_duration if output_duration is not None else spp
    # Total output frames needed for this photo segment
    total_frames = int(duration * fps)
    # Zoom completes over seconds_per_photo (not the extended duration for xfade)
    zoom_frames = int(spp * fps)
    # zoompan: slow Ken Burns zoom from 1.0 to 1.1 over seconds_per_photo,
    # then holds at 1.1 for any remaining xfade buffer frames
    # d = total output frames to generate (NOT frames per input!)
    # fps = output frame rate (must match config.fps)
    # z = zoom expression; min() ensures zoom maxes at 1.1 even past zoom_frames
    # s = output size
    # CRITICAL: zoompan must come BEFORE any fps filter, so it receives the still
    # image as a single frame and generates total_frames outputs at fps rate.
    return (
        f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,"
        f"zoompan=z='min(1.1,1+0.1*on/{zoom_frames})':d={total_frames}:fps={fps}:s={W}x{H}[v{i}]"
    )


def _escape_drawtext_value(text: str) -> str:
    r"""Escape text for ffmpeg's drawtext filter (used without surrounding quotes).

    FFmpeg's filtergraph parser requires backslash-escaping for special characters.
    Since we use text=value (NOT text='value'), we must escape spaces along with
    other metacharacters. Order matters: backslash must be escaped first.

    Args:
        text: Raw text value (e.g. "Foo's Bar: 100%")

    Returns:
        Escaped text suitable for text=... in drawtext filter
        (e.g. r"Foo\'s\ Bar\:\ 100\%")
    """
    # Order is critical: escape backslash first, then other chars that need backslash escaping
    text = text.replace("\\", "\\\\")  # \ → \\
    text = text.replace(" ", "\\ ")    # space → \ (required when not using quotes)
    text = text.replace(":", "\\:")    # : → \: (option separator)
    text = text.replace("'", "\\'")    # ' → \' (quote character)
    text = text.replace("%", "\\%")    # % → \% (metadata expansion)
    # Additional filtergraph metacharacters that could appear in display names
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    return text


def _watermark_filter(config: VideoConfig, source_label: str) -> tuple[str, str] | None:
    """Build a drawtext filter segment for the watermark if configured.

    Args:
        config: Video configuration (uses watermark_text and watermark_font_path).
        source_label: Bracketed input label, e.g. "[vout]" or "[v0]".

    Returns (filter_segment, output_label) tuple if watermark is configured and
            font file exists, otherwise None. Output label is always "[vwm]".
    """
    if not config.watermark_text:
        return None

    # BLOCKING FIX #3: Check for non-empty string AND is_file() (not just exists()).
    # Path("").exists() resolves to "." (current dir) which exists, and any directory
    # also exists but is not a valid font file — both must skip gracefully.
    if not config.watermark_font_path or not config.watermark_font_path.strip():
        logger.warning(
            "Watermark font path is empty — skipping watermark. "
            "Set FIELDKIT_WATERMARK_FONT_PATH to a valid .ttf/.ttc file."
        )
        return None

    font_path = Path(config.watermark_font_path)
    if not font_path.is_file():
        logger.warning(
            "Watermark font file not found or is not a file at %s — skipping watermark. "
            "Set FIELDKIT_WATERMARK_FONT_PATH to a valid .ttf/.ttc file.",
            font_path
        )
        return None

    # BLOCKING FIX #1: Use _escape_drawtext_value and do NOT wrap text in quotes.
    # text='Foo\'s Bar' fails because the \' does not escape the quote inside '...',
    # it terminates the quoted section early. Correct syntax is text=Foo\'s\ Bar
    # (no quotes, escape spaces and special chars).
    escaped_text = _escape_drawtext_value(config.watermark_text)

    # BLOCKING FIX #2: Escape the font path as well. Paths containing : (option
    # separator) or other metacharacters will corrupt the filter graph.
    escaped_font_path = _escape_drawtext_value(str(font_path))

    # Semi-opaque background box for legibility against arbitrary photo backgrounds.
    # Position at bottom-right with padding. Font size 28 for 1080px width.
    # box=1 enables background box, boxcolor with @alpha for opacity.
    # x position clamped to avoid negative/clipped rendering for long names on narrow output.
    filter_segment = (
        f"{source_label}drawtext="
        f"text={escaped_text}:"
        f"fontfile={escaped_font_path}:"
        f"fontsize=28:"
        f"fontcolor=white:"
        f"box=1:"
        f"boxcolor=black@0.6:"
        f"boxborderw=8:"
        f"x=max(20\\,w-text_w-20):"
        f"y=h-text_h-20"
        f"[vwm]"
    )
    return (filter_segment, "[vwm]")


def _freeze_filter(config: VideoConfig, source_label: str) -> tuple[str, str]:
    """Build a tpad filter segment that holds the last frame of source_label.

    Args:
        config: Video configuration (uses freeze_duration).
        source_label: Bracketed input label, e.g. "[v0]" or "[xout]".

    Returns (filter_segment, output_label) — output_label is always "[vout]".
    """
    return (
        f"{source_label}tpad=stop_mode=clone:stop_duration={config.freeze_duration:g}[vout]",
        "[vout]",
    )


def _output_flags(config: VideoConfig) -> list[str]:
    """Build the shared encoding output flags."""
    return [
        "-c:v", "libx264",
        "-preset", "medium",
        "-b:v", config.bitrate,
        "-an",
        "-r", str(config.fps),
        "-pix_fmt", "yuv420p",
    ]


def _build_single_photo_cmd(photo: Path, config: VideoConfig, output_path: Path) -> list[str]:
    """FFmpeg command for N=1: read the still image once; zoompan expands it to full duration."""
    filters = [_scale_crop_filter(0, config)]
    map_label = "[v0]"

    # Apply watermark if configured (after zoompan, before freeze)
    watermark_result = _watermark_filter(config, "[v0]")
    if watermark_result:
        watermark_filter, map_label = watermark_result
        filters.append(watermark_filter)

    # Apply freeze if configured (after watermark)
    if config.freeze_duration > 0:
        freeze_filter, map_label = _freeze_filter(config, map_label)
        filters.append(freeze_filter)

    return [
        "ffmpeg",
        "-i", str(photo),
        "-filter_complex", ";".join(filters),
        "-map", map_label,
        *_output_flags(config),
        str(output_path),
    ]


def _build_multi_photo_cmd(photos: list[Path], config: VideoConfig, output_path: Path) -> list[str]:
    """FFmpeg command for N≥2: per-photo zoompan followed by an xfade chain."""
    n = len(photos)
    spp = config.seconds_per_photo
    xfade = config.crossfade_duration
    # Each photo's zoompan filter generates spp + xfade seconds worth of frames,
    # giving the xfade chain sufficient buffer for transitions.
    zoompan_duration = spp + xfade

    cmd = ["ffmpeg"]
    for photo in photos:
        # Read each still image once; zoompan will expand it to zoompan_duration
        cmd += ["-i", str(photo)]

    # Per-photo scale/crop/zoompan filters with extended duration for xfade
    filters = [_scale_crop_filter(i, config, output_duration=zoompan_duration) for i in range(n)]

    # Xfade chain: N photos → N−1 transitions
    # offset[i] = (i + 1) × (seconds_per_photo − crossfade_duration)
    # Labels use underscore separator [x{i}_{i+1}] to stay unambiguous for N≥10.
    # :g suppresses trailing zeros; FFmpeg filter values must not contain unnecessary whitespace.
    for i in range(n - 1):
        left = f"[v{i}]" if i == 0 else f"[x{i - 1}_{i}]"
        right = f"[v{i + 1}]"
        out = "[xout]" if i == n - 2 else f"[x{i}_{i + 1}]"
        offset = (i + 1) * (spp - xfade)
        filters.append(
            f"{left}{right}xfade=transition=fade:duration={xfade:g}:offset={offset:g}{out}"
        )

    map_label = "[xout]"

    # Apply watermark if configured (after xfade chain, before freeze)
    watermark_result = _watermark_filter(config, "[xout]")
    if watermark_result:
        watermark_filter, map_label = watermark_result
        filters.append(watermark_filter)

    # Apply freeze if configured (after watermark)
    if config.freeze_duration > 0:
        freeze_filter, map_label = _freeze_filter(config, map_label)
        filters.append(freeze_filter)

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", map_label,
        *_output_flags(config),
        str(output_path),
    ]
    return cmd
