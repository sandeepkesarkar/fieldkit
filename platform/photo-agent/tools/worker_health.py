"""
worker_health.py — Liveness heartbeats for the photo-agent cron workers.

The problem this exists to solve: enabling a platform and deploying its worker
are two SEPARATE acts, and nothing in the repo made them atomic. Setting
IG_BUSINESS_ACCOUNT_ID in a client .env is a one-line edit; installing
upload_instagram.py's crontab entry is a manual step on the machine. Do the
first without the second and check_approval.py would enqueue an Instagram job
that nothing ever drains — which in turn made upload_facebook.py retain the
shared local video indefinitely (see tools/upload_cleanup.py), and would have
let a temporary public Drive link outlive the run that created it, with no code
path left that would ever revoke it.

The fix is not to try to force the two acts to be atomic — a repo cannot install
a crontab entry, and an entry can be removed again afterwards. It is to make the
absence DETECTABLE at runtime, from either direction:

  - Every cron worker stamps a heartbeat on every tick, before doing anything
    else. A heartbeat is therefore proof that the worker is actually deployed and
    running, not merely configured.
  - check_approval.py refuses to enqueue a job for a platform whose worker has no
    fresh heartbeat, and alerts the admin instead. Nothing is queued, no video is
    retained, no public link is ever created.
  - tools/upload_cleanup.py stops waiting on a platform whose worker has gone
    stale, so a worker that is removed AFTER a job was queued cannot strand the
    shared video forever either.

This is self-healing in both directions and needs no deployment action of its
own: install the cron and the next tick (within a minute) makes the platform
healthy again; remove it and the system notices within the stale window.

Heartbeats live in $FIELDKIT_DATA_DIR/photo-agent/worker_health.json, per client,
alongside the state files. No credential, no PII. A lost or deleted heartbeat
file degrades to "not deployed", which is the SAFE direction — refuse to enqueue,
don't wait on it — never a silent duplicate or a leak.

What a heartbeat IS and IS NOT load-bearing for, because the distinction matters
and an earlier version of this docstring flattened it:

  - NOT load-bearing for duplicate publication (FR-011). Nothing here decides
    whether a Reel may be published twice; that is entirely the quarantine and
    idempotency machinery in tools/instagram_state.py, which does not consult
    heartbeats at all. A wrong heartbeat cannot produce a duplicate.
  - LOAD-BEARING for the public-link exposure. The IG_NOWORKER refusal in
    check_approval.py is gated on is_deployed(), and that refusal is what keeps a
    job from being queued for a worker that will never drain it.

On that second point, one thing is worth stating precisely rather than leaving to
inference. STALE_AFTER_SECONDS is an hour, so for up to an hour after the cron
dies a heartbeat still reads fresh and approvals still enqueue. That window does
NOT create an unrevoked public Drive link: the only production caller of
drive.create_temporary_share_link() is upload_instagram._process_upload, which is
reached only from that script's main() — the very worker that is dead. A link's
only creator is the thing whose absence is in question, so "heartbeat wrongly
fresh" and "a link was created" cannot both hold. What the window actually
produces is an inert queued job, and a shared video retained by
tools/upload_cleanup.py until the heartbeat does go stale.

The real single point of failure is elsewhere and is not a heartbeat problem: if
the worker dies mid-attempt, after creating a link and before revoking it, the
link is public and the obligation is recorded — but the drain that would revoke
it, and the daily alert that would report it, both live in that same stopped
worker. See docs/instagram/README.md.
"""

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_data_dir_raw = os.environ.get("FIELDKIT_DATA_DIR", "")
if not _data_dir_raw:
    raise RuntimeError("FIELDKIT_DATA_DIR is not set — add it to your client .env file")
DATA_DIR = Path(_data_dir_raw) / "photo-agent"
HEALTH_FILE = DATA_DIR / "worker_health.json"

__all__ = [
    "record_heartbeat",
    "seconds_since_heartbeat",
    "is_deployed",
    "STALE_AFTER_SECONDS",
]

# How long a worker may go without a heartbeat before it is presumed not to be
# running. Both cron workers tick every minute, so this is SIXTY consecutive
# missed ticks — deliberately far beyond any plausible scheduling jitter,
# machine sleep, or a single long-running attempt (an Instagram attempt can
# legitimately occupy ~5 minutes of container polling). Being wrong in the
# "presumed dead but actually alive" direction is the expensive mistake: it
# would refuse a legitimate enqueue and could release a video another worker
# still wants. An hour buys certainty at the cost of an hour's delay in
# noticing, which is the right trade for something whose failure mode is a
# manual deployment step nobody performed.
STALE_AFTER_SECONDS = 60 * 60


def _read(file_obj) -> dict:
    """Read and parse worker_health.json from an open, locked file object.

    A corrupt or unreadable heartbeat file reads as "no heartbeats". Unlike the
    state files — where corruption is escalated because it may mean a real job
    was lost — this one holds nothing that cannot be rebuilt by the next tick,
    and refusing to run over it would turn a cosmetic problem into an outage.
    """
    file_obj.seek(0)
    content = file_obj.read()
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("worker_health.json is corrupt — treating as empty: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write(file_obj, data: dict) -> None:
    """Overwrite worker_health.json via an open, locked file object."""
    file_obj.seek(0)
    file_obj.write(json.dumps(data, indent=2))
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def record_heartbeat(worker: str) -> None:
    """Stamp worker as alive as of now.

    Called at the very top of each cron worker's tick, BEFORE any per-client
    enable gate — the heartbeat attests that the cron ENTRY exists and fired,
    which is a separate fact from whether the platform is currently switched on
    for this client. A worker that exits early because its platform is disabled
    has still proved it is deployed.

    Best-effort: never raises. A heartbeat that cannot be written must not take
    down an otherwise healthy upload; it degrades to "not deployed", which only
    ever makes the callers more conservative.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd_no = os.open(HEALTH_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        with os.fdopen(fd_no, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = _read(f)
                data[worker] = {"last_seen_at": now}
                _write(f, data)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("could not record %s worker heartbeat: %s", worker, exc)
        return
    logger.debug("worker heartbeat: worker=%s at=%s", worker, now)


def seconds_since_heartbeat(worker: str) -> float | None:
    """Return the age in seconds of worker's last heartbeat, or None if it has none.

    None means "never seen" — the worker has not ticked since this client's data
    directory was created. That is a different fact from "seen, but long ago",
    and callers word their alerts differently for the two, so it is not collapsed
    into a large number here.
    """
    try:
        with open(HEALTH_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = _read(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("could not read worker heartbeats: %s", exc)
        return None

    entry = data.get(worker)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("last_seen_at")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning("unparseable %s heartbeat timestamp=%r", worker, raw)
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds()


def is_deployed(worker: str, *, stale_after_seconds: int = STALE_AFTER_SECONDS) -> bool:
    """Return True if worker has heartbeated recently enough to be presumed running.

    Named "deployed" rather than "healthy" on purpose: a True here says the cron
    entry exists and fires, nothing at all about whether its last attempt
    succeeded. Upload success and failure are tracked by the state files.
    """
    age = seconds_since_heartbeat(worker)
    return age is not None and age <= stale_after_seconds
