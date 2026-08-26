"""
e2e_stage4_await_approval.py — E2E Stage 4: Wait for Telegram approval.

Polls state.json and facebook_state.json until check_approval.py has cleared
the pending_approval and enqueued a Facebook upload for this test run.

A pending enqueue (facebook_state.get_pending_upload()) is one success signal,
but not the only one: upload_facebook.py's cron leg can race ahead and publish
the job — clearing pending_facebook_upload (issue #34) — before this poll ever
samples the brief window it was pending in. facebook_state.find_published() is
checked as a second, equally-valid success signal for exactly that race.

Reply /photo_approve (or run `check_approval.py --callback-data approve`
directly) in Telegram while this script is polling — issue #49 removed the
inline Approve button and the cron poller in favor of the /photo_approve
and /photo_reject Hermes commands.

Usage:
    python3 scripts/e2e_stage4_await_approval.py
    python3 scripts/e2e_stage4_await_approval.py --timeout 600
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# CLIENT_NAME resolution order (issue #45): a pre-set CLIENT_NAME in the
# process environment wins over the root .env's value — see
# run_e2e_test.py's module docstring comment for the full rationale.
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

from tools import facebook_state, state

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"
_POLL_INTERVAL = 10


def _wait_for_approval(test_name: str, timeout: int) -> None:
    """Poll until pending_approval is cleared and a matching FB upload is enqueued (or has
    already resolved — see module docstring).

    Raises SystemExit on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = state.get_pending_approval()
        if pending is None:
            fb = facebook_state.get_pending_upload()
            if fb is not None and fb.get("project_name") == test_name:
                return
            if facebook_state.find_published(test_name) is not None:
                return
        time.sleep(_POLL_INTERVAL)
    raise SystemExit(f"Stage 4 timed out after {timeout}s waiting for approval of {test_name}")


def main(argv=None) -> None:
    """Entry point — read run state, poll for approval."""
    parser = argparse.ArgumentParser(description="E2E Stage 4: Wait for Telegram approval.")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds (default 600)")
    args = parser.parse_args(argv)

    if not _RUN_STATE_FILE.exists():
        print(f"ERROR: No run state at {_RUN_STATE_FILE} — run Stages 1–3 first.", file=sys.stderr)
        sys.exit(1)

    run_state = json.loads(_RUN_STATE_FILE.read_text())
    test_name = run_state["test_name"]

    print(f"Waiting for approval of {test_name} (timeout: {args.timeout}s)...")
    print("  Reply /photo_approve in Telegram, or run "
          "'check_approval.py --callback-data approve' directly.")

    try:
        _wait_for_approval(test_name, args.timeout)
    except SystemExit as exc:
        print(f"Stage 4 ❌ — {exc}", file=sys.stderr)
        sys.exit(1)

    print("Stage 4 ✅ — Approved, FB upload enqueued")


if __name__ == "__main__":
    main()
