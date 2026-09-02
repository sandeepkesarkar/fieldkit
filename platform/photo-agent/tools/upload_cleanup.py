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

The rule this module implements: the video is deleted by whichever ENABLED platform
resolves LAST. "Enabled" is per-client, read from the same env var that gates each
platform elsewhere, so a client with no Instagram configured never waits on an
Instagram job that will never run. "Resolves" means reaches a terminal state —
published OR terminally failed — both of which clear the pending record, which is
exactly what has_outstanding_job() reports on.

Ordering requirement for callers: mark_published()/mark_failed() MUST be called
BEFORE consulting this module. That ordering is what makes the check race-free. If
both scripts resolve at nearly the same moment, each one's resolution is already
durable in its own state file before it reads the other's, so at least one of them
must observe the other as terminal and delete. Both observing each other as terminal
is fine too — deleting an already-absent file is a no-op. Reading before resolving,
by contrast, would let both see the other as outstanding and leak the file forever.
"""

import logging
import os

from tools import facebook_state, instagram_state

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


def other_platforms_pending(idempotency_key: str, *, platform: str) -> list[str]:
    """Return the OTHER enabled platforms still holding an unresolved job for this key.

    An empty list means the caller is the last one to finish and may clean up. A platform
    that is not enabled for this client is never included: there is no job to wait for.

    Call this only AFTER recording your own terminal state — see the module docstring.
    """
    if platform not in _PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")

    waiting = []
    for name, (state_module, enable_var) in _PLATFORMS.items():
        if name == platform:
            continue
        if not os.environ.get(enable_var):
            continue
        if state_module.has_outstanding_job(idempotency_key):
            waiting.append(name)
    return waiting


def is_last_to_finish(idempotency_key: str, *, platform: str) -> bool:
    """True if every other enabled platform has already resolved this key."""
    return not other_platforms_pending(idempotency_key, platform=platform)
