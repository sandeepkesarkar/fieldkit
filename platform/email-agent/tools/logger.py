"""
Activity log writer for the email agent.

Appends structured human-readable lines to <repo-root>/logs/email-agent.log.
Each function corresponds to a distinct lifecycle event in the email pipeline.
The log directory is created on first write if it does not exist.

Sensitive fields (email addresses, subjects) appear in the log file as intended
for admin review but are never passed to the Python logging subsystem.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parents[3] / "logs"
LOG_FILE = LOG_DIR / "email-agent.log"


def _now() -> str:
    """Return the current UTC time as 'YYYY-MM-DD HH:MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _append(line: str) -> None:
    """Create the log directory if needed and append line to the activity log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")
    logger.debug("Wrote activity log line")


def log_received(from_addr: str, subject: str, attachments: int, ref_id: str) -> None:
    """Append a RECEIVED line to the activity log."""
    event = "RECEIVED"
    safe_subject = subject.replace('"', "'")
    line = f'{_now()} | {event:<12} | from={from_addr} subject="{safe_subject}" attachments={attachments} ref={ref_id}'
    _append(line)


def log_rejected(from_addr: str, subject: str) -> None:
    """Append a REJECTED line to the activity log."""
    event = "REJECTED"
    safe_subject = subject.replace('"', "'")
    line = f'{_now()} | {event:<12} | from={from_addr} subject="{safe_subject}"'
    _append(line)


def log_stale_alert(ref_ids: List[str]) -> None:
    """Append a STALE_ALERT line to the activity log."""
    event = "STALE_ALERT"
    count = len(ref_ids)
    refs = ",".join(ref_ids)
    line = f"{_now()} | {event:<12} | count={count} refs={refs}"
    _append(line)


def log_cycle(processed: int, rejected: int) -> None:
    """Append a CYCLE line to the activity log."""
    event = "CYCLE"
    line = f"{_now()} | {event:<12} | processed={processed} rejected={rejected}"
    _append(line)
