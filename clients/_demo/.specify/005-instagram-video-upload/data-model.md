# Feature 005 — Data Model

## Entities

### InstagramAccountConnection

Represents a client's linked Instagram professional account, discovered through the already-connected Facebook Page (Feature 003). One per client. Stored as environment variables in `.env` — no separate model file.

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `IG_BUSINESS_ACCOUNT_ID` | `str` | `.env`, written by `check_instagram_connection.py` | Instagram professional account ID (numeric string) discovered via `GET /{page_id}?fields=instagram_business_account` |

No new access token is stored — this feature reuses `FB_PAGE_ACCESS_TOKEN` (Feature 003) for all Instagram Graph API calls.

---

### InstagramUploadJob

Represents one upload attempt for an approved video, on the Instagram platform. Persisted in `data/photo-agent/instagram_state.json` under the `pending_instagram_upload` key. Structurally identical to `VideoUploadJob` (Feature 003) except for one additional transient field for the async container flow.

| Field | Type | Nullable | Description |
|-------|------|----------|--------------|
| `project_name` | `str` | No | Identifies the business project |
| `video_local_path` | `str` | No | Absolute path to the MP4 file on disk |
| `ig_business_account_id` | `str` | No | Target Instagram account ID |
| `status` | `str` | No | One of: `pending`, `uploading`, `published`, `failed` |
| `attempt_count` | `int` | No | Number of upload attempts made so far (0 = not yet tried) |
| `last_attempt_at` | `str\|null` | Yes | ISO-8601 UTC timestamp of the last attempt (null = never tried) |
| `triggered_at` | `str` | No | ISO-8601 UTC timestamp when the upload was enqueued |
| `idempotency_key` | `str` | No | Unique key for duplicate detection (same Telegram message_id already used for the Facebook job) |
| `container_id` | `str\|null` | Yes | Instagram media container ID for the in-flight attempt (null once the attempt ends, success or failure) |
| `ig_post_id` | `str\|null` | Yes | Instagram post ID after successful publish (null until published) |

#### State transitions

```
pending → uploading → published
                    ↘ failed (after 3 attempts, or on token expiry, or on container-poll timeout)
```

- `pending`: set by `check_approval.py` when owner approves (alongside, not instead of, the Facebook job)
- `uploading`: set by `upload_instagram.py` at the start of each attempt
- `published`: set after Instagram Graph API confirms the publish
- `failed`: set after 3 failed attempts OR on irrecoverable error (token expiry, revocation) OR container-poll timeout

This state machine is independent of `VideoUploadJob`'s (Feature 003) — the two are correlated only by sharing the same `idempotency_key` for the same approved video (FR-013).

---

### UploadAttempt

Not persisted separately — captured as fields on `InstagramUploadJob` (`attempt_count`, `last_attempt_at`) plus log entries in `photo-agent.log`. Full error details (including Instagram container status/error codes) are written to the log file.

---

## State file: `instagram_state.json`

Location: `data/photo-agent/instagram_state.json` (respects `FIELDKIT_DATA_DIR` override; separate file from `facebook_state.json`, same directory)

```json
{
  "pending_instagram_upload": {
    "project_name": "...",
    "video_local_path": "/abs/path/to/video.mp4",
    "ig_business_account_id": "17841400000000000",
    "status": "pending",
    "attempt_count": 0,
    "last_attempt_at": null,
    "triggered_at": "2026-08-31T14:00:00Z",
    "idempotency_key": "98765",
    "container_id": null,
    "ig_post_id": null
  },
  "published_idempotency_keys": ["98765", "12345"]
}
```

`published_idempotency_keys` grows with each successful post; acceptable for demo scale (same accepted pattern as Feature 003).

---

## Validation rules

- `project_name` and `status` must match `^[A-Za-z0-9_-]+$` (consistent with `logger.py`)
- `attempt_count` must be `0 ≤ n ≤ 3`
- `video_local_path` must be an absolute path; existence is verified before upload
- `idempotency_key` must be a non-empty string
- `status` must be one of the four valid states; transitions are enforced by `instagram_state.py`
- `container_id`, once set during an attempt, must be cleared (`null`) when that attempt concludes (published or failed) — it must never be reused across attempts, since a fresh container is created on each retry
