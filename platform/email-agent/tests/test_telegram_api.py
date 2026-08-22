"""
Tests for tools/telegram_api.py — Telegram Bot API wrapper (email agent).

All tests mock requests.post so no real HTTP call is ever made.
TELEGRAM_BOT_TOKEN is set via an autouse fixture for every test.
"""

from unittest.mock import MagicMock, patch

import pytest

from tools.telegram_api import send_message

_TOKEN = "test_bot_token_123"
_CHAT_ID = "987654321"


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    """Set TELEGRAM_BOT_TOKEN in the environment for every test."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)


def _ok_response() -> MagicMock:
    mock = MagicMock()
    mock.ok = True
    mock.json.return_value = {"ok": True, "result": {"message_id": 1}}
    return mock


def _http_error_response(status_code: int = 500) -> MagicMock:
    mock = MagicMock()
    mock.ok = False
    mock.status_code = status_code
    mock.json.return_value = {}
    return mock


def _telegram_error_response(description: str = "Bad Request") -> MagicMock:
    mock = MagicMock()
    mock.ok = True
    mock.json.return_value = {"ok": False, "description": description}
    return mock


def test_send_message_posts_chat_id_and_text():
    """send_message() POSTs the given chat_id and text to sendMessage."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(_CHAT_ID, "hello")
    body = mock_post.call_args.kwargs["json"]
    assert body == {"chat_id": _CHAT_ID, "text": "hello"}


def test_send_message_uses_bot_token_in_url(monkeypatch):
    """send_message() embeds TELEGRAM_BOT_TOKEN in the request URL."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret_token_abc")
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response()
        send_message(_CHAT_ID, "hello")
    url = mock_post.call_args.args[0]
    assert "secret_token_abc" in url


def test_send_message_empty_chat_id_raises_without_request():
    """send_message() raises RuntimeError for an empty chat_id without making a request."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        with pytest.raises(RuntimeError, match="chat_id must not be empty"):
            send_message("", "hello")
        mock_post.assert_not_called()


def test_send_message_raises_on_http_error():
    """send_message() raises RuntimeError on a non-OK HTTP response."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _http_error_response(500)
        with pytest.raises(RuntimeError, match="Telegram HTTP error 500"):
            send_message(_CHAT_ID, "hello")


def test_send_message_raises_on_telegram_api_error():
    """send_message() raises RuntimeError when Telegram responds with ok:false."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _telegram_error_response("chat not found")
        with pytest.raises(RuntimeError, match="chat not found"):
            send_message(_CHAT_ID, "hello")


def test_send_message_raises_on_network_failure():
    """send_message() wraps a requests exception in RuntimeError."""
    import requests

    with patch("tools.telegram_api.requests.post", side_effect=requests.exceptions.ConnectionError("boom")):
        with pytest.raises(RuntimeError, match="Telegram request failed"):
            send_message(_CHAT_ID, "hello")


def test_send_message_raises_when_token_not_set(monkeypatch):
    """send_message() raises RuntimeError when TELEGRAM_BOT_TOKEN is not set."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        send_message(_CHAT_ID, "hello")
