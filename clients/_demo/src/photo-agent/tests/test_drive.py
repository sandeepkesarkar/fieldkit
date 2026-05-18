"""
Tests for tools/drive.py — the Drive REST API wrapper.

All tests mock requests.get / requests.post / requests.delete and
_get_access_token so no real HTTP calls are made. Tests verify
query construction, JSON parsing, filtering/sorting logic, and error propagation.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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

def _ok_response(data) -> MagicMock:
    """Return a mock requests.Response with ok=True and JSON body."""
    m = MagicMock()
    m.ok = True
    m.status_code = 200
    m.json.return_value = data if isinstance(data, dict) else {"files": data}
    return m


def _err_response(status: int = 403) -> MagicMock:
    """Return a mock requests.Response with ok=False."""
    m = MagicMock()
    m.ok = False
    m.status_code = status
    return m


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_token():
    """Patch _get_access_token for all tests so no credentials file is needed."""
    with patch("tools.drive._get_access_token", return_value="fake_token"):
        yield


# ---------------------------------------------------------------------------
# _load_credentials — credentials file loading
# ---------------------------------------------------------------------------

def test_load_credentials_raises_when_file_missing(tmp_path, monkeypatch):
    """_load_credentials raises RuntimeError when the credentials file does not exist."""
    monkeypatch.setenv("GOOGLE_USER_CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    from tools.drive import _load_credentials
    with pytest.raises(RuntimeError, match="credentials file not found"):
        _load_credentials()


def test_load_credentials_raises_on_invalid_json(tmp_path, monkeypatch):
    """_load_credentials raises RuntimeError when the credentials file is not valid JSON."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text("not json")
    monkeypatch.setenv("GOOGLE_USER_CREDENTIALS_FILE", str(creds_file))
    from tools.drive import _load_credentials
    with pytest.raises(RuntimeError, match="Failed to read credentials"):
        _load_credentials()


# ---------------------------------------------------------------------------
# find_folder
# ---------------------------------------------------------------------------

def test_find_folder_returns_folder_id():
    """find_folder() parses Drive JSON and returns the matching folder ID."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [{"id": "folder123"}]})
        result = find_folder("kitchen_remodel", "root_parent")
    assert result == "folder123"


def test_find_folder_raises_when_empty():
    """find_folder() raises DriveFolderNotFoundError when Drive returns an empty file list."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": []})
        with pytest.raises(DriveFolderNotFoundError, match="kitchen_remodel"):
            find_folder("kitchen_remodel", "root_parent")


def test_find_folder_error_has_structured_fields():
    """DriveFolderNotFoundError carries .name and .parent_id attributes."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": []})
        with pytest.raises(DriveFolderNotFoundError) as exc_info:
            find_folder("my_project", "parent_xyz")
    assert exc_info.value.name == "my_project"
    assert exc_info.value.parent_id == "parent_xyz"


def test_find_folder_query_includes_name_and_parent():
    """find_folder() passes name and parent_id in the q parameter."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [{"id": "abc"}]})
        find_folder("my_project", "parent_xyz")
    params = mock_get.call_args.kwargs.get("params", {})
    assert "my_project" in params.get("q", "")
    assert "parent_xyz" in params.get("q", "")


def test_find_folder_raises_on_http_error():
    """find_folder() raises RuntimeError on Drive API HTTP error."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _err_response(403)
        with pytest.raises(RuntimeError, match="Drive GET failed"):
            find_folder("project", "parent")


def test_find_folder_rejects_unsafe_name():
    """find_folder() raises ValueError when the folder name contains query-unsafe characters."""
    with pytest.raises(ValueError, match="unsafe folder name"):
        find_folder('bad"name', "parent")


def test_find_folder_warns_on_multiple_results(caplog):
    """find_folder() logs a warning and returns the first result when multiple folders match."""
    import logging
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [{"id": "folder_a"}, {"id": "folder_b"}]})
        with caplog.at_level(logging.WARNING, logger="tools.drive"):
            result = find_folder("my_project", "parent")
    assert result == "folder_a"
    assert "folders named" in caplog.text


# ---------------------------------------------------------------------------
# list_photos
# ---------------------------------------------------------------------------

def test_list_photos_filters_non_image_mime_types():
    """list_photos() excludes files that are not image/jpeg or image/png."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [
            {"id": "1", "name": "doc.pdf",  "mimeType": "application/pdf",  "size": "1000"},
            {"id": "2", "name": "img.jpg",  "mimeType": "image/jpeg",        "size": "5000"},
            {"id": "3", "name": "img.png",  "mimeType": "image/png",         "size": "4000"},
            {"id": "4", "name": "vid.mp4",  "mimeType": "video/mp4",         "size": "9000"},
        ]})
        results = list_photos("folder123")
    ids = [r["id"] for r in results]
    assert "2" in ids
    assert "3" in ids
    assert "1" not in ids
    assert "4" not in ids


def test_list_photos_sorted_alphabetically():
    """list_photos() returns results sorted alphabetically by name."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [
            {"id": "3", "name": "charlie.jpg", "mimeType": "image/jpeg", "size": "1000"},
            {"id": "1", "name": "alpha.jpg",   "mimeType": "image/jpeg", "size": "1000"},
            {"id": "2", "name": "bravo.jpg",   "mimeType": "image/jpeg", "size": "1000"},
        ]})
        results = list_photos("folder123")
    assert [r["name"] for r in results] == ["alpha.jpg", "bravo.jpg", "charlie.jpg"]


def test_list_photos_skips_zero_byte_files():
    """list_photos() excludes files with size == "0"."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [
            {"id": "1", "name": "empty.jpg", "mimeType": "image/jpeg", "size": "0"},
            {"id": "2", "name": "real.jpg",  "mimeType": "image/jpeg", "size": "8192"},
        ]})
        results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_skips_zero_byte_integer_size():
    """list_photos() excludes files with size == 0 as an integer."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [
            {"id": "1", "name": "empty.jpg", "mimeType": "image/jpeg", "size": 0},
            {"id": "2", "name": "real.jpg",  "mimeType": "image/jpeg", "size": "8192"},
        ]})
        results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_skips_missing_size_field():
    """list_photos() treats a missing size field as zero and skips the file."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _ok_response({"files": [
            {"id": "1", "name": "no_size.jpg",  "mimeType": "image/jpeg"},
            {"id": "2", "name": "has_size.jpg", "mimeType": "image/jpeg", "size": "1024"},
        ]})
        results = list_photos("folder123")
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_list_photos_raises_on_http_error():
    """list_photos() raises RuntimeError on Drive API HTTP error."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value = _err_response(403)
        with pytest.raises(RuntimeError, match="Drive GET failed"):
            list_photos("folder123")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def test_download_writes_content_to_output_path(tmp_path):
    """download() fetches file content and writes it to output_path."""
    output = tmp_path / "photo.jpg"
    fake_bytes = b"\xff\xd8\xff" * 16

    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value.ok = True
        mock_get.return_value.iter_content.return_value = [fake_bytes]
        download("file_abc", output)

    assert output.read_bytes() == fake_bytes


def test_download_calls_drive_api_with_correct_file_id(tmp_path):
    """download() calls the Drive REST API URL containing the correct file_id."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value.ok = True
        mock_get.return_value.iter_content.return_value = [b"data"]
        download("file_abc", tmp_path / "out.jpg")

    url = mock_get.call_args.args[0]
    assert "file_abc" in url
    assert mock_get.call_args.kwargs.get("params", {}).get("alt") == "media"


def test_download_raises_on_http_error(tmp_path):
    """download() raises RuntimeError when the Drive REST API returns an error."""
    with patch("tools.drive.requests.get") as mock_get:
        mock_get.return_value.ok = False
        mock_get.return_value.status_code = 403
        with pytest.raises(RuntimeError, match="Drive download failed"):
            download("file_abc", tmp_path / "out.jpg")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_upload_returns_drive_file_id(tmp_path):
    """upload() returns the Drive file ID from the upload response."""
    local = tmp_path / "video.mp4"
    local.write_bytes(b"x" * 100)

    init_resp = MagicMock()
    init_resp.ok = True
    init_resp.headers = {"Location": "https://upload.googleapis.com/session/abc"}

    upload_resp = MagicMock()
    upload_resp.ok = True
    upload_resp.json.return_value = {"id": "new_file_id"}

    with patch("tools.drive.requests.post", return_value=init_resp), \
         patch("tools.drive.requests.put", return_value=upload_resp):
        result = upload(local, "parent_xyz", "video.mp4")

    assert result == "new_file_id"


def test_upload_raises_if_local_path_missing(tmp_path):
    """upload() raises FileNotFoundError before any API call if the local file does not exist."""
    with patch("tools.drive.requests.post") as mock_post:
        with pytest.raises(FileNotFoundError, match="local file not found"):
            upload(tmp_path / "ghost.mp4", "parent", "ghost.mp4")
    mock_post.assert_not_called()


def test_upload_raises_on_initiation_failure(tmp_path):
    """upload() raises RuntimeError when the upload initiation request fails."""
    local = tmp_path / "video.mp4"
    local.write_bytes(b"x")
    init_resp = MagicMock()
    init_resp.ok = False
    init_resp.status_code = 403
    with patch("tools.drive.requests.post", return_value=init_resp):
        with pytest.raises(RuntimeError, match="Drive upload initiation failed"):
            upload(local, "parent", "video.mp4")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_calls_correct_url():
    """delete() calls the Drive files DELETE endpoint with the correct file ID."""
    with patch("tools.drive.requests.delete") as mock_del:
        mock_del.return_value.ok = True
        mock_del.return_value.status_code = 204
        delete("file_to_delete")
    url = mock_del.call_args.args[0]
    assert "file_to_delete" in url


def test_delete_raises_on_http_error():
    """delete() raises RuntimeError on Drive API HTTP error."""
    with patch("tools.drive.requests.delete") as mock_del:
        mock_del.return_value.ok = False
        mock_del.return_value.status_code = 403
        with pytest.raises(RuntimeError, match="Drive delete failed"):
            delete("file_abc")


# ---------------------------------------------------------------------------
# folder_link
# ---------------------------------------------------------------------------

def test_folder_link_returns_correct_url():
    """folder_link() returns the canonical Drive folder web link."""
    assert folder_link("abc123") == "https://drive.google.com/drive/folders/abc123"


def test_folder_link_does_not_make_http_calls():
    """folder_link() is a pure function — it must not make any HTTP calls."""
    with patch("tools.drive.requests.get") as mock_get:
        folder_link("xyz789")
    mock_get.assert_not_called()
