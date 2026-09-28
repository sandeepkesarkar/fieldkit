"""
resolve_instagram_quarantine.py — Operator tool: settle a quarantined Instagram publish (issue #88).

Usage:
    python3 scripts/resolve_instagram_quarantine.py list
    python3 scripts/resolve_instagram_quarantine.py resolve <container_id>
    python3 scripts/resolve_instagram_quarantine.py override <container_id> --accept-duplicate-risk

A quarantine entry means FieldKit asked Instagram to publish <container_id>, never learned
the outcome, and is blocking re-approval of that video until the outcome is definitive.
upload_instagram.py re-checks every tick and normally resolves it by itself. This tool is
for an entry that stays stuck, and for asking on demand.

`resolve` releases the key only on an answer Meta documents as final. It reads the
container's status_code once and:

  - PUBLISHED — the Reel is live. The publish is recorded (the key is retired permanently)
    and the quarantine is cleared. Nothing will post it again.
  - EXPIRED   — "The container was not published within 24 hours and has expired" (Meta's
    IG Container reference). It never published and now never can, so the key is released
    and the video can be re-approved.
  - anything else — FINISHED, ERROR, IN_PROGRESS, an unrecognised value, an unreadable
    container, a network or token error — leaves the quarantine in place and exits 1.

Why FINISHED and ERROR are not enough: FINISHED means "ready to be published", i.e. not
published YET, and Meta's content-publishing troubleshooting tells callers whose
media_publish returned no ID to keep polling status_code for up to 5 minutes — a lost
publish can still land. ERROR is "failed to complete the publishing process", and Meta
does not document it as final. Unlike a Facebook video (scripts/resolve_facebook_quarantine.py),
an IG container cannot be deleted to force a definitive answer: Meta's IG Container
reference lists deleting as "not supported". So the definitive negative has to come from
Meta's own expiry. A FINISHED container that never publishes should read EXPIRED within
24 hours of its creation, and `resolve` (or the cron drain) releases it then.

"It isn't on the account right now" is never enough to release a key. For the case
`resolve` cannot settle — a container stuck in ERROR, or one Instagram will no longer
answer about — `override` releases the key WITHOUT a definitive answer. It requires
--accept-duplicate-risk, because that is exactly what it does: if the Reel was in fact
published, re-approving will post it twice.

Sources (checked 2026-09-28):
  https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container
  https://developers.facebook.com/docs/instagram-platform/content-publishing

Takes upload_instagram.lock, so it never runs while a cron tick is mid-flight. The Page
token is sent in an Authorization header only (tools/instagram_api._graph_get), and every
error printed is redacted. All state changes go through tools/instagram_state's verbs.
"""

import argparse
import fcntl
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Same CLIENT_NAME / .env resolution as upload_instagram.py — see its comment block.
_ROOT = Path(os.environ.get("FIELDKIT_ROOT", str(Path(__file__).parents[3])))
load_dotenv(_ROOT / ".env", override=False)
_CLIENT = os.environ.get("CLIENT_NAME")
if not _CLIENT:
    sys.exit("ERROR: CLIENT_NAME is not set in fieldkit/.env")
load_dotenv(_ROOT / "clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)
os.environ["CLIENT_NAME"] = _CLIENT

sys.path.insert(0, str(Path(__file__).parents[1]))

from tools import instagram_api, instagram_logger, instagram_state  # noqa: E402
from tools.instagram_api import InstagramTokenError, InstagramUploadError  # noqa: E402
from tools.redaction import redact_secrets, redact_value  # noqa: E402

_log = logging.getLogger(__name__)

# Meta's documented container lifetime: "not published within 24 hours and has expired".
_CONTAINER_LIFETIME = timedelta(hours=24)


def _try_acquire_upload_lock():
    """Take upload_instagram.lock non-blocking; None if a cron tick holds it."""
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "upload_instagram.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _find_entry(container_id: str) -> dict | None:
    """Return the quarantine entry for container_id, or None."""
    for entry in instagram_state.list_publish_reconciliations():
        if entry.get("container_id") == container_id:
            return entry
    return None


def _cmd_list() -> int:
    """Print every quarantined container. Returns the exit code."""
    entries = instagram_state.list_publish_reconciliations()
    if not entries:
        print("No quarantined Instagram publishes.")
        return 0
    for e in entries:
        print(
            f"container_id={e.get('container_id')} project={e.get('project_name')} "
            f"key={e.get('idempotency_key')} since={e.get('recorded_at')} "
            f"checks={e.get('attempts')}"
        )
    return 0


def _expiry_hint(entry: dict) -> str:
    """When a never-published container should read EXPIRED, from the entry's recorded_at.

    The container was created before the quarantine was recorded, so creation + 24h is no
    later than recorded_at + 24h. Only a hint for the operator; nothing is decided on it.
    """
    try:
        recorded = datetime.fromisoformat(entry.get("recorded_at") or "")
    except ValueError:
        return "within 24 hours of the container's creation"
    return f"by {(recorded + _CONTAINER_LIFETIME).isoformat()} at the latest"


def _cmd_resolve(entry: dict, token: str) -> int:
    """Release entry's key only on a definitive answer (see module docstring). Exit code."""
    container_id = entry["container_id"]
    project_name = entry.get("project_name", "unknown")
    idem_key = entry.get("idempotency_key", "")

    try:
        status = instagram_api.get_container_status(token, container_id)
    except (InstagramTokenError, InstagramUploadError) as exc:
        print(
            f"Could not read container {container_id}: {_safe(exc, token)}\n"
            "The quarantine stays in place. Not getting an answer is not proof the Reel "
            "never went live. Run resolve again later; if Instagram never answers again, "
            "see `override` in docs/instagram/README.md.",
            file=sys.stderr,
        )
        return 1

    if status == "PUBLISHED":
        instagram_state.record_recovered_publish(idem_key, project_name, container_id)
        instagram_state.clear_publish_reconciliation(container_id, "PUBLISHED")
        instagram_logger.log_upload_recovered(project_name, container_id)
        print(
            f"Container {container_id} IS published. Recorded it; key {idem_key} is retired "
            "and the video will not be posted again."
        )
        return 0

    if status == "EXPIRED":
        instagram_state.clear_publish_reconciliation(container_id, "EXPIRED")
        instagram_logger.log_publish_resolved(project_name, container_id, "EXPIRED")
        print(
            f"Container {container_id} EXPIRED without being published, and now never can "
            f"be. Key {idem_key} is released; the video can be re-approved and will be "
            "posted once."
        )
        return 0

    if status == "FINISHED":
        advice = (
            "FINISHED means 'not published yet', not 'never published': a publish request "
            "whose response was lost can still land. If it never publishes, Instagram "
            f"should report it EXPIRED {_expiry_hint(entry)}. Run resolve again after that."
        )
    elif status == "ERROR":
        advice = (
            "Instagram does not document ERROR as final, so it is not proof the Reel never "
            "went live. Run resolve again later. If it stays in ERROR, the only way to "
            "release the key is `override` — see docs/instagram/README.md."
        )
    else:
        advice = "That does not establish whether it published. Run resolve again later."
    print(
        f"Container {container_id} reports status {_safe(status, token)!r}. The quarantine "
        f"stays in place.\n{advice}",
        file=sys.stderr,
    )
    return 1


def _cmd_override(entry: dict) -> int:
    """Release entry's key WITHOUT a definitive answer. Returns the exit code."""
    container_id = entry["container_id"]
    instagram_state.clear_publish_reconciliation(container_id, instagram_state.OPERATOR_OVERRIDE)
    instagram_logger.log_publish_resolved(
        entry.get("project_name", "unknown"), container_id, instagram_state.OPERATOR_OVERRIDE
    )
    print(
        f"OVERRIDE: key {entry.get('idempotency_key')} released with NO definitive answer "
        f"about container {container_id}. If that Reel was published, re-approving will post "
        "it a second time."
    )
    return 0


def _safe(detail, token: str) -> str:
    """Text with any credential removed (see tools/redaction.py)."""
    return redact_value(redact_secrets(str(detail)), token)


def main(argv=None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="Settle a quarantined Instagram publish.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show quarantined containers")
    p_resolve = sub.add_parser(
        "resolve", help="record as published, or release if Instagram reports EXPIRED"
    )
    p_resolve.add_argument("container_id")
    p_override = sub.add_parser("override", help="release WITHOUT a definitive answer")
    p_override.add_argument("container_id")
    p_override.add_argument("--accept-duplicate-risk", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "override" and not args.accept_duplicate_risk:
        print(
            "Refusing: override releases the key without knowing whether the Reel was "
            "published, so re-approving may post it twice. Re-run with "
            "--accept-duplicate-risk if that is what you intend.",
            file=sys.stderr,
        )
        return 2

    lock_f = _try_acquire_upload_lock()
    if lock_f is None:
        print("upload_instagram.py is running — try again in a minute.", file=sys.stderr)
        return 1
    try:
        if args.command == "list":
            return _cmd_list()
        entry = _find_entry(args.container_id)
        if entry is None:
            print(f"No quarantine entry for container {args.container_id}.", file=sys.stderr)
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
