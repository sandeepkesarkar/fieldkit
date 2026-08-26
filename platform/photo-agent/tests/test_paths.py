"""
Tests for tools/paths.py — shared VIDEO_TMP_DIR resolution.

process_photos.py (producer) and check_approval.py / upload_facebook.py
(the two cleanup consumers) all call tools.paths.get_video_tmp_root() so
their resolution can't drift apart again — see issue #47, where the
producer's initial fix left the two consumers independently resolving
relative paths against the shared repo root instead of FIELDKIT_DATA_DIR.
These tests cover the three VIDEO_TMP_DIR states (unset, relative,
absolute) that resolution must handle identically for all three scripts.
"""

from tools.paths import get_video_tmp_root


def test_unset_video_tmp_dir_defaults_to_fieldkit_data_dir_photo_agent_tmp(monkeypatch, tmp_path):
    """With VIDEO_TMP_DIR unset, the default is <FIELDKIT_DATA_DIR>/photo-agent/tmp."""
    monkeypatch.delenv("VIDEO_TMP_DIR", raising=False)
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    assert get_video_tmp_root() == (tmp_path / "photo-agent" / "tmp").resolve()


def test_relative_video_tmp_dir_resolves_against_fieldkit_data_dir(monkeypatch, tmp_path):
    """A relative VIDEO_TMP_DIR resolves against FIELDKIT_DATA_DIR, not the repo root."""
    monkeypatch.setenv("VIDEO_TMP_DIR", "data/photo-agent/tmp")
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    assert get_video_tmp_root() == (tmp_path / "data" / "photo-agent" / "tmp").resolve()


def test_absolute_video_tmp_dir_used_as_is(monkeypatch, tmp_path):
    """An absolute VIDEO_TMP_DIR is used verbatim, ignoring FIELDKIT_DATA_DIR."""
    absolute = tmp_path / "custom-tmp"
    monkeypatch.setenv("VIDEO_TMP_DIR", str(absolute))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "data"))
    assert get_video_tmp_root() == absolute.resolve()


def test_two_clients_same_relative_video_tmp_dir_resolve_differently(monkeypatch, tmp_path):
    """Two clients with the same relative VIDEO_TMP_DIR value must not collide (#47)."""
    monkeypatch.setenv("VIDEO_TMP_DIR", "data/photo-agent/tmp")

    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "clients" / "mercury" / "data"))
    mercury = get_video_tmp_root()

    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "clients" / "venus" / "data"))
    venus = get_video_tmp_root()

    assert mercury != venus
