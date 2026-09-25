"""
Tests for tools/worker_health.py — cron worker liveness heartbeats.

The behaviour under test is a deployment guard, so the tests are written around the
two questions callers actually ask: "has this worker ever run?" and "has it run
recently enough that I should wait for it / queue work for it?".

The bias throughout is that UNCERTAINTY MUST READ AS "NOT DEPLOYED". Every failure
mode here — no file, corrupt file, unparseable timestamp, unwritable directory —
resolves to False, because being wrong that way refuses an enqueue or releases a
video, while being wrong the other way strands a job forever with no worker to
drain it.
"""

from datetime import datetime, timedelta, timezone

import pytest

import tools.worker_health as wh


@pytest.fixture(autouse=True)
def isolated_health(tmp_path, monkeypatch):
    """Redirect the heartbeat file to an isolated tmp directory for every test."""
    data_dir = tmp_path / "photo-agent"
    monkeypatch.setattr(wh, "DATA_DIR", data_dir)
    monkeypatch.setattr(wh, "HEALTH_FILE", data_dir / "worker_health.json")


def _write_heartbeat(worker: str, age_seconds: float) -> None:
    """Stamp worker as last seen age_seconds ago."""
    import json
    wh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    existing = {}
    if wh.HEALTH_FILE.exists():
        existing = json.loads(wh.HEALTH_FILE.read_text())
    existing[worker] = {"last_seen_at": when.isoformat()}
    wh.HEALTH_FILE.write_text(json.dumps(existing))


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_record_heartbeat_creates_the_file_and_marks_the_worker_alive():
    """A first tick on a brand new client directory is enough to register as deployed."""
    wh.record_heartbeat("instagram")
    assert wh.HEALTH_FILE.exists()
    assert wh.is_deployed("instagram") is True


def test_each_worker_is_tracked_independently():
    """Facebook being deployed must never make Instagram look deployed.

    This is the whole point: the Facebook cron is the one that reliably exists on this
    machine, and the Instagram one is the one that may not have been installed.
    """
    wh.record_heartbeat("facebook")
    assert wh.is_deployed("facebook") is True
    assert wh.is_deployed("instagram") is False


def test_recording_one_worker_does_not_erase_another():
    """Both workers write the same file on their own schedules."""
    wh.record_heartbeat("facebook")
    wh.record_heartbeat("instagram")
    assert wh.is_deployed("facebook") is True
    assert wh.is_deployed("instagram") is True


def test_a_later_heartbeat_refreshes_a_stale_worker():
    """Self-healing: installing the cron restores health with no other action."""
    _write_heartbeat("instagram", age_seconds=wh.STALE_AFTER_SECONDS + 600)
    assert wh.is_deployed("instagram") is False
    wh.record_heartbeat("instagram")
    assert wh.is_deployed("instagram") is True


# ---------------------------------------------------------------------------
# Reading — every unknown resolves to "not deployed"
# ---------------------------------------------------------------------------

def test_a_worker_that_never_ran_is_not_deployed():
    """The undeployed-cron case this whole mechanism exists for."""
    assert wh.seconds_since_heartbeat("instagram") is None
    assert wh.is_deployed("instagram") is False


def test_a_missing_file_is_not_deployed():
    """A fresh client directory, or one whose data was wiped."""
    assert wh.HEALTH_FILE.exists() is False
    assert wh.is_deployed("instagram") is False


def test_a_corrupt_file_is_not_deployed_and_does_not_raise():
    """Corruption must degrade, not crash: this is read on every approval."""
    wh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    wh.HEALTH_FILE.write_text("{not json at all")
    assert wh.is_deployed("instagram") is False


def test_an_unparseable_timestamp_is_not_deployed():
    """A hand-edited or truncated entry must not read as alive."""
    import json
    wh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    wh.HEALTH_FILE.write_text(json.dumps({"instagram": {"last_seen_at": "yesterday-ish"}}))
    assert wh.is_deployed("instagram") is False


def test_a_json_file_that_is_not_an_object_is_not_deployed():
    """Defensive: a list or a bare string where a dict was expected."""
    wh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    wh.HEALTH_FILE.write_text("[1, 2, 3]")
    assert wh.is_deployed("instagram") is False


def test_a_recent_heartbeat_is_deployed():
    """A worker ticking every minute is comfortably inside the window."""
    _write_heartbeat("instagram", age_seconds=120)
    assert wh.is_deployed("instagram") is True


def test_a_heartbeat_older_than_the_window_is_not_deployed():
    """Sixty consecutive missed one-minute ticks means the entry is gone."""
    _write_heartbeat("instagram", age_seconds=wh.STALE_AFTER_SECONDS + 1)
    assert wh.is_deployed("instagram") is False


def test_the_staleness_window_is_caller_overridable():
    """Callers may be stricter; the default is deliberately generous."""
    _write_heartbeat("instagram", age_seconds=300)
    assert wh.is_deployed("instagram", stale_after_seconds=60) is False
    assert wh.is_deployed("instagram", stale_after_seconds=600) is True


def test_the_default_window_tolerates_a_long_single_attempt():
    """An Instagram attempt can legitimately occupy ~5 minutes of container polling.

    Pins the intent behind the hour-long default: a worker mid-attempt must never be
    mistaken for a worker that was never installed.
    """
    assert wh.STALE_AFTER_SECONDS > 10 * 60


def test_seconds_since_heartbeat_reports_a_real_age():
    """Callers word their alerts from this, so it has to be a number, not a flag."""
    _write_heartbeat("facebook", age_seconds=900)
    age = wh.seconds_since_heartbeat("facebook")
    assert 880 < age < 920


def test_a_naive_timestamp_is_treated_as_utc():
    """Back-compatibility with any entry written without an offset."""
    import json
    wh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    wh.HEALTH_FILE.write_text(json.dumps({"instagram": {"last_seen_at": naive}}))
    assert wh.is_deployed("instagram") is True


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def test_record_heartbeat_never_raises_when_it_cannot_write(monkeypatch):
    """A heartbeat problem must not take down an otherwise healthy upload.

    record_heartbeat() runs at the very top of both cron scripts, before any real work.
    If it could raise, an unwritable data directory would stop a client's video from
    being posted — trading a cosmetic failure for a real one.
    """
    def _boom(*args, **kwargs):
        raise OSError("read-only file system")
    monkeypatch.setattr(wh.os, "open", _boom)
    wh.record_heartbeat("instagram")  # must not raise
    assert wh.is_deployed("instagram") is False
