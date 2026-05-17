"""
Tests for tools/drive.py — the gws Drive wrapper.

All tests mock subprocess.run so gws is never invoked and no Drive API calls
are made. Tests verify command construction, JSON parsing, filtering/sorting
logic, and error propagation.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.drive import (
    DriveFolderNotFoundError,
    delete,
    download,
    find_folder,
    folder_link,
    list_photos,
    upload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gws_ok(stdout: str):
    """Return a CompletedProcess simulating a successful gws run."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _gws_fail(stderr: str = "gws error"):
    """Return a CompletedProcess simulating a failed gws run."""
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def _folder_response(files: list[dict]) -> str:
    return json.dumps({"files": files})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gws():
    """Patch subprocess.run for all drive module calls."""
    with patch("tools.drive.subprocess.run") as mock:
        yield mock


# ---------------------------------------------------------------------------
# find_folder
# ---------------------------------------------------------------------------

def test_find_folder_returns_folder_id(mock_gws):
    """find_folder() parses gws JSON and returns the matching folder ID."""
    mock_gws.return_value = _gws_ok(_folder_response([{"id": "folder123"}]))
    result = find_folder("kitchen_remodel", "root_parent")
    assert result == "folder123"


def test_find_folder_raises_when_empty(mock_gws):
    """find_folder() raises DriveFolderNotFoundError when gws returns an empty file list."""
    mock_gws.return_value = _gws_ok(_folder_response([]))
    with pytest.raises(DriveFolderNotFoundError, match="kitchen_remodel"):
        find_folder("kitchen_remodel", "root_parent")


def test_find_folder_error_has_structured_fields(mock_gws):
    """DriveFolderNotFoundError carries .name and .parent_id attributes."""
    mock_gws.return_value = _gws_ok(_folder_response([]))
    with pytest.raises(DriveFolderNotFoundError) as exc_info:
        find_folder("my_project", "parent_xyz")
    assert exc_info.value.name == "my_project"
    assert exc_info.value.parent_id == "parent_xyz"


def test_find_folder_query_includes_name_and_parent(mock_gws):
    """find_folder() passes name and parent_id in the query string."""
    mock_gws.return_value = _gws_ok(_folder_response([{"id": "abc"}]))
    find_folder("my_project", "parent_xyz")
    cmd = mock_gws.call_args.args[0]
    params_str = cmd[cmd.index("--params") + 1]
    params = json.loads(params_str)
    assert "my_project" in params["q"]
    assert "parent_xyz" in params["q"]


def test_find_folder_nonzero_exit_raises_runtime_error(mock_gws):
    """find_folder() raises RuntimeError on non-zero gws exit."""
    mock_gws.return_value = _gws_fail("permission denied")
    with pytest.raises(RuntimeError, match="gws exited"):
        find_folder("project", "parent")


# ---------------------------------------------------------------------------
# list_photos
# ---------------------------------------------------------------------------

def test_list_photos_filters_non_image_mime_types(mock_gws):
    """list_photos() excludes files that are not image/jpeg or image/png."""
    mock_gws.return_value = _gws_ok(_folder_response([
        {"id": "1", "name": "doc.pdf",  "mimeType": "application/pdf",  "size": "1000"},
        {"id": "2", "name": "img.jpg",  "mimeType": "image/jpeg",        "size": "5000"},
        {"id": "3", "name": "img.png",  "mimeType": "image/png",         "size": "4000"},
        {"id": "4", "name": "vid.mp4",  "mimeType": "video/mp4",         "size": "9000"},
    ]))
    results = list_photos("folder123")
    ids = [r["id"] for r in results]
    assert "2" in ids
    assert "3" in ids
    assert "1" not in ids
    assert "4" not in ids


def test_list_photos_sorted_alphabetically(mock_gws):
    """list_photos() returns results sorted alphabetically by name."""
    mock_gws.return_value = _gws_ok(_folder_response([
        {"id": "3", "name": "charlie.jpg", "mimeType": "image/jpeg", "size": "1000"},
        {"id": "1", "name": "alpha.jpg",   "mimeType": "image/jpeg", "size": "1000"},
        {"id": "2", "name": "bravo.jpg",   "mimeType": "image/jpeg", "size": "1000"},
    ]))
    results = list_photos("folder123")
    assert [r["name"] for r in results] == ["alpha.jpg", "bravo.jpg", "charlie.jpg"]


def test_list_photos_skips_zero_byte_files(mock_gws):
    """list_photos() excludes files with size == "0" (string, as returned by Drive API)."""
    mock_gws.return_value = _gws_ok(_folder_response([
        {"id": "1", "name": "empty.jpg", "mimeType": "image/jpeg", "size": "0"},
        {"id": "2", "name": "real.jpg",  "mimeType": "image/jpeg", "size": "8192"},
    ]))
    results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_skips_zero_byte_integer_size(mock_gws):
    """list_photos() excludes files with size == 0 as an integer (defensive against API type variation)."""
    mock_gws.return_value = _gws_ok(_folder_response([
        {"id": "1", "name": "empty.jpg", "mimeType": "image/jpeg", "size": 0},
        {"id": "2", "name": "real.jpg",  "mimeType": "image/jpeg", "size": "8192"},
    ]))
    results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_skips_missing_size_field(mock_gws):
    """list_photos() treats a missing size field as zero and skips the file."""
    mock_gws.return_value = _gws_ok(_folder_response([
        {"id": "1", "name": "no_size.jpg", "mimeType": "image/jpeg"},
        {"id": "2", "name": "has_size.jpg", "mimeType": "image/jpeg", "size": "1024"},
    ]))
    results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_nonzero_exit_raises_runtime_error(mock_gws):
    """list_photos() raises RuntimeError on non-zero gws exit."""
    mock_gws.return_value = _gws_fail()
    with pytest.raises(RuntimeError, match="gws exited"):
        list_photos("folder123")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

_FAKE_CREDS = json.dumps({
    "client_id": "cid", "client_secret": "csecret",
    "refresh_token": "rtoken", "type": "authorized_user",
})


def test_download_writes_content_to_output_path(mock_gws, tmp_path):
    """download() fetches file content from Drive REST API and writes it to output_path."""
    mock_gws.return_value = _gws_ok(_FAKE_CREDS)
    output = tmp_path / "photo.jpg"
    fake_bytes = b"\xff\xd8\xff" * 16

    with patch("tools.drive.requests.post") as mock_post, \
         patch("tools.drive.requests.get") as mock_get:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"access_token": "tok123"}
        mock_get.return_value.ok = True
        mock_get.return_value.iter_content.return_value = [fake_bytes]
        download("file_abc", output)

    assert output.read_bytes() == fake_bytes


def test_download_calls_drive_api_with_correct_file_id(mock_gws, tmp_path):
    """download() calls the Drive REST API URL containing the correct file_id."""
    mock_gws.return_value = _gws_ok(_FAKE_CREDS)

    with patch("tools.drive.requests.post") as mock_post, \
         patch("tools.drive.requests.get") as mock_get:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"access_token": "tok"}
        mock_get.return_value.ok = True
        mock_get.return_value.iter_content.return_value = [b"data"]
        download("file_abc", tmp_path / "out.jpg")

    url = mock_get.call_args.args[0]
    assert "file_abc" in url
    assert mock_get.call_args.kwargs.get("params", {}).get("alt") == "media"


def test_download_raises_on_auth_export_failure(mock_gws, tmp_path):
    """download() raises RuntimeError when gws auth export fails."""
    mock_gws.return_value = _gws_fail("keyring error")
    with pytest.raises(RuntimeError, match="gws auth export failed"):
        download("file_abc", tmp_path / "out.jpg")


def test_download_raises_on_http_error(mock_gws, tmp_path):
    """download() raises RuntimeError when the Drive REST API returns an error."""
    mock_gws.return_value = _gws_ok(_FAKE_CREDS)

    with patch("tools.drive.requests.post") as mock_post, \
         patch("tools.drive.requests.get") as mock_get:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"access_token": "tok"}
        mock_get.return_value.ok = False
        mock_get.return_value.status_code = 403
        with pytest.raises(RuntimeError, match="Drive download failed"):
            download("file_abc", tmp_path / "out.jpg")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_upload_passes_parent_and_name_flags(mock_gws, tmp_path):
    """upload() passes --parent, --name, and the local file path in the gws command."""
    mock_gws.return_value = _gws_ok(json.dumps({"id": "new_file_id"}))
    local = tmp_path / "video.mp4"
    local.write_bytes(b"x")
    upload(local, "parent_xyz", "my_video.mp4")
    cmd = mock_gws.call_args.args[0]
    assert "--parent" in cmd and cmd[cmd.index("--parent") + 1] == "parent_xyz"
    assert "--name" in cmd and cmd[cmd.index("--name") + 1] == "my_video.mp4"
    assert str(local) in cmd


def test_upload_returns_drive_file_id(mock_gws, tmp_path):
    """upload() returns the Drive file ID parsed from the gws response."""
    mock_gws.return_value = _gws_ok(json.dumps({"id": "new_file_id"}))
    local = tmp_path / "video.mp4"
    local.write_bytes(b"x")
    result = upload(local, "parent_xyz", "video.mp4")
    assert result == "new_file_id"


def test_upload_raises_if_local_path_missing(mock_gws, tmp_path):
    """upload() raises FileNotFoundError before calling gws if the local file does not exist."""
    with pytest.raises(FileNotFoundError, match="local file not found"):
        upload(tmp_path / "ghost.mp4", "parent", "ghost.mp4")
    mock_gws.assert_not_called()


def test_upload_nonzero_exit_raises_runtime_error(mock_gws, tmp_path):
    """upload() raises RuntimeError on non-zero gws exit."""
    mock_gws.return_value = _gws_fail()
    local = tmp_path / "video.mp4"
    local.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="gws exited"):
        upload(local, "parent", "video.mp4")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_passes_file_id(mock_gws):
    """delete() passes the correct fileId in --params to the gws command."""
    mock_gws.return_value = _gws_ok("")
    delete("file_to_delete")
    cmd = mock_gws.call_args.args[0]
    params_str = cmd[cmd.index("--params") + 1]
    assert json.loads(params_str)["fileId"] == "file_to_delete"


def test_delete_nonzero_exit_raises_runtime_error(mock_gws):
    """delete() raises RuntimeError on non-zero gws exit."""
    mock_gws.return_value = _gws_fail()
    with pytest.raises(RuntimeError, match="gws exited"):
        delete("file_abc")


# ---------------------------------------------------------------------------
# folder_link
# ---------------------------------------------------------------------------

def test_folder_link_returns_correct_url():
    """folder_link() returns the canonical Drive folder web link."""
    assert folder_link("abc123") == "https://drive.google.com/drive/folders/abc123"


def test_folder_link_does_not_call_subprocess():
    """folder_link() is a pure function — it must not invoke any subprocess."""
    with patch("tools.drive.subprocess.run") as mock_run:
        folder_link("xyz789")
    mock_run.assert_not_called()
