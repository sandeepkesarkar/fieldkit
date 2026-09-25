"""
Cross-platform coordination for deleting the approved video file.

One approval produces ONE video file on disk and, since Feature 005, up to TWO
independent consumers of it: upload_facebook.py and upload_instagram.py. Each runs
on its own cron schedule, with its own lock and its own state file, in whichever
order the schedule happens to interleave them.

That makes "delete the local file after I publish" wrong for both of them. Whichever
script finished first would delete a file the other still needs, and the second would
find it missing and terminally discard its job — a silent, permanent failure with no
retry and no alert. (Before Feature 005 there was only one consumer, so
upload_facebook.py deleting on its own success was correct; adding a second consumer
is what invalidated it.)

The rule this module implements: the video is deleted by whichever ENABLED, DEPLOYED
platform resolves LAST. "Resolves" means reaches a terminal state — published OR
terminally failed — both of which clear the pending record, which is exactly what
has_outstanding_job() reports on.

"Enabled AND deployed" is two conditions, and the second one was learned the hard
way. Enabled is per-client, read from the same env var that gates each platform
elsewhere, so a client with no Instagram configured never waits on an Instagram job
that will never be enqueued. But an env var being set says only that a platform is
CONFIGURED — it does not say its cron worker is installed and running. A platform
that is switched on in .env while its crontab entry is absent would leave a job
outstanding forever, and this module would dutifully retain the shared video forever
waiting on it. So a platform whose worker has stopped heartbeating (see
tools/worker_health.py) is no longer waited on: a worker that is not running will
never resolve that job, and the file has to be released. That release is loud — the
platform's own script alerts when it eventually finds the file gone — which is
strictly better than a silent unbounded leak.

Ordering requirement for callers: mark_published()/mark_failed() MUST be called
BEFORE consulting this module. That ordering is what makes the check free of the
CONCURRENCY race. If both scripts resolve at nearly the same moment, each one's
resolution is already durable in its own state file before it reads the other's, so
at least one of them must observe the other as terminal and delete. Both observing
each other as terminal is fine too — deleting an already-absent file is a no-op.
Reading before resolving, by contrast, would let both see the other as outstanding
and leak the file.

What that ordering does NOT give you is CRASH safety, and this module used to claim
otherwise. The surviving window is real: B records its terminal state, sees A still
outstanding, and returns; A then records its terminal state and is killed before it
consults this module. Both records are now clear, so no later job will ever invoke
cleanup for that key, and the file is leaked. No ordering of two independent
processes closes that window, because the second process's death is not observable
by the first. sweep_orphaned_videos() is the answer instead: an unconditional sweep,
run by both workers on every tick, that deletes any video in VIDEO_TMP_DIR old
enough that no pending approval and no outstanding job on any enabled platform can
still be referring to it. Recovery rather than prevention — which is the honest
shape for this problem.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import facebook_state, instagram_state, paths, state, worker_health

logger = logging.getLogger(__name__)

FACEBOOK = "facebook"
INSTAGRAM = "instagram"

# Each platform's state module and the per-client env var that enables it. The enable
# vars are the same ones check_approval.py gates its enqueues on, so "enabled" means the
# same thing here as it does there — a platform that is off enqueues no job, and must
# therefore never be waited on.
_PLATFORMS = {
    FACEBOOK: (facebook_state, "FB_PAGE_ID"),
    INSTAGRAM: (instagram_state, "IG_BUSINESS_ACCOUNT_ID"),
}

# How old a video must be before the orphan sweep will consider deleting it. This is
# NOT a retry or exposure window — every normal path deletes the file the moment the
# last platform resolves, so anything the sweep sees has already escaped the normal
# path. The grace exists purely so the sweep can never race a file that is legitimately
# mid-flight: a video is created by process_photos.py, waits for a human to press
# Approve in Telegram, and only then acquires any upload job at all. A human who
# approves the next morning must find their video intact, so the grace has to comfortably
# exceed "overnight". Belt and braces on top of the explicit in-use checks in
# _paths_in_use(), which are what actually protect a live file.
_ORPHAN_GRACE_SECONDS = 48 * 60 * 60


def _enabled_platforms() -> dict:
    """Return the _PLATFORMS entries whose enable var is set for this client."""
    return {
        name: modules
        for name, modules in _PLATFORMS.items()
        if os.environ.get(modules[1])
    }


def other_platforms_pending(idempotency_key: str, *, platform: str) -> list[str]:
    """Return the OTHER platforms that are still going to need this key's video file.

    An empty list means the caller is the last one to finish and may clean up.

    A platform is excluded — i.e. NOT waited on — in three cases:
      - it is the caller itself;
      - it is not enabled for this client, so there is no job to wait for;
      - its cron worker has not heartbeated within worker_health.STALE_AFTER_SECONDS,
        so whatever it has outstanding is never going to be resolved. Waiting on a
        worker that is not running is how a configured-but-undeployed platform used
        to retain the shared video indefinitely.

    Call this only AFTER recording your own terminal state — see the module docstring
    for what that ordering does and does not buy.
    """
    if platform not in _PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")

    waiting = []
    for name, (state_module, _enable_var) in _enabled_platforms().items():
        if name == platform:
            continue
        if not state_module.has_outstanding_job(idempotency_key):
            continue
        if not worker_health.is_deployed(name):
            logger.warning(
                "not waiting on %s for key=%s — its cron worker has not run recently; "
                "releasing the shared video rather than retaining it indefinitely",
                name, idempotency_key,
            )
            continue
        waiting.append(name)
    return waiting


def is_last_to_finish(idempotency_key: str, *, platform: str) -> bool:
    """True if every other enabled, deployed platform has already resolved this key."""
    return not other_platforms_pending(idempotency_key, platform=platform)


def _paths_in_use() -> set:
    """Return the resolved video paths that some live record still refers to.

    Three sources, covering every stage a video can be at: awaiting a human's Telegram
    approval, and holding an outstanding upload job on either platform. A path in this
    set is off limits to the sweep no matter how old the file is.

    Propagates rather than swallows a state-file read error, so the caller can
    abandon the sweep entirely. A state file that cannot be read means the sweep does
    not know what is live, and the only safe interpretation of not knowing is to
    delete nothing.
    """
    in_use = set()
    readers = [state.get_pending_approval]
    readers += [module.get_pending_upload for module, _ in _PLATFORMS.values()]
    for read in readers:
        record = read()
        if not record:
            continue
        raw = record.get("video_local_path")
        if raw:
            in_use.add(str(Path(raw).resolve()))
    return in_use


def sweep_orphaned_videos(*, grace_seconds: int = _ORPHAN_GRACE_SECONDS) -> list:
    """Delete approved videos in VIDEO_TMP_DIR that nothing can still be waiting on.

    The recovery half of this module. other_platforms_pending() prevents the ordinary
    leak; this catches the residue it cannot — chiefly a worker killed between
    recording its terminal state and consulting the coordination check, after which no
    future job will ever run cleanup for that key (see the module docstring).

    A file is deleted only if ALL of the following hold:
      - it lives under the resolved VIDEO_TMP_DIR root (same containment guard the
        per-job deletes use — nothing outside it is ever unlinked);
      - it has not been modified for grace_seconds;
      - no pending approval and no outstanding upload job on any platform refers to it.

    Both cron workers call this on every tick, so it runs as long as EITHER platform is
    deployed — which is what keeps it independent of the Instagram cron specifically
    having been installed.

    Best-effort and non-fatal by construction: it never raises, because it runs ahead
    of real work on every tick and a sweep problem must not cost a client their post.
    Returns the paths actually deleted, for logging and tests.
    """
    try:
        root = paths.get_video_tmp_root()
    except KeyError as exc:
        logger.warning("cannot sweep orphaned videos — %s is not set", exc)
        return []
    if not root.is_dir():
        return []

    try:
        in_use = _paths_in_use()
    except (OSError, RuntimeError):
        logger.warning("skipping orphan sweep — could not determine which videos are in use")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
    deleted = []
    for candidate in sorted(root.rglob("*.mp4")):
        try:
            resolved = candidate.resolve()
            # rglob cannot escape root, but a symlink inside it can point anywhere.
            # Re-assert containment on the RESOLVED path before unlinking.
            resolved.relative_to(root)
            if not resolved.is_file():
                continue
            if str(resolved) in in_use:
                continue
            mtime = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
            if mtime > cutoff:
                continue
            resolved.unlink()
        except ValueError:
            logger.warning("orphan sweep: refusing to delete outside the tmp root")
            continue
        except OSError as exc:
            logger.warning("orphan sweep: could not delete %s: %s", candidate.name, exc)
            continue
        deleted.append(str(resolved))
        logger.warning(
            "orphan sweep deleted an abandoned approved video (age > %ds, no live "
            "record referenced it): %s",
            grace_seconds, resolved.name,
        )
    return deleted
