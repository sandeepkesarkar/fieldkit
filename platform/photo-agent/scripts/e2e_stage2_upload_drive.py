"""
e2e_stage2_upload_drive.py — E2E Stage 2: Upload frames to Google Drive.

Reads run state from data/photo-agent/e2e_run_state.json, uploads all JPEG
frames to a new Drive folder, then updates the state with the folder_id.

Usage:
    python3 scripts/e2e_stage2_upload_drive.py

Requires DRIVE_ROOT_FOLDER_ID env var and valid Drive credentials in
~/.config/gws/user_credentials.json. Run setup_drive_auth.py if auth fails.
"""

import json
import os
import sys
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

from tools import drive

_DATA_DIR = Path(os.environ.get("FIELDKIT_DATA_DIR", str(Path(__file__).parents[3] / "data"))) / "photo-agent"
_RUN_STATE_FILE = _DATA_DIR / "e2e_run_state.json"


def _upload_frames_to_drive(
    frames_dir: Path, test_name: str, root_folder_id: str
) -> tuple[str, list[str]]:
    """Create a Drive folder and upload all JPEG frames. Returns (folder_id, file_ids)."""
    folder_id = drive.create_folder(test_name, root_folder_id)
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    file_ids = []
    for frame in frames:
        fid = drive.upload(frame, folder_id, frame.name, content_type="image/jpeg")
        file_ids.append(fid)
    return folder_id, file_ids


def main() -> None:
    """Entry point — read run state, upload frames, update state with folder_id."""
    root_folder_id = os.environ.get("DRIVE_ROOT_FOLDER_ID")
    if not root_folder_id:
        print("ERROR: DRIVE_ROOT_FOLDER_ID is not set", file=sys.stderr)
        sys.exit(1)

    if not _RUN_STATE_FILE.exists():
        print(f"ERROR: No run state at {_RUN_STATE_FILE} — run Stage 1 first.", file=sys.stderr)
        sys.exit(1)

    run_state = json.loads(_RUN_STATE_FILE.read_text())
    test_name = run_state["test_name"]
    frames_dir = Path(run_state["frames_dir"])
    n_frames = run_state["n_frames"]

    if not frames_dir.exists() or not list(frames_dir.glob("frame_*.jpg")):
        print(f"ERROR: No frames in {frames_dir} — run Stage 1 first.", file=sys.stderr)
        sys.exit(1)

    try:
        folder_id, _ = _upload_frames_to_drive(frames_dir, test_name, root_folder_id)
    except Exception as exc:
        print(f"Stage 2 ❌ — Drive upload failed: {exc}", file=sys.stderr)
        sys.exit(1)

    run_state["folder_id"] = folder_id
    _RUN_STATE_FILE.write_text(json.dumps(run_state, indent=2))

    print(f"Stage 2 ✅ — {n_frames} files uploaded")
    print(f"  folder_id: {folder_id}")


if __name__ == "__main__":
    main()
