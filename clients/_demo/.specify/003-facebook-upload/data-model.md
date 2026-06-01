# Feature 003 — Data Model

## Entities

### FacebookPageConnection

Represents a client's linked Facebook Page. One per client. Stored as environment variables in `.env` — no separate model file.

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `FB_PAGE_ID` | `str` | `.env` | Facebook Page ID (numeric string) |
| `FB_PAGE_ACCESS_TOKEN` | `str` | `.env` | Permanent Page access token (derived from long-lived user token) |
| `FB_APP_ID` | `str` | `.env` | Meta Developer App ID |
| `FB_APP_SECRET` | `str` | `.env` | Meta Developer App secret (used only during `generate_auth_link.py`) |
| `FB_REDIRECT_URI` | `str` | `.env` | OAuth redirect URI (default: `http://localhost:8080/callback`) |

---

### VideoUploadJob

Represents one upload attempt for an approved video. Persisted in `data/photo-agent/facebook_state.json` under the `pending_facebook_upload` key.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `project_name` | `str` | No | Identifies the business project |
| `video_local_path` | `str` | No | Absolute path to the MP4 file on disk |
| `page_id` | `str` | No | Target Facebook Page ID |
| `status` | `str` | No | One of: `pending`, `uploading`, `published`, `failed` |
| `attempt_count` | `int` | No | Number of upload attempts made so far (0 = not yet tried) |
| `last_attempt_at` | `str\|null` | Yes | ISO-8601 UTC timestamp of the last attempt (null = never tried) |
| `triggered_at` | `str` | No | ISO-8601 UTC timestamp when the upload was enqueued |
| `idempotency_key` | `str` | No | Unique key for duplicate detection (Telegram message_id as str) |
| `fb_post_id` | `str\|null` | Yes | Facebook post ID after successful publish (null until published) |

#### State transitions

```
pending → uploading → published
                    ↘ failed (after 3 attempts, or on token expiry)
```

- `pending`: set by `check_approval.py` when owner approves
- `uploading`: set by `upload_facebook.py` at the start of each attempt
- `published`: set after Facebook API confirms the post
- `failed`: set after 3 failed attempts OR on irrecoverable error (token expiry, revocation)

---

### UploadAttempt

Not persisted separately — captured as fields on `VideoUploadJob` (`attempt_count`, `last_attempt_at`) plus log entries in `photo-agent.log`. Full error details are written to the log file.

---

## State file: `facebook_state.json`

Location: `data/photo-agent/facebook_state.json` (respects `FIELDKIT_DATA_DIR` override)

```json
{
  "pending_facebook_upload": {
    "project_name": "...",
    "video_local_path": "/abs/path/to/video.mp4",
    "page_id": "123456789",
    "status": "pending",
    "attempt_count": 0,
    "last_attempt_at": null,
    "triggered_at": "2026-05-30T14:00:00Z",
    "idempotency_key": "98765",
    "fb_post_id": null
  },
  "published_idempotency_keys": ["98765", "12345"]
}
```

`published_idempotency_keys` grows with each successful post; acceptable for demo scale.

---

## Validation rules

- `project_name` and `status` must match `^[A-Za-z0-9_-]+$` (consistent with logger.py)
- `attempt_count` must be `0 ≤ n ≤ 3`
- `video_local_path` must be an absolute path; existence is verified before upload
- `idempotency_key` must be a non-empty string
- `status` must be one of the four valid states; transitions are enforced by `facebook_state.py`
