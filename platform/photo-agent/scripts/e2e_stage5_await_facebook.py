"""
e2e_stage5_await_facebook.py — E2E Stage 5: Wait for Facebook upload to complete.

Polls facebook_state.json until upload_facebook.py marks the job as 'published',
then updates e2e_run_state.json with the fb_post_id.

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
    """Poll until upload_facebook.py marks the job as 'published'. Returns fb_post_id.

    Raises SystemExit on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        fb = facebook_state.get_pending_upload()
        if (fb is not None
                and fb.get("project_name") == test_name
                and fb.get("status") == "published"):
            return fb.get("fb_post_id", "")
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
