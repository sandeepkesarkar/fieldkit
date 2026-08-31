"""
Tests for tools/instagram_logger.py — the Instagram upload activity log writer.

Covers all eight log functions, pipe-delimited format integrity, the
FIELDKIT_LOG_DIR import-time requirement, and that no PII or token values
appear in log output.

Modeled on tests/test_facebook_logger.py — instagram_logger.py writes into the
SAME per-client photo-agent.log, so its lines must obey the identical format
contract or the combined log becomes unparseable.
"""

import importlib

import pytest

import tools.instagram_logger as ig_logger
from tools.instagram_logger import (
    log_container_created,
    log_container_ready,
    log_token_expired,
    log_upload_attempt_failed,
    log_upload_enqueued,
    log_upload_exhausted,
    log_upload_published,
    log_upload_started,
)


@pytest.fixture(autouse=True)
def patch_log_paths(tmp_path, monkeypatch):
    """Redirect LOG_DIR and LOG_FILE to an isolated tmp directory for every test."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(ig_logger, "LOG_DIR", log_dir)
    monkeypatch.setattr(ig_logger, "LOG_FILE", log_dir / "photo-agent.log")


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """Freeze the log timestamp so format assertions are exact."""
    monkeypatch.setattr(ig_logger, "_now", lambda: "2026-08-31 14:00")


def _line() -> str:
    return ig_logger.LOG_FILE.read_text().strip()


# --- Format tests (one per log function) ---

def test_log_upload_enqueued_format():
    """IG_ENQUEUED line includes timestamp, padded event, and project name."""
    log_upload_enqueued("kitchen_remodel")
    assert _line() == "2026-08-31 14:00 | IG_ENQUEUED  | project=kitchen_remodel"


def test_log_upload_started_format():
    """IG_STARTED line includes the attempt number."""
    log_upload_started("kitchen_remodel", 2)
    assert _line() == "2026-08-31 14:00 | IG_STARTED   | project=kitchen_remodel attempt=2"


def test_log_container_created_format():
    """IG_CONT_NEW line includes the media container id."""
    log_container_created("kitchen_remodel", "17890000000000000")
    assert _line() == (
        "2026-08-31 14:00 | IG_CONT_NEW  | project=kitchen_remodel container_id=17890000000000000"
    )


def test_log_container_ready_format():
    """IG_CONT_RDY line marks the container as FINISHED and ready to publish."""
    log_container_ready("kitchen_remodel", "17890000000000000")
    assert _line() == (
        "2026-08-31 14:00 | IG_CONT_RDY  | project=kitchen_remodel container_id=17890000000000000"
    )


def test_log_upload_published_format():
    """IG_PUBLISHED line includes the published Instagram post id."""
    log_upload_published("kitchen_remodel", "18001234567890123")
    assert _line() == (
        "2026-08-31 14:00 | IG_PUBLISHED | project=kitchen_remodel post_id=18001234567890123"
    )


def test_log_upload_attempt_failed_format():
    """IG_FAILED line includes the attempt number and a quoted error string."""
    log_upload_attempt_failed("kitchen_remodel", 1, "container creation failed")
    assert _line() == (
        '2026-08-31 14:00 | IG_FAILED    | project=kitchen_remodel attempt=1 '
        'error="container creation failed"'
    )


def test_log_upload_exhausted_format():
    """IG_EXHAUSTED line records the terminal, retries-consumed outcome."""
    log_upload_exhausted("kitchen_remodel")
    assert _line() == "2026-08-31 14:00 | IG_EXHAUSTED | project=kitchen_remodel"


def test_log_token_expired_format():
    """IG_TOKEN_EXP line records an unrecoverable token failure."""
    log_token_expired("kitchen_remodel")
    assert _line() == "2026-08-31 14:00 | IG_TOKEN_EXP | project=kitchen_remodel"


# --- Pipe-delimited format integrity ---

def test_error_pipes_are_stripped():
    """A pipe in the error detail is replaced so the log stays parseable."""
    log_upload_attempt_failed("kitchen_remodel", 1, "bad | error | detail")
    line = _line()
    assert line.count("|") == 2  # only the two structural delimiters
    assert "bad   error   detail" in line


def test_error_newlines_are_stripped():
    """Newlines in the error detail never split one event across two log lines."""
    log_upload_attempt_failed("kitchen_remodel", 1, "line one\nline two\r\nline three")
    content = ig_logger.LOG_FILE.read_text()
    assert content.count("\n") == 1
    assert "line one line two" in content


def test_error_double_quotes_are_normalized():
    """Double quotes in the error detail can't unbalance the quoted error field."""
    log_upload_attempt_failed("kitchen_remodel", 1, 'said "boom"')
    assert _line().endswith("error=\"said 'boom'\"")


@pytest.mark.parametrize("bad_project", ["bad project", "bad|project", "bad/project", "bad.project"])
def test_invalid_project_name_raises(bad_project):
    """A project_name that would corrupt the delimited format is rejected."""
    with pytest.raises(ValueError, match="project_name"):
        log_upload_enqueued(bad_project)


@pytest.mark.parametrize("bad_container", ["abc def", "abc|def", "abc\ndef"])
def test_invalid_container_id_raises(bad_container):
    """A container_id that would corrupt the delimited format is rejected."""
    with pytest.raises(ValueError, match="container_id"):
        log_container_created("kitchen_remodel", bad_container)


def test_invalid_post_id_raises():
    """A post_id that would corrupt the delimited format is rejected."""
    with pytest.raises(ValueError, match="post_id"):
        log_upload_published("kitchen_remodel", "post|id")


def test_events_append_rather_than_overwrite():
    """Successive events accumulate in the shared photo-agent.log."""
    log_upload_enqueued("kitchen_remodel")
    log_upload_started("kitchen_remodel", 1)
    log_upload_published("kitchen_remodel", "18001234567890123")
    assert len(ig_logger.LOG_FILE.read_text().strip().splitlines()) == 3


def test_log_dir_created_on_demand():
    """The log directory is created if it does not already exist."""
    assert not ig_logger.LOG_DIR.exists()
    log_upload_enqueued("kitchen_remodel")
    assert ig_logger.LOG_FILE.exists()


def test_shares_photo_agent_log_filename():
    """Instagram events land in the same per-client photo-agent.log as every other event."""
    assert ig_logger.LOG_FILE.name == "photo-agent.log"


# --- No secrets / PII in output ---

def test_no_token_value_can_be_logged():
    """No log function accepts a token argument — tokens cannot reach the log at all."""
    import inspect
    for name, fn in vars(ig_logger).items():
        if name.startswith("log_") and callable(fn):
            params = inspect.signature(fn).parameters
            assert not any("token" in p.lower() for p in params), name


def test_error_detail_is_the_only_freeform_field():
    """A full upload lifecycle logs no email address, path, or URL."""
    log_upload_enqueued("kitchen_remodel")
    log_upload_started("kitchen_remodel", 1)
    log_container_created("kitchen_remodel", "17890000000000000")
    log_container_ready("kitchen_remodel", "17890000000000000")
    log_upload_published("kitchen_remodel", "18001234567890123")
    content = ig_logger.LOG_FILE.read_text()
    assert "@" not in content
    assert "http" not in content
    assert "/" not in content


# --- Env resolution ---

def test_log_file_path_derives_from_fieldkit_log_dir(monkeypatch, tmp_path):
    """LOG_FILE resolves under $FIELDKIT_LOG_DIR at import time."""
    log_dir = tmp_path / "clientlogs"
    log_dir.mkdir()
    monkeypatch.setenv("FIELDKIT_LOG_DIR", str(log_dir))
    reloaded = importlib.reload(ig_logger)
    try:
        assert reloaded.LOG_FILE == log_dir.resolve() / "photo-agent.log"
    finally:
        importlib.reload(ig_logger)


def test_import_without_fieldkit_log_dir_raises(monkeypatch):
    """Importing instagram_logger without FIELDKIT_LOG_DIR raises, like facebook_logger."""
    monkeypatch.delenv("FIELDKIT_LOG_DIR", raising=False)
    with pytest.raises(RuntimeError, match="FIELDKIT_LOG_DIR is not set"):
        importlib.reload(ig_logger)
    monkeypatch.undo()
    importlib.reload(ig_logger)
