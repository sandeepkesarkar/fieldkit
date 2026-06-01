# Feature 003 — CLI Contracts

## generate_auth_link.py

**Purpose**: One-time admin setup — generates Facebook OAuth URL, catches the redirect, exchanges for a permanent Page access token, and writes credentials to `.env`.

**Invocation**:
```
python3 scripts/generate_auth_link.py [--port PORT] [--page-id PAGE_ID]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | `8080` | Port for the local OAuth callback server |
| `--page-id` | from `FB_PAGE_ID` in `.env` | Facebook Page ID to select (if owner has multiple Pages) |

**Reads from `.env`**:
- `FB_APP_ID` (required)
- `FB_APP_SECRET` (required)
- `FB_REDIRECT_URI` (optional; default: `http://localhost:8080/callback`)
- `FB_PAGE_ID` (optional; used to auto-select when owner has multiple Pages)

**Writes to `.env`**:
- `FB_PAGE_ID` — numeric Page ID of the linked Page
- `FB_PAGE_ACCESS_TOKEN` — permanent Page access token

**stdout**:
```
Facebook authorization URL:
https://www.facebook.com/dialog/oauth?...

Waiting for authorization on http://localhost:8080/callback ...
Authorization complete. Page access token written to .env.
Linked Page: "My Business" (ID: 123456789)
```

**Exit codes**:
- `0` — success, token written
- `1` — environment misconfiguration (missing `FB_APP_ID` / `FB_APP_SECRET`)
- `2` — OAuth flow failed (user denied, bad code, network error)
- `3` — Page selection failed (no pages found, or specified page ID not in account)

---

## upload_facebook.py

**Purpose**: Cron script — picks up a pending Facebook upload from state, attempts to post the video, retries on failure, sends Telegram notifications.

**Invocation**:
```
python3 scripts/upload_facebook.py [--source cron]
```

**Arguments**:

| Argument | Default | Description |
|----------|---------|-------------|
| `--source` | `None` | Invocation label for logging (informational only) |

**Reads from `.env`**:
- `FB_PAGE_ACCESS_TOKEN` (required)
- `FB_PAGE_ID` (required)
- `TELEGRAM_BOT_TOKEN` (required — for success/failure notifications)
- `ADMIN_TELEGRAM_CHAT_ID` (required)
- `FIELDKIT_DATA_DIR` (optional)
- `FIELDKIT_LOG_DIR` (optional)

**Behaviour**:
1. Reads `facebook_state.json` for a `pending` or `uploading` record
2. If none → exits silently
3. If `attempt_count == 3` and status still not `published` → alerts and exits
4. If `last_attempt_at` is within the last 60 s → exits (retry cooldown not elapsed)
5. Marks status as `uploading`, calls Facebook Graph API
6. On success: marks `published`, sends Telegram confirmation with post link
7. On failure: increments `attempt_count`, updates `last_attempt_at`; if count reaches 3 marks `failed` and sends Telegram alert

**Exit codes**:
- `0` — normal exit (no pending job, upload succeeded, or cooldown not elapsed)
- `1` — environment misconfiguration
- `2` — unrecoverable state error (corrupt state file)

---

## .env additions (Feature 003)

```bash
# Facebook Page connection — written by generate_auth_link.py
FB_APP_ID=         # Meta Developer App ID (required before running generate_auth_link.py)
FB_APP_SECRET=     # Meta Developer App secret (required before running generate_auth_link.py)
FB_PAGE_ID=        # Written by generate_auth_link.py
FB_PAGE_ACCESS_TOKEN=  # Written by generate_auth_link.py — permanent token

# Optional
FB_REDIRECT_URI=http://localhost:8080/callback  # Override if using a different port
```
