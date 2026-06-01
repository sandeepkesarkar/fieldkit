# Feature 003 — Sequence Diagram: Facebook Video Upload (Happy Path)

```mermaid
sequenceDiagram
    actor Admin
    actor Owner
    participant Telegram
    participant check_approval.py
    participant facebook_state.py
    participant upload_facebook.py
    participant facebook_api.py
    participant FacebookGraphAPI

    Note over Admin,FacebookGraphAPI: One-time setup (generate_auth_link.py — not shown)

    Admin->>Telegram: /process_photos → approval message sent
    Owner->>Telegram: taps Approve

    Telegram->>check_approval.py: callback_data=approve
    check_approval.py->>facebook_state.py: set_pending_upload(project, video_path, idempotency_key)
    check_approval.py->>Telegram: ✅ Approved: {project}

    Note over upload_facebook.py,FacebookGraphAPI: Cron tick (~1 min later)
    upload_facebook.py->>facebook_state.py: get_pending_upload()
    facebook_state.py-->>upload_facebook.py: VideoUploadJob (status=pending)
    upload_facebook.py->>facebook_state.py: mark_uploading()
    upload_facebook.py->>facebook_api.py: upload_video(token, page_id, video_path)
    facebook_api.py->>FacebookGraphAPI: POST /{page_id}/videos (multipart)
    FacebookGraphAPI-->>facebook_api.py: {post_id}
    facebook_api.py-->>upload_facebook.py: post_id
    upload_facebook.py->>facebook_state.py: mark_published(idempotency_key, post_id)
    upload_facebook.py->>Telegram: ✅ Video live! facebook.com/{post_id}
    Telegram-->>Owner: confirmation message with post link
```

## Failure path — transient error with retry

```mermaid
sequenceDiagram
    participant upload_facebook.py
    participant facebook_api.py
    participant FacebookGraphAPI
    participant facebook_state.py
    participant Telegram

    upload_facebook.py->>facebook_api.py: upload_video(...)
    facebook_api.py->>FacebookGraphAPI: POST /{page_id}/videos
    FacebookGraphAPI-->>facebook_api.py: HTTP 500 / network error
    facebook_api.py-->>upload_facebook.py: raises FacebookUploadError
    upload_facebook.py->>facebook_state.py: increment_attempt() [count=1]
    Note right of upload_facebook.py: status stays uploading; exits

    Note over upload_facebook.py: 60s cooldown; next cron tick
    upload_facebook.py->>facebook_api.py: upload_video(...)
    facebook_api.py->>FacebookGraphAPI: POST /{page_id}/videos
    FacebookGraphAPI-->>facebook_api.py: {post_id}
    facebook_api.py-->>upload_facebook.py: post_id
    upload_facebook.py->>facebook_state.py: mark_published(...)
    upload_facebook.py->>Telegram: ✅ Video live! (no failure alert sent)
```

## Failure path — token expiry (irrecoverable)

```mermaid
sequenceDiagram
    participant upload_facebook.py
    participant facebook_api.py
    participant FacebookGraphAPI
    participant facebook_state.py
    participant Telegram

    upload_facebook.py->>facebook_api.py: upload_video(...)
    facebook_api.py->>FacebookGraphAPI: POST /{page_id}/videos
    FacebookGraphAPI-->>facebook_api.py: OAuthException (error code 190)
    facebook_api.py-->>upload_facebook.py: raises FacebookTokenError
    upload_facebook.py->>facebook_state.py: mark_failed()
    upload_facebook.py->>Telegram: ⚠️ Facebook token expired — reconnect your Page
```
