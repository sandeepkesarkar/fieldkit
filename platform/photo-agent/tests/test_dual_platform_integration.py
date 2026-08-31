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
_SHARE_LINK = "https://drive.google.com/uc?export=download&id=drive_file_1"


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
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FIELDKIT_LOG_DIR", str(tmp_path / "logs"))
    return data_dir


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "video.mp4"
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
    mocker.patch.object(uf, "_delete_local_file")
    for name in ("log_upload_started", "log_upload_published",
                 "log_upload_attempt_failed", "log_upload_exhausted", "log_token_expired"):
        mocker.patch.object(uf.facebook_logger, name)

    mocker.patch.object(ui.drive, "create_temporary_share_link", return_value=_SHARE_LINK)
    mocker.patch.object(ui.drive, "revoke_share_link")
    mocker.patch.object(ui.instagram_api, "create_media_container", return_value=_CONTAINER_ID)
    mocker.patch.object(ui.instagram_api, "get_container_status", return_value="FINISHED")
    mocker.patch.object(ui.instagram_api, "publish_container", return_value=_IG_POST_ID)
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
    assert f"https://www.instagram.com/p/{_IG_POST_ID}" in ui.telegram_api.send_message.call_args.args[1]


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
