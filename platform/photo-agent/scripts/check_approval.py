"""
check_approval.py — Process an admin approve/reject decision for the pending video.

Usage:
    python3 scripts/check_approval.py --callback-data approve
    python3 scripts/check_approval.py --callback-data reject

Invoked directly by Hermes's /photo_approve and /photo_reject skills (see
platform/photo-agent/skills/photo-approve/SKILL.md and
platform/photo-agent/skills/photo-reject/SKILL.md — named with a `photo-`
prefix, not the bare `approve`/`reject` this issue originally specified,
because `approve` collides with a built-in Hermes core command; see those
SKILL.md files' naming notes) — Hermes shells out to this script
synchronously in response to the admin's command and relays its output;
there is no poller or background process on either side.

The system operates on a single-pending-approval-at-a-time model
(state.json's pending_approval field is singular), so a bare /photo_approve
or /photo_reject carries unambiguous semantics: whichever approval is
currently pending. If nothing is pending, the script exits 0 with no output.

---

Historical note (issue #49, 2026-08-26): this script used to be a
cron-driven poller. Before this change, `process_photos.py` sent the
approval-request message with inline Approve/Reject buttons on a second,
dedicated `TELEGRAM_APPROVAL_BOT_TOKEN` (issue #29), and this script's
`--source cron` entry polled that bot's `getUpdates` once a minute, looking
for the button-tap `callback_query`, long-polling for up to 45s to close a
callback-freshness race against Telegram's own ~15s answer window (issue
#31). A `--callback-data` direct path already existed alongside the cron
path — added by issue #8 for a manual "/check_approval" Hermes command that
could force an immediate re-check — because Hermes's Telegram adapter has
no hook for a raw button-tap `callback_query` at all (verified against
Hermes's own source; see platform/docs/hermes/04-check-approval-skill.md).

Issue #49 eliminates the poller entirely rather than continuing to shrink
its race window: plain text/slash-command messages have no callback-
freshness deadline in Telegram's API (only `callback_query` does), so
routing approve/reject through Hermes's own always-running gateway poller
— which the direct path already exercised — removes the race
architecturally. The buttons are gone from the approval-request message
(see process_photos.py), the cron leg and its long-poll/offset/callback-
matching machinery are gone from this script, and the second bot token
(TELEGRAM_APPROVAL_BOT_TOKEN) is retired along with them — one bot per
client now handles both Hermes's gateway traffic and the approval flow. See
platform/docs/hermes/10-text-based-approval-migration.md for the full
writeup, the empirical dispatch verification, and the live-migration steps
for already-deployed clients (deferred to a human follow-up, not part of
this change).
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

# CLIENT_NAME resolution order (issue #45): a CLIENT_NAME already present in
# the process environment when this script starts (e.g. `env CLIENT_NAME=foo
# python3 ...` on a crontab line, or an inline override on a manual
# invocation) wins over the root .env's CLIENT_NAME, because
# load_dotenv(_ROOT / ".env") below passes override=False EXPLICITLY —
# this repo owns that contract rather than leaning on python-dotenv's
# current default (unpinned in requirements.txt), so it never clobbers an
# already-set env var regardless of what a future dependency upgrade does.
# This is the supported mechanism for running multiple clients' cron-driven
# flows concurrently on one machine: each cron entry sets CLIENT_NAME
# inline and never touches the shared root .env, so there's no mutable
# state one client's run could accidentally repoint at another's. Today's
# single-client posture (no inline override, CLIENT_NAME only in the root
# .env) is unaffected. See platform/docs/hermes/05-cron-verification.md.
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

from tools import drive, state
from tools import facebook_state
from tools import logger as activity_log
from tools import telegram_api

_log = logging.getLogger(__name__)
_PHOTO_AGENT_DIR = Path(__file__).parents[1]
_REPO_ROOT = Path(__file__).parents[3]


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
        telegram_api.send_message(chat_id, message)
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


def main(argv=None) -> None:
    """Parse --callback-data and dispatch the approve/reject decision."""
    parser = argparse.ArgumentParser(
        description="Process an admin approve/reject decision for the pending video."
    )
    parser.add_argument(
        "--callback-data",
        choices=["approve", "reject"],
        required=True,
        dest="callback_data",
        help="Decision to process: 'approve' or 'reject'.",
    )
    args = parser.parse_args(argv)

    _load_env()

    lock_f = _try_acquire_check_lock()
    if lock_f is None:
        _log.debug("another check_approval instance is running — exiting")
        return

    try:
        return _run(args.callback_data)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _run(callback_data: str) -> None:
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

    else:  # reject
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

    state.clear_pending_approval()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
