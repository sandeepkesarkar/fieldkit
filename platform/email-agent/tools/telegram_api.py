"""
Telegram Bot API wrapper for the email agent.

Bot token is read from TELEGRAM_BOT_TOKEN in the environment.
Raises RuntimeError on HTTP error, network failure, or non-OK Telegram response.

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


def _redact_token(text: str) -> str:
    """Strip the bot token out of a message before it is raised or logged.

    requests exceptions on connection failures embed the full request URL
    (including /bot<TOKEN>/...) in their string representation.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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


def send_message(chat_id: str, text: str) -> None:
    """Send a plain-text message to chat_id. Raises RuntimeError on failure."""
    if not chat_id:
        raise RuntimeError("send_message: chat_id must not be empty")
    logger.debug("send_message: sending")
    try:
        response = requests.post(
            _url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc))}") from exc
    _check(response)
    logger.debug("send_message: ok")
