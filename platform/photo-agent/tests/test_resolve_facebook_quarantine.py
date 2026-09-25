"""
Tests for scripts/resolve_facebook_quarantine.py — the operator tool that settles a
quarantined Facebook publish (issue #78, PR #87 review).

REAL tools.facebook_state against an isolated tmp file and the REAL facebook_api, with
only HTTP (requests.get/delete inside facebook_api) and the activity log mocked. No
network access, and nothing is written outside tmp_path.
"""

import fcntl

import pytest
import requests

import tools.facebook_state as fb_state

_TOKEN = "page_token_SECRET_value_123"
_KEY = "42"
_VID = "vid_q"


def _resp(body, ok=True, status=200):
    r = type("R", (), {})()
    r.ok, r.status_code, r.json = ok, status, (lambda: body)
    return r


_GONE = _resp({"error": {"code": 100, "message": "Object does not exist"}}, ok=False, status=400)
_PROCESSING = _resp({"status": {"video_status": "processing",
                                "publishing_phase": {"publish_status": "draft"}}})
_PUBLISHED = _resp({"status": {"video_status": "ready",
                               "publishing_phase": {"publish_status": "published"}}})


class FakeGraph:
    """Answers the two GET shapes (fields=status / fields=id) and DELETE."""

    def __init__(self, status=_PROCESSING, delete=None, after_delete=_GONE):
        self.status, self.delete_resp, self.after_delete = status, delete, after_delete
        self.deleted = False
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        if params == {"fields": "status"}:
            return self.status
        assert params == {"fields": "id"}
        return self.after_delete if self.deleted else _resp({"id": _VID})

    def delete(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("DELETE", url, kwargs.get("params"), headers))
        if isinstance(self.delete_resp, Exception):
            raise self.delete_resp
        self.deleted = True
        return self.delete_resp or _resp({"success": True})


@pytest.fixture
def tool(tmp_path, monkeypatch, mocker):
    """Isolated state + a quarantined video for key 42."""
    import scripts.resolve_facebook_quarantine as rq
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _TOKEN)
    for name in ("log_upload_recovered", "log_publish_resolved"):
        mocker.patch.object(rq.facebook_logger, name)

    record = {
        "project_name": "p", "video_local_path": "/x.mp4", "page_id": "1",
        "status": "pending", "attempt_count": 0, "last_attempt_at": None,
        "triggered_at": "t", "idempotency_key": _KEY, "fb_post_id": None,
    }
    fb_state.set_pending_upload(record)
    fb_state.claim_pending_upload(_KEY, cooldown_seconds=0, max_attempts=3, lease_seconds=900)
    fb_state.set_upload_session(_KEY, _VID, "s")
    fb_state.mark_publish_attempted(_KEY, _VID)
    fb_state.mark_failed(_KEY)
    assert fb_state.has_unresolved_publish(_KEY)
    rq.record = record
    return rq


def _install(mocker, fake):
    import tools.facebook_api as fb_api
    mocker.patch.object(fb_api.requests, "get", side_effect=fake.get)
    mocker.patch.object(fb_api.requests, "delete", side_effect=fake.delete)


def test_published_video_is_recorded_not_deleted(tool, mocker):
    fake = FakeGraph(status=_PUBLISHED)
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 0
    assert fb_state.is_published(_KEY)
    assert not fb_state.has_unresolved_publish(_KEY)
    assert not any(c[0] == "DELETE" for c in fake.calls)
    with pytest.raises(ValueError, match="already in published"):
        fb_state.set_pending_upload(tool.record)


def test_unconfirmed_video_is_deleted_confirmed_gone_then_released(tool, mocker):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 0
    assert [c[0] for c in fake.calls] == ["GET", "DELETE", "GET"]
    assert fake.calls[1][1] == f"https://graph.facebook.com/v25.0/{_VID}"
    assert not fb_state.has_unresolved_publish(_KEY)
    assert not fb_state.is_published(_KEY)
    fb_state.set_pending_upload(tool.record)  # re-approval now accepted


def test_delete_reporting_does_not_exist_keeps_the_quarantine(tool, mocker, capsys):
    """Code 100 on the DELETE is ambiguous (gone vs. not visible) — never a release."""
    fake = FakeGraph(delete=_resp({"error": {"code": 100, "message": "does not exist"}},
                                  ok=False, status=400))
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 1
    assert fb_state.has_unresolved_publish(_KEY)
    assert "not proof it never went live" in capsys.readouterr().err


def test_video_still_readable_after_delete_keeps_the_quarantine(tool, mocker):
    fake = FakeGraph(after_delete=_resp({"id": _VID}))
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 1
    assert fb_state.has_unresolved_publish(_KEY)


def test_status_check_failure_still_goes_through_delete_and_confirm(tool, mocker):
    """An unreachable status read is 'not confirmed published', never 'not published':
    the key is released only via delete + confirmed gone."""
    fake = FakeGraph(status=_resp({"error": {"code": 2, "message": "down"}}, ok=False, status=500))
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 0
    assert [c[0] for c in fake.calls] == ["GET", "DELETE", "GET"]


def test_errors_are_redacted_and_token_only_in_header(tool, mocker, capsys):
    fake = FakeGraph(delete=requests.exceptions.ConnectionError(
        f"https://graph.facebook.com/v25.0/{_VID}?access_token={_TOKEN} reset"))
    _install(mocker, fake)
    assert tool.main(["resolve", _VID]) == 1
    out = capsys.readouterr()
    assert _TOKEN not in out.out + out.err
    for _, url, params, headers in fake.calls:
        assert headers == {"Authorization": f"OAuth {_TOKEN}"}
        assert _TOKEN not in url and _TOKEN not in repr(params)
    assert fb_state.has_unresolved_publish(_KEY)


def test_override_requires_the_explicit_flag(tool, mocker, capsys):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["override", _VID]) == 2
    assert fb_state.has_unresolved_publish(_KEY)
    assert "post it twice" in capsys.readouterr().err
    assert fake.calls == []


def test_override_with_flag_releases_and_says_so(tool, mocker, capsys):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["override", _VID, "--accept-duplicate-risk"]) == 0
    assert not fb_state.has_unresolved_publish(_KEY)
    assert "a second time" in capsys.readouterr().out
    assert fake.calls == []  # no Graph call — it is explicitly not an answer
    tool.facebook_logger.log_publish_resolved.assert_called_once_with(
        "p", _VID, fb_state.OPERATOR_OVERRIDE
    )


def test_unknown_video_id_is_refused(tool, mocker):
    fake = FakeGraph()
    _install(mocker, fake)
    assert tool.main(["resolve", "nope"]) == 1
    assert fake.calls == []


def test_refuses_while_an_upload_tick_holds_the_lock(tool, mocker, tmp_path):
    fake = FakeGraph()
    _install(mocker, fake)
    with open(tmp_path / "photo-agent" / "upload_facebook.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tool.main(["resolve", _VID]) == 1
    assert fake.calls == []
    assert fb_state.has_unresolved_publish(_KEY)


def test_list_prints_entries(tool, capsys):
    assert tool.main(["list"]) == 0
    assert f"video_id={_VID}" in capsys.readouterr().out
