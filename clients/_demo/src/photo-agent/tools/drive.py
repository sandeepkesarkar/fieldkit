"""
Google Drive wrapper for the photo-video agent.

Most operations are thin wrappers around `gws drive` CLI subcommands.
Downloads call the Drive REST API directly via requests because gws 0.22.5
does not support binary file downloads (files.get?alt=media is rejected by
the gws CLI). _get_access_token() exchanges the gws refresh token for a
short-lived access token using the standard OAuth2 token endpoint.

Raises RuntimeError on non-zero gws exit, HTTP error, or malformed JSON.
"""

import json
import logging
import re
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png"})


class DriveFolderNotFoundError(RuntimeError):
    """Raised by find_folder() when no matching folder exists in Drive."""

    def __init__(self, message: str, *, name: str, parent_id: str) -> None:
        super().__init__(message)
        self.name = name
        self.parent_id = parent_id


def _get_access_token() -> str:
    """Exchange the gws refresh token for a fresh Drive access token.

    Calls `gws auth export` to read stored credentials, then POSTs to the
    Google OAuth2 token endpoint. Raises RuntimeError on any failure.
    """
    result = subprocess.run(
        ["gws", "auth", "export"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gws auth export failed: {result.stderr.strip()}")
    raw = result.stdout
    json_start = raw.find("{")
    if json_start == -1:
        raise RuntimeError("gws auth export returned no JSON")
    try:
        creds = json.loads(raw[json_start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gws auth export returned invalid JSON: {exc}") from exc
    try:
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
        refresh_token = creds["refresh_token"]
    except KeyError as exc:
        raise RuntimeError(f"gws auth export response missing expected key: {exc}") from exc
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
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


def _run_gws(cmd: list[str]) -> str:
    """Run a gws command and return stdout. Raises RuntimeError on non-zero exit."""
    logger.debug("Running gws command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.debug("gws stderr: %s", result.stderr)
        raise RuntimeError(f"gws exited {result.returncode}")
    return result.stdout


_SAFE_FOLDER_NAME_RE = re.compile(r'^[A-Za-z0-9_\- ]+$')


def find_folder(name: str, parent_id: str) -> str:
    """Return the Drive folder ID matching name under parent_id.

    Raises DriveFolderNotFoundError if no matching folder is found.
    Raises ValueError if name contains characters that would corrupt the Drive query.
    """
    if not _SAFE_FOLDER_NAME_RE.match(name):
        raise ValueError(f"find_folder: unsafe folder name: {name!r}")
    params = json.dumps({
        "q": (
            f'name="{name}" and "{parent_id}" in parents'
            ' and mimeType="application/vnd.google-apps.folder"'
            " and trashed=false"
        ),
        "fields": "files(id)",
    })
    output = _run_gws(["gws", "drive", "files", "list", "--params", params])
    try:
        files = json.loads(output).get("files", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws returned invalid JSON: {e}") from e
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
    except KeyError as e:
        raise RuntimeError(f"gws response missing expected key: {e}") from e
    logger.info("find_folder: name=%s folder_id=%s", name, folder_id)
    return folder_id


def list_photos(folder_id: str) -> list[dict]:
    """Return image files in the Drive folder, sorted by name, zero-byte files excluded.

    Each entry is {"id": ..., "name": ...}. Only image/jpeg and image/png are returned.
    """
    params = json.dumps({
        "q": f'"{folder_id}" in parents and trashed=false',
        "fields": "files(id,name,mimeType,size)",
    })
    output = _run_gws(["gws", "drive", "files", "list", "--params", params])
    try:
        files = json.loads(output).get("files", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws returned invalid JSON: {e}") from e

    results = []
    for f in files:
        if f.get("mimeType") not in _PHOTO_MIME_TYPES:
            continue
        # Drive API returns size as a string; use "0" as default to keep types consistent.
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
    """Download a Drive file by ID to output_path via the Drive REST API.

    Uses requests directly because gws 0.22.5 does not support binary downloads
    via the CLI (files.get?alt=media is rejected by the gws CLI layer).
    """
    if output_path.exists():
        logger.warning("download: file_id=%s — output already exists and will be overwritten", file_id)
    access_token = _get_access_token()
    try:
        resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
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


def upload(local_path: Path, parent_id: str, name: str) -> str:
    """Upload local_path to Drive under parent_id. Returns the new Drive file ID."""
    if not local_path.exists():
        raise FileNotFoundError(f"upload: local file not found: {local_path}")
    output = _run_gws([
        "gws", "drive", "+upload", str(local_path),
        "--parent", parent_id,
        "--name", name,
    ])
    try:
        file_id = json.loads(output)["id"]
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gws returned invalid JSON: {e}") from e
    except KeyError as e:
        raise RuntimeError(f"gws response missing expected key: {e}") from e
    logger.info("upload: name=%s file_id=%s", name, file_id)
    return file_id


def delete(file_id: str) -> None:
    """Delete a Drive file by ID."""
    _run_gws(["gws", "drive", "files", "delete", "--params", json.dumps({"fileId": file_id})])
    logger.info("delete: file_id=%s", file_id)


def folder_link(folder_id: str) -> str:
    """Return the web link to a Drive folder."""
    return f"https://drive.google.com/drive/folders/{folder_id}"
