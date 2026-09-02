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
import tools.upload_cleanup as cleanup

_KEY = "42"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "photo-agent"
    data_dir.mkdir()
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")


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
