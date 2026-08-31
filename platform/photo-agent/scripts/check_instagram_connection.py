"""
check_instagram_connection.py — One-time admin CLI: link a client's Instagram account.

Usage:
    CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py
    CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py --page-id 123456789

Discovers the Instagram professional account linked to the client's already-connected
Facebook Page (Feature 003) and writes its ID to that client's .env as
IG_BUSINESS_ACCOUNT_ID, which is what enables Instagram publishing for that client.

There is deliberately no OAuth flow here, unlike generate_auth_link.py. An Instagram
account must already be converted to Business/Creator and linked to a Facebook Page
before the Graph API can publish to it at all; given that, the Page access token
FieldKit already holds is sufficient to discover and publish to it. This feature
therefore adds NO new secret: IG_BUSINESS_ACCOUNT_ID is a public account identifier,
and every API call reuses FB_PAGE_ACCESS_TOKEN.

Run once per client, by the administrator. The business owner takes no separate
action beyond what Feature 003 already required.

Exit codes:
    0 — success; IG_BUSINESS_ACCOUNT_ID written to .env
    1 — environment misconfiguration (missing FB_PAGE_ACCESS_TOKEN / FB_PAGE_ID /
        CLIENT_NAME), or the token is expired, or the Graph API call failed
    3 — no eligible Instagram account found: nothing linked to the Page, or the
        linked account is PERSONAL rather than Business/Creator. Both print
        actionable guidance rather than a stack trace (FR-005).
"""

import argparse
import fcntl
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# CLIENT_NAME resolution order (issue #45): a CLIENT_NAME already present in
# the process environment when this script starts (e.g. `env CLIENT_NAME=foo
# python3 ...` on a crontab line, or an inline override on a manual
# invocation) wins over the root .env's CLIENT_NAME, because
# load_dotenv(_ROOT / ".env") below passes override=False EXPLICITLY —
# this repo owns that contract rather than leaning on python-dotenv's
# current default (unpinned in requirements.txt), so it never clobbers an
# already-set env var regardless of what a future dependency upgrade does.
# This override remains available as an ad-hoc, single-invocation escape
# hatch (a manual test run against a client other than the one currently
# installed, without disturbing it) — it does NOT support running multiple
# clients' cron/gateway flows concurrently as a matter of policy. That
# concurrent-multi-client design (per-client Hermes profiles, per-cron-entry
# overrides) was retired by issue #61: this fieldkit install runs exactly
# ONE client at a time, switched via
# platform/photo-agent/scripts/install_client.sh, which is what keeps this
# CLIENT_NAME resolution's fallback-to-root-.env branch always correct — it
# was the concurrent-profile design itself that caused issue #59, not a gap
# in this resolution order. See platform/docs/hermes/09-per-client-model-profiles.md.
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env", override=False)
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)
# The client .env above loads with override=True. If it ever defines its
# own CLIENT_NAME (it shouldn't — see platform/photo-agent/.env.example),
# that would silently clobber the value resolved above. Re-assert it so
# os.environ["CLIENT_NAME"] always matches _CLIENT afterward, including
# for anything this process later shells out to.
os.environ["CLIENT_NAME"] = _CLIENT

sys.path.insert(0, str(Path(__file__).parents[1]))

from dotenv import set_key

from tools import instagram_api
from tools.instagram_api import (
    InstagramAccountNotFoundError,
    InstagramTokenError,
    InstagramUploadError,
)

_log = logging.getLogger(__name__)

_ENV_PATH = _ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env"

_LINK_HELP_URL = "https://www.facebook.com/business/help/898752960195806"


def main(argv=None) -> None:
    """Discover the linked Instagram account and write its ID to the client's .env."""
    parser = argparse.ArgumentParser(
        description="Check for an Instagram account linked to this client's Facebook Page."
    )
    parser.add_argument(
        "--page-id",
        dest="page_id",
        default=None,
        help="Facebook Page ID to check (default: FB_PAGE_ID from the client's .env).",
    )
    args = parser.parse_args(argv)

    page_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    page_id = args.page_id or os.environ.get("FB_PAGE_ID", "")

    if not page_token:
        print(
            "Error: FB_PAGE_ACCESS_TOKEN is not set — run generate_auth_link.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not page_id:
        print(
            "Error: FB_PAGE_ID is not set — run generate_auth_link.py first, "
            "or pass --page-id.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Checking Facebook Page {page_id} for a linked Instagram account...")

    try:
        account = instagram_api.discover_business_account(page_token, page_id)
    except InstagramAccountNotFoundError as exc:
        # Two distinct setup problems share this exception and this exit code, but not
        # the same fix — so they must not share the same message (FR-005). Distinguished
        # on the account type the discovery reported, which is the only thing that
        # separates "link an account" from "convert the one you linked".
        if "PERSONAL" in str(exc):
            print(_personal_account_guidance(str(exc)))
        else:
            print(_no_account_guidance())
        sys.exit(3)
    except InstagramTokenError:
        print(
            "Error: the Facebook Page access token is invalid or expired.\n"
            "Re-run generate_auth_link.py to reconnect the Page, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)
    except InstagramUploadError as exc:
        print(f"Error: could not reach the Instagram Graph API: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found linked Instagram account: @{account['username']} (ID: {account['id']})")
    print(f"Account type: {account['account_type']}")

    _write_env_var("IG_BUSINESS_ACCOUNT_ID", account["id"])
    print("Instagram publishing enabled. IG_BUSINESS_ACCOUNT_ID written to .env.")


def _no_account_guidance() -> str:
    """Message for a Page with no linked Instagram account at all."""
    return (
        "No Instagram account is linked to this Facebook Page.\n"
        "Link an Instagram Business or Creator account to this Page in Meta's\n"
        f"Account Settings, then re-run this script. See:\n{_LINK_HELP_URL}"
    )


def _personal_account_guidance(detail: str) -> str:
    """Message for a Page whose linked Instagram account is still PERSONAL."""
    return (
        f"{detail}\n"
        "Convert it to a Business or Creator account in the Instagram app\n"
        "(Settings > Account type), then re-run this script."
    )


def _write_env_var(key: str, value: str) -> None:
    """Write or update a single key=value pair in .env, preserving all other lines.

    Same mechanism as generate_auth_link.py's, so a client's .env is only ever
    updated in place — never rewritten or reordered.
    """
    _ENV_PATH.touch(exist_ok=True)
    set_key(str(_ENV_PATH), key, value, quote_mode="never")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
