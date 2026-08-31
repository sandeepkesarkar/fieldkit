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
import requests

from tools.drive import (
    DriveFolderNotFoundError,
    create_folder,
    create_temporary_share_link,
    delete,
    download,
    extract_file_id,
    find_folder,
    folder_link,
    list_photos,
    revoke_share_link,
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


# ---------------------------------------------------------------------------
# T006: create_folder
# ---------------------------------------------------------------------------

def test_create_folder_returns_folder_id():
    """create_folder() POSTs to Drive v3 and returns the new folder ID."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "new_folder_id_abc"}
    with patch("tools.drive.requests.post", return_value=mock_resp) as mock_post:
        result = create_folder("e2e-test-20260620-143000", "parent_folder_id")
    assert result == "new_folder_id_abc"


def test_create_folder_sends_correct_mime_type():
    """create_folder() sets mimeType to application/vnd.google-apps.folder in the request body."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "folder_id"}
    with patch("tools.drive.requests.post", return_value=mock_resp) as mock_post:
        create_folder("e2e-test-20260620-143000", "parent_id")
    call_kwargs = mock_post.call_args
    body = json.loads(call_kwargs.kwargs.get("data") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["data"])
    assert body.get("mimeType") == "application/vnd.google-apps.folder"
    assert body.get("parents") == ["parent_id"]


def test_create_folder_raises_on_http_error():
    """create_folder() raises RuntimeError when Drive returns an HTTP error."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    with patch("tools.drive.requests.post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="403"):
            create_folder("test-folder", "parent_id")


def test_create_folder_raises_on_unsafe_name():
    """create_folder() raises ValueError for folder names containing unsafe characters."""
    with pytest.raises(ValueError, match="unsafe folder name"):
        create_folder("test/folder!", "parent_id")


# ---------------------------------------------------------------------------
# T006: upload with content_type parameter
# ---------------------------------------------------------------------------

def test_upload_uses_content_type_for_jpeg(tmp_path):
    """upload() uses content_type='image/jpeg' in the X-Upload-Content-Type header when specified."""
    local_file = tmp_path / "frame_001.jpg"
    local_file.write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)

    init_resp = MagicMock()
    init_resp.ok = True
    init_resp.headers = {"Location": "https://upload.googleapis.com/session/abc"}

    upload_resp = MagicMock()
    upload_resp.ok = True
    upload_resp.json.return_value = {"id": "file_id_xyz"}

    with patch("tools.drive.requests.post", return_value=init_resp) as mock_post, \
         patch("tools.drive.requests.put", return_value=upload_resp):
        upload(local_file, "parent_id", "frame_001.jpg", content_type="image/jpeg")

    headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args.args[1]
    assert headers["X-Upload-Content-Type"] == "image/jpeg"


def test_upload_defaults_to_video_mp4(tmp_path):
    """upload() defaults to 'video/mp4' for X-Upload-Content-Type when content_type is not given."""
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"\x00" * 64)

    init_resp = MagicMock()
    init_resp.ok = True
    init_resp.headers = {"Location": "https://upload.googleapis.com/session/def"}

    upload_resp = MagicMock()
    upload_resp.ok = True
    upload_resp.json.return_value = {"id": "file_id_zzz"}

    with patch("tools.drive.requests.post", return_value=init_resp) as mock_post, \
         patch("tools.drive.requests.put", return_value=upload_resp):
        upload(local_file, "parent_id", "video.mp4")

    headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args.args[1]
    assert headers["X-Upload-Content-Type"] == "video/mp4"


# ---------------------------------------------------------------------------
# Feature 005 — temporary share links for Instagram's video_url requirement
# ---------------------------------------------------------------------------

_FILE_ID = "drive_file_abc123"


@pytest.fixture
def share_env(monkeypatch):
    """DRIVE_ROOT_FOLDER_ID must be configured for share-link creation."""
    monkeypatch.setenv("DRIVE_ROOT_FOLDER_ID", "root_folder_1")


@pytest.fixture
def video_file(tmp_path):
    """A real on-disk file so upload()'s existence check passes."""
    p = tmp_path / "kitchen_remodel.mp4"
    p.write_bytes(b"fake mp4 bytes")
    return p


# --- create_temporary_share_link ---

def test_create_temporary_share_link_returns_fetchable_url(mocker, share_env, video_file):
    """Returns a URL containing the uploaded file's ID, usable as Instagram's video_url."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("requests.post", return_value=_ok_response({"id": "anyoneWithLink"}))

    url = create_temporary_share_link(video_file)
    assert url.startswith("https://")
    assert _FILE_ID in url


def test_create_temporary_share_link_sets_anyone_reader_permission(mocker, share_env, video_file):
    """Grants exactly an anyone/reader permission — Instagram fetches the URL unauthenticated."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    post = mocker.patch("requests.post", return_value=_ok_response({"id": "anyoneWithLink"}))

    create_temporary_share_link(video_file)
    args, kwargs = post.call_args
    assert f"/files/{_FILE_ID}/permissions" in args[0]
    body = json.loads(kwargs["data"])
    assert body == {"role": "reader", "type": "anyone"}


def test_create_temporary_share_link_uploads_into_the_root_folder(mocker, share_env, video_file):
    """The video is uploaded under DRIVE_ROOT_FOLDER_ID with video/mp4 content type."""
    up = mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("requests.post", return_value=_ok_response({"id": "anyoneWithLink"}))

    create_temporary_share_link(video_file)
    args, kwargs = up.call_args
    assert args[1] == "root_folder_1"
    assert kwargs.get("content_type", "video/mp4") == "video/mp4"


def test_create_temporary_share_link_raises_when_file_missing(share_env, tmp_path):
    """A missing local video raises rather than producing a dead link."""
    with pytest.raises(FileNotFoundError):
        create_temporary_share_link(tmp_path / "nope.mp4")


def test_create_temporary_share_link_raises_without_root_folder(monkeypatch, video_file):
    """An unconfigured DRIVE_ROOT_FOLDER_ID is a clear error, not a silent no-op."""
    monkeypatch.delenv("DRIVE_ROOT_FOLDER_ID", raising=False)
    with pytest.raises(RuntimeError, match="DRIVE_ROOT_FOLDER_ID"):
        create_temporary_share_link(video_file)


def test_create_temporary_share_link_raises_on_permission_http_error(mocker, share_env, video_file):
    """A failed permission call raises — never returns an unreachable URL."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("requests.post", return_value=_err_response(403))

    with pytest.raises(RuntimeError, match="share permission"):
        create_temporary_share_link(video_file)


def test_create_temporary_share_link_raises_on_network_error(mocker, share_env, video_file):
    """A network failure while sharing raises RuntimeError, not a bare requests error."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("requests.post", side_effect=requests.exceptions.ConnectionError("down"))

    with pytest.raises(RuntimeError, match="share permission"):
        create_temporary_share_link(video_file)


def test_create_temporary_share_link_propagates_upload_failure(mocker, share_env, video_file):
    """An upload failure is not swallowed into a bogus link."""
    mocker.patch("tools.drive.upload", side_effect=RuntimeError("Drive upload failed: HTTP 500"))
    with pytest.raises(RuntimeError, match="Drive upload failed"):
        create_temporary_share_link(video_file)


# --- revoke_share_link ---

def test_revoke_share_link_deletes_anyone_permissions(mocker):
    """revoke_share_link() removes every anyone-type permission on the file."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [
            {"id": "owner_perm", "type": "user"},
            {"id": "anyoneWithLink", "type": "anyone"},
        ]
    })
    delete_req = mocker.patch("requests.delete", return_value=_ok_response({}))

    revoke_share_link(_FILE_ID)
    assert delete_req.call_count == 1
    assert f"/files/{_FILE_ID}/permissions/anyoneWithLink" in delete_req.call_args.args[0]


def test_revoke_share_link_leaves_owner_permission_alone(mocker):
    """Only the public permission is revoked — the file itself stays owned and intact."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [{"id": "owner_perm", "type": "user"}]
    })
    delete_req = mocker.patch("requests.delete", return_value=_ok_response({}))

    revoke_share_link(_FILE_ID)
    delete_req.assert_not_called()


def test_revoke_share_link_accepts_204(mocker):
    """Drive's 204 No Content is the success response for a permission delete."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [{"id": "anyoneWithLink", "type": "anyone"}]
    })
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 204
    mocker.patch("requests.delete", return_value=resp)

    revoke_share_link(_FILE_ID)  # must not raise


def test_revoke_share_link_raises_on_http_error(mocker):
    """A failed revoke raises rather than silently leaving the video publicly reachable."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [{"id": "anyoneWithLink", "type": "anyone"}]
    })
    mocker.patch("requests.delete", return_value=_err_response(500))

    with pytest.raises(RuntimeError, match="revoke"):
        revoke_share_link(_FILE_ID)


def test_revoke_share_link_raises_on_network_error(mocker):
    """A network failure while revoking raises RuntimeError, not a bare requests error."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [{"id": "anyoneWithLink", "type": "anyone"}]
    })
    mocker.patch("requests.delete", side_effect=requests.exceptions.ConnectionError("down"))

    with pytest.raises(RuntimeError, match="revoke"):
        revoke_share_link(_FILE_ID)


def test_revoke_share_link_propagates_list_failure(mocker):
    """A failure listing permissions is surfaced, not treated as 'nothing to revoke'."""
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("tools.drive._drive_get", side_effect=RuntimeError("Drive GET failed: HTTP 500"))
    with pytest.raises(RuntimeError, match="Drive GET failed"):
        revoke_share_link(_FILE_ID)


# --- extract_file_id ---

def test_extract_file_id_round_trips_with_create(mocker, share_env, video_file):
    """The URL create_temporary_share_link() returns yields its file id back."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="tok")
    mocker.patch("requests.post", return_value=_ok_response({"id": "anyoneWithLink"}))

    url = create_temporary_share_link(video_file)
    assert extract_file_id(url) == _FILE_ID


def test_extract_file_id_raises_on_unrecognized_url():
    """A URL with no id parameter is an error, not a silently wrong file id."""
    with pytest.raises(ValueError, match="file id"):
        extract_file_id("https://example.com/not-a-drive-link")


# --- Import-time requests availability (shared by both helpers) ---

def test_share_helpers_never_log_the_access_token(mocker, share_env, video_file, caplog):
    """Neither helper writes the Drive access token into log output."""
    mocker.patch("tools.drive.upload", return_value=_FILE_ID)
    mocker.patch("tools.drive._get_access_token", return_value="super_secret_drive_token")
    mocker.patch("requests.post", return_value=_ok_response({"id": "anyoneWithLink"}))
    mocker.patch("tools.drive._drive_get", return_value={
        "permissions": [{"id": "anyoneWithLink", "type": "anyone"}]
    })
    mocker.patch("requests.delete", return_value=_ok_response({}))

    with caplog.at_level("DEBUG"):
        url = create_temporary_share_link(video_file)
        revoke_share_link(extract_file_id(url))
    assert "super_secret_drive_token" not in caplog.text
