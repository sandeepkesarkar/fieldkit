"""
process_photos.py — Generate a photo slideshow video and send for approval.

Usage:
    python3 scripts/process_photos.py --project <name>

Invoked by the /process_photos Hermes skill. Integrates Drive, FFmpeg,
Telegram, and state management. Exits non-zero on any failure, sending a
Telegram error message via the Bot API before doing so.

Lock discipline: run.lock is held for the duration of the pipeline to prevent
concurrent runs. state.json is separately locked inside each tools/state.py
call. The two locks are on different files; no ordering conflict is possible.
"""

import argparse
import fcntl
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

# Two-step env loading: root config first (CLIENT_NAME, FIELDKIT_ROOT), then
# client secrets (override=True so client values win). Must run before any
# FieldKit module import — state.py and logger.py raise RuntimeError at import
# time if FIELDKIT_DATA_DIR / FIELDKIT_LOG_DIR are unset.
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive, paths, state
from tools import logger as activity_log
from tools import telegram_api
from tools.drive import DriveFolderNotFoundError
from tools.video_generator import FFmpegVideoGenerator, VideoConfig, VideoGenerationError

_log = logging.getLogger(__name__)

_PHOTO_AGENT_DIR = Path(__file__).parents[1]

_MIN_PHOTOS = 2
_MAX_PHOTOS = 30
_DEFAULT_SPP = 4
_PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def _load_env() -> None:
    pass  # .env already loaded at module import time (before FieldKit module imports)


def _telegram_error(message: str) -> NoReturn:
    """Send an error to the admin via the Telegram Bot API and exit non-zero.

    Best-effort — a failed notification must never prevent the non-zero exit.
    """
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send error notification")
    else:
        try:
            telegram_api.send_message(chat_id, message)
        except Exception as exc:
            _log.warning("failed to send error notification: %s", exc)
    sys.exit(1)


def _acquire_run_lock():
    """Exclusively lock data/photo-agent/run.lock. Returns the open file object."""
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "run.lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def scrub(photos: list[Path]) -> list[Path]:
    """Privacy scrub placeholder — returns photos unchanged. Activate in a future phase."""
    return photos


def _safe_filename(raw_name: str) -> str:
    """Return just the filename component, rejecting names with non-photo extensions."""
    name = Path(raw_name).name
    if not name or Path(name).suffix.lower() not in _PHOTO_SUFFIXES:
        raise ValueError(f"Unsafe or non-photo filename from Drive: {raw_name!r}")
    return name


def _approval_text(project_name: str, photo_count: int, duration_sec: float, folder_link: str) -> str:
    # Plain text, deliberately no Markdown syntax — project_name may contain
    # underscores (see _PROJECT_NAME_RE) and Telegram's legacy "Markdown"
    # parse_mode treats any unescaped '_' as an italic delimiter, which stripped
    # the underscores from /photo_approve and /photo_reject (issue #54). Telegram
    # auto-links bare URLs, so folder_link is still tappable without [text](url).
    return (
        f"📸 {project_name} — {photo_count} photos, {duration_sec:g}s video\n"
        f"View folder: {folder_link}\n\n"
        "Reply /photo_approve or /photo_reject."
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Process photos and send for approval.")
    parser.add_argument("--project", help="Project name (Drive subfolder name)")
    args = parser.parse_args(argv)

    _load_env()

    if not args.project:
        _telegram_error("❌ Usage: /process_photos project=<name>")

    project_name = args.project
    if not _PROJECT_NAME_RE.match(project_name):
        _telegram_error(
            f"❌ Invalid project name: {project_name!r} — "
            "only letters, digits, hyphens, and underscores are allowed."
        )

    activity_log.log_command(project_name)

    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID")
    if not chat_id:
        _telegram_error("❌ ADMIN_TELEGRAM_CHAT_ID is not set.")

    root_folder_id = os.environ.get("DRIVE_ROOT_FOLDER_ID")
    if not root_folder_id:
        _telegram_error("❌ DRIVE_ROOT_FOLDER_ID is not set.")

    try:
        spp = int(os.environ.get("SECONDS_PER_PHOTO", str(_DEFAULT_SPP)))
    except ValueError:
        _telegram_error(
            f"❌ SECONDS_PER_PHOTO must be an integer; got: "
            f"{os.environ.get('SECONDS_PER_PHOTO')!r}"
        )

    tmp_base = paths.get_video_tmp_root()

    try:
        lock_f = _acquire_run_lock()
    except OSError as exc:
        _telegram_error(f"❌ {project_name}: failed to acquire run lock — {exc}")

    try:
        # Guard: reject if another approval is already pending
        try:
            pending = state.get_pending_approval()
        except RuntimeError as exc:
            _telegram_error(f"❌ {project_name}: failed to read state — {exc}")
        if pending is not None:
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
        try:
            if project_tmp.exists():
                shutil.rmtree(project_tmp)
            project_tmp.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _telegram_error(f"❌ {project_name}: failed to prepare temp directory — {exc}")

        # Validate all filenames upfront before downloading — catches duplicates and
        # unsafe names without starting any downloads.
        safe_names: list[str] = []
        seen_names: set[str] = set()
        for meta in photos_meta:
            try:
                safe_name = _safe_filename(meta["name"])
            except ValueError as exc:
                _telegram_error(f"❌ {project_name}: {exc}")
            if safe_name in seen_names:
                _telegram_error(
                    f"❌ {project_name}: duplicate photo filename in Drive folder — "
                    "rename or remove the duplicate and re-trigger."
                )
            seen_names.add(safe_name)
            safe_names.append(safe_name)

        # Download photos
        local_photos: list[Path] = []
        for meta, safe_name in zip(photos_meta, safe_names):
            local_path = project_tmp / safe_name
            try:
                drive.download(meta["id"], local_path)
            except RuntimeError as exc:
                _telegram_error(f"❌ {project_name}: photo download failed — {exc}")
            local_photos.append(local_path)

        activity_log.log_downloaded(project_name, len(local_photos))

        # Privacy scrub (no-op placeholder)
        local_photos = scrub(local_photos)
        n = len(local_photos)

        # Generate video
        cfg = VideoConfig(seconds_per_photo=spp)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = project_tmp / f"{project_name}_{ts}.mp4"
        try:
            FFmpegVideoGenerator().generate(local_photos, cfg, output_path)
        except VideoGenerationError as exc:
            _telegram_error(f"❌ {project_name}: video generation failed — {exc}")

        xfade = cfg.crossfade_duration
        duration_sec = n * spp - (n - 1) * xfade
        activity_log.log_generated(project_name, duration_sec, output_path.stat().st_size)

        # Upload video to Drive; retain local file on failure for manual recovery
        try:
            drive_video_file_id = drive.upload(output_path, folder_id, output_path.name)
        except (RuntimeError, FileNotFoundError) as exc:
            _telegram_error(f"❌ {project_name}: Drive upload failed — {exc}")

        activity_log.log_uploaded(project_name, drive_video_file_id)

        # Send the approval-request message as plain text — no inline buttons.
        # The admin replies /photo_approve or /photo_reject as a Hermes command,
        # dispatched through Hermes's own always-running gateway poller on the
        # single TELEGRAM_BOT_TOKEN (issue #49 retired the dedicated second bot
        # and the button-callback flow that required it).
        folder_link_url = drive.folder_link(folder_id)
        try:
            msg_id = telegram_api.send_message(
                chat_id,
                _approval_text(project_name, n, duration_sec, folder_link_url),
            )
        except RuntimeError as exc:
            _telegram_error(f"❌ {project_name}: failed to send approval message — {exc}")

        # Persist approval state
        triggered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            state.set_pending_approval({
                "project_name": project_name,
                "drive_folder_id": folder_id,
                "drive_video_file_id": drive_video_file_id,
                "drive_folder_link": folder_link_url,
                "video_local_path": str(output_path),
                "telegram_message_id": msg_id,
                "triggered_at": triggered_at,
            })
        except RuntimeError:
            _telegram_error(
                f"❌ {project_name}: approval message sent (msg_id={msg_id}) but "
                f"state write failed. Drive folder: {folder_link_url}"
            )
        activity_log.log_approval_req(project_name, msg_id)

    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
