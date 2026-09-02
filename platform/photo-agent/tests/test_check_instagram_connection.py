"""
Tests for scripts/check_instagram_connection.py — the one-time Instagram
connection admin CLI (US2).

Covers: successful discovery writes IG_BUSINESS_ACCOUNT_ID to .env without
disturbing existing vars, the two actionable not-eligible messages (nothing
linked / PERSONAL account) exiting 3, env-var validation exiting 1, and
--page-id overriding the .env value.

All network calls are mocked and .env writes are redirected to tmp_path. No
real Graph API call and no write to a real client .env is possible here.
"""

import pytest

from scripts.check_instagram_connection import main
from tools.instagram_api import (
    InstagramAccountNotFoundError,
    InstagramTokenError,
    InstagramUploadError,
)

_PAGE_ID = "123456789"
_PAGE_TOKEN = "page_token_abc"
_IG_ACCOUNT_ID = "17841400000000000"
_USERNAME = "my_business_demo"

_EXISTING_ENV = (
    "FB_APP_ID=existing_app\n"
    "FB_PAGE_ID=123456789\n"
    "FB_PAGE_ACCESS_TOKEN=page_token_abc\n"
    "TELEGRAM_BOT_TOKEN=bot_token\n"
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _PAGE_TOKEN)
    monkeypatch.setenv("FB_PAGE_ID", _PAGE_ID)


@pytest.fixture
def base(mocker, env, tmp_path):
    """Discovery succeeds with a BUSINESS account; .env writes go to tmp_path."""
    import scripts.check_instagram_connection as cic
    mocker.patch.object(
        cic.instagram_api,
        "discover_business_account",
        return_value={"id": _IG_ACCOUNT_ID, "username": _USERNAME, "account_type": "BUSINESS"},
    )
    env_path = tmp_path / ".env"
    env_path.write_text(_EXISTING_ENV)
    mocker.patch.object(cic, "_ENV_PATH", env_path)
    return mocker


def _env_vars(path) -> dict:
    vars_ = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vars_[k.strip()] = v.strip()
    return vars_


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------

def test_success_exits_zero(base):
    """A linked BUSINESS account is a clean success."""
    assert main([]) in (None, 0)


def test_success_writes_account_id_to_env(base, tmp_path):
    """IG_BUSINESS_ACCOUNT_ID is written to the client's .env (FR-006)."""
    main([])
    assert _env_vars(tmp_path / ".env")["IG_BUSINESS_ACCOUNT_ID"] == _IG_ACCOUNT_ID


def test_success_preserves_existing_env_vars(base, tmp_path):
    """Every pre-existing var survives the write."""
    main([])
    vars_ = _env_vars(tmp_path / ".env")
    assert vars_["FB_APP_ID"] == "existing_app"
    assert vars_["FB_PAGE_ID"] == _PAGE_ID
    assert vars_["FB_PAGE_ACCESS_TOKEN"] == _PAGE_TOKEN
    assert vars_["TELEGRAM_BOT_TOKEN"] == "bot_token"


def test_success_prints_username_and_account_type(base, capsys):
    """FR-005: the admin sees which account was linked."""
    main([])
    out = capsys.readouterr().out
    assert f"@{_USERNAME}" in out
    assert _IG_ACCOUNT_ID in out
    assert "BUSINESS" in out


def test_success_discovers_using_the_page_token(base):
    """Discovery reuses FB_PAGE_ACCESS_TOKEN — no new credential is introduced."""
    import scripts.check_instagram_connection as cic
    main([])
    cic.instagram_api.discover_business_account.assert_called_once_with(_PAGE_TOKEN, _PAGE_ID)


def test_success_updates_an_existing_account_id(base, tmp_path):
    """Re-running against an already-configured client overwrites, not duplicates."""
    (tmp_path / ".env").write_text(_EXISTING_ENV + "IG_BUSINESS_ACCOUNT_ID=old_value\n")
    main([])
    content = (tmp_path / ".env").read_text()
    assert content.count("IG_BUSINESS_ACCOUNT_ID") == 1
    assert _env_vars(tmp_path / ".env")["IG_BUSINESS_ACCOUNT_ID"] == _IG_ACCOUNT_ID


def test_creator_account_is_accepted(base, capsys):
    """A CREATOR account is equally publishable."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.return_value = {
        "id": _IG_ACCOUNT_ID, "username": _USERNAME, "account_type": "CREATOR",
    }
    main([])
    assert "CREATOR" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --page-id override
# ---------------------------------------------------------------------------

def test_page_id_argument_overrides_env(base):
    """--page-id takes precedence over FB_PAGE_ID from .env."""
    import scripts.check_instagram_connection as cic
    main(["--page-id", "999888777"])
    cic.instagram_api.discover_business_account.assert_called_once_with(_PAGE_TOKEN, "999888777")


def test_page_id_argument_works_without_env_page_id(base, monkeypatch):
    """--page-id alone is enough — FB_PAGE_ID need not be set."""
    import scripts.check_instagram_connection as cic
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    main(["--page-id", "999888777"])
    cic.instagram_api.discover_business_account.assert_called_once_with(_PAGE_TOKEN, "999888777")


# ---------------------------------------------------------------------------
# Exit 3 — no eligible account (FR-005)
# ---------------------------------------------------------------------------

def test_no_linked_account_exits_3(base):
    """InstagramAccountNotFoundError is a setup problem, exit code 3."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramAccountNotFoundError(
        "No Instagram professional account is linked to Facebook Page 123456789"
    )
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 3


def test_no_linked_account_prints_actionable_guidance(base, capsys):
    """The admin is told exactly what to do, not shown a stack trace."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramAccountNotFoundError(
        "No Instagram professional account is linked to Facebook Page 123456789"
    )
    with pytest.raises(SystemExit):
        main([])
    out = capsys.readouterr().out
    assert "No Instagram account is linked" in out
    assert "Account Settings" in out
    assert "https://www.facebook.com/business/help/" in out
    assert "Traceback" not in out


def test_personal_account_exits_3(base):
    """A PERSONAL account cannot publish — exit 3, same as nothing linked."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramAccountNotFoundError(
        f"Instagram account @{_USERNAME} (ID {_IG_ACCOUNT_ID}) is a PERSONAL account; "
        "a Business or Creator account is required to publish"
    )
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 3


def test_personal_account_prints_convert_guidance(base, capsys):
    """The PERSONAL case gets its own message: convert the account."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramAccountNotFoundError(
        f"Instagram account @{_USERNAME} (ID {_IG_ACCOUNT_ID}) is a PERSONAL account; "
        "a Business or Creator account is required to publish"
    )
    with pytest.raises(SystemExit):
        main([])
    out = capsys.readouterr().out
    assert "PERSONAL" in out
    assert "Convert it to a Business or Creator account" in out
    assert "Settings > Account type" in out


def test_failed_discovery_does_not_write_env(base, tmp_path):
    """Nothing is written to .env unless an eligible account was actually found."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramAccountNotFoundError("none")
    with pytest.raises(SystemExit):
        main([])
    assert "IG_BUSINESS_ACCOUNT_ID" not in (tmp_path / ".env").read_text()


# ---------------------------------------------------------------------------
# Exit 1 — environment misconfiguration
# ---------------------------------------------------------------------------

def test_missing_page_token_exits_1(base, monkeypatch):
    """FB_PAGE_ACCESS_TOKEN is required (from Feature 003)."""
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


def test_missing_page_id_exits_1(base, monkeypatch):
    """FB_PAGE_ID is required unless --page-id is given."""
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


def test_missing_env_does_not_call_the_api(base, monkeypatch):
    """Validation happens before any network call."""
    import scripts.check_instagram_connection as cic
    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main([])
    cic.instagram_api.discover_business_account.assert_not_called()


# ---------------------------------------------------------------------------
# Other API failures
# ---------------------------------------------------------------------------

def test_token_error_exits_1_with_reconnect_guidance(base, capsys):
    """An expired token is an environment problem (exit 1), not a linkage problem."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramTokenError("expired")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1
    # Error diagnostics go to stderr, matching generate_auth_link.py's convention;
    # only the guidance for the two exit-3 setup problems is written to stdout.
    captured = capsys.readouterr()
    assert "token" in captured.err.lower()
    assert "generate_auth_link.py" in captured.err


def test_transient_api_error_exits_1(base):
    """A network/API failure exits nonzero rather than pretending success."""
    import scripts.check_instagram_connection as cic
    cic.instagram_api.discover_business_account.side_effect = InstagramUploadError("HTTP 500")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


def test_no_token_value_is_ever_printed(base, capsys):
    """The Page token must never reach stdout."""
    main([])
    assert _PAGE_TOKEN not in capsys.readouterr().out
