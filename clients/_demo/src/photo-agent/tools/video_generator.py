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
        return output_path


def _scale_crop_filter(i: int, config: VideoConfig) -> str:
    """Build the scale/crop/setsar/fps filter segment for photo index i."""
    W, H, fps = config.width, config.height, config.fps
    return (
        f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,fps={fps}[v{i}]"
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
    """FFmpeg command for N=1: -loop 1 keeps the still image source alive; -t fixes the duration."""
    return [
        "ffmpeg",
        "-loop", "1",
        "-t", str(config.seconds_per_photo),
        "-i", str(photo),
        "-filter_complex", _scale_crop_filter(0, config),
        "-map", "[v0]",
        *_output_flags(config),
        str(output_path),
    ]


def _build_multi_photo_cmd(photos: list[Path], config: VideoConfig, output_path: Path) -> list[str]:
    """FFmpeg command for N≥2: per-photo scale/crop followed by an xfade chain."""
    n = len(photos)
    spp = config.seconds_per_photo
    xfade = config.crossfade_duration
    # Each still-image input needs -loop 1 and -t so FFmpeg generates enough
    # frames for the xfade chain. spp + xfade gives the right-side input of
    # each transition a sufficient buffer of frames.
    input_duration = spp + xfade

    cmd = ["ffmpeg"]
    for photo in photos:
        cmd += ["-loop", "1", "-t", f"{input_duration:g}", "-i", str(photo)]

    # Per-photo scale/crop filters
    filters = [_scale_crop_filter(i, config) for i in range(n)]

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

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[xout]",
        *_output_flags(config),
        str(output_path),
    ]
    return cmd
