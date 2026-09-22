"""
redaction.py — Strip credentials out of text before it is logged or persisted.

Exists because of a specific, already-realised failure mode in this repo: issue
#27 leaked a token and required a live rotation. FB_PAGE_ACCESS_TOKEN is a
PERMANENT Page token with no expiry, so a single leaked copy stays valid until a
human notices and rotates it by hand.

The indirect path this guards is the one that is easy to miss. No logging
function here takes a token argument, and no token is passed in subprocess argv
or Telegram text — but a `requests` connection, redirect, or timeout exception
renders the PREPARED URL in its str(), query string included. Fold that
exception into an error message and the credential travels with it into the
activity log, which is durable, or onto a CLI's stdout.

Two independent defences, deliberately both:

  1. Don't put the credential in the URL. tools/instagram_api.py sends the token
     as an Authorization header on every GET, so there is nothing in the query
     string to leak in the first place.
  2. Redact anyway. This module is applied at both the raise site and the
     persistence site, so a credential introduced by some future call site — or
     by a library rendering a request some other way — still does not reach disk.

redact_secrets() is pattern-based and needs no knowledge of the actual secret,
which is what lets tools/instagram_logger.py use it: the logger never sees a
token and must not have to.
"""

import re

PLACEHOLDER = "***REDACTED***"

# Credential-bearing parameters, as they appear in a query string or a urlencoded
# form body. Matched case-insensitively and terminated by any character that
# cannot be part of a parameter value, so a trailing "&foo=bar" or a closing
# quote in an exception's repr survives intact and stays readable.
_SECRET_PARAM_RE = re.compile(
    r"((?:access_token|client_secret|refresh_token|appsecret_proof|api_key)=)"
    r"[^&\s\"'<>)\]}]+",
    re.IGNORECASE,
)

# Authorization: Bearer <token>, in a header dict repr or a rendered request.
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Return text with any embedded credential values replaced by PLACEHOLDER.

    Pattern-based: redacts `access_token=...` and friends wherever they appear
    (query string, form body, exception repr) and `Bearer <token>`. The
    surrounding text — the parameter name, the rest of the URL, the error itself
    — is preserved, because an operator still has to be able to read the error.

    Safe on text that contains no secret: it is returned unchanged.
    """
    if not text:
        return text
    redacted = _SECRET_PARAM_RE.sub(r"\1" + PLACEHOLDER, text)
    return _BEARER_RE.sub(r"\1" + PLACEHOLDER, redacted)


def redact_value(text: str, secret: str | None) -> str:
    """Return text with every literal occurrence of secret replaced by PLACEHOLDER.

    Complements redact_secrets() for the case where the caller actually holds the
    credential and can therefore match it exactly, rather than relying on it
    appearing in a recognised `name=value` shape. A falsy or very short secret is
    ignored — substituting a 1-2 character string would corrupt unrelated text
    for no security benefit.
    """
    if not text or not secret or len(secret) < 8:
        return text
    return text.replace(secret, PLACEHOLDER)
