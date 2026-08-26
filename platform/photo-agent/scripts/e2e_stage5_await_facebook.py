"""
e2e_stage5_await_facebook.py — E2E Stage 5: Wait for Facebook upload to complete.

Polls facebook_state.json's published_history for this test's job, then
updates e2e_run_state.json with the fb_post_id.

published_history (not pending_facebook_upload) is the source of truth here:
mark_published() clears pending_facebook_upload as soon as a job resolves
(issue #34 — a cron entrypoint that keeps seeing a resolved job as pending
reprocesses it forever), so a 'published' status is never observable on the
pending record itself. facebook_state.find_published() searches the capped
history by project_name, so an unrelated publish landing in between can't
hide this run's own record.

Run upload_facebook.py in another terminal while this polls, or wait for cron.

Usage:
    python3 scripts/e2e_stage5_await_facebook.py
    python3 scripts/e2e_stage5_await_facebook.py --timeout 600
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
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import facebook_state

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"
_POLL_INTERVAL = 10


def _wait_for_facebook_post(test_name: str, timeout: int) -> str:
    """Poll until upload_facebook.py publishes this test's job. Returns fb_post_id.

    Raises SystemExit on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = facebook_state.find_published(test_name)
        if found is not None:
            return found.get("fb_post_id", "")
        time.sleep(_POLL_INTERVAL)
    raise SystemExit(f"Stage 5 timed out after {timeout}s waiting for Facebook post for {test_name}")


def main(argv=None) -> None:
    """Entry point — read run state, poll for Facebook post, update state."""
    parser = argparse.ArgumentParser(description="E2E Stage 5: Wait for Facebook post.")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds (default 600)")
    args = parser.parse_args(argv)

    if not _RUN_STATE_FILE.exists():
        print(f"ERROR: No run state at {_RUN_STATE_FILE} — run Stages 1–4 first.", file=sys.stderr)
        sys.exit(1)

    run_state = json.loads(_RUN_STATE_FILE.read_text())
    test_name = run_state["test_name"]

    print(f"Waiting for Facebook post for {test_name} (timeout: {args.timeout}s)...")
    print("  Run upload_facebook.py if not using cron.")

    try:
        fb_post_id = _wait_for_facebook_post(test_name, args.timeout)
    except SystemExit as exc:
        print(f"Stage 5 ❌ — {exc}", file=sys.stderr)
        sys.exit(1)

    run_state["fb_post_id"] = fb_post_id
    _RUN_STATE_FILE.write_text(json.dumps(run_state, indent=2))

    print(f"Stage 5 ✅ — https://www.facebook.com/{fb_post_id}")


if __name__ == "__main__":
    main()
