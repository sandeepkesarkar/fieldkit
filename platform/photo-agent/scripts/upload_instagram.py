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

    The cleanup obligation is registered BEFORE the file is made public, not after
    the share call returns (see _register_share and drive.create_temporary_share_link's
    on_file_id hook). Registering it afterwards left one unrecoverable case: a
    permission POST that succeeds server-side and then loses its response raises
    without ever yielding a file id, so the link would be real and untracked, with
    nothing left that could revoke it. Registering first means the id is written
    down regardless of how that call turns out, and revoking a file that never
    became public is a harmless no-op. An anonymous Drive permission cannot carry
    an expirationTime — the API restricts that to user and group permissions — so
    this pre-registration plus the per-tick drain IS the time bound: at worst one
    attempt plus one cron tick, even if this process is killed at the worst moment.

  - Duplicate-publish reconciliation (FR-011). publish_container() is the
    irreversible external side effect; mark_published() is the durable record of
    it. A crash, kill, or lost HTTP response between the two leaves Meta holding a
    live Reel this system has no record of. The re-entrancy lock cannot help — the
    holder is already dead — so the container id is persisted instead and survives
    across attempts, and no attempt publishes anything while a previous container's
    fate is unknown. _classify_prior_container() asks Instagram directly: a
    container whose status_code is PUBLISHED is authoritative proof the Reel is
    already live, and it is recorded rather than published again. A duplicate Reel
    on a real client account cannot be taken back, so ambiguity is always resolved
    by refusing to publish, never by trying again.

    Surviving attempts is not enough on its own, because a job also has to survive
    TERMINAL FAILURE. If the publish lands at Meta, its response is lost, and the
    container then cannot be reconciled for the whole retry budget, mark_failed()
    would discard the record and the container id with it — leaving nothing that
    could ever check again, and no reason for a later re-approval to be refused.
    So an unsettled container is QUARANTINED durably instead
    (instagram_state.record_publish_reconciliation): the entry outlives the job,
    blocks its idempotency key against re-approval, is retried by
    _drain_publish_reconciliations() on every later tick, and clears only when
    Instagram is definitive — PUBLISHED (recorded, key retired permanently) or
    FINISHED/ERROR/EXPIRED (never published, key released). A Telegram warning is
    NOT the control here; it explains the control.

    Note what this deliberately does NOT change: instagram_state.mark_failed()
    still discards the whole record, mirroring facebook_state.mark_failed()
    exactly. The obligation lives outside the job record — the same shape as
    pending_share_cleanups — which is what lets the two state modules stay aligned
    while Instagram still satisfies FR-011.

    Facebook has the SAME latent exposure and is NOT fixed here. If
    facebook_api.upload_video()'s response is lost the video may be live with no
    record of it, and a re-approval would post it twice. It cannot be fixed this
    way AS facebook_api.py IS CURRENTLY WRITTEN: its one-shot multipart POST knows
    no identifier until the response arrives, so there is nothing to reconcile
    against afterwards and matching the Page's recent videos would be a heuristic
    rather than an authority. That is a limit of this implementation, not of Meta's
    API — Meta's sessionized video upload returns an upload_session_id AND a
    video_id from its start phase, before any bytes move, which is exactly the
    durable pre-known handle reconciliation needs (see Meta's official Python
    Business SDK, facebook_business/video_uploader.py). Closing it therefore means
    moving Facebook onto the sessionized upload: out of scope for this feature, and
    tracked as an urgent fast-follow in issue #78 rather than left implied by an
    Instagram-only fix.

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

  - Deployment heartbeat. The env var above says Instagram is CONFIGURED; it says
    nothing about whether this script is installed in crontab, and those are two
    separate acts that nothing can make atomic. So every tick stamps a heartbeat
    (tools/worker_health.py) before any gate. check_approval.py refuses to enqueue
    an Instagram job without a fresh one, and tools/upload_cleanup.py stops
    retaining the shared video for a platform whose worker has gone quiet. Both
    directions self-heal: install the cron and the next tick restores normal
    behaviour with no other action.

  - The account a job publishes to is the one recorded ON THE JOB at approval time,
    not whatever IG_BUSINESS_ACCOUNT_ID holds when the cron happens to run. If the
    two disagree the job is failed and the owner is told, rather than silently
    preferring either: reconfiguring a client between approval and publish must not
    be able to post their video to a different Instagram account.

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
    worker_health,
)
from tools.instagram_api import InstagramTokenError, InstagramUploadError
from tools.redaction import redact_secrets

_log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_COOLDOWN_SECONDS = 60
# How long a claim (status='uploading') is presumed still genuinely in-progress before
# claim_pending_upload() treats it as abandoned and allows reclaiming it. Deliberately
# larger than upload_facebook.py's 900s: an Instagram attempt can legitimately run for a
# Drive upload PLUS the 300s container-poll cap, and a lease that expired mid-attempt
# would let the next cron tick create a second container for a job already in flight.
_UPLOAD_LEASE_SECONDS = 1800

# How many unresolved publishes must pile up before every tick starts shouting about the
# backlog. Deliberately NOT a cap: the list is never trimmed, because dropping an entry
# would release an idempotency key while a Reel's fate is still unknown — the exact
# duplicate this mechanism exists to prevent. Growth is the safe direction; this only
# makes it visible. Low enough that a real accumulation is noticed early, high enough that
# one unlucky upload does not trigger it.
_QUARANTINE_BACKLOG_THRESHOLD = 5


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

    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    # Only the per-client data/log dirs are validated up front. They gate everything
    # below — including share-link cleanup, which reads and writes instagram_state.
    for var in ("FIELDKIT_DATA_DIR", "FIELDKIT_LOG_DIR"):
        if not os.environ.get(var):
            _log.error("%s is required — add it to your client .env file", var)
            sys.exit(1)

    # Stamp liveness BEFORE the lock and before every gate below. The heartbeat attests
    # that this cron ENTRY exists and fired, which is a different fact from whether
    # Instagram is switched on for this client or whether there is anything to do. It is
    # what check_approval.py consults before queueing a job it would otherwise have no way
    # of knowing nobody will ever drain, and what tools/upload_cleanup.py consults before
    # retaining a shared video on this platform's behalf. See tools/worker_health.py.
    worker_health.record_heartbeat(upload_cleanup.INSTAGRAM)

    lock_f = _try_acquire_upload_lock()
    if lock_f is None:
        _log.debug("another upload_instagram instance is running — exiting")
        return
    try:
        # Recovery sweep for approved videos that nothing can still be waiting on. Like the
        # share-link drain below it is unconditional: it exists to catch files the normal
        # coordinated delete could not, which by definition means no job will invoke it.
        upload_cleanup.sweep_orphaned_videos()

        # Share-link cleanup runs FIRST, before the Instagram/Meta config gates below,
        # and is deliberately not conditional on either of them. Revoking a Drive
        # permission needs Drive credentials and nothing else — not an Instagram account
        # id, not a Meta token. Gating it on those would mean that disabling Instagram
        # for a client, or letting its Page token expire, permanently stranded any link
        # that was already dangling: still publicly reachable, with no code path left
        # that would ever retry it. A link that is already public stays public whether
        # or not anyone intends to publish another Reel.
        _drain_share_cleanups(chat_id)

        # Unresolved publishes are drained next, and — like the share-link drain above —
        # BEFORE the enable gate below. Reading a container's status needs the Page token
        # and nothing else: no account id, no pending job, no Instagram still being
        # switched on for this client. A quarantined container is a Reel that may be live
        # on a real account with nothing recording it, and it holds an idempotency key
        # blocked until Instagram is definitive. Gating that on the feature still being
        # enabled would strand it exactly as gating the share drain would have stranded a
        # public link.
        page_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
        if page_token:
            _drain_publish_reconciliations(page_token, chat_id)
        else:
            # Without the token nothing can be asked of Instagram — but an entry that
            # blocks a video from ever being re-approved must not do so silently. This was
            # the last quiet failure mode left in the mechanism.
            _warn_quarantines_unreachable(chat_id)

        # FR-016: a client without Instagram configured is not misconfigured, it is
        # simply not using this feature, so it exits 0 rather than reporting an
        # environment error — and, per the above, only AFTER cleanup has had its turn.
        ig_account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
        if not ig_account_id:
            _log.debug("IG_BUSINESS_ACCOUNT_ID not set — Instagram publishing disabled")
            return

        if not page_token:
            _log.error("FB_PAGE_ACCESS_TOKEN is required for Instagram publishing")
            sys.exit(1)

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

    container_id is read from the snapshot too, and unlike the rest it is NOT immutable —
    it is read here because it belongs to the PREVIOUS attempt, which is exactly what makes
    it useful. Reading it before the claim is safe because this whole function runs under
    upload_instagram.lock: no other invocation of this script can be mutating it.
    """
    project_name = record["project_name"]
    video_path = record["video_local_path"]
    idem_key = record["idempotency_key"]
    attempt_count = record.get("attempt_count", 0)  # pre-claim value; claim() advances it by 1
    prior_container_id = record.get("container_id")
    prior_publish_attempted = bool(record.get("publish_attempted_at"))

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
        _handle_exhausted(
            page_token, prior_container_id, prior_publish_attempted,
            project_name, idem_key, video_path, chat_id,
        )
        return
    assert claim == "claimed", f"unexpected claim outcome: {claim!r}"

    # The job names the account it was approved FOR. Publishing to whatever
    # IG_BUSINESS_ACCOUNT_ID happens to hold right now would mean that reconfiguring a
    # client between approval and publish sends their video to a different Instagram
    # account — a wrong, irreversible post on someone's real account. The queued value
    # wins, and a disagreement is terminal rather than silently resolved in either
    # direction: neither value can be shown to be the intended one, so the only correct
    # action is to publish nothing and say so.
    queued_account_id = record.get("ig_business_account_id") or ""
    if queued_account_id != ig_account_id:
        _log.error(
            "Instagram account mismatch — refusing to publish: project=%s queued=%r current=%r",
            project_name, queued_account_id, ig_account_id,
        )
        instagram_state.mark_failed(idem_key)
        instagram_logger.log_upload_attempt_failed(
            project_name,
            attempt_count + 1,
            "queued Instagram account does not match the configured one",
        )
        _delete_local_file_if_last(video_path, project_name, idem_key)
        _send_alert(
            chat_id,
            f"⚠️ Instagram upload cancelled for {project_name} — this video was approved "
            f"for account {queued_account_id or '(none recorded)'}, but "
            f"IG_BUSINESS_ACCOUNT_ID is now {ig_account_id}. Nothing was published. "
            "Re-approve the video once the account configuration is settled.",
        )
        return

    attempt_number = attempt_count + 1
    instagram_logger.log_upload_started(project_name, attempt_number)

    share_file_id = None
    # The container currently in play, and whether anything has been published from it.
    # Both are seeded from the PREVIOUS attempt, because the previous attempt is exactly
    # what may have published without FieldKit learning of it — reading only this attempt's
    # flag is what made the old exhaustion alert claim an ordinary failure when the job had
    # in fact already reached a publish call on an earlier tick.
    active_container_id = prior_container_id
    publish_attempted = prior_publish_attempted

    def _register_share(file_id: str) -> None:
        """Record the cleanup obligation the instant the Drive file exists.

        Runs BEFORE the file is made public (see drive.create_temporary_share_link's
        on_file_id). Capturing the id here rather than parsing it out of the returned URL
        is what makes the obligation survive a share call that raises — including the
        ambiguous case where the permission was actually created and only its response
        was lost.
        """
        nonlocal share_file_id
        share_file_id = file_id
        instagram_state.record_share_intent(file_id, project_name)

    try:
        container_id = None
        if prior_container_id:
            outcome = _classify_prior_container(page_token, prior_container_id)
            if outcome == "published":
                _log.warning(
                    "container was already published by Instagram — recovering instead of "
                    "republishing: project=%s container_id=%s", project_name, prior_container_id,
                )
                _record_recovered(
                    project_name, idem_key, prior_container_id, video_path, chat_id
                )
                return
            if outcome == "reusable":
                _log.info(
                    "resuming an interrupted attempt on its existing container: "
                    "project=%s container_id=%s", project_name, prior_container_id,
                )
                container_id = prior_container_id
                # FINISHED is Instagram's own word for "ingested, NOT published". That is
                # authoritative, so whatever an earlier attempt may have tried, nothing was
                # published from this container and there is no unknown left to carry.
                publish_attempted = False

        if container_id is None:
            if not Path(video_path).exists():
                # Reachable only when there is no container to fall back on. Under the
                # coordinated-deletion rule the other platform can no longer pull the file
                # out from under us, so this means it genuinely vanished (manual cleanup,
                # disk loss). Terminal — there is nothing to upload — but alerted rather
                # than failing silently.
                _log.error("video file missing: project=%s path=%s", project_name, video_path)
                instagram_state.mark_failed(idem_key)
                instagram_logger.log_upload_attempt_failed(
                    project_name, attempt_number, "video file missing on disk"
                )
                _send_alert(
                    chat_id,
                    f"⚠️ Instagram upload failed for {project_name} — the approved video file "
                    "is missing on disk",
                )
                return

            share_link = drive.create_temporary_share_link(
                video_path, on_file_id=_register_share
            )
            container_id = instagram_api.create_media_container(
                page_token, queued_account_id, share_link
            )
            instagram_state.set_container_id(idem_key, container_id)
            # A brand new container: nothing has been published from it, and
            # set_container_id() has cleared the durable marker to match.
            active_container_id = container_id
            publish_attempted = False
            instagram_logger.log_container_created(project_name, container_id)

            instagram_api.wait_for_container(page_token, container_id)
            instagram_logger.log_container_ready(project_name, container_id)

        # Durable BEFORE the irreversible call, not after it returns — a marker written
        # afterwards would be missing in precisely the case it exists to describe.
        instagram_state.mark_publish_attempted(idem_key)
        active_container_id = container_id
        publish_attempted = True
        post_id = instagram_api.publish_container(page_token, queued_account_id, container_id)
        permalink = _fetch_permalink(page_token, post_id, project_name)
    except InstagramTokenError as exc:
        # Token expiry is terminal after ONE attempt (FR-008): retrying cannot fix it, and
        # burning the remaining attempt budget would only delay the alert the owner needs.
        # Checked before InstagramUploadError below — it is deliberately NOT a subclass.
        _revoke_share_link(share_file_id, project_name, chat_id)
        _log.error("Instagram token error: project=%s: %s", project_name, _safe_error(exc))
        quarantined = False
        if publish_attempted and active_container_id:
            # A publish was attempted and the token is what failed, so there is no way to
            # ask Instagram what happened — the very call that would answer is the one
            # returning "your token is invalid". Quarantine without attempting a
            # classification that cannot succeed. The drain will keep asking on later
            # ticks, and will get an answer the moment the Page is reconnected.
            _quarantine_unresolved_publish(
                active_container_id, project_name, idem_key, chat_id
            )
            quarantined = True
        instagram_state.mark_failed(idem_key)
        instagram_logger.log_token_expired(project_name)
        _delete_local_file_if_last(video_path, project_name, idem_key)
        alert = (
            f"⚠️ Instagram token expired — reconnect {project_name}'s account "
            "via generate_auth_link.py"
        )
        if quarantined:
            alert += (
                f". This upload had already reached the publish step, so the Reel MAY "
                f"already be live. {_UNRESOLVED_ADVICE}"
            )
        _send_alert(chat_id, alert)
        return
    except (InstagramUploadError, RuntimeError, OSError) as exc:
        # Transient: an Instagram API/network error, a container that reported ERROR or
        # never finished within the poll cap, or a Drive failure creating the share link.
        # RuntimeError/OSError are caught alongside InstagramUploadError because the Drive
        # helpers raise those — a Drive failure is just as retryable as an Instagram one.
        _revoke_share_link(share_file_id, project_name, chat_id)
        detail = _safe_error(exc)
        _log.error("upload failed: project=%s attempt=%d: %s", project_name, attempt_number, detail)
        instagram_logger.log_upload_attempt_failed(project_name, attempt_number, detail)
        if attempt_number >= _MAX_ATTEMPTS:
            outcome = _settle_terminal_container(
                page_token, active_container_id if publish_attempted else None,
                project_name, idem_key, chat_id,
            )
            if outcome == "published":
                # The Reel is live after all. This is a successful, terminal job — not a
                # failure — however badly the attempt that produced it went.
                _record_recovered(
                    project_name, idem_key, active_container_id, video_path, chat_id
                )
                return

            instagram_state.mark_failed(idem_key)
            instagram_logger.log_upload_exhausted(project_name)
            _delete_local_file_if_last(video_path, project_name, idem_key)
            _send_alert(
                chat_id, _exhausted_alert(project_name, unresolved=outcome == "unresolved")
            )
        else:
            # A KNOWN, caught failure with retries remaining: release the claim immediately so
            # the next attempt is gated by the short _COOLDOWN_SECONDS rather than the much
            # longer _UPLOAD_LEASE_SECONDS an abandoned claim would wait out. release_claim()
            # deliberately KEEPS container_id so the next attempt can reconcile against it.
            instagram_state.release_claim(idem_key)
        return

    # Success path. The share link is revoked first: Instagram has already ingested the
    # video by the time a container publishes, so nothing needs it to stay public.
    _revoke_share_link(share_file_id, project_name, chat_id)
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


def _safe_error(exc) -> str:
    """Render an exception as text with any embedded credential removed.

    tools/instagram_api.py already redacts everything it raises, so in the normal case
    this changes nothing. It is applied again here because this is where exception text
    fans out to TWO sinks — the durable activity log and stderr, which under cron becomes
    mail or a captured job log — and not every exception reaching this handler came from
    instagram_api (Drive and OS errors land here too). A permanent Page token is worth
    redacting twice; see tools/redaction.py.
    """
    return redact_secrets(str(exc))


def _classify_prior_container(page_token: str, container_id: str) -> str:
    """Ask Instagram what became of a container left behind by an earlier attempt.

    This is the FR-011 duplicate-publication guard. publish_container() is the irreversible
    external side effect and mark_published() is the durable record of it; a crash, a kill,
    or a lost HTTP response in between leaves Meta holding a live Reel that FieldKit has no
    record of. Without this check the next attempt simply publishes again, and a duplicate
    Reel on a real client account cannot be taken back.

    The container's own status_code is the authority, because Meta is the only party that
    knows what actually happened. Returns:

      "published" — status_code PUBLISHED. The Reel is already live. Record it; never
                    publish again.
      "reusable"  — status_code FINISHED. Ingested, not yet published: publishing THIS
                    container is the correct, duplicate-free way to finish the job.
      "restart"   — status_code ERROR or EXPIRED. Definitively never published and no
                    longer usable, so a fresh container is safe.

    Anything else — IN_PROGRESS, or a status_code this code does not recognise — raises
    InstagramUploadError and is handled as an ordinary retryable failure. That is the
    deliberate strict reading: IN_PROGRESS means a container that already blew through the
    300s poll cap once, which is spec.md's "stuck container" case and is better retried than
    waited on again; and an unrecognised value might be a publish state Meta has added since,
    so treating it as "safe to build a new container" could duplicate a post. Failing the
    attempt costs a retry from a bounded budget. Guessing costs a client a duplicate Reel.

    Network and API failures propagate for the same reason: not knowing a container's fate
    is never grounds for publishing another one.
    """
    status = instagram_api.get_container_status(page_token, container_id)
    if status == "PUBLISHED":
        return "published"
    if status == "FINISHED":
        return "reusable"
    if status in ("ERROR", "EXPIRED"):
        return "restart"
    raise InstagramUploadError(
        f"Container {container_id} is in state {status!r}; refusing to create a second "
        "container until its fate is known (FR-011)"
    )


def _record_recovered(
    project_name: str, idem_key: str, container_id: str, video_path: str, chat_id: str
) -> None:
    """Record a publish that Instagram reports as done but this system never observed.

    The job is terminal and successful — the Reel IS live — so it takes the success path:
    the idempotency key is retired, the shared video is released, and the owner is told
    their Reel is up. The message is deliberately honest about the gap rather than
    presenting a normal success, because FieldKit cannot name the post or link to it: the
    Graph API offers no container → media lookup.
    """
    instagram_state.record_recovered_publish(idem_key, project_name, container_id)
    instagram_logger.log_upload_recovered(project_name, container_id)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    _send_confirmation(
        chat_id,
        f"✅ Reel is live on Instagram for {project_name} — an earlier attempt was "
        "interrupted after publishing, so FieldKit could not record the post link. "
        "Nothing was posted twice. Open the account to see it.",
    )


def _handle_exhausted(
    page_token: str,
    prior_container_id: str | None,
    prior_publish_attempted: bool,
    project_name: str,
    idem_key: str,
    video_path: str,
    chat_id: str,
) -> None:
    """Resolve a job whose attempt budget ran out.

    claim_pending_upload() has already cleared the record — and, if that record carried an
    unresolved publish, has already quarantined it IN THE SAME TRANSACTION as the clear
    (see instagram_state._quarantine_unresolved_in_txn). So by the time this runs the
    obligation is durable whatever happens next: this function makes the system faster to
    resolve, not safer. A process that dies here leaves exactly the state the next tick's
    _drain_publish_reconciliations() expects.

    That ordering is the round-4 fix. Previously the clear was fsynced first and the
    quarantine was created afterwards by this function, so a process that died in the gap
    left neither a pending job nor an obligation — and the next re-approval could publish a
    duplicate Reel.
    """
    outcome = "unpublished"
    if prior_publish_attempted and prior_container_id:
        outcome = _reconcile_quarantined_container(
            page_token,
            {
                "container_id": prior_container_id,
                "project_name": project_name,
                "idempotency_key": idem_key,
            },
            chat_id,
            # Silent: this path sends ONE consolidated message below that also reports the
            # exhaustion, rather than two that each tell half the story.
            announce=False,
        )

    if outcome == "published":
        _log.warning(
            "attempt budget exhausted, but the last container was already published: "
            "project=%s container_id=%s", project_name, prior_container_id,
        )
        _delete_local_file_if_last(video_path, project_name, idem_key)
        _send_confirmation(chat_id, _recovered_message(project_name))
        return

    if outcome == "unresolved":
        # The activity-log record of the quarantine. claim_pending_upload() created the
        # ENTRY (atomically, which is the guarantee), but it does not write to the activity
        # log — the state module deliberately knows nothing about logging. Written here
        # rather than in _reconcile_quarantined_container() so it appears once, when the
        # container becomes unresolved, instead of on every later drain tick that would
        # bury it.
        instagram_logger.log_publish_unresolved(project_name, prior_container_id)

    _log.error("attempt budget exhausted: project=%s key=%s", project_name, idem_key)
    instagram_logger.log_upload_exhausted(project_name)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    _send_alert(chat_id, _exhausted_alert(project_name, unresolved=outcome == "unresolved"))


def _settle_terminal_container(
    page_token: str,
    container_id: str | None,
    project_name: str,
    idem_key: str,
    chat_id: str,
) -> str:
    """Establish, at terminal time, whether container_id put a Reel on the account.

    For the terminal path where the pending record still EXISTS and has not been
    quarantined — _process_upload()'s attempt-budget branch. (The exhausted-claim path is
    different: there the record is already gone and already quarantined, so it uses
    _reconcile_quarantined_container() instead.)

    Returns:
      "published"   — Instagram reports PUBLISHED. The Reel is live; the caller records it.
      "unpublished" — Instagram reports FINISHED, ERROR or EXPIRED. All three mean this
                      container never published. The record's unresolved-publish marker is
                      cleared here, which is what stops the mark_failed() that follows from
                      quarantining a video Instagram has just confirmed was never posted.
      "unresolved"  — no definitive answer. The container is QUARANTINED durably, the
                      idempotency key is blocked, and later ticks keep asking until
                      Instagram is definitive (see _drain_publish_reconciliations).

    A None container_id means no publish was ever attempted and returns "unpublished"
    without a call — Meta cannot have published something it was never asked to publish.
    """
    if not container_id:
        return "unpublished"
    try:
        if _classify_prior_container(page_token, container_id) == "published":
            return "published"
    except (InstagramTokenError, InstagramUploadError) as exc:
        _log.error(
            "could not settle the final container before giving up — quarantining: "
            "project=%s container_id=%s error=%s",
            project_name, container_id, _safe_error(exc),
        )
        _quarantine_unresolved_publish(container_id, project_name, idem_key, chat_id)
        return "unresolved"
    instagram_state.mark_publish_settled(idem_key)
    return "unpublished"


def _quarantine_unresolved_publish(
    container_id: str, project_name: str, idem_key: str, chat_id: str
) -> None:
    """Durably record that container_id may have published, and block its idempotency key.

    The alternative — warning the owner and moving on — is not a control. It asks a person
    to remember a caveat at the exact moment the system has told them the upload failed,
    and the cost of them forgetting is an irreversible duplicate Reel on a client's
    account. So the block is enforced in state, and the question keeps being asked.

    Explicit rather than relying on instagram_state's in-transaction safety net: this is
    what produces a TIMELY alert and the IG_UNKNOWN log line. The safety net guarantees the
    entry exists; this one makes sure a human hears about it at the right moment.
    """
    entry = instagram_state.record_publish_reconciliation(
        container_id, project_name=project_name, idempotency_key=idem_key
    )
    instagram_logger.log_publish_unresolved(project_name, container_id)
    if entry:
        _send_alert(chat_id, _unresolved_publish_alert(entry))


def _reconcile_quarantined_container(
    page_token: str, entry: dict, chat_id: str, *, announce: bool
) -> str:
    """Ask Instagram about ONE quarantined container and act on a definitive answer.

    Shared by the per-tick drain and the exhausted-claim path, which need identical
    semantics and differ only in who does the talking. Returns "published",
    "unpublished", or "unresolved".

    announce=True sends the resolution message itself, for the drain — nothing else will.
    announce=False stays silent so a caller can fold the outcome into one message of its
    own. The state changes and log lines happen either way; only the Telegram text is
    suppressed, and the alert-throttle stamp is still taken so the drain does not
    immediately repeat what the caller just said.
    """
    container_id = entry.get("container_id")
    project_name = entry.get("project_name", "unknown")
    idem_key = entry.get("idempotency_key", "")

    try:
        outcome = _classify_prior_container(page_token, container_id)
    except (InstagramTokenError, InstagramUploadError) as exc:
        _log.error(
            "publish outcome still unresolved: project=%s container_id=%s error=%s",
            project_name, container_id, _safe_error(exc),
        )
        updated = instagram_state.record_publish_reconciliation(
            container_id, project_name=project_name, idempotency_key=idem_key
        )
        if updated and announce:
            _send_alert(chat_id, _unresolved_publish_alert(updated))
        return "unresolved"

    if outcome == "published":
        # record_recovered_publish() retires the key permanently AND drops the quarantine
        # in one transaction; the explicit clear below is belt and braces for an entry
        # filed under a container id that key no longer matches.
        instagram_state.record_recovered_publish(idem_key, project_name, container_id)
        instagram_logger.log_upload_recovered(project_name, container_id)
        instagram_state.clear_publish_reconciliation(container_id)
        if announce:
            _send_confirmation(chat_id, _recovered_message(project_name))
        return "published"

    # "reusable" (FINISHED) and "restart" (ERROR/EXPIRED) all mean: never published.
    status = "FINISHED" if outcome == "reusable" else "ERROR_OR_EXPIRED"
    instagram_state.clear_publish_reconciliation(container_id)
    instagram_logger.log_publish_resolved(project_name, container_id, status)
    if announce:
        _send_alert(chat_id, _not_published_message(project_name))
    return "unpublished"


def _drain_publish_reconciliations(page_token: str, chat_id: str) -> None:
    """Keep asking Instagram about every quarantined container until it answers definitively.

    The counterpart to _drain_share_cleanups(), and deliberately the same shape: an
    unresolved external obligation, retried on every tick — including ticks with no job and
    ticks where Instagram is no longer enabled for this client — cleared only on a real
    answer, and re-alerted on a fixed interval so it cannot go quiet.

    Both definitive answers resolve it, in opposite directions:

      - PUBLISHED: the Reel is live. Recorded via record_recovered_publish(), which also
        retires the idempotency key permanently, and the owner is told it is up.
      - FINISHED / ERROR / EXPIRED: it never published. The quarantine lifts, the key is
        released, and the owner is told it is safe to re-approve.

    Anything else leaves the entry in place. That is the point. Nothing here ever drops an
    entry to keep the list short — an entry disappearing without an answer is the exact
    failure this whole mechanism exists to prevent — so a backlog is reported rather than
    trimmed; see _report_quarantine_backlog().
    """
    entries = instagram_state.list_publish_reconciliations()
    _report_quarantine_backlog(entries)
    for entry in entries:
        if not entry.get("container_id"):
            continue
        _reconcile_quarantined_container(page_token, entry, chat_id, announce=True)


def _warn_quarantines_unreachable(chat_id: str) -> None:
    """Report quarantines that cannot even be CHECKED because there is no Page token.

    Without this the credential-absent case was the one silent failure left in the
    mechanism: entries kept blocking re-approval correctly, but nothing could ask
    Instagram anything and nothing said so, so an operator who removed
    FB_PAGE_ACCESS_TOKEN would see a video permanently un-postable with no explanation
    anywhere. Every other failure mode — an invalid token, a Graph outage — already
    retries every tick and re-alerts daily.

    counts_as_check=False: the alert schedule advances, the check count does not. No
    check happened, and a count that claimed otherwise would misrepresent how hard
    FieldKit has actually tried.
    """
    entries = instagram_state.list_publish_reconciliations()
    if not entries:
        return
    _report_quarantine_backlog(entries)
    _log.error(
        "%d unresolved Instagram publish(es) cannot be checked — FB_PAGE_ACCESS_TOKEN is "
        "not set. These keys stay blocked until the token is restored.",
        len(entries),
    )
    for entry in entries:
        container_id = entry.get("container_id")
        if not container_id:
            continue
        updated = instagram_state.record_publish_reconciliation(
            container_id,
            project_name=entry.get("project_name", "unknown"),
            idempotency_key=entry.get("idempotency_key", ""),
            counts_as_check=False,
        )
        if updated:
            _send_alert(
                chat_id,
                f"⚠️ Instagram: the Reel for {updated.get('project_name', 'unknown')} MAY "
                "already be live, and FieldKit cannot find out — FB_PAGE_ACCESS_TOKEN is "
                "not set, so it cannot ask Instagram anything. Re-approving this video "
                "stays blocked until the token is restored and the check can run. "
                "Reconnect the Page via generate_auth_link.py.",
            )


def _report_quarantine_backlog(entries: list) -> None:
    """Make a growing quarantine list visible, without ever shortening it.

    pending_publish_reconciliations has no size or age cap by design. Capping it would mean
    dropping an entry — releasing an idempotency key while a Reel's fate is still unknown —
    which is precisely the duplicate this mechanism prevents, so growth is the SAFE
    direction and must stay that way.

    What growth must not be is invisible. Past the threshold every tick logs the backlog,
    and the per-entry alerts carry the count, so "one unlucky upload" is distinguishable
    from "this has been quietly accumulating for a month" without anyone reading the state
    file. Resolving them is an operator action, documented in docs/instagram/README.md.
    """
    if len(entries) < _QUARANTINE_BACKLOG_THRESHOLD:
        return
    oldest = min(
        (e.get("recorded_at") or "" for e in entries), default="unknown"
    ) or "unknown"
    _log.error(
        "Instagram publish quarantine backlog: %d unresolved container(s), oldest since "
        "%s. Each one blocks its video from being re-approved. See "
        "docs/instagram/README.md for how to resolve them.",
        len(entries), oldest,
    )


def _recovered_message(project_name: str) -> str:
    """The confirmation for a publish discovered after the fact. One wording, two callers."""
    return (
        f"✅ Resolved: the Instagram Reel for {project_name} IS live — an earlier attempt "
        "published it but could not confirm it at the time. Nothing was posted twice, and "
        "FieldKit could not read back the post link, so open the account to see it."
    )


def _not_published_message(project_name: str) -> str:
    """The confirmation that a quarantined container never went live."""
    return (
        f"✅ Resolved: the Instagram Reel for {project_name} was NOT published — Instagram "
        "confirms the upload never went live. Nothing is on the account, and you can "
        "safely re-approve this video to try again."
    )


# Appended to any alert about a publish whose outcome is unknown. Kept in one place so the
# promise is worded identically everywhere it is made — and so it stays true: FieldKit
# really does keep checking, and really does block the re-approval until it knows.
_UNRESOLVED_ADVICE = (
    "FieldKit is still checking with Instagram and will tell you as soon as it knows. "
    "Re-approving this video is blocked until then, so it cannot be posted twice. "
    "Do NOT post it manually before you hear back."
)


def _unresolved_publish_alert(entry: dict) -> str:
    """Build the admin alert for a publish whose outcome could not be established.

    The first alert and every re-escalation use this same wording, differing only in the
    check count, so the message never promises follow-up it does not deliver.

    Carries the size of the whole quarantine list when it has grown past
    _QUARANTINE_BACKLOG_THRESHOLD. One stuck upload and a backlog that has been quietly
    accumulating for a month produce the same per-entry message otherwise, and they call
    for very different responses.
    """
    project_name = entry.get("project_name", "unknown")
    container_id = entry.get("container_id", "unknown")
    attempts = entry.get("attempts", 1)
    since = entry.get("recorded_at", "unknown")
    backlog = len(instagram_state.list_publish_reconciliations())
    suffix = ""
    if backlog >= _QUARANTINE_BACKLOG_THRESHOLD:
        suffix = (
            f"\n\nNote: {backlog} Instagram uploads are now in this state, the oldest "
            "since {since}. Each one blocks its own video from being re-approved. Nothing "
            "is ever dropped from that list, so it will keep growing until the underlying "
            "problem is fixed — see docs/instagram/README.md."
        ).format(since=since)
    return (
        f"⚠️ Instagram: the Reel for {project_name} MAY already be live. FieldKit asked "
        f"Instagram to publish it but never learned whether it succeeded, and cannot get "
        f"a definitive answer (container {container_id}).\n"
        f"Checks so far: {attempts}, first unresolved: {since}.\n"
        + _UNRESOLVED_ADVICE
        + suffix
    )


def _exhausted_alert(project_name: str, unresolved: bool) -> str:
    """Build the terminal-failure alert, distinguishing "did not post" from "may have".

    An owner who believes nothing was posted will re-approve the video. If the job actually
    reached publish_container() and only lost its response, that re-approval is how a
    duplicate Reel gets onto a client's account — so the two cases cannot share one message.
    When the outcome is unknown the re-approval is blocked in state as well as discouraged
    here; this message exists to explain the block, not to be the block.
    """
    if unresolved:
        return (
            f"⚠️ Instagram upload failed for {project_name} after {_MAX_ATTEMPTS} attempts, "
            "and the Reel MAY already be live — the publish step was reached and Instagram "
            f"has not confirmed either way. {_UNRESOLVED_ADVICE}"
        )
    return (
        f"⚠️ Instagram upload failed for {project_name} after {_MAX_ATTEMPTS} attempts "
        "— check logs. Nothing was published, so the video can be re-approved."
    )


def _revoke_share_link(file_id: str | None, project_name: str, chat_id: str) -> None:
    """Revoke the temporary public Drive link, if one was created this attempt.

    Takes the file id captured by _register_share() before the file was ever made public,
    not the returned URL — so a share call that raised after creating the permission is
    still revocable. Revoking a file that never actually became public is a harmless no-op.

    A revoke failure must not undo a live post or mask the real upload error, so it does
    not raise here. But it is emphatically NOT treated as success: the file id stays in
    instagram_state's pending-cleanup list, is retried on every later tick by
    _drain_share_cleanups(), and the admin is alerted, naming the specific file, so a
    public link can never be left dangling with no record of it.
    """
    if not file_id:
        return
    try:
        drive.revoke_share_link(file_id)
    except RuntimeError as exc:
        _log.error(
            "failed to revoke temporary share link — video remains publicly reachable: "
            "project=%s file_id=%s error=%s",
            project_name, file_id, exc,
        )
        entry = instagram_state.record_share_cleanup(file_id, project_name)
        if entry:
            _send_alert(chat_id, _share_cleanup_alert(entry))
        return
    # Revoked for real — retire the obligation registered before the file was shared.
    instagram_state.clear_share_cleanup(file_id)


def _drain_share_cleanups(chat_id: str) -> None:
    """Retry every previously-failed share-link revocation, clearing the ones that succeed.

    Runs on every tick, whether or not there is an upload job AND whether or not Instagram
    is still configured for this client, because a dangling public link is a standing
    privacy problem that outlives both the job that created it and the feature being
    enabled at all.

    An entry that fails again stays recorded with its attempt count bumped, and the admin
    is re-alerted on the schedule instagram_state.record_share_cleanup() decides — so a
    link that never gets revoked keeps surfacing instead of being mentioned once and then
    silently retried forever.
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
            updated = instagram_state.record_share_cleanup(file_id, project_name)
            if updated:
                _send_alert(chat_id, _share_cleanup_alert(updated))
            continue
        instagram_state.clear_share_cleanup(file_id)
        _log.info(
            "share-link revocation succeeded on retry: project=%s file_id=%s",
            project_name, file_id,
        )


def _share_cleanup_alert(entry: dict) -> str:
    """Build the admin alert for a Drive share link that could not be revoked.

    The first alert and every re-escalation use this same wording, differing only in the
    attempt count, so the message never promises follow-up it does not deliver: FieldKit
    really does keep retrying, and really does keep reminding.
    """
    attempts = entry.get("attempts", 1)
    project_name = entry.get("project_name", "unknown")
    file_id = entry.get("file_id", "unknown")
    since = entry.get("recorded_at", "unknown")
    return (
        f"⚠️ Instagram: could not remove the temporary public link for {project_name} "
        f"(Drive file {file_id}). The video may still be publicly reachable.\n"
        f"Failed attempts: {attempts}, first failed: {since}.\n"
        "FieldKit keeps retrying every cron tick and will remind you daily until it "
        "succeeds. To fix it now, remove the file's 'Anyone with the link' permission "
        "in Drive."
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
