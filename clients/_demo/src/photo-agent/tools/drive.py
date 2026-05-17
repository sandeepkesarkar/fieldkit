"""
Google Drive wrapper for the photo-video agent.

Thin wrapper around `gws drive` CLI subcommands. Each function builds a gws
command via subprocess, parses the JSON output, and returns a clean Python
value. Raises RuntimeError on non-zero gws exit or malformed JSON response.

No files are read or written by this module except via the gws download/upload
commands which operate on paths provided by the caller.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png"})


class DriveFolderNotFoundError(RuntimeError):
    """Raised by find_folder() when no matching folder exists in Drive."""

    def __init__(self, message: str, *, name: str, parent_id: str) -> None:
        super().__init__(message)
        self.name = name
        self.parent_id = parent_id


def _run_gws(cmd: list[str]) -> str:
    """Run a gws command and return stdout. Raises RuntimeError on non-zero exit."""
    logger.debug("Running gws command: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.debug("gws stderr: %s", result.stderr)
        raise RuntimeError(f"gws exited {result.returncode}")
    return result.stdout


def find_folder(name: str, parent_id: str) -> str:
    """Return the Drive folder ID matching name under parent_id.

    Raises DriveFolderNotFoundError if no matching folder is found.
    """
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
                "Skipping zero-byte file: id=%s name=%s in folder_id=%s",
                f.get("id"), f.get("name"), folder_id,
            )
            continue
        results.append({"id": f["id"], "name": f["name"]})

    results.sort(key=lambda x: x["name"])
    logger.info("list_photos: folder_id=%s count=%d", folder_id, len(results))
    return results


def download(file_id: str, output_path: Path) -> None:
    """Download a Drive file by ID to output_path."""
    if output_path.exists():
        logger.warning("download: output_path already exists and will be overwritten: %s", output_path)
    _run_gws([
        "gws", "drive", "files", "get",
        "--fileId", file_id,
        "--output", str(output_path),
    ])
    logger.info("download: file_id=%s output_path=%s", file_id, output_path)


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
    _run_gws(["gws", "drive", "files", "delete", "--fileId", file_id])
    logger.info("delete: file_id=%s", file_id)


def folder_link(folder_id: str) -> str:
    """Return the web link to a Drive folder."""
    return f"https://drive.google.com/drive/folders/{folder_id}"
