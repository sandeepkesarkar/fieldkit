"""
Tests for tools/facebook_logger.py — the Facebook upload activity log writer.

Covers all six log functions, pipe-delimited format integrity, FIELDKIT_LOG_DIR
env override, and that no PII or token values appear in log output.
"""

import pytest

import tools.facebook_logger as fb_logger
from tools.facebook_logger import (
    log_upload_enqueued,
    log_upload_started,
    log_upload_published,
    log_upload_attempt_failed,
    log_upload_exhausted,
    log_token_expired,
)


@pytest.fixture(autouse=True)
def patch_log_paths(tmp_path, monkeypatch):
    """Redirect LOG_DIR and LOG_FILE to an isolated tmp directory for every test."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(fb_logger, "LOG_DIR", log_dir)
    monkeypatch.setattr(fb_logger, "LOG_FILE", log_dir / "photo-agent.log")


# --- Format tests ---

def test_log_upload_enqueued_format(monkeypatch):
    """FB_ENQUEUED line includes timestamp, padded event, and project name."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_enqueued("kitchen_remodel")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == "2026-05-30 14:00 | FB_ENQUEUED  | project=kitchen_remodel"


def test_log_upload_started_format(monkeypatch):
    """FB_STARTED line includes timestamp, padded event, project name, and attempt number."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:01")
    log_upload_started("kitchen_remodel", 1)
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == "2026-05-30 14:01 | FB_STARTED   | project=kitchen_remodel attempt=1"


def test_log_upload_published_format(monkeypatch):
    """FB_PUBLISHED line includes timestamp, padded event, project name, and post_id."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:02")
    log_upload_published("kitchen_remodel", "12345678901234")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == "2026-05-30 14:02 | FB_PUBLISHED | project=kitchen_remodel post_id=12345678901234"


def test_log_upload_attempt_failed_format(monkeypatch):
    """FB_FAILED line includes timestamp, padded event, project, attempt, and error."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:03")
    log_upload_attempt_failed("kitchen_remodel", 1, "connection timeout")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == '2026-05-30 14:03 | FB_FAILED    | project=kitchen_remodel attempt=1 error="connection timeout"'


def test_log_upload_exhausted_format(monkeypatch):
    """FB_EXHAUSTED line includes timestamp, padded event, and project name."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:04")
    log_upload_exhausted("kitchen_remodel")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == "2026-05-30 14:04 | FB_EXHAUSTED | project=kitchen_remodel"


def test_log_token_expired_format(monkeypatch):
    """FB_TOKEN_EXP line includes timestamp, padded event, and project name."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:05")
    log_token_expired("kitchen_remodel")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line == "2026-05-30 14:05 | FB_TOKEN_EXP | project=kitchen_remodel"


# --- Pipe-delimited format integrity ---

def test_no_pipe_chars_in_project_name_field(monkeypatch):
    """project_name containing a pipe character raises ValueError."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    with pytest.raises(ValueError):
        log_upload_enqueued("bad|project")


def test_no_pipe_chars_in_project_name_for_all_functions(monkeypatch):
    """All six functions reject project_name values containing pipe characters."""
    bad = "bad|name"
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    with pytest.raises(ValueError):
        log_upload_enqueued(bad)
    with pytest.raises(ValueError):
        log_upload_started(bad, 1)
    with pytest.raises(ValueError):
        log_upload_published(bad, "post123")
    with pytest.raises(ValueError):
        log_upload_attempt_failed(bad, 1, "err")
    with pytest.raises(ValueError):
        log_upload_exhausted(bad)
    with pytest.raises(ValueError):
        log_token_expired(bad)


def test_log_lines_have_exactly_two_pipes(monkeypatch):
    """Each log line contains exactly two pipe separators."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_enqueued("proj")
    log_upload_started("proj", 1)
    log_upload_published("proj", "post1")
    log_upload_exhausted("proj")
    log_token_expired("proj")
    lines = fb_logger.LOG_FILE.read_text().strip().splitlines()
    for line in lines:
        assert line.count("|") == 2, f"Expected 2 pipes in: {line!r}"


def test_attempt_failed_error_pipe_sanitized(monkeypatch):
    """Pipe characters in the error field of log_upload_attempt_failed are sanitized."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_attempt_failed("proj", 1, "error|with|pipes")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert line.count("|") == 2


# --- Append behaviour ---

def test_log_appends_not_overwrites(monkeypatch):
    """Calling a log function twice appends a second line rather than overwriting."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_enqueued("proj")
    log_upload_enqueued("proj")
    lines = fb_logger.LOG_FILE.read_text().strip().splitlines()
    assert len(lines) == 2


# --- Directory auto-creation ---

def test_log_dir_created_on_first_write(monkeypatch):
    """The log directory is created automatically on first write."""
    assert not fb_logger.LOG_DIR.exists()
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_enqueued("proj")
    assert fb_logger.LOG_DIR.exists()


# --- No PII or token values in log output ---

def test_no_token_in_log_upload_published(monkeypatch):
    """log_upload_published only logs project_name and post_id — not a page access token."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_published("proj", "987654321")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert "EAAabc" not in line  # token prefix must not appear
    assert "access_token" not in line


def test_attempt_failed_sanitizes_newlines(monkeypatch):
    """Newlines in the error field are replaced to keep the log single-line."""
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_attempt_failed("proj", 1, "line1\nline2\r\nline3")
    line = fb_logger.LOG_FILE.read_text().strip()
    assert "\n" not in line
    assert "\r" not in line


# --- FIELDKIT_LOG_DIR env override ---

def test_log_dir_env_override(tmp_path, monkeypatch):
    """LOG_DIR can be overridden via monkeypatch (simulating FIELDKIT_LOG_DIR at import time)."""
    alt_dir = tmp_path / "custom_logs"
    alt_file = alt_dir / "photo-agent.log"
    monkeypatch.setattr(fb_logger, "LOG_DIR", alt_dir)
    monkeypatch.setattr(fb_logger, "LOG_FILE", alt_file)
    monkeypatch.setattr(fb_logger, "_now", lambda: "2026-05-30 14:00")
    log_upload_enqueued("proj")
    assert alt_file.exists()
    assert "FB_ENQUEUED" in alt_file.read_text()
