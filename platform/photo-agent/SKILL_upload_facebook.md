# SKILL: upload_facebook.py — Cron Video Upload to Facebook

Uploads the pending Facebook video job (set by `check_approval.py` on approve) to the
linked Facebook Page. Runs every minute alongside the existing cron.

---

## Prerequisites

Before this cron will do anything useful, the following must be in place:

| Requirement | How to fulfill |
|---|---|
| Meta Developer App | Create at developers.facebook.com — follow [docs/facebook/01-create-app.md](docs/facebook/01-create-app.md) |
| Test Facebook Page | Create via your personal Facebook account |
| FB_PAGE_ACCESS_TOKEN | Run `generate_auth_link.py` once, OR follow the manual flow in [docs/facebook/02-manual-test.md](docs/facebook/02-manual-test.md) Parts A–D |
| FB_PAGE_ID | Written by `generate_auth_link.py`, or copy from `GET /me/accounts` response |
| TELEGRAM_BOT_TOKEN | Already set for existing features |
| ADMIN_TELEGRAM_CHAT_ID | Already set for existing features |

---

## Environment Variables (`.env`)

```bash
FB_PAGE_ID=               # Set by generate_auth_link.py
FB_PAGE_ACCESS_TOKEN=     # Set by generate_auth_link.py (permanent token, never expires)
```

Required — set in `clients/<client>/src/photo-agent/.env`:
```bash
FIELDKIT_DATA_DIR=   # Absolute path to client data directory (e.g. /path/to/fieldkit/clients/<client>/data)
FIELDKIT_LOG_DIR=    # Absolute path to client log directory (e.g. /path/to/fieldkit/clients/<client>/logs)
```

---

## Cron Setup

Add to crontab (`crontab -e`) alongside the existing `check_approval.py` entry:

```cron
* * * * * /usr/local/bin/python3 /path/to/fieldkit/platform/photo-agent/scripts/upload_facebook.py --source cron >> /path/to/fieldkit/logs/cron.log 2>&1
```

Replace `/path/to/fieldkit` with the absolute path to your FieldKit repo root.

---

## Manual Invocation

From the repo root:
```bash
python3 platform/photo-agent/scripts/upload_facebook.py
python3 platform/photo-agent/scripts/upload_facebook.py --source cron
```

---

## Behavior

1. Reads `data/photo-agent/facebook_state.json` for a `pending` or `uploading` job.
2. If none → exits silently (0).
3. Checks 60-second retry cooldown — exits silently if too soon.
4. Checks that the video file exists — marks `failed` if not.
5. Marks status `uploading`, calls Facebook Graph API (`POST /{page_id}/videos`).
6. On success: marks `published`, sends Telegram confirmation with the post URL.
7. On `FacebookUploadError`: increments `attempt_count`; after 3 failures marks `failed`
   and sends Telegram alert.
8. On `FacebookTokenError` (token invalid/expired): marks `failed` immediately and
   sends Telegram alert to reconnect the Page.

---

## Log File

All events are appended to `logs/photo-agent.log`:

```
2026-05-30 14:01 | FB_STARTED   | project=kitchen_remodel attempt=1
2026-05-30 14:01 | FB_PUBLISHED | project=kitchen_remodel post_id=12345678901234
```

Event types: `FB_ENQUEUED`, `FB_STARTED`, `FB_PUBLISHED`, `FB_FAILED`, `FB_EXHAUSTED`, `FB_TOKEN_EXP`

---

## Retry Policy

| Scenario | Behaviour |
|---|---|
| Transient error (network, server 5xx) | Retry up to 3× with 60s cooldown between attempts |
| Token expired / revoked (error code 190) | Mark failed immediately; no retries; Telegram alert |
| Video file missing | Mark failed immediately; no retries |
| 3 consecutive failures | Mark failed; send Telegram alert |
