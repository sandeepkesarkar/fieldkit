"""
Tests for tools/logger.py — the photo-video agent's activity log writer.

Each public function appends one structured line to logs/photo-agent.log.
Tests verify the exact format of each line, field-level constraints, and
file I/O behaviour (append-not-overwrite, directory auto-creation).
"""

from datetime import datetime, timezone

import pytest

import tools.logger as logger_mod
from tools.logger import (
    log_approval_req,
    log_approved,
    log_command,
    log_downloaded,
    log_error,
    log_generated,
    log_rejected,
    log_uploaded,
)


@pytest.fixture(autouse=True)
def patch_log_paths(tmp_path, monkeypatch):
    """
    Redirect LOG_DIR and LOG_FILE to an isolated tmp directory for every test.

    LOG_DIR is intentionally NOT pre-created — tests that verify directory
    auto-creation depend on it being absent at the start of the test.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logger_mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(logger_mod, "LOG_FILE", log_dir / "photo-agent.log")


# --- Format tests ---
# Each test freezes _now() to a fixed string so the assertion is an exact match.

def test_log_command_format(monkeypatch):
    """COMMAND line includes timestamp, event padded to 12 chars, and project name."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:00")
    log_command("kitchen_remodel")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:00 | COMMAND      | project=kitchen_remodel"


def test_log_downloaded_format(monkeypatch):
    """DOWNLOADED line includes project name and photo count."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:00")
    log_downloaded("kitchen_remodel", 6)
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:00 | DOWNLOADED   | project=kitchen_remodel count=6"


def test_log_generated_format(monkeypatch):
    """GENERATED line uses :g format — duration_sec=22.0 renders as 22, not 22.0."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:01")
    log_generated("kitchen_remodel", 22.0, 9437184)
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:01 | GENERATED    | project=kitchen_remodel duration_sec=22 size_bytes=9437184"


def test_log_generated_fractional_duration(monkeypatch):
    """GENERATED line preserves fractional duration_sec when non-whole (e.g. 11.5)."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:01")
    log_generated("kitchen_remodel", 11.5, 5000000)
    line = logger_mod.LOG_FILE.read_text().strip()
    assert "duration_sec=11.5" in line


def test_log_uploaded_format(monkeypatch):
    """UPLOADED line includes project name and Drive file ID."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:01")
    log_uploaded("kitchen_remodel", "1xyz")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:01 | UPLOADED     | project=kitchen_remodel drive_file_id=1xyz"


def test_log_approval_req_format(monkeypatch):
    """APPROVAL_REQ line includes project name and Telegram message ID."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:01")
    log_approval_req("kitchen_remodel", 42)
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:01 | APPROVAL_REQ | project=kitchen_remodel message_id=42"


def test_log_approved_format(monkeypatch):
    """APPROVED line includes only project name — no other fields."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:03")
    log_approved("kitchen_remodel")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:03 | APPROVED     | project=kitchen_remodel"


def test_log_rejected_format(monkeypatch):
    """REJECTED line includes only project name — no other fields."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:03")
    log_rejected("kitchen_remodel")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-16 10:03 | REJECTED     | project=kitchen_remodel"


def test_log_error_format(monkeypatch):
    """ERROR line includes project name, phase, and detail wrapped in double quotes."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:02")
    log_error("kitchen_remodel", "generate", "ffmpeg exited 1: error")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == '2026-05-16 10:02 | ERROR        | project=kitchen_remodel phase=generate detail="ffmpeg exited 1: error"'


def test_log_error_includes_phase_and_detail(monkeypatch):
    """ERROR line includes both phase and detail fields."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:02")
    log_error("kitchen_remodel", "upload", "timeout after 30s")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert "phase=upload" in line
    assert 'detail="timeout after 30s"' in line


# --- Sanitization tests ---

def test_log_error_sanitizes_double_quotes(monkeypatch):
    """log_error replaces double quotes in detail with single quotes."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:02")
    log_error("p", "gen", 'bad "quoted" detail')
    line = logger_mod.LOG_FILE.read_text().strip()
    assert "detail=\"bad 'quoted' detail\"" in line


def test_log_error_sanitizes_newlines(monkeypatch):
    """log_error replaces newlines in detail so the entry stays a single log line."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:02")
    log_error("p", "gen", "line1\nline2")
    lines = logger_mod.LOG_FILE.read_text().strip().splitlines()
    assert len(lines) == 1
    assert "line1 line2" in lines[0]


# --- Validation tests ---

def test_log_command_rejects_project_name_with_space():
    """log_command raises ValueError when project_name contains a space."""
    with pytest.raises(ValueError, match="project_name"):
        log_command("my project")


def test_log_error_rejects_phase_with_space():
    """log_error raises ValueError when phase contains a space."""
    with pytest.raises(ValueError, match="phase"):
        log_error("valid_project", "generate video", "detail")


def test_log_command_rejects_project_name_with_pipe():
    """log_command raises ValueError when project_name contains a pipe character."""
    with pytest.raises(ValueError, match="project_name"):
        log_command("my|project")


# --- File behaviour tests ---

def test_appends_to_existing_file(monkeypatch):
    """Successive calls append new lines rather than overwriting the file."""
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-16 10:00")
    log_command("project_a")
    log_command("project_b")
    lines = logger_mod.LOG_FILE.read_text().strip().split("\n")
    assert len(lines) == 2


def test_creates_log_directory():
    """The log directory is auto-created on first write if it does not exist."""
    assert not logger_mod.LOG_DIR.exists()
    log_command("any_project")
    assert logger_mod.LOG_FILE.exists()


def test_timestamp_format():
    """The real _now() produces a UTC timestamp in 'YYYY-MM-DD HH:MM' format (no seconds)."""
    log_command("any_project")
    line = logger_mod.LOG_FILE.read_text().strip()
    ts = line.split(" | ")[0]
    parsed = datetime.strptime(ts, "%Y-%m-%d %H:%M")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc - parsed).total_seconds()) < 120, f"Timestamp {ts!r} is not within 2 min of UTC"
