"""
Activity log writer for Instagram video upload events (Feature 005).

Appends structured pipe-delimited lines to $FIELDKIT_LOG_DIR/photo-agent.log —
the SAME per-client file as logger.py and facebook_logger.py (FR-012 specifies
one log per client, not one per platform). Every line therefore has to obey the
identical `TIMESTAMP | EVENT<pad 12> | key=value ...` contract, or the combined
log stops being parseable. Event tags are all <= 12 characters so the padded
column stays aligned alongside the FB_* tags.

Events:
  IG_ENQUEUED  — an Instagram upload job was enqueued by check_approval.py
  IG_NOWORKER  — an enqueue was REFUSED because the upload_instagram cron is not
    running; Instagram is configured but not deployed (see tools/worker_health.py)
  IG_STARTED   — an upload attempt began
  IG_CONT_NEW  — a media container was created for this attempt
  IG_CONT_RDY  — the container finished processing and is ready to publish
  IG_PUBLISHED — the Reel was published
  IG_RECOVER   — a container was found ALREADY published by Instagram after an
    interrupted run; recorded without republishing (FR-011)
  IG_FAILED    — one attempt failed (retryable)
  IG_EXHAUSTED — all retry attempts consumed; terminal failure
  IG_TOKEN_EXP — the reused Facebook Page token is invalid/expired; retries skipped

project_name, container_id, and post_id must contain only alphanumerics,
underscores, or hyphens. PII is never written.

No function here takes a token argument — but that alone was never enough to keep
tokens out of this file, because log_upload_attempt_failed() persists an arbitrary
exception string, and a requests exception can render a URL with its query string.
_safe_error() therefore redacts credentials as well as protecting the delimiter
format. That is the LAST line of defence, not the first: tools/instagram_api.py
already keeps the token out of every URL it builds (see its module docstring). Both
exist because this log is durable and a leaked FB_PAGE_ACCESS_TOKEN never expires
on its own (issue #27).
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from tools.redaction import redact_secrets

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
    """Strip credentials from an error string, then make it safe for the log format.

    TWO separate jobs, in this order, and conflating them is how a token reaches
    disk. redact_secrets() protects the SECRET; the character replacements protect
    the pipe-delimited FORMAT. Redaction has to run first, on the untouched text,
    so a credential is matched in the exact shape it was written rather than after
    quotes and newlines have been rewritten around it.
    """
    return (
        redact_secrets(detail)
        .replace("|", " ")
        .replace('"', "'")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def log_upload_enqueued(project_name: str) -> None:
    """Append an IG_ENQUEUED line when an Instagram upload job is enqueued."""
    _validate_token(project_name, "project_name")
    logger.info("IG_ENQUEUED project=%s", project_name)
    _append(f"{_now()} | {'IG_ENQUEUED':<12} | project={project_name}")


def log_upload_started(project_name: str, attempt: int) -> None:
    """Append an IG_STARTED line when an upload attempt begins."""
    _validate_token(project_name, "project_name")
    logger.info("IG_STARTED project=%s attempt=%d", project_name, attempt)
    _append(f"{_now()} | {'IG_STARTED':<12} | project={project_name} attempt={attempt}")


def log_container_created(project_name: str, container_id: str) -> None:
    """Append an IG_CONT_NEW line when a media container is created for this attempt."""
    _validate_token(project_name, "project_name")
    _validate_token(container_id, "container_id")
    logger.info("IG_CONT_NEW project=%s container_id=%s", project_name, container_id)
    _append(
        f"{_now()} | {'IG_CONT_NEW':<12} | project={project_name} container_id={container_id}"
    )


def log_container_ready(project_name: str, container_id: str) -> None:
    """Append an IG_CONT_RDY line when the container finishes processing (status FINISHED)."""
    _validate_token(project_name, "project_name")
    _validate_token(container_id, "container_id")
    logger.info("IG_CONT_RDY project=%s container_id=%s", project_name, container_id)
    _append(
        f"{_now()} | {'IG_CONT_RDY':<12} | project={project_name} container_id={container_id}"
    )


def log_upload_published(project_name: str, post_id: str) -> None:
    """Append an IG_PUBLISHED line when the Reel is successfully published."""
    _validate_token(project_name, "project_name")
    _validate_token(post_id, "post_id")
    logger.info("IG_PUBLISHED project=%s post_id=%s", project_name, post_id)
    _append(f"{_now()} | {'IG_PUBLISHED':<12} | project={project_name} post_id={post_id}")


def log_enqueue_blocked(project_name: str) -> None:
    """Append an IG_NOWORKER line when an enqueue is refused for want of a running cron.

    Instagram is configured for this client but upload_instagram.py is not running,
    so a queued job would never be drained: the Reel would never publish, the shared
    local video would be retained indefinitely, and nothing would revoke a temporary
    public Drive link. Refusing the enqueue is the safe outcome; this records that it
    happened, so the gap is visible in the same log as everything else.
    """
    _validate_token(project_name, "project_name")
    logger.error("IG_NOWORKER project=%s", project_name)
    _append(f"{_now()} | {'IG_NOWORKER':<12} | project={project_name}")


def log_upload_recovered(project_name: str, container_id: str) -> None:
    """Append an IG_RECOVER line when an interrupted publish is found already live.

    Distinct from IG_PUBLISHED because the two are operationally different: this one
    means FieldKit did not observe the publish happening and cannot name the media it
    produced, only that Instagram reports the container as PUBLISHED. It carries the
    container id rather than a post id for exactly that reason.
    """
    _validate_token(project_name, "project_name")
    _validate_token(container_id, "container_id")
    logger.warning("IG_RECOVER project=%s container_id=%s", project_name, container_id)
    _append(
        f"{_now()} | {'IG_RECOVER':<12} | project={project_name} container_id={container_id}"
    )


def log_upload_attempt_failed(project_name: str, attempt: int, error: str) -> None:
    """Append an IG_FAILED line when an upload attempt fails (retryable)."""
    _validate_token(project_name, "project_name")
    safe = _safe_error(error)
    logger.error("IG_FAILED project=%s attempt=%d", project_name, attempt)
    _append(f'{_now()} | {"IG_FAILED":<12} | project={project_name} attempt={attempt} error="{safe}"')


def log_upload_exhausted(project_name: str) -> None:
    """Append an IG_EXHAUSTED line when all retry attempts are consumed."""
    _validate_token(project_name, "project_name")
    logger.error("IG_EXHAUSTED project=%s", project_name)
    _append(f"{_now()} | {'IG_EXHAUSTED':<12} | project={project_name}")


def log_token_expired(project_name: str) -> None:
    """Append an IG_TOKEN_EXP line when the Facebook Page token is invalid or expired."""
    _validate_token(project_name, "project_name")
    logger.error("IG_TOKEN_EXP project=%s", project_name)
    _append(f"{_now()} | {'IG_TOKEN_EXP':<12} | project={project_name}")
