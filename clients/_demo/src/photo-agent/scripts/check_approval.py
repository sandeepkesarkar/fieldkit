"""
check_approval.py — Poll Telegram for admin approval/rejection and process it.

Usage:
    python3 scripts/check_approval.py                        # cron path
    python3 scripts/check_approval.py --source cron          # cron with source label
    python3 scripts/check_approval.py --callback-query-id <id> --callback-data approve --message-id <msg_id>

Cron path: reads state.json for a pending approval, polls Telegram getUpdates for
a callback_query matching the pending message_id, dispatches approve or reject.

Direct path: when OpenClaw receives a button callback and passes the data as CLI
args, getUpdates is bypassed entirely (OpenClaw already consumed the update).
"""

import argparse
import base64
import email.mime.text
import logging
import os
import subprocess
import sys
from pathlib import Path

import requests

from dotenv import load_dotenv

# Must be called before any FieldKit module import: state.py and logger.py compute
# DATA_DIR/LOG_DIR as module-level constants at import time, so FIELDKIT_DATA_DIR
# and FIELDKIT_LOG_DIR in .env only take effect if os.environ is populated first.
load_dotenv(Path(__file__).parents[1] / ".env")

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive, state
from tools import logger as activity_log
from tools import telegram_api

_log = logging.getLogger(__name__)
_PHOTO_AGENT_DIR = Path(__file__).parents[1]
_REPO_ROOT = Path(__file__).parents[5]


def _load_env() -> None:
    pass  # .env already loaded at module import time (before FieldKit module imports)


def _openclaw_send(message: str) -> None:
    """Send a plain-text message via openclaw. Failures are logged but not raised."""
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "--channel", "telegram", message],
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            _log.warning("openclaw exited %d while sending message", result.returncode)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("openclaw failed while sending message: %s", exc)


def _send_approval_email(agent_email: str, admin_email: str, project_name: str, folder_link: str) -> None:
    """Send the approval email via Gmail REST API. Raises RuntimeError on failure."""
    subject = f"FieldKit — {project_name} approved"
    body = (
        f"The video for project {project_name} has been approved.\n\n"
        f"View folder: {folder_link}"
    )
    msg = email.mime.text.MIMEText(body)
    msg["to"] = admin_email
    msg["from"] = agent_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        access_token = drive._get_access_token()
    except RuntimeError as exc:
        raise RuntimeError(f"Failed to get Gmail access token: {exc}") from exc

    try:
        resp = requests.post(
            "https://www.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Gmail send request failed: {exc}") from exc

    if not resp.ok:
        raise RuntimeError(f"Gmail send failed: HTTP {resp.status_code}")
    _log.info("approval email sent: project=%s", project_name)


def _get_tmp_root() -> Path:
    """Return the allowed root directory for local video files."""
    tmp_raw = os.environ.get("VIDEO_TMP_DIR", "")
    if tmp_raw:
        p = Path(tmp_raw)
        return (p if p.is_absolute() else (_REPO_ROOT / p)).resolve()
    return (_REPO_ROOT / "data" / "photo-agent" / "tmp").resolve()


def _delete_local_file(video_local_path: str, project_name: str) -> None:
    """Delete the local temp video file, refusing to act on paths outside the tmp directory."""
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


def _find_matching_callback(updates: list[dict], message_id: int) -> dict | None:
    """Return the first update whose callback_query.message.message_id matches, or None."""
    for update in updates:
        cq = update.get("callback_query")
        if not cq:
            continue
        if cq.get("message", {}).get("message_id") == message_id:
            return update
    return None


def main(argv=None) -> None:
    """Poll Telegram for the pending approval callback and dispatch approve or reject."""
    parser = argparse.ArgumentParser(description="Check for pending approval and process it.")
    parser.add_argument(
        "--source",
        choices=["cron"],
        default=None,
        help="Invocation source; 'cron' is informational (logged but does not change behaviour).",
    )
    parser.add_argument(
        "--callback-query-id",
        default=None,
        dest="callback_query_id",
        help="Telegram callback_query.id (OpenClaw direct path — bypasses getUpdates).",
    )
    parser.add_argument(
        "--callback-data",
        default=None,
        dest="callback_data",
        help="Telegram callback_query.data, 'approve' or 'reject' (OpenClaw direct path).",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        default=None,
        dest="message_id",
        help="Telegram message_id of the approval message (OpenClaw direct path).",
    )
    args = parser.parse_args(argv)
    if args.source:
        _log.debug("invoked from source=%s", args.source)

    _load_env()

    record = state.get_pending_approval()
    if record is None:
        _log.debug("no pending approval — exiting")
        return

    project_name = record["project_name"]
    drive_video_file_id = record["drive_video_file_id"]
    drive_folder_link = record["drive_folder_link"]
    video_local_path = record["video_local_path"]
    telegram_message_id = record["telegram_message_id"]

    agent_email = os.environ.get("AGENT_EMAIL", "")
    admin_email = os.environ.get("ADMIN_EMAIL", "")

    # Direct path: OpenClaw passes callback data as CLI args, bypassing getUpdates.
    # This is needed because OpenClaw's internal Telegram polling consumes the
    # callback_query update before the cron's getUpdates call can see it.
    direct = (
        args.callback_query_id is not None
        and args.callback_data is not None
        and args.message_id is not None
    )

    if direct:
        if args.message_id != telegram_message_id:
            _log.warning(
                "direct callback message_id=%d does not match pending approval message_id=%d — ignoring",
                args.message_id,
                telegram_message_id,
            )
            return
        callback_query_id = args.callback_query_id
        callback_data = args.callback_data
        new_offset = None  # OpenClaw manages its own offset
    else:
        # Cron path: poll getUpdates for the callback.
        offset = state.get_telegram_offset()

        try:
            updates = telegram_api.get_updates(offset)
        except RuntimeError as exc:
            _log.error("get_updates failed — retrying on next run: %s", exc)
            return

        new_offset = max(u["update_id"] for u in updates) + 1 if updates else offset

        match = _find_matching_callback(updates, telegram_message_id)
        if match is None:
            _log.debug(
                "no matching callback for message_id=%d — advancing offset to %d",
                telegram_message_id,
                new_offset,
            )
            state.set_telegram_offset(new_offset)
            return

        cq = match["callback_query"]
        callback_query_id = cq["id"]
        callback_data = cq.get("data", "")

    # Dismiss the spinner first — per spec, before any email or Telegram message.
    # On failure: advance offset (cron path) so this callback is not reprocessed; leave state intact.
    try:
        telegram_api.answer_callback_query(callback_query_id)
    except RuntimeError as exc:
        _log.error("answer_callback_query failed: %s", exc)
        if new_offset is not None:
            state.set_telegram_offset(new_offset)
        return

    if callback_data == "approve":
        email_sent = False
        if agent_email and admin_email:
            try:
                _send_approval_email(agent_email, admin_email, project_name, drive_folder_link)
                email_sent = True
            except RuntimeError as exc:
                _log.error("approval email failed: %s", exc)
                _openclaw_send(
                    f"⚠️ {project_name}: approved, but email delivery failed.\n"
                    f"View folder: {drive_folder_link}"
                )
        else:
            _log.error("AGENT_EMAIL or ADMIN_EMAIL not set — approval email skipped")
            activity_log.log_error(
                project_name, "approve-email-config", "AGENT_EMAIL or ADMIN_EMAIL not set"
            )
            _openclaw_send(
                f"⚠️ {project_name}: approved, but email not configured.\n"
                f"View folder: {drive_folder_link}"
            )

        if email_sent:
            _openclaw_send(f"✅ Approved: {project_name}\nView folder: {drive_folder_link}")

        _delete_local_file(video_local_path, project_name)
        activity_log.log_approved(project_name)

    elif callback_data == "reject":
        # Drive delete is best-effort — failure is logged but does not block the rejection.
        try:
            drive.delete(drive_video_file_id)
        except RuntimeError as exc:
            _log.error("Drive delete failed for file_id=%s: %s", drive_video_file_id, exc)
            activity_log.log_error(project_name, "drive-delete", str(exc))

        _delete_local_file(video_local_path, project_name)
        _openclaw_send(
            f"❌ Rejected: {project_name}\n"
            "Update the photos in Drive and re-trigger /process_photos."
        )
        activity_log.log_rejected(project_name)

    else:
        # Unknown callback_data: advance offset (cron path) so this update is not
        # reprocessed, but leave the pending approval intact — the admin must re-tap.
        _log.warning(
            "unexpected callback_data=%r for project=%s — ignoring",
            callback_data,
            project_name,
        )
        if new_offset is not None:
            state.set_telegram_offset(new_offset)
        return

    # Runs only on the approve/reject paths.
    # try/finally ensures offset advances (cron path) even if clear_pending_approval raises.
    try:
        state.clear_pending_approval()
    finally:
        if new_offset is not None:
            state.set_telegram_offset(new_offset)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    main()
