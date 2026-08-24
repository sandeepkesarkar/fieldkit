"""
Tests for scripts/setup_drive_auth.py — the one-time OAuth2 setup script.

Covers the _SCOPE constant (issue #35: this credential is deliberately shared
between Drive and Gmail-send — check_approval.py's _send_approval_email()
reuses drive.py's _get_access_token() rather than minting a separate Gmail
token) and main()'s propagation of that scope into the auth URL. Network
calls, the browser, and stdin are all mocked — no real HTTP or interaction.
"""

import json
import urllib.parse

import pytest

from scripts.setup_drive_auth import _SCOPE


# ---------------------------------------------------------------------------
# _SCOPE — issue #35: must request both Drive and Gmail-send
# ---------------------------------------------------------------------------

def test_scope_includes_drive():
    """_SCOPE requests the Drive scope needed by tools/drive.py's Drive calls."""
    assert "https://www.googleapis.com/auth/drive" in _SCOPE.split()


def test_scope_includes_gmail_send():
    """_SCOPE requests gmail.send — check_approval.py's _send_approval_email()
    reuses this same credential to send mail via the Gmail API."""
    assert "https://www.googleapis.com/auth/gmail.send" in _SCOPE.split()


def test_scope_is_space_separated_per_oauth2_convention():
    """_SCOPE is a single space-separated string, not a list or comma-joined."""
    scopes = _SCOPE.split(" ")
    assert len(scopes) == 2
    assert "," not in _SCOPE


# ---------------------------------------------------------------------------
# main() — the scope must actually reach the auth URL sent to Google
# ---------------------------------------------------------------------------

def test_main_auth_url_requests_full_scope(mocker, tmp_path):
    """main() builds the OAuth2 auth URL with the full drive+gmail.send scope."""
    import scripts.setup_drive_auth as sda

    client_secret_file = tmp_path / "client_secret.json"
    client_secret_file.write_text(json.dumps({
        "installed": {"client_id": "cid", "client_secret": "csecret"}
    }))
    creds_file = tmp_path / "user_credentials.json"
    mocker.patch.object(sda, "_CLIENT_SECRET_FILE", client_secret_file)
    mocker.patch.object(sda, "_USER_CREDENTIALS_FILE", creds_file)

    mock_open = mocker.patch.object(sda, "webbrowser")
    mocker.patch("builtins.input", return_value="auth_code_123")

    mock_post = mocker.patch("scripts.setup_drive_auth.requests.post")
    mock_post.return_value.ok = True
    mock_post.return_value.json.return_value = {
        "refresh_token": "rtok",
        "access_token": "atok",
    }

    sda.main()

    auth_url = mock_open.open.call_args.args[0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
    assert query["scope"][0] == _SCOPE
    assert "gmail.send" in query["scope"][0]
    assert "drive" in query["scope"][0]
