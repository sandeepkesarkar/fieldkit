"""
State manager for the Facebook video upload pipeline.

Manages:
  $FIELDKIT_DATA_DIR/photo-agent/facebook_state.json
    — pending VideoUploadJob record + published idempotency keys

All read-modify-write operations acquire an exclusive file lock (fcntl.LOCK_EX)
before reading and release it after writing, mirroring the pattern in state.py.

FB_APP_SECRET is never stored here. Sensitive token values are never logged.
"""

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_data_dir_raw = os.environ.get("FIELDKIT_DATA_DIR", "")
if not _data_dir_raw:
    raise RuntimeError("FIELDKIT_DATA_DIR is not set — add it to your client .env file")
DATA_DIR = Path(_data_dir_raw) / "photo-agent"
STATE_FILE = DATA_DIR / "facebook_state.json"

__all__ = [
    "get_pending_upload",
    "set_pending_upload",
    "mark_uploading",
    "mark_published",
    "mark_failed",
    "increment_attempt",
    "is_published",
]

_REQUIRED_UPLOAD_KEYS = frozenset({
    "project_name",
    "video_local_path",
    "page_id",
    "status",
    "attempt_count",
    "last_attempt_at",
    "triggered_at",
    "idempotency_key",
    "fb_post_id",
})

_DEFAULTS = {"pending_facebook_upload": None, "published_idempotency_keys": []}


def _read(file_obj) -> dict:
    """Read and parse facebook_state.json from an open, locked file object."""
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        return dict(_DEFAULTS)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("facebook_state.json is corrupt: %s", exc)
        raise RuntimeError("facebook_state.json is corrupt — delete or restore it manually") from exc


def _write(file_obj, data: dict) -> None:
    """Overwrite facebook_state.json via an open, locked file object."""
    content = json.dumps(data, indent=2)
    file_obj.seek(0)
    file_obj.write(content)
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _open_for_write():
    """Open facebook_state.json for read+write, creating it if absent."""
    fd_no = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd_no, "r+")


def get_pending_upload() -> dict | None:
    """Return the pending VideoUploadJob record, or None if absent or null."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                return data.get("pending_facebook_upload")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return None


def set_pending_upload(record: dict) -> None:
    """Write the pending VideoUploadJob. Raises ValueError on missing keys or duplicate idempotency_key."""
    missing = _REQUIRED_UPLOAD_KEYS - set(record.keys())
    if missing:
        raise ValueError(f"set_pending_upload: missing required keys: {missing}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            key = record["idempotency_key"]
            if key in data.get("published_idempotency_keys", []):
                raise ValueError(
                    f"set_pending_upload: idempotency_key {key!r} already in published_idempotency_keys"
                )
            data["pending_facebook_upload"] = record
            _write(f, data)
            logger.info("set_pending_upload: project=%s key=%s", record.get("project_name"), key)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _update_pending(idempotency_key: str, updater) -> None:
    """Read, apply updater(record, data), then write — under exclusive lock."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            record = data.get("pending_facebook_upload")
            if record is not None:
                updater(record, data)
            _write(f, data)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def mark_uploading(idempotency_key: str) -> None:
    """Transition the pending record's status to 'uploading'."""
    def _update(record, data):
        record["status"] = "uploading"
    _update_pending(idempotency_key, _update)
    logger.info("mark_uploading: key=%s", idempotency_key)


def mark_published(idempotency_key: str, post_id: str) -> None:
    """Set status=published, store post_id, and append the key to published_idempotency_keys."""
    def _update(record, data):
        record["status"] = "published"
        record["fb_post_id"] = post_id
        keys = data.setdefault("published_idempotency_keys", [])
        if idempotency_key not in keys:
            keys.append(idempotency_key)
    _update_pending(idempotency_key, _update)
    logger.info("mark_published: key=%s post_id=%s", idempotency_key, post_id)


def mark_failed(idempotency_key: str) -> None:
    """Transition the pending record's status to 'failed'."""
    def _update(record, data):
        record["status"] = "failed"
    _update_pending(idempotency_key, _update)
    logger.error("mark_failed: key=%s", idempotency_key)


def increment_attempt(idempotency_key: str) -> None:
    """Increment attempt_count by 1 and set last_attempt_at to now (UTC ISO-8601)."""
    now = datetime.now(timezone.utc).isoformat()

    def _update(record, data):
        record["attempt_count"] = record.get("attempt_count", 0) + 1
        record["last_attempt_at"] = now
    _update_pending(idempotency_key, _update)
    logger.info("increment_attempt: key=%s", idempotency_key)


def is_published(idempotency_key: str) -> bool:
    """Return True if idempotency_key is in published_idempotency_keys."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                return idempotency_key in data.get("published_idempotency_keys", [])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
