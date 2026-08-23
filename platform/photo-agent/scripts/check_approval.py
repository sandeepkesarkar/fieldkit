"""
check_approval.py — Poll Telegram for admin approval/rejection and process it.

Usage:
    python3 scripts/check_approval.py                        # cron path
    python3 scripts/check_approval.py --source cron          # cron with source label
    python3 scripts/check_approval.py --callback-query-id <id> --callback-data approve --message-id <msg_id>

Cron path: reads state.json for a pending approval, polls Telegram getUpdates for
a callback_query matching the pending message_id, dispatches approve or reject.

Direct path: invoked by Hermes's /check_approval skill with --callback-data
approve, getUpdates is bypassed entirely (see platform/photo-agent/skills/
check-approval/SKILL.md — Hermes cannot dispatch off the raw button tap itself,
so this path only ever fires from the manual /check_approval command).

Bot-token split (issue #29): the entire button-callback surface — sending the
approval message with its inline keyboard (process_photos.py), polling
getUpdates for the tap, answering the callback query, and editing the message
to remove the buttons — runs on TELEGRAM_APPROVAL_BOT_TOKEN, a bot token
dedicated to this cron leg and never shared with Hermes's gateway. Hermes's
own continuous getUpdates long-poll runs on TELEGRAM_BOT_TOKEN. Both tokens
poll independently against Telegram's own servers, so there is no shared
per-token offset for Hermes to advance past a real button tap before this
cron leg's once-a-minute run sees it — the two bots simply never compete for
the same update stream. Plain-text admin notifications (_notify_admin) also
use the approval bot token so the whole approve/reject interaction — button,
tap acknowledgement, and outcome message — stays in one Telegram
conversation, separate from Hermes's own bot conversation.
"""

import argparse
import base64
import email.mime.text
import fcntl
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from dotenv import load_dotenv

_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import drive, state
from tools import facebook_state
from tools import logger as activity_log
from tools import telegram_api

_log = logging.getLogger(__name__)
_PHOTO_AGENT_DIR = Path(__file__).parents[1]
_REPO_ROOT = Path(__file__).parents[3]

# Dedicated bot token for the entire button-callback surface (issue #29) —
# never the same token as Hermes's TELEGRAM_BOT_TOKEN gateway poll.
_APPROVAL_TOKEN_ENV = "TELEGRAM_APPROVAL_BOT_TOKEN"


def _try_acquire_check_lock() -> "IO | None":
    """Try to acquire check_approval.lock exclusively (non-blocking).

    Returns the open lock file object on success, or None if another
    check_approval instance is already running. The caller must close
    the returned file object to release the lock.
    """
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "check_approval.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _load_env() -> None:
    pass  # .env already loaded at module import time (before FieldKit module imports)


def _notify_admin(message: str) -> None:
    """Send a plain-text message to the admin via the Telegram Bot API.

    Failures are logged but not raised — matches the best-effort semantics of
    every call site (an approve/reject decision must never be blocked by a
    failed notification).
    """
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send admin notification")
        return
    try:
        telegram_api.send_message(chat_id, message, token_env_var=_APPROVAL_TOKEN_ENV)
    except Exception as exc:
        _log.warning("failed to send admin notification: %s", exc)


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
    # Default to client-specific data dir so clients never share a tmp directory.
    return (Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent" / "tmp").resolve()


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


def _enqueue_facebook_upload(
    project_name: str, video_local_path: str, telegram_message_id: int
) -> None:
    """Enqueue a Facebook video upload job after approval.

    Skipped silently when FB_PAGE_ID is not configured.
    Failure is logged as an error but does NOT abort the approve flow.
    """
    page_id = os.environ.get("FB_PAGE_ID", "")
    if not page_id:
        return
    idem_key = str(telegram_message_id)
    try:
        if facebook_state.is_published(idem_key):
            _log.warning("FB upload already published for key=%s — skipping enqueue", idem_key)
            return
        facebook_state.set_pending_upload({
            "project_name": project_name,
            "video_local_path": video_local_path,
            "page_id": page_id,
            "status": "pending",
            "attempt_count": 0,
            "last_attempt_at": None,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "idempotency_key": idem_key,
            "fb_post_id": None,
        })
        _log.info("FB upload enqueued: project=%s key=%s", project_name, idem_key)
    except Exception as exc:
        _log.error("Failed to enqueue FB upload for project=%s: %s", project_name, exc)


_TAP_TOASTS = {"approve": "✅ Approving...", "reject": "❌ Rejecting..."}


def _tap_toast(callback_data: str) -> str:
    """Return the brief toast text shown to the admin after tapping Approve or Reject."""
    return _TAP_TOASTS.get(callback_data, "⚠️ Unknown action")


def _acknowledge_tap(
    callback_query_id: str | None,
    callback_data: str,
    chat_id: str,
    message_id: int,
) -> None:
    """Answer the callback query (best-effort) and remove the inline buttons.

    Used on the direct path where the /check_approval skill may or may not supply the callback_query_id.
    Both operations are best-effort: failures are logged as warnings and do not abort
    the approval — the spinner auto-clears after ~10s and button removal is cosmetic.
    """
    if callback_query_id:
        try:
            telegram_api.answer_callback_query(
                callback_query_id, text=_tap_toast(callback_data), token_env_var=_APPROVAL_TOKEN_ENV
            )
        except RuntimeError as exc:
            _log.warning("answer_callback_query failed (non-fatal): %s", exc)
    _remove_buttons(chat_id, message_id)


def _remove_buttons(chat_id: str, message_id: int) -> None:
    """Edit the approval message to remove its inline keyboard. Best-effort."""
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot remove approval buttons")
        return
    try:
        telegram_api.edit_message_reply_markup(chat_id, message_id, token_env_var=_APPROVAL_TOKEN_ENV)
    except RuntimeError as exc:
        _log.warning("edit_message_reply_markup failed (non-fatal): %s", exc)


def _find_matching_callback(updates: list[dict], message_id: int, chat_id: str) -> dict | None:
    """Return the first update whose callback_query matches message_id and chat_id, or None."""
    for update in updates:
        cq = update.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message", {})
        if msg.get("message_id") != message_id:
            continue
        if chat_id and str(msg.get("chat", {}).get("id", "")) != chat_id:
            continue
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
        help="Telegram callback_query.id (direct path — bypasses getUpdates).",
    )
    parser.add_argument(
        "--callback-data",
        choices=["approve", "reject"],
        default=None,
        dest="callback_data",
        help="Telegram callback_query.data, 'approve' or 'reject' (direct path).",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        default=None,
        dest="message_id",
        help="Telegram message_id of the approval message (direct path).",
    )
    args = parser.parse_args(argv)
    if args.source:
        _log.debug("invoked from source=%s", args.source)

    _load_env()

    lock_f = _try_acquire_check_lock()
    if lock_f is None:
        _log.debug("another check_approval instance is running — exiting")
        return

    try:
        return _run(args)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _run(args) -> None:
    """Core logic, called after the check_approval.lock is held."""
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
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    # Direct path: the /check_approval skill (see skills/check-approval/SKILL.md)
    # passes --callback-data approve|reject, bypassing getUpdates. Hermes's Telegram
    # adapter cannot dispatch off the raw button-tap callback_query itself (only the
    # cron leg below sees it), so this path only ever fires from the manual command.
    # --callback-query-id and --message-id are optional extras; --callback-data alone suffices.
    direct = args.callback_data is not None

    if direct:
        callback_data = args.callback_data
        new_offset = None  # direct path has no getUpdates offset to advance

        # Verify message_id if provided — guards against stale direct invocations.
        if args.message_id is not None and args.message_id != telegram_message_id:
            _log.warning(
                "direct callback message_id=%d does not match pending approval message_id=%d — ignoring",
                args.message_id,
                telegram_message_id,
            )
            return

        _acknowledge_tap(args.callback_query_id, callback_data, chat_id, telegram_message_id)

    else:
        # Cron path: poll getUpdates for the callback.
        offset = state.get_telegram_offset()

        try:
            updates = telegram_api.get_updates(offset, token_env_var=_APPROVAL_TOKEN_ENV)
        except RuntimeError as exc:
            _log.error("get_updates failed — retrying on next run: %s", exc)
            return

        new_offset = max(u["update_id"] for u in updates) + 1 if updates else offset

        match = _find_matching_callback(updates, telegram_message_id, chat_id)
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

        # Dismiss the spinner and remove buttons before any email or Telegram message.
        # answer_callback_query failure on the cron path → advance offset and bail
        # (leave state intact so the admin can re-tap the next approval message).
        try:
            _toast = _tap_toast(callback_data)
            telegram_api.answer_callback_query(
                callback_query_id, text=_toast, token_env_var=_APPROVAL_TOKEN_ENV
            )
        except RuntimeError as exc:
            _log.error("answer_callback_query failed: %s", exc)
            state.set_telegram_offset(new_offset)
            return
        _remove_buttons(chat_id, telegram_message_id)

    if callback_data == "approve":
        email_sent = False
        if agent_email and admin_email:
            try:
                _send_approval_email(agent_email, admin_email, project_name, drive_folder_link)
                email_sent = True
            except RuntimeError as exc:
                _log.error("approval email failed: %s", exc)
                _notify_admin(
                    f"⚠️ {project_name}: approved, but email delivery failed.\n"
                    f"View folder: {drive_folder_link}"
                )
        else:
            _log.error("AGENT_EMAIL or ADMIN_EMAIL not set — approval email skipped")
            try:
                activity_log.log_error(
                    project_name, "approve-email-config", "AGENT_EMAIL or ADMIN_EMAIL not set"
                )
            except (ValueError, OSError) as exc:
                _log.error("activity log failed: %s", exc)
            _notify_admin(
                f"⚠️ {project_name}: approved, but email not configured.\n"
                f"View folder: {drive_folder_link}"
            )

        if email_sent:
            _notify_admin(f"✅ Approved: {project_name}\nView folder: {drive_folder_link}")

        try:
            activity_log.log_approved(project_name)
        except (ValueError, OSError) as exc:
            _log.error("activity log failed after approval: %s", exc)

        _enqueue_facebook_upload(project_name, video_local_path, telegram_message_id)

    elif callback_data == "reject":
        # Drive delete is best-effort — failure is logged but does not block the rejection.
        try:
            drive.delete(drive_video_file_id)
        except RuntimeError as exc:
            _log.error("Drive delete failed for file_id=%s: %s", drive_video_file_id, exc)
            try:
                activity_log.log_error(project_name, "drive-delete", str(exc))
            except (ValueError, OSError) as log_exc:
                _log.error("activity log failed after drive-delete error: %s", log_exc)

        _delete_local_file(video_local_path, project_name)
        _notify_admin(
            f"❌ Rejected: {project_name}\n"
            "Update the photos in Drive and re-trigger /process_photos."
        )
        try:
            activity_log.log_rejected(project_name)
        except (ValueError, OSError) as exc:
            _log.error("activity log failed after rejection: %s", exc)

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
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
