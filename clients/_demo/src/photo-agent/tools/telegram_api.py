"""
Telegram Bot API wrapper for the photo-video agent.

Handles operations that OpenClaw's `message send` cannot perform:
inline keyboards (reply_markup), callback dismissal, and update polling.

Bot token is read from TELEGRAM_BOT_TOKEN in the environment.
All functions raise RuntimeError on HTTP error, network failure, or
non-OK Telegram response.

Security note: the Telegram Bot API embeds the token in the URL path
(https://api.telegram.org/bot{TOKEN}/method). Never enable DEBUG-level
logging on urllib3.connectionpool in production — it logs the full
request line including the token.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


def _token() -> str:
    """Return the bot token from the environment. Raises RuntimeError if unset."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{_token()}/{method}"


def _check(response: requests.Response) -> dict:
    """Raise RuntimeError on HTTP error or Telegram ok:false."""
    if not response.ok:
        try:
            desc = response.json().get("description", "")
        except Exception:
            desc = ""
        detail = f": {desc}" if desc else ""
        raise RuntimeError(f"Telegram HTTP error {response.status_code}{detail}")
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError(f"Telegram returned non-JSON body: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
    return data


def send_message_with_buttons(
    chat_id: str,
    text: str,
    buttons: list[tuple[str, str]],
    parse_mode: str | None = None,
) -> int:
    """Send a message with an inline keyboard. Returns the Telegram message_id.

    buttons is a list of (label, callback_data) pairs rendered as a single keyboard row.
    parse_mode is passed through to Telegram (e.g. "Markdown", "MarkdownV2", "HTML").
    """
    if not buttons:
        raise ValueError("send_message_with_buttons: buttons must not be empty")
    reply_markup = {
        "inline_keyboard": [
            [{"text": label, "callback_data": cb} for label, cb in buttons]
        ]
    }
    payload: dict = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    logger.debug("send_message_with_buttons: sending")
    try:
        response = requests.post(
            _url("sendMessage"),
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    data = _check(response)
    try:
        message_id: int = data["result"]["message_id"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Telegram response missing message_id: {data!r}") from exc
    logger.info("send_message_with_buttons: message_id=%d", message_id)
    return message_id


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Dismiss the spinner on the admin's button tap.

    If text is given (max ~200 chars), Telegram shows a brief toast notification
    on the user's device acknowledging the tap.
    """
    logger.debug("answer_callback_query: callback_query_id=%s", callback_query_id)
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        response = requests.post(_url("answerCallbackQuery"), json=payload, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    _check(response)
    logger.debug("answer_callback_query: ok")


def edit_message_reply_markup(chat_id: str, message_id: int) -> None:
    """Remove all inline keyboard buttons from a message.

    Called immediately after a button tap to prevent the admin from tapping again
    while approval processing is in progress.
    Raises RuntimeError on failure.
    """
    logger.debug("edit_message_reply_markup: chat_id=%s message_id=%d", chat_id, message_id)
    try:
        response = requests.post(
            _url("editMessageReplyMarkup"),
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    _check(response)
    logger.debug("edit_message_reply_markup: ok")


def get_updates(offset: int) -> list[dict]:
    """Poll getUpdates with the given offset. Returns the raw list of update objects.

    The Telegram timeout param is fixed at 0 (return immediately). The requests
    socket timeout (10 s) must exceed the Telegram timeout param to avoid a
    spurious Timeout exception.
    """
    logger.debug("get_updates: offset=%d", offset)
    try:
        response = requests.get(
            _url("getUpdates"),
            params={"offset": offset, "timeout": 0},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    data = _check(response)
    updates: list[dict] = data.get("result", [])
    logger.info("get_updates: offset=%d count=%d", offset, len(updates))
    return updates
