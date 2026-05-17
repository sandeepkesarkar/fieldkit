"""
process_photos.py — Generate a photo slideshow video and send for approval.

Usage:
    python3 scripts/process_photos.py --project <name>

Invoked by the /process_photos OpenClaw skill. Integrates Drive, FFmpeg,
Telegram, and state management. Exits non-zero on any failure, sending a
Telegram error message via openclaw before doing so.
"""

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive, state
from tools import logger as activity_log
from tools import telegram_api
from tools.drive import DriveFolderNotFoundError
from tools.video_generator import FFmpegVideoGenerator, VideoConfig, VideoGenerationError

_REPO_ROOT = Path(__file__).parents[5]
_PHOTO_AGENT_DIR = Path(__file__).parents[1]

_MIN_PHOTOS = 2
_MAX_PHOTOS = 30
_DEFAULT_SPP = 4


def _load_env() -> None:
    load_dotenv(_PHOTO_AGENT_DIR / ".env")


def _telegram_error(message: str) -> None:
    """Send an error to the admin via openclaw Telegram and exit non-zero."""
    subprocess.run(
        ["openclaw", "message", "send", "--channel", "telegram", message],
        check=False,
    )
    sys.exit(1)


def _acquire_run_lock():
    """Exclusively lock data/photo-agent/run.lock. Returns the open file object."""
    data_dir = (
        Path(os.environ.get("FIELDKIT_DATA_DIR", str(_REPO_ROOT / "data")))
        / "photo-agent"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "run.lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def scrub(photos: list[Path]) -> list[Path]:
    """Privacy scrub placeholder — returns photos unchanged. Activate in a future phase."""
    return photos


def _approval_text(project_name: str, photo_count: int, duration_sec: float, folder_link: str) -> str:
    return (
        f"📸 *{project_name}* — {photo_count} photos, {duration_sec:g}s video\n"
        f"[View folder]({folder_link})\n\n"
        "Approve or reject:"
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Process photos and send for approval.")
    parser.add_argument("--project", help="Project name (Drive subfolder name)")
    args = parser.parse_args(argv)

    _load_env()

    if not args.project:
        _telegram_error("❌ Usage: /process_photos project=<name>")

    project_name = args.project
    chat_id = os.environ["ADMIN_TELEGRAM_CHAT_ID"]
    root_folder_id = os.environ["DRIVE_ROOT_FOLDER_ID"]
    spp = int(os.environ.get("SECONDS_PER_PHOTO", str(_DEFAULT_SPP)))

    tmp_base_raw = os.environ.get("VIDEO_TMP_DIR", "")
    if tmp_base_raw:
        p = Path(tmp_base_raw)
        tmp_base = p if p.is_absolute() else (_REPO_ROOT / p).resolve()
    else:
        tmp_base = _REPO_ROOT / "data" / "photo-agent" / "tmp"

    lock_f = _acquire_run_lock()
    try:
        # Guard: reject if another approval is already pending
        if state.get_pending_approval() is not None:
            _telegram_error(
                f"⚠️ {project_name}: already awaiting approval. Use /check_approval first."
            )

        # Locate the Drive project folder
        try:
            folder_id = drive.find_folder(project_name, root_folder_id)
        except DriveFolderNotFoundError:
            _telegram_error(f"❌ Drive folder not found: {project_name!r}")

        # Validate photo count
        photos_meta = drive.list_photos(folder_id)
        count = len(photos_meta)
        if count < _MIN_PHOTOS:
            _telegram_error(
                f"❌ {project_name}: found {count} photo(s) — need at least {_MIN_PHOTOS}."
            )
        if count > _MAX_PHOTOS:
            _telegram_error(
                f"❌ {project_name}: found {count} photos — maximum is {_MAX_PHOTOS}."
            )

        # Clear and recreate project temp directory
        project_tmp = tmp_base / project_name
        if project_tmp.exists():
            shutil.rmtree(project_tmp)
        project_tmp.mkdir(parents=True, exist_ok=True)

        # Download photos
        local_photos: list[Path] = []
        for meta in photos_meta:
            local_path = project_tmp / meta["name"]
            try:
                drive.download(meta["id"], local_path)
            except RuntimeError as exc:
                _telegram_error(f"❌ {project_name}: photo download failed — {exc}")
            activity_log.log_downloaded(project_name, meta["name"])
            local_photos.append(local_path)

        # Privacy scrub (no-op placeholder)
        local_photos = scrub(local_photos)

        # Generate video
        cfg = VideoConfig(seconds_per_photo=spp)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = project_tmp / f"{project_name}_{ts}.mp4"
        try:
            FFmpegVideoGenerator().generate(local_photos, cfg, output_path)
        except VideoGenerationError as exc:
            _telegram_error(f"❌ {project_name}: video generation failed — {exc}")

        xfade = cfg.crossfade_duration
        duration_sec = count * spp - (count - 1) * xfade
        activity_log.log_generated(project_name, duration_sec)

        # Upload video to Drive; retain local file on failure for manual recovery
        try:
            drive_video_file_id = drive.upload(output_path, folder_id, output_path.name)
        except (RuntimeError, FileNotFoundError) as exc:
            _telegram_error(f"❌ {project_name}: Drive upload failed — {exc}")

        activity_log.log_uploaded(project_name, drive_video_file_id)

        # Send approval message with inline keyboard
        folder_link_url = drive.folder_link(folder_id)
        msg_id = telegram_api.send_message_with_buttons(
            chat_id,
            _approval_text(project_name, count, duration_sec, folder_link_url),
            [("✅ Approve", "approve"), ("❌ Reject", "reject")],
        )

        # Persist approval state
        triggered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.set_pending_approval({
            "project_name": project_name,
            "drive_folder_id": folder_id,
            "drive_video_file_id": drive_video_file_id,
            "drive_folder_link": folder_link_url,
            "video_local_path": str(output_path),
            "telegram_message_id": msg_id,
            "triggered_at": triggered_at,
        })
        activity_log.log_approval_req(project_name, msg_id)

    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


if __name__ == "__main__":
    main()
