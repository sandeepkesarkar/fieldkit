"""
Email intake agent main script.

Polls the agent Gmail inbox, enforces ADMIN_ALLOWLIST, assigns ref IDs,
applies the fk-received Gmail label, and sends Telegram acks via
`openclaw message send`.

Usage:
    python3 scripts/check_email.py                # user-triggered (/check_email)
    python3 scripts/check_email.py --source cron  # cron-triggered (silent on no mail)
"""

import argparse
import base64
import email.message
import email.utils
import fcntl
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Allow `from tools.state import ...` without installing the package.
sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.logger import log_cycle, log_received, log_rejected, log_stale_alert
from tools.state import (
    DATA_DIR,
    dequeue_pending,
    enqueue_pending,
    get_label_id,
    get_ref_id_for_message,
    get_stale_pending,
    save_label_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_ENV_FILE = Path(__file__).parents[1] / ".env"
_LOCK_FILE = DATA_DIR / "run.lock"


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env into os.environ without overwriting already-set variables."""
    if not _ENV_FILE.exists():
        return
    with _ENV_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip matching outer quotes produced by some editors (e.g. VAR="value").
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _acquire_run_lock() -> Optional[object]:
    """Return a locked fd, or None if another instance is already running."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except IOError:
        fd.close()
        return None


# ---------------------------------------------------------------------------
# gws + Telegram wrappers
# ---------------------------------------------------------------------------

def _gws(args: list) -> dict:
    """Run a gws command and return parsed JSON. Raises RuntimeError on failure."""
    # Build a safe summary: include subcommand path but drop --params/--json values
    # to avoid leaking message IDs or request bodies in error strings sent to Telegram.
    safe_parts = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--params", "--json"):
            skip_next = True
            continue
        safe_parts.append(arg)
    cmd_summary = " ".join(safe_parts)

    try:
        result = subprocess.run(["gws"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("gws binary not found — is it installed and on PATH?")
    if result.returncode != 0:
        raise RuntimeError(
            f"gws {cmd_summary} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gws {cmd_summary} returned non-JSON: {result.stdout[:200]}"
        ) from exc
    # Surface Gmail API-level errors returned inside a 200 response.
    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"gws {cmd_summary} API error {err.get('code')}: "
            f"{err.get('message')}"
        )
    return data


_TELEGRAM_MAX_LEN = 4096


def _sanitize_for_telegram(text: str, max_len: int = 200) -> str:
    """Truncate and strip characters that could affect Telegram message rendering."""
    sanitized = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "…"
    return sanitized


def _telegram(chat_id: str, message: str) -> None:
    """Send a Telegram message via the OpenClaw CLI (best-effort, no raise)."""
    result = subprocess.run(
        [
            "openclaw", "message", "send",
            "--channel", "telegram",
            "--target", chat_id,
            "--message", message[:_TELEGRAM_MAX_LEN],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "_telegram: openclaw exited %d — %s",
            result.returncode,
            result.stderr.strip()[:200],
        )


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def _resolve_label_id() -> str:
    """Return the fk-received label ID, creating it if absent, caching the result."""
    label_id = get_label_id()
    if label_id:
        return label_id

    data = _gws(["gmail", "users", "labels", "list", "--params", '{"userId": "me"}'])
    for label in data.get("labels", []):
        if label.get("name") == "fk-received":
            save_label_id(label["id"])
            return label["id"]

    data = _gws([
        "gmail", "users", "labels", "create",
        "--params", '{"userId": "me"}',
        "--json", '{"name": "fk-received"}',
    ])
    if "id" not in data:
        raise RuntimeError(f"gmail labels create returned no id field: {data}")
    save_label_id(data["id"])
    return data["id"]


def _send_stale_alert(stale_entries: List[dict], agent_email: str, admin_email: str) -> None:
    """Send a warning email listing pending entries that may not have been delivered."""
    lines = [
        f'Ref {e["ref_id"]} — {e["subject"]} (queued {e["queued_at"][:16].replace("T", " ")})'
        for e in stale_entries
    ]
    body = (
        "These acknowledgements may not have been delivered via Telegram:\n\n"
        + "\n".join(lines)
        + "\n\nCheck Telegram history or send /check_email to confirm."
    )
    msg = email.message.EmailMessage()
    msg["From"] = agent_email
    msg["To"] = admin_email
    msg["Subject"] = "⚠️ FieldKit: Possible undelivered notifications"
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    # Route through _gws() so PATH resolution, error wrapping, and FileNotFoundError
    # handling are consistent with all other gws calls.
    _gws([
        "gmail", "users", "messages", "send",
        "--params", '{"userId": "me"}',
        "--json", json.dumps({"raw": raw}),
    ])


def _extract_header(headers: list, name: str) -> str:
    """Return the first matching header value, or empty string."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_from_addr(from_header: str) -> str:
    """Extract the bare email address from a From: header value."""
    _, addr = email.utils.parseaddr(from_header)
    # Use an RFC-invalid sentinel so a malformed header can never accidentally
    # match a real allowlist entry (angle brackets make it an invalid addr-spec).
    return addr.lower().strip() if addr else "<unparseable>"


def _count_attachments(payload: dict) -> int:
    """Recursively count message parts that have a filename (attachments)."""
    count = 0
    for part in payload.get("parts", []):
        if part.get("filename"):
            count += 1
        else:
            count += _count_attachments(part)
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run one email intake cycle."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=["user", "cron"],
        default="user",
        help="Invocation source — cron suppresses the 'no new emails' reply",
    )
    args = parser.parse_args()
    triggered_by_cron = args.source == "cron"

    _load_env()

    agent_email = os.environ.get("AGENT_EMAIL", "")
    allowlist_raw = os.environ.get("ADMIN_ALLOWLIST", "")
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    if not chat_id:
        logger.error("ADMIN_TELEGRAM_CHAT_ID is not set")
        sys.exit(1)

    if not agent_email:
        _telegram(chat_id, "check_email: AGENT_EMAIL is not set — add it to .env")
        sys.exit(1)

    # Build an ordered list (first entry = stale alert recipient) + a set for O(1) lookup.
    allowlist_ordered = [a.strip().lower() for a in allowlist_raw.split(",") if a.strip()]
    allowlist = set(allowlist_ordered)
    if not allowlist:
        _telegram(chat_id, "check_email: ADMIN_ALLOWLIST is empty — add at least one address to .env")
        sys.exit(1)

    admin_email = allowlist_ordered[0]

    # Prevent duplicate runs from cron + manual trigger overlapping.
    lock_fd = _acquire_run_lock()
    if lock_fd is None:
        logger.info("Another instance is already running — exiting")
        sys.exit(0)

    try:
        # Phase 1 — Resolve fk-received label
        try:
            label_id = _resolve_label_id()
        except RuntimeError as exc:
            _telegram(chat_id, f"check_email: failed to resolve fk-received label — {exc}")
            sys.exit(1)

        # Phase 2 — Stale check
        try:
            stale = get_stale_pending(threshold_minutes=15)
        except RuntimeError as exc:
            _telegram(chat_id, f"check_email: state error — {exc}")
            sys.exit(1)

        if stale:
            alert_sent = False
            try:
                _send_stale_alert(stale, agent_email, admin_email)
                alert_sent = True
            except Exception as exc:
                logger.warning("Stale alert email failed: %s", exc)
            if alert_sent:
                for entry in stale:
                    dequeue_pending(entry["ref_id"])
                log_stale_alert([e["ref_id"] for e in stale])
            else:
                logger.error(
                    "STALE_ALERT_SEND_FAILED — %d entries remain in pending queue: %s",
                    len(stale),
                    [e["ref_id"] for e in stale],
                )

        # Phase 3 — List unread, unlabeled messages
        try:
            data = _gws([
                "gmail", "users", "messages", "list",
                "--params", '{"userId": "me", "q": "is:unread -label:fk-received"}',
            ])
        except RuntimeError as exc:
            _telegram(chat_id, f"check_email: Gmail list failed — {exc}")
            sys.exit(1)

        messages = data.get("messages", [])
        if "nextPageToken" in data:
            logger.warning(
                "Gmail returned a nextPageToken — inbox has >100 unread unlabeled messages; "
                "only the first page will be processed this cycle"
            )
            _telegram(
                chat_id,
                "⚠ Gmail inbox overflow: more than 100 unread unlabeled messages. "
                "Only the first 100 will be processed this cycle.",
            )

        # Phase 4 — Process each message
        processed = 0
        rejected = 0

        for stub in messages:
            msg_id = stub.get("id")
            if not msg_id:
                continue

            try:
                msg_data = _gws([
                    "gmail", "users", "messages", "get",
                    "--params", json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                ])
            except RuntimeError as exc:
                logger.error("Skipping message %s — fetch failed: %s", msg_id, exc)
                continue

            payload = msg_data.get("payload", {})
            headers = payload.get("headers", [])
            from_addr = _parse_from_addr(_extract_header(headers, "From"))
            subject = _extract_header(headers, "Subject")
            received_at = _extract_header(headers, "Date")
            attachments = _count_attachments(payload)

            if from_addr not in allowlist:
                _telegram(
                    chat_id,
                    f"✗ Email rejected — not in allowlist\n"
                    f"From: {_sanitize_for_telegram(from_addr)}\n"
                    f"Subject: {_sanitize_for_telegram(subject)}",
                )
                subprocess.run(
                    [
                        "gws", "gmail", "users", "messages", "modify",
                        "--params", json.dumps({"userId": "me", "id": msg_id}),
                        "--json", '{"removeLabelIds": ["UNREAD"]}',
                    ],
                    check=False,
                )
                log_rejected(from_addr, subject)
                rejected += 1
            else:
                try:
                    ref_id = get_ref_id_for_message(msg_id)
                    enqueue_pending(ref_id, msg_id, from_addr, subject)
                except RuntimeError as exc:
                    _telegram(chat_id, f"check_email: state error — {exc}")
                    log_cycle(processed, rejected)
                    sys.exit(1)

                _telegram(
                    chat_id,
                    (
                        f"✓ Email received\n"
                        f"From: {_sanitize_for_telegram(from_addr)}\n"
                        f"Subject: {_sanitize_for_telegram(subject)}\n"
                        f"Received: {received_at}\n"
                        f"Attachments: {attachments}\n"
                        f"Ref: {ref_id}"
                    ),
                )
                dequeue_pending(ref_id)
                log_received(from_addr, subject, attachments, ref_id)

                # Best-effort — label apply failure does not abort the cycle.
                subprocess.run(
                    [
                        "gws", "gmail", "users", "messages", "modify",
                        "--params", json.dumps({"userId": "me", "id": msg_id}),
                        "--json", json.dumps({
                            "removeLabelIds": ["UNREAD"],
                            "addLabelIds": [label_id],
                        }),
                    ],
                    check=False,
                )
                processed += 1

        # Phase 5 — Cycle complete
        log_cycle(processed, rejected)

        if not triggered_by_cron and processed == 0 and rejected == 0:
            _telegram(chat_id, "No new emails.")

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
