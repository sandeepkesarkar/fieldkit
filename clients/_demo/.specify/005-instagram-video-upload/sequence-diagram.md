# Feature 005 — Sequence Diagram: Instagram Video Upload (Happy Path)

```mermaid
sequenceDiagram
    actor Admin
    actor Owner
    participant Telegram
    participant check_approval.py
    participant facebook_state.py
    participant instagram_state.py
    participant upload_instagram.py
    participant drive.py
    participant instagram_api.py
    participant InstagramGraphAPI

    Note over Admin,InstagramGraphAPI: One-time setup (check_instagram_connection.py — not shown)

    Admin->>Telegram: /process_photos → approval message sent
    Owner->>Telegram: taps Approve

    Telegram->>check_approval.py: callback_data=approve
    check_approval.py->>facebook_state.py: set_pending_upload(project, video_path, idempotency_key)
    check_approval.py->>instagram_state.py: set_pending_upload(project, video_path, idempotency_key)
    check_approval.py->>Telegram: ✅ Approved: {project}

    Note over upload_instagram.py,InstagramGraphAPI: Cron tick (~1 min later, independent of upload_facebook.py)
    upload_instagram.py->>instagram_state.py: get_pending_upload()
    instagram_state.py-->>upload_instagram.py: InstagramUploadJob (status=pending)
    upload_instagram.py->>instagram_state.py: mark_uploading()
    upload_instagram.py->>drive.py: create_temporary_share_link(video_path)
    drive.py-->>upload_instagram.py: video_url
    upload_instagram.py->>instagram_api.py: create_media_container(token, ig_user_id, video_url)
    instagram_api.py->>InstagramGraphAPI: POST /{ig_user_id}/media
    InstagramGraphAPI-->>instagram_api.py: {container_id}
    instagram_api.py-->>upload_instagram.py: container_id

    loop poll every 5s, cap 3 min
        upload_instagram.py->>instagram_api.py: get_container_status(token, container_id)
        instagram_api.py->>InstagramGraphAPI: GET /{container_id}?fields=status_code
        InstagramGraphAPI-->>instagram_api.py: status_code
        instagram_api.py-->>upload_instagram.py: status_code
    end
    Note right of upload_instagram.py: status_code == FINISHED

    upload_instagram.py->>instagram_api.py: publish_container(token, ig_user_id, container_id)
    instagram_api.py->>InstagramGraphAPI: POST /{ig_user_id}/media_publish
    InstagramGraphAPI-->>instagram_api.py: {post_id}
    instagram_api.py-->>upload_instagram.py: post_id
    upload_instagram.py->>drive.py: revoke_share_link(video_url)
    upload_instagram.py->>instagram_state.py: mark_published(idempotency_key, post_id)
    upload_instagram.py->>Telegram: ✅ Reel live! instagram.com/p/{post_id}
    Telegram-->>Owner: confirmation message with post link
```

## Failure path — transient error with retry

```mermaid
sequenceDiagram
    participant upload_instagram.py
    participant instagram_api.py
    participant InstagramGraphAPI
    participant instagram_state.py
    participant Telegram

    upload_instagram.py->>instagram_api.py: create_media_container(...)
    instagram_api.py->>InstagramGraphAPI: POST /{ig_user_id}/media
    InstagramGraphAPI-->>instagram_api.py: HTTP 500 / network error
    instagram_api.py-->>upload_instagram.py: raises InstagramUploadError
    upload_instagram.py->>instagram_state.py: increment_attempt() [count=1]
    Note right of upload_instagram.py: status stays uploading; exits

    Note over upload_instagram.py: 60s cooldown; next cron tick
    upload_instagram.py->>instagram_api.py: create_media_container(...)
    instagram_api.py->>InstagramGraphAPI: POST /{ig_user_id}/media
    InstagramGraphAPI-->>instagram_api.py: {container_id}
    Note over upload_instagram.py: poll → publish (as in happy path)
    upload_instagram.py->>instagram_state.py: mark_published(...)
    upload_instagram.py->>Telegram: ✅ Reel live! (no failure alert sent)
```

## Failure path — token expiry (irrecoverable)

```mermaid
sequenceDiagram
    participant upload_instagram.py
    participant instagram_api.py
    participant InstagramGraphAPI
    participant instagram_state.py
    participant Telegram

    upload_instagram.py->>instagram_api.py: create_media_container(...)
    instagram_api.py->>InstagramGraphAPI: POST /{ig_user_id}/media
    InstagramGraphAPI-->>instagram_api.py: OAuthException (error code 190)
    instagram_api.py-->>upload_instagram.py: raises InstagramTokenError
    upload_instagram.py->>instagram_state.py: mark_failed()
    upload_instagram.py->>Telegram: ⚠️ Instagram token expired — reconnect your account
```

## Failure path — Instagram upload fails while Facebook upload succeeds (platform independence, FR-013)

```mermaid
sequenceDiagram
    participant upload_facebook.py
    participant upload_instagram.py
    participant facebook_state.py
    participant instagram_state.py
    participant Telegram

    par Facebook job (independent queue)
        upload_facebook.py->>facebook_state.py: get_pending_upload()
        Note over upload_facebook.py: uploads successfully (see Feature 003 diagram)
        upload_facebook.py->>Telegram: ✅ Video live on Facebook!
    and Instagram job (independent queue)
        upload_instagram.py->>instagram_state.py: get_pending_upload()
        Note over upload_instagram.py: container creation fails 3x, retries exhausted
        upload_instagram.py->>instagram_state.py: mark_failed()
        upload_instagram.py->>Telegram: ⚠️ Instagram upload failed — needs attention
    end
    Note over upload_facebook.py,upload_instagram.py: Neither job blocks, retries, or rolls back the other
```
