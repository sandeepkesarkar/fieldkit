"""
e2e_stage3_process.py — E2E Stage 3: Run process_photos.py.

Reads test_name and spp_effective from data/photo-agent/e2e_run_state.json,
then runs process_photos.py as a subprocess. When it completes, a Telegram
approval message has been sent.

Usage:
    python3 scripts/e2e_stage3_process.py
    python3 scripts/e2e_stage3_process.py --timeout 600
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env")
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"
_PROCESS_PHOTOS = str(Path(__file__).parent / "process_photos.py")


def _run_process_photos(test_name: str, spp_effective: int, timeout: int) -> None:
    """Run process_photos.py as a subprocess with SECONDS_PER_PHOTO override.

    Raises RuntimeError if the subprocess exits non-zero or times out.
    """
    env = {**os.environ, "SECONDS_PER_PHOTO": str(spp_effective)}
    try:
        result = subprocess.run(
            [sys.executable, _PROCESS_PHOTOS, "--project", test_name],
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"process_photos.py timed out after {timeout}s for project={test_name}")
    if result.returncode != 0:
        raise RuntimeError(f"process_photos.py exited {result.returncode} for project={test_name}")


def main(argv=None) -> None:
    """Entry point — read run state, run process_photos.py."""
    parser = argparse.ArgumentParser(description="E2E Stage 3: Run process_photos.py.")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Subprocess timeout in seconds (default 600)")
    args = parser.parse_args(argv)

    if not _RUN_STATE_FILE.exists():
        print(f"ERROR: No run state at {_RUN_STATE_FILE} — run Stages 1–2 first.", file=sys.stderr)
        sys.exit(1)

    run_state = json.loads(_RUN_STATE_FILE.read_text())
    test_name = run_state["test_name"]
    spp_effective = run_state["spp_effective"]

    try:
        _run_process_photos(test_name, spp_effective, args.timeout)
    except RuntimeError as exc:
        print(f"Stage 3 ❌ — {exc}", file=sys.stderr)
        sys.exit(1)

    print("Stage 3 ✅ — Telegram approval message sent")
    print("  Approve or reject in Telegram to continue.")


if __name__ == "__main__":
    main()
