# Feature 005 — CLI Contracts

## check_instagram_connection.py

**Purpose**: One-time admin setup — discovers the Instagram professional account linked to the client's already-connected Facebook Page, and writes its ID to `.env`. No OAuth flow (reuses Feature 003's `FB_PAGE_ACCESS_TOKEN`).

**Invocation**:
```
python3 scripts/check_instagram_connection.py [--page-id PAGE_ID]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|--------------|
| `--page-id` | from `FB_PAGE_ID` in `.env` | Facebook Page ID to check for a linked Instagram account |

**Reads from `.env`**:
- `FB_PAGE_ACCESS_TOKEN` (required — from Feature 003)
- `FB_PAGE_ID` (required — from Feature 003, unless `--page-id` given)

**Writes to `.env`**:
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
- `1` — environment misconfiguration (missing `FB_PAGE_ACCESS_TOKEN` / `FB_PAGE_ID`)
- `3` — no linked Instagram account found, or found account is not Business/Creator (mirrors Feature 003's Page-selection-failure exit code)

---

## upload_instagram.py

**Purpose**: Cron script — picks up a pending Instagram upload from state, creates a temporary Drive share link for the video, publishes it as a Reel via the Instagram Graph API's container flow, retries on failure, sends Telegram notifications. Runs independently of `upload_facebook.py`.

**Invocation**:
```
python3 scripts/upload_instagram.py [--source cron]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|--------------|
| `--source` | `None` | Invocation label for logging (informational only) |

**Reads from `.env`**:
- `FB_PAGE_ACCESS_TOKEN` (required — reused from Feature 003)
- `IG_BUSINESS_ACCOUNT_ID` (required — from `check_instagram_connection.py`; if absent, the script exits `0` silently — Instagram publishing is not configured for this client, per FR-016)
- `TELEGRAM_BOT_TOKEN` (required — for success/failure notifications)
- `ADMIN_TELEGRAM_CHAT_ID` (required)
- `FIELDKIT_DATA_DIR` (optional)
- `FIELDKIT_LOG_DIR` (optional)

**Behaviour**:
1. If `IG_BUSINESS_ACCOUNT_ID` is not set → exits `0` silently (feature not enabled for this client)
2. Reads `instagram_state.json` for a `pending` or `uploading` record
3. If none → exits silently
4. If `attempt_count == 3` and status still not `published` → alerts and exits
5. If `last_attempt_at` is within the last 60s → exits (retry cooldown not elapsed)
6. Marks status as `uploading`; creates a temporary Drive share link for `video_local_path`
7. Creates an Instagram media container (`media_type=REELS`, the share link as `video_url`); polls status (5s interval, 3-minute cap)
8. On container `FINISHED`: publishes it, revokes the Drive share link, marks `published`, sends Telegram confirmation with post link
9. On failure at any step (container create, poll timeout, publish): revokes the Drive share link if one was created, increments `attempt_count`, updates `last_attempt_at`; if count reaches 3, marks `failed` and sends Telegram alert
10. On token-expiry error (irrecoverable): marks `failed` immediately (skips remaining retries) and sends Telegram alert to reconnect

**Exit codes**:
- `0` — normal exit (feature not configured, no pending job, upload succeeded, or cooldown not elapsed)
- `1` — environment misconfiguration
- `2` — unrecoverable state error (corrupt state file)

---

## check_approval.py (extended)

**Change**: On the existing approve path, after the existing `facebook_state.set_pending_upload(...)` call, add:

```
if os.environ.get("IG_BUSINESS_ACCOUNT_ID") and not instagram_state.is_published(idempotency_key):
    instagram_state.set_pending_upload(project_name, video_local_path, ig_business_account_id, idempotency_key)
```

No new CLI arguments or flags are introduced. Both platform enqueues happen synchronously within the same Telegram callback handler, before the `✅ Approved: {project}` acknowledgment is sent — same behavior as Feature 003 already established for the Facebook enqueue.

---

## .env additions (Feature 005)

```bash
# Instagram publishing — written by check_instagram_connection.py
IG_BUSINESS_ACCOUNT_ID=  # Written by check_instagram_connection.py; absent = Instagram publishing disabled for this client

# No new secret is introduced — Instagram publishing reuses FB_PAGE_ACCESS_TOKEN (Feature 003)
```
