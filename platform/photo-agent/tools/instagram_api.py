"""
Instagram Graph API v25.0 wrapper for the FieldKit photo-video agent.

Handles Reels container creation, publish-status polling, and publishing
via the Instagram Content Publishing API.

Exception hierarchy:
  InstagramUploadError(RuntimeError) — all failures (container creation,
    polling ERROR/timeout, publish); retry eligible per caller's policy.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v25.0"

_POLL_INTERVAL_SECONDS = 5
_MAX_POLL_ATTEMPTS = 60


class InstagramUploadError(RuntimeError):
    """Raised on Instagram Graph API or network failures during Reels upload.

    The caller may retry according to the configured retry policy.
    """


def _create_container(access_token: str, ig_user_id: str, video_url: str) -> str:
    """Create a Reels media container. Returns the container id.

    Raises InstagramUploadError on HTTP error, network failure, or API error.
    """
    url = f"{_GRAPH_BASE}/{ig_user_id}/media"
    try:
        resp = requests.post(
            url,
            data={
                "video_url": video_url,
                "media_type": "REELS",
                "access_token": access_token,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Container creation request failed: {exc}") from exc

    try:
        data = resp.json()
    except Exception:
        data = {}

    error = data.get("error") if isinstance(data, dict) else None
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        msg = error.get("message", "") if isinstance(error, dict) else str(error)
        raise InstagramUploadError(f"Instagram API error {code} creating container: {msg}")

    if not resp.ok:
        raise InstagramUploadError(f"Container creation failed: HTTP {resp.status_code}")

    try:
        return data["id"]
    except (KeyError, TypeError) as exc:
        raise InstagramUploadError(
            f"Container creation response missing 'id' field: {exc} — response: {data!r}"
        ) from exc


def _poll_container_status(access_token: str, container_id: str) -> None:
    """Poll the container's status_code until FINISHED or ERROR.

    Raises InstagramUploadError on ERROR status, on any polling request
    failure, or if _MAX_POLL_ATTEMPTS is exceeded without reaching a
    terminal state.
    """
    url = f"{_GRAPH_BASE}/{container_id}"

    for attempt in range(1, _MAX_POLL_ATTEMPTS + 1):
        try:
            resp = requests.get(
                url,
                params={"fields": "status_code", "access_token": access_token},
                timeout=30,
            )
        except requests.exceptions.RequestException as exc:
            raise InstagramUploadError(f"Container status poll request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception:
            data = {}

        error = data.get("error") if isinstance(data, dict) else None
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            msg = error.get("message", "") if isinstance(error, dict) else str(error)
            raise InstagramUploadError(f"Instagram API error {code} polling container: {msg}")

        if not resp.ok:
            raise InstagramUploadError(f"Container status poll failed: HTTP {resp.status_code}")

        status_code = data.get("status_code")
        logger.info(
            "instagram container poll: container_id=%s attempt=%d status=%s",
            container_id,
            attempt,
            status_code,
        )

        if status_code == "FINISHED":
            return
        if status_code == "ERROR":
            raise InstagramUploadError(
                f"Container {container_id} failed processing: status_code=ERROR"
            )

        time.sleep(_POLL_INTERVAL_SECONDS)

    raise InstagramUploadError(
        f"Container {container_id} did not finish processing after "
        f"{_MAX_POLL_ATTEMPTS} attempts ({_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS}s)"
    )


def _publish_container(access_token: str, ig_user_id: str, container_id: str) -> str:
    """Publish a finished container. Returns the published post id.

    Raises InstagramUploadError on HTTP error, network failure, or API error.
    """
    url = f"{_GRAPH_BASE}/{ig_user_id}/media_publish"
    try:
        resp = requests.post(
            url,
            data={
                "creation_id": container_id,
                "access_token": access_token,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Publish request failed: {exc}") from exc

    try:
        data = resp.json()
    except Exception:
        data = {}

    error = data.get("error") if isinstance(data, dict) else None
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        msg = error.get("message", "") if isinstance(error, dict) else str(error)
        raise InstagramUploadError(f"Instagram API error {code} publishing container: {msg}")

    if not resp.ok:
        raise InstagramUploadError(f"Publish failed: HTTP {resp.status_code}")

    try:
        return data["id"]
    except (KeyError, TypeError) as exc:
        raise InstagramUploadError(
            f"Publish response missing 'id' field: {exc} — response: {data!r}"
        ) from exc


def upload_reel(access_token: str, ig_user_id: str, video_url: str) -> str:
    """Upload a Reel to Instagram from a publicly reachable video_url.

    Runs the three-step Content Publishing flow: create a media container,
    poll its status until processing finishes, then publish it. Returns the
    published post ID.

    Raises:
        InstagramUploadError — on container-creation failure, a polling
            ERROR status, a polling timeout, or a publish failure.
    """
    logger.info(
        "upload_reel: starting ig_user_id=%s video_url=%s", ig_user_id, video_url
    )

    container_id = _create_container(access_token, ig_user_id, video_url)
    logger.info("upload_reel: container created container_id=%s", container_id)

    _poll_container_status(access_token, container_id)
    logger.info("upload_reel: container finished processing container_id=%s", container_id)

    post_id = _publish_container(access_token, ig_user_id, container_id)
    logger.info("upload_reel: published post_id=%s", post_id)

    return post_id
