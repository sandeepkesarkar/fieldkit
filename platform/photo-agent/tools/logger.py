"""
Activity log writer for the photo-video agent.

Appends structured human-readable lines to <repo-root>/logs/photo-agent.log.
Each function corresponds to a distinct lifecycle event in the photo pipeline.
The log directory is created on first write if it does not exist.

project_name and phase must contain only alphanumerics, underscores, or hyphens.
Whitespace or pipe characters in either field would corrupt the pipe-delimited format.

Sensitive fields (chat IDs, bot tokens) are never written to log output.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_log_dir_raw = os.environ.get("FIELDKIT_LOG_DIR", "")
if not _log_dir_raw:
    raise RuntimeError("FIELDKIT_LOG_DIR is not set — add it to your client .env file")
LOG_DIR = Path(_log_dir_raw).resolve()
LOG_FILE = LOG_DIR / "photo-agent.log"

_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_token(value: str, field: str) -> None:
    """Raise ValueError if value would corrupt the pipe-delimited log format."""
    if not _TOKEN_RE.match(value):
        raise ValueError(f"{field} must contain only alphanumerics, underscores, or hyphens; got: {value!r}")


def _now() -> str:
    """Return the current UTC time as 'YYYY-MM-DD HH:MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _append(line: str) -> None:
    """Create the log directory if needed and append line to the activity log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def log_command(project_name: str) -> None:
    """Append a COMMAND line — triggered when /process_photos is invoked."""
    _validate_token(project_name, "project_name")
    logger.info("COMMAND project=%s", project_name)
    _append(f"{_now()} | {'COMMAND':<12} | project={project_name}")


def log_downloaded(project_name: str, count: int) -> None:
    """Append a DOWNLOADED line with the number of photos fetched from Drive."""
    _validate_token(project_name, "project_name")
    logger.info("DOWNLOADED project=%s count=%d", project_name, count)
    _append(f"{_now()} | {'DOWNLOADED':<12} | project={project_name} count={count}")


def log_generated(project_name: str, duration_sec: float, size_bytes: int) -> None:
    """Append a GENERATED line with video duration and file size."""
    _validate_token(project_name, "project_name")
    logger.info("GENERATED project=%s duration_sec=%g size_bytes=%d", project_name, duration_sec, size_bytes)
    _append(f"{_now()} | {'GENERATED':<12} | project={project_name} duration_sec={duration_sec:g} size_bytes={size_bytes}")


def log_uploaded(project_name: str, drive_file_id: str) -> None:
    """Append an UPLOADED line with the Drive file ID of the generated video."""
    _validate_token(project_name, "project_name")
    logger.info("UPLOADED project=%s drive_file_id=%s", project_name, drive_file_id)
    _append(f"{_now()} | {'UPLOADED':<12} | project={project_name} drive_file_id={drive_file_id}")


def log_approval_req(project_name: str, message_id: int) -> None:
    """Append an APPROVAL_REQ line with the Telegram message ID of the approval prompt."""
    _validate_token(project_name, "project_name")
    logger.info("APPROVAL_REQ project=%s message_id=%d", project_name, message_id)
    _append(f"{_now()} | {'APPROVAL_REQ':<12} | project={project_name} message_id={message_id}")


def log_approved(project_name: str) -> None:
    """Append an APPROVED line when the admin approves the video."""
    _validate_token(project_name, "project_name")
    logger.info("APPROVED project=%s", project_name)
    _append(f"{_now()} | {'APPROVED':<12} | project={project_name}")


def log_rejected(project_name: str) -> None:
    """Append a REJECTED line when the admin rejects the video."""
    _validate_token(project_name, "project_name")
    logger.info("REJECTED project=%s", project_name)
    _append(f"{_now()} | {'REJECTED':<12} | project={project_name}")


def log_error(project_name: str, phase: str, detail: str) -> None:
    """Append an ERROR line with the pipeline phase and error detail."""
    _validate_token(project_name, "project_name")
    _validate_token(phase, "phase")
    safe_detail = detail.replace('"', "'").replace("\n", " ").replace("\r", " ")
    logger.error("ERROR project=%s phase=%s", project_name, phase)
    _append(f'{_now()} | {"ERROR":<12} | project={project_name} phase={phase} detail="{safe_detail}"')
