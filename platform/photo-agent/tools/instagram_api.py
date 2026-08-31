"""
Instagram Graph API v25.0 wrapper for the FieldKit photo-video agent.

Handles Instagram professional-account discovery, Reels container creation,
publish-status polling, and publishing via the Instagram Content Publishing API.

Authentication reuses the Facebook Page access token from Feature 003
(FB_PAGE_ACCESS_TOKEN) — Instagram content publishing for professional
accounts is served by graph.facebook.com through the linked Page, so this
feature introduces no new token or OAuth flow of its own.

The three publish steps are exposed as independently callable public functions
(create_media_container / get_container_status / publish_container) rather than
only behind upload_reel(): upload_instagram.py drives them one at a time so it
can persist the container id, log each transition, and revoke the temporary
Drive share link at exactly the right moment.

Note that publish_container() returns the Graph API MEDIA ID, which is not a
shareable URL and cannot be turned into one by string formatting — Instagram's
public URLs use an unrelated permalink shortcode. get_media_permalink() fetches
the real, working link; anything shown to a human must come from there.

Exception hierarchy (mirrors facebook_api.py's):
  InstagramTokenError(RuntimeError)          — token invalid/expired (Graph API
    error code 190); skip retries, the token must be renewed via generate_auth_link.py
  InstagramAccountNotFoundError(RuntimeError) — the Facebook Page has no linked
    Instagram professional account; setup problem, not a transient failure
  InstagramUploadError(RuntimeError)          — all other failures (container
    creation, polling ERROR/timeout, publish); retry eligible per caller's policy
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v25.0"

_POLL_INTERVAL_SECONDS = 5
_MAX_POLL_ATTEMPTS = 60


class InstagramTokenError(RuntimeError):
    """Raised when the access token is invalid or expired (Graph API error code 190).

    The caller should not retry — the token must be renewed via generate_auth_link.py.
    Mirrors facebook_api.FacebookTokenError; the two wrap the SAME underlying Page
    token, so a 190 from either platform means the same reconnect is needed.
    """


class InstagramAccountNotFoundError(RuntimeError):
    """Raised when a Facebook Page has no linked Instagram professional account.

    A setup problem (nothing linked, or the linked account is PERSONAL rather than
    BUSINESS/CREATOR), not a transient API failure — retrying cannot fix it.
    """


class InstagramUploadError(RuntimeError):
    """Raised on Instagram Graph API or network failures during Reels upload.

    The caller may retry according to the configured retry policy.
    """


_ACCOUNT_TYPES = frozenset({"BUSINESS", "CREATOR"})


def _json_or_empty(resp) -> dict:
    """Return the response's parsed JSON body, or {} if it isn't valid JSON."""
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _raise_for_api_error(data: dict, context: str) -> None:
    """Raise on a Graph API error payload, distinguishing token expiry from everything else.

    The Graph API can return a 2xx HTTP status with an error body (notably for token
    expiry), so this must be checked before resp.ok. Error code 190 becomes an
    InstagramTokenError — retrying it is pointless and would just burn the job's
    attempt budget — exactly as facebook_api.upload_video() treats the same code.
    """
    error = data.get("error")
    if not error:
        return
    code = error.get("code") if isinstance(error, dict) else None
    msg = error.get("message", "") if isinstance(error, dict) else str(error)
    if code == 190:
        raise InstagramTokenError(f"Instagram token invalid/expired {context}: {msg}")
    raise InstagramUploadError(f"Instagram API error {code} {context}: {msg}")


def create_media_container(access_token: str, ig_user_id: str, video_url: str) -> str:
    """Create a Reels media container. Returns the container id.

    video_url must be publicly reachable — Instagram fetches the video itself rather
    than accepting uploaded bytes. See drive.create_temporary_share_link().

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on any other API error, HTTP error, or network failure.
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

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "creating container")

    if not resp.ok:
        raise InstagramUploadError(f"Container creation failed: HTTP {resp.status_code}")

    try:
        return data["id"]
    except (KeyError, TypeError) as exc:
        raise InstagramUploadError(
            f"Container creation response missing 'id' field: {exc} — response: {data!r}"
        ) from exc


def get_container_status(access_token: str, container_id: str) -> str:
    """Return the container's current status_code from a SINGLE Graph API call.

    One call, no sleeping — the bounded polling loop lives in
    wait_for_container(). Kept separate so callers can drive (and tests can
    assert on) one poll at a time.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on any other API error, HTTP error, or network failure.
    """
    url = f"{_GRAPH_BASE}/{container_id}"
    try:
        resp = requests.get(
            url,
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Container status poll request failed: {exc}") from exc

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "polling container")

    if not resp.ok:
        raise InstagramUploadError(f"Container status poll failed: HTTP {resp.status_code}")

    return data.get("status_code")


def wait_for_container(access_token: str, container_id: str) -> None:
    """Poll get_container_status() until FINISHED, capped at _MAX_POLL_ATTEMPTS.

    Instagram ingests video asynchronously: the container must finish transcoding
    before it can be published. A container that never reaches a terminal state is
    the "stuck container" edge case from spec.md — bounded here at
    _MAX_POLL_ATTEMPTS x _POLL_INTERVAL_SECONDS (300s) and surfaced as an ordinary
    retryable InstagramUploadError rather than an unbounded wait inside a cron tick.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on ERROR status, any polling failure, or the poll cap.
    """
    for attempt in range(1, _MAX_POLL_ATTEMPTS + 1):
        status_code = get_container_status(access_token, container_id)
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


def get_media_permalink(access_token: str, media_id: str) -> str:
    """Return the public permalink URL for a published media object.

    The Graph API media ID that publish_container() returns is an internal identifier.
    Instagram's public URLs (https://www.instagram.com/reel/<shortcode>/) are keyed by an
    unrelated shortcode, so a link built by interpolating the media ID into a /p/ or /reel/
    URL does NOT resolve to the post. The permalink has to be read back from the API, which
    is what this does.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on any other API error, HTTP error, network failure, or a
            response with no permalink field.
    """
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": access_token},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Permalink lookup request failed: {exc}") from exc

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "fetching permalink")

    if not resp.ok:
        raise InstagramUploadError(f"Permalink lookup failed: HTTP {resp.status_code}")

    permalink = data.get("permalink")
    if not permalink:
        raise InstagramUploadError(
            f"Permalink lookup response missing 'permalink' field — response: {data!r}"
        )
    logger.info("get_media_permalink: media_id=%s", media_id)
    return permalink


def publish_container(access_token: str, ig_user_id: str, container_id: str) -> str:
    """Publish a finished container. Returns the published MEDIA ID.

    The return value is a Graph API identifier, NOT a shareable link. Use
    get_media_permalink() for anything a human will click.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on any other API error, HTTP error, or network failure.
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

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "publishing container")

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

    Convenience wrapper over the three public steps, kept for callers that want the
    whole flow in one call. upload_instagram.py deliberately does NOT use it — it
    drives the steps individually so it can persist the container id, log each
    transition, and revoke the temporary Drive share link at the right moment.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramUploadError — on container-creation failure, a polling
            ERROR status, a polling timeout, or a publish failure.
    """
    logger.info(
        "upload_reel: starting ig_user_id=%s video_url=%s", ig_user_id, video_url
    )

    container_id = create_media_container(access_token, ig_user_id, video_url)
    logger.info("upload_reel: container created container_id=%s", container_id)

    wait_for_container(access_token, container_id)
    logger.info("upload_reel: container finished processing container_id=%s", container_id)

    post_id = publish_container(access_token, ig_user_id, container_id)
    logger.info("upload_reel: published post_id=%s", post_id)

    return post_id


def discover_business_account(page_access_token: str, page_id: str) -> dict:
    """Return the Instagram professional account linked to a Facebook Page.

    Returns {"id", "username", "account_type"} where account_type is BUSINESS or
    CREATOR. This is the whole of Feature 005's "connection" step: an Instagram
    account must already be converted to Business/Creator and linked to the Page for
    Graph API publishing to be possible at all, so the Page token FieldKit already
    holds from Feature 003 is sufficient to find it — no new OAuth round-trip.

    Raises:
        InstagramTokenError — Graph API error code 190 (token invalid/expired).
        InstagramAccountNotFoundError — no linked account, or the linked account is
            not BUSINESS/CREATOR (e.g. still PERSONAL). Actionable setup problem.
        InstagramUploadError — on any other API error, HTTP error, or network failure.
    """
    url = f"{_GRAPH_BASE}/{page_id}"
    try:
        resp = requests.get(
            url,
            params={
                "fields": "instagram_business_account{id,username}",
                "access_token": page_access_token,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Instagram account discovery request failed: {exc}") from exc

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "discovering Instagram account")

    if not resp.ok:
        raise InstagramUploadError(
            f"Instagram account discovery failed: HTTP {resp.status_code}"
        )

    linked = data.get("instagram_business_account")
    if not linked or not isinstance(linked, dict) or not linked.get("id"):
        raise InstagramAccountNotFoundError(
            f"No Instagram professional account is linked to Facebook Page {page_id}"
        )

    ig_user_id = linked["id"]
    username = linked.get("username", "")

    # The Page edge only exposes id/username, so account_type needs its own lookup on
    # the IG user node. A PERSONAL account cannot publish via the Graph API at all, so
    # discovering one is reported as a setup problem rather than silently accepted.
    account_type = _get_account_type(page_access_token, ig_user_id)
    if account_type not in _ACCOUNT_TYPES:
        raise InstagramAccountNotFoundError(
            f"Instagram account @{username} (ID {ig_user_id}) is a {account_type} account; "
            "a Business or Creator account is required to publish"
        )

    logger.info(
        "discover_business_account: page_id=%s ig_user_id=%s account_type=%s",
        page_id, ig_user_id, account_type,
    )
    return {"id": ig_user_id, "username": username, "account_type": account_type}


def _get_account_type(access_token: str, ig_user_id: str) -> str:
    """Return an Instagram user node's account_type (BUSINESS / CREATOR / PERSONAL).

    A missing account_type field is reported as "PERSONAL": the Graph API omits it for
    accounts that were never converted, and treating "unknown" as publishable would
    push the failure to publish time instead of setup time.
    """
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/{ig_user_id}",
            params={"fields": "account_type,username", "access_token": access_token},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise InstagramUploadError(f"Instagram account type lookup failed: {exc}") from exc

    data = _json_or_empty(resp)
    _raise_for_api_error(data, "reading Instagram account type")

    if not resp.ok:
        raise InstagramUploadError(
            f"Instagram account type lookup failed: HTTP {resp.status_code}"
        )

    return data.get("account_type") or "PERSONAL"
