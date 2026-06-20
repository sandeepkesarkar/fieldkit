"""
run_e2e_test.py — End-to-end pipeline test rig for the FieldKit photo agent.

Usage:
    python3 scripts/run_e2e_test.py --duration 30
    python3 scripts/run_e2e_test.py --duration 60 --approval-timeout 600
    python3 scripts/run_e2e_test.py --cleanup

Generates synthetic JPEG clock frames (one per second, MM/DD/YYYY HH:MM:SS overlay),
uploads them to Google Drive, then runs the full pipeline:
  process_photos.py → Telegram approval → check_approval.py → upload_facebook.py → Facebook.

Reports each stage with a timestamped ✅/❌ status line.
"""

import argparse
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive, facebook_state, state
from tools.facebook_api import FacebookUploadError
import tools.facebook_api as facebook_api

_log = logging.getLogger(__name__)

_PHOTO_AGENT_DIR = Path(__file__).parents[1]
_PROCESS_PHOTOS = str(_PHOTO_AGENT_DIR / "scripts" / "process_photos.py")

_REQUIRED_ENV = [
    "FB_PAGE_ACCESS_TOKEN",
    "FB_PAGE_ID",
    "DRIVE_ROOT_FOLDER_ID",
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_CHAT_ID",
]

_DURATION_MIN = 2
_DURATION_MAX = 300
_XFADE = 0.5
_SPP_BASE_DEFAULT = 4
_N_FRAMES_CAP = 30
_POLL_INTERVAL = 10

_FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"


def _compute_frames(duration: int, spp_base: int) -> tuple[int, int]:
    """Return (n_frames, spp_effective) for the given duration and spp_base.

    Caps n_frames at 30 (_N_FRAMES_CAP); if capped, spp_effective is bumped so the
    formula n*spp - (n-1)*xfade ≈ duration still holds.
    """
    n_frames = math.ceil((duration - _XFADE) / (spp_base - _XFADE))
    if n_frames > _N_FRAMES_CAP:
        n_frames = _N_FRAMES_CAP
        spp_effective = math.ceil((duration + (_N_FRAMES_CAP - 1) * _XFADE) / _N_FRAMES_CAP)
    else:
        spp_effective = spp_base
    return n_frames, spp_effective


def _generate_clock_frames(n_frames: int, start_unix: int, frames_dir: Path) -> None:
    """Generate n_frames JPEG clock frames via FFmpeg with localtime PTS overlay.

    Each frame shows the real date/time (advancing 1 second per frame) as:
      FieldKit E2E Test
      MM/DD/YYYY
      HH:MM:SS
    """
    output_pattern = str(frames_dir / "frame_%03d.jpg")
    vf = (
        f"color=c=#1D4ED8:size=1080x1920:rate=1,"
        f"drawtext=fontfile={_FONT_PATH}:"
        f"fontsize=80:fontcolor=white:x=(w-text_w)/2:y=h*0.35:"
        f"text='FieldKit E2E Test',"
        f"drawtext=fontfile={_FONT_PATH}:"
        f"fontsize=120:fontcolor=white:x=(w-text_w)/2:y=h*0.50:"
        f"text='%{{pts\\:localtime\\:{start_unix}\\:%m/%d/%Y}}',"
        f"drawtext=fontfile={_FONT_PATH}:"
        f"fontsize=140:fontcolor=white:x=(w-text_w)/2:y=h*0.62:"
        f"text='%{{pts\\:localtime\\:{start_unix}\\:%H\\:%M\\:%S}}'"
    )
    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", f"color=c=#1D4ED8:size=1080x1920:rate=1",
        "-vf", vf,
        "-frames:v", str(n_frames),
        "-y",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg clock frame generation failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')}"
        )
    missing = [
        frames_dir / f"frame_{i:03d}.jpg"
        for i in range(1, n_frames + 1)
        if not (frames_dir / f"frame_{i:03d}.jpg").exists()
           or (frames_dir / f"frame_{i:03d}.jpg").stat().st_size == 0
    ]
    if missing:
        raise RuntimeError(f"FFmpeg produced {len(missing)} missing/empty frames: {missing[:3]}")


def _upload_frames_to_drive(
    frames_dir: Path, test_name: str, root_folder_id: str
) -> tuple[str, list[str]]:
    """Create a Drive folder and upload all JPEG frames. Returns (folder_id, file_ids)."""
    folder_id = drive.create_folder(test_name, root_folder_id)
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    file_ids = []
    for frame in frames:
        fid = drive.upload(frame, folder_id, frame.name, content_type="image/jpeg")
        file_ids.append(fid)
    return folder_id, file_ids


def _run_process_photos(test_name: str, spp_effective: int, timeout: int) -> None:
    """Run process_photos.py as a subprocess with SECONDS_PER_PHOTO override.

    Raises RuntimeError if the subprocess exits non-zero.
    After the subprocess exits, polls state.json until pending_approval appears
    for this test_name (confirms process_photos.py wrote state).
    """
    env = {**os.environ, "SECONDS_PER_PHOTO": str(spp_effective)}
    result = subprocess.run(
        [sys.executable, _PROCESS_PHOTOS, "--project", test_name],
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"process_photos.py exited {result.returncode} for project={test_name}"
        )


def _wait_for_approval(test_name: str, timeout: int) -> None:
    """Poll until check_approval.py has processed the approval (pending_approval cleared
    and a matching pending FB upload is enqueued).

    Raises SystemExit on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = state.get_pending_approval()
        fb = facebook_state.get_pending_upload()
        if pending is None and fb is not None and fb.get("project_name") == test_name:
            return
        time.sleep(_POLL_INTERVAL)
    raise SystemExit(
        f"Stage 4 timed out after {timeout}s waiting for approval of {test_name}"
    )


def _wait_for_facebook_post(test_name: str, timeout: int) -> str:
    """Poll until upload_facebook.py marks the job as 'published'. Returns fb_post_id.

    Raises SystemExit on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        fb = facebook_state.get_pending_upload()
        if fb is not None and fb.get("project_name") == test_name and fb.get("status") == "published":
            return fb.get("fb_post_id", "")
        time.sleep(_POLL_INTERVAL)
    raise SystemExit(
        f"Stage 5 timed out after {timeout}s waiting for Facebook post for {test_name}"
    )


def _cleanup(folder_id: str, fb_post_id: str | None, page_access_token: str) -> None:
    """Delete the Drive test folder and Facebook post created by the most recent run.

    Drive deletion is attempted first. Facebook deletion is skipped when fb_post_id
    is None. FacebookUploadError is caught and logged as a warning (post may have
    already been deleted), so cleanup always exits cleanly.
    """
    try:
        drive.delete(folder_id)
        _log.info("cleanup: deleted Drive folder %s", folder_id)
    except Exception as exc:
        _log.error("cleanup: failed to delete Drive folder %s: %s", folder_id, exc)

    if fb_post_id is None:
        return

    try:
        facebook_api.delete_post(page_access_token, fb_post_id)
        _log.info("cleanup: deleted Facebook post %s", fb_post_id)
    except FacebookUploadError as exc:
        _log.warning(
            "WARNING: Facebook post %s not found or already deleted — skipping (%s)",
            fb_post_id, exc,
        )


def _print_stage(n: int, total: int, label: str, status: str, elapsed: float) -> None:
    """Print a timestamped stage status line to stdout."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    symbol = "✅" if status == "ok" else "❌"
    suffix = f"done ({elapsed:.0f}s)" if status == "ok" else status
    print(f"[{ts}] Stage {n}/{total}: {label} {symbol} {suffix}", flush=True)


def main(argv=None) -> None:
    """Entry point — validate env, run 5-stage pipeline test."""
    parser = argparse.ArgumentParser(
        description="Run the full FieldKit photo-agent pipeline end-to-end."
    )
    parser.add_argument("--duration", type=int, default=30,
                        help="Test video duration in seconds (2–300, default 30)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds for process_photos.py subprocess (default 600)")
    parser.add_argument("--approval-timeout", type=int, default=600,
                        help="Timeout in seconds for the Telegram approval stage (default 600)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete the Drive folder and Facebook post from the most recent run")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    # Pre-flight: required env vars
    missing_vars = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing_vars:
        for var in missing_vars:
            print(f"ERROR: required env var {var} is not set", file=sys.stderr)
        sys.exit(1)

    # Pre-flight: pending approval guard
    existing = state.get_pending_approval()
    if existing:
        print(
            f"ERROR: pending approval already exists for project={existing.get('project_name')} "
            "— resolve it before running the e2e test",
            file=sys.stderr,
        )
        sys.exit(1)

    # Pre-flight: FFmpeg must be on PATH
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH", file=sys.stderr)
        sys.exit(1)

    # Pre-flight: duration range
    if args.duration < _DURATION_MIN:
        print(f"ERROR: --duration must be at least {_DURATION_MIN} seconds", file=sys.stderr)
        sys.exit(1)
    if args.duration > _DURATION_MAX:
        print(f"ERROR: --duration must be at most {_DURATION_MAX} seconds", file=sys.stderr)
        sys.exit(1)

    root_folder_id = os.environ["DRIVE_ROOT_FOLDER_ID"]
    spp_base = int(os.environ.get("SECONDS_PER_PHOTO", str(_SPP_BASE_DEFAULT)))
    n_frames, spp_effective = _compute_frames(args.duration, spp_base)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    test_name = f"e2e-test-{timestamp}"
    start_unix = int(time.time())

    _log.info("Starting e2e test: name=%s duration=%d n_frames=%d spp_effective=%d",
              test_name, args.duration, n_frames, spp_effective)

    total = 5
    overall_start = time.time()

    with tempfile.TemporaryDirectory(prefix="fieldkit_e2e_") as tmp:
        frames_dir = Path(tmp) / "frames"
        frames_dir.mkdir()

        # Stage 1: Generate clock frames
        t0 = time.time()
        try:
            _generate_clock_frames(n_frames, start_unix, frames_dir)
            _print_stage(1, total, f"Clock frames generated ({n_frames} frames)", "ok", time.time() - t0)
        except Exception as exc:
            _print_stage(1, total, "Clock frame generation", f"failed: {exc}", time.time() - t0)
            sys.exit(1)

        # Stage 2: Upload frames to Drive
        t0 = time.time()
        try:
            folder_id, file_ids = _upload_frames_to_drive(frames_dir, test_name, root_folder_id)
            _print_stage(2, total, f"Drive upload ({n_frames} files)", "ok", time.time() - t0)
        except Exception as exc:
            _print_stage(2, total, "Drive upload", f"failed: {exc}", time.time() - t0)
            sys.exit(1)

        # Stage 3: Run process_photos.py + wait for Telegram message
        t0 = time.time()
        try:
            _run_process_photos(test_name, spp_effective, args.timeout)
            _print_stage(3, total, "process_photos.py + Telegram sent", "ok", time.time() - t0)
        except Exception as exc:
            _print_stage(3, total, "process_photos.py", f"failed: {exc}", time.time() - t0)
            sys.exit(1)

    print(f"Tap Approve in Telegram to continue (timeout: {args.approval_timeout}s)", flush=True)

    # Stage 4: Wait for admin approval
    t0 = time.time()
    try:
        _wait_for_approval(test_name, args.approval_timeout)
        _print_stage(4, total, "Approval received", "ok", time.time() - t0)
    except (SystemExit, RuntimeError) as exc:
        elapsed = time.time() - t0
        _print_stage(4, total, "Approval received",
                     f"timed out after {elapsed:.0f}s", elapsed)
        sys.exit(1)

    # Stage 5: Wait for Facebook post to go live
    t0 = time.time()
    try:
        fb_post_id = _wait_for_facebook_post(test_name, args.timeout)
        _print_stage(5, total, "Facebook post live", "ok", time.time() - t0)
    except (SystemExit, RuntimeError) as exc:
        elapsed = time.time() - t0
        _print_stage(5, total, "Facebook post live",
                     f"timed out after {elapsed:.0f}s", elapsed)
        sys.exit(1)

    total_elapsed = time.time() - overall_start
    mins = int(total_elapsed) // 60
    secs = int(total_elapsed) % 60
    print(f"✅ All stages passed. Total: {mins}m {secs}s", flush=True)
    print(f"Post: https://www.facebook.com/{fb_post_id}", flush=True)


if __name__ == "__main__":
    main()
