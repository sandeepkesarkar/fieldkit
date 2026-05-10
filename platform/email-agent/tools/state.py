"""
State manager for the email agent.

Manages two runtime files on the Mac Mini:
  - ~/fieldkit/data/email-agent/state.json  — ref ID counter, processed message map, label ID cache
  - ~/fieldkit/data/email-agent/pending.json — dead-letter queue for unconfirmed Telegram acks

All read-modify-write operations acquire an exclusive file lock (fcntl.LOCK_EX) before reading
and release it after writing. This prevents ref ID collisions when a cron run and a /check-email
invocation overlap.

Sensitive fields (email addresses, subjects) are never written to log output.
"""

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / "fieldkit" / "data" / "email-agent"
STATE_FILE = DATA_DIR / "state.json"
PENDING_FILE = DATA_DIR / "pending.json"

# ---------------------------------------------------------------------------
# Internal helpers — file I/O with the lock already held
# ---------------------------------------------------------------------------

def _read_state(fd) -> dict:
    """Read and parse state.json from an open, locked file descriptor."""
    fd.seek(0)
    content = fd.read()
    if not content:
        logger.debug("state.json is empty — using defaults")
        # Return a fresh dict each time — never share a mutable default across callers.
        return {"last_ref_id": 0, "processed": {}}
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("state.json is corrupt and cannot be parsed: %s", exc)
        raise RuntimeError("state.json is corrupt — delete or restore it manually") from exc


def _write_state(fd, data: dict) -> None:
    """Overwrite state.json via an open, locked file descriptor."""
    fd.seek(0)
    fd.truncate()
    fd.write(json.dumps(data, indent=2))
    fd.flush()
    os.fsync(fd.fileno())


def _read_pending(fd) -> dict:
    """Read and parse pending.json from an open, locked file descriptor."""
    fd.seek(0)
    content = fd.read()
    if not content:
        logger.debug("pending.json is empty — using defaults")
        # Return a fresh dict each time — never share a mutable default across callers.
        return {"pending": []}
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("pending.json is corrupt and cannot be parsed: %s", exc)
        raise RuntimeError("pending.json is corrupt — delete or restore it manually") from exc


def _write_pending(fd, data: dict) -> None:
    """Overwrite pending.json via an open, locked file descriptor."""
    fd.seek(0)
    fd.truncate()
    fd.write(json.dumps(data, indent=2))
    fd.flush()
    os.fsync(fd.fileno())


# ---------------------------------------------------------------------------
# Ref ID management
# ---------------------------------------------------------------------------

def get_ref_id_for_message(gmail_message_id: str) -> str:
    """Return the ref ID for a Gmail message ID, assigning a new one if unseen."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Open in append+read mode so the file is created if missing without truncating an existing one.
    with open(STATE_FILE, "a+") as fd:
        logger.debug("Acquiring exclusive lock on state.json")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read_state(fd)

            existing = data.get("processed", {}).get(gmail_message_id)
            if existing:
                # Already processed — reuse the same ref ID so duplicate acks are identifiable.
                logger.info("Message already processed — reusing ref_id=%s", existing)
                return existing

            # Increment counter and zero-pad to 4 digits (e.g. 1 → #0001, 1000 → #1000).
            data.setdefault("processed", {})
            data["last_ref_id"] = data.get("last_ref_id", 0) + 1
            ref_id = f"#{data['last_ref_id']:04d}"

            data["processed"][gmail_message_id] = ref_id
            _write_state(fd, data)
            logger.info("Assigned new ref_id=%s (gmail_message_id=%s)", ref_id, gmail_message_id)
            return ref_id
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            logger.debug("Released lock on state.json")


def read_last_ref_id() -> int:
    """Return the current ref ID counter value without modifying state."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        logger.debug("state.json missing — last_ref_id=0")
        return 0
    with open(STATE_FILE, "r") as fd:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            content = fd.read()
            if not content:
                return 0
            value = json.loads(content).get("last_ref_id", 0)
            logger.debug("read_last_ref_id=%d", value)
            return value
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Label ID cache
# ---------------------------------------------------------------------------

def get_label_id() -> Optional[str]:
    """Return the cached fk-received label ID, or None if not yet resolved."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        logger.debug("state.json missing — no label ID cached")
        return None
    with open(STATE_FILE, "r") as fd:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            content = fd.read()
            if not content:
                return None
            label_id = json.loads(content).get("fk_received_label_id")
            logger.debug("get_label_id=%s", label_id)
            return label_id
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def save_label_id(label_id: str) -> None:
    """Cache the resolved fk-received label ID in state.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "a+") as fd:
        logger.debug("Acquiring exclusive lock on state.json for save_label_id")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read_state(fd)
            data["fk_received_label_id"] = label_id
            _write_state(fd, data)
            logger.info("Cached fk-received label_id=%s", label_id)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            logger.debug("Released lock on state.json")


# ---------------------------------------------------------------------------
# Pending queue (dead-letter queue for unconfirmed Telegram acks)
# ---------------------------------------------------------------------------

def enqueue_pending(ref_id: str, gmail_message_id: str, from_addr: str, subject: str) -> None:
    """Add a pending entry before attempting Telegram delivery.

    Sensitive fields (from_addr, subject) are stored in the file for alert email
    generation but are never written to log output.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PENDING_FILE, "a+") as fd:
        logger.debug("Acquiring exclusive lock on pending.json for enqueue ref_id=%s", ref_id)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read_pending(fd)
            # Timestamp in UTC with Z suffix — consistent with ISO 8601 and easy to parse.
            queued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data["pending"].append({
                "ref_id": ref_id,
                "gmail_message_id": gmail_message_id,
                "from": from_addr,       # stored for alert email body; never logged
                "subject": subject,      # stored for alert email body; never logged
                "queued_at": queued_at,
            })
            _write_pending(fd, data)
            logger.info(
                "Enqueued pending entry ref_id=%s gmail_message_id=%s queued_at=%s",
                ref_id, gmail_message_id, queued_at,
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            logger.debug("Released lock on pending.json")


def dequeue_pending(ref_id: str) -> None:
    """Remove a pending entry after it has been acted upon.

    Called after a Telegram send attempt (success or failure — OpenClaw delivery
    cannot be observed) and after a stale-alert email is dispatched for an entry.
    If the ref_id is not found, a warning is logged and no write occurs.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PENDING_FILE.exists():
        logger.warning("dequeue_pending: pending.json missing — nothing to dequeue for ref_id=%s", ref_id)
        return
    with open(PENDING_FILE, "a+") as fd:
        logger.debug("Acquiring exclusive lock on pending.json for dequeue ref_id=%s", ref_id)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = _read_pending(fd)
            before = len(data["pending"])
            # Filter out the matching entry; leave all others intact.
            data["pending"] = [e for e in data["pending"] if e["ref_id"] != ref_id]
            after = len(data["pending"])
            if before == after:
                logger.warning("dequeue_pending: ref_id=%s not found in pending.json", ref_id)
            else:
                _write_pending(fd, data)
                logger.info("Dequeued pending entry ref_id=%s", ref_id)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            logger.debug("Released lock on pending.json")


def get_stale_pending(threshold_minutes: int = 15) -> List[dict]:
    """Return pending entries older than threshold_minutes.

    Used at the start of each cycle to detect Telegram messages that may not
    have been delivered — if an entry has been sitting for more than 15 minutes,
    something likely went wrong and the admin should be alerted via email.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PENDING_FILE.exists():
        logger.debug("pending.json missing — no stale entries")
        return []

    with open(PENDING_FILE, "r") as fd:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            content = fd.read()
            if not content:
                return []
            data = json.loads(content)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)

    now = datetime.now(timezone.utc)
    stale = []
    for entry in data.get("pending", []):
        # Replace Z suffix with +00:00 so fromisoformat works on Python < 3.11.
        queued_at = datetime.fromisoformat(entry["queued_at"].replace("Z", "+00:00"))
        age_minutes = (now - queued_at).total_seconds() / 60
        if age_minutes > threshold_minutes:
            stale.append(entry)

    if stale:
        # Log ref IDs only — not from/subject which are sensitive.
        stale_refs = [e["ref_id"] for e in stale]
        logger.warning(
            "Found %d stale pending entries (>%d min): %s",
            len(stale), threshold_minutes, stale_refs,
        )
    else:
        logger.debug("No stale pending entries found")

    return stale
