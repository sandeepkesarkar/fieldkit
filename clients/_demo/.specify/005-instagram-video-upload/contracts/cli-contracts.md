# Feature 005 — CLI Contracts

**Revision note**: Paths corrected — both scripts below live under `platform/photo-agent/scripts/` (post-Platform-002-migration shared code), invoked with cwd `platform/photo-agent/`, exactly like `upload_facebook.py` and `check_approval.py`. Only `.env`/`.env.example` remain per-client under `clients/{name}/src/photo-agent/`. `upload_instagram.py`'s retry/cooldown behavior below is described in terms of `instagram_state.claim_pending_upload(...)`, matching the actual current `facebook_state.py` API — see `plan.md`'s Revision Note and `data-model.md`.

## check_instagram_connection.py

**Purpose**: One-time admin setup — discovers the Instagram professional account linked to the client's already-connected Facebook Page, and writes its ID to the client's `.env`. No OAuth flow (reuses Feature 003's `FB_PAGE_ACCESS_TOKEN`).

**Invocation** (from `platform/photo-agent/`, same `CLIENT_NAME`/`FIELDKIT_ROOT` resolution as every other entrypoint script):
```
cd platform/photo-agent
CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py [--page-id PAGE_ID]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|--------------|
| `--page-id` | from `FB_PAGE_ID` in the client's `.env` | Facebook Page ID to check for a linked Instagram account |

**Reads from the client's `.env`** (`clients/{CLIENT_NAME}/src/photo-agent/.env`):
- `FB_PAGE_ACCESS_TOKEN` (required — from Feature 003)
- `FB_PAGE_ID` (required — from Feature 003, unless `--page-id` given)

**Writes to the client's `.env`**:
- `IG_BUSINESS_ACCOUNT_ID` — numeric Instagram professional account ID, only on success

**stdout (success)**:
```
Checking Facebook Page 123456789 for a linked Instagram account...
Found linked Instagram account: @my_business_demo (ID: 17841400000000000)
Account type: BUSINESS
Instagram publishing enabled. IG_BUSINESS_ACCOUNT_ID written to .env.
```

**stdout (no linked account)**:
```
Checking Facebook Page 123456789 for a linked Instagram account...
No Instagram account is linked to this Facebook Page.
Link an Instagram Business or Creator account to this Page in Meta's
Account Settings, then re-run this script. See:
https://www.facebook.com/business/help/898752960195806
```

**stdout (wrong account type)**:
```
Checking Facebook Page 123456789 for a linked Instagram account...
Found linked Instagram account: @my_business_demo, but it is a PERSONAL account.
Convert it to a Business or Creator account in the Instagram app
(Settings > Account type), then re-run this script.
```

**Exit codes**:
- `0` — success, `IG_BUSINESS_ACCOUNT_ID` written
- `1` — environment misconfiguration (missing `FB_PAGE_ACCESS_TOKEN` / `FB_PAGE_ID`, or `CLIENT_NAME` unset — mirrors every other entrypoint script's env-validation exit code)
- `3` — no linked Instagram account found (`InstagramAccountNotFoundError`), or found account is not Business/Creator (mirrors Feature 003's Page-selection-failure exit code)

---

## upload_instagram.py

**Purpose**: Cron script — picks up a pending Instagram upload from state via an atomic claim, creates a temporary Drive share link for the video, publishes it as a Reel via the Instagram Graph API's container flow, retries on failure, sends Telegram notifications. Runs independently of `upload_facebook.py`, guarded by its own `upload_instagram.lock` re-entrancy lock.

**Invocation**:
```
cd platform/photo-agent
CLIENT_NAME=_demo python3 scripts/upload_instagram.py [--source cron]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|--------------|
| `--source` | `None` | Invocation label for logging (informational only) |

**Reads from the client's `.env`**:
- `FB_PAGE_ACCESS_TOKEN` (required — reused from Feature 003)
- `IG_BUSINESS_ACCOUNT_ID` (required — from `check_instagram_connection.py`; if absent, the script exits `0` silently — Instagram publishing is not configured for this client, per FR-016)
- `TELEGRAM_BOT_TOKEN` (required — for success/failure notifications)
- `ADMIN_TELEGRAM_CHAT_ID` (required)
- `FIELDKIT_DATA_DIR` (required — same as `facebook_state.py`)
- `FIELDKIT_LOG_DIR` (required — same as `facebook_logger.py`)

**Behaviour** (mirrors `upload_facebook.py`'s current claim-based logic, not a hand-rolled cooldown check):
1. If `IG_BUSINESS_ACCOUNT_ID` is not set → exits `0` silently (feature not enabled for this client)
2. Acquires `upload_instagram.lock` (re-entrancy guard); exits silently if already held by another invocation
3. Reads `instagram_state.get_pending_upload()`; if none → exits silently
4. Calls `instagram_state.claim_pending_upload(idempotency_key, cooldown_seconds=60, max_attempts=3, lease_seconds=...)`; acts only on `"claimed"` — any other return value (`cooldown`, `in_flight`, `exhausted`, `stale_published`, `stale_failed`, `mismatch`) means exit without calling any external API, exactly as `upload_facebook.py` does for the equivalent Facebook return values
5. Checks the video file exists (calls `mark_failed` + logs if missing, without attempting a Drive/Instagram call)
6. Creates a temporary Drive share link (`drive.create_temporary_share_link`) → `instagram_api.create_media_container` → polls `instagram_api.get_container_status` (5s interval, 300s/60-attempt cap) → `instagram_api.publish_container`
7. On success: revokes the Drive share link, calls `mark_published`, `log_upload_published`, sends Telegram `"✅ Reel live on Instagram! {post_url}"` via `telegram_api.send_message`
8. On any `InstagramUploadError` (including a poll-cap timeout): revokes the Drive share link if one was created, calls `release_claim` (if under the attempt ceiling — the state module tracks whether this was the final attempt) or `mark_failed` + `log_upload_exhausted` + Telegram alert (if this was the 3rd attempt)
9. On `InstagramTokenError`: revokes the Drive share link if one was created, calls `mark_failed` immediately (skips remaining retries), `log_token_expired`, sends Telegram alert

**Exit codes**:
- `0` — normal exit (feature not configured, no pending job, claim not granted, upload succeeded, or lock already held)
- `1` — environment misconfiguration
- `2` — unrecoverable state error (corrupt state file)

---

## check_approval.py (extended)

**Change**: On the existing approve path (in `platform/photo-agent/scripts/check_approval.py`, invoked synchronously by Hermes's `photo-approve` skill — not cron), after the existing `facebook_state.set_pending_upload(...)` call, add:

```python
ig_business_account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID")
if ig_business_account_id and not instagram_state.is_published(idempotency_key):
    instagram_state.set_pending_upload({
        "project_name": project_name,
        "video_local_path": video_local_path,
        "ig_business_account_id": ig_business_account_id,
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": None,
        "triggered_at": now_iso8601,
        "idempotency_key": idempotency_key,
        "container_id": None,
        "ig_post_id": None,
    })
```

No new CLI arguments or flags are introduced. Both platform enqueues happen synchronously within the same invocation, before the `"Approved: {project}"` stdout line — same behavior as Feature 003 already established for the Facebook enqueue. A failure enqueueing the Instagram job is logged as an error but does not abort the Facebook enqueue or the approve flow itself.

---

## .env additions (Feature 005)

In `clients/_demo/src/photo-agent/.env.example` (and each enabled client's real `.env`):

```bash
# Instagram publishing — written by check_instagram_connection.py
IG_BUSINESS_ACCOUNT_ID=  # Written by check_instagram_connection.py; absent = Instagram publishing disabled for this client

# No new secret is introduced — Instagram publishing reuses FB_PAGE_ACCESS_TOKEN (Feature 003)
```
