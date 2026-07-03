"""
upload_facebook.py — Cron script: post the pending Facebook video upload.

Usage:
    python3 scripts/upload_facebook.py
    python3 scripts/upload_facebook.py --source cron

Reads the pending VideoUploadJob from facebook_state.json, uploads the video
to the linked Facebook Page via Graph API, and sends a Telegram confirmation.

Retry and failure-recovery logic (US3):
  - Cooldown: if the last attempt was within 60 seconds, exits silently.
  - Retry limit: 3 attempts. After the 3rd failure, marks the job as failed
    and sends a Telegram alert.
  - Token expiry (FacebookTokenError): skips the retry budget entirely, marks
    failed immediately, and alerts the admin to reconnect the Page.

FB_APP_SECRET is never read here (used only by generate_auth_link.py).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import facebook_api, facebook_logger, facebook_state, telegram_api
from tools.facebook_api import FacebookTokenError, FacebookUploadError

_log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_COOLDOWN_SECONDS = 60
_REPO_ROOT = Path(__file__).parents[3]


def _get_tmp_root() -> Path:
    """Return the allowed root directory for local video files."""
    tmp_raw = os.environ.get("VIDEO_TMP_DIR", "")
    if tmp_raw:
        p = Path(tmp_raw)
        return (p if p.is_absolute() else (_REPO_ROOT / p)).resolve()
    return (_REPO_ROOT / "data" / "photo-agent" / "tmp").resolve()


def _delete_local_file(video_local_path: str, project_name: str) -> None:
    """Delete the local temp video file. Best-effort: logs on failure, never raises."""
    try:
        p = Path(video_local_path).resolve()
        allowed = _get_tmp_root()
        try:
            p.relative_to(allowed)
        except ValueError:
            _log.error(
                "refused to delete file outside tmp directory: project=%s",
                project_name,
            )
            return
        if p.exists():
            p.unlink()
            _log.info("deleted local video file: project=%s", project_name)
        else:
            _log.debug("local video file already absent: project=%s", project_name)
    except OSError as exc:
        _log.warning("failed to delete local video file: project=%s error=%s", project_name, exc)


def main(argv=None) -> None:
    """Entry point — validate env, load pending job, attempt upload."""
    parser = argparse.ArgumentParser(description="Upload the pending Facebook video.")
    parser.add_argument(
        "--source",
        choices=["cron"],
        default=None,
        help="Invocation label for logging (informational only).",
    )
    args = parser.parse_args(argv)
    if args.source:
        _log.debug("invoked from source=%s", args.source)

    page_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    page_id = os.environ.get("FB_PAGE_ID", "")
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    if not page_token or not page_id:
        _log.error("FB_PAGE_ACCESS_TOKEN and FB_PAGE_ID are required")
        sys.exit(1)

    record = facebook_state.get_pending_upload()
    if record is None:
        _log.debug("no pending facebook upload — exiting")
        return

    _process_upload(record, page_token, page_id, chat_id)


def _process_upload(record: dict, page_token: str, page_id: str, chat_id: str) -> None:
    """Attempt to upload the video described by record. Handles retry cooldown and failures."""
    project_name = record["project_name"]
    video_path = record["video_local_path"]
    idem_key = record["idempotency_key"]
    attempt_count = record.get("attempt_count", 0)
    last_attempt_at = record.get("last_attempt_at")

    # Cooldown check: do not retry within 60 seconds of the last attempt.
    if last_attempt_at is not None:
        try:
            last_dt = datetime.fromisoformat(last_attempt_at)
            if datetime.now(timezone.utc) - last_dt < timedelta(seconds=_COOLDOWN_SECONDS):
                _log.debug("cooldown not elapsed for project=%s — exiting", project_name)
                return
        except ValueError:
            _log.warning("unparseable last_attempt_at=%r — proceeding", last_attempt_at)

    # Video file must exist before we call the API.
    if not Path(video_path).exists():
        _log.error("video file missing: project=%s path=%s", project_name, video_path)
        facebook_state.mark_failed(idem_key)
        return

    attempt_number = attempt_count + 1
    facebook_state.mark_uploading(idem_key)
    facebook_logger.log_upload_started(project_name, attempt_number)

    try:
        post_id = facebook_api.upload_video(page_token, page_id, video_path)
    except FacebookTokenError as exc:
        _log.error("Facebook token error: project=%s: %s", project_name, exc)
        facebook_state.mark_failed(idem_key)
        facebook_logger.log_token_expired(project_name)
        _send_alert(
            chat_id,
            f"⚠️ Facebook token expired for {project_name} — reconnect your Page via generate_auth_link.py",
        )
        return
    except FacebookUploadError as exc:
        _log.error("upload failed: project=%s attempt=%d: %s", project_name, attempt_number, exc)
        facebook_state.increment_attempt(idem_key)
        facebook_logger.log_upload_attempt_failed(project_name, attempt_number, str(exc))
        new_count = attempt_count + 1
        if new_count >= _MAX_ATTEMPTS:
            facebook_state.mark_failed(idem_key)
            facebook_logger.log_upload_exhausted(project_name)
            _send_alert(
                chat_id,
                f"⚠️ Facebook upload failed for {project_name} after {_MAX_ATTEMPTS} attempts — check logs",
            )
        return

    # Success path.
    facebook_state.mark_published(idem_key, post_id)
    _delete_local_file(video_path, project_name)
    facebook_logger.log_upload_published(project_name, post_id)
    post_url = f"https://www.facebook.com/{post_id}"
    _send_confirmation(chat_id, f"✅ Video live on Facebook! {post_url}")


def _send_confirmation(chat_id: str, text: str) -> None:
    """Send a success notification. Failure is logged but does not raise."""
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send Telegram confirmation")
        return
    try:
        telegram_api.send_message(chat_id, text)
    except RuntimeError as exc:
        _log.warning("Telegram confirmation failed (non-fatal): %s", exc)


def _send_alert(chat_id: str, text: str) -> None:
    """Send a failure alert. Failure is logged but does not raise."""
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send Telegram alert")
        return
    try:
        telegram_api.send_message(chat_id, text)
    except RuntimeError as exc:
        _log.warning("Telegram alert failed (non-fatal): %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
