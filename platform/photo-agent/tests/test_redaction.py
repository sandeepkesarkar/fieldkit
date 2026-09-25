"""
Tests for tools/redaction.py — stripping credentials out of text before it is
logged or persisted.

This module exists because of a REAL incident: issue #27 leaked a token and forced
a live rotation, and FB_PAGE_ACCESS_TOKEN is a permanent Page token that never
expires on its own. So these tests are written as a security contract, not as
formatting tests — each one names the shape a credential can arrive in and asserts
the literal secret is gone afterwards.
"""

import pytest

from tools.redaction import PLACEHOLDER, redact_secrets, redact_value

_TOKEN = "EAABsbCS1iHgBO7ZAZCxyzQWERTY1234567890abcdefGHIJKLmnop"


# ---------------------------------------------------------------------------
# redact_secrets — pattern based, needs no knowledge of the secret
# ---------------------------------------------------------------------------

def test_redacts_an_access_token_in_a_query_string():
    """The exact leak path: a requests exception renders the prepared URL."""
    text = (
        "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries exceeded "
        f"with url: /v25.0/17841400000000000?fields=status_code&access_token={_TOKEN}"
    )
    result = redact_secrets(text)
    assert _TOKEN not in result
    assert PLACEHOLDER in result


def test_redacts_a_token_in_a_urlencoded_form_body():
    """POST bodies carry the token too, and land in exception reprs the same way."""
    result = redact_secrets(f"body: creation_id=container_1&access_token={_TOKEN}")
    assert _TOKEN not in result


def test_redacts_a_bearer_authorization_header():
    """Moving the token to a header does not help if the header itself gets printed."""
    result = redact_secrets(f"{{'Authorization': 'Bearer {_TOKEN}'}}")
    assert _TOKEN not in result
    assert "Bearer" in result  # the shape stays readable; only the value goes


@pytest.mark.parametrize(
    "param", ["access_token", "client_secret", "refresh_token", "appsecret_proof", "api_key"]
)
def test_redacts_every_credential_parameter_name(param):
    """Drive refresh tokens and the Meta app secret are as damaging as the Page token."""
    assert _TOKEN not in redact_secrets(f"?{param}={_TOKEN}")


def test_is_case_insensitive_about_the_parameter_name():
    """A differently-cased parameter is the same credential."""
    assert _TOKEN not in redact_secrets(f"?Access_Token={_TOKEN}")


def test_preserves_the_surrounding_error_text():
    """An operator still has to be able to read what went wrong.

    Redaction that destroyed the error would push people toward disabling it, so the
    URL, the parameter name, and the rest of the message all survive.
    """
    result = redact_secrets(
        f"Connection timed out for /v25.0/media?fields=permalink&access_token={_TOKEN}&x=1"
    )
    assert "Connection timed out" in result
    assert "/v25.0/media" in result
    assert "fields=permalink" in result
    assert "x=1" in result           # the next parameter is not swallowed
    assert _TOKEN not in result


def test_leaves_text_without_secrets_untouched():
    """No false positives on ordinary error text."""
    text = "Container container_99 failed processing: status_code=ERROR"
    assert redact_secrets(text) == text


def test_handles_empty_input():
    """Called on every error string, including empty ones."""
    assert redact_secrets("") == ""


def test_redacts_several_occurrences_at_once():
    """A retry-wrapped exception can carry the same URL more than once."""
    result = redact_secrets(f"first ?access_token={_TOKEN} then ?access_token={_TOKEN}")
    assert _TOKEN not in result
    assert result.count(PLACEHOLDER) == 2


# ---------------------------------------------------------------------------
# redact_value — exact match, for callers that hold the secret
# ---------------------------------------------------------------------------

def test_redacts_a_bare_token_with_no_parameter_name():
    """The case patterns cannot catch: a token printed on its own.

    Some libraries render a token without any surrounding name=value shape, which is
    exactly why the caller-supplied exact match exists alongside the pattern match.
    """
    result = redact_value(f"auth failed for {_TOKEN} at 12:00", _TOKEN)
    assert _TOKEN not in result
    assert PLACEHOLDER in result


def test_ignores_a_missing_secret():
    """A caller with no token in hand must still get usable text back."""
    assert redact_value("some error", None) == "some error"
    assert redact_value("some error", "") == "some error"


def test_refuses_to_substitute_a_dangerously_short_secret():
    """A 1-2 character "secret" would corrupt unrelated text for no security benefit.

    Guards against a misconfigured or truncated env var turning every log line into
    placeholder confetti while protecting nothing.
    """
    assert redact_value("a cat sat on a mat", "a") == "a cat sat on a mat"


def test_the_two_functions_compose():
    """How tools/instagram_api.py actually uses them: pattern first, then exact."""
    text = f"url ?access_token={_TOKEN} and bare {_TOKEN}"
    result = redact_value(redact_secrets(text), _TOKEN)
    assert _TOKEN not in result
