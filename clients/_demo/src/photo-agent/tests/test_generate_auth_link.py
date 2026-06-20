"""
Tests for scripts/generate_auth_link.py — the one-time Facebook OAuth admin CLI.

Covers: auth URL scope/app_id/redirect_uri content, code exchange call order,
.env write (add/update without removing existing vars), missing env var exits,
no Page found exit, and --page-id selection.

All network calls and the HTTP server are mocked. No real OAuth flow.
"""

import os

import pytest

from scripts.generate_auth_link import main

_APP_ID = "my_app_id"
_APP_SECRET = "my_app_secret"  # noqa: S106 — test credential, not real
_PAGE_ID = "111222333"
_PAGE_NAME = "My Test Page"
_SHORT_TOKEN = "short_token_abc"
_LONG_TOKEN = "long_token_xyz"
_PAGE_TOKEN = "permanent_page_token_123"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FB_APP_ID", _APP_ID)
    monkeypatch.setenv("FB_APP_SECRET", _APP_SECRET)


@pytest.fixture
def base(mocker, env, tmp_path):
    """Common mocks: server callback returns code, token chain succeeds, .env in tmp_path."""
    import scripts.generate_auth_link as gal
    mocker.patch.object(gal, "_wait_for_oauth_callback", return_value="test_auth_code")
    mocker.patch.object(gal.facebook_api, "exchange_code_for_token", return_value=_SHORT_TOKEN)
    mocker.patch.object(gal.facebook_api, "exchange_for_long_lived_token", return_value=_LONG_TOKEN)
    mocker.patch.object(gal.facebook_api, "get_page_access_token", return_value=_PAGE_TOKEN)
    # Point .env writes at a temp file
    env_path = tmp_path / ".env"
    mocker.patch.object(gal, "_ENV_PATH", env_path)
    return mocker


# ---------------------------------------------------------------------------
# Auth URL content
# ---------------------------------------------------------------------------

def test_auth_url_contains_required_scopes(base, capsys):
    """The printed auth URL includes pages_show_list, pages_read_engagement, pages_manage_posts."""
    main(["--page-id", _PAGE_ID])
    out = capsys.readouterr().out
    for scope in ("pages_show_list", "pages_read_engagement", "pages_manage_posts"):
        assert scope in out, f"Missing scope in URL: {scope}"


def test_auth_url_contains_app_id(base, capsys):
    """The printed auth URL includes the app_id."""
    main(["--page-id", _PAGE_ID])
    out = capsys.readouterr().out
    assert _APP_ID in out


def test_auth_url_contains_redirect_uri(base, capsys):
    """The printed auth URL includes the redirect URI (localhost:8080/callback by default)."""
    main(["--page-id", _PAGE_ID])
    out = capsys.readouterr().out
    assert "localhost" in out or "callback" in out


# ---------------------------------------------------------------------------
# Code exchange call order
# ---------------------------------------------------------------------------

def test_code_exchange_calls_in_order(base):
    """exchange_code_for_token → exchange_for_long_lived_token → get_page_access_token."""
    import scripts.generate_auth_link as gal
    call_order = []
    gal.facebook_api.exchange_code_for_token.side_effect = lambda *a, **kw: (
        call_order.append("exchange_code") or _SHORT_TOKEN
    )
    gal.facebook_api.exchange_for_long_lived_token.side_effect = lambda *a, **kw: (
        call_order.append("exchange_long") or _LONG_TOKEN
    )
    gal.facebook_api.get_page_access_token.side_effect = lambda *a, **kw: (
        call_order.append("get_page_token") or _PAGE_TOKEN
    )
    main(["--page-id", _PAGE_ID])
    assert call_order == ["exchange_code", "exchange_long", "get_page_token"]


def test_exchange_code_receives_auth_code(base):
    """exchange_code_for_token is called with the code from the OAuth callback."""
    import scripts.generate_auth_link as gal
    main(["--page-id", _PAGE_ID])
    call = gal.facebook_api.exchange_code_for_token.call_args
    assert call.args[0] == "test_auth_code"


def test_exchange_long_receives_short_token(base):
    """exchange_for_long_lived_token is called with the short-lived token."""
    import scripts.generate_auth_link as gal
    main(["--page-id", _PAGE_ID])
    call = gal.facebook_api.exchange_for_long_lived_token.call_args
    assert call.args[0] == _SHORT_TOKEN


def test_get_page_access_token_receives_long_token_and_page_id(base):
    """get_page_access_token is called with the long-lived token and the page_id."""
    import scripts.generate_auth_link as gal
    main(["--page-id", _PAGE_ID])
    call = gal.facebook_api.get_page_access_token.call_args
    assert call.args[0] == _LONG_TOKEN
    assert call.args[1] == _PAGE_ID


# ---------------------------------------------------------------------------
# .env write
# ---------------------------------------------------------------------------

def test_env_write_adds_fb_page_id(base, tmp_path):
    """After success, FB_PAGE_ID is written to .env."""
    import scripts.generate_auth_link as gal
    main(["--page-id", _PAGE_ID])
    content = gal._ENV_PATH.read_text()
    assert f"FB_PAGE_ID={_PAGE_ID}" in content


def test_env_write_adds_fb_page_access_token(base, tmp_path):
    """After success, FB_PAGE_ACCESS_TOKEN is written to .env."""
    import scripts.generate_auth_link as gal
    main(["--page-id", _PAGE_ID])
    content = gal._ENV_PATH.read_text()
    assert f"FB_PAGE_ACCESS_TOKEN={_PAGE_TOKEN}" in content


def test_env_write_preserves_existing_vars(base, tmp_path):
    """Existing .env variables are not removed when FB vars are written."""
    import scripts.generate_auth_link as gal
    # Pre-populate .env with existing vars
    gal._ENV_PATH.write_text(
        "TELEGRAM_BOT_TOKEN=existing_token\nADMIN_EMAIL=admin@example.com\n"
    )
    main(["--page-id", _PAGE_ID])
    content = gal._ENV_PATH.read_text()
    assert "TELEGRAM_BOT_TOKEN=existing_token" in content
    assert "ADMIN_EMAIL=admin@example.com" in content


def test_env_write_updates_existing_fb_page_id(base, tmp_path):
    """If FB_PAGE_ID already exists in .env, it is updated rather than duplicated."""
    import scripts.generate_auth_link as gal
    gal._ENV_PATH.write_text("FB_PAGE_ID=old_page_id\n")
    main(["--page-id", _PAGE_ID])
    content = gal._ENV_PATH.read_text()
    assert f"FB_PAGE_ID={_PAGE_ID}" in content
    assert "old_page_id" not in content


# ---------------------------------------------------------------------------
# Env var validation
# ---------------------------------------------------------------------------

def test_missing_fb_app_id_exits_1(monkeypatch):
    """Missing FB_APP_ID causes sys.exit(1)."""
    monkeypatch.delenv("FB_APP_ID", raising=False)
    monkeypatch.setenv("FB_APP_SECRET", _APP_SECRET)
    with pytest.raises(SystemExit) as exc_info:
        main(["--page-id", _PAGE_ID])
    assert exc_info.value.code == 1


def test_missing_fb_app_secret_exits_1(monkeypatch):
    """Missing FB_APP_SECRET causes sys.exit(1)."""
    monkeypatch.setenv("FB_APP_ID", _APP_ID)
    monkeypatch.delenv("FB_APP_SECRET", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main(["--page-id", _PAGE_ID])
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Page selection failures
# ---------------------------------------------------------------------------

def test_no_page_found_exits_3(base):
    """FacebookUploadError from get_page_access_token causes sys.exit(3)."""
    import scripts.generate_auth_link as gal
    from tools.facebook_api import FacebookUploadError
    gal.facebook_api.get_page_access_token.side_effect = FacebookUploadError("Page not found")
    with pytest.raises(SystemExit) as exc_info:
        main(["--page-id", "nonexistent_page"])
    assert exc_info.value.code == 3


def test_page_id_arg_selects_correct_page(base):
    """--page-id passes the given page ID to get_page_access_token."""
    import scripts.generate_auth_link as gal
    main(["--page-id", "999888777"])
    call = gal.facebook_api.get_page_access_token.call_args
    assert call.args[1] == "999888777"


# ---------------------------------------------------------------------------
# Success confirmation output
# ---------------------------------------------------------------------------

def test_success_prints_confirmation(base, capsys):
    """After success, the output includes a confirmation message."""
    main(["--page-id", _PAGE_ID])
    out = capsys.readouterr().out
    assert "complete" in out.lower() or "written" in out.lower() or "token" in out.lower()
