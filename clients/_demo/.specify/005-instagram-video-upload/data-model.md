# Feature 005 — Data Model

**Revision note**: Corrected against `tools/facebook_state.py`'s actual current public API (an atomic-claim state machine), which differs from the simpler `mark_uploading`/`increment_attempt` shape an earlier draft of this document assumed by analogy to Feature 003's original (pre-migration) design docs. See `plan.md`'s Revision Note for full context. **`tools/instagram_state.py`'s implementer should read `tools/facebook_state.py`'s source directly** as the authoritative interface to mirror — this document describes the intent and confirmed public function names, not a guaranteed-exact reproduction of internal logic.

## Entities

### InstagramAccountConnection

Represents a client's linked Instagram professional account, discovered through the already-connected Facebook Page (Feature 003). One per client. Stored as environment variables in `clients/{name}/src/photo-agent/.env` — no separate model file.

| Field | Type | Source | Description |
|-------|------|--------|--------------|
| `IG_BUSINESS_ACCOUNT_ID` | `str` | `.env`, written by `check_instagram_connection.py` | Instagram professional account ID (numeric string) discovered via `GET /{page_id}?fields=instagram_business_account` |

No new access token is stored — this feature reuses `FB_PAGE_ACCESS_TOKEN` (Feature 003) for all Instagram Graph API calls.

---

### InstagramUploadJob

Represents one upload attempt for an approved video, on the Instagram platform. Persisted in `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json`, managed exclusively through `tools/instagram_state.py`'s claim-based API — callers never read/write the JSON directly.

**Confirmed public API to mirror (from `tools/facebook_state.py`)**:

```python
def get_pending_upload() -> dict | None
def set_pending_upload(record: dict) -> None                       # raises ValueError on bad record
def claim_pending_upload(idempotency_key: str, *, cooldown_seconds: int,
                          max_attempts: int, lease_seconds: int) -> str
    # returns one of: "mismatch" | "in_flight" | "cooldown" | "stale_published"
    #                 | "stale_failed" | "exhausted" | "claimed"
def release_claim(idempotency_key: str) -> None
def clear_pending_upload(expected_idempotency_key: str) -> bool
def mark_published(idempotency_key: str, post_id: str) -> None
def mark_failed(idempotency_key: str) -> None
def is_published(idempotency_key: str) -> bool
def find_published(project_name: str) -> dict | None
```

`upload_instagram.py` MUST use `claim_pending_upload(...)` as the single atomic entrypoint for deciding whether to act on the pending record (exactly as `upload_facebook.py` does), rather than manually reading `status`/`attempt_count`/`last_attempt_at` and deciding cooldown/retry logic itself. This is what makes the cron re-entrancy guarantee and the 60s-cooldown/3-attempt behavior correct under concurrent cron ticks.

**Record fields** (adapted from `facebook_state.py`'s required-key set: `project_name, video_local_path, page_id, status, attempt_count, last_attempt_at, triggered_at, idempotency_key, fb_post_id`):

| Field | Type | Nullable | Description |
|-------|------|----------|--------------|
| `project_name` | `str` | No | Identifies the business project |
| `video_local_path` | `str` | No | Absolute path to the MP4 file on disk |
| `ig_business_account_id` | `str` | No | Target Instagram account ID (analogous to `page_id`) |
| `status` | `str` | No | One of: `pending`, `uploading`, `published`, `failed` |
| `attempt_count` | `int` | No | Number of upload attempts made so far (0 = not yet tried) |
| `last_attempt_at` | `str\|null` | Yes | ISO-8601 UTC timestamp of the last attempt (null = never tried) |
| `triggered_at` | `str` | No | ISO-8601 UTC timestamp when the upload was enqueued |
| `idempotency_key` | `str` | No | Unique key for duplicate detection — same value already used for that video's Facebook job (Telegram `message_id`) |
| `container_id` | `str\|null` | Yes | Instagram media container ID for the in-flight attempt (Instagram-specific; cleared once the attempt ends, success or failure) |
| `ig_post_id` | `str\|null` | Yes | Instagram post ID after successful publish (analogous to `fb_post_id`; null until published) |

#### State transitions

```
pending → uploading → published
                    ↘ failed (after 3 attempts, or on token expiry, or on container-poll timeout)
```

- `pending`: set by `check_approval.py` when owner approves (alongside, not instead of, the Facebook job)
- `uploading`: entered via a successful `claim_pending_upload(...)` call at the start of an attempt
- `published`: set by `mark_published(...)` after Instagram Graph API confirms the publish
- `failed`: set by `mark_failed(...)` after 3 failed attempts OR on irrecoverable error (token expiry, revocation) OR container-poll timeout

This state machine is independent of `VideoUploadJob`'s (Feature 003) — the two are correlated only by sharing the same `idempotency_key` for the same approved video (FR-013), never by sharing state, locks, or claim namespaces.

---

### UploadAttempt

Not persisted separately — captured as fields on `InstagramUploadJob` (`attempt_count`, `last_attempt_at`) plus log entries in `photo-agent.log`. Full error details (including Instagram container status/error codes) are written to the log file.

---

## State file: `instagram_state.json`

Location: `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json` (e.g. `clients/_demo/data/photo-agent/instagram_state.json`) — separate file from `facebook_state.json`, same directory, same `fcntl` locking discipline.

Illustrative shape (exact on-disk schema should match whatever `facebook_state.py` actually persists internally — confirm by reading its source, do not treat this JSON as gospel over the code):

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

---

## Validation rules

- `project_name` and `status` must match `^[A-Za-z0-9_-]+$` (consistent with `logger.py`)
- `attempt_count` must be `0 ≤ n ≤ 3`
- `video_local_path` must be an absolute path; existence is verified before upload
- `idempotency_key` must be a non-empty string
- `status` must be one of the four valid states; transitions are enforced by `instagram_state.py`'s `claim_pending_upload`/`mark_published`/`mark_failed`, never by direct field writes
- `container_id`, once set during an attempt, must be cleared (`null`) when that attempt concludes (published or failed) — it must never be reused across attempts, since a fresh container is created on each retry
- Callers of `instagram_state.py` MUST use `claim_pending_upload(...)` before acting on a pending record, and MUST call `release_claim(...)` on any early-exit path that doesn't reach `mark_published`/`mark_failed`, exactly mirroring `upload_facebook.py`'s usage of `facebook_state.claim_pending_upload(...)`
