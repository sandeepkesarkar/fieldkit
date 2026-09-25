"""
Tests for tools/facebook_api.py — the Facebook Graph API v25.0 wrapper.

Covers: build_auth_url, exchange_code_for_token, exchange_for_long_lived_token,
get_page_access_token, the sessionized upload_video, get_video_publish_state,
and the two custom exception types
(FacebookTokenError, FacebookUploadError).

All HTTP calls are mocked; no real network calls are made.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools.facebook_api import (
    FacebookTokenError,
    FacebookUploadError,
    build_auth_url,
    delete_post,
    exchange_code_for_token,
    exchange_for_long_lived_token,
    get_page_access_token,
    get_video_publish_state,
    upload_video,
)


# ---------------------------------------------------------------------------
# build_auth_url
# ---------------------------------------------------------------------------

def test_build_auth_url_contains_app_id():
    """The generated URL embeds the app_id in the client_id parameter."""
    url = build_auth_url("MY_APP_ID", "http://localhost:8080/callback", ["pages_show_list"], "state123")
    assert "MY_APP_ID" in url


def test_build_auth_url_contains_redirect_uri():
    """The generated URL includes the redirect_uri parameter."""
    url = build_auth_url("app", "http://localhost:8080/callback", ["pages_show_list"], "state123")
    assert "localhost" in url
    assert "callback" in url


def test_build_auth_url_contains_required_scopes():
    """The URL includes all scopes passed in the scopes list."""
    scopes = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
    url = build_auth_url("app", "http://localhost:8080/callback", scopes, "state123")
    for scope in scopes:
        assert scope in url


def test_build_auth_url_contains_state_token():
    """The URL includes the state token for CSRF protection."""
    url = build_auth_url("app", "http://localhost:8080/callback", ["pages_show_list"], "my_state_abc")
    assert "my_state_abc" in url


def test_build_auth_url_points_to_facebook_oauth():
    """The URL points to the Facebook OAuth dialog endpoint."""
    url = build_auth_url("app", "http://localhost:8080/callback", ["pages_show_list"], "s")
    assert "facebook.com" in url
    assert "oauth" in url


# ---------------------------------------------------------------------------
# exchange_code_for_token
# ---------------------------------------------------------------------------

def test_exchange_code_for_token_returns_token(mocker):
    """exchange_code_for_token() returns the access_token string on success."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"access_token": "short_token_abc"}
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    token = exchange_code_for_token("code123", "app_id", "app_secret", "http://localhost:8080/callback")
    assert token == "short_token_abc"


def test_exchange_code_for_token_raises_on_non_ok(mocker):
    """exchange_code_for_token() raises FacebookUploadError on non-OK HTTP response."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        exchange_code_for_token("bad_code", "app_id", "app_secret", "http://localhost:8080/callback")


def test_exchange_code_for_token_raises_on_network_error(mocker):
    """exchange_code_for_token() raises FacebookUploadError on network failure."""
    mocker.patch(
        "tools.facebook_api.requests.post",
        side_effect=requests.exceptions.RequestException("timeout"),
    )
    with pytest.raises(FacebookUploadError):
        exchange_code_for_token("code", "app_id", "app_secret", "http://localhost:8080/callback")


def test_exchange_code_for_token_raises_on_fb_error(mocker):
    """exchange_code_for_token() raises FacebookUploadError when response contains FB error."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"error": {"message": "Invalid code", "type": "OAuthException"}}
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        exchange_code_for_token("bad_code", "app_id", "app_secret", "http://localhost:8080/callback")


# ---------------------------------------------------------------------------
# exchange_for_long_lived_token
# ---------------------------------------------------------------------------

def test_exchange_for_long_lived_token_returns_token(mocker):
    """exchange_for_long_lived_token() returns the long-lived access_token string."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"access_token": "long_lived_token_xyz"}
    mocker.patch("tools.facebook_api.requests.get", return_value=mock_resp)

    token = exchange_for_long_lived_token("short_token", "app_id", "app_secret")
    assert token == "long_lived_token_xyz"


def test_exchange_for_long_lived_token_raises_on_non_ok(mocker):
    """exchange_for_long_lived_token() raises FacebookUploadError on non-OK response."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mocker.patch("tools.facebook_api.requests.get", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        exchange_for_long_lived_token("token", "app_id", "app_secret")


# ---------------------------------------------------------------------------
# get_page_access_token
# ---------------------------------------------------------------------------

_ME_ACCOUNTS_RESPONSE = {
    "data": [
        {"id": "111", "name": "Page One", "access_token": "page_token_111"},
        {"id": "222", "name": "Page Two", "access_token": "page_token_222"},
    ]
}


def test_get_page_access_token_returns_correct_token(mocker):
    """get_page_access_token() returns the token for the requested page_id."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = _ME_ACCOUNTS_RESPONSE
    mocker.patch("tools.facebook_api.requests.get", return_value=mock_resp)

    token = get_page_access_token("long_user_token", "222")
    assert token == "page_token_222"


def test_get_page_access_token_raises_if_page_not_found(mocker):
    """get_page_access_token() raises FacebookUploadError when page_id is not in the account."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = _ME_ACCOUNTS_RESPONSE
    mocker.patch("tools.facebook_api.requests.get", return_value=mock_resp)

    with pytest.raises(FacebookUploadError, match="[Pp]age"):
        get_page_access_token("long_user_token", "999")


def test_get_page_access_token_raises_on_non_ok(mocker):
    """get_page_access_token() raises FacebookUploadError on HTTP error from /me/accounts."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mocker.patch("tools.facebook_api.requests.get", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        get_page_access_token("expired_token", "111")


def test_get_page_access_token_raises_on_network_error(mocker):
    """get_page_access_token() raises FacebookUploadError on network failure."""
    mocker.patch(
        "tools.facebook_api.requests.get",
        side_effect=requests.exceptions.RequestException("connection refused"),
    )
    with pytest.raises(FacebookUploadError):
        get_page_access_token("token", "111")


# ---------------------------------------------------------------------------
# upload_video
# ---------------------------------------------------------------------------

_TOKEN = "page_token_SECRET_value_123"
_SESSION = "sess_1"
_VIDEO = "vid_987"


def _resp(body, ok=True, status=200):
    """Build a mocked requests.Response returning body as JSON."""
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.json.return_value = body
    return r


def _start(size, end=None):
    """A START response for a file of `size` bytes, with the first chunk ending at `end`."""
    return _resp({
        "upload_session_id": _SESSION,
        "video_id": _VIDEO,
        "start_offset": "0",
        "end_offset": str(size if end is None else end),
    })


def _video_file(tmp_path, size=64):
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(bytes(range(256))[:size] if size <= 256 else b"\x01" * size)
    return video_file


def _upload(video_file, **kwargs):
    """Call upload_video with recording callbacks unless overridden."""
    kwargs.setdefault("on_session_started", MagicMock())
    kwargs.setdefault("on_before_finish", MagicMock())
    return upload_video(_TOKEN, "PAGE_123", video_file, **kwargs)


def test_upload_video_runs_start_transfer_finish_and_returns_video_id(mocker, tmp_path):
    """The sessionized protocol: start → transfer → finish, returning the START video_id."""
    video_file = _video_file(tmp_path, 64)
    post = mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(64),
        _resp({"start_offset": "64", "end_offset": "64"}),
        _resp({"success": True}),
    ])
    assert _upload(video_file) == _VIDEO

    phases = [c.kwargs["data"]["upload_phase"] for c in post.call_args_list]
    assert phases == ["start", "transfer", "finish"]
    start, transfer, finish = post.call_args_list
    assert start.kwargs["data"]["file_size"] == "64"
    assert transfer.kwargs["data"]["upload_session_id"] == _SESSION
    assert transfer.kwargs["data"]["start_offset"] == "0"
    assert transfer.kwargs["files"]["video_file_chunk"][1] == video_file.read_bytes()
    assert finish.kwargs["data"] == {"upload_phase": "finish", "upload_session_id": _SESSION}
    for call in post.call_args_list:
        assert call.args[0] == "https://graph-video.facebook.com/v25.0/PAGE_123/videos"


def test_upload_video_sends_token_in_authorization_header_never_url_or_body(mocker, tmp_path):
    """The permanent Page token travels only in the Authorization header."""
    video_file = _video_file(tmp_path, 8)
    post = mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(8), _resp({"start_offset": "8", "end_offset": "8"}), _resp({"success": True}),
    ])
    _upload(video_file)
    for call in post.call_args_list:
        assert call.kwargs["headers"] == {"Authorization": f"OAuth {_TOKEN}"}
        assert _TOKEN not in call.args[0]
        assert _TOKEN not in repr(call.kwargs["data"])
        assert "params" not in call.kwargs


def test_upload_video_transfers_in_the_chunks_meta_asks_for(mocker, tmp_path):
    """Each transfer sends exactly [start_offset, end_offset) and follows Meta's next offsets."""
    video_file = _video_file(tmp_path, 100)
    data = video_file.read_bytes()
    post = mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(100, end=40),
        _resp({"start_offset": "40", "end_offset": "100"}),
        _resp({"start_offset": "100", "end_offset": "100"}),
        _resp({"success": True}),
    ])
    _upload(video_file)
    transfers = [c for c in post.call_args_list if c.kwargs["data"]["upload_phase"] == "transfer"]
    assert [c.kwargs["data"]["start_offset"] for c in transfers] == ["0", "40"]
    assert transfers[0].kwargs["files"]["video_file_chunk"][1] == data[0:40]
    assert transfers[1].kwargs["files"]["video_file_chunk"][1] == data[40:100]


def test_upload_video_callbacks_run_in_order_before_transfer_and_before_finish(mocker, tmp_path):
    """on_session_started gets the handle before any transfer; on_before_finish runs
    after the last transfer and before FINISH — the ordering issue #78 depends on."""
    video_file = _video_file(tmp_path, 8)
    events = []

    def _post(url, data, files=None, headers=None, timeout=None):
        events.append(("post", data["upload_phase"]))
        return {
            "start": _start(8),
            "transfer": _resp({"start_offset": "8", "end_offset": "8"}),
            "finish": _resp({"success": True}),
        }[data["upload_phase"]]

    mocker.patch("tools.facebook_api.requests.post", side_effect=_post)
    _upload(
        video_file,
        on_session_started=lambda vid, sid: events.append(("session", vid, sid)),
        on_before_finish=lambda: events.append(("before_finish",)),
    )
    assert events == [
        ("post", "start"),
        ("session", _VIDEO, _SESSION),
        ("post", "transfer"),
        ("before_finish",),
        ("post", "finish"),
    ]


def test_upload_video_does_not_finish_if_before_finish_callback_raises(mocker, tmp_path):
    """If the publish marker cannot be persisted, FINISH (the publish) is never sent."""
    video_file = _video_file(tmp_path, 8)
    post = mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(8), _resp({"start_offset": "8", "end_offset": "8"}),
    ])
    with pytest.raises(RuntimeError, match="state write failed"):
        _upload(video_file, on_before_finish=MagicMock(side_effect=RuntimeError("state write failed")))
    assert [c.kwargs["data"]["upload_phase"] for c in post.call_args_list] == ["start", "transfer"]


def test_upload_video_does_not_transfer_if_session_callback_raises(mocker, tmp_path):
    """If the handle cannot be persisted, nothing further is sent."""
    video_file = _video_file(tmp_path, 8)
    post = mocker.patch("tools.facebook_api.requests.post", side_effect=[_start(8)])
    with pytest.raises(RuntimeError):
        _upload(video_file, on_session_started=MagicMock(side_effect=RuntimeError("x")))
    assert post.call_count == 1


def test_upload_video_callbacks_are_required():
    """The persistence hooks cannot be omitted."""
    with pytest.raises(TypeError):
        upload_video(_TOKEN, "PAGE_123", "video.mp4")  # noqa — missing keyword-only args


def test_upload_video_raises_facebook_token_error_on_error_code_190(mocker, tmp_path):
    """FacebookTokenError when the Graph API reports code 190, even on HTTP 200."""
    video_file = _video_file(tmp_path)
    mocker.patch("tools.facebook_api.requests.post", return_value=_resp({
        "error": {"message": "Invalid OAuth access token", "type": "OAuthException", "code": 190}
    }))
    with pytest.raises(FacebookTokenError):
        _upload(video_file)


def test_upload_video_raises_facebook_upload_error_on_http_500(mocker, tmp_path):
    video_file = _video_file(tmp_path)
    mocker.patch("tools.facebook_api.requests.post", return_value=_resp({}, ok=False, status=500))
    with pytest.raises(FacebookUploadError):
        _upload(video_file)


def test_upload_video_raises_facebook_upload_error_on_non_190_fb_error(mocker, tmp_path):
    video_file = _video_file(tmp_path)
    mocker.patch("tools.facebook_api.requests.post", return_value=_resp({
        "error": {"message": "Video too large", "type": "GraphMethodException", "code": 100}
    }))
    with pytest.raises(FacebookUploadError) as excinfo:
        _upload(video_file)
    assert not isinstance(excinfo.value, FacebookTokenError)


def test_upload_video_network_error_is_upload_error_with_token_redacted(mocker, tmp_path):
    """A requests exception whose text carries the token is re-raised without it."""
    video_file = _video_file(tmp_path)
    mocker.patch(
        "tools.facebook_api.requests.post",
        side_effect=requests.exceptions.ConnectionError(
            f"https://graph-video.facebook.com/x?access_token={_TOKEN} reset; OAuth {_TOKEN}"
        ),
    )
    with pytest.raises(FacebookUploadError) as excinfo:
        _upload(video_file)
    assert _TOKEN not in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # original (token-bearing) exception not chained


def test_upload_video_start_missing_video_id_is_upload_error(mocker, tmp_path):
    video_file = _video_file(tmp_path)
    mocker.patch("tools.facebook_api.requests.post", return_value=_resp(
        {"upload_session_id": _SESSION, "start_offset": "0", "end_offset": "64"}
    ))
    session = MagicMock()
    with pytest.raises(FacebookUploadError, match="video_id"):
        _upload(video_file, on_session_started=session)
    session.assert_not_called()


def test_upload_video_finish_without_success_is_upload_error(mocker, tmp_path):
    """A FINISH response that does not report success is an error — outcome unknown."""
    video_file = _video_file(tmp_path, 8)
    mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(8), _resp({"start_offset": "8", "end_offset": "8"}), _resp({"success": False}),
    ])
    with pytest.raises(FacebookUploadError, match="did not report success"):
        _upload(video_file)


def test_upload_video_transfer_without_progress_is_upload_error(mocker, tmp_path):
    """A transfer that does not advance the offset fails instead of looping forever."""
    video_file = _video_file(tmp_path, 64)
    mocker.patch("tools.facebook_api.requests.post", side_effect=[
        _start(64, end=32),
        _resp({"start_offset": "0", "end_offset": "32"}),
    ])
    before_finish = MagicMock()
    with pytest.raises(FacebookUploadError, match="no progress"):
        _upload(video_file, on_before_finish=before_finish)
    before_finish.assert_not_called()


# ---------------------------------------------------------------------------
# get_video_publish_state — reconciliation against a pre-known video_id (issue #78)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, expected", [
    ({"video_status": "ready", "publishing_phase": {"status": "complete", "publish_status": "published"}},
     ("published", "publish_status_published")),
    ({"video_status": "error"}, ("not_published", "video_status_error")),
    ({"video_status": "upload_failed"}, ("not_published", "video_status_upload_failed")),
    ({"video_status": "expired"}, ("not_published", "video_status_expired")),
    ({"video_status": "ready", "publishing_phase": {"publish_status": "error"}},
     ("not_published", "publish_status_error")),
])
def test_get_video_publish_state_definitive(mocker, status, expected):
    mocker.patch("tools.facebook_api.requests.get", return_value=_resp({"status": status}))
    assert get_video_publish_state(_TOKEN, _VIDEO) == expected


@pytest.mark.parametrize("body", [
    {"status": {"video_status": "processing", "publishing_phase": {"publish_status": "draft"}}},
    {"status": {"video_status": "ready", "publishing_phase": {"publish_status": "scheduled"}}},
    {"status": {"video_status": "ready"}},  # no publishing_phase at all
    {"status": {"video_status": "upload_complete", "publishing_phase": {"status": "not_started"}}},
    {"status": {"video_status": "something_new"}},
    {"status": "weird"},
    {},
])
def test_get_video_publish_state_anything_else_is_unknown(mocker, body):
    """Only the documented definitive readings count; everything else is unknown."""
    mocker.patch("tools.facebook_api.requests.get", return_value=_resp(body))
    assert get_video_publish_state(_TOKEN, _VIDEO)[0] == "unknown"


def test_get_video_publish_state_queries_the_video_node_with_header_auth(mocker):
    get = mocker.patch("tools.facebook_api.requests.get", return_value=_resp({"status": {}}))
    get_video_publish_state(_TOKEN, _VIDEO)
    assert get.call_args.args[0] == f"https://graph.facebook.com/v25.0/{_VIDEO}"
    assert get.call_args.kwargs["params"] == {"fields": "status"}
    assert get.call_args.kwargs["headers"] == {"Authorization": f"OAuth {_TOKEN}"}


def test_get_video_publish_state_never_lists_page_videos(mocker):
    """Issue #78 hard rule: reconciliation asks about ONE video id, never the Page's
    recent videos. Only one GET, to the video node itself."""
    get = mocker.patch("tools.facebook_api.requests.get", return_value=_resp({"status": {}}))
    get_video_publish_state(_TOKEN, _VIDEO)
    assert get.call_count == 1
    assert "/videos" not in get.call_args.args[0]


def test_get_video_publish_state_raises_on_failure(mocker):
    """A failure to get an answer raises — callers must treat it as unknown."""
    mocker.patch(
        "tools.facebook_api.requests.get",
        side_effect=requests.exceptions.Timeout(f"timeout access_token={_TOKEN}"),
    )
    with pytest.raises(FacebookUploadError) as excinfo:
        get_video_publish_state(_TOKEN, _VIDEO)
    assert _TOKEN not in str(excinfo.value)


def test_get_video_publish_state_raises_token_error_on_190(mocker):
    mocker.patch("tools.facebook_api.requests.get", return_value=_resp(
        {"error": {"code": 190, "message": "expired"}}
    ))
    with pytest.raises(FacebookTokenError):
        get_video_publish_state(_TOKEN, _VIDEO)


def test_observed_tokens_match_facebook_state():
    """The two copies of the definitive observations must agree."""
    import tools.facebook_state as fb_state
    from tools import facebook_api
    assert facebook_api.OBSERVED_PUBLISHED == fb_state._OBSERVED_PUBLISHED
    assert facebook_api.OBSERVED_NOT_PUBLISHED == fb_state._OBSERVED_NOT_PUBLISHED


def test_facebook_token_error_is_subclass_of_runtime_error():
    """FacebookTokenError is a RuntimeError subclass."""
    assert issubclass(FacebookTokenError, RuntimeError)


def test_facebook_upload_error_is_subclass_of_runtime_error():
    """FacebookUploadError is a RuntimeError subclass."""
    assert issubclass(FacebookUploadError, RuntimeError)


def test_facebook_token_error_is_not_facebook_upload_error():
    """FacebookTokenError and FacebookUploadError are distinct types."""
    assert not issubclass(FacebookTokenError, FacebookUploadError)


# ---------------------------------------------------------------------------
# T009: delete_post
# ---------------------------------------------------------------------------

def test_delete_post_succeeds_on_200_true(mocker):
    """delete_post() returns None on a successful {'success': true} response."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"success": True}
    mocker.patch("tools.facebook_api.requests.delete", return_value=mock_resp)

    delete_post("page_token", "post_id_abc")  # must not raise


def test_delete_post_sends_delete_to_graph_api(mocker):
    """delete_post() sends DELETE to /{post_id}?access_token=... on the Graph API."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"success": True}
    mock_delete = mocker.patch("tools.facebook_api.requests.delete", return_value=mock_resp)

    delete_post("my_page_token", "post_999")

    url = mock_delete.call_args.args[0]
    assert "post_999" in url
    assert "graph.facebook.com" in url
    params = mock_delete.call_args.kwargs.get("params") or {}
    assert params.get("access_token") == "my_page_token"


def test_delete_post_raises_on_graph_error_code_100(mocker):
    """delete_post() raises FacebookUploadError when the Graph API returns error code 100."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "error": {
            "message": "Invalid parameter",
            "type": "GraphMethodException",
            "code": 100,
        }
    }
    mocker.patch("tools.facebook_api.requests.delete", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        delete_post("page_token", "post_id_abc")


def test_delete_post_raises_on_http_error(mocker):
    """delete_post() raises FacebookUploadError on an HTTP error response."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    mock_resp.json.return_value = {}
    mocker.patch("tools.facebook_api.requests.delete", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        delete_post("page_token", "post_id_abc")


def test_delete_post_raises_on_network_error(mocker):
    """delete_post() raises FacebookUploadError on a requests network exception."""
    mocker.patch(
        "tools.facebook_api.requests.delete",
        side_effect=requests.exceptions.RequestException("timeout"),
    )

    with pytest.raises(FacebookUploadError):
        delete_post("page_token", "post_id_abc")
