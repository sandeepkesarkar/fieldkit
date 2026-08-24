"""
Google Drive wrapper for the photo-video agent.

All Drive operations call the Drive REST API directly via requests. No gws CLI
calls are made at runtime — gws is only needed once to export credentials.

Credentials are read from ~/.config/gws/user_credentials.json (ADC format:
client_id, client_secret, refresh_token). This file is created once via:
    gws auth export 2>/dev/null | python3 -c "
        import sys, json; raw = sys.stdin.read()
        print(json.dumps(json.loads(raw[raw.find('{'):]), indent=2))
    " > ~/.config/gws/user_credentials.json

_get_access_token() exchanges the stored refresh_token for a short-lived
access_token via the OAuth2 token endpoint on every call.

Raises RuntimeError on HTTP error, missing credentials, or malformed JSON.
"""

import json
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
_SAFE_FOLDER_NAME_RE = re.compile(r'^[A-Za-z0-9_\- ]+$')
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEFAULT_CREDS_FILE = Path("~/.config/gws/user_credentials.json").expanduser()


class DriveFolderNotFoundError(RuntimeError):
    """Raised by find_folder() when no matching folder exists in Drive."""

    def __init__(self, message: str, *, name: str, parent_id: str) -> None:
        super().__init__(message)
        self.name = name
        self.parent_id = parent_id


def _load_credentials() -> dict:
    """Load OAuth credentials from the user credentials file (ADC format).

    Reads from ~/.config/gws/user_credentials.json by default. Override the
    path with the GOOGLE_USER_CREDENTIALS_FILE environment variable.
    """
    import os
    creds_path_raw = os.environ.get("GOOGLE_USER_CREDENTIALS_FILE", "")
    creds_path = Path(creds_path_raw).expanduser() if creds_path_raw else _DEFAULT_CREDS_FILE
    if not creds_path.exists():
        raise RuntimeError(
            f"Drive credentials file not found: {creds_path}\n"
            "Run: gws auth login -s drive,gmail && gws auth export 2>/dev/null | "
            "python3 -c \"import sys,json; raw=sys.stdin.read(); "
            "print(json.dumps(json.loads(raw[raw.find('{'):]), indent=2))\" "
            f"> {creds_path}"
        )
    try:
        return json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to read credentials file {creds_path}: {exc}") from exc


def _get_access_token() -> str:
    """Exchange the stored refresh token for a fresh Drive access token.

    Reads credentials from the user credentials file — no gws process is spawned.
    Raises RuntimeError on any failure.

    Deliberately reused as the Gmail-send credential too: check_approval.py's
    _send_approval_email() calls this directly rather than minting a separate
    Gmail token. The underlying refresh token must therefore carry both the
    drive and gmail.send scopes — see scripts/setup_drive_auth.py's _SCOPE
    (issue #35). Do not assume this token is Drive-only.
    """
    creds = _load_credentials()
    try:
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
        refresh_token = creds["refresh_token"]
    except KeyError as exc:
        raise RuntimeError(f"Credentials file missing expected key: {exc}") from exc
    try:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Token refresh request failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"Token refresh failed: HTTP {resp.status_code}")
    try:
        return resp.json()["access_token"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Token refresh response missing access_token: {exc}") from exc


def _drive_get(endpoint: str, params: dict) -> dict:
    """GET request to the Drive v3 API. Raises RuntimeError on failure."""
    access_token = _get_access_token()
    try:
        resp = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive GET request failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"Drive GET failed: HTTP {resp.status_code}")
    return resp.json()


def create_folder(name: str, parent_id: str) -> str:
    """Create a new folder under parent_id and return its Drive file ID.

    Raises ValueError for unsafe folder names. Raises RuntimeError on HTTP error.
    """
    if not _SAFE_FOLDER_NAME_RE.match(name):
        raise ValueError(f"create_folder: unsafe folder name: {name!r}")
    access_token = _get_access_token()
    metadata = json.dumps({
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    })
    try:
        resp = requests.post(
            _DRIVE_FILES_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=metadata,
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive create_folder request failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"Drive create_folder failed: HTTP {resp.status_code}")
    try:
        folder_id = resp.json()["id"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Drive create_folder response missing id: {exc}") from exc
    logger.info("create_folder: name=%s folder_id=%s", name, folder_id)
    return folder_id


def find_folder(name: str, parent_id: str) -> str:
    """Return the Drive folder ID matching name under parent_id.

    Raises DriveFolderNotFoundError if no matching folder is found.
    Raises ValueError if name contains characters that would corrupt the Drive query.
    """
    if not _SAFE_FOLDER_NAME_RE.match(name):
        raise ValueError(f"find_folder: unsafe folder name: {name!r}")
    data = _drive_get(_DRIVE_FILES_URL, {
        "q": (
            f'name="{name}" and "{parent_id}" in parents'
            ' and mimeType="application/vnd.google-apps.folder"'
            " and trashed=false"
        ),
        "fields": "files(id)",
    })
    files = data.get("files", [])
    if not files:
        raise DriveFolderNotFoundError(
            f"Drive folder not found: {name!r} under parent {parent_id!r}",
            name=name,
            parent_id=parent_id,
        )
    if len(files) > 1:
        logger.warning(
            "find_folder: %d folders named %r under parent %s — using first",
            len(files), name, parent_id,
        )
    try:
        folder_id = files[0]["id"]
    except KeyError as exc:
        raise RuntimeError(f"Drive response missing expected key: {exc}") from exc
    logger.info("find_folder: name=%s folder_id=%s", name, folder_id)
    return folder_id


def list_photos(folder_id: str) -> list[dict]:
    """Return image files in the Drive folder, sorted by name, zero-byte files excluded.

    Each entry is {"id": ..., "name": ...}. Only image/jpeg and image/png are returned.
    """
    data = _drive_get(_DRIVE_FILES_URL, {
        "q": f'"{folder_id}" in parents and trashed=false',
        "fields": "files(id,name,mimeType,size)",
    })
    files = data.get("files", [])

    results = []
    for f in files:
        if f.get("mimeType") not in _PHOTO_MIME_TYPES:
            continue
        if int(f.get("size", "0")) == 0:
            logger.warning(
                "Skipping zero-byte file: id=%s in folder_id=%s",
                f.get("id"), folder_id,
            )
            continue
        results.append({"id": f["id"], "name": f["name"]})

    results.sort(key=lambda x: x["name"])
    logger.info("list_photos: folder_id=%s count=%d", folder_id, len(results))
    return results


def download(file_id: str, output_path: Path) -> None:
    """Download a Drive file by ID to output_path via the Drive REST API."""
    if output_path.exists():
        logger.warning("download: file_id=%s — output already exists and will be overwritten", file_id)
    access_token = _get_access_token()
    try:
        resp = requests.get(
            f"{_DRIVE_FILES_URL}/{file_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"alt": "media"},
            timeout=60,
            stream=True,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive download request failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"Drive download failed: HTTP {resp.status_code}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    logger.info("download: file_id=%s bytes=%d", file_id, output_path.stat().st_size)


def upload(local_path: Path, parent_id: str, name: str, content_type: str = "video/mp4") -> str:
    """Upload local_path to Drive under parent_id using resumable upload.

    Returns the new Drive file ID. Resumable upload handles files of any size.
    content_type defaults to "video/mp4"; pass "image/jpeg" for JPEG frame uploads.
    """
    if not local_path.exists():
        raise FileNotFoundError(f"upload: local file not found: {local_path}")

    file_size = local_path.stat().st_size
    access_token = _get_access_token()
    metadata = json.dumps({"name": name, "parents": [parent_id]})

    # Initiate the resumable upload session.
    try:
        init_resp = requests.post(
            f"{_DRIVE_UPLOAD_URL}?uploadType=resumable",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(file_size),
            },
            data=metadata,
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive upload initiation failed: {exc}") from exc
    if not init_resp.ok:
        raise RuntimeError(f"Drive upload initiation failed: HTTP {init_resp.status_code}")

    session_uri = init_resp.headers.get("Location")
    if not session_uri:
        raise RuntimeError("Drive upload initiation response missing Location header")

    # Upload the file content.
    try:
        with open(local_path, "rb") as f:
            upload_resp = requests.put(
                session_uri,
                headers={
                    "Content-Length": str(file_size),
                },
                data=f,
                timeout=600,
            )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive upload content transfer failed: {exc}") from exc
    if not upload_resp.ok:
        raise RuntimeError(f"Drive upload failed: HTTP {upload_resp.status_code}")

    try:
        file_id = upload_resp.json()["id"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Drive upload response missing file id: {exc}") from exc
    logger.info("upload: name=%s file_id=%s", name, file_id)
    return file_id


def delete(file_id: str) -> None:
    """Delete a Drive file by ID."""
    access_token = _get_access_token()
    try:
        resp = requests.delete(
            f"{_DRIVE_FILES_URL}/{file_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive delete request failed: {exc}") from exc
    # 204 No Content is the success response for delete
    if not resp.ok and resp.status_code != 204:
        raise RuntimeError(f"Drive delete failed: HTTP {resp.status_code}")
    logger.info("delete: file_id=%s", file_id)


def folder_link(folder_id: str) -> str:
    """Return the web link to a Drive folder."""
    return f"https://drive.google.com/drive/folders/{folder_id}"
