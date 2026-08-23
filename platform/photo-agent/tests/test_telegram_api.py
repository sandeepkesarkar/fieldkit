"""
Tests for tools/telegram_api.py — Telegram Bot API wrapper.

All tests mock requests.post / requests.get so no real HTTP calls are made.
TELEGRAM_BOT_TOKEN is set via an autouse fixture for every test.
"""

import requests
from unittest.mock import MagicMock, patch

import pytest

from tools.telegram_api import (
    answer_callback_query,
    edit_message_reply_markup,
    get_updates,
    send_message_with_buttons,
)

_TOKEN = "test_bot_token_123"
_CHAT_ID = "987654321"
_BUTTONS = [("✅ Approve", "approve"), ("❌ Reject", "reject")]


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
# send_message_with_buttons
# ---------------------------------------------------------------------------

def test_send_message_constructs_inline_keyboard():
    """send_message_with_buttons() builds a single-row inline keyboard from button pairs."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 42})
        send_message_with_buttons(_CHAT_ID, "Please review", _BUTTONS)
    body = mock_post.call_args.kwargs["json"]
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1
    assert keyboard[0][0] == {"text": "✅ Approve", "callback_data": "approve"}
    assert keyboard[0][1] == {"text": "❌ Reject", "callback_data": "reject"}


def test_send_message_returns_message_id():
    """send_message_with_buttons() returns the message_id from the Telegram response."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 99})
        result = send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)
    assert result == 99


def test_send_message_includes_parse_mode_when_provided():
    """send_message_with_buttons() includes parse_mode in the request body when given."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message_with_buttons(_CHAT_ID, "text", _BUTTONS, parse_mode="Markdown")
    body = mock_post.call_args.kwargs["json"]
    assert body.get("parse_mode") == "Markdown"


def test_send_message_omits_parse_mode_when_not_provided():
    """send_message_with_buttons() omits parse_mode from the body when not explicitly passed."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)
    body = mock_post.call_args.kwargs["json"]
    assert "parse_mode" not in body


def test_send_message_uses_bot_token_in_url(monkeypatch):
    """send_message_with_buttons() embeds TELEGRAM_BOT_TOKEN in the request URL."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret_token_abc")
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response({"message_id": 1})
        send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)
    url = mock_post.call_args.args[0]
    assert "secret_token_abc" in url


def test_send_message_empty_buttons_raises_value_error():
    """send_message_with_buttons() raises ValueError immediately when buttons is empty."""
    with pytest.raises(ValueError, match="buttons must not be empty"):
        send_message_with_buttons(_CHAT_ID, "text", [])


# ---------------------------------------------------------------------------
# answer_callback_query
# ---------------------------------------------------------------------------

def test_answer_callback_query_calls_correct_endpoint():
    """answer_callback_query() POSTs to answerCallbackQuery with the callback_query_id."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response(True)
        answer_callback_query("cq_id_xyz")
    url = mock_post.call_args.args[0]
    assert "answerCallbackQuery" in url
    body = mock_post.call_args.kwargs["json"]
    assert body["callback_query_id"] == "cq_id_xyz"


def test_answer_callback_query_includes_text_when_provided():
    """answer_callback_query() includes 'text' in the body when given."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response(True)
        answer_callback_query("cq_id_xyz", text="✅ Approving...")
    body = mock_post.call_args.kwargs["json"]
    assert body.get("text") == "✅ Approving..."


def test_answer_callback_query_omits_text_when_not_provided():
    """answer_callback_query() omits 'text' from the body when not passed."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response(True)
        answer_callback_query("cq_id_xyz")
    body = mock_post.call_args.kwargs["json"]
    assert "text" not in body


def test_answer_callback_query_http_error_raises_runtime_error():
    """answer_callback_query() raises RuntimeError on HTTP error."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _http_error_response(500)
        with pytest.raises(RuntimeError, match="500"):
            answer_callback_query("cq_id_xyz")


def test_answer_callback_query_telegram_error_raises_runtime_error():
    """answer_callback_query() raises RuntimeError on Telegram ok:false."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _telegram_error_response("query too old")
        with pytest.raises(RuntimeError, match="query too old"):
            answer_callback_query("cq_id_xyz")


# ---------------------------------------------------------------------------
# edit_message_reply_markup
# ---------------------------------------------------------------------------

def test_edit_message_reply_markup_calls_correct_endpoint():
    """edit_message_reply_markup() POSTs to editMessageReplyMarkup with empty inline_keyboard."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _ok_response(True)
        edit_message_reply_markup(_CHAT_ID, 42)
    url = mock_post.call_args.args[0]
    assert "editMessageReplyMarkup" in url
    body = mock_post.call_args.kwargs["json"]
    assert body["chat_id"] == _CHAT_ID
    assert body["message_id"] == 42
    assert body["reply_markup"] == {"inline_keyboard": []}


def test_edit_message_reply_markup_http_error_raises_runtime_error():
    """edit_message_reply_markup() raises RuntimeError on HTTP error."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _http_error_response(400)
        with pytest.raises(RuntimeError, match="400"):
            edit_message_reply_markup(_CHAT_ID, 42)


def test_edit_message_reply_markup_telegram_error_raises_runtime_error():
    """edit_message_reply_markup() raises RuntimeError on Telegram ok:false."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _telegram_error_response("message not modified")
        with pytest.raises(RuntimeError, match="message not modified"):
            edit_message_reply_markup(_CHAT_ID, 42)


def test_edit_message_reply_markup_missing_token_raises_runtime_error(monkeypatch):
    """edit_message_reply_markup() raises RuntimeError when TELEGRAM_BOT_TOKEN is not set."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        edit_message_reply_markup(_CHAT_ID, 42)


# ---------------------------------------------------------------------------
# get_updates
# ---------------------------------------------------------------------------

def test_get_updates_passes_offset():
    """get_updates() passes offset and timeout=0 as query parameters."""
    with patch("tools.telegram_api.requests.get") as mock_get:
        mock_get.return_value = _ok_response([])
        get_updates(42)
    params = mock_get.call_args.kwargs["params"]
    assert params["offset"] == 42
    assert params["timeout"] == 0


def test_get_updates_returns_update_list():
    """get_updates() returns the raw list of update objects from the response."""
    updates = [{"update_id": 1, "callback_query": {}}, {"update_id": 2}]
    with patch("tools.telegram_api.requests.get") as mock_get:
        mock_get.return_value = _ok_response(updates)
        result = get_updates(0)
    assert result == updates


def test_get_updates_returns_empty_list_when_no_updates():
    """get_updates() returns an empty list when the Telegram result is empty."""
    with patch("tools.telegram_api.requests.get") as mock_get:
        mock_get.return_value = _ok_response([])
        result = get_updates(0)
    assert result == []


def test_get_updates_http_error_raises_runtime_error():
    """get_updates() raises RuntimeError on HTTP error."""
    with patch("tools.telegram_api.requests.get") as mock_get:
        mock_get.return_value = _http_error_response(429)
        with pytest.raises(RuntimeError, match="429"):
            get_updates(0)


def test_get_updates_telegram_error_raises_runtime_error():
    """get_updates() raises RuntimeError on Telegram ok:false."""
    with patch("tools.telegram_api.requests.get") as mock_get:
        mock_get.return_value = _telegram_error_response("Unauthorized")
        with pytest.raises(RuntimeError, match="Unauthorized"):
            get_updates(0)


# ---------------------------------------------------------------------------
# Error handling — shared
# ---------------------------------------------------------------------------

def test_http_error_raises_runtime_error():
    """RuntimeError is raised when the HTTP response status is not OK."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _http_error_response(403)
        with pytest.raises(RuntimeError, match="403"):
            send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)


def test_telegram_ok_false_raises_runtime_error():
    """RuntimeError is raised when the Telegram payload contains ok:false."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.return_value = _telegram_error_response("chat not found")
        with pytest.raises(RuntimeError, match="chat not found"):
            send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)


def test_network_error_raises_runtime_error():
    """RuntimeError is raised when a network-level exception occurs."""
    with patch("tools.telegram_api.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("network down")
        with pytest.raises(RuntimeError, match="Telegram request failed"):
            send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)


def test_missing_token_raises_runtime_error(monkeypatch):
    """RuntimeError is raised when TELEGRAM_BOT_TOKEN is not set."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)


def test_missing_token_raises_for_answer_callback_query(monkeypatch):
    """RuntimeError is raised when TELEGRAM_BOT_TOKEN is not set for answer_callback_query."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        answer_callback_query("cq_id")


def test_missing_token_raises_for_get_updates(monkeypatch):
    """RuntimeError is raised when TELEGRAM_BOT_TOKEN is not set for get_updates."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        get_updates(0)


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
            send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)
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
            send_message_with_buttons(_CHAT_ID, "text", _BUTTONS)
