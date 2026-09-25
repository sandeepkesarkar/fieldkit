"""
Tests for tools/upload_cleanup.py — cross-platform coordination of the shared
approved-video file's deletion.

One approval produces one file on disk with two independent consumers
(upload_facebook.py, upload_instagram.py) running on separate cron schedules. This
module answers "am I the last one done, and may I delete it?" — these tests pin
that answer for every combination of enabled/disabled and resolved/outstanding.

Uses the REAL state modules against isolated tmp files: the whole point of the
module is reading real job state across two state files.
"""

import pytest

import tools.facebook_state as fb_state
import tools.instagram_state as ig_state
import tools.state as approval_state
import tools.upload_cleanup as cleanup
import tools.worker_health as wh

_KEY = "42"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    monkeypatch.setattr(approval_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(approval_state, "STATE_FILE", data_dir / "state.json")
    monkeypatch.setattr(wh, "DATA_DIR", data_dir)
    monkeypatch.setattr(wh, "HEALTH_FILE", data_dir / "worker_health.json")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    # Both workers deployed and ticking is the NORMAL state, and the baseline these tests
    # are written against. Without it every "is the other platform still working on this?"
    # question would answer "no, its worker is dead", and the coordination tests below
    # would pass for the wrong reason. The stale-worker behaviour gets its own tests.
    wh.record_heartbeat(cleanup.FACEBOOK)
    wh.record_heartbeat(cleanup.INSTAGRAM)


@pytest.fixture
def both_enabled(monkeypatch):
    monkeypatch.setenv("FB_PAGE_ID", "123456789")
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "17841400000000000")


def _enqueue_fb(key=_KEY):
    fb_state.set_pending_upload({
        "project_name": "proj", "video_local_path": "/tmp/v.mp4", "page_id": "123456789",
        "status": "pending", "attempt_count": 0, "last_attempt_at": None,
        "triggered_at": "2026-08-31T14:00:00Z", "idempotency_key": key, "fb_post_id": None,
    })


def _enqueue_ig(key=_KEY):
    ig_state.set_pending_upload({
        "project_name": "proj", "video_local_path": "/tmp/v.mp4",
        "ig_business_account_id": "17841400000000000", "status": "pending",
        "attempt_count": 0, "last_attempt_at": None,
        "triggered_at": "2026-08-31T14:00:00Z", "idempotency_key": key,
        "container_id": None, "ig_post_id": None,
    })


# --- both enabled, both outstanding ---

def test_facebook_waits_while_instagram_is_outstanding(both_enabled):
    """The core fix: Facebook may NOT delete while an Instagram job still needs the file."""
    _enqueue_fb()
    _enqueue_ig()
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == ["instagram"]
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is False


def test_instagram_waits_while_facebook_is_outstanding(both_enabled):
    """Symmetrically, Instagram may not delete while Facebook still has work."""
    _enqueue_fb()
    _enqueue_ig()
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.INSTAGRAM) == ["facebook"]
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.INSTAGRAM) is False


# --- both enabled, other side resolved ---

def test_facebook_may_delete_once_instagram_published(both_enabled):
    """Once Instagram publishes, Facebook is free to clean up."""
    _enqueue_ig()
    ig_state.mark_published(_KEY, "ig_1")
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


def test_facebook_may_delete_once_instagram_failed(both_enabled):
    """A terminally failed Instagram job releases the file too."""
    _enqueue_ig()
    ig_state.mark_failed(_KEY)
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


def test_instagram_may_delete_once_facebook_published(both_enabled):
    """Once Facebook publishes, Instagram is free to clean up."""
    _enqueue_fb()
    fb_state.mark_published(_KEY, "fb_1")
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.INSTAGRAM) is True


def test_neither_waits_when_nothing_was_ever_enqueued(both_enabled):
    """A platform that never got a job for this key is not something to wait on."""
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.INSTAGRAM) is True


# --- per-client enablement ---

def test_disabled_instagram_is_never_waited_on(monkeypatch):
    """FR-016: a client with no Instagram configured must not block on an Instagram job."""
    monkeypatch.setenv("FB_PAGE_ID", "123456789")
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    _enqueue_ig()  # a stale record that should be ignored outright
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


def test_empty_instagram_account_id_counts_as_disabled(monkeypatch):
    """An empty value (as shipped in .env.example) is disabled, not enabled-with-blank."""
    monkeypatch.setenv("FB_PAGE_ID", "123456789")
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "")
    _enqueue_ig()
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


def test_disabled_facebook_is_never_waited_on(monkeypatch):
    """Symmetric: Instagram doesn't wait on a Facebook job that can't exist."""
    monkeypatch.delenv("FB_PAGE_ID", raising=False)
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", "17841400000000000")
    _enqueue_fb()
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.INSTAGRAM) is True


# --- key scoping ---

def test_a_different_approvals_job_does_not_block(both_enabled):
    """Only the SAME approval's job matters — a newer, unrelated job must not block cleanup."""
    _enqueue_ig(key="999")
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


# --- misuse ---

def test_unknown_platform_raises(both_enabled):
    """A typo'd platform name fails loudly rather than silently permitting deletion."""
    with pytest.raises(ValueError, match="unknown platform"):
        cleanup.other_platforms_pending(_KEY, platform="tiktok")


def test_a_platform_never_waits_on_itself(both_enabled):
    """Your own still-pending record must not stop you from cleaning up after resolving."""
    _enqueue_fb()
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == []


# ---------------------------------------------------------------------------
# A platform whose worker is not running is not waited on
# ---------------------------------------------------------------------------
#
# Enabling a platform and installing its cron are two separate acts. Set
# IG_BUSINESS_ACCOUNT_ID without installing upload_instagram.py's crontab entry and
# an enqueued Instagram job can never resolve — which used to mean this module
# retained the shared video forever waiting on it. check_approval.py now refuses to
# queue such a job at all; this is the other half, for a worker removed AFTER a job
# was already queued.

def test_a_platform_whose_worker_stopped_is_not_waited_on(both_enabled, monkeypatch):
    """The unbounded-retention fix: a dead worker will never resolve its job."""
    import tools.worker_health as wh
    _enqueue_fb()
    _enqueue_ig()
    # Instagram enabled, job outstanding, but its cron has not run in a long time.
    monkeypatch.setattr(wh, "is_deployed", lambda name, **kw: name != cleanup.INSTAGRAM)
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == []
    assert cleanup.is_last_to_finish(_KEY, platform=cleanup.FACEBOOK) is True


def test_a_never_deployed_worker_is_not_waited_on(both_enabled, monkeypatch):
    """The configured-but-never-installed case, which is how this starts in practice."""
    import tools.worker_health as wh
    _enqueue_fb()
    _enqueue_ig()
    monkeypatch.setattr(wh, "HEALTH_FILE", wh.DATA_DIR / "does_not_exist.json")
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == []


def test_a_live_worker_is_still_waited_on(both_enabled):
    """The guard must not fire in normal operation — that would delete a needed file.

    Deleting the video out from under a genuinely running Instagram job is the exact
    failure this whole module exists to prevent, so the staleness escape hatch has to
    stay firmly shut while the other worker is ticking.
    """
    _enqueue_fb()
    _enqueue_ig()
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == ["instagram"]


def test_a_stale_worker_with_nothing_outstanding_changes_nothing(both_enabled, monkeypatch):
    """No job means nothing to wait for, deployed or not — no spurious warning path."""
    import tools.worker_health as wh
    _enqueue_fb()
    monkeypatch.setattr(wh, "is_deployed", lambda name, **kw: False)
    assert cleanup.other_platforms_pending(_KEY, platform=cleanup.FACEBOOK) == []


# ---------------------------------------------------------------------------
# sweep_orphaned_videos — recovery for the crash window ordering cannot close
# ---------------------------------------------------------------------------
#
# The window: B records its terminal state, sees A outstanding, returns; A records
# its terminal state and is killed before consulting this module. Both records are
# now clear, so no future job will ever run cleanup for that key. No ordering of two
# independent processes closes that — the second process's death is not observable by
# the first — so the answer is recovery, not prevention.

@pytest.fixture
def tmp_videos(tmp_path, monkeypatch):
    """Point VIDEO_TMP_DIR at an isolated directory and return it."""
    root = tmp_path / "videos"
    (root / "proj").mkdir(parents=True)
    monkeypatch.setenv("VIDEO_TMP_DIR", str(root))
    return root.resolve()


def _aged_video(root, name="proj/old.mp4", age_hours=72):
    """Create a video file with an mtime age_hours in the past."""
    import os
    import time
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 16)
    old = time.time() - age_hours * 3600
    os.utime(p, (old, old))
    return p


def test_sweep_deletes_an_abandoned_video(tmp_videos, both_enabled):
    """The crash-window leak, recovered: old file, no live record referring to it."""
    victim = _aged_video(tmp_videos)
    deleted = cleanup.sweep_orphaned_videos()
    assert deleted == [str(victim)]
    assert victim.exists() is False


def test_sweep_spares_a_video_an_outstanding_job_still_needs(tmp_videos, both_enabled):
    """An old file is not an abandoned file if a job still points at it."""
    victim = _aged_video(tmp_videos)
    ig_state.set_pending_upload({
        "project_name": "proj", "video_local_path": str(victim),
        "ig_business_account_id": "17841400000000000", "status": "pending",
        "attempt_count": 0, "last_attempt_at": None,
        "triggered_at": "2026-08-31T14:00:00Z", "idempotency_key": _KEY,
        "container_id": None, "ig_post_id": None,
    })
    assert cleanup.sweep_orphaned_videos() == []
    assert victim.exists() is True


def test_sweep_spares_a_video_awaiting_human_approval(tmp_videos, both_enabled):
    """The most dangerous mistake this could make, and the reason for _paths_in_use().

    A video sits in VIDEO_TMP_DIR from the moment process_photos.py makes it until a
    human presses Approve in Telegram — with NO upload job of any kind attached. Sweeping
    one of those would silently destroy work the owner is still deciding about.
    """
    import tools.state as approval_state
    victim = _aged_video(tmp_videos)
    approval_state.set_pending_approval({
        "project_name": "proj", "drive_folder_id": "f1", "drive_video_file_id": "v1",
        "drive_folder_link": "https://drive.google.com/drive/folders/f1",
        "video_local_path": str(victim), "telegram_message_id": 42,
        "triggered_at": "2026-08-31T14:00:00Z",
    })
    assert cleanup.sweep_orphaned_videos() == []
    assert victim.exists() is True


def test_sweep_spares_a_recent_video(tmp_videos, both_enabled):
    """The grace period has to comfortably outlast an overnight approval."""
    fresh = _aged_video(tmp_videos, name="proj/fresh.mp4", age_hours=1)
    assert cleanup.sweep_orphaned_videos() == []
    assert fresh.exists() is True


def test_the_grace_period_outlasts_an_overnight_approval():
    """Pins the intent rather than the number: approve-tomorrow-morning must be safe."""
    assert cleanup._ORPHAN_GRACE_SECONDS >= 24 * 60 * 60


def test_sweep_deletes_nothing_outside_the_tmp_root(tmp_path, tmp_videos, both_enabled):
    """Same containment guard the per-job deletes use — nothing outside is ever unlinked."""
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"\x00" * 16)
    import os, time
    old = time.time() - 72 * 3600
    os.utime(outside, (old, old))
    cleanup.sweep_orphaned_videos()
    assert outside.exists() is True


def test_sweep_refuses_to_follow_a_symlink_out_of_the_tmp_root(tmp_path, tmp_videos, both_enabled):
    """rglob cannot escape the root, but a symlink inside it can point anywhere."""
    import os, time
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"\x00" * 16)
    link = tmp_videos / "proj" / "sneaky.mp4"
    link.symlink_to(outside)
    old = time.time() - 72 * 3600
    os.utime(outside, (old, old))
    assert cleanup.sweep_orphaned_videos() == []
    assert outside.exists() is True


def test_sweep_finds_videos_in_project_subdirectories(tmp_videos, both_enabled):
    """process_photos.py writes to VIDEO_TMP_DIR/<project>/<name>.mp4, not the root."""
    victim = _aged_video(tmp_videos, name="kitchen_remodel/kitchen_remodel_20260831.mp4")
    assert cleanup.sweep_orphaned_videos() == [str(victim)]


def test_sweep_ignores_non_video_files(tmp_videos, both_enabled):
    """Only the approved videos this module is responsible for are in scope."""
    import os, time
    other = tmp_videos / "proj" / "notes.txt"
    other.write_text("keep me")
    old = time.time() - 72 * 3600
    os.utime(other, (old, old))
    assert cleanup.sweep_orphaned_videos() == []
    assert other.exists() is True


def test_sweep_is_a_noop_when_the_tmp_root_does_not_exist(monkeypatch, tmp_path, both_enabled):
    """Runs on every cron tick, including on a client that has never made a video."""
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path / "never_created"))
    assert cleanup.sweep_orphaned_videos() == []


def test_sweep_deletes_nothing_when_state_cannot_be_read(tmp_videos, both_enabled, monkeypatch):
    """Not knowing what is live is never grounds for deleting.

    A corrupt state file means the in-use set is unknown, and the only safe reading of
    "unknown" is to leave every file alone.
    """
    victim = _aged_video(tmp_videos)
    monkeypatch.setattr(
        ig_state, "get_pending_upload",
        lambda: (_ for _ in ()).throw(RuntimeError("instagram_state.json is corrupt")),
    )
    assert cleanup.sweep_orphaned_videos() == []
    assert victim.exists() is True


def test_sweep_never_raises_when_a_delete_fails(tmp_videos, both_enabled, monkeypatch):
    """It runs ahead of real work on every tick; a sweep problem must not cost a post."""
    _aged_video(tmp_videos)
    monkeypatch.setattr(
        cleanup.Path, "unlink",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
    )
    assert cleanup.sweep_orphaned_videos() == []   # no raise, nothing reported deleted


def test_sweep_grace_is_caller_overridable(tmp_videos, both_enabled):
    """Tests and any future operator tooling can tighten it; the default stays generous."""
    victim = _aged_video(tmp_videos, name="proj/recent.mp4", age_hours=2)
    assert cleanup.sweep_orphaned_videos(grace_seconds=3600) == [str(victim)]
