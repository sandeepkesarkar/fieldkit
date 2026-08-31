"""
Tests for tools/instagram_api.py — the Instagram Graph API v25.0 Reels wrapper.

Covers: upload_reel (container creation, status polling, publish) and the
InstagramUploadError exception type.

All HTTP calls are mocked; no real network calls are made.
"""

from unittest.mock import MagicMock

import pytest
import requests

from tools.instagram_api import InstagramUploadError, upload_reel


_ACCESS_TOKEN = "secret_ig_access_token_xyz789"


def _mock_response(json_data, ok=True, status_code=200):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# upload_reel — happy path
# ---------------------------------------------------------------------------

def test_upload_reel_returns_post_id_on_success(mocker):
    """upload_reel() returns the published post_id after container finishes and publishes."""
    create_resp = _mock_response({"id": "container_123"})
    poll_in_progress = _mock_response({"status_code": "IN_PROGRESS"})
    poll_finished = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response({"id": "post_id_abc123"})

    mocker.patch("tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp])
    mocker.patch("tools.instagram_api.requests.get", side_effect=[poll_in_progress, poll_finished])
    mocker.patch("tools.instagram_api.time.sleep")

    post_id = upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")
    assert post_id == "post_id_abc123"


def test_upload_reel_polls_multiple_times_before_finished(mocker):
    """upload_reel() keeps polling while status_code is IN_PROGRESS, stops at FINISHED."""
    create_resp = _mock_response({"id": "container_123"})
    poll_responses = [
        _mock_response({"status_code": "IN_PROGRESS"}),
        _mock_response({"status_code": "IN_PROGRESS"}),
        _mock_response({"status_code": "FINISHED"}),
    ]
    publish_resp = _mock_response({"id": "post_id_xyz"})

    mock_get = mocker.patch(
        "tools.instagram_api.requests.get", side_effect=poll_responses
    )
    mocker.patch("tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp])
    mocker.patch("tools.instagram_api.time.sleep")

    post_id = upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")
    assert post_id == "post_id_xyz"
    assert mock_get.call_count == 3


def test_upload_reel_sends_correct_container_creation_request(mocker):
    """upload_reel() POSTs video_url and media_type=REELS to the /media endpoint."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response({"id": "post_1"})

    mock_post = mocker.patch(
        "tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp]
    )
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    upload_reel(_ACCESS_TOKEN, "IG_USER_99", "https://example.com/reel.mp4")

    first_call = mock_post.call_args_list[0]
    url = first_call.args[0]
    assert "IG_USER_99" in url
    assert "/media" in url
    assert "graph.facebook.com" in url
    data = first_call.kwargs.get("data") or {}
    assert data.get("video_url") == "https://example.com/reel.mp4"
    assert data.get("media_type") == "REELS"
    assert data.get("access_token") == _ACCESS_TOKEN


def test_upload_reel_sends_correct_publish_request(mocker):
    """upload_reel() POSTs the container/creation id to the /media_publish endpoint."""
    create_resp = _mock_response({"id": "container_777"})
    poll_resp = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response({"id": "post_777"})

    mock_post = mocker.patch(
        "tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp]
    )
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")

    publish_call = mock_post.call_args_list[1]
    url = publish_call.args[0]
    assert "media_publish" in url
    data = publish_call.kwargs.get("data") or {}
    assert data.get("creation_id") == "container_777"
    assert data.get("access_token") == _ACCESS_TOKEN


# ---------------------------------------------------------------------------
# upload_reel — container creation failures
# ---------------------------------------------------------------------------

def test_upload_reel_raises_on_container_creation_http_error(mocker):
    """upload_reel() raises InstagramUploadError on non-OK HTTP response from /media."""
    create_resp = _mock_response({}, ok=False, status_code=400)
    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


def test_upload_reel_raises_on_container_creation_api_error(mocker):
    """upload_reel() raises InstagramUploadError when the Graph API returns an error payload."""
    create_resp = _mock_response(
        {"error": {"message": "Invalid video_url", "type": "OAuthException", "code": 100}}
    )
    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


def test_upload_reel_raises_on_container_creation_network_error(mocker):
    """upload_reel() raises InstagramUploadError on a network failure during container creation."""
    mocker.patch(
        "tools.instagram_api.requests.post",
        side_effect=requests.exceptions.RequestException("connection reset"),
    )

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


# ---------------------------------------------------------------------------
# upload_reel — polling failures
# ---------------------------------------------------------------------------

def test_upload_reel_raises_on_poll_error_status(mocker):
    """upload_reel() raises InstagramUploadError when polling returns status_code=ERROR."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "ERROR"})

    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


def test_upload_reel_raises_on_poll_timeout(mocker):
    """upload_reel() raises InstagramUploadError if status never reaches FINISHED/ERROR."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "IN_PROGRESS"})

    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)
    mock_get = mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mock_sleep = mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError, match="did not finish"):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")

    # Bounded — never polls forever.
    assert mock_get.call_count > 1
    assert mock_sleep.call_count > 1


def test_upload_reel_raises_on_poll_network_error(mocker):
    """upload_reel() raises InstagramUploadError on a network failure while polling."""
    create_resp = _mock_response({"id": "container_1"})

    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)
    mocker.patch(
        "tools.instagram_api.requests.get",
        side_effect=requests.exceptions.RequestException("timeout"),
    )
    mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


# ---------------------------------------------------------------------------
# upload_reel — publish failures
# ---------------------------------------------------------------------------

def test_upload_reel_raises_on_publish_http_error(mocker):
    """upload_reel() raises InstagramUploadError on a non-OK HTTP response from /media_publish."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response({}, ok=False, status_code=500)

    mocker.patch("tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp])
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


def test_upload_reel_raises_on_publish_api_error(mocker):
    """upload_reel() raises InstagramUploadError when /media_publish returns an API error payload."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response(
        {"error": {"message": "Media not ready", "type": "OAuthException", "code": 9007}}
    )

    mocker.patch("tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp])
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


def test_upload_reel_raises_on_publish_network_error(mocker):
    """upload_reel() raises InstagramUploadError on a network failure during publish."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "FINISHED"})

    mocker.patch(
        "tools.instagram_api.requests.post",
        side_effect=[create_resp, requests.exceptions.RequestException("connection reset")],
    )
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with pytest.raises(InstagramUploadError):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")


# ---------------------------------------------------------------------------
# Exception type
# ---------------------------------------------------------------------------

def test_instagram_upload_error_is_subclass_of_runtime_error():
    """InstagramUploadError is a RuntimeError subclass."""
    assert issubclass(InstagramUploadError, RuntimeError)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

def test_upload_reel_never_logs_access_token_on_success(mocker, caplog):
    """upload_reel() does not emit the access_token in any log record on the happy path."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "FINISHED"})
    publish_resp = _mock_response({"id": "post_1"})

    mocker.patch("tools.instagram_api.requests.post", side_effect=[create_resp, publish_resp])
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with caplog.at_level("DEBUG"):
        upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")

    for record in caplog.records:
        assert _ACCESS_TOKEN not in record.getMessage()


def test_upload_reel_never_logs_access_token_on_failure(mocker, caplog):
    """upload_reel() does not emit the access_token in any log record when polling errors out."""
    create_resp = _mock_response({"id": "container_1"})
    poll_resp = _mock_response({"status_code": "ERROR"})

    mocker.patch("tools.instagram_api.requests.post", return_value=create_resp)
    mocker.patch("tools.instagram_api.requests.get", return_value=poll_resp)
    mocker.patch("tools.instagram_api.time.sleep")

    with caplog.at_level("DEBUG"):
        with pytest.raises(InstagramUploadError):
            upload_reel(_ACCESS_TOKEN, "ig_user_1", "https://example.com/video.mp4")

    for record in caplog.records:
        assert _ACCESS_TOKEN not in record.getMessage()
