# Instagram Integration — Setup Docs

Feature 005 publishes an approved video to a client's Instagram professional
account as a Reel, from the **same** Telegram approval that already triggers the
Facebook post. There is no second approval step and no second bot.

Unlike the Facebook integration, this one needs **no new Meta Developer App and
no new OAuth flow** — which is why there is no `01-create-app.md` here. Everything
below rides on the Facebook Page connection you already made in
[`../facebook/`](../facebook/README.md).

---

## Why there is no new app or token

Instagram content publishing for professional accounts is served by the
**Facebook** Graph API (`graph.facebook.com`), through the Facebook Page the
Instagram account is linked to. An Instagram account must already be a
Business/Creator account linked to a Page before the API can publish to it at
all — and once that's true, the Page access token FieldKit already holds from
Feature 003 is sufficient.

So this feature adds exactly one new environment variable:

| Variable | What it is | Where it comes from |
|---|---|---|
| `IG_BUSINESS_ACCOUNT_ID` | Instagram professional account ID (numeric) | Written by `check_instagram_connection.py` |

`IG_BUSINESS_ACCOUNT_ID` is a **public account identifier, not a secret**. No new
token, app secret, or credential class is introduced anywhere in Feature 005.

**If `IG_BUSINESS_ACCOUNT_ID` is absent or empty, Instagram publishing is off for
that client** — `check_approval.py` enqueues no Instagram job and
`upload_instagram.py` exits 0 without touching state. That absence is the entire
per-client enable switch; there is no client-name special-casing in the code.

---

## Prerequisites

1. The client's Facebook Page is already connected (see
   [`../facebook/README.md`](../facebook/README.md)) — `FB_PAGE_ID` and
   `FB_PAGE_ACCESS_TOKEN` are in the client's `.env`.
2. The client's Instagram account is a **Business or Creator** account.
   In the Instagram app: *Settings → Account type and tools → Switch to
   professional account*.
3. That Instagram account is **linked to the Facebook Page**.
   In Meta Business Suite / Page settings: *Linked accounts → Instagram → Connect*.
   Reference: <https://www.facebook.com/business/help/898752960195806>

Steps 2 and 3 are done by a human in Meta's UI. They are not automatable, and
`check_instagram_connection.py` will tell you clearly if either is missing.

---

## Step 1 — Link the account (one-time, per client)

Run the connection check from `platform/photo-agent/`:

```bash
cd platform/photo-agent
CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py
```

Optionally target a specific Page instead of the `.env` value:

```bash
CLIENT_NAME=_demo python3 scripts/check_instagram_connection.py --page-id 123456789
```

**On success** it writes `IG_BUSINESS_ACCOUNT_ID` into
`clients/<client>/src/photo-agent/.env` (preserving every other variable) and prints:

```
Checking Facebook Page 123456789 for a linked Instagram account...
Found linked Instagram account: @my_business_demo (ID: 17841400000000000)
Account type: BUSINESS
Instagram publishing enabled. IG_BUSINESS_ACCOUNT_ID written to .env.
```

**Exit codes:**

| Code | Meaning | What to do |
|---|---|---|
| `0` | Success | Nothing — Instagram publishing is now enabled for this client |
| `1` | `FB_PAGE_ACCESS_TOKEN` / `FB_PAGE_ID` missing, token expired, or the API call failed | Re-run `generate_auth_link.py`, or retry if it was transient |
| `3` | No linked Instagram account, or the linked one is `PERSONAL` | Do prerequisites 2/3 above, then re-run |

Both exit-3 cases print specific, actionable guidance — not a stack trace. The
`PERSONAL` case tells you to convert the account; the not-linked case tells you
to link one.

Re-running the script against an already-configured client is safe: it updates
`IG_BUSINESS_ACCOUNT_ID` in place rather than appending a duplicate.

---

## Step 2 — Install the cron entry

`upload_instagram.py` is cron-invoked, on the same cadence as
`upload_facebook.py`. Add it to `crontab -e` alongside the existing entries:

```cron
* * * * * /usr/local/bin/python3 /path/to/fieldkit/platform/photo-agent/scripts/upload_instagram.py --source cron >> /path/to/fieldkit/logs/cron.log 2>&1
```

The two upload scripts are independent: separate state files, separate lock
files, separate claim namespaces. Neither serializes against the other, and
neither can block, retry, or roll back the other's post.

A tick with nothing to do — no pending job, a claim declined, or Instagram not
configured for this client — exits `0` silently and costs nothing.

---

## Required environment variables

All in `clients/<client>/src/photo-agent/.env`:

| Variable | Purpose | Feature |
|---|---|---|
| `IG_BUSINESS_ACCOUNT_ID` | Target Instagram account; absent = Instagram off | 005 (new) |
| `FB_PAGE_ACCESS_TOKEN` | Reused for every Instagram Graph API call | 003 |
| `FB_PAGE_ID` | Read by `check_instagram_connection.py` | 003 |
| `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID` | Success/failure notifications | 001/002 |
| `DRIVE_ROOT_FOLDER_ID` | Where the temporary share link's file is uploaded | 002 |
| `FIELDKIT_DATA_DIR` | Holds `instagram_state.json` and `upload_instagram.lock` | platform |
| `FIELDKIT_LOG_DIR` | Holds `photo-agent.log` | platform |

---

## How a publish actually works (and why Drive is involved)

Instagram's content-publishing endpoint does **not** accept uploaded bytes. It
takes a `video_url` that Instagram's own servers fetch, and it ingests video
asynchronously. So each attempt is:

```
create temporary Drive share link   (drive.create_temporary_share_link)
        ↓
POST /{ig_user_id}/media            (create container, media_type=REELS)
        ↓
GET  /{container_id}?fields=status_code   ← poll every 5s, cap 300s
        ↓  FINISHED
POST /{ig_user_id}/media_publish    (publish)
        ↓
revoke the Drive share link         (drive.revoke_share_link)
```

**About the share link.** The Mac Mini has no public web server, so the approved
video is briefly published through Drive — the framework's already-sanctioned
host for client-approved media. The exposure is deliberately bounded:

- it covers exactly one already-approved video
- it is the **same metadata-stripped asset the Facebook upload posts** — the video
  is never re-processed or re-encoded for Instagram
- the link is created immediately before the container call and revoked on
  **every** exit path: success, transient failure, and token expiry

If a revoke ever fails, it is logged as an error (`failed to revoke temporary
share link — video may remain publicly reachable`). Grep for that string when
auditing; it is the one condition that leaves a video shared.

**The local video file is not deleted here.** `upload_facebook.py` owns that
cleanup after its own successful post. Deleting it from the Instagram script
would break the Facebook upload for the same approval.

---

## Retries and failure handling

Handled by `instagram_state.claim_pending_upload()`, not by hand-rolled timing
in the script:

- **3 attempts**, with a **60-second cooldown** between them
- A transient failure releases the claim; the next cron tick retries
- After the 3rd failure: the job is marked failed, `IG_EXHAUSTED` is logged, and
  the owner gets `⚠️ Instagram upload failed for <project> after 3 attempts`
- **Token expiry is terminal after one attempt** — retrying can't fix it, so the
  owner is alerted immediately to reconnect the Page
- A container stuck in processing past 300s is treated as an ordinary transient
  failure and retried

---

## Logs

Everything lands in `$FIELDKIT_LOG_DIR/photo-agent.log`, the same per-client file
as every other pipeline event, in the same pipe-delimited format:

| Event | Meaning |
|---|---|
| `IG_ENQUEUED` | An approval enqueued an Instagram job |
| `IG_STARTED` | An upload attempt began (with attempt number) |
| `IG_CONT_NEW` | Media container created |
| `IG_CONT_RDY` | Container finished processing, ready to publish |
| `IG_PUBLISHED` | Reel published (with post ID) |
| `IG_FAILED` | One attempt failed (retryable, with error detail) |
| `IG_EXHAUSTED` | All 3 attempts consumed — terminal |
| `IG_TOKEN_EXP` | Page token invalid/expired — reconnect needed |

No token value or PII is ever written to the log: none of the logging functions
even accepts a token argument.

State lives in `$FIELDKIT_DATA_DIR/photo-agent/instagram_state.json` — a separate
file from `facebook_state.json`.

---

## A note on Hermes skills

**Neither of Feature 005's scripts gets a `SKILL.md`, deliberately.** This matches
how Feature 003's equivalents are handled, and it is why their usage is
documented here instead:

- `upload_instagram.py` is cron-only, exactly like `upload_facebook.py` — which
  has no entry under `platform/photo-agent/skills/` either. The owner never
  invokes it; cron does.
- `check_instagram_connection.py` is a one-time **admin** CLI, like
  `generate_auth_link.py` — also not a skill. The business owner never runs it.

The only Hermes skills in the photo-agent are the ones an owner actually types:
`process-photos`, `photo-approve`, `photo-reject`. Adding a skill for either
script here would expose an operator tool as an owner-facing command.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `check_instagram_connection.py` exits 3, "No Instagram account is linked" | Prerequisite 3 not done — link the account to the Page in Meta's settings |
| Exits 3, "is a PERSONAL account" | Prerequisite 2 not done — convert to Business/Creator in the Instagram app |
| Exits 1, "token is invalid or expired" | Re-run `generate_auth_link.py` to reconnect the Page |
| Nothing happens on approval; no `IG_ENQUEUED` in the log | `IG_BUSINESS_ACCOUNT_ID` not set for this client — run step 1 |
| `IG_FAILED` with "did not finish processing" | Container stuck past 300s; retried automatically, often a large or oddly-encoded video |
| `IG_TOKEN_EXP` | Page token expired — the Facebook upload will be failing too; reconnect once, fixes both |
| Facebook posted but Instagram didn't (or vice versa) | Expected and by design — the two are independent (FR-013). Check the log for that platform's own events |
