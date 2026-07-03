"""
e2e_stage1_generate_frames.py — E2E Stage 1: Generate synthetic clock frames.

Creates JPEG clock frames in data/photo-agent/e2e_frames/ and writes
data/photo-agent/e2e_run_state.json for downstream stages to consume.

Usage:
    python3 scripts/e2e_stage1_generate_frames.py
    python3 scripts/e2e_stage1_generate_frames.py --duration 20 --spp 4
"""

import argparse
import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_FRAMES_DIR = _DATA_DIR / "e2e_frames"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"

_XFADE = 0.5
_N_FRAMES_CAP = 30
_DURATION_MIN = 2
_DURATION_MAX = 300


def _compute_frames(duration: int, spp_base: int) -> tuple[int, int]:
    """Return (n_frames, spp_effective) for the given duration and spp_base.

    Caps n_frames at 30; if capped, spp_effective is bumped so total duration is preserved.
    """
    n_frames = math.ceil((duration - _XFADE) / (spp_base - _XFADE))
    if n_frames > _N_FRAMES_CAP:
        n_frames = _N_FRAMES_CAP
        spp_effective = math.ceil((duration + (_N_FRAMES_CAP - 1) * _XFADE) / _N_FRAMES_CAP)
    else:
        spp_effective = spp_base
    return n_frames, spp_effective


def _generate_clock_frames(n_frames: int, start_unix: int, frames_dir: Path) -> None:
    """Generate n_frames JPEG clock frames (1080×1920) in frames_dir.

    Each frame shows MM/DD/YYYY and HH:MM:SS advancing one second per frame.
    Raises RuntimeError if the font file cannot be loaded.
    """
    font_path = os.environ.get("FIELDKIT_E2E_FONT_PATH", "/System/Library/Fonts/Helvetica.ttc")
    try:
        font_label = ImageFont.truetype(font_path, 80)
        font_date = ImageFont.truetype(font_path, 120)
        font_time = ImageFont.truetype(font_path, 160)
    except (IOError, OSError) as exc:
        raise RuntimeError(
            f"Could not load font {font_path!r}: {exc}. "
            "Set FIELDKIT_E2E_FONT_PATH to a valid .ttf/.ttc file."
        ) from exc

    W, H = 1080, 1920
    for i in range(n_frames):
        ts = datetime.fromtimestamp(start_unix + i)
        img = Image.new("RGB", (W, H), color=(29, 78, 216))
        draw = ImageDraw.Draw(img)

        def cx(text, font):
            bbox = draw.textbbox((0, 0), text, font=font)
            return (W - (bbox[2] - bbox[0])) // 2

        draw.text((cx("FieldKit E2E Test", font_label), int(H * 0.35)),
                  "FieldKit E2E Test", font=font_label, fill="white")
        draw.text((cx(ts.strftime("%m/%d/%Y"), font_date), int(H * 0.50)),
                  ts.strftime("%m/%d/%Y"), font=font_date, fill="white")
        draw.text((cx(ts.strftime("%H:%M:%S"), font_time), int(H * 0.62)),
                  ts.strftime("%H:%M:%S"), font=font_time, fill="white")

        img.save(frames_dir / f"frame_{i + 1:03d}.jpg", "JPEG", quality=90)


def main(argv=None) -> None:
    """Entry point — parse args, generate frames, write run state."""
    parser = argparse.ArgumentParser(description="E2E Stage 1: Generate clock frames.")
    parser.add_argument("--duration", type=int, default=20,
                        help="Video duration in seconds (2–300, default 20)")
    parser.add_argument("--spp", type=int, default=4,
                        help="Seconds per photo base value (default 4)")
    args = parser.parse_args(argv)

    if args.duration < _DURATION_MIN:
        print(f"ERROR: --duration must be at least {_DURATION_MIN}", file=sys.stderr)
        sys.exit(1)
    if args.duration > _DURATION_MAX:
        print(f"ERROR: --duration must be at most {_DURATION_MAX}", file=sys.stderr)
        sys.exit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    test_name = f"e2e-test-{timestamp}"
    n_frames, spp_effective = _compute_frames(args.duration, args.spp)

    if _FRAMES_DIR.exists():
        shutil.rmtree(_FRAMES_DIR)
    _FRAMES_DIR.mkdir(parents=True)

    try:
        _generate_clock_frames(n_frames, int(time.time()), _FRAMES_DIR)
    except RuntimeError as exc:
        print(f"Stage 1 ❌ — {exc}", file=sys.stderr)
        sys.exit(1)

    run_state = {
        "test_name": test_name,
        "frames_dir": str(_FRAMES_DIR),
        "n_frames": n_frames,
        "spp_effective": spp_effective,
        "duration": args.duration,
        "folder_id": None,
        "fb_post_id": None,
    }
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _RUN_STATE_FILE.write_text(json.dumps(run_state, indent=2))

    print(f"Stage 1 ✅ — {n_frames} frames → {_FRAMES_DIR}")
    print(f"  test_name: {test_name}")


if __name__ == "__main__":
    main()
