"""
resolve_facebook_quarantine.py — Operator tool: settle a quarantined Facebook publish (issue #78).

Usage:
    python3 scripts/resolve_facebook_quarantine.py list
    python3 scripts/resolve_facebook_quarantine.py resolve <video_id>
    python3 scripts/resolve_facebook_quarantine.py override <video_id> --accept-duplicate-risk

A quarantine entry means FieldKit sent Facebook's publish step for <video_id>, never
learned the outcome, and is blocking re-approval of that video until the outcome is
definitive. upload_facebook.py re-checks every tick and normally resolves it by itself.
This tool is for an entry that stays stuck.

`resolve` makes the outcome definitive rather than guessing it:

  1. It reads the video's status. If Facebook reports it published, the publish is
     recorded (the key is retired permanently) and nothing is deleted.
  2. Otherwise it DELETEs that exact video node, then reads it back and requires Graph
     error 100 (object does not exist). Only then is the key released: the video that
     might have gone live no longer exists, so re-approving cannot leave two posts.
  3. Anything else — the delete fails, reports the video does not exist, or the node can
     still be read afterwards — leaves the quarantine in place and exits 1. A code-100
     "does not exist" on the DELETE is NOT treated as success, because Graph returns it
     both for a video that is gone and for one this token cannot see.

"It isn't on the Page right now" is never enough to release a key: an accepted publish
can still be processing. For the case `resolve` cannot settle, `override` releases the
key WITHOUT a definitive answer. It requires --accept-duplicate-risk, because that is
exactly what it does: if the video was in fact published, re-approving will post it twice.

Takes upload_facebook.lock, so it never runs while a cron tick is mid-flight. The Page
token is sent in an Authorization header only, and every error printed is redacted.
"""

import argparse
import fcntl
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same CLIENT_NAME / .env resolution as upload_facebook.py — see its comment block.
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env", override=False)
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)
os.environ["CLIENT_NAME"] = _CLIENT

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import facebook_api, facebook_logger, facebook_state  # noqa: E402
from tools.facebook_api import FacebookTokenError, FacebookUploadError  # noqa: E402
from tools.redaction import redact_secrets, redact_value  # noqa: E402

_log = logging.getLogger(__name__)


def _try_acquire_upload_lock():
    """Take upload_facebook.lock non-blocking; None if a cron tick holds it."""
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "upload_facebook.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _find_entry(video_id: str) -> dict | None:
    """Return the quarantine entry for video_id, or None."""
    for entry in facebook_state.list_publish_reconciliations():
        if entry.get("video_id") == video_id:
            return entry
    return None


def _cmd_list() -> int:
    """Print every quarantined video. Returns the exit code."""
    entries = facebook_state.list_publish_reconciliations()
    if not entries:
        print("No quarantined Facebook publishes.")
        return 0
    for e in entries:
        print(
            f"video_id={e.get('video_id')} project={e.get('project_name')} "
            f"key={e.get('idempotency_key')} since={e.get('recorded_at')} "
            f"checks={e.get('attempts')}  https://www.facebook.com/{e.get('video_id')}"
        )
    return 0


def _cmd_resolve(entry: dict, token: str) -> int:
    """Make entry's outcome definitive (see module docstring). Returns the exit code."""
    video_id = entry["video_id"]
    project_name = entry.get("project_name", "unknown")
    idem_key = entry.get("idempotency_key", "")

    try:
        outcome, observed = facebook_api.get_video_publish_state(token, video_id)
    except (FacebookTokenError, FacebookUploadError) as exc:
        outcome, observed = "unknown", _safe(exc, token)

    if outcome == "published":
        facebook_state.record_recovered_publish(idem_key, project_name, video_id, observed)
        facebook_state.clear_publish_reconciliation(video_id, observed)
        facebook_logger.log_upload_recovered(project_name, video_id)
        print(
            f"Video {video_id} IS published. Recorded it; key {idem_key} is retired and "
            "the video will not be posted again. Nothing was deleted."
        )
        return 0

    print(f"Video {video_id} is not confirmed published ({observed}). Deleting it...")
    try:
        facebook_api.delete_video(token, video_id)
    except (FacebookTokenError, FacebookUploadError) as exc:
        print(
            f"Delete did NOT succeed: {_safe(exc, token)}\n"
            "The quarantine stays in place. If the error says the video does not exist, "
            "that is not proof it never went live — the token may simply be unable to see "
            "it. Check the Page by hand; see docs/facebook/README.md.",
            file=sys.stderr,
        )
        return 1

    if not facebook_api.video_is_gone(token, video_id):
        print(
            f"Delete reported success, but video {video_id} can still be read (or the "
            "check failed). The quarantine stays in place; run resolve again shortly.",
            file=sys.stderr,
        )
        return 1

    facebook_state.clear_publish_reconciliation(video_id, facebook_state.OPERATOR_DELETED)
    facebook_logger.log_publish_resolved(project_name, video_id, facebook_state.OPERATOR_DELETED)
    print(
        f"Video {video_id} deleted and confirmed gone. Key {idem_key} is released; the "
        "video can be re-approved and will be posted once."
    )
    return 0


def _cmd_override(entry: dict) -> int:
    """Release entry's key WITHOUT a definitive answer. Returns the exit code."""
    video_id = entry["video_id"]
    facebook_state.clear_publish_reconciliation(video_id, facebook_state.OPERATOR_OVERRIDE)
    facebook_logger.log_publish_resolved(
        entry.get("project_name", "unknown"), video_id, facebook_state.OPERATOR_OVERRIDE
    )
    print(
        f"OVERRIDE: key {entry.get('idempotency_key')} released with NO definitive answer "
        f"about video {video_id}. If that video was published, re-approving will post it "
        "a second time."
    )
    return 0


def _safe(exc, token: str) -> str:
    """Exception text with any credential removed (see tools/redaction.py)."""
    return redact_value(redact_secrets(str(exc)), token)


def main(argv=None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="Settle a quarantined Facebook publish.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show quarantined videos")
    p_resolve = sub.add_parser("resolve", help="record as published, or delete and release")
    p_resolve.add_argument("video_id")
    p_override = sub.add_parser("override", help="release WITHOUT a definitive answer")
    p_override.add_argument("video_id")
    p_override.add_argument("--accept-duplicate-risk", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "override" and not args.accept_duplicate_risk:
        print(
            "Refusing: override releases the key without knowing whether the video was "
            "published, so re-approving may post it twice. Re-run with "
            "--accept-duplicate-risk if that is what you intend.",
            file=sys.stderr,
        )
        return 2

    lock_f = _try_acquire_upload_lock()
    if lock_f is None:
        print("upload_facebook.py is running — try again in a minute.", file=sys.stderr)
        return 1
    try:
        if args.command == "list":
            return _cmd_list()
        entry = _find_entry(args.video_id)
        if entry is None:
            print(f"No quarantine entry for video {args.video_id}.", file=sys.stderr)
            return 1
        if args.command == "override":
            return _cmd_override(entry)
        token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
        if not token:
            print("FB_PAGE_ACCESS_TOKEN is not set.", file=sys.stderr)
            return 1
        return _cmd_resolve(entry, token)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    sys.exit(main())
