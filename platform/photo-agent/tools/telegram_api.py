"""
Telegram Bot API wrapper for the photo-video agent.

Handles every Telegram operation the photo-agent scripts need: plain-text
messages, inline keyboards (reply_markup), callback dismissal, and update
polling.

Bot token is read from TELEGRAM_BOT_TOKEN in the environment by default.
Every function accepts an optional token_env_var to read a different bot
token instead — used by check_approval.py's button-callback flow to poll a
second, dedicated bot (TELEGRAM_APPROVAL_BOT_TOKEN) so its getUpdates offset
never shares a token with Hermes's own continuous long-poll (issue #29: the
two would otherwise race for the same offset, and Hermes wins every time).

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


def _token(token_env_var: str = "TELEGRAM_BOT_TOKEN") -> str:
    """Return the bot token from the environment. Raises RuntimeError if unset."""
    token = os.environ.get(token_env_var, "")
    if not token:
        raise RuntimeError(f"{token_env_var} is not set")
    return token


def _url(method: str, token_env_var: str = "TELEGRAM_BOT_TOKEN") -> str:
    return f"https://api.telegram.org/bot{_token(token_env_var)}/{method}"


def _redact_token(text: str, token_env_var: str = "TELEGRAM_BOT_TOKEN") -> str:
    """Strip the bot token out of a message before it is raised or logged.

    requests exceptions on connection failures embed the full request URL
    (including /bot<TOKEN>/...) in their string representation.
    """
    token = os.environ.get(token_env_var, "")
    return text.replace(token, "***REDACTED***") if token else text


def _check(response: requests.Response) -> dict:
    """Raise RuntimeError on HTTP error, a malformed body, or Telegram ok:false."""
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
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data!r}")
    return data


def send_message(chat_id: str, text: str, token_env_var: str = "TELEGRAM_BOT_TOKEN") -> None:
    """Send a plain-text message to chat_id. Raises RuntimeError on failure."""
    if not chat_id:
        raise RuntimeError("send_message: chat_id must not be empty")
    logger.debug("send_message: sending")
    try:
        response = requests.post(
            _url("sendMessage", token_env_var),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc), token_env_var)}") from exc
    _check(response)
    logger.debug("send_message: ok")


def send_message_with_buttons(
    chat_id: str,
    text: str,
    buttons: list[tuple[str, str]],
    parse_mode: str | None = None,
    token_env_var: str = "TELEGRAM_BOT_TOKEN",
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
            _url("sendMessage", token_env_var),
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc), token_env_var)}") from exc
    data = _check(response)
    try:
        message_id: int = data["result"]["message_id"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Telegram response missing message_id: {data!r}") from exc
    logger.info("send_message_with_buttons: message_id=%d", message_id)
    return message_id


def answer_callback_query(
    callback_query_id: str, text: str = "", token_env_var: str = "TELEGRAM_BOT_TOKEN"
) -> None:
    """Dismiss the spinner on the admin's button tap.

    If text is given (max ~200 chars), Telegram shows a brief toast notification
    on the user's device acknowledging the tap.
    """
    logger.debug("answer_callback_query: callback_query_id=%s", callback_query_id)
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        response = requests.post(_url("answerCallbackQuery", token_env_var), json=payload, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc), token_env_var)}") from exc
    _check(response)
    logger.debug("answer_callback_query: ok")


def edit_message_reply_markup(
    chat_id: str, message_id: int, token_env_var: str = "TELEGRAM_BOT_TOKEN"
) -> None:
    """Remove all inline keyboard buttons from a message.

    Called immediately after a button tap to prevent the admin from tapping again
    while approval processing is in progress.
    Raises RuntimeError on failure.
    """
    logger.debug("edit_message_reply_markup: message_id=%d", message_id)
    try:
        response = requests.post(
            _url("editMessageReplyMarkup", token_env_var),
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc), token_env_var)}") from exc
    _check(response)
    logger.debug("edit_message_reply_markup: ok")


def get_updates(offset: int, token_env_var: str = "TELEGRAM_BOT_TOKEN") -> list[dict]:
    """Poll getUpdates with the given offset. Returns the raw list of update objects.

    The Telegram timeout param is fixed at 0 (return immediately). The requests
    socket timeout (10 s) must exceed the Telegram timeout param to avoid a
    spurious Timeout exception.
    """
    logger.debug("get_updates: offset=%d", offset)
    try:
        response = requests.get(
            _url("getUpdates", token_env_var),
            params={"offset": offset, "timeout": 0},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc), token_env_var)}") from exc
    data = _check(response)
    updates: list[dict] = data.get("result", [])
    logger.info("get_updates: offset=%d count=%d", offset, len(updates))
    return updates
