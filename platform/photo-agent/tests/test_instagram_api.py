"""
Tests for tools/instagram_api.py — the Instagram Graph API v25.0 Reels wrapper.

Covers: upload_reel (container creation, status polling, publish) and the
InstagramUploadError exception type.

Feature 005 extensions below the original prototype block:
  - InstagramTokenError on Graph API error code 190, from all three call sites
  - the three de-privatized public step functions (create_media_container,
    get_container_status, publish_container) and wait_for_container's poll loop
  - discover_business_account / InstagramAccountNotFoundError

All HTTP calls are mocked; no real network calls are made.
"""

from unittest.mock import MagicMock

import pytest
import requests

import tools.instagram_api as ig_api
from tools.instagram_api import (
    InstagramAccountNotFoundError,
    InstagramTokenError,
    InstagramUploadError,
    create_media_container,
    discover_business_account,
    get_container_status,
    publish_container,
    upload_reel,
    wait_for_container,
)


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


# ===========================================================================
# Feature 005 extensions
# ===========================================================================

_PAGE_ID = "123456789"
_IG_USER_ID = "17841400000000000"


def _error_response(code, message="boom", ok=True, status_code=200):
    """Graph API error payload. Defaults to a 2xx body, which is how token expiry arrives."""
    return _mock_response({"error": {"code": code, "message": message}}, ok=ok, status_code=status_code)


# ---------------------------------------------------------------------------
# InstagramTokenError — error code 190 at each of the three call sites
# ---------------------------------------------------------------------------

def test_create_media_container_raises_token_error_on_190(mocker):
    """Error code 190 during container creation raises InstagramTokenError, not UploadError."""
    mocker.patch("requests.post", return_value=_error_response(190, "Session has expired"))
    with pytest.raises(InstagramTokenError, match="token invalid/expired"):
        create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")


def test_get_container_status_raises_token_error_on_190(mocker):
    """Error code 190 while polling raises InstagramTokenError."""
    mocker.patch("requests.get", return_value=_error_response(190, "Session has expired"))
    with pytest.raises(InstagramTokenError, match="token invalid/expired"):
        get_container_status(_ACCESS_TOKEN, "container_1")


def test_publish_container_raises_token_error_on_190(mocker):
    """Error code 190 while publishing raises InstagramTokenError."""
    mocker.patch("requests.post", return_value=_error_response(190, "Session has expired"))
    with pytest.raises(InstagramTokenError, match="token invalid/expired"):
        publish_container(_ACCESS_TOKEN, _IG_USER_ID, "container_1")


def test_upload_reel_propagates_token_error_from_container_creation(mocker):
    """The whole-flow wrapper surfaces a 190 as InstagramTokenError too."""
    mocker.patch("requests.post", return_value=_error_response(190))
    with pytest.raises(InstagramTokenError):
        upload_reel(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")


def test_token_error_is_not_an_upload_error():
    """InstagramTokenError must NOT be catchable as InstagramUploadError.

    upload_instagram.py's retry policy depends on the two being distinguishable: a
    token failure skips the remaining attempt budget, an upload failure consumes one.
    """
    assert issubclass(InstagramTokenError, RuntimeError)
    assert not issubclass(InstagramTokenError, InstagramUploadError)


@pytest.mark.parametrize("code", [1, 100, 200, 4, None])
def test_non_190_error_codes_stay_upload_errors(mocker, code):
    """Every error code other than 190 remains a retryable InstagramUploadError."""
    mocker.patch("requests.post", return_value=_error_response(code))
    with pytest.raises(InstagramUploadError):
        create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")


# ---------------------------------------------------------------------------
# The three public step functions are independently callable
# ---------------------------------------------------------------------------

def test_create_media_container_returns_id_and_sends_reels_request(mocker):
    """create_media_container() POSTs media_type=REELS and returns the container id."""
    post = mocker.patch("requests.post", return_value=_mock_response({"id": "container_42"}))
    assert create_media_container(_ACCESS_TOKEN, "IG_USER_99", "https://example.com/reel.mp4") == "container_42"
    args, kwargs = post.call_args
    assert args[0].endswith("/IG_USER_99/media")
    assert kwargs["data"]["media_type"] == "REELS"
    assert kwargs["data"]["video_url"] == "https://example.com/reel.mp4"
    assert kwargs["data"]["access_token"] == _ACCESS_TOKEN


def test_create_media_container_raises_on_http_error(mocker):
    """A non-OK HTTP response from /media raises InstagramUploadError."""
    mocker.patch("requests.post", return_value=_mock_response({}, ok=False, status_code=500))
    with pytest.raises(InstagramUploadError, match="HTTP 500"):
        create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")


def test_create_media_container_raises_when_id_missing(mocker):
    """A 200 response with no 'id' is an error, not a silent None."""
    mocker.patch("requests.post", return_value=_mock_response({"unexpected": "shape"}))
    with pytest.raises(InstagramUploadError, match="missing 'id'"):
        create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")


def test_get_container_status_returns_status_code_from_one_call(mocker):
    """get_container_status() performs exactly one GET and returns its status_code."""
    get = mocker.patch("requests.get", return_value=_mock_response({"status_code": "IN_PROGRESS"}))
    assert get_container_status(_ACCESS_TOKEN, "container_42") == "IN_PROGRESS"
    assert get.call_count == 1
    args, kwargs = get.call_args
    assert args[0].endswith("/container_42")
    assert kwargs["params"]["fields"] == "status_code"


def test_get_container_status_does_not_sleep(mocker):
    """get_container_status() never sleeps — the bounded loop lives in wait_for_container()."""
    sleep = mocker.patch("tools.instagram_api.time.sleep")
    mocker.patch("requests.get", return_value=_mock_response({"status_code": "IN_PROGRESS"}))
    get_container_status(_ACCESS_TOKEN, "container_42")
    sleep.assert_not_called()


def test_get_container_status_raises_on_http_error(mocker):
    """A non-OK HTTP response while polling raises InstagramUploadError."""
    mocker.patch("requests.get", return_value=_mock_response({}, ok=False, status_code=502))
    with pytest.raises(InstagramUploadError, match="HTTP 502"):
        get_container_status(_ACCESS_TOKEN, "container_42")


def test_publish_container_returns_post_id_and_sends_creation_id(mocker):
    """publish_container() POSTs creation_id to /media_publish and returns the post id."""
    post = mocker.patch("requests.post", return_value=_mock_response({"id": "ig_post_7"}))
    assert publish_container(_ACCESS_TOKEN, "IG_USER_99", "container_42") == "ig_post_7"
    args, kwargs = post.call_args
    assert args[0].endswith("/IG_USER_99/media_publish")
    assert kwargs["data"]["creation_id"] == "container_42"


def test_publish_container_raises_on_http_error(mocker):
    """A non-OK HTTP response from /media_publish raises InstagramUploadError."""
    mocker.patch("requests.post", return_value=_mock_response({}, ok=False, status_code=400))
    with pytest.raises(InstagramUploadError, match="HTTP 400"):
        publish_container(_ACCESS_TOKEN, _IG_USER_ID, "container_42")


def test_public_step_functions_are_not_underscore_prefixed():
    """contracts/cli-contracts.md requires these three as public, importable names."""
    for name in ("create_media_container", "get_container_status", "publish_container"):
        assert hasattr(ig_api, name)
        assert not name.startswith("_")


# ---------------------------------------------------------------------------
# wait_for_container — bounded poll loop
# ---------------------------------------------------------------------------

def test_wait_for_container_returns_when_finished(mocker):
    """wait_for_container() returns as soon as the status reaches FINISHED."""
    mocker.patch("tools.instagram_api.time.sleep")
    status = mocker.patch(
        "tools.instagram_api.get_container_status",
        side_effect=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"],
    )
    wait_for_container(_ACCESS_TOKEN, "container_42")
    assert status.call_count == 3


def test_wait_for_container_raises_on_error_status(mocker):
    """A container reporting ERROR is a retryable upload failure."""
    mocker.patch("tools.instagram_api.time.sleep")
    mocker.patch("tools.instagram_api.get_container_status", return_value="ERROR")
    with pytest.raises(InstagramUploadError, match="status_code=ERROR"):
        wait_for_container(_ACCESS_TOKEN, "container_42")


def test_wait_for_container_caps_at_300_seconds(mocker):
    """A stuck container gives up after 60 attempts x 5s and raises InstagramUploadError."""
    sleep = mocker.patch("tools.instagram_api.time.sleep")
    status = mocker.patch("tools.instagram_api.get_container_status", return_value="IN_PROGRESS")
    with pytest.raises(InstagramUploadError, match="did not finish processing"):
        wait_for_container(_ACCESS_TOKEN, "container_42")
    assert status.call_count == ig_api._MAX_POLL_ATTEMPTS == 60
    assert sleep.call_args_list[0].args[0] == ig_api._POLL_INTERVAL_SECONDS == 5
    assert ig_api._MAX_POLL_ATTEMPTS * ig_api._POLL_INTERVAL_SECONDS == 300


def test_wait_for_container_propagates_token_error(mocker):
    """A 190 mid-poll surfaces as InstagramTokenError rather than being retried."""
    mocker.patch("tools.instagram_api.time.sleep")
    mocker.patch(
        "tools.instagram_api.get_container_status",
        side_effect=InstagramTokenError("expired"),
    )
    with pytest.raises(InstagramTokenError):
        wait_for_container(_ACCESS_TOKEN, "container_42")


# ---------------------------------------------------------------------------
# discover_business_account
# ---------------------------------------------------------------------------

def _discovery_responses(linked, account_type="BUSINESS"):
    """Return the two sequential GET responses discover_business_account() makes."""
    page_resp = _mock_response({"instagram_business_account": linked} if linked else {"id": _PAGE_ID})
    node_resp = _mock_response({"account_type": account_type, "username": "my_business_demo"})
    return [page_resp, node_resp]


def test_discover_business_account_returns_account_on_success(mocker):
    """A linked BUSINESS account is returned as {id, username, account_type}."""
    mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}
    ))
    result = discover_business_account(_ACCESS_TOKEN, _PAGE_ID)
    assert result == {
        "id": _IG_USER_ID,
        "username": "my_business_demo",
        "account_type": "BUSINESS",
    }


def test_discover_business_account_accepts_creator_accounts(mocker):
    """A CREATOR account is equally publishable and must be accepted."""
    mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}, account_type="CREATOR"
    ))
    assert discover_business_account(_ACCESS_TOKEN, _PAGE_ID)["account_type"] == "CREATOR"


def test_discover_business_account_queries_the_page_node(mocker):
    """Discovery reads instagram_business_account off the Page, using the Page token."""
    get = mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}
    ))
    discover_business_account(_ACCESS_TOKEN, _PAGE_ID)
    args, kwargs = get.call_args_list[0]
    assert args[0].endswith(f"/{_PAGE_ID}")
    assert "instagram_business_account" in kwargs["params"]["fields"]
    # The token authenticates via the Authorization header and must NOT be in the query
    # string — a requests exception renders the prepared URL, and that text ends up in
    # the durable activity log. See tools/instagram_api.py's module docstring.
    assert "access_token" not in kwargs["params"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"


def test_discover_business_account_raises_when_nothing_linked(mocker):
    """No instagram_business_account edge raises InstagramAccountNotFoundError."""
    mocker.patch("requests.get", return_value=_mock_response({"id": _PAGE_ID}))
    with pytest.raises(InstagramAccountNotFoundError, match="No Instagram professional account"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_raises_when_edge_is_null(mocker):
    """An explicitly null instagram_business_account is also 'not found'."""
    mocker.patch("requests.get", return_value=_mock_response({"instagram_business_account": None}))
    with pytest.raises(InstagramAccountNotFoundError):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_rejects_personal_accounts(mocker):
    """A linked PERSONAL account cannot publish and is reported as a setup problem."""
    mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}, account_type="PERSONAL"
    ))
    with pytest.raises(InstagramAccountNotFoundError, match="PERSONAL"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_treats_missing_account_type_as_personal(mocker):
    """An omitted account_type fails at setup time rather than at publish time."""
    page_resp = _mock_response({"instagram_business_account": {"id": _IG_USER_ID, "username": "u"}})
    node_resp = _mock_response({"username": "u"})
    mocker.patch("requests.get", side_effect=[page_resp, node_resp])
    with pytest.raises(InstagramAccountNotFoundError, match="PERSONAL"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_raises_token_error_on_190(mocker):
    """Error code 190 during discovery raises InstagramTokenError."""
    mocker.patch("requests.get", return_value=_error_response(190))
    with pytest.raises(InstagramTokenError):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_raises_on_http_error(mocker):
    """A non-OK HTTP response during discovery raises InstagramUploadError."""
    mocker.patch("requests.get", return_value=_mock_response({}, ok=False, status_code=403))
    with pytest.raises(InstagramUploadError, match="HTTP 403"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_discover_business_account_raises_on_network_error(mocker):
    """A network failure during discovery raises InstagramUploadError."""
    mocker.patch("requests.get", side_effect=requests.exceptions.ConnectionError("down"))
    with pytest.raises(InstagramUploadError, match="discovery request failed"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)


def test_account_not_found_error_is_not_an_upload_error():
    """A setup problem must not be swept up by the retry path's InstagramUploadError catch."""
    assert issubclass(InstagramAccountNotFoundError, RuntimeError)
    assert not issubclass(InstagramAccountNotFoundError, InstagramUploadError)


def test_discover_business_account_never_logs_access_token(mocker, caplog):
    """The Page token never appears in discovery log output."""
    mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}
    ))
    with caplog.at_level("DEBUG"):
        discover_business_account(_ACCESS_TOKEN, _PAGE_ID)
    assert _ACCESS_TOKEN not in caplog.text


def test_step_functions_never_log_access_token(mocker, caplog):
    """None of the three public step functions leak the token into logs."""
    mocker.patch("requests.post", return_value=_mock_response({"id": "x"}))
    mocker.patch("requests.get", return_value=_mock_response({"status_code": "FINISHED"}))
    with caplog.at_level("DEBUG"):
        create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://example.com/v.mp4")
        get_container_status(_ACCESS_TOKEN, "container_1")
        publish_container(_ACCESS_TOKEN, _IG_USER_ID, "container_1")
    assert _ACCESS_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# get_media_permalink — the Graph API media ID is not a shareable URL
# ---------------------------------------------------------------------------

def test_get_media_permalink_returns_the_permalink(mocker):
    """The real, clickable post URL is read back from the API."""
    permalink = "https://www.instagram.com/reel/AbCdEfGhIjK/"
    get = mocker.patch("requests.get", return_value=_mock_response({"permalink": permalink}))
    assert ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1") == permalink
    args, kwargs = get.call_args
    assert args[0].endswith("/media_1")
    assert kwargs["params"]["fields"] == "permalink"
    assert "access_token" not in kwargs["params"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"


def test_get_media_permalink_differs_from_the_media_id(mocker):
    """Guards the actual bug: the permalink is NOT the media ID in a URL template."""
    permalink = "https://www.instagram.com/reel/AbCdEfGhIjK/"
    mocker.patch("requests.get", return_value=_mock_response({"permalink": permalink}))
    result = ig_api.get_media_permalink(_ACCESS_TOKEN, "17999999999999999")
    assert "17999999999999999" not in result


def test_get_media_permalink_raises_when_field_missing(mocker):
    """A response with no permalink is an error, not an empty string."""
    mocker.patch("requests.get", return_value=_mock_response({"id": "media_1"}))
    with pytest.raises(InstagramUploadError, match="missing 'permalink'"):
        ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")


def test_get_media_permalink_raises_token_error_on_190(mocker):
    """Error code 190 during the permalink lookup raises InstagramTokenError."""
    mocker.patch("requests.get", return_value=_error_response(190))
    with pytest.raises(InstagramTokenError):
        ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")


def test_get_media_permalink_raises_on_http_error(mocker):
    """A non-OK HTTP response raises InstagramUploadError."""
    mocker.patch("requests.get", return_value=_mock_response({}, ok=False, status_code=500))
    with pytest.raises(InstagramUploadError, match="HTTP 500"):
        ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")


def test_get_media_permalink_raises_on_network_error(mocker):
    """A network failure raises InstagramUploadError."""
    mocker.patch("requests.get", side_effect=requests.exceptions.ConnectionError("down"))
    with pytest.raises(InstagramUploadError, match="Permalink lookup request failed"):
        ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")


def test_get_media_permalink_never_logs_access_token(mocker, caplog):
    """The token never appears in permalink-lookup log output."""
    mocker.patch("requests.get", return_value=_mock_response({"permalink": "https://x/"}))
    with caplog.at_level("DEBUG"):
        ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")
    assert _ACCESS_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# Credential handling — the token must never be in a URL, and never in an error
# ---------------------------------------------------------------------------
#
# FB_PAGE_ACCESS_TOKEN is a PERMANENT Page token: a leaked copy stays valid until
# a human rotates it, and this repo has already had to do that once (issue #27).
# The leak path these tests close is indirect and easy to miss — a requests
# connection/redirect/timeout exception renders the PREPARED URL including its
# query string, and that exception text is folded into an InstagramUploadError
# which upload_instagram.py then persists to the durable activity log.

_LEAKY_EXC = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries exceeded "
    "with url: /v25.0/node?fields=status_code&access_token="
    + _ACCESS_TOKEN
    + " (Caused by NewConnectionError)"
)


def test_get_container_status_sends_the_token_as_a_header_not_a_query_param(mocker):
    """The primary fix: nothing to leak, because nothing is in the URL."""
    get = mocker.patch("requests.get", return_value=_mock_response({"status_code": "FINISHED"}))
    ig_api.get_container_status(_ACCESS_TOKEN, "container_1")
    _args, kwargs = get.call_args
    assert "access_token" not in kwargs["params"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"


def test_account_type_lookup_sends_the_token_as_a_header_not_a_query_param(mocker):
    """The fourth GET — covered too, so no read path is left carrying a token in a URL."""
    get = mocker.patch("requests.get", side_effect=_discovery_responses(
        {"id": _IG_USER_ID, "username": "my_business_demo"}
    ))
    discover_business_account(_ACCESS_TOKEN, _PAGE_ID)
    _args, kwargs = get.call_args_list[1]        # the account_type node lookup
    assert "access_token" not in kwargs["params"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_ACCESS_TOKEN}"


def test_no_get_in_this_module_puts_a_token_in_the_query_string(mocker):
    """Belt and braces across every read path at once, so a new one cannot regress it."""
    get = mocker.patch("requests.get", side_effect=[
        _mock_response({"status_code": "FINISHED"}),
        _mock_response({"permalink": "https://www.instagram.com/reel/AbCdEfGhIjK/"}),
        *_discovery_responses({"id": _IG_USER_ID, "username": "my_business_demo"}),
    ])
    ig_api.get_container_status(_ACCESS_TOKEN, "container_1")
    ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1")
    discover_business_account(_ACCESS_TOKEN, _PAGE_ID)
    for _args, kwargs in get.call_args_list:
        assert "access_token" not in (kwargs.get("params") or {})


@pytest.mark.parametrize("call", [
    lambda: ig_api.get_container_status(_ACCESS_TOKEN, "container_1"),
    lambda: ig_api.get_media_permalink(_ACCESS_TOKEN, "media_1"),
    lambda: discover_business_account(_ACCESS_TOKEN, _PAGE_ID),
])
def test_a_token_bearing_request_exception_is_redacted_on_every_get(mocker, call):
    """Defence in depth: even if a URL did carry the token, the error would not."""
    mocker.patch("requests.get", side_effect=_LEAKY_EXC)
    with pytest.raises(InstagramUploadError) as excinfo:
        call()
    assert _ACCESS_TOKEN not in str(excinfo.value)
    assert "***REDACTED***" in str(excinfo.value)


@pytest.mark.parametrize("call", [
    lambda: ig_api.create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://vid"),
    lambda: ig_api.publish_container(_ACCESS_TOKEN, _IG_USER_ID, "container_1"),
])
def test_a_token_bearing_request_exception_is_redacted_on_every_post(mocker, call):
    """The POSTs carry the token in the body, but their errors are redacted too."""
    mocker.patch("requests.post", side_effect=_LEAKY_EXC)
    with pytest.raises(InstagramUploadError) as excinfo:
        call()
    assert _ACCESS_TOKEN not in str(excinfo.value)


def test_a_token_echoed_back_in_a_graph_api_error_message_is_redacted(mocker):
    """Meta chooses what to echo in an error body; it is not text we authored."""
    mocker.patch("requests.post", return_value=_mock_response({
        "error": {
            "code": 100,
            "message": f"Invalid request: ?access_token={_ACCESS_TOKEN}",
        }
    }))
    with pytest.raises(InstagramUploadError) as excinfo:
        ig_api.create_media_container(_ACCESS_TOKEN, _IG_USER_ID, "https://vid")
    assert _ACCESS_TOKEN not in str(excinfo.value)


def test_a_token_in_a_malformed_response_body_is_redacted(mocker):
    """The missing-'id' branches interpolate the whole response body into the error."""
    mocker.patch("requests.post", return_value=_mock_response(
        {"debug": f"sent access_token={_ACCESS_TOKEN}"}
    ))
    with pytest.raises(InstagramUploadError) as excinfo:
        ig_api.publish_container(_ACCESS_TOKEN, _IG_USER_ID, "container_1")
    assert _ACCESS_TOKEN not in str(excinfo.value)
    assert "missing 'id'" in str(excinfo.value)


def test_a_token_expiry_error_is_redacted_too(mocker):
    """InstagramTokenError is logged and alerted on exactly like the others."""
    mocker.patch("requests.get", return_value=_mock_response({
        "error": {"code": 190, "message": f"expired for ?access_token={_ACCESS_TOKEN}"}
    }))
    with pytest.raises(InstagramTokenError) as excinfo:
        ig_api.get_container_status(_ACCESS_TOKEN, "container_1")
    assert _ACCESS_TOKEN not in str(excinfo.value)


def test_redaction_keeps_the_error_diagnosable(mocker):
    """An unreadable error is an error people route around."""
    mocker.patch("requests.get", side_effect=_LEAKY_EXC)
    with pytest.raises(InstagramUploadError) as excinfo:
        ig_api.get_container_status(_ACCESS_TOKEN, "container_1")
    message = str(excinfo.value)
    assert "Container status poll request failed" in message
    assert "graph.facebook.com" in message
    assert "fields=status_code" in message
