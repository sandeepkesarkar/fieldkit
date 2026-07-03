"""
setup_drive_auth.py — One-time Google Drive OAuth2 setup.

Reads client_id and client_secret from ~/.config/gws/client_secret.json,
opens a browser-based OAuth2 consent flow, exchanges the auth code for a
refresh_token, saves it to ~/.config/gws/user_credentials.json, and
immediately verifies it works.

Usage:
    python3 scripts/setup_drive_auth.py

Run this whenever the stored refresh token stops working (HTTP 400/401 from
drive.py's _get_access_token). It bypasses `gws auth export`, which can export
stale credentials even after a fresh `gws auth login`.
"""

import json
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests

_CLIENT_SECRET_FILE = Path("~/.config/gws/client_secret.json").expanduser()
_USER_CREDENTIALS_FILE = Path("~/.config/gws/user_credentials.json").expanduser()
_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/drive"
_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def _load_client_secret() -> tuple[str, str]:
    """Return (client_id, client_secret) from ~/.config/gws/client_secret.json."""
    if not _CLIENT_SECRET_FILE.exists():
        raise RuntimeError(
            f"Client secret file not found: {_CLIENT_SECRET_FILE}\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )
    try:
        raw = json.loads(_CLIENT_SECRET_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse {_CLIENT_SECRET_FILE}: {exc}") from exc
    cs = raw.get("installed") or raw.get("web") or raw
    try:
        return cs["client_id"], cs["client_secret"]
    except KeyError as exc:
        raise RuntimeError(f"Missing key in {_CLIENT_SECRET_FILE}: {exc}") from exc


def _exchange_code(client_id: str, client_secret: str, code: str) -> str:
    """Exchange OAuth2 auth code for a refresh_token. Returns the refresh_token."""
    resp = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=10,
    )
    if not resp.ok:
        body = resp.json() if resp.content else {}
        raise RuntimeError(
            f"Token exchange failed (HTTP {resp.status_code}): "
            f"{body.get('error_description', body.get('error', resp.text))}"
        )
    tokens = resp.json()
    if "refresh_token" not in tokens:
        raise RuntimeError(
            "No refresh_token in response. Try revoking app access at "
            "https://myaccount.google.com/permissions then re-running."
        )
    return tokens["refresh_token"]


def _verify_refresh_token(client_id: str, client_secret: str, refresh_token: str) -> None:
    """Raise RuntimeError if the refresh_token can't obtain an access_token."""
    resp = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    if not resp.ok:
        body = resp.json() if resp.content else {}
        raise RuntimeError(
            f"Token refresh test failed (HTTP {resp.status_code}): "
            f"{body.get('error_description', body.get('error', resp.text))}"
        )


def main() -> None:
    """Run the OAuth2 browser flow and save credentials."""
    try:
        client_id, client_secret = _load_client_secret()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    params = {
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = _AUTH_URL + "?" + urllib.parse.urlencode(params)

    print("Opening browser for Google OAuth2 authorization...")
    webbrowser.open(auth_url)
    print("\nIf the browser did not open, visit this URL manually:")
    print(auth_url)
    print()

    code = input("Paste the auth code here: ").strip()
    if not code:
        print("ERROR: No auth code provided.", file=sys.stderr)
        sys.exit(1)

    try:
        refresh_token = _exchange_code(client_id, client_secret, code)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "type": "authorized_user",
    }
    _USER_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USER_CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2))

    try:
        _verify_refresh_token(client_id, client_secret, refresh_token)
    except RuntimeError as exc:
        print(f"WARNING: Credentials saved but verification failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Token refresh: OK — saved to {_USER_CREDENTIALS_FILE}")


if __name__ == "__main__":
    main()
