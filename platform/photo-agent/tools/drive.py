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

Feature 005 adds create_temporary_share_link() / revoke_share_link() /
delete_temporary_share(); the first is the only function here that makes a file
publicly reachable. See their docstrings for why
that exposure is needed and how it is bounded.
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


# ---------------------------------------------------------------------------
# Temporary public share links (Feature 005 — Instagram video_url)
# ---------------------------------------------------------------------------
#
# Instagram's media-container endpoint does not accept uploaded bytes: it takes a
# video_url that Instagram's own servers fetch. The Mac Mini has no public web
# server, so the approved video is briefly published through Drive — the
# framework's already-sanctioned host for client-approved media — as a disposable
# COPY uploaded fresh for each attempt, which is permanently deleted again once
# Instagram has ingested it (delete_temporary_share).
#
# The exposure this creates is deliberately bounded: it covers ONE
# already-approved, already-metadata-stripped video (the same asset the Facebook
# upload posts — never a re-processed copy), the link is created immediately
# before the container call, and upload_instagram.py deletes the copy on every exit
# path, success or failure. Nothing else in the pipeline uses these functions.
#
# The permission granted is {"role": "reader", "type": "anyone"} with
# allowFileDiscovery left unset, which means link-access only: the file is not
# search-discoverable, and someone would have to hold the URL to reach it. That is
# a meaningful limit, but it is still UNAUTHENTICATED access to a client's video,
# so the real bound has to be time, not obscurity.
#
# Why that bound is not an expiring permission: the Drive API's
# permissions.expirationTime field cannot be used here. It is restricted to user
# and group permissions — an "anyone" permission cannot carry one — so there is no
# server-side way to make an anonymous link self-destruct. The bound is therefore
# enforced by FieldKit instead, and enforced BEFORE the exposure exists rather than
# after: create_temporary_share_link() hands the caller the file id through
# on_file_id the moment the file is uploaded and before any permission is granted,
# so a durable cleanup obligation is recorded while the file is still private. See
# upload_instagram.py's _create_share_link(); the maximum exposure becomes one
# attempt plus one cron tick, even if this process dies at the worst moment.


def create_temporary_share_link(video_path, on_file_id=None) -> str:
    """Upload video_path to Drive, make it link-readable, and return a fetchable URL.

    The returned URL is suitable for Instagram's video_url parameter — reachable
    without credentials. The caller MUST pair this with delete_temporary_share() on
    every exit path (revoke_share_link() alone is not enough — see there), and should get the file id from on_file_id below rather than from the
    returned URL — only the hook fires on the paths where this function raises.

    on_file_id, if given, is called with the new file's id AFTER the upload and
    BEFORE the public permission is granted. It exists to close a window that the
    return value alone cannot: the file id is knowable before the file is public,
    but a caller that only learns it from the returned URL learns nothing if the
    permission POST succeeds server-side and its response is then lost to a timeout
    or a crash. The permission would exist, this function would raise, and the
    caller would hold no id to clean up — an untracked public link, forever. A caller
    that records its cleanup obligation in on_file_id can always delete the copy,
    including one that never actually became public — or whose permission Drive only
    applies later: a deleted file cannot become public.

    A raising on_file_id aborts before the file is ever shared, deliberately: a
    caller that cannot record the obligation must not be handed the exposure.

    Raises:
        FileNotFoundError — video_path does not exist.
        RuntimeError — DRIVE_ROOT_FOLDER_ID unset, or the upload/permission call failed.
            Never returns a URL that isn't actually shared.
    """
    import os
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"create_temporary_share_link: local file not found: {video_path}")

    parent_id = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
    if not parent_id:
        raise RuntimeError(
            "DRIVE_ROOT_FOLDER_ID is not set — add it to your client .env file"
        )

    file_id = upload(video_path, parent_id, video_path.name, content_type="video/mp4")

    # The file exists but is still private. This is the only moment at which the
    # caller can be told the id with a guarantee that nothing public has been
    # created yet, so the obligation is registered here rather than on return.
    if on_file_id is not None:
        on_file_id(file_id)

    access_token = _get_access_token()
    try:
        resp = requests.post(
            f"{_DRIVE_FILES_URL}/{file_id}/permissions",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=json.dumps({"role": "reader", "type": "anyone"}),
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive share permission request failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"Drive share permission failed: HTTP {resp.status_code}")

    logger.info("create_temporary_share_link: file_id=%s", file_id)
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def revoke_share_link(file_id: str) -> None:
    """Remove every public ("anyone") permission from a Drive file.

    Deliberately loud rather than best-effort: a silently failed revoke leaves a
    client's video publicly reachable indefinitely, which is exactly the state this
    feature promises not to leave behind. The caller logs and carries on, but it has
    to know it happened.

    Raises RuntimeError if the permissions cannot be listed or a delete fails.
    """
    data = _drive_get(f"{_DRIVE_FILES_URL}/{file_id}/permissions", {
        "fields": "permissions(id,type)",
    })
    access_token = _get_access_token()
    for permission in data.get("permissions", []):
        if permission.get("type") != "anyone":
            continue
        permission_id = permission.get("id")
        try:
            resp = requests.delete(
                f"{_DRIVE_FILES_URL}/{file_id}/permissions/{permission_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Drive revoke share link request failed for file {file_id}: {exc}"
            ) from exc
        # 204 No Content is the success response for a permission delete.
        if not resp.ok and resp.status_code != 204:
            raise RuntimeError(
                f"Drive revoke share link failed for file {file_id}: HTTP {resp.status_code}"
            )
        logger.info("revoke_share_link: file_id=%s permission_id=%s", file_id, permission_id)


_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class TemporaryShareRefused(RuntimeError):
    """delete_temporary_share() refused: the file does not look like FieldKit's temporary copy.

    A RuntimeError so every existing caller already keeps the cleanup obligation and retries
    — but distinct, so callers can tell the admin that nothing was changed and why, rather
    than reporting a transient Drive failure.
    """


def _check_temporary_copy(file_id: str, meta: dict, provenance: dict) -> None:
    """Raise TemporaryShareRefused unless `meta` describes the temporary copy we created.

    provenance (see tools/share_cleanup.provenance_for):
      root_folder_id      — the configured DRIVE_ROOT_FOLDER_ID. The temporary copy is
                            uploaded DIRECTLY into it; the client's approved video lives one
                            level down, in the project folder (process_photos.py).
      recorded_parent_id  — the parent recorded when the copy was created, or None for an
                            entry written before provenance was recorded.
      expected_name       — the file name recorded at creation, or None (legacy entry).
      protected_ids       — ids that must never be deleted (e.g. the approved video's id).

    Every check here refuses rather than guesses: a refusal keeps the obligation and alerts,
    which a human can resolve; a wrong deletion of client content cannot be undone.
    """
    root = provenance.get("root_folder_id")
    reason = None
    if not root:
        reason = "DRIVE_ROOT_FOLDER_ID is not configured, so its parent cannot be verified"
    elif file_id == root:
        reason = "it is the Drive root folder itself"
    elif file_id in set(provenance.get("protected_ids") or ()):
        reason = "it is a protected file (the approved video)"
    elif provenance.get("recorded_parent_id") not in (None, root):
        reason = "the folder recorded at creation is not the configured Drive root folder"
    elif meta.get("mimeType") == _FOLDER_MIME_TYPE:
        reason = "it is a folder"
    elif not str(meta.get("mimeType", "")).startswith("video/"):
        reason = "it is not a video"
    elif root not in (meta.get("parents") or []):
        reason = "it is not directly inside the configured Drive root folder"
    elif provenance.get("expected_name") not in (None, meta.get("name")):
        reason = "its name does not match the copy FieldKit uploaded"
    if reason:
        raise TemporaryShareRefused(
            f"refused to delete Drive file {file_id}: {reason}"
        )


def delete_temporary_share(file_id: str, *, provenance: dict) -> None:
    """End a temporary share for good: verify, revoke, permanently delete, confirm it gone.

    ONLY for the disposable copy create_temporary_share_link() uploaded for Instagram to
    fetch — never for a client's approved video or folder. That copy is uploaded fresh on
    every attempt and nothing else refers to it, so deleting it loses nothing.

    Why deletion, and not just revoke_share_link(): a revoke proves only that no public
    permission is visible AT THAT MOMENT. A permission POST that Drive accepted but whose
    response was lost (a client-side timeout) is not proven to have finished acting, so it
    could still be applied after the revoke has looked, seen nothing, and moved on. A file
    that no longer exists cannot become public whenever such a grant lands, so a confirmed
    permanent deletion is the definitive end of the exposure, and the only thing callers
    may retire a cleanup obligation on.

    Why verify first: the file id comes from instagram_state.json. If that file were ever
    corrupted or hand-edited to name a client's real file, an unconditional permanent
    delete would turn cleanup into data loss. provenance is keyword-only and REQUIRED, so a
    caller cannot skip the check by omission.

    Steps:
      0. files.get(id,name,mimeType,parents,trashed), then _check_temporary_copy(). A 404
         here means it is already gone — the answer every later step is waiting for — so it
         returns. A refusal raises TemporaryShareRefused having CHANGED NOTHING: no revoke,
         no delete.
      1. revoke_share_link() — best effort, the fast first step. Its failure is logged,
         not raised: step 2 ends the exposure regardless.
      2. files.delete — which "Permanently deletes a file owned by the user without moving
         it to the trash". Never files.update(trashed=true): per Google, "other users can
         still access the file in the owner's trash until it's permanently deleted".
         https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete
         https://developers.google.com/workspace/drive/api/guides/delete
         A 404 here means it is already gone; that is still confirmed in step 3.
      3. files.get must answer 404. Anything else — the file still listed (including
         trashed), or no definitive answer — raises.

    Raises TemporaryShareRefused (a RuntimeError) if the file fails verification, and
    RuntimeError unless the file is confirmed gone. Callers keep the obligation either way.
    """
    access_token = _get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        meta_resp = requests.get(
            f"{_DRIVE_FILES_URL}/{file_id}",
            headers=headers,
            params={"fields": "id,name,mimeType,parents,trashed"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive could not read file {file_id} to verify it: {exc}") from exc
    if meta_resp.status_code == 404:
        logger.info("delete_temporary_share: file_id=%s already gone", file_id)
        return
    if not meta_resp.ok:
        raise RuntimeError(
            f"Drive could not read file {file_id} to verify it: HTTP {meta_resp.status_code}"
        )
    try:
        meta = meta_resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Drive returned unreadable metadata for file {file_id}") from exc
    _check_temporary_copy(file_id, meta, provenance)

    try:
        revoke_share_link(file_id)
    except RuntimeError as exc:
        logger.warning(
            "delete_temporary_share: revoke failed, deleting anyway: file_id=%s error=%s",
            file_id, exc,
        )
    try:
        resp = requests.delete(f"{_DRIVE_FILES_URL}/{file_id}", headers=headers, timeout=30)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Drive delete request failed for file {file_id}: {exc}") from exc
    if not resp.ok and resp.status_code not in (204, 404):
        raise RuntimeError(f"Drive delete failed for file {file_id}: HTTP {resp.status_code}")
    try:
        check = requests.get(
            f"{_DRIVE_FILES_URL}/{file_id}",
            headers=headers,
            params={"fields": "id,trashed"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Drive could not confirm deletion of file {file_id}: {exc}"
        ) from exc
    if check.status_code == 404:
        logger.info("delete_temporary_share: file_id=%s deleted and confirmed gone", file_id)
        return
    if check.ok:
        raise RuntimeError(f"Drive file {file_id} still exists after delete")
    raise RuntimeError(
        f"Drive could not confirm deletion of file {file_id}: HTTP {check.status_code}"
    )


def extract_file_id(share_link: str) -> str:
    """Return the Drive file id embedded in a create_temporary_share_link() URL.

    For a caller that holds nothing but the URL. A caller that is CREATING the link
    should take the id from create_temporary_share_link()'s on_file_id hook instead:
    this function can only run once a URL has been returned, and the case that matters
    most — the share call raising after the permission was actually created — never
    returns one. upload_instagram.py uses the hook for exactly that reason.

    Kept because the seam is real: create_temporary_share_link() returns a URL (that's
    what Instagram needs) while revoke_share_link() takes a file id, and crossing that
    seam deserves a documented function rather than ad-hoc string slicing at a call site.

    Raises ValueError if the URL carries no id parameter.
    """
    from urllib.parse import parse_qs, urlparse
    file_ids = parse_qs(urlparse(share_link).query).get("id", [])
    if not file_ids or not file_ids[0]:
        raise ValueError(f"extract_file_id: no Drive file id in URL: {share_link!r}")
    return file_ids[0]
