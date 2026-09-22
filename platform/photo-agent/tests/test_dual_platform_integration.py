"""
Integration tests for FR-013 / SC-007 — Facebook and Instagram publishing are
independent outcomes of one approval.

Unlike the unit suites, these use the REAL tools.facebook_state and
tools.instagram_state read-modify-write state machines, redirected to isolated
tmp files, and drive the actual approve -> enqueue -> cron-upload sequence. Only
the outward-facing edges (Graph APIs, Drive, Telegram, email, activity logs) are
mocked. A fully-mocked state layer cannot demonstrate the property under test:
that the two platforms genuinely share no state, lock, or claim namespace.

Covers:
  - one approval enqueues BOTH platforms, under one shared idempotency key
  - an Instagram-only total failure leaves the Facebook job publishing normally,
    with its own confirmation, and vice versa
  - neither cron script reads or writes the other's state file or lock file
"""

import json
from pathlib import Path

import pytest

import tools.facebook_state as fb_state
import tools.instagram_state as ig_state
import tools.worker_health as wh
from scripts.check_approval import main as approve_main
from scripts.upload_facebook import main as fb_main
from scripts.upload_instagram import main as ig_main
from tools.instagram_api import InstagramUploadError

_PROJECT = "dual_platform_project"
_MESSAGE_ID = 4242
_IDEM_KEY = str(_MESSAGE_ID)
_FB_PAGE_ID = "123456789"
_IG_ACCOUNT_ID = "17841400000000000"
_PAGE_TOKEN = "page_token_abc"
_CHAT_ID = "telegram_chat_id"
_FB_POST_ID = "fb_post_1"
_IG_POST_ID = "ig_post_1"
_CONTAINER_ID = "container_1"
_PERMALINK = "https://www.instagram.com/reel/AbCdEfGhIjK/"
_SHARE_LINK = "https://drive.google.com/uc?export=download&id=drive_file_1"


@pytest.fixture(autouse=True)
def isolated_activity_log(tmp_path, monkeypatch):
    """Keep the activity log out of the developer's real client log directory.

    instagram_logger resolves LOG_DIR from FIELDKIT_LOG_DIR at IMPORT time, and the
    scripts under test load the real client .env at import — so any logging call that
    is not individually mocked appends to the actual checkout's photo-agent.log. Relying
    on the mock list staying exhaustive is what let that happen; patching the path makes
    it structural, and keeps newly-added log events from silently reintroducing it.
    """
    import tools.instagram_logger as ig_logger
    log_dir = tmp_path / "activity_log"
    monkeypatch.setattr(ig_logger, "LOG_DIR", log_dir)
    monkeypatch.setattr(ig_logger, "LOG_FILE", log_dir / "photo-agent.log")
    return ig_logger


@pytest.fixture
def real_state(tmp_path, monkeypatch):
    """Point BOTH real state modules at isolated tmp files in one data dir.

    Deliberately the same directory, as in production — the isolation that matters
    is that they are different FILES, not different parents.
    """
    data_dir = tmp_path / "data" / "photo-agent"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(fb_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(fb_state, "STATE_FILE", data_dir / "facebook_state.json")
    monkeypatch.setattr(ig_state, "DATA_DIR", data_dir)
    monkeypatch.setattr(ig_state, "STATE_FILE", data_dir / "instagram_state.json")
    monkeypatch.setattr(wh, "DATA_DIR", data_dir)
    monkeypatch.setattr(wh, "HEALTH_FILE", data_dir / "worker_health.json")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "data"))
    # Both cron workers installed and ticking — the normal deployed state these
    # end-to-end flows assume. check_approval.py refuses to enqueue an Instagram job
    # without a fresh Instagram heartbeat, and upload_cleanup.py stops waiting on a
    # platform whose worker has gone quiet; both behaviours have their own tests.
    wh.record_heartbeat("facebook")
    wh.record_heartbeat("instagram")
    monkeypatch.setenv("FIELDKIT_LOG_DIR", str(tmp_path / "logs"))
    # Pin VIDEO_TMP_DIR explicitly. Left unset it DEFAULTS to a path under
    # FIELDKIT_DATA_DIR — but only if the ambient environment has not already set it to
    # something absolute, in which case the `video` fixture's file would sit outside the
    # allowed root and _delete_local_file would refuse it. These tests would then pass or
    # fail according to the developer's shell rather than the coordination behaviour they
    # exist to check.
    monkeypatch.setenv("VIDEO_TMP_DIR", str(tmp_path / "data" / "photo-agent" / "tmp"))
    return data_dir


@pytest.fixture
def video(real_state, tmp_path):
    """A real video file under the resolved VIDEO_TMP_DIR root.

    Placed there deliberately: _delete_local_file refuses to unlink anything outside
    that root, so a video written anywhere else would make these tests pass for the
    wrong reason — the file would survive because deletion was refused, not because
    the cross-platform coordination held it back.
    """
    # Resolved through paths.get_video_tmp_root(), the SAME function _delete_local_file
    # uses, rather than by rebuilding the default layout by hand. Hard-coding it meant the
    # fixture and the code under test could disagree about where the root is — which is
    # exactly what happened once VIDEO_TMP_DIR was set in the environment.
    import tools.paths as paths
    tmp_root = paths.get_video_tmp_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    p = tmp_root / "video.mp4"
    p.write_bytes(b"\x00" * 64)
    return p


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_EMAIL", "agent@example.com")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_bot_token")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", _CHAT_ID)
    monkeypatch.setenv("FB_PAGE_ID", _FB_PAGE_ID)
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", _PAGE_TOKEN)
    monkeypatch.setenv("IG_BUSINESS_ACCOUNT_ID", _IG_ACCOUNT_ID)


@pytest.fixture
def approved(mocker, env, real_state, video):
    """Run the real approve path so both platforms are enqueued through real state."""
    import scripts.check_approval as ca
    mocker.patch.object(ca, "_try_acquire_check_lock", return_value=mocker.MagicMock())
    mocker.patch.object(ca.fcntl, "flock")
    mocker.patch.object(ca.state, "get_pending_approval", return_value={
        "project_name": _PROJECT,
        "drive_video_file_id": "drive_video_1",
        "drive_folder_link": "https://drive.google.com/drive/folders/folder_1",
        "video_local_path": str(video),
        "telegram_message_id": _MESSAGE_ID,
        "triggered_at": "2026-08-31T14:00:00Z",
    })
    mocker.patch.object(ca.state, "clear_pending_approval")
    mocker.patch.object(ca, "_send_approval_email")
    mocker.patch.object(ca, "_notify_admin")
    mocker.patch.object(ca.activity_log, "log_approved")
    mocker.patch.object(ca.activity_log, "log_error")
    mocker.patch.object(ca.instagram_logger, "log_upload_enqueued")
    approve_main(["--callback-data", "approve"])
    return mocker


@pytest.fixture
def cron(approved, mocker, video):
    """Mock both cron scripts' external edges; leave their state machines real."""
    import scripts.upload_facebook as uf
    import scripts.upload_instagram as ui

    for mod in (uf, ui):
        mocker.patch.object(mod, "_try_acquire_upload_lock", return_value=mocker.MagicMock())
        mocker.patch.object(mod.fcntl, "flock")
        # Replace each script's module-level `telegram_api` NAME with its own mock,
        # rather than patching send_message on the single shared tools.telegram_api
        # module both scripts import. Otherwise the two scripts' notifications land
        # in one call list and these tests cannot tell which platform sent what —
        # which is precisely the thing under test.
        mocker.patch.object(mod, "telegram_api")

    mocker.patch.object(uf.facebook_api, "upload_video", return_value=_FB_POST_ID)
    # _delete_local_file is deliberately NOT mocked in either script: these tests exist to
    # verify the REAL cross-platform deletion behaviour, and mocking it out is exactly what
    # let the deletion race hide here in the first place.
    for name in ("log_upload_started", "log_upload_published",
                 "log_upload_attempt_failed", "log_upload_exhausted", "log_token_expired"):
        mocker.patch.object(uf.facebook_logger, name)

    # A faithful fake, not a bare return_value: the real
    # drive.create_temporary_share_link() hands the caller the new file's id through
    # on_file_id BEFORE it grants the public permission, and upload_instagram.py depends
    # on that to register its cleanup obligation. A mock that skipped the callback would
    # make every revoke assertion below pass vacuously against code that never revokes.
    def _fake_share_link(video_path, on_file_id=None):
        if on_file_id is not None:
            on_file_id("drive_file_1")
        return _SHARE_LINK

    mocker.patch.object(
        ui.drive, "create_temporary_share_link", side_effect=_fake_share_link
    )
    mocker.patch.object(ui.drive, "revoke_share_link")
    mocker.patch.object(ui.instagram_api, "create_media_container", return_value=_CONTAINER_ID)
    mocker.patch.object(ui.instagram_api, "get_container_status", return_value="FINISHED")
    mocker.patch.object(ui.instagram_api, "publish_container", return_value=_IG_POST_ID)
    mocker.patch.object(ui.instagram_api, "get_media_permalink", return_value=_PERMALINK)
    mocker.patch.object(ui.instagram_api.time, "sleep")
    for name in ("log_upload_started", "log_container_created", "log_container_ready",
                 "log_upload_published", "log_upload_attempt_failed",
                 "log_upload_exhausted", "log_token_expired"):
        mocker.patch.object(ui.instagram_logger, name)
    return mocker


def _run_instagram_until_resolved(ticks=3):
    """Drive up to `ticks` cron invocations, bypassing the 60s cooldown between them.

    Real cron ticks are a minute apart, which is what makes the cooldown a no-op in
    production. Rewinding last_attempt_at is how a test covers three attempts without
    sleeping three minutes.
    """
    for _ in range(ticks):
        ig_main([])
        record = ig_state.get_pending_upload()
        if record is None:
            return
        record["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
        ig_state.set_pending_upload(record)


# ---------------------------------------------------------------------------
# One approval enqueues both platforms
# ---------------------------------------------------------------------------

def test_one_approval_enqueues_both_platforms(approved, video):
    """FR-002/SC-007: a single approval produces a pending job on each platform."""
    fb_record = fb_state.get_pending_upload()
    ig_record = ig_state.get_pending_upload()
    assert fb_record is not None
    assert ig_record is not None
    assert fb_record["project_name"] == ig_record["project_name"] == _PROJECT
    assert fb_record["video_local_path"] == ig_record["video_local_path"] == str(video)


def test_both_pending_records_share_one_idempotency_key(approved):
    """The two jobs are correlated only by the approval's key — never by shared state."""
    assert fb_state.get_pending_upload()["idempotency_key"] == _IDEM_KEY
    assert ig_state.get_pending_upload()["idempotency_key"] == _IDEM_KEY


def test_state_lives_in_two_separate_files(approved, real_state):
    """FR-013 structurally: neither platform's record appears in the other's file."""
    fb_raw = (real_state / "facebook_state.json").read_text()
    ig_raw = (real_state / "instagram_state.json").read_text()
    assert "pending_facebook_upload" in fb_raw
    assert "pending_instagram_upload" in ig_raw
    assert "pending_instagram_upload" not in fb_raw
    assert "pending_facebook_upload" not in ig_raw
    assert "ig_business_account_id" not in fb_raw
    assert "page_id" not in ig_raw


# ---------------------------------------------------------------------------
# Instagram fails totally; Facebook succeeds
# ---------------------------------------------------------------------------

def test_instagram_exhaustion_does_not_affect_facebook_publish(cron):
    """FR-013: three Instagram failures leave the Facebook job publishing normally."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")

    _run_instagram_until_resolved()
    fb_main([])

    assert ig_state.get_pending_upload() is None
    assert ig_state.is_published(_IDEM_KEY) is False
    assert fb_state.is_published(_IDEM_KEY) is True
    assert fb_state.find_published(_PROJECT)["fb_post_id"] == _FB_POST_ID


def test_instagram_exhaustion_alerts_while_facebook_confirms(cron):
    """SC-007: each platform reports its own outcome to the owner, independently."""
    import scripts.upload_facebook as uf
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")

    _run_instagram_until_resolved()
    fb_main([])

    ig_texts = [c.args[1] for c in ui.telegram_api.send_message.call_args_list]
    fb_texts = [c.args[1] for c in uf.telegram_api.send_message.call_args_list]
    assert any("Instagram upload failed" in t for t in ig_texts)
    assert any("Video live on Facebook" in t for t in fb_texts)
    assert not any("Instagram" in t for t in fb_texts)


def test_instagram_failure_never_touches_facebook_state_file(cron, real_state):
    """The exhausted Instagram job leaves facebook_state.json's pending record intact."""
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")

    _run_instagram_until_resolved()

    fb_record = fb_state.get_pending_upload()
    assert fb_record is not None
    assert fb_record["idempotency_key"] == _IDEM_KEY
    assert fb_record["status"] == "pending"
    assert fb_record["attempt_count"] == 0


# ---------------------------------------------------------------------------
# Facebook fails totally; Instagram succeeds
# ---------------------------------------------------------------------------

def test_facebook_exhaustion_does_not_affect_instagram_publish(cron):
    """FR-013 in the other direction: a Facebook failure never blocks the Reel."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("API 500")

    for _ in range(3):
        fb_main([])
        record = fb_state.get_pending_upload()
        if record is None:
            break
        record["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
        fb_state.set_pending_upload(record)

    ig_main([])

    assert fb_state.is_published(_IDEM_KEY) is False
    assert ig_state.is_published(_IDEM_KEY) is True
    assert ig_state.find_published(_PROJECT)["ig_post_id"] == _IG_POST_ID


# ---------------------------------------------------------------------------
# Both succeed
# ---------------------------------------------------------------------------

def test_both_platforms_publish_from_one_approval(cron):
    """SC-007 happy path: one approval, two posts, two confirmations."""
    import scripts.upload_facebook as uf
    import scripts.upload_instagram as ui

    fb_main([])
    ig_main([])

    assert fb_state.is_published(_IDEM_KEY) is True
    assert ig_state.is_published(_IDEM_KEY) is True
    assert f"https://www.facebook.com/{_FB_POST_ID}" in uf.telegram_api.send_message.call_args.args[1]
    assert _PERMALINK in ui.telegram_api.send_message.call_args.args[1]


def test_published_instagram_job_is_not_republished(cron):
    """FR-011/SC-006: a second cron tick after publishing does not post again."""
    import scripts.upload_instagram as ui
    ig_main([])
    ui.instagram_api.publish_container.reset_mock()
    ig_main([])
    ui.instagram_api.publish_container.assert_not_called()


def test_reapproval_after_publish_does_not_reenqueue_instagram(cron, approved):
    """FR-011: re-approving the same video enqueues no second Instagram job."""
    ig_main([])
    assert ig_state.is_published(_IDEM_KEY) is True
    approve_main(["--callback-data", "approve"])
    assert ig_state.get_pending_upload() is None


# ---------------------------------------------------------------------------
# Lock file isolation
# ---------------------------------------------------------------------------

def test_the_two_cron_scripts_use_different_lock_files(env, real_state, monkeypatch):
    """FR-013: neither upload script can ever serialize against the other."""
    import scripts.upload_facebook as uf
    import scripts.upload_instagram as ui
    fb_lock = uf._try_acquire_upload_lock()
    try:
        # The Instagram lock must still be acquirable while Facebook's is held.
        ig_lock = ui._try_acquire_upload_lock()
        assert ig_lock is not None
        try:
            assert Path(fb_lock.name).name == "upload_facebook.lock"
            assert Path(ig_lock.name).name == "upload_instagram.lock"
        finally:
            ig_lock.close()
    finally:
        fb_lock.close()


# ---------------------------------------------------------------------------
# Shared video file lifetime — the cross-platform deletion race
# ---------------------------------------------------------------------------
#
# One approval, one file on disk, two independently-scheduled consumers. Whichever
# cron happens to run first must NOT delete the file, or the other platform finds it
# missing and terminally discards its job with nothing published and nobody alerted.
#
# These tests run the REAL _delete_local_file in both scripts (nothing mocked out) in
# BOTH orderings, so neither the bug nor a one-sided fix can pass.

def test_facebook_first_leaves_the_file_for_instagram(cron, video):
    """Facebook publishing first must leave the video for Instagram's pending job."""
    fb_main([])
    assert fb_state.is_published(_IDEM_KEY) is True
    assert video.exists(), "Facebook deleted the video while Instagram still needed it"


def test_facebook_first_then_instagram_publishes_successfully(cron, video):
    """The exact reported failure: Instagram running after Facebook must still publish."""
    import scripts.upload_instagram as ui
    fb_main([])
    ig_main([])
    assert ig_state.is_published(_IDEM_KEY) is True
    ui.instagram_api.create_media_container.assert_called_once()
    assert _PERMALINK in ui.telegram_api.send_message.call_args.args[1]


def test_facebook_first_then_instagram_deletes_the_file(cron, video):
    """Instagram, finishing last, is the one that cleans up."""
    fb_main([])
    assert video.exists()
    ig_main([])
    assert not video.exists()


def test_instagram_first_leaves_the_file_for_facebook(cron, video):
    """Symmetric ordering: Instagram first must leave the video for Facebook."""
    ig_main([])
    assert ig_state.is_published(_IDEM_KEY) is True
    assert video.exists(), "Instagram deleted the video while Facebook still needed it"


def test_instagram_first_then_facebook_publishes_successfully(cron, video):
    """Facebook running after Instagram must still find its video and publish."""
    import scripts.upload_facebook as uf
    ig_main([])
    fb_main([])
    assert fb_state.is_published(_IDEM_KEY) is True
    uf.facebook_api.upload_video.assert_called_once()


def test_instagram_first_then_facebook_deletes_the_file(cron, video):
    """Facebook, finishing last, is the one that cleans up."""
    ig_main([])
    assert video.exists()
    fb_main([])
    assert not video.exists()


def test_file_is_deleted_exactly_once_and_no_run_errors(cron, video):
    """A third tick after both are done is a harmless no-op, not a crash."""
    fb_main([])
    ig_main([])
    assert not video.exists()
    fb_main([])
    ig_main([])
    assert not video.exists()


def test_instagram_failure_still_releases_the_file_for_cleanup(cron, video):
    """FB succeeds, IG exhausts: the last platform to resolve cleans up either way.

    Without this, gating deletion on the other platform would trade a data-loss bug for
    a disk leak — the file would survive forever once either side failed.
    """
    import scripts.upload_instagram as ui
    ui.instagram_api.create_media_container.side_effect = InstagramUploadError("API 500")
    fb_main([])
    assert video.exists()
    _run_instagram_until_resolved()
    assert ig_state.get_pending_upload() is None
    assert not video.exists()


def test_facebook_failure_still_releases_the_file_for_cleanup(cron, video):
    """Mirror case: IG succeeds, FB exhausts, and the file is still cleaned up."""
    import scripts.upload_facebook as uf
    from tools.facebook_api import FacebookUploadError
    uf.facebook_api.upload_video.side_effect = FacebookUploadError("API 500")
    ig_main([])
    assert video.exists()
    for _ in range(3):
        fb_main([])
        record = fb_state.get_pending_upload()
        if record is None:
            break
        record["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
        fb_state.set_pending_upload(record)
    assert fb_state.get_pending_upload() is None
    assert not video.exists()


def test_instagram_disabled_lets_facebook_delete_immediately(cron, video, monkeypatch):
    """FR-016: a client without Instagram must not wait on a job that will never exist."""
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    ig_state.clear_pending_upload(_IDEM_KEY)
    fb_main([])
    assert not video.exists()


# ---------------------------------------------------------------------------
# Dangling Drive share links survive across ticks
# ---------------------------------------------------------------------------

def test_failed_revoke_is_recorded_and_retried_next_tick(cron, video):
    """A revoke failure is durable state plus a retry, never a silent success."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")

    ig_main([])
    assert ig_state.is_published(_IDEM_KEY) is True
    pending = ig_state.list_share_cleanups()
    assert [e["file_id"] for e in pending] == ["drive_file_1"]

    # Drive recovers; the next tick drains the backlog even with no new job.
    ui.drive.revoke_share_link.side_effect = None
    ig_main([])
    assert ig_state.list_share_cleanups() == []


def test_failed_revoke_alerts_once_within_the_reminder_window(cron, video):
    """The admin is told which Drive file may still be public, without per-tick spam."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")

    ig_main([])
    ig_main([])

    alerts = [
        c.args[1] for c in ui.telegram_api.send_message.call_args_list
        if "could not remove the temporary public link" in c.args[1]
    ]
    assert len(alerts) == 1
    assert "drive_file_1" in alerts[0]


def test_failed_revoke_reescalates_once_the_window_elapses(cron, video, monkeypatch):
    """A link that never gets revoked keeps reminding the admin — it never goes quiet.

    Gap 2b: previously the admin got exactly ONE alert ever for a given dangling link,
    however long cleanup kept failing, while the message implied follow-up would come.
    """
    import scripts.upload_instagram as ui
    monkeypatch.setattr(ig_state, "_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS", 0)
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")

    for _ in range(4):
        ig_main([])

    alerts = [
        c.args[1] for c in ui.telegram_api.send_message.call_args_list
        if "could not remove the temporary public link" in c.args[1]
    ]
    assert len(alerts) == 4
    # Each reminder reports the growing failure count, so escalation is visible.
    assert "Failed attempts: 1" in alerts[0]
    assert "Failed attempts: 4" in alerts[-1]


def test_reminder_stops_as_soon_as_the_revoke_succeeds(cron, video, monkeypatch):
    """Re-escalation must end when the problem does — no reminders about a fixed link."""
    import scripts.upload_instagram as ui
    monkeypatch.setattr(ig_state, "_SHARE_CLEANUP_ALERT_INTERVAL_SECONDS", 0)
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")
    ig_main([])
    ui.drive.revoke_share_link.side_effect = None
    ig_main([])
    ui.telegram_api.send_message.reset_mock()

    ig_main([])

    assert ig_state.list_share_cleanups() == []
    assert not any(
        "could not remove the temporary public link" in c.args[1]
        for c in ui.telegram_api.send_message.call_args_list
    )


# ---------------------------------------------------------------------------
# Cleanup survives Instagram being disabled or its token going away (gap 2a)
# ---------------------------------------------------------------------------

def test_cleanup_still_drains_after_instagram_is_disabled(cron, video, monkeypatch):
    """Disabling Instagram mid-flight must not strand an already-public link.

    The exact reported scenario: a revoke fails, the client then clears
    IG_BUSINESS_ACCOUNT_ID, and the link must still get cleaned up on a later tick
    rather than staying publicly reachable forever with no code path left to fix it.
    """
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")
    ig_main([])
    assert [e["file_id"] for e in ig_state.list_share_cleanups()] == ["drive_file_1"]

    # The client turns Instagram off entirely, and Drive recovers afterwards.
    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    ui.drive.revoke_share_link.side_effect = None

    ig_main([])

    ui.drive.revoke_share_link.assert_called_with("drive_file_1")
    assert ig_state.list_share_cleanups() == []


def test_cleanup_still_drains_after_the_meta_token_is_removed(cron, video, monkeypatch):
    """A revoked/expired Meta token must not strand cleanup either.

    Revoking a Drive permission needs Drive credentials and nothing else.
    """
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")
    ig_main([])
    assert len(ig_state.list_share_cleanups()) == 1

    monkeypatch.delenv("FB_PAGE_ACCESS_TOKEN", raising=False)
    ui.drive.revoke_share_link.side_effect = None

    with pytest.raises(SystemExit):
        ig_main([])  # still reports the misconfiguration, but only after cleaning up

    assert ig_state.list_share_cleanups() == []


def test_disabled_instagram_drains_cleanup_without_publishing(cron, video, monkeypatch):
    """Ungating cleanup must not ungate publishing — FR-016 still holds."""
    import scripts.upload_instagram as ui
    ui.drive.revoke_share_link.side_effect = RuntimeError("Drive down")
    ig_main([])
    ig_state.clear_pending_upload(_IDEM_KEY)

    monkeypatch.delenv("IG_BUSINESS_ACCOUNT_ID", raising=False)
    ui.drive.revoke_share_link.side_effect = None
    ui.instagram_api.create_media_container.reset_mock()

    ig_main([])

    assert ig_state.list_share_cleanups() == []
    ui.instagram_api.create_media_container.assert_not_called()


# ---------------------------------------------------------------------------
# FR-011 end to end: a lost publish response must never become a second Reel
# ---------------------------------------------------------------------------
#
# Walks the exact sequence a cross-vendor review identified, against the REAL state
# machines rather than mocks — because every component of the guarantee tested
# elsewhere in isolation can be individually correct while the path between them
# leaks. The sequence:
#
#   1. publish_container() succeeds at Meta, but its response is lost.
#   2. The saved container cannot be reconciled for the whole retry budget.
#   3. The final attempt takes the terminal path and calls mark_failed().
#   4. mark_failed() deletes the record, and container_id with it.
#   5. A later re-approval is accepted, because no tombstone remains.
#   6. A fresh container is created and published -> duplicate Reel, irreversibly.
#
# Steps 5 and 6 are what must now be impossible.


def _lost_publish_then_unreachable(ui):
    """Make the publish land at Meta invisibly, and every later question go unanswered.

    One FINISHED poll lets the first attempt reach the publish call; after that nothing
    can be established about the container, which is precisely the state that used to end
    with the container id discarded and the key free.
    """
    calls = {"n": 0}

    def _status(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "FINISHED"
        raise InstagramUploadError("Graph API unavailable")

    ui.instagram_api.get_container_status.side_effect = _status
    ui.instagram_api.publish_container.side_effect = InstagramUploadError(
        "Publish request failed: connection reset by peer"
    )


def test_an_unreconcilable_publish_is_quarantined_not_forgotten(cron, video):
    """Steps 1-4: the job ends, but the obligation does not."""
    import scripts.upload_instagram as ui
    _lost_publish_then_unreachable(ui)

    _run_instagram_until_resolved()

    assert ig_state.get_pending_upload() is None            # step 4: record really is gone
    assert ig_state.is_published(_IDEM_KEY) is False        # nothing was ever confirmed
    entries = ig_state.list_publish_reconciliations()
    assert [e["container_id"] for e in entries] == [_CONTAINER_ID]
    assert entries[0]["idempotency_key"] == _IDEM_KEY


def test_a_re_approval_cannot_publish_a_second_reel(cron, video, mocker):
    """Steps 5-6, the irreversible outcome — now refused.

    The re-approval runs the real approve path again. Before the quarantine existed it
    would have enqueued a fresh job, and the next cron tick would have built and published
    a second container onto the client's real Instagram account.
    """
    import scripts.check_approval as ca
    import scripts.upload_instagram as ui
    _lost_publish_then_unreachable(ui)
    _run_instagram_until_resolved()
    assert ig_state.has_unresolved_publish(_IDEM_KEY) is True

    created_before = ui.instagram_api.create_media_container.call_count
    approve_main(["--callback-data", "approve"])

    assert ig_state.get_pending_upload() is None            # nothing re-queued
    ig_main([])
    assert ui.instagram_api.create_media_container.call_count == created_before
    ui.instagram_api.publish_container.assert_called_once()  # still exactly the one attempt


def test_the_quarantine_does_not_block_the_facebook_side(cron, video):
    """FR-013 holds even here: Facebook publishes normally and is not held back."""
    import scripts.upload_instagram as ui
    _lost_publish_then_unreachable(ui)
    _run_instagram_until_resolved()
    fb_main([])
    assert fb_state.is_published(_IDEM_KEY) is True


def test_a_later_tick_resolves_the_quarantine_as_published(cron, video):
    """Instagram finally answers PUBLISHED: recorded once, key retired permanently."""
    import scripts.upload_instagram as ui
    _lost_publish_then_unreachable(ui)
    _run_instagram_until_resolved()

    # The Graph API comes back and reports what actually happened.
    ui.instagram_api.get_container_status.side_effect = None
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    ig_main([])

    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.is_published(_IDEM_KEY) is True
    entry = ig_state.find_published(_PROJECT)
    assert entry["recovered"] is True
    assert entry["ig_container_id"] == _CONTAINER_ID
    # And the key stays retired: a re-approval is still refused, permanently this time.
    approve_main(["--callback-data", "approve"])
    assert ig_state.get_pending_upload() is None


def test_a_later_tick_resolves_the_quarantine_as_never_published(cron, video):
    """Instagram answers EXPIRED: nothing went live, so the video becomes re-approvable.

    The mirror image, and just as important — a quarantine that could never lift would
    turn one lost response into a permanently un-postable video.
    """
    import scripts.upload_instagram as ui
    _lost_publish_then_unreachable(ui)
    _run_instagram_until_resolved()

    ui.instagram_api.get_container_status.side_effect = None
    ui.instagram_api.get_container_status.return_value = "EXPIRED"
    ig_main([])

    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.has_unresolved_publish(_IDEM_KEY) is False
    assert ig_state.is_published(_IDEM_KEY) is False

    # Re-approval now goes through, and a fresh upload can publish for real.
    ui.instagram_api.get_container_status.return_value = "FINISHED"
    ui.instagram_api.publish_container.side_effect = None
    ui.instagram_api.publish_container.return_value = _IG_POST_ID
    approve_main(["--callback-data", "approve"])
    assert ig_state.get_pending_upload() is not None
    ig_main([])
    assert ig_state.is_published(_IDEM_KEY) is True


# ---------------------------------------------------------------------------
# The exhausted transition is atomic with the quarantine it requires
# ---------------------------------------------------------------------------
#
# Round 3's quarantine survives mark_failed(), but CREATING it was not atomic with
# the clear that necessitates it. claim_pending_upload() cleared and fsynced the
# pending record, returned "exhausted", and only then did the caller quarantine —
# so a process that died in between left neither a pending job nor an obligation,
# and the next re-approval could publish a duplicate Reel. Driven here against the
# REAL state machine, with the caller deliberately prevented from running.


def _force_exhausted_with_unresolved_publish():
    """Leave the pending record one tick away from an exhausted claim, publish unresolved.

    Reproduces the state a process leaves behind when it dies during its final attempt:
    the attempt budget is spent, and the durable marker plus container id say a publish
    was asked for and never confirmed.
    """
    record = ig_state.get_pending_upload()
    record["attempt_count"] = 3
    record["status"] = "pending"
    record["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    record["container_id"] = _CONTAINER_ID
    record["publish_attempted_at"] = "2026-08-31T14:05:00Z"
    ig_state.set_pending_upload(record)


def test_a_process_that_dies_after_the_exhausted_claim_leaves_the_quarantine_behind(
    cron, video, mocker
):
    """THE round-4 fix, end to end: the entry is durable before the caller gets a turn."""
    import scripts.upload_instagram as ui
    _force_exhausted_with_unresolved_publish()
    # The process dies the instant claim_pending_upload() returns "exhausted".
    mocker.patch.object(ui, "_handle_exhausted", side_effect=RuntimeError("process killed"))

    with pytest.raises(RuntimeError, match="process killed"):
        ig_main([])

    assert ig_state.get_pending_upload() is None          # the clear happened
    assert ig_state.has_unresolved_publish(_IDEM_KEY) is True   # so did the quarantine
    assert [e["container_id"] for e in ig_state.list_publish_reconciliations()] == [
        _CONTAINER_ID
    ]


def test_a_re_approval_after_that_death_is_still_refused(cron, video, mocker):
    """Step 6 of the surviving sequence, now impossible."""
    import scripts.upload_instagram as ui
    _force_exhausted_with_unresolved_publish()
    # The container stays unanswerable, so the block genuinely has to hold rather than
    # being lifted by a definitive "never published" on the very next tick.
    ui.instagram_api.get_container_status.side_effect = InstagramUploadError("graph down")
    mocker.patch.object(ui, "_handle_exhausted", side_effect=RuntimeError("process killed"))
    with pytest.raises(RuntimeError):
        ig_main([])

    created_before = ui.instagram_api.create_media_container.call_count
    published_before = ui.instagram_api.publish_container.call_count
    approve_main(["--callback-data", "approve"])
    assert ig_state.get_pending_upload() is None        # nothing re-queued

    # And a later tick cannot turn it into a second Reel either — the quarantine is
    # still unresolved, so the drain leaves it in place and no job exists to run.
    ig_main([])
    assert ui.instagram_api.create_media_container.call_count == created_before
    assert ui.instagram_api.publish_container.call_count == published_before
    assert ig_state.has_unresolved_publish(_IDEM_KEY) is True


def test_the_next_tick_picks_up_the_orphaned_quarantine_and_resolves_it(cron, video, mocker):
    """The obligation is not just durable, it is actionable by whatever runs next."""
    import scripts.upload_instagram as ui
    _force_exhausted_with_unresolved_publish()
    handler = mocker.patch.object(
        ui, "_handle_exhausted", side_effect=RuntimeError("process killed")
    )
    with pytest.raises(RuntimeError):
        ig_main([])

    # A later tick, with the Graph API answering again.
    handler.side_effect = None
    ui.instagram_api.get_container_status.side_effect = None
    ui.instagram_api.get_container_status.return_value = "PUBLISHED"
    ig_main([])

    assert ig_state.list_publish_reconciliations() == []
    assert ig_state.is_published(_IDEM_KEY) is True


def test_an_exhausted_job_that_never_published_stays_re_approvable(cron, video):
    """The other direction: no publish attempt means no block, so the owner can retry."""
    record = ig_state.get_pending_upload()
    record["attempt_count"] = 3
    record["status"] = "pending"
    record["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    record["container_id"] = _CONTAINER_ID          # built, but never published from
    ig_state.set_pending_upload(record)

    ig_main([])

    assert ig_state.get_pending_upload() is None
    assert ig_state.has_unresolved_publish(_IDEM_KEY) is False
    assert ig_state.list_publish_reconciliations() == []
