"""
Tests for tools/facebook_api.py — the Facebook Graph API v25.0 wrapper.

Covers: build_auth_url, exchange_code_for_token, exchange_for_long_lived_token,
get_page_access_token, upload_video, and the two custom exception types
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

def test_upload_video_returns_post_id(mocker, tmp_path):
    """upload_video() returns the post_id string on success."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "post_id_abc123"}
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    post_id = upload_video("page_access_token", "123456789", video_file)
    assert post_id == "post_id_abc123"


def test_upload_video_posts_to_graph_api(mocker, tmp_path):
    """upload_video() sends a POST to the Graph API videos endpoint for the given page_id."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "post_id_abc"}
    mock_post = mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    upload_video("token", "PAGE_123", video_file)
    url = mock_post.call_args.args[0]
    assert "PAGE_123" in url
    assert "videos" in url
    assert "graph.facebook.com" in url


def test_upload_video_raises_facebook_token_error_on_error_code_190(mocker, tmp_path):
    """upload_video() raises FacebookTokenError when the FB error code is 190 (token invalid)."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "error": {
            "message": "Invalid OAuth access token",
            "type": "OAuthException",
            "code": 190,
        }
    }
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    with pytest.raises(FacebookTokenError):
        upload_video("expired_token", "123456789", video_file)


def test_upload_video_raises_facebook_upload_error_on_http_500(mocker, tmp_path):
    """upload_video() raises FacebookUploadError on HTTP 500 from the Graph API."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        upload_video("token", "123456789", video_file)


def test_upload_video_raises_facebook_upload_error_on_network_error(mocker, tmp_path):
    """upload_video() raises FacebookUploadError on network failure."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mocker.patch(
        "tools.facebook_api.requests.post",
        side_effect=requests.exceptions.RequestException("connection reset"),
    )
    with pytest.raises(FacebookUploadError):
        upload_video("token", "123456789", video_file)


def test_upload_video_raises_facebook_upload_error_on_non_190_fb_error(mocker, tmp_path):
    """upload_video() raises FacebookUploadError (not FacebookTokenError) for FB errors other than 190."""
    video_file = tmp_path / "video.mp4"
    video_file.write_bytes(b"\x00" * 64)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "error": {
            "message": "Video too large",
            "type": "GraphMethodException",
            "code": 100,
        }
    }
    mocker.patch("tools.facebook_api.requests.post", return_value=mock_resp)

    with pytest.raises(FacebookUploadError):
        upload_video("token", "123456789", video_file)


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
