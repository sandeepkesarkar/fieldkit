"""
Tests for tools/logger.py — the email agent's activity log writer.

Each public function (log_received, log_rejected, log_stale_alert, log_cycle)
appends one structured line to ~/fieldkit/logs/email-agent.log. These tests verify
the exact format of those lines, field-level constraints from the spec, and file
I/O behaviour (append-not-overwrite, directory auto-creation).
"""

from datetime import datetime, timezone

import pytest

import tools.logger as logger_mod
from tools.logger import log_cycle, log_received, log_rejected, log_stale_alert


@pytest.fixture(autouse=True)
def patch_log_paths(tmp_path, monkeypatch):
    """
    Redirect LOG_DIR and LOG_FILE to an isolated tmp directory for every test.

    LOG_DIR is intentionally NOT pre-created here — tests that verify directory
    auto-creation (test_creates_log_directory) depend on it being absent at the
    start of the test. The _append() helper creates it on first write.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logger_mod, "LOG_DIR", log_dir)
    monkeypatch.setattr(logger_mod, "LOG_FILE", log_dir / "email-agent.log")


# --- Format tests ---
# Each test freezes _now() to a fixed string so the assertion can be an exact
# match rather than a regex. Column widths matter: event names are left-padded
# to 12 chars, so any change to that constant will break these tests immediately.

def test_log_received_format(monkeypatch):
    """
    RECEIVED line includes timestamp, event, and all four payload fields.

    Verifies the full line layout including column spacing, field ordering, and
    that the subject value is wrapped in double quotes (not repr-style singles).
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:32")
    log_received("admin@example.com", "Job #42", 3, "#0014")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == '2026-05-09 14:32 | RECEIVED     | from=admin@example.com subject="Job #42" attachments=3 ref=#0014'


def test_log_rejected_format(monkeypatch):
    """
    REJECTED line includes only from and subject — no attachments, no ref_id.

    Rejected messages are dropped before a ref_id is assigned, so those fields
    must not appear. The subject is still double-quoted for log readability.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:33")
    log_rejected("unknown@example.com", "Offer")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == '2026-05-09 14:33 | REJECTED     | from=unknown@example.com subject="Offer"'


def test_log_stale_alert_format(monkeypatch):
    """
    STALE_ALERT line includes count and a comma-separated refs list, no from/subject.

    STALE_ALERT is the longest event name (11 chars) — tightest fit in the 12-char
    column. Verifies the padding still aligns correctly at the limit.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:33")
    log_stale_alert(2, ["#0012", "#0013"])
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-09 14:33 | STALE_ALERT  | count=2 refs=#0012,#0013"


def test_log_cycle_format(monkeypatch):
    """
    CYCLE line includes only processed and rejected integer totals.

    Cycle lines are written at the end of each agent run. The values are raw
    integers — no quotes, no type decorators.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:35")
    log_cycle(1, 1)
    line = logger_mod.LOG_FILE.read_text().strip()
    assert line == "2026-05-09 14:35 | CYCLE        | processed=1 rejected=1"


# --- Field-presence tests ---

def test_log_received_no_channel_field(monkeypatch):
    """
    RECEIVED lines must not include a 'channel=' field.

    The spec explicitly excludes it from this event type. This test guards against
    a future refactor (e.g. adding a Telegram channel ID) silently breaking the
    format contract.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:32")
    log_received("admin@example.com", "Job #42", 3, "#0014")
    line = logger_mod.LOG_FILE.read_text().strip()
    assert "channel=" not in line


def test_log_stale_alert_formats_refs_as_comma_separated(monkeypatch):
    """
    STALE_ALERT ref_ids are joined with commas and no spaces.

    Uses 3 refs so the join behaviour is unambiguous — a list of 1 would pass
    even without a join call. No trailing comma, no brackets.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:33")
    log_stale_alert(3, ["#0001", "#0002", "#0003"])
    line = logger_mod.LOG_FILE.read_text().strip()
    assert "refs=#0001,#0002,#0003" in line


# --- File behaviour tests ---

def test_appends_to_existing_file(monkeypatch):
    """
    Successive calls append new lines rather than overwriting the file.

    Writes two CYCLE lines and verifies both survive. An open("w") bug would
    leave only the second line, causing this test to fail with len(lines) == 1.
    """
    monkeypatch.setattr(logger_mod, "_now", lambda: "2026-05-09 14:35")
    log_cycle(1, 0)
    log_cycle(2, 1)
    lines = logger_mod.LOG_FILE.read_text().strip().split("\n")
    assert len(lines) == 2


def test_creates_log_directory():
    """
    The log directory is auto-created on first write if it does not exist.

    The autouse fixture deliberately leaves LOG_DIR absent so this test can
    confirm _append() calls mkdir(). Removing the mkdir() call from the
    implementation would raise FileNotFoundError here.
    """
    assert not logger_mod.LOG_DIR.exists()
    log_cycle(0, 0)
    assert logger_mod.LOG_FILE.exists()


def test_timestamp_format():
    """
    The real _now() produces a timestamp in 'YYYY-MM-DD HH:MM' format.

    Does NOT monkeypatch _now — exercises the actual datetime call and parses
    the result with strptime. The format must be exactly HH:MM (minutes only,
    no seconds), as specified. strptime raises ValueError on any mismatch.
    """
    log_cycle(0, 0)
    line = logger_mod.LOG_FILE.read_text().strip()
    ts = line.split(" | ")[0]
    datetime.strptime(ts, "%Y-%m-%d %H:%M")
