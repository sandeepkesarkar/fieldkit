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
    A revoke that FAILS is recorded durably in instagram_state and retried on every
    later tick until it succeeds (see _drain_share_cleanups) — never written off as
    an acceptable success, because that would leave a client's video publicly
    reachable indefinitely with nothing recording it. The video shared is the same
    already-approved, already-metadata-stripped asset the Facebook upload posts,
    never a re-processed copy (FR-014).

  - Deleting the local video file is COORDINATED, not owned by either script. One
    approval produces one file with two independent consumers, so whichever enabled
    platform resolves LAST deletes it — see tools/upload_cleanup.py. Deleting on
    one's own success (which is what upload_facebook.py did when it was the only
    consumer) would pull the file out from under the other platform's still-pending
    job, which then fails terminally with nothing published and no alert.

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

from tools import (
    drive,
    instagram_api,
    instagram_logger,
    instagram_state,
    paths,
    telegram_api,
    upload_cleanup,
)
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
        # Runs before (and independently of) any upload work: a dangling public share
        # link from an earlier tick has to keep getting retried whether or not there
        # is a new job to publish today.
        _drain_share_cleanups()

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
        # Under the coordinated-deletion rule this should no longer be reachable via the
        # other platform having deleted the file out from under us; it now means the file
        # genuinely vanished (manual cleanup, disk loss). Still terminal — there is
        # nothing to upload — but alert rather than failing silently.
        _log.error("video file missing: project=%s path=%s", project_name, video_path)
        instagram_state.mark_failed(idem_key)
        instagram_logger.log_upload_attempt_failed(
            project_name, attempt_count + 1, "video file missing on disk"
        )
        _send_alert(
            chat_id,
            f"⚠️ Instagram upload failed for {project_name} — the approved video file "
            "is missing on disk",
        )
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
        permalink = _fetch_permalink(page_token, post_id, project_name)
    except InstagramTokenError as exc:
        # Token expiry is terminal after ONE attempt (FR-008): retrying cannot fix it, and
        # burning the remaining attempt budget would only delay the alert the owner needs.
        # Checked before InstagramUploadError below — it is deliberately NOT a subclass.
        _revoke_share_link(share_link, project_name, chat_id)
        _log.error("Instagram token error: project=%s: %s", project_name, exc)
        instagram_state.mark_failed(idem_key)
        instagram_logger.log_token_expired(project_name)
        _delete_local_file_if_last(video_path, project_name, idem_key)
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
        _revoke_share_link(share_link, project_name, chat_id)
        _log.error("upload failed: project=%s attempt=%d: %s", project_name, attempt_number, exc)
        instagram_logger.log_upload_attempt_failed(project_name, attempt_number, str(exc))
        if attempt_number >= _MAX_ATTEMPTS:
            instagram_state.mark_failed(idem_key)
            instagram_logger.log_upload_exhausted(project_name)
            _delete_local_file_if_last(video_path, project_name, idem_key)
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
    _revoke_share_link(share_link, project_name, chat_id)
    instagram_state.mark_published(idem_key, post_id, permalink=permalink)
    instagram_logger.log_upload_published(project_name, post_id)
    # mark_published() above is what makes this job terminal in the state file, and it has
    # to happen BEFORE the coordination check — see tools/upload_cleanup.py's docstring.
    _delete_local_file_if_last(video_path, project_name, idem_key)
    if permalink:
        _send_confirmation(chat_id, f"✅ Reel live on Instagram! {permalink}")
    else:
        # Never fabricate a link from the media ID: it would not resolve. Say what is
        # true instead — the Reel is live, the link just could not be read back.
        _send_confirmation(
            chat_id,
            f"✅ Reel live on Instagram! (media {post_id} — could not fetch the "
            "post link; check the account)",
        )


def _revoke_share_link(share_link: str | None, project_name: str, chat_id: str) -> None:
    """Revoke the temporary public Drive link, if one was created this attempt.

    A revoke failure must not undo a live post or mask the real upload error, so it does
    not raise here. But it is emphatically NOT treated as success: the file id is written
    to instagram_state's pending-cleanup list, retried on every later tick by
    _drain_share_cleanups(), and the admin is alerted once, naming the specific file, so a
    public link can never be left dangling with no record of it.
    """
    if not share_link:
        return
    try:
        file_id = drive.extract_file_id(share_link)
    except ValueError as exc:
        # Nothing to record against — we cannot name the file to retry or clean up.
        _log.error("cannot parse share link to revoke it: project=%s error=%s", project_name, exc)
        return

    try:
        drive.revoke_share_link(file_id)
    except RuntimeError as exc:
        _log.error(
            "failed to revoke temporary share link — video remains publicly reachable: "
            "project=%s file_id=%s error=%s",
            project_name, file_id, exc,
        )
        newly_recorded = instagram_state.record_share_cleanup(file_id, project_name)
        if newly_recorded:
            _send_alert(
                chat_id,
                f"⚠️ Instagram: could not remove the temporary public link for "
                f"{project_name} (Drive file {file_id}). The video may still be publicly "
                "reachable. FieldKit will keep retrying; remove the link manually if this "
                "persists.",
            )


def _drain_share_cleanups() -> None:
    """Retry every previously-failed share-link revocation, clearing the ones that succeed.

    Runs on every tick, independently of whether there is an upload job, because a dangling
    public link is a standing privacy problem that outlives the job that created it. Still
    failing entries stay recorded (with their attempt count bumped) for the next tick.
    """
    for entry in instagram_state.list_share_cleanups():
        file_id = entry.get("file_id")
        project_name = entry.get("project_name", "unknown")
        if not file_id:
            continue
        try:
            drive.revoke_share_link(file_id)
        except RuntimeError as exc:
            _log.error(
                "retry of share-link revocation still failing: project=%s file_id=%s error=%s",
                project_name, file_id, exc,
            )
            instagram_state.record_share_cleanup(file_id, project_name)
            continue
        instagram_state.clear_share_cleanup(file_id)
        _log.info(
            "share-link revocation succeeded on retry: project=%s file_id=%s",
            project_name, file_id,
        )


def _fetch_permalink(page_token: str, post_id: str, project_name: str) -> str | None:
    """Return the published Reel's real permalink, or None if it can't be read back.

    Deliberately non-fatal: by the time this runs the Reel is already live, so a failed
    permalink lookup must not fail the job, consume a retry, or re-publish anything. The
    caller degrades the confirmation message instead of inventing a link — a URL built
    from the media ID would not resolve.
    """
    try:
        return instagram_api.get_media_permalink(page_token, post_id)
    except (InstagramTokenError, InstagramUploadError) as exc:
        _log.error(
            "published but could not fetch permalink: project=%s post_id=%s error=%s",
            project_name, post_id, exc,
        )
        return None


def _delete_local_file_if_last(video_local_path: str, project_name: str, idem_key: str) -> None:
    """Delete the approved video, but only once every OTHER enabled platform is done with it.

    MUST be called after this job's own terminal state is recorded — see
    tools/upload_cleanup.py for why that ordering is what makes the check race-free.
    """
    waiting = upload_cleanup.other_platforms_pending(
        idem_key, platform=upload_cleanup.INSTAGRAM
    )
    if waiting:
        _log.info(
            "leaving local video in place — still needed by %s: project=%s",
            ", ".join(waiting), project_name,
        )
        return
    _delete_local_file(video_local_path, project_name)


def _delete_local_file(video_local_path: str, project_name: str) -> None:
    """Delete the local temp video file. Best-effort: logs on failure, never raises.

    Same guard as upload_facebook.py's copy: refuses to unlink anything outside the
    resolved VIDEO_TMP_DIR root.
    """
    try:
        p = Path(video_local_path).resolve()
        allowed = paths.get_video_tmp_root()
        try:
            p.relative_to(allowed)
        except ValueError:
            _log.error(
                "refused to delete file outside tmp directory: project=%s",
                project_name,
            )
            return
        if p.exists():
            p.unlink()
            _log.info("deleted local video file: project=%s", project_name)
        else:
            _log.debug("local video file already absent: project=%s", project_name)
    except OSError as exc:
        _log.warning("failed to delete local video file: project=%s error=%s", project_name, exc)


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
