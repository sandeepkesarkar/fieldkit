"""
run_e2e_test.py — End-to-end pipeline orchestrator for the FieldKit photo agent.

Chains all 5 stage scripts in sequence. Each stage can also be run independently:
  Stage 1: e2e_stage1_generate_frames.py  — generate clock frames
  Stage 2: e2e_stage2_upload_drive.py     — upload frames to Drive
  Stage 3: e2e_stage3_process.py          — run process_photos.py
  Stage 4: e2e_stage4_await_approval.py   — wait for Telegram approval
  Stage 5: e2e_stage5_await_facebook.py   — wait for Facebook post

Usage:
    python3 scripts/run_e2e_test.py --duration 20
    python3 scripts/run_e2e_test.py --cleanup
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# CLIENT_NAME resolution order (issue #45): a CLIENT_NAME already present in
# the process environment when this script starts (e.g. an inline override
# like `env CLIENT_NAME=foo python3 ...`) wins over the root .env's
# CLIENT_NAME, because load_dotenv(_ROOT / ".env") below passes
# override=False EXPLICITLY — this repo owns that contract rather than
# leaning on python-dotenv's current default (unpinned in
# requirements.txt) — and so never clobbers an already-set env var. This is
# the supported way to run this e2e suite against a specific client without
# touching the shared root .env — see
# platform/docs/hermes/05-cron-verification.md.
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env", override=False)
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)
# The client .env above loads with override=True. If it ever defines its
# own CLIENT_NAME (it shouldn't — see platform/photo-agent/.env.example),
# that would silently clobber the value resolved above. Re-assert it so
# os.environ["CLIENT_NAME"] always matches _CLIENT afterward, including
# for anything this process later shells out to.
os.environ["CLIENT_NAME"] = _CLIENT

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive
import tools.facebook_api as facebook_api
from tools.facebook_api import FacebookUploadError
from tools import state

# Import helpers from stage scripts so existing tests can import them from here.
from scripts.e2e_stage1_generate_frames import _compute_frames, _generate_clock_frames
from scripts.e2e_stage2_upload_drive import _upload_frames_to_drive
from scripts.e2e_stage3_process import _run_process_photos
from scripts.e2e_stage4_await_approval import _wait_for_approval
from scripts.e2e_stage5_await_facebook import _wait_for_facebook_post

_log = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_FRAMES_DIR = _DATA_DIR / "e2e_frames"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"

_REQUIRED_ENV = [
    "FB_PAGE_ACCESS_TOKEN",
    "FB_PAGE_ID",
    "DRIVE_ROOT_FOLDER_ID",
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_CHAT_ID",
]

_DURATION_MIN = 2
_DURATION_MAX = 300
_SPP_BASE_DEFAULT = 4


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

    if args.cleanup:
        missing_token = [k for k in ["FB_PAGE_ACCESS_TOKEN"] if not os.environ.get(k)]
        if missing_token:
            print("ERROR: required env var FB_PAGE_ACCESS_TOKEN is not set", file=sys.stderr)
            sys.exit(1)
        if not _RUN_STATE_FILE.exists():
            print(
                f"ERROR: No previous run found at {_RUN_STATE_FILE} — run the test first.",
                file=sys.stderr,
            )
            sys.exit(1)
        run_state = json.loads(_RUN_STATE_FILE.read_text())
        folder_id_c = run_state.get("folder_id")
        fb_post_id_c = run_state.get("fb_post_id")
        if not folder_id_c:
            print("ERROR: run state has no folder_id — Stage 2 may not have completed.", file=sys.stderr)
            sys.exit(1)
        page_token = os.environ["FB_PAGE_ACCESS_TOKEN"]
        _cleanup(folder_id_c, fb_post_id_c, page_token)
        print("✅ Cleanup complete.", flush=True)
        return

    missing_vars = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing_vars:
        for var in missing_vars:
            print(f"ERROR: required env var {var} is not set", file=sys.stderr)
        sys.exit(1)

    existing = state.get_pending_approval()
    if existing:
        print(
            f"ERROR: pending approval already exists for project={existing.get('project_name')} "
            "— resolve it before running the e2e test",
            file=sys.stderr,
        )
        sys.exit(1)

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH (required by process_photos.py)", file=sys.stderr)
        sys.exit(1)

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

    total = 5
    overall_start = time.time()

    # Stage 1: Generate clock frames to persistent dir
    if _FRAMES_DIR.exists():
        shutil.rmtree(_FRAMES_DIR)
    _FRAMES_DIR.mkdir(parents=True)

    t0 = time.time()
    try:
        _generate_clock_frames(n_frames, start_unix, _FRAMES_DIR)
        _print_stage(1, total, f"Clock frames generated ({n_frames} frames)", "ok", time.time() - t0)
    except Exception as exc:
        _print_stage(1, total, "Clock frame generation", f"failed: {exc}", time.time() - t0)
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

    # Stage 2: Upload frames to Drive
    t0 = time.time()
    try:
        folder_id, _ = _upload_frames_to_drive(_FRAMES_DIR, test_name, root_folder_id)
        _print_stage(2, total, f"Drive upload ({n_frames} files)", "ok", time.time() - t0)
    except Exception as exc:
        _print_stage(2, total, "Drive upload", f"failed: {exc}", time.time() - t0)
        sys.exit(1)

    run_state["folder_id"] = folder_id
    _RUN_STATE_FILE.write_text(json.dumps(run_state, indent=2))

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
        _print_stage(4, total, "Approval received", f"timed out after {elapsed:.0f}s", elapsed)
        sys.exit(1)

    # Stage 5: Wait for Facebook post to go live
    t0 = time.time()
    try:
        fb_post_id = _wait_for_facebook_post(test_name, args.timeout)
        _print_stage(5, total, "Facebook post live", "ok", time.time() - t0)
    except (SystemExit, RuntimeError) as exc:
        elapsed = time.time() - t0
        _print_stage(5, total, "Facebook post live", f"timed out after {elapsed:.0f}s", elapsed)
        sys.exit(1)

    run_state["fb_post_id"] = fb_post_id
    _RUN_STATE_FILE.write_text(json.dumps(run_state, indent=2))

    total_elapsed = time.time() - overall_start
    mins = int(total_elapsed) // 60
    secs = int(total_elapsed) % 60
    print(f"✅ All stages passed. Total: {mins}m {secs}s", flush=True)
    print(f"Post: https://www.facebook.com/{fb_post_id}", flush=True)
    print(
        "Run 'python3 scripts/run_e2e_test.py --cleanup' to remove the Drive folder and Facebook post.",
        flush=True,
    )


if __name__ == "__main__":
    main()
