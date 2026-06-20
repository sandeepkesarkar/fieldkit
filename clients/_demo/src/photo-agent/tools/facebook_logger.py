"""
Activity log writer for Facebook video upload events.

Appends structured pipe-delimited lines to <repo-root>/logs/photo-agent.log
(the same file as logger.py). Each function corresponds to a distinct lifecycle
event in the Facebook video upload pipeline.

project_name must contain only alphanumerics, underscores, or hyphens.
Token values and PII are never written to log output.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = Path(os.environ.get("FIELDKIT_LOG_DIR", str(Path(__file__).parents[5] / "logs"))).resolve()
LOG_FILE = LOG_DIR / "photo-agent.log"

_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_token(value: str, field: str) -> None:
    """Raise ValueError if value would corrupt the pipe-delimited log format."""
    if not _TOKEN_RE.match(value):
        raise ValueError(
            f"{field} must contain only alphanumerics, underscores, or hyphens; got: {value!r}"
        )


def _now() -> str:
    """Return current UTC time as 'YYYY-MM-DD HH:MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _append(line: str) -> None:
    """Create the log directory if needed and append line to the activity log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def _safe_error(detail: str) -> str:
    """Sanitize an error string for safe inclusion in the pipe-delimited log."""
    return (
        detail
        .replace("|", " ")
        .replace('"', "'")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def log_upload_enqueued(project_name: str) -> None:
    """Append a FB_ENQUEUED line when a video upload job is enqueued."""
    _validate_token(project_name, "project_name")
    logger.info("FB_ENQUEUED project=%s", project_name)
    _append(f"{_now()} | {'FB_ENQUEUED':<12} | project={project_name}")


def log_upload_started(project_name: str, attempt: int) -> None:
    """Append a FB_STARTED line when an upload attempt begins."""
    _validate_token(project_name, "project_name")
    logger.info("FB_STARTED project=%s attempt=%d", project_name, attempt)
    _append(f"{_now()} | {'FB_STARTED':<12} | project={project_name} attempt={attempt}")


def log_upload_published(project_name: str, post_id: str) -> None:
    """Append a FB_PUBLISHED line when the video is successfully published."""
    _validate_token(project_name, "project_name")
    logger.info("FB_PUBLISHED project=%s post_id=%s", project_name, post_id)
    _append(f"{_now()} | {'FB_PUBLISHED':<12} | project={project_name} post_id={post_id}")


def log_upload_attempt_failed(project_name: str, attempt: int, error: str) -> None:
    """Append a FB_FAILED line when an upload attempt fails (retryable)."""
    _validate_token(project_name, "project_name")
    safe = _safe_error(error)
    logger.error("FB_FAILED project=%s attempt=%d", project_name, attempt)
    _append(f'{_now()} | {"FB_FAILED":<12} | project={project_name} attempt={attempt} error="{safe}"')


def log_upload_exhausted(project_name: str) -> None:
    """Append a FB_EXHAUSTED line when all retry attempts are consumed."""
    _validate_token(project_name, "project_name")
    logger.error("FB_EXHAUSTED project=%s", project_name)
    _append(f"{_now()} | {'FB_EXHAUSTED':<12} | project={project_name}")


def log_token_expired(project_name: str) -> None:
    """Append a FB_TOKEN_EXP line when the Facebook Page token is invalid or expired."""
    _validate_token(project_name, "project_name")
    logger.error("FB_TOKEN_EXP project=%s", project_name)
    _append(f"{_now()} | {'FB_TOKEN_EXP':<12} | project={project_name}")
