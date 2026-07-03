"""
generate_auth_link.py — One-time admin CLI: authorize FieldKit to post to a Facebook Page.

Usage:
    python3 scripts/generate_auth_link.py --page-id PAGE_ID
    python3 scripts/generate_auth_link.py --page-id PAGE_ID --port 8081

Reads FB_APP_ID and FB_APP_SECRET from .env.
Generates a Facebook OAuth URL, starts a local HTTP server to catch the redirect,
exchanges the code for a permanent Page access token, and writes FB_PAGE_ID and
FB_PAGE_ACCESS_TOKEN back to .env.

Exit codes:
    0  — success, token written
    1  — missing FB_APP_ID or FB_APP_SECRET
    2  — OAuth flow failed (user denied, bad code, network error)
    3  — Page selection failed (page not found or account has no pages)
"""

import argparse
import http.server
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv, set_key

_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import facebook_api
from tools.facebook_api import FacebookUploadError

_log = logging.getLogger(__name__)

_ENV_PATH = _ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env"
_OAUTH_SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]


def main(argv=None) -> None:
    """Entry point — validate env, print auth URL, wait for callback, write token."""
    parser = argparse.ArgumentParser(
        description="Generate a Facebook Page access token and write it to .env."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for the local OAuth callback server (default: 8080).",
    )
    parser.add_argument(
        "--page-id",
        default=None,
        dest="page_id",
        help="Facebook Page ID to link (required if account has multiple Pages).",
    )
    args = parser.parse_args(argv)

    app_id = os.environ.get("FB_APP_ID", "")
    app_secret = os.environ.get("FB_APP_SECRET", "")

    if not app_id:
        print("Error: FB_APP_ID is not set — add it to .env first.", file=sys.stderr)
        sys.exit(1)
    if not app_secret:
        print("Error: FB_APP_SECRET is not set — add it to .env first.", file=sys.stderr)
        sys.exit(1)

    redirect_uri = os.environ.get(
        "FB_REDIRECT_URI", f"http://localhost:{args.port}/callback"
    )

    import secrets as _secrets
    state_token = _secrets.token_hex(16)
    auth_url = facebook_api.build_auth_url(app_id, redirect_uri, _OAUTH_SCOPES, state_token)

    print(f"Facebook authorization URL:\n{auth_url}\n")
    print(f"Waiting for authorization on http://localhost:{args.port}/callback ...")

    try:
        code = _wait_for_oauth_callback(args.port)
    except Exception as exc:
        print(f"OAuth flow failed: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        short_token = facebook_api.exchange_code_for_token(
            code, app_id, app_secret, redirect_uri
        )
        long_token = facebook_api.exchange_for_long_lived_token(short_token, app_id, app_secret)
    except FacebookUploadError as exc:
        print(f"OAuth token exchange failed: {exc}", file=sys.stderr)
        sys.exit(2)

    page_id = args.page_id
    if not page_id:
        page_id = _discover_first_page_id(long_token)

    try:
        page_token = facebook_api.get_page_access_token(long_token, page_id)
    except FacebookUploadError as exc:
        print(f"Page selection failed: {exc}", file=sys.stderr)
        sys.exit(3)

    _write_env_var("FB_PAGE_ID", page_id)
    _write_env_var("FB_PAGE_ACCESS_TOKEN", page_token)

    print("Authorization complete. Page access token written to .env.")
    print(f"Linked Page ID: {page_id}")


def _wait_for_oauth_callback(port: int) -> str:
    """Start a local HTTP server on localhost:port and wait for GET /callback?code=...

    Returns the authorization code.
    Raises RuntimeError on OAuth error or missing code.
    """
    code_holder: list[str] = []
    error_holder: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                code_holder.append(params["code"][0])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"Authorization complete. You can close this window."
                )
            elif "error" in params:
                desc = params.get("error_description", ["unknown"])[0]
                error_holder.append(desc)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed.")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass  # suppress default access log to stdout

    with http.server.HTTPServer(("localhost", port), _Handler) as httpd:
        httpd.handle_request()

    if error_holder:
        raise RuntimeError(f"OAuth error: {error_holder[0]}")
    if not code_holder:
        raise RuntimeError("OAuth callback did not return a code")
    return code_holder[0]


def _discover_first_page_id(long_token: str) -> str:
    """Fetch all Pages via /me/accounts and return the first one's ID.

    Exits with code 3 if the account has no pages.
    """
    import requests as _requests
    try:
        resp = _requests.get(
            "https://graph.facebook.com/v25.0/me/accounts",
            params={"access_token": long_token},
            timeout=30,
        )
    except _requests.exceptions.RequestException as exc:
        print(f"Failed to list Pages: {exc}", file=sys.stderr)
        sys.exit(3)

    if not resp.ok:
        print(f"Failed to list Pages: HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(3)

    pages = resp.json().get("data", [])
    if not pages:
        print("Error: No Facebook Pages found in this account.", file=sys.stderr)
        sys.exit(3)

    first = pages[0]
    print(f"Auto-selected Page: \"{first.get('name', 'Unknown')}\" (ID: {first['id']})")
    return first["id"]


def _write_env_var(key: str, value: str) -> None:
    """Write or update a single key=value pair in .env, preserving all other lines."""
    _ENV_PATH.touch(exist_ok=True)
    set_key(str(_ENV_PATH), key, value, quote_mode="never")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
