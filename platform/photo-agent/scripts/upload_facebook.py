"""
upload_facebook.py — Cron script: post the pending Facebook video upload.

Usage:
    python3 scripts/upload_facebook.py
    python3 scripts/upload_facebook.py --source cron

Reads the pending VideoUploadJob from facebook_state.json, uploads the video
to the linked Facebook Page via Graph API, and sends a Telegram confirmation.

Cron re-entrancy lock (issue #34 follow-up): upload_facebook.lock, acquired
non-blocking and held for the ENTIRE duration of processing a claimed job —
mirrors check_approval.py's _try_acquire_check_lock. This is the actual
guarantee against two overlapping cron invocations both calling the Facebook
API for the same job: a lease-timeout-based reclaim alone (see
claim_pending_upload()) cannot distinguish a crashed holder from a merely
slow-but-still-live one — flock is what can, since the OS releases it
automatically on process death but never on a process that's simply still
running. A second invocation that can't acquire this lock exits immediately,
before ever touching facebook_state.

Retry and failure-recovery logic (US3):
  - Claiming (tools/facebook_state.py's claim_pending_upload()) is a single
    atomic exclusive-lock transaction that checks staleness, cooldown, and the
    attempt budget, and transitions the job to 'uploading' — all before this
    script ever calls the Facebook API. Combined with the re-entrancy lock
    above, this is what stops two overlapping cron invocations (e.g. a slow
    upload still running when the next minute's tick starts) from both
    claiming the same job and both posting a real duplicate (issue #34).
  - attempt_count/last_attempt_at are persisted by the claim itself, BEFORE
    the Facebook API call — not only after a failure — so a process killed
    mid-upload still leaves a bounded, cooldown-gated trail instead of being
    retried immediately forever.
  - Cooldown: if the last attempt was within 60 seconds, the claim declines.
  - Retry limit: 3 attempts. After the 3rd failure, marks the job as failed
    and sends a Telegram alert.
  - Token expiry (FacebookTokenError): marks failed immediately after just
    this one attempt (does not wait for the retry budget to exhaust), and
    alerts the admin to reconnect the Page.

Duplicate-publish reconciliation (issue #78). The upload is SESSIONIZED
(facebook_api.upload_video()): its start phase returns a video_id before any
bytes move, and only its finish phase publishes. That video_id is persisted
(facebook_state.set_upload_session) straight after start, and a publish marker
(facebook_state.mark_publish_attempted) BEFORE finish is sent. So when finish
succeeds at Meta but its response is lost — the case that used to end in
mark_failed() and then a second post on re-approval — the record names the exact
video to ask about:

  - the next attempt asks Facebook about that video before uploading anything:
    published → recorded as a recovered publish (key retired, nothing re-posted);
    definitively not published → settled, then a fresh upload; no definitive
    answer → the attempt fails WITHOUT uploading;
  - if the job goes terminal with the answer still unknown, the video is
    quarantined in facebook_state's pending_publish_reconciliations (the
    chokepoint does this even if a call site forgets), which blocks re-approval
    of the key; _drain_publish_reconciliations() re-asks every tick and lifts it
    only on a definitive answer about that video_id.

Only Facebook's status for that specific video_id is ever treated as an answer.
Matching the Page's recent videos by time is not, because it cannot tell
FieldKit's upload from one a human posted.

A resolved job (published, or terminally failed) always clears
pending_facebook_upload (see tools/facebook_state.py) — claim_pending_upload()
additionally self-heals a stale or pre-fix state file (an already-published
idempotency_key, or a status already 'failed', found still sitting in
pending_facebook_upload) by clearing it instead of reprocessing (issue #34).

Local video cleanup is COORDINATED, not owned by this script (Feature 005). This
script used to delete the approved video on its own successful publish, which was
correct while it was the file's only consumer. Feature 005 added a second,
independently-scheduled consumer (upload_instagram.py) of the SAME file, so deleting
on one's own success would pull the file out from under the other platform's
still-pending job — which then fails terminally, publishing nothing and alerting
nobody. Deletion now happens only when every enabled platform has resolved this
approval; see tools/upload_cleanup.py.

FB_APP_SECRET is never read here (used only by generate_auth_link.py).
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
    facebook_api,
    facebook_logger,
    facebook_state,
    paths,
    telegram_api,
    upload_cleanup,
    worker_health,
)
from tools.facebook_api import FacebookTokenError, FacebookUploadError
from tools.redaction import redact_secrets

_log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_COOLDOWN_SECONDS = 60
# How long a claim (status='uploading') is presumed still genuinely in-progress before
# claim_pending_upload() treats it as an abandoned/crashed attempt and allows reclaiming it.
# Deliberately much longer than _COOLDOWN_SECONDS and any realistic video upload duration for
# these short social-media clips — a lease that expires while a legitimate upload is still
# running would let a second cron tick reclaim and re-call the Facebook API for the same job.
# See claim_pending_upload()'s docstring for the full tradeoff.
_UPLOAD_LEASE_SECONDS = 900


def _try_acquire_upload_lock() -> "IO | None":
    """Try to acquire upload_facebook.lock exclusively (non-blocking).

    Mirrors check_approval.py's _try_acquire_check_lock: this is the actual mutual-exclusion
    guarantee for a claimed job's ENTIRE processing (claim through mark_published/mark_failed/
    release_claim), not just the state-file transitions in between — see the module docstring
    for why the lease-timeout reclaim in claim_pending_upload() cannot substitute for this.

    Returns the open lock file object on success, or None if another upload_facebook instance
    is already running. The caller must close the returned file object to release the lock.
    """
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"]) / "photo-agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "upload_facebook.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def _delete_local_file_if_last(video_local_path: str, project_name: str, idem_key: str) -> None:
    """Delete the approved video, but only once every OTHER enabled platform is done with it.

    MUST be called after this job's own terminal state is recorded — see
    tools/upload_cleanup.py for why that ordering is what makes the check race-free.
    """
    waiting = upload_cleanup.other_platforms_pending(
        idem_key, platform=upload_cleanup.FACEBOOK
    )
    if waiting:
        _log.info(
            "leaving local video in place — still needed by %s: project=%s",
            ", ".join(waiting), project_name,
        )
        return
    _delete_local_file(video_local_path, project_name)


def _delete_local_file(video_local_path: str, project_name: str) -> None:
    """Delete the local temp video file. Best-effort: logs on failure, never raises."""
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


def main(argv=None) -> None:
    """Entry point — validate env, load pending job, attempt upload."""
    parser = argparse.ArgumentParser(description="Upload the pending Facebook video.")
    parser.add_argument(
        "--source",
        choices=["cron"],
        default=None,
        help="Invocation label for logging (informational only).",
    )
    args = parser.parse_args(argv)
    if args.source:
        _log.debug("invoked from source=%s", args.source)

    page_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    page_id = os.environ.get("FB_PAGE_ID", "")
    chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")

    # Stamp liveness before the config gate below: the heartbeat attests that this cron
    # ENTRY exists and fired, which is separate from whether Facebook is configured for
    # this client. tools/upload_cleanup.py consults it before retaining a shared video on
    # this platform's behalf, and check_approval.py before queueing a job for it.
    worker_health.record_heartbeat(upload_cleanup.FACEBOOK)

    if not page_token or not page_id:
        _log.error("FB_PAGE_ACCESS_TOKEN and FB_PAGE_ID are required")
        sys.exit(1)

    lock_f = _try_acquire_upload_lock()
    if lock_f is None:
        _log.debug("another upload_facebook instance is running — exiting")
        return
    try:
        # Recovery sweep for approved videos no live record can still be waiting on.
        # Deliberately unconditional and ahead of the pending-job check: it exists to catch
        # files the coordinated delete could not, and those by definition have no job left
        # to carry them. Running it from BOTH cron workers is what keeps the sweep alive
        # when only one of the two is deployed.
        upload_cleanup.sweep_orphaned_videos()

        # Unresolved publishes (issue #78) are re-checked every tick, ahead of and
        # independent of any pending job: a quarantined video may be live on the Page
        # with nothing recording it, and its idempotency key stays blocked until
        # Facebook answers.
        _drain_publish_reconciliations(page_token, chat_id)

        record = facebook_state.get_pending_upload()
        if record is None:
            _log.debug("no pending facebook upload — exiting")
            return

        _process_upload(record, page_token, page_id, chat_id)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _process_upload(record: dict, page_token: str, page_id: str, chat_id: str) -> None:
    """Claim and attempt to upload the video described by record.

    record is only a snapshot (from main()'s get_pending_upload()) used here for its immutable
    fields — project_name/video_local_path/page_id/idempotency_key never change across a
    record's lifetime. Every decision about whether and how to proceed (staleness, cooldown,
    attempt budget, claiming) is made by claim_pending_upload() against the CURRENT,
    freshly-locked state under one exclusive-lock transaction — not by reasoning from this
    possibly-stale snapshot — so two overlapping invocations of this script can never both
    observe an unclaimed job and both call the Facebook API (issue #34 follow-up).

    video_id / publish_attempted_at are read from the snapshot too, and are NOT immutable:
    they belong to the PREVIOUS attempt, which is exactly what makes them useful. Reading
    them before the claim is safe because this whole function runs under
    upload_facebook.lock, so no other invocation can be mutating them (issue #78).
    """
    project_name = record["project_name"]
    video_path = record["video_local_path"]
    idem_key = record["idempotency_key"]
    attempt_count = record.get("attempt_count", 0)  # pre-claim value; claim() advances it by 1
    prior_video_id = record.get("video_id")
    prior_publish_attempted = bool(prior_video_id) and bool(record.get("publish_attempted_at"))

    claim = facebook_state.claim_pending_upload(
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
            page_token, prior_video_id if prior_publish_attempted else None,
            project_name, idem_key, video_path, chat_id,
        )
        return
    assert claim == "claimed", f"unexpected claim outcome: {claim!r}"

    attempt_number = attempt_count + 1

    # A previous attempt sent FINISH and never learned the outcome. Nothing new may be
    # uploaded until Facebook says what became of THAT video — otherwise a lost response
    # becomes a second post (issue #78).
    if prior_publish_attempted:
        outcome, observed = _classify_video(page_token, prior_video_id, project_name)
        if outcome == "published":
            _log.warning(
                "video was already published by Facebook — recovering instead of "
                "re-uploading: project=%s video_id=%s", project_name, prior_video_id,
            )
            _record_recovered(project_name, idem_key, prior_video_id, observed, video_path, chat_id)
            return
        if outcome == "not_published":
            facebook_state.mark_publish_settled(idem_key, prior_video_id, observed)
        else:
            _fail_attempt_unresolved(
                prior_video_id, project_name, idem_key, video_path, chat_id, attempt_number,
                f"previous publish of video {prior_video_id} is still unresolved ({observed})",
            )
            return

    # Video file must exist before we call the API. Checked only after a successful claim — the
    # claim is the single gate against a concurrent duplicate regardless of ordering here, and
    # this way the filesystem is only ever touched for a job we've actually secured.
    if not Path(video_path).exists():
        _log.error("video file missing: project=%s path=%s", project_name, video_path)
        facebook_state.mark_failed(idem_key)
        # Alert rather than failing silently — matches upload_instagram.py's equivalent
        # path. A vanished video is a real, terminal failure the owner needs to know about.
        _send_alert(
            chat_id,
            f"⚠️ Facebook upload failed for {project_name} — the approved video file "
            "is missing on disk",
        )
        return

    facebook_logger.log_upload_started(project_name, attempt_number)

    # The video this attempt created, and whether FINISH has been attempted for it. Set by
    # the callbacks below, each only AFTER its durable state write has succeeded.
    active_video_id = None
    publish_attempted = False

    def _on_session_started(video_id: str, upload_session_id: str) -> None:
        """Persist the pre-known handle before any bytes are transferred."""
        nonlocal active_video_id
        facebook_state.set_upload_session(idem_key, video_id, upload_session_id)
        active_video_id = video_id

    def _on_before_finish() -> None:
        """Durably mark the publish as attempted BEFORE the irreversible FINISH."""
        nonlocal publish_attempted
        facebook_state.mark_publish_attempted(idem_key, active_video_id)
        publish_attempted = True

    try:
        post_id = facebook_api.upload_video(
            page_token, page_id, video_path,
            on_session_started=_on_session_started,
            on_before_finish=_on_before_finish,
        )
    except FacebookTokenError as exc:
        _log.error("Facebook token error: project=%s: %s", project_name, _safe_error(exc))
        quarantined = False
        if publish_attempted and active_video_id:
            # FINISH was sent and the token is what failed, so the status check that would
            # answer cannot succeed either. Quarantine; the drain keeps asking once the Page
            # is reconnected.
            _quarantine_unresolved_publish(active_video_id, project_name, idem_key, chat_id)
            quarantined = True
        facebook_state.mark_failed(idem_key)
        facebook_logger.log_token_expired(project_name)
        _delete_local_file_if_last(video_path, project_name, idem_key)
        alert = (
            f"⚠️ Facebook token expired for {project_name} — reconnect your Page via "
            "generate_auth_link.py"
        )
        if quarantined:
            alert += (
                ". This upload had already reached the publish step, so the video MAY "
                f"already be live. {_UNRESOLVED_ADVICE}"
            )
        _send_alert(chat_id, alert)
        return
    except FacebookUploadError as exc:
        detail = _safe_error(exc)
        _log.error("upload failed: project=%s attempt=%d: %s", project_name, attempt_number, detail)
        facebook_logger.log_upload_attempt_failed(project_name, attempt_number, detail)
        if attempt_number >= _MAX_ATTEMPTS:
            outcome = "unpublished"
            if publish_attempted and active_video_id:
                outcome = _settle_terminal_video(
                    page_token, active_video_id, project_name, idem_key, chat_id
                )
            if outcome == "published":
                _record_recovered(
                    project_name, idem_key, active_video_id, facebook_api.OBSERVED_PUBLISHED,
                    video_path, chat_id,
                )
                return
            facebook_state.mark_failed(idem_key)
            facebook_logger.log_upload_exhausted(project_name)
            _delete_local_file_if_last(video_path, project_name, idem_key)
            _send_alert(chat_id, _exhausted_alert(project_name, unresolved=outcome == "unresolved"))
        else:
            # A KNOWN, caught failure with retries remaining: release the claim immediately so
            # the next attempt is gated by the short _COOLDOWN_SECONDS, not the much longer
            # _UPLOAD_LEASE_SECONDS a genuinely abandoned/crashed claim would otherwise wait out.
            # video_id and the publish marker are kept, so the next attempt reconciles first.
            facebook_state.release_claim(idem_key)
        return

    # Success path. mark_published() is what makes this job terminal in the state file, and
    # it has to happen BEFORE the coordination check — see tools/upload_cleanup.py.
    facebook_state.mark_published(idem_key, post_id)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    facebook_logger.log_upload_published(project_name, post_id)
    post_url = f"https://www.facebook.com/{post_id}"
    _send_confirmation(chat_id, f"✅ Video live on Facebook! {post_url}")


def _safe_error(exc) -> str:
    """Render an exception as text with any embedded credential removed.

    facebook_api already redacts what it raises; applied again here because this is where
    exception text fans out to the durable activity log and stderr. See tools/redaction.py.
    """
    return redact_secrets(str(exc))


def _classify_video(page_token: str, video_id: str, project_name: str) -> tuple[str, str]:
    """Ask Facebook what became of video_id. Returns ("published" | "not_published" |
    "unknown", observed).

    Only facebook_api.get_video_publish_state()'s definitive readings count. Any failure to
    get an answer — network, Graph error, expired token — is "unknown", never "not
    published": not knowing a video's fate is never grounds for uploading it again.
    """
    try:
        return facebook_api.get_video_publish_state(page_token, video_id)
    except (FacebookTokenError, FacebookUploadError) as exc:
        detail = _safe_error(exc)
        _log.error(
            "could not read publish state: project=%s video_id=%s error=%s",
            project_name, video_id, detail,
        )
        return "unknown", detail


def _record_recovered(
    project_name: str, idem_key: str, video_id: str, observed: str, video_path: str, chat_id: str
) -> None:
    """Record a publish Facebook reports as live but this system never observed, and take the
    success path: key retired, shared video released, owner told honestly what happened."""
    facebook_state.record_recovered_publish(idem_key, project_name, video_id, observed)
    facebook_logger.log_upload_recovered(project_name, video_id)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    _send_confirmation(chat_id, _recovered_message(project_name, video_id))


def _fail_attempt_unresolved(
    video_id: str,
    project_name: str,
    idem_key: str,
    video_path: str,
    chat_id: str,
    attempt_number: int,
    detail: str,
) -> None:
    """Count an attempt that could not proceed because a previous publish is unresolved.

    Nothing is uploaded. Below the budget the claim is released and the next tick asks
    again; on the last attempt the video is quarantined, and mark_failed() drops the job —
    the quarantine keeps the key blocked.
    """
    _log.error("upload blocked: project=%s attempt=%d: %s", project_name, attempt_number, detail)
    facebook_logger.log_upload_attempt_failed(project_name, attempt_number, detail)
    if attempt_number < _MAX_ATTEMPTS:
        facebook_state.release_claim(idem_key)
        return
    _quarantine_unresolved_publish(video_id, project_name, idem_key, chat_id)
    facebook_state.mark_failed(idem_key)
    facebook_logger.log_upload_exhausted(project_name)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    _send_alert(chat_id, _exhausted_alert(project_name, unresolved=True))


def _handle_exhausted(
    page_token: str,
    attempted_video_id: str | None,
    project_name: str,
    idem_key: str,
    video_path: str,
    chat_id: str,
) -> None:
    """Resolve a job whose attempt budget ran out at claim time.

    claim_pending_upload() has already cleared the record and, if it carried an open
    publish, quarantined it in the same write. This asks Facebook once, silently, so the
    single message sent below tells the whole story.
    """
    outcome = "unpublished"
    if attempted_video_id:
        outcome = _reconcile_quarantined_video(
            page_token,
            {"video_id": attempted_video_id, "project_name": project_name, "idempotency_key": idem_key},
            chat_id,
            announce=False,
        )
    if outcome == "published":
        _log.warning(
            "attempt budget exhausted, but the video was already published: project=%s "
            "video_id=%s", project_name, attempted_video_id,
        )
        _delete_local_file_if_last(video_path, project_name, idem_key)
        _send_confirmation(chat_id, _recovered_message(project_name, attempted_video_id))
        return
    if outcome == "unresolved":
        facebook_logger.log_publish_unresolved(project_name, attempted_video_id)

    # claim_pending_upload() has already cleared the record, so this job is terminal:
    # release the shared video too, or a crash during the final attempt would leave it
    # on disk with nothing left to clean it up.
    _log.error("attempt budget exhausted: project=%s key=%s", project_name, idem_key)
    facebook_logger.log_upload_exhausted(project_name)
    _delete_local_file_if_last(video_path, project_name, idem_key)
    _send_alert(chat_id, _exhausted_alert(project_name, unresolved=outcome == "unresolved"))


def _settle_terminal_video(
    page_token: str, video_id: str, project_name: str, idem_key: str, chat_id: str
) -> str:
    """At terminal failure, establish whether video_id went live. The record still exists.

    Returns "published" (caller records it), "unpublished" (Facebook said no; the marker
    is settled so mark_failed() does not quarantine), or "unresolved" (quarantined with an
    alert; the key stays blocked).
    """
    outcome, observed = _classify_video(page_token, video_id, project_name)
    if outcome == "published":
        return "published"
    if outcome == "not_published":
        facebook_state.mark_publish_settled(idem_key, video_id, observed)
        return "unpublished"
    _quarantine_unresolved_publish(video_id, project_name, idem_key, chat_id)
    return "unresolved"


def _quarantine_unresolved_publish(
    video_id: str, project_name: str, idem_key: str, chat_id: str
) -> None:
    """Durably record that video_id may have published, block its key, and alert.

    facebook_state's chokepoint guarantees the entry exists whenever the job is dropped;
    this explicit call is what produces a timely alert and the FB_UNKNOWN log line.
    """
    entry = facebook_state.record_publish_reconciliation(
        video_id, project_name=project_name, idempotency_key=idem_key
    )
    facebook_logger.log_publish_unresolved(project_name, video_id)
    if entry:
        _send_alert(chat_id, _unresolved_publish_alert(entry))


def _reconcile_quarantined_video(
    page_token: str, entry: dict, chat_id: str, *, announce: bool
) -> str:
    """Ask Facebook about ONE quarantined video and act only on a definitive answer.

    Returns "published", "unpublished", or "unresolved". announce=False suppresses the
    Telegram text (the caller sends its own), not the state changes or log lines.
    """
    video_id = entry.get("video_id")
    project_name = entry.get("project_name", "unknown")
    idem_key = entry.get("idempotency_key", "")

    outcome, observed = _classify_video(page_token, video_id, project_name)
    if outcome == "published":
        facebook_state.record_recovered_publish(idem_key, project_name, video_id, observed)
        facebook_logger.log_upload_recovered(project_name, video_id)
        facebook_state.clear_publish_reconciliation(video_id, observed)
        if announce:
            _send_confirmation(chat_id, _recovered_message(project_name, video_id))
        return "published"
    if outcome == "not_published":
        facebook_state.clear_publish_reconciliation(video_id, observed)
        facebook_logger.log_publish_resolved(project_name, video_id, observed)
        if announce:
            _send_alert(chat_id, _not_published_message(project_name))
        return "unpublished"

    updated = facebook_state.record_publish_reconciliation(
        video_id, project_name=project_name, idempotency_key=idem_key
    )
    if updated and announce:
        _send_alert(chat_id, _unresolved_publish_alert(updated))
    return "unresolved"


def _drain_publish_reconciliations(page_token: str, chat_id: str) -> None:
    """Re-ask Facebook about every quarantined video, every tick, until it is definitive.

    Mirrors upload_instagram.py's drain. Nothing here drops an entry without an answer
    about that specific video_id — in particular, never on the strength of a recent-video
    match on the Page (issue #78).
    """
    for entry in facebook_state.list_publish_reconciliations():
        if not entry.get("video_id"):
            continue
        _reconcile_quarantined_video(page_token, entry, chat_id, announce=True)


# Appended to any alert about a publish whose outcome is unknown, so the promise is worded
# identically everywhere — and stays true: FieldKit keeps checking, and blocks re-approval.
_UNRESOLVED_ADVICE = (
    "FieldKit is still checking with Facebook and will tell you as soon as it knows. "
    "Re-approving this video is blocked until then, so it cannot be posted twice. "
    "Do NOT post it manually before you hear back."
)


def _unresolved_publish_alert(entry: dict) -> str:
    """The admin alert for a publish whose outcome could not be established.

    Names the video id so a person can look at it on the Page themselves — evidence for a
    human; FieldKit itself only acts on Facebook's status for that id.
    """
    project_name = entry.get("project_name", "unknown")
    video_id = entry.get("video_id", "unknown")
    attempts = entry.get("attempts", 1)
    since = entry.get("recorded_at", "unknown")
    return (
        f"⚠️ Facebook: the video for {project_name} MAY already be live. FieldKit asked "
        f"Facebook to publish it but never learned whether it succeeded, and cannot get a "
        f"definitive answer (video {video_id}, https://www.facebook.com/{video_id}).\n"
        f"Checks so far: {attempts}, first unresolved: {since}.\n"
        + _UNRESOLVED_ADVICE
    )


def _recovered_message(project_name: str, video_id: str) -> str:
    """The confirmation for a publish discovered after the fact."""
    return (
        f"✅ Video live on Facebook for {project_name} — an earlier attempt published it "
        f"but could not confirm it at the time. Nothing was posted twice. "
        f"https://www.facebook.com/{video_id}"
    )


def _not_published_message(project_name: str) -> str:
    """The confirmation that a quarantined video never went live."""
    return (
        f"✅ Resolved: the Facebook video for {project_name} was NOT published — Facebook "
        "confirms the upload never went live. You can safely re-approve this video."
    )


def _exhausted_alert(project_name: str, unresolved: bool) -> str:
    """The terminal-failure alert, distinguishing "did not post" from "may have posted"."""
    if unresolved:
        return (
            f"⚠️ Facebook upload failed for {project_name} after {_MAX_ATTEMPTS} attempts, "
            "and the video MAY already be live — the publish step was reached and Facebook "
            f"has not confirmed either way. {_UNRESOLVED_ADVICE}"
        )
    return (
        f"⚠️ Facebook upload failed for {project_name} after {_MAX_ATTEMPTS} attempts — check logs"
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
