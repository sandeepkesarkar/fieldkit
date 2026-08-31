"""
upload_instagram.py — Cron script: publish the pending Instagram Reel upload.

Usage:
    python3 scripts/upload_instagram.py
    python3 scripts/upload_instagram.py --source cron

Reads the pending InstagramUploadJob from instagram_state.json, publishes the
already-approved video as a Reel via the Instagram Graph API's container flow,
and sends a Telegram confirmation.

Deliberately modeled line-for-line on upload_facebook.py — same claim-based
state machine, same re-entrancy lock pattern, same retry/token-expiry dispatch —
so the two cron scripts stay reviewable side by side. Read upload_facebook.py's
module docstring for the full rationale behind the claim/lease/lock design; only
the Instagram-specific differences are restated here.

Instagram-specific differences from upload_facebook.py:

  - Container flow. Instagram ingests video asynchronously: create a media
    container from a URL, poll until it finishes transcoding, then publish it.
    The poll is capped at 300s inside instagram_api.wait_for_container(); a
    container that never finishes surfaces as an ordinary retryable
    InstagramUploadError (spec.md's "stuck container" edge case).

  - Temporary Drive share link. The container endpoint takes a video_url that
    Instagram's own servers fetch — it does not accept uploaded bytes — so the
    approved video is briefly shared through Drive and unshared again. The link
    is revoked on EVERY exit path: success, transient failure, and token expiry.
    The video shared is the same already-approved, already-metadata-stripped
    asset the Facebook upload posts, never a re-processed copy (FR-014).

  - The local video file is NOT deleted here. upload_facebook.py deletes it after
    its own successful post; deleting it from this script would break the Facebook
    upload for the same approved video, which is exactly the cross-platform
    coupling FR-013 forbids. Instagram publishing leaves cleanup to the Facebook
    path that already owns it.

  - Per-client enable switch. IG_BUSINESS_ACCOUNT_ID absent (or empty) means
    Instagram publishing is not configured for this client, and the script exits 0
    without touching state (FR-016). That absence is the entire mechanism keeping
    clients like _construction_co out of this code path — no client-name
    special-casing anywhere.

Platform independence (FR-013): instagram_state.json, upload_instagram.lock, and
this script's claim namespace are all separate from the Facebook equivalents. A
Facebook failure can neither block nor retry an Instagram job, and vice versa.

No new credential is introduced: Instagram Graph API calls reuse
FB_PAGE_ACCESS_TOKEN from Feature 003. FB_APP_SECRET is never read here.
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

from tools import drive, instagram_api, instagram_logger, instagram_state, telegram_api
from tools.instagram_api import InstagramTokenError, InstagramUploadError

_log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_COOLDOWN_SECONDS = 60
# How long a claim (status='uploading') is presumed still genuinely in-progress before
# claim_pending_upload() treats it as abandoned and allows reclaiming it. Deliberately
# larger than upload_facebook.py's 900s: an Instagram attempt can legitimately run for a
# Drive upload PLUS the 300s container-poll cap, and a lease that expired mid-attempt
# would let the next cron tick create a second container for a job already in flight.
_UPLOAD_LEASE_SECONDS = 1800


def _try_acquire_upload_lock() -> "IO | None":
    """Try to acquire upload_instagram.lock exclusively (non-blocking).

    Mirrors upload_facebook.py's _try_acquire_upload_lock, against a SEPARATE lock file:
    the two cron scripts must never serialize against each other (FR-013), only against
    other invocations of themselves.

    Returns the open lock file object on success, or None if another upload_instagram
    instance is already running. The caller must close the returned file object.
    """
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "upload_instagram.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def main(argv=None) -> None:
    """Entry point — validate env, load the pending job, attempt the publish."""
    parser = argparse.ArgumentParser(description="Publish the pending Instagram Reel.")
    parser.add_argument(
        "--source",
        choices=["cron"],
        default=None,
        help="Invocation label for logging (informational only).",
    )
    args = parser.parse_args(argv)
    if args.source:
        _log.debug("invoked from source=%s", args.source)

    # FR-016 is checked FIRST, ahead of any other validation: a client without
    # Instagram configured is not misconfigured, it is simply not using this feature,
    # and must exit 0 rather than reporting an environment error.
    ig_account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
    if not ig_account_id:
        _log.debug("IG_BUSINESS_ACCOUNT_ID not set — Instagram publishing disabled")
        return

    page_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    if not page_token:
        _log.error("FB_PAGE_ACCESS_TOKEN is required for Instagram publishing")
        sys.exit(1)
    for var in ("FIELDKIT_DATA_DIR", "FIELDKIT_LOG_DIR"):
        if not os.environ.get(var):
            _log.error("%s is required — add it to your client .env file", var)
            sys.exit(1)

    lock_f = _try_acquire_upload_lock()
    if lock_f is None:
        _log.debug("another upload_instagram instance is running — exiting")
        return
    try:
        record = instagram_state.get_pending_upload()
        if record is None:
            _log.debug("no pending instagram upload — exiting")
            return

        _process_upload(record, page_token, ig_account_id, chat_id)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _process_upload(record: dict, page_token: str, ig_account_id: str, chat_id: str) -> None:
    """Claim and attempt to publish the Reel described by record.

    record is only a snapshot (from main()'s get_pending_upload()) used for its immutable
    fields. Every decision about whether and how to proceed — staleness, cooldown, attempt
    budget, claiming — is made by claim_pending_upload() against the CURRENT, freshly-locked
    state in one exclusive-lock transaction, exactly as upload_facebook.py does.
    """
    project_name = record["project_name"]
    video_path = record["video_local_path"]
    idem_key = record["idempotency_key"]
    attempt_count = record.get("attempt_count", 0)  # pre-claim value; claim() advances it by 1

    claim = instagram_state.claim_pending_upload(
        idem_key,
        cooldown_seconds=_COOLDOWN_SECONDS,
        max_attempts=_MAX_ATTEMPTS,
        lease_seconds=_UPLOAD_LEASE_SECONDS,
    )

    if claim in ("mismatch", "in_flight", "cooldown"):
        _log.debug("declined claim (%s): project=%s key=%s", claim, project_name, idem_key)
        return
    if claim in ("stale_published", "stale_failed"):
        _log.warning(
            "cleared stale pending record (%s) without reprocessing: project=%s key=%s",
            claim, project_name, idem_key,
        )
        return
    if claim == "exhausted":
        _log.error("attempt budget exhausted: project=%s key=%s", project_name, idem_key)
        instagram_logger.log_upload_exhausted(project_name)
        _send_alert(
            chat_id,
            f"⚠️ Instagram upload failed for {project_name} after {_MAX_ATTEMPTS} attempts — check logs",
        )
        return
    assert claim == "claimed", f"unexpected claim outcome: {claim!r}"

    if not Path(video_path).exists():
        _log.error("video file missing: project=%s path=%s", project_name, video_path)
        instagram_state.mark_failed(idem_key)
        return

    attempt_number = attempt_count + 1
    instagram_logger.log_upload_started(project_name, attempt_number)

    share_link = None
    try:
        share_link = drive.create_temporary_share_link(video_path)

        container_id = instagram_api.create_media_container(
            page_token, ig_account_id, share_link
        )
        instagram_state.set_container_id(idem_key, container_id)
        instagram_logger.log_container_created(project_name, container_id)

        instagram_api.wait_for_container(page_token, container_id)
        instagram_logger.log_container_ready(project_name, container_id)

        post_id = instagram_api.publish_container(page_token, ig_account_id, container_id)
    except InstagramTokenError as exc:
        # Token expiry is terminal after ONE attempt (FR-008): retrying cannot fix it, and
        # burning the remaining attempt budget would only delay the alert the owner needs.
        # Checked before InstagramUploadError below — it is deliberately NOT a subclass.
        _revoke_share_link(share_link, project_name)
        _log.error("Instagram token error: project=%s: %s", project_name, exc)
        instagram_state.mark_failed(idem_key)
        instagram_logger.log_token_expired(project_name)
        _send_alert(
            chat_id,
            f"⚠️ Instagram token expired — reconnect {project_name}'s account "
            "via generate_auth_link.py",
        )
        return
    except (InstagramUploadError, RuntimeError, OSError) as exc:
        # Transient: an Instagram API/network error, a container that reported ERROR or
        # never finished within the poll cap, or a Drive failure creating the share link.
        # RuntimeError/OSError are caught alongside InstagramUploadError because the Drive
        # helpers raise those — a Drive failure is just as retryable as an Instagram one.
        _revoke_share_link(share_link, project_name)
        _log.error("upload failed: project=%s attempt=%d: %s", project_name, attempt_number, exc)
        instagram_logger.log_upload_attempt_failed(project_name, attempt_number, str(exc))
        if attempt_number >= _MAX_ATTEMPTS:
            instagram_state.mark_failed(idem_key)
            instagram_logger.log_upload_exhausted(project_name)
            _send_alert(
                chat_id,
                f"⚠️ Instagram upload failed for {project_name} after {_MAX_ATTEMPTS} attempts — check logs",
            )
        else:
            # A KNOWN, caught failure with retries remaining: release the claim immediately so
            # the next attempt is gated by the short _COOLDOWN_SECONDS rather than the much
            # longer _UPLOAD_LEASE_SECONDS an abandoned claim would wait out. release_claim()
            # also clears container_id — the next attempt builds a fresh container.
            instagram_state.release_claim(idem_key)
        return

    # Success path. The share link is revoked first: Instagram has already ingested the
    # video by the time a container publishes, so nothing needs it to stay public.
    _revoke_share_link(share_link, project_name)
    instagram_state.mark_published(idem_key, post_id)
    instagram_logger.log_upload_published(project_name, post_id)
    post_url = f"https://www.instagram.com/p/{post_id}"
    _send_confirmation(chat_id, f"✅ Reel live on Instagram! {post_url}")


def _revoke_share_link(share_link: str | None, project_name: str) -> None:
    """Revoke the temporary public Drive link, if one was created this attempt.

    Best-effort at the call site — a revoke failure must not undo a live post or mask the
    real upload error — but loudly logged, because the failure mode it leaves behind is a
    client's video still publicly reachable. drive.revoke_share_link() raises rather than
    swallowing, precisely so this is a decision made here rather than hidden there.
    """
    if not share_link:
        return
    try:
        drive.revoke_share_link(drive.extract_file_id(share_link))
    except (RuntimeError, ValueError) as exc:
        _log.error(
            "failed to revoke temporary share link — video may remain publicly "
            "reachable: project=%s error=%s",
            project_name, exc,
        )


def _send_confirmation(chat_id: str, text: str) -> None:
    """Send a success notification. Failure is logged but does not raise."""
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send Telegram confirmation")
        return
    try:
        telegram_api.send_message(chat_id, text)
    except RuntimeError as exc:
        _log.warning("Telegram confirmation failed (non-fatal): %s", exc)


def _send_alert(chat_id: str, text: str) -> None:
    """Send a failure alert. Failure is logged but does not raise."""
    if not chat_id:
        _log.warning("ADMIN_TELEGRAM_CHAT_ID not set — cannot send Telegram alert")
        return
    try:
        telegram_api.send_message(chat_id, text)
    except RuntimeError as exc:
        _log.warning("Telegram alert failed (non-fatal): %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    main()
