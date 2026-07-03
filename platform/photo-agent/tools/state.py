"""
State manager for the photo-video agent.

Manages:
  <repo-root>/data/photo-agent/state.json  — pending approval record + Telegram update offset

All read-modify-write operations acquire an exclusive file lock (fcntl.LOCK_EX) before
reading and release it after writing. This prevents corruption when process_photos.py
and check_approval.py overlap.

Sensitive fields (chat IDs, bot tokens) are never written to log output.
"""

import fcntl
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_data_dir_raw = os.environ.get("FIELDKIT_DATA_DIR", "")
if not _data_dir_raw:
    raise RuntimeError("FIELDKIT_DATA_DIR is not set — add it to your client .env file")
DATA_DIR = Path(_data_dir_raw) / "photo-agent"
STATE_FILE = DATA_DIR / "state.json"

__all__ = [
    "get_pending_approval",
    "set_pending_approval",
    "clear_pending_approval",
    "get_telegram_offset",
    "set_telegram_offset",
]

_REQUIRED_APPROVAL_KEYS = frozenset({
    "project_name",
    "drive_folder_id",
    "drive_video_file_id",
    "drive_folder_link",
    "video_local_path",
    "telegram_message_id",
    "triggered_at",
})

_DEFAULTS = {"telegram_update_offset": 0, "pending_approval": None}


def _read(file_obj) -> dict:
    """Read and parse state.json from an open, locked file object."""
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        logger.debug("state.json is empty — using defaults")
        return dict(_DEFAULTS)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("state.json is corrupt and cannot be parsed: %s", exc)
        raise RuntimeError("state.json is corrupt — delete or restore it manually") from exc


def _write(file_obj, data: dict) -> None:
    """Overwrite state.json via an open, locked file object.

    Writes before truncating so a crash mid-write leaves new content intact
    rather than an empty file (truncate-then-write would zero the file first).
    """
    content = json.dumps(data, indent=2)
    file_obj.seek(0)
    file_obj.write(content)
    file_obj.truncate()  # remove any trailing bytes from a previously longer file
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _open_for_write():
    """Open state.json for read+write, creating it if absent, without O_APPEND.

    O_APPEND forces writes to end-of-file regardless of seek position, which
    makes the seek/truncate/write sequence in _write fragile if truncate is
    partial. Using O_RDWR | O_CREAT gives full cursor control.
    """
    fd_no = os.open(STATE_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd_no, "r+")


def get_pending_approval() -> dict | None:
    """Return the pending approval record, or None if absent or null."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                record = data.get("pending_approval")
                logger.debug("get_pending_approval: %s", "present" if record is not None else "null")
                return record
            finally:
                logger.debug("Releasing lock on state.json")
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        logger.debug("state.json missing — no pending approval")
        return None


def set_pending_approval(record: dict) -> None:
    """Write the pending approval record. Raises ValueError if required keys are missing."""
    missing = _REQUIRED_APPROVAL_KEYS - set(record.keys())
    if missing:
        raise ValueError(f"set_pending_approval: missing required keys: {missing}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        logger.debug("Acquiring exclusive lock on state.json for set_pending_approval")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            data["pending_approval"] = record
            _write(f, data)
            logger.info("set_pending_approval: project_name=%s", record.get("project_name"))
        finally:
            logger.debug("Releasing lock on state.json")
            fcntl.flock(f, fcntl.LOCK_UN)


def clear_pending_approval() -> None:
    """Set pending_approval to null in state.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        logger.debug("Acquiring exclusive lock on state.json for clear_pending_approval")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            data["pending_approval"] = None
            _write(f, data)
            logger.info("clear_pending_approval: pending_approval cleared")
        finally:
            logger.debug("Releasing lock on state.json")
            fcntl.flock(f, fcntl.LOCK_UN)


def get_telegram_offset() -> int:
    """Return the stored Telegram update offset. Returns 0 if state.json is missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
                offset = data.get("telegram_update_offset", 0)
                logger.debug("get_telegram_offset=%d", offset)
                return offset
            finally:
                logger.debug("Releasing lock on state.json")
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        logger.debug("state.json missing — telegram_update_offset=0")
        return 0


def set_telegram_offset(offset: int) -> None:
    """Write the Telegram update offset without touching pending_approval."""
    if not isinstance(offset, int):
        raise TypeError(f"set_telegram_offset: offset must be int, got {type(offset).__name__}")
    if offset < 0:
        raise ValueError(f"set_telegram_offset: offset must be >= 0, got {offset}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _open_for_write() as f:
        logger.debug("Acquiring exclusive lock on state.json for set_telegram_offset")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = _read(f)
            data["telegram_update_offset"] = offset
            _write(f, data)
            logger.info("set_telegram_offset=%d", offset)
        finally:
            logger.debug("Releasing lock on state.json")
            fcntl.flock(f, fcntl.LOCK_UN)
