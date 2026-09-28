"""
Tests for scripts/resolve_instagram_quarantine.py — the operator tool that settles a
quarantined Instagram publish (issue #88).

REAL tools.instagram_state against an isolated tmp file, the REAL instagram_api, and the
REAL instagram_logger writing to a tmp log file. Only HTTP (requests.get inside
instagram_api) is mocked. No network access, and nothing is written outside tmp_path.
"""

import fcntl

import pytest
import requests

import tools.instagram_logger as ig_logger
import tools.instagram_state as ig_state

_TOKEN = "page_token_SECRET_value_123"
_KEY = "42"
_CID = "17900000000000001"
_GRAPH = "https://graph.facebook.com/v25.0"


def _resp(body, ok=True, status=200):
    r = type("R", (), {})()
    r.ok, r.status_code, r.json = ok, status, (lambda: body)
    return r


def _status(code):
    return _resp({"status_code": code, "id": _CID})


class FakeGraph:
    """Answers GET /{container_id}?fields=status_code from a script of responses.

    Each item is a status_code string, a prepared response, or an exception to raise.
    Records every call so tests can assert on URL, params and headers.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        assert url == f"{_GRAPH}/{_CID}" and params == {"fields": "status_code"}
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _status(item) if isinstance(item, str) else item


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """Isolated state and log, plus a quarantined container for key 42.

    The quarantine is reached through the real transitions, exactly as upload_instagram.py
    produces it: a job, its container, the publish marker, then a terminal mark_failed().
    """
    import scripts.resolve_instagram_quarantine as rq
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    monkeypatch.setattr(ig_logger, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ig_logger, "LOG_FILE", tmp_path / "logs" / "photo-agent.log")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _TOKEN)

    record = {
        "project_name": "p", "video_local_path": "/x.mp4",
        "ig_business_account_id": "17841400000000000", "status": "pending",
        "attempt_count": 0, "last_attempt_at": None, "triggered_at": "t",
        "idempotency_key": _KEY, "container_id": None, "ig_post_id": None,
    }
    ig_state.set_pending_upload(record)
    ig_state.set_container_id(_KEY, _CID)
    ig_state.mark_publish_attempted(_KEY)
    ig_state.mark_failed(_KEY)
    assert ig_state.has_unresolved_publish(_KEY)
    rq.record = record
    rq.log_file = tmp_path / "logs" / "photo-agent.log"
    return rq


def _install(mocker, fake):
    import tools.instagram_api as ig_api
    mocker.patch.object(ig_api.requests, "get", side_effect=fake.get)
    mocker.patch.object(ig_api.requests, "post", side_effect=AssertionError("no POST allowed"))


def _reapproval_refused(record) -> bool:
    """True if set_pending_upload() — the backstop behind every re-approval — refuses."""
    try:
        ig_state.set_pending_upload(record)
    except ValueError:
        return True
    return False


# --- each status branch ---

def test_published_container_is_recorded_and_key_retired(tool, mocker, capsys):
    fake = FakeGraph("PUBLISHED")
    _install(mocker, fake)
    assert tool.main(["resolve", _CID]) == 0
    assert ig_state.is_published(_KEY)
    assert not ig_state.has_unresolved_publish(_KEY)
    with pytest.raises(ValueError, match="already in published"):
        ig_state.set_pending_upload(tool.record)
    assert "IS published" in capsys.readouterr().out
    assert "IG_RECOVER" in tool.log_file.read_text()


def test_expired_container_releases_the_key(tool, mocker, capsys):
    fake = FakeGraph("EXPIRED")
    _install(mocker, fake)
    assert tool.main(["resolve", _CID]) == 0
    assert not ig_state.has_unresolved_publish(_KEY)
    assert not ig_state.is_published(_KEY)
    ig_state.set_pending_upload(tool.record)            # re-approval now accepted
    assert "status=EXPIRED" in tool.log_file.read_text()


@pytest.mark.parametrize("status", ["FINISHED", "ERROR", "IN_PROGRESS", "SOMETHING_NEW"])
def test_non_definitive_status_keeps_the_quarantine(tool, mocker, capsys, status):
    """Only PUBLISHED and EXPIRED are answers. FINISHED is 'not published YET'."""
    fake = FakeGraph(status)
    _install(mocker, fake)
    assert tool.main(["resolve", _CID]) == 1
    assert ig_state.has_unresolved_publish(_KEY)
    assert not ig_state.is_published(_KEY)
    assert _reapproval_refused(tool.record)
    assert "quarantine stays in place" in capsys.readouterr().err


def test_finished_tells_the_operator_when_it_should_expire(tool, mocker, capsys):
    _install(mocker, FakeGraph("FINISHED"))
    assert tool.main(["resolve", _CID]) == 1
    err = capsys.readouterr().err
    assert "not 'never published'" in err
    assert "EXPIRED by " in err


def test_error_status_points_at_override(tool, mocker, capsys):
    _install(mocker, FakeGraph("ERROR"))
    assert tool.main(["resolve", _CID]) == 1
    assert "override" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [
    _resp({"error": {"code": 100, "message": "Unsupported get request"}}, ok=False, status=400),
    _resp({"error": {"code": 190, "message": "token expired"}}, ok=False, status=400),
    _resp({"error": {"code": 2, "message": "service unavailable"}}, ok=False, status=500),
    requests.exceptions.ConnectionError("reset"),
])
def test_unreadable_container_keeps_the_quarantine(tool, mocker, capsys, failure):
    """An error — including 'does not exist' — is not an answer about whether it published."""
    _install(mocker, FakeGraph(failure))
    assert tool.main(["resolve", _CID]) == 1
    assert ig_state.has_unresolved_publish(_KEY)
    assert _reapproval_refused(tool.record)
    assert "not proof" in capsys.readouterr().err


# --- safeguards ---

def test_errors_are_redacted_and_token_only_in_header(tool, mocker, capsys):
    fake = FakeGraph(requests.exceptions.ConnectionError(
        f"{_GRAPH}/{_CID}?access_token={_TOKEN} reset by peer {_TOKEN}"))
    _install(mocker, fake)
    assert tool.main(["resolve", _CID]) == 1
    out = capsys.readouterr()
    assert _TOKEN not in out.out + out.err
    assert fake.calls
    for url, params, headers in fake.calls:
        assert headers == {"Authorization": f"Bearer {_TOKEN}"}
        assert _TOKEN not in url and _TOKEN not in repr(params)
    assert ig_state.has_unresolved_publish(_KEY)


def test_override_requires_the_explicit_flag(tool, mocker, capsys):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["override", _CID]) == 2
    assert ig_state.has_unresolved_publish(_KEY)
    assert "post it twice" in capsys.readouterr().err
    assert fake.calls == []
    assert not tool.log_file.exists()


def test_override_with_flag_releases_warns_and_logs(tool, mocker, capsys):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["override", _CID, "--accept-duplicate-risk"]) == 0
    assert not ig_state.has_unresolved_publish(_KEY)
    assert "a second time" in capsys.readouterr().out
    assert fake.calls == []  # no Graph call — it is explicitly not an answer
    line = tool.log_file.read_text()
    assert "IG_RESOLVED" in line and f"status={ig_state.OPERATOR_OVERRIDE}" in line


def test_unknown_container_id_is_refused(tool, mocker):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["resolve", "nope"]) == 1
    assert tool.main(["override", "nope", "--accept-duplicate-risk"]) == 1
    assert fake.calls == []
    assert ig_state.has_unresolved_publish(_KEY)


def test_missing_token_is_refused(tool, mocker, monkeypatch):
    fake = FakeGraph()
    _install(mocker, fake)
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN")
    assert tool.main(["resolve", _CID]) == 1
    assert fake.calls == []
    assert ig_state.has_unresolved_publish(_KEY)


@pytest.mark.parametrize("argv", [
    ["resolve", _CID], ["override", _CID, "--accept-duplicate-risk"], ["list"],
])
def test_refuses_while_an_upload_tick_holds_the_lock(tool, mocker, tmp_path, argv):
    """Mutually exclusive with upload_instagram.py's cron via upload_instagram.lock."""
    fake = FakeGraph("EXPIRED")
    _install(mocker, fake)
    with open(tmp_path / "photo-agent" / "upload_instagram.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tool.main(argv) == 1
    assert fake.calls == []
    assert ig_state.has_unresolved_publish(_KEY)


def test_the_lock_is_released_afterwards(tool, mocker, tmp_path):
    _install(mocker, FakeGraph("FINISHED"))
    tool.main(["resolve", _CID])
    with open(tmp_path / "photo-agent" / "upload_instagram.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # would raise if still held


def test_list_prints_entries(tool, capsys):
    assert tool.main(["list"]) == 0
    assert f"container_id={_CID}" in capsys.readouterr().out


# --- replay of the duplicate-Reel sequence (issue #88) ---

def test_replay_a_late_landing_publish_is_never_published_twice(tool, mocker):
    """The sequence the old README allowed to become a duplicate Reel.

    A publish response is lost; the Reel is "not live" when the operator looks (FINISHED),
    and stays unanswerable for a while (ERROR, a Graph outage). Under the old procedure the
    operator removed the entry at the first look and re-approved. Here every one of those
    looks keeps re-approval refused; when the late publish lands and Instagram reports
    PUBLISHED, it is recorded — and re-approval stays refused for good.
    """
    fake = FakeGraph(
        "FINISHED",
        "ERROR",
        _resp({"error": {"code": 2, "message": "down"}}, ok=False, status=500),
        "IN_PROGRESS",
        "PUBLISHED",
    )
    _install(mocker, fake)
    for _ in range(4):
        assert tool.main(["resolve", _CID]) == 1
        assert ig_state.has_unresolved_publish(_KEY)
        assert _reapproval_refused(tool.record)
    assert tool.main(["resolve", _CID]) == 0
    assert ig_state.is_published(_KEY)
    assert _reapproval_refused(tool.record)
    assert fake.script == []


def test_replay_a_publish_that_never_lands_is_released_only_on_expiry(tool, mocker):
    """The same sequence when the publish really was lost: released only once EXPIRED."""
    _install(mocker, FakeGraph("FINISHED", "FINISHED", "EXPIRED"))
    assert tool.main(["resolve", _CID]) == 1
    assert _reapproval_refused(tool.record)
    assert tool.main(["resolve", _CID]) == 1
    assert _reapproval_refused(tool.record)
    assert tool.main(["resolve", _CID]) == 0
    ig_state.set_pending_upload(tool.record)            # re-approved exactly once
    assert not ig_state.is_published(_KEY)
