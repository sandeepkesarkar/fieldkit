"""
Telegram Bot API wrapper for the photo-video agent.

Sends plain-text messages (admin notifications, the approval-request
message) via TELEGRAM_BOT_TOKEN — the same bot token Hermes's own gateway
polls. Before issue #49, this module also carried inline-keyboard buttons,
getUpdates polling, and callback-query handling for a separate, dedicated
TELEGRAM_APPROVAL_BOT_TOKEN; that whole surface is retired now that
approve/reject are plain Hermes commands with no button/poller of their own
— see platform/docs/hermes/10-text-based-approval-migration.md.

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


def send_message(chat_id: str, text: str, parse_mode: str | None = None) -> int:
    """Send a plain-text message to chat_id. Returns the Telegram message_id.

    parse_mode is passed through to Telegram (e.g. "Markdown", "MarkdownV2", "HTML").
    Raises RuntimeError on failure.
    """
    if not chat_id:
        raise RuntimeError("send_message: chat_id must not be empty")
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    logger.debug("send_message: sending")
    try:
        response = requests.post(
            _url("sendMessage"),
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {_redact_token(str(exc))}") from exc
    data = _check(response)
    try:
        message_id: int = data["result"]["message_id"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Telegram response missing message_id: {data!r}") from exc
    logger.info("send_message: message_id=%d", message_id)
    return message_id
