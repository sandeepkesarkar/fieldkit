"""
Tests for tools/telegram_api.py — Telegram Bot API wrapper.

All tests mock requests.post so no real HTTP calls are made.
TELEGRAM_BOT_TOKEN is set via an autouse fixture for every test.

Before issue #49, this module also carried send_message_with_buttons,
get_updates, answer_callback_query, and edit_message_reply_markup (the
inline-button/callback-poll surface) plus a per-call token_env_var override
for a second, dedicated approval-bot token. All of that is retired along
with the button/poller flow it served — send_message is now the module's
only function, always on TELEGRAM_BOT_TOKEN. See git history for the pre-#49
version of this file if that coverage is ever needed for reference.
"""

import requests
from unittest.mock import MagicMock, patch

import pytest

from tools.telegram_api import send_message

_TOKEN = "test_bot_token_123"
_CHAT_ID = "987654321"


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    """Set TELEGRAM_BOT_TOKEN in the environment for every test."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)


def _ok_response(result) -> MagicMock:
    """Mock requests.Response with ok=True and the given result."""
    mock = MagicMock()
    mock.ok = True
    mock.json.return_value = {"ok": True, "result": result}
    return mock


def _http_error_response(status_code: int = 500) -> MagicMock:
    """Mock requests.Response with an HTTP error status and empty JSON body."""
    mock = MagicMock()
    mock.ok = False
    mock.status_code = status_code
    mock.json.return_value = {}
    return mock


def _telegram_error_response(description: str = "Bad Request") -> MagicMock:
    """Mock requests.Response with HTTP 200 but Telegram ok:false payload."""
    mock = MagicMock()
    mock.ok = True
    mock.json.return_value = {"ok": False, "description": description}
    return mock


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

def test_send_message_posts_chat_id_and_text():
    """send_message() POSTs chat_id and text to sendMessage."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 42})
        send_message(_CHAT_ID, "Please review")
    url = mock_post.call_args.args[0]
    body = mock_post.call_args.kwargs["json"]
    assert "sendMessage" in url
    assert body == {"chat_id": _CHAT_ID, "text": "Please review"}


def test_send_message_returns_message_id():
    """send_message() returns the message_id from the Telegram response."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 99})
        result = send_message(_CHAT_ID, "text")
    assert result == 99


def test_send_message_includes_parse_mode_when_provided():
    """send_message() includes parse_mode in the request body when given."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message(_CHAT_ID, "text", parse_mode="Markdown")
    body = mock_post.call_args.kwargs["json"]
    assert body.get("parse_mode") == "Markdown"


def test_send_message_omits_parse_mode_when_not_provided():
    """send_message() omits parse_mode from the body when not explicitly passed."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message(_CHAT_ID, "text")
    body = mock_post.call_args.kwargs["json"]
    assert "parse_mode" not in body


def test_send_message_uses_bot_token_in_url(monkeypatch):
    """send_message() embeds TELEGRAM_BOT_TOKEN in the request URL."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret_token_abc")
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message(_CHAT_ID, "text")
    url = mock_post.call_args.args[0]
    assert "secret_token_abc" in url


def test_send_message_empty_chat_id_raises_runtime_error():
    """send_message() raises RuntimeError immediately when chat_id is empty."""
    with pytest.raises(RuntimeError, match="chat_id must not be empty"):
        send_message("", "text")


def test_send_message_missing_response_message_id_raises_runtime_error():
    """A malformed Telegram response missing result.message_id raises RuntimeError."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({})
        with pytest.raises(RuntimeError, match="missing message_id"):
            send_message(_CHAT_ID, "text")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_http_error_raises_runtime_error():
    """RuntimeError is raised when the HTTP response status is not OK."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _http_error_response(403)
        with pytest.raises(RuntimeError, match="403"):
            send_message(_CHAT_ID, "text")


def test_telegram_ok_false_raises_runtime_error():
    """RuntimeError is raised when the Telegram payload contains ok:false."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _telegram_error_response("chat not found")
        with pytest.raises(RuntimeError, match="chat not found"):
            send_message(_CHAT_ID, "text")


def test_network_error_raises_runtime_error():
    """RuntimeError is raised when a network-level exception occurs."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("network down")
        with pytest.raises(RuntimeError, match="Telegram request failed"):
            send_message(_CHAT_ID, "text")


def test_missing_token_raises_runtime_error(monkeypatch):
    """RuntimeError is raised when TELEGRAM_BOT_TOKEN is not set."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        send_message(_CHAT_ID, "text")


def test_network_error_redacts_token_from_message(monkeypatch):
    """The bot token must not appear in the RuntimeError raised on a connection failure.

    requests exceptions on connection failures embed the full request URL
    (including /bot<TOKEN>/sendMessage) in their string representation. A caller
    that logs the exception verbatim (e.g. a best-effort notification wrapper)
    would otherwise leak the live bot token into logs.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super_secret_token")
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: /botsuper_secret_token/sendMessage"
        )
        with pytest.raises(RuntimeError) as exc_info:
            send_message(_CHAT_ID, "text")
    assert "super_secret_token" not in str(exc_info.value)


def test_malformed_non_dict_response_raises_runtime_error():
    """A response body that is valid JSON but not an object (e.g. Telegram returning
    null or a bare list) raises RuntimeError, not an unhandled AttributeError from
    calling .get() on a non-dict."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock = MagicMock()
        mock.ok = True
        mock.json.return_value = None
        mock_post.return_value = mock
        with pytest.raises(RuntimeError, match="Telegram API error"):
            send_message(_CHAT_ID, "text")
