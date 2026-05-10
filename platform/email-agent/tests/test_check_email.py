"""
Tests for scripts/check_email.py — the email intake main script.

Covers three groups:
  1. Pure helper functions (_extract_header, _parse_from_addr, _count_attachments)
     — no mocking required, inputs and outputs are deterministic.
  2. Environment loading (_load_env) — file I/O redirected via tmp_path.
  3. main() integration — one test per invocation path. All external calls
     (_gws, _telegram, subprocess.run, state functions, logger functions) are
     stubbed via monkeypatch so tests run without Gmail, Telegram, or gws.

The base_env autouse fixture handles the three concerns shared by every
integration test: redirecting the lockfile, suppressing real .env loading,
and providing the three required env vars.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import scripts.check_email as ce


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_LABEL_ID = "Label_fk123"

_FULL_MSG_ALLOWED = {
    "id": "msg001",
    "payload": {
        "headers": [
            {"name": "From", "value": "Admin User <admin@example.com>"},
            {"name": "Subject", "value": "Job #1"},
            {"name": "Date", "value": "Sun, 10 May 2026 10:00:00 +0000"},
        ],
        "parts": [],
    },
}

_FULL_MSG_REJECTED = {
    "id": "msg002",
    "payload": {
        "headers": [
            {"name": "From", "value": "spam@evil.com"},
            {"name": "Subject", "value": "Buy now"},
            {"name": "Date", "value": "Sun, 10 May 2026 11:00:00 +0000"},
        ],
        "parts": [],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def base_env(monkeypatch, tmp_path):
    """
    Set required env vars and redirect lockfile/.env for every test.

    Prevents tests from reading the real .env file or acquiring a lock on
    the production data directory. All three required env vars are set so
    tests that are not testing missing-var behaviour work without extra setup.

    _acquire_run_lock is patched to return a MagicMock (truthy, has .close())
    so main() always acquires the lock without touching the filesystem.
    fcntl.flock is patched to a no-op so the finally block doesn't fail when
    called with a MagicMock file descriptor.
    """
    monkeypatch.setenv("AGENT_EMAIL", "agent@example.com")
    monkeypatch.setenv("ADMIN_ALLOWLIST", "admin@example.com")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(ce, "_LOCK_FILE", tmp_path / "run.lock")
    monkeypatch.setattr(ce, "_ENV_FILE", tmp_path / "nonexistent.env")
    monkeypatch.setattr(ce, "_acquire_run_lock", lambda: MagicMock())
    monkeypatch.setattr(ce.fcntl, "flock", MagicMock())
    monkeypatch.setattr(sys, "argv", ["check_email.py"])


@pytest.fixture
def stub_state(monkeypatch):
    """
    Stub all state functions to no-ops / sensible defaults.

    label cached, no stale entries, ref ID always #0001. Tests that care
    about specific state behaviour override individual attributes after
    requesting this fixture.
    """
    monkeypatch.setattr(ce, "get_label_id", lambda: _LABEL_ID)
    monkeypatch.setattr(ce, "save_label_id", MagicMock())
    monkeypatch.setattr(ce, "get_stale_pending", lambda **_: [])
    monkeypatch.setattr(ce, "get_ref_id_for_message", lambda _: "#0001")
    monkeypatch.setattr(ce, "enqueue_pending", MagicMock())
    monkeypatch.setattr(ce, "dequeue_pending", MagicMock())


@pytest.fixture
def stub_logger(monkeypatch):
    """Stub all logger functions to no-ops so tests can assert on them selectively."""
    monkeypatch.setattr(ce, "log_received", MagicMock())
    monkeypatch.setattr(ce, "log_rejected", MagicMock())
    monkeypatch.setattr(ce, "log_stale_alert", MagicMock())
    monkeypatch.setattr(ce, "log_cycle", MagicMock())


# ---------------------------------------------------------------------------
# 1 — Pure helper function tests
# ---------------------------------------------------------------------------

def test_extract_header_returns_matching_value():
    """
    _extract_header returns the value of the first header whose name matches.

    The From header is the most critical — it drives allowlist enforcement.
    Verifies the basic case before testing case-insensitivity and the missing case.
    """
    headers = [
        {"name": "From", "value": "admin@example.com"},
        {"name": "Subject", "value": "Hello"},
    ]
    assert ce._extract_header(headers, "From") == "admin@example.com"


def test_extract_header_is_case_insensitive():
    """
    _extract_header matches header names regardless of capitalisation.

    Gmail returns headers with mixed capitalisation (e.g. 'MIME-Version',
    'Content-Type'). A case-sensitive check would silently miss headers
    whose capitalisation differs from the spec's expectation.
    """
    headers = [{"name": "subject", "value": "lowercase"}]
    assert ce._extract_header(headers, "Subject") == "lowercase"


def test_extract_header_returns_empty_string_when_absent():
    """
    _extract_header returns '' for a missing header, not None or an exception.

    The caller passes the result to _parse_from_addr or uses it as a string
    directly. An empty string is safe in both cases; None would cause a
    TypeError in string operations.
    """
    assert ce._extract_header([], "From") == ""


def test_parse_from_addr_strips_display_name():
    """
    _parse_from_addr extracts the bare address from 'Display Name <addr>' format.

    Most business emails include a display name. Allowlist matching is done on
    the bare address so the display name must be stripped before comparison.
    """
    assert ce._parse_from_addr("Admin User <admin@example.com>") == "admin@example.com"


def test_parse_from_addr_handles_bare_address():
    """
    _parse_from_addr returns the address unchanged when no display name is present.

    Some automated senders omit the display name. The function must handle both
    formats to avoid false rejections on well-formed bare addresses.
    """
    assert ce._parse_from_addr("admin@example.com") == "admin@example.com"


def test_parse_from_addr_lowercases_result():
    """
    _parse_from_addr lowercases the extracted address.

    Allowlist entries are stored lowercase. A sender using 'Admin@Example.COM'
    must still match 'admin@example.com' in the allowlist — without lowercasing
    it would be rejected silently.
    """
    assert ce._parse_from_addr("Admin@Example.COM") == "admin@example.com"


def test_parse_from_addr_returns_unknown_for_malformed_header():
    """
    _parse_from_addr returns 'unknown' when the header value cannot be parsed.

    A malformed From header (empty string, garbage value) must not crash the
    script. 'unknown' will never match the allowlist, so the message is rejected
    and logged — the correct safe-fail behaviour.
    """
    assert ce._parse_from_addr("") == "unknown"


def test_count_attachments_returns_zero_for_plain_text():
    """
    _count_attachments returns 0 when the message has no parts with a filename.

    Plain-text emails have no 'parts' key in the payload. The function must
    return 0 rather than crashing on a missing key.
    """
    assert ce._count_attachments({"headers": []}) == 0


def test_count_attachments_counts_nested_parts():
    """
    _count_attachments recurses into nested parts to count all attachments.

    Multipart messages nest parts inside parts (e.g. multipart/mixed wrapping
    multipart/alternative and an attachment). A non-recursive count would miss
    attachments that are not at the top level of the payload.
    """
    payload = {
        "parts": [
            {"filename": "photo.jpg", "parts": []},
            {
                "filename": "",
                "parts": [
                    {"filename": "doc.pdf", "parts": []},
                ],
            },
        ]
    }
    assert ce._count_attachments(payload) == 2


# ---------------------------------------------------------------------------
# 2 — _load_env tests
# ---------------------------------------------------------------------------

def test_load_env_reads_file_into_environ(tmp_path, monkeypatch):
    """
    _load_env sets variables from a .env file that are not already in os.environ.

    Uses a key that is deliberately absent from os.environ to ensure the value
    comes from the file, not a pre-existing environment variable.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("FIELDKIT_TEST_VAR=hello\n")
    monkeypatch.setattr(ce, "_ENV_FILE", env_file)
    monkeypatch.delenv("FIELDKIT_TEST_VAR", raising=False)

    ce._load_env()

    assert os.environ.get("FIELDKIT_TEST_VAR") == "hello"


def test_load_env_does_not_overwrite_existing_env_var(tmp_path, monkeypatch):
    """
    _load_env skips variables that are already set in os.environ.

    The .env file is the fallback; environment variables set by ~/.zshrc (which
    OpenClaw reads at startup) take precedence. Without this guard, sourcing .env
    after shell startup would silently overwrite the already-exported values.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("FIELDKIT_TEST_VAR=from_file\n")
    monkeypatch.setattr(ce, "_ENV_FILE", env_file)
    monkeypatch.setenv("FIELDKIT_TEST_VAR", "from_shell")

    ce._load_env()

    assert os.environ.get("FIELDKIT_TEST_VAR") == "from_shell"


# ---------------------------------------------------------------------------
# 3 — main() integration tests
# ---------------------------------------------------------------------------

def test_happy_path_sends_ack_and_updates_state(monkeypatch, stub_state, stub_logger):
    """
    A valid email from an allowlisted sender produces a Telegram ack, a state update,
    and a log entry — in the correct order.

    Verified order: enqueue_pending → _telegram ack → dequeue_pending. The pending
    entry must exist before the ack is sent (so the stale-alert path can recover if
    Telegram is down), and must be removed only after the send attempt (so a crash
    between send and dequeue still leaves a recoverable pending entry).
    """
    call_log = []

    monkeypatch.setattr(ce, "enqueue_pending",
                        lambda *a, **kw: call_log.append("enqueue"))
    monkeypatch.setattr(ce, "dequeue_pending",
                        lambda ref_id: call_log.append(("dequeue", ref_id)))
    monkeypatch.setattr(ce, "_telegram",
                        lambda chat_id, msg: call_log.append(("telegram", msg)))
    monkeypatch.setattr(ce, "_gws", lambda args: (
        {"messages": [{"id": "msg001"}]} if "messages list" in " ".join(args)
        else _FULL_MSG_ALLOWED
    ))
    monkeypatch.setattr(ce, "subprocess", MagicMock())

    ce.main()

    # Verify content
    telegram_msgs = [e[1] for e in call_log if isinstance(e, tuple) and e[0] == "telegram"]
    assert any("✓ Email received" in m for m in telegram_msgs)
    assert any("#0001" in m for m in telegram_msgs)

    # Verify order: enqueue → telegram ack → dequeue
    events = [e if isinstance(e, str) else e[0] for e in call_log]
    enqueue_pos = events.index("enqueue")
    ack_pos = next(i for i, e in enumerate(call_log)
                   if isinstance(e, tuple) and e[0] == "telegram" and "✓ Email received" in e[1])
    dequeue_pos = next(i for i, e in enumerate(call_log)
                       if isinstance(e, tuple) and e[0] == "dequeue")
    assert enqueue_pos < ack_pos < dequeue_pos

    ce.log_received.assert_called_once()
    ce.log_cycle.assert_called_once_with(1, 0)


def test_rejection_path_sends_rejection_notification(monkeypatch, stub_state, stub_logger):
    """
    An email from a sender not in ADMIN_ALLOWLIST triggers a rejection Telegram
    message and logs REJECTED — no ref ID is assigned, no label is applied.

    The rejection message must contain the from address and subject so the admin
    can identify the sender. log_cycle must record 0 processed and 1 rejected.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws", lambda args: (
        {"messages": [{"id": "msg002"}]} if "messages list" in " ".join(args)
        else _FULL_MSG_REJECTED
    ))
    monkeypatch.setattr(ce, "subprocess", MagicMock())

    ce.main()

    assert any("✗ Email rejected" in m for m in telegram_calls)
    assert any("spam@evil.com" in m for m in telegram_calls)
    ce.log_rejected.assert_called_once()
    ce.log_cycle.assert_called_once_with(0, 1)
    ce.enqueue_pending.assert_not_called()


def test_no_new_emails_user_triggered_sends_telegram(monkeypatch, stub_state, stub_logger):
    """
    When triggered by the user (/check_email) with an empty inbox, the script
    sends 'No new emails.' via Telegram.

    The cron path must NOT send this message (covered in the next test). The
    distinction matters because a cron job fires every 5 minutes; sending
    'No new emails.' on every quiet cycle would spam the admin.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws", lambda args: {})  # empty inbox

    ce.main()

    assert any("No new emails" in m for m in telegram_calls)
    ce.log_cycle.assert_called_once_with(0, 0)


def test_no_new_emails_cron_triggered_stays_silent(monkeypatch, stub_state, stub_logger):
    """
    When triggered by cron with an empty inbox, no Telegram message is sent.

    Passes --source cron to simulate the cron invocation path. The only
    observable side effect must be log_cycle(0, 0) — no Telegram call.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws", lambda args: {})

    import sys
    monkeypatch.setattr(sys, "argv", ["check_email.py", "--source", "cron"])
    ce.main()

    assert telegram_calls == []
    ce.log_cycle.assert_called_once_with(0, 0)


def test_stale_entries_trigger_alert_and_are_dequeued(monkeypatch, stub_state, stub_logger):
    """
    Stale pending entries (> 15 min old) cause an alert email, dequeue of each entry,
    and a log_stale_alert call — before any new messages are processed.

    Uses two stale entries to verify all are dequeued and logged, not just the first.
    The stale check runs in Phase 2, before Phase 3 (list messages), so the alert
    fires even when there are no new emails to process.
    """
    stale = [
        {"ref_id": "#0001", "subject": "Job A", "queued_at": "2026-05-10T10:00:00Z", "from": "a@b.com"},
        {"ref_id": "#0002", "subject": "Job B", "queued_at": "2026-05-10T10:01:00Z", "from": "a@b.com"},
    ]
    monkeypatch.setattr(ce, "get_stale_pending", lambda **_: stale)
    monkeypatch.setattr(ce, "_telegram", MagicMock())
    monkeypatch.setattr(ce, "_gws", lambda args: {})
    monkeypatch.setattr(ce, "subprocess", MagicMock())  # alert email send

    ce.main()

    assert ce.dequeue_pending.call_count == 2
    ce.log_stale_alert.assert_called_once_with(["#0001", "#0002"])


def test_empty_allowlist_sends_error_and_exits(monkeypatch, stub_state, stub_logger):
    """
    An empty ADMIN_ALLOWLIST causes the script to send an error message and exit(1).

    ADMIN_ALLOWLIST being empty is a misconfiguration, not a recoverable runtime
    error. The exit must happen before any Gmail or state access so no partial state
    is written. The error message tells the admin exactly which variable to fix.
    """
    telegram_calls = []
    monkeypatch.setenv("ADMIN_ALLOWLIST", "")
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    assert any("ADMIN_ALLOWLIST" in m for m in telegram_calls)
    ce.log_cycle.assert_not_called()


def test_missing_chat_id_exits_without_telegram(monkeypatch, stub_state, stub_logger):
    """
    A missing ADMIN_TELEGRAM_CHAT_ID causes exit(1) before any external call.

    Without a chat ID, the script cannot send any notification — including the
    error notification itself. The only safe behaviour is to exit immediately.
    log_cycle must not be called because no cycle completes.
    """
    monkeypatch.delenv("ADMIN_TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    ce.log_cycle.assert_not_called()


def test_concurrent_run_exits_cleanly(monkeypatch, stub_state, stub_logger):
    """
    When the lockfile is already held by another process, main() exits with code 0.

    A cron job and a manual /check_email trigger can overlap within the same minute.
    The second instance must not process the same messages — it exits immediately
    without touching Gmail, state, or Telegram. Exit code 0 (not 1) because this
    is not an error condition.
    """
    monkeypatch.setattr(ce, "_acquire_run_lock", lambda: None)
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 0
    assert telegram_calls == []
    ce.log_cycle.assert_not_called()


def test_gmail_error_on_single_message_skips_and_continues(monkeypatch, stub_state, stub_logger):
    """
    A fetch failure on one message is logged and skipped; subsequent messages are
    still processed and log_cycle reflects the actual count.

    Simulates two messages: the first fetch fails with RuntimeError, the second
    succeeds. The script must continue to the second message (not abort) and
    report processed=1, rejected=0 in log_cycle.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "subprocess", MagicMock())

    call_count = {"n": 0}

    def gws_side_effect(args):
        cmd = " ".join(args)
        if "messages list" in cmd:
            return {"messages": [{"id": "msg001"}, {"id": "msg002"}]}
        if "messages get" in cmd:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("API timeout")
            return _FULL_MSG_ALLOWED
        return {}

    monkeypatch.setattr(ce, "_gws", gws_side_effect)

    ce.main()

    # First message skipped, second processed — cycle must reflect reality.
    ce.log_cycle.assert_called_once_with(1, 0)
    assert any("✓ Email received" in m for m in telegram_calls)


def test_state_error_sends_telegram_and_exits(monkeypatch, stub_state, stub_logger):
    """
    A RuntimeError from get_ref_id_for_message (e.g. corrupt state.json) causes
    the script to send an error message via Telegram and exit(1).

    State corruption must be surfaced to the admin immediately so they can
    intervene before the next cron cycle silently fails. The error message
    must contain enough detail for diagnosis.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws", lambda args: (
        {"messages": [{"id": "msg001"}]} if "messages list" in " ".join(args)
        else _FULL_MSG_ALLOWED
    ))
    monkeypatch.setattr(ce, "subprocess", MagicMock())
    monkeypatch.setattr(ce, "get_ref_id_for_message",
                        lambda _: (_ for _ in ()).throw(RuntimeError("state.json is corrupt")))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    assert any("state error" in m for m in telegram_calls)


def test_gmail_list_failure_sends_telegram_and_exits(monkeypatch, stub_state, stub_logger):
    """
    A RuntimeError from the Gmail messages/list call (Phase 3) causes the script
    to send a Telegram error message and exit(1).

    Phase 1 (label) and Phase 2 (stale check) both pass. The failure occurs at the
    inbox poll — the most common point for transient API errors. The admin must be
    notified so they know the cycle did not run, rather than assuming silence means
    no new emails.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws",
                        lambda args: (_ for _ in ()).throw(RuntimeError("quota exceeded")))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    assert any("Gmail list failed" in m for m in telegram_calls)
    ce.log_cycle.assert_not_called()


def test_resolve_label_id_failure_sends_telegram_and_exits(monkeypatch, stub_state, stub_logger):
    """
    A RuntimeError from the labels/list gws call (Phase 1) causes the script
    to send a Telegram error message and exit(1).

    Forces the cache-miss path by patching get_label_id to return None, then
    makes the gws labels/list call raise. The script must not proceed to Phase 2
    or Phase 3 — no state is read or written, and log_cycle is not called.
    """
    telegram_calls = []
    monkeypatch.setattr(ce, "get_label_id", lambda: None)
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))
    monkeypatch.setattr(ce, "_gws",
                        lambda args: (_ for _ in ()).throw(RuntimeError("labels API down")))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    assert any("fk-received label" in m for m in telegram_calls)
    ce.log_cycle.assert_not_called()


def test_missing_agent_email_sends_error_and_exits(monkeypatch, stub_state, stub_logger):
    """
    A missing AGENT_EMAIL causes exit(1) with a Telegram notification before any
    Gmail or state access.

    AGENT_EMAIL is required to build the From: header in stale alert emails. An
    empty value produces a malformed RFC 2822 message that the Gmail API rejects
    silently (check=False), and the stale entries would be dequeued without the
    admin ever receiving the alert. Failing fast with a clear error prevents that
    silent data loss.
    """
    telegram_calls = []
    monkeypatch.delenv("AGENT_EMAIL", raising=False)
    monkeypatch.setattr(ce, "_telegram", lambda chat_id, msg: telegram_calls.append(msg))

    with pytest.raises(SystemExit) as exc_info:
        ce.main()

    assert exc_info.value.code == 1
    assert any("AGENT_EMAIL" in m for m in telegram_calls)
    ce.log_cycle.assert_not_called()


def test_gws_file_not_found_raises_runtime_error(monkeypatch):
    """
    A missing gws binary raises RuntimeError, not FileNotFoundError.

    _gws() catches FileNotFoundError from subprocess and re-raises it as
    RuntimeError so that all callers (which only catch RuntimeError) surface
    the error via Telegram rather than crashing silently.
    """
    import subprocess as real_subprocess

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("No such file or directory: 'gws'")

    monkeypatch.setattr(ce.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gws binary not found"):
        ce._gws(["gmail", "users", "messages", "list", "--params", "{}"])


import os  # noqa: E402  — placed here to avoid shadowing the top-level fixture
